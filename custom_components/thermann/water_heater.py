"""Water heater platform for Thermowatt Smart."""
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
from .const import CMD_TO_MODE, DEFAULT_TEMP, DOMAIN, MAX_TEMP, MIN_TEMP, OPERATION_MODES
from .entity import ThermannEntity, as_float, as_int

_LOGGER = logging.getLogger(__name__)

# Extra /status fields shown as attributes. Their exact meaning isn't
# documented; names describe what they appear to be from observed values.
EXTRA_ATTRIBUTES = {
    "desired_temperature": "T_dsrd",
    "schedule_temperature": "Tmsch",
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
    _attr_operation_list = OPERATION_MODES
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
    )

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
        return as_float(self.status.get("T_SetPoint"))

    @property
    def current_operation(self) -> str | None:
        cmd = as_int(self.status.get("Cmd"))
        return CMD_TO_MODE.get(cmd) if cmd is not None else None

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
        try:
            await self.hass.async_add_executor_job(func, *args)
        except ThermowattApiError as err:
            raise HomeAssistantError(f"Thermowatt command failed: {err}") from err
        # Debounced by the coordinator, so rapid changes don't each poll.
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = int(kwargs[ATTR_TEMPERATURE])
        if not self.min_temp <= temp <= self.max_temp:
            raise ServiceValidationError(
                f"{temp} °C is outside this heater's range "
                f"({self.min_temp:g}-{self.max_temp:g} °C)"
            )
        await self._async_send(self.coordinator.client.set_temperature, self._serial, temp)
        self._last_setpoint = temp

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        await self._async_send(
            self.coordinator.client.set_mode, self._serial, operation_mode, self._last_setpoint
        )
