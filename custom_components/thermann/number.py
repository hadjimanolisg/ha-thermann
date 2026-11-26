"""Number controls for Thermann Smart: the auto mode's two temperatures.

- Scheduled temperature: what the heater keeps during the schedule's hours
  (Tmsch). Changed the way the app does it: the schedule is read, its
  highest temperature replaced and the schedule sent back (2 requests).
- Comfort temperature: what it keeps outside them ("T comfort auto",
  TComfort from /waterheater-settings, read once at setup).

The schedule's hours themselves are never changed; use the app for that.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ThermowattCoordinator
from .api import ThermowattApiError
from .const import DOMAIN, MAX_TEMP, MIN_TEMP
from .entity import ThermannEntity, as_float, as_int, decode_mode
from .schedule import ScheduleError


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ThermowattCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = []
    for serial in coordinator.devices:
        entities += [
            ThermannScheduledTemperature(coordinator, serial),
            ThermannComfortTemperature(coordinator, serial),
        ]
    async_add_entities(entities)


class _AutoTemperature(ThermannEntity, NumberEntity):
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 1

    @property
    def native_min_value(self) -> float:
        return as_int(self.status.get("T_set_min")) or MIN_TEMP

    @property
    def native_max_value(self) -> float:
        return as_int(self.status.get("T_set_max")) or MAX_TEMP

    def _check_range(self, temp: int) -> None:
        if not self.native_min_value <= temp <= self.native_max_value:
            raise ServiceValidationError(
                f"{temp} °C is outside this heater's range "
                f"({self.native_min_value:g}-{self.native_max_value:g} °C)"
            )

    async def _async_send(self, func: Callable[..., Any], *args: Any) -> Any:
        if self.coordinator.rate_limited:
            # Don't spend more of an exhausted quota; nothing is queued.
            raise HomeAssistantError(
                "Thermowatt is rate limiting this account; commands are paused "
                "until a status poll succeeds again. Try later or use the app."
            )
        try:
            return await self.hass.async_add_executor_job(func, *args)
        except ScheduleError as err:
            raise ServiceValidationError(str(err)) from err
        except ThermowattApiError as err:
            raise HomeAssistantError(f"Thermowatt command failed: {err}") from err


class ThermannScheduledTemperature(_AutoTemperature):
    """The auto mode's temperature during scheduled hours (Tmsch)."""

    _attr_translation_key = "scheduled_temperature"

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "scheduled_temperature")

    @property
    def native_value(self) -> float | None:
        return as_float(self.status.get("Tmsch"))

    async def async_set_native_value(self, value: float) -> None:
        temp = int(value)
        if decode_mode(self.status.get("Cmd")) != "auto":
            # Sending the schedule also selects auto mode, as in the app.
            raise ServiceValidationError(
                "The scheduled temperature can only be changed in auto mode: "
                "sending it also switches the heater to auto. Switch to auto first."
            )
        self._check_range(temp)
        await self._async_send(self.coordinator.client.set_scheduled_temperature, self._serial, temp)
        # Show it now; the next poll replaces this with what the heater reports.
        old = as_int(self.status.get("Tmsch"))
        changes = {"Tmsch": str(temp)}
        if old is not None and as_int(self.status.get("T_dsrd")) == old:
            # Inside scheduled hours the current target follows it.
            changes["T_dsrd"] = str(temp)
        if self.status:
            self.status.update(changes)
            self.coordinator.async_update_listeners()


class ThermannComfortTemperature(_AutoTemperature):
    """The auto mode's temperature outside scheduled hours ("T comfort auto")."""

    _attr_translation_key = "comfort_temperature"

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "comfort_temperature")

    @property
    def native_value(self) -> float | None:
        # Read once at setup; unknown if that read failed (e.g. rate limited).
        return as_float(self.coordinator.settings.get(self._serial, {}).get("TComfort"))

    async def async_set_native_value(self, value: float) -> None:
        temp = int(value)
        self._check_range(temp)
        scheduled = as_int(self.status.get("Tmsch"))
        if scheduled is not None and temp >= scheduled:
            raise ServiceValidationError(
                f"The comfort temperature must be below the scheduled temperature ({scheduled} °C)"
            )
        await self._async_send(self.coordinator.client.set_comfort_temperature, self._serial, temp)
        self.coordinator.settings.setdefault(self._serial, {})["TComfort"] = temp
        self.async_write_ha_state()
