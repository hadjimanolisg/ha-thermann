"""Comfort temperature for Thermann Smart: auto mode, outside scheduled hours.

The app's "T comfort auto" (TComfort from /waterheater-settings, read once at
setup). Changed the way the app's settings page does it: the setting is
stored (POST /tcomfort) and the schedule's lowest temperature replaced
(3 requests). Only usable in auto mode, where the heater follows the
schedule. The scheduled-hours temperature is the water heater's own target
in auto mode.
"""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ThermowattCoordinator
from .api import ThermowattApiError
from .const import DOMAIN, MIN_TEMP
from .entity import ThermannEntity, as_float, as_int, decode_mode
from .schedule import ScheduleError


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ThermowattCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ThermannComfortTemperature(coordinator, serial) for serial in coordinator.devices)


class ThermannComfortTemperature(ThermannEntity, NumberEntity):
    """The auto mode's temperature outside scheduled hours ("T comfort auto")."""

    _attr_translation_key = "comfort_temperature"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 1

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        # Same unique ID as 0.9.8's number, so existing entities carry over.
        super().__init__(coordinator, serial, "comfort_temperature")

    @property
    def available(self) -> bool:
        # Only meaningful (and only offered) in auto mode.
        return super().available and decode_mode(self.status.get("Cmd")) == "auto"

    @property
    def native_value(self) -> float | None:
        # Read once at setup; unknown if that read failed (e.g. rate limited).
        return as_float(self.coordinator.settings.get(self._serial, {}).get("TComfort"))

    @property
    def native_min_value(self) -> float:
        return as_int(self.status.get("T_set_min")) or MIN_TEMP

    @property
    def native_max_value(self) -> float:
        # Must stay below the scheduled temperature.
        scheduled = as_int(self.status.get("Tmsch"))
        top = as_int(self.status.get("T_set_max")) or 70
        return min(top, scheduled - 1) if scheduled is not None else top

    async def async_set_native_value(self, value: float) -> None:
        temp = int(value)
        if decode_mode(self.status.get("Cmd")) != "auto":
            raise ServiceValidationError("The comfort temperature can only be changed in auto mode.")
        if not self.native_min_value <= temp <= self.native_max_value:
            raise ServiceValidationError(
                f"{temp} °C is outside the allowed range "
                f"({self.native_min_value:g}-{self.native_max_value:g} °C; "
                "it must stay below the scheduled temperature)"
            )
        if self.coordinator.rate_limited:
            # Don't spend more of an exhausted quota; nothing is queued.
            raise HomeAssistantError(
                "Thermowatt is rate limiting this account; commands are paused "
                "until a status poll succeeds again. Try later or use the app."
            )
        old = as_int(self.native_value)
        try:
            await self.hass.async_add_executor_job(
                self.coordinator.client.set_comfort_temperature, self._serial, temp
            )
        except ScheduleError as err:
            raise ServiceValidationError(str(err)) from err
        except ThermowattApiError as err:
            raise HomeAssistantError(f"Thermowatt command failed: {err}") from err
        self.coordinator.settings.setdefault(self._serial, {})["TComfort"] = temp
        if self.status and old is not None and as_int(self.status.get("T_dsrd")) == old:
            # Outside scheduled hours the current target follows it.
            self.status["T_dsrd"] = str(temp)
        # Refreshes this and the water heater (comfort attribute, minimum).
        self.coordinator.async_update_listeners()
