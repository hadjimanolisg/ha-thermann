"""Water heater platform for Thermann Smart."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ThermowattCoordinator
from .api import ThermowattApiError
from .const import CMD_FAMILIES, CMD_ON_BIT, DEFAULT_TEMP, DOMAIN, MAX_TEMP, MIN_TEMP, OPERATION_MODES
from .entity import ThermannEntity, as_float, as_int, decode_mode

# Mode -> Cmd value with the on bit set (the inverse of decode_mode).
MODE_CMD = {mode: family | CMD_ON_BIT for family, mode in CMD_FAMILIES.items()}

_LOGGER = logging.getLogger(__name__)

# Extra /status fields shown as attributes. Meanings confirmed against the
# app where noted; the others are best guesses from observed values.
EXTRA_ATTRIBUTES = {
    # What the heater is aiming for right now (confirmed): the setpoint in
    # manual; in auto, the schedule temperature inside scheduled hours and
    # "T comfort auto" (e.g. 20) outside them.
    "desired_temperature": "T_dsrd",
    # The temperature manual mode uses (confirmed: changes with /manual).
    "manual_setpoint": "T_SetPoint",
    "schedule_comfort_temperature": "Tmsch",
    "boost_temperature": "TBoost",
    "time_remaining": "TimeRemaining",
    "frost_protection_temperature": "T_set_off",
    "demand_response_status": "Drm_status",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ThermowattCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ThermowattWaterHeater(coordinator, serial) for serial in coordinator.devices)


# Home Assistant 2026.11 renamed the water heater temperature properties to
# native_* and deprecated the old names (removed in 2027.11). Older versions
# only read the old names, so on those we add them as aliases. Defining the
# old names on 2026.11+ is what triggers the deprecation warning, so the
# aliases are only mixed in when the base class has no native_* support.
NATIVE_TEMPERATURES = hasattr(WaterHeaterEntity, "native_current_temperature")


class _LegacyTemperatureNames:
    """Old property names for Home Assistant < 2026.11."""

    @property
    def temperature_unit(self) -> str:
        return self.native_temperature_unit

    @property
    def current_temperature(self) -> float | None:
        return self.native_current_temperature

    @property
    def target_temperature(self) -> float | None:
        return self.native_target_temperature


_COMPAT_BASES: tuple[type, ...] = () if NATIVE_TEMPERATURES else (_LegacyTemperatureNames,)


class ThermowattWaterHeater(*_COMPAT_BASES, ThermannEntity, WaterHeaterEntity):
    """Representation of one Thermowatt/Thermann Smart boiler."""

    _attr_name = None
    # Names for all five modes (translations/en.json). HA only translates its
    # own modes (off, eco); without this manual/auto/holiday show raw.
    _attr_translation_key = "heater"
    _attr_operation_list = OPERATION_MODES

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, key=None)
        # Kept from earlier versions so existing entities keep their history.
        self._attr_unique_id = f"thermowatt_{serial}"
        self._last_setpoint = DEFAULT_TEMP

    @property
    def native_temperature_unit(self) -> str:
        return UnitOfTemperature.CELSIUS

    @property
    def min_temp(self) -> float:
        """The heater reports its own limits; fall back if it doesn't."""
        return as_int(self.status.get("T_set_min")) or MIN_TEMP

    @property
    def max_temp(self) -> float:
        return as_int(self.status.get("T_set_max")) or MAX_TEMP

    @property
    def native_current_temperature(self) -> float | None:
        return as_float(self.status.get("T_Avg"))

    @property
    def native_target_temperature(self) -> float | None:
        """What the heater is aiming for now, in every mode (T_dsrd).

        In auto that follows the schedule, so it can differ from the manual
        setpoint. Falls back to T_SetPoint if the heater doesn't report it.
        """
        desired = as_float(self.status.get("T_dsrd"))
        return desired if desired is not None else as_float(self.status.get("T_SetPoint"))

    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        """Offer the temperature control only in manual mode.

        The API sets the temperature through /manual, which also switches the
        heater to manual. Hiding the control in other modes stops Home
        Assistant from quietly taking the heater off its schedule.
        """
        features = WaterHeaterEntityFeature.OPERATION_MODE
        if self.current_operation == "manual":
            features |= WaterHeaterEntityFeature.TARGET_TEMPERATURE
        return features

    @property
    def current_operation(self) -> str | None:
        return decode_mode(self.status.get("Cmd"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            name: as_float(self.status.get(field))
            for name, field in EXTRA_ATTRIBUTES.items()
            if self.status.get(field) is not None
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        # Remember the last real setpoint so switching back to "manual"
        # restores it. Done here rather than in a property getter.
        setpoint = as_int(self.status.get("T_SetPoint"))
        if setpoint is not None:
            self._last_setpoint = setpoint
        super()._handle_coordinator_update()

    async def _async_send(self, func, *args) -> None:
        if self.coordinator.rate_limited:
            # Don't spend more of an exhausted quota; nothing is queued.
            raise HomeAssistantError(
                "Thermowatt is rate limiting this account; commands are paused "
                "until a status poll succeeds again. Try later or use the app."
            )
        try:
            await self.hass.async_add_executor_job(func, *args)
        except ThermowattApiError as err:
            raise HomeAssistantError(f"Thermowatt command failed: {err}") from err
        # No status poll here: right after a command the cloud still reports
        # the old state, so a poll would cost a request and show stale values.

    @callback
    def _async_show_commanded(self, changes: dict[str, str]) -> None:
        """Show an accepted command now instead of after the next poll.

        Patches this heater's last /status in place (values as strings, like
        the API). The next poll replaces it with what the heater reports, so
        a command the heater didn't apply is corrected then.
        """
        status = self.status
        if not status:
            return
        status.update(changes)
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = int(kwargs[ATTR_TEMPERATURE])
        if self.current_operation != "manual":
            # Automations and scripts can still call the service directly.
            raise ServiceValidationError(
                "The temperature can only be set in manual mode: setting it "
                "switches the heater to manual. Switch to manual first."
            )
        if not self.min_temp <= temp <= self.max_temp:
            raise ServiceValidationError(
                f"{temp} °C is outside this heater's range "
                f"({self.min_temp:g}-{self.max_temp:g} °C)"
            )
        await self._async_send(self.coordinator.client.set_temperature, self._serial, temp)
        self._last_setpoint = temp
        self._async_show_commanded(
            {"Cmd": str(MODE_CMD["manual"]), "T_SetPoint": str(temp), "T_dsrd": str(temp)}
        )

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        setpoint = self._last_setpoint
        await self._async_send(
            self.coordinator.client.set_mode, self._serial, operation_mode, setpoint
        )
        if operation_mode == "manual":
            self._async_show_commanded(
                {"Cmd": str(MODE_CMD["manual"]), "T_SetPoint": str(setpoint), "T_dsrd": str(setpoint)}
            )
        elif operation_mode in MODE_CMD:
            # The target in eco/auto/holiday comes from the heater; leave it
            # until the next poll.
            self._async_show_commanded({"Cmd": str(MODE_CMD[operation_mode])})
        elif operation_mode == "off":
            # Off = the current family with the on bit cleared. If the current
            # Cmd is unknown, don't guess; the next poll shows it.
            cmd = as_int(self.status.get("Cmd"))
            if cmd is not None and (cmd & ~CMD_ON_BIT) in CMD_FAMILIES:
                self._async_show_commanded({"Cmd": str(cmd & ~CMD_ON_BIT)})
