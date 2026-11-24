"""Sensor platform for Thermann Smart: temperatures, estimated energy, Wi-Fi signal."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import REQUEST_KINDS

from . import ThermowattCoordinator
from .const import CONF_ELEMENT_KW, DEFAULT_ELEMENT_KW, DOMAIN
from .entity import ThermannEntity, as_float, as_int


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ThermowattCoordinator = hass.data[DOMAIN][entry.entry_id]
    element_kw = entry.options.get(CONF_ELEMENT_KW, DEFAULT_ELEMENT_KW)

    entities: list[SensorEntity] = []
    for serial in coordinator.devices:
        entities += [
            ThermannTemperatureSensor(coordinator, serial),
            ThermannEnergySensor(coordinator, serial, element_kw),
            ThermannAmbientTemperatureSensor(coordinator, serial),
            ThermannWifiSignalSensor(coordinator, serial),
            ThermannTimeRemainingSensor(coordinator, serial),
        ]
    # Requests are per account, so one sensor, on the first heater's device.
    if coordinator.devices:
        entities.append(ThermannApiRequestsSensor(coordinator, next(iter(coordinator.devices))))
    async_add_entities(entities)


class ThermannTemperatureSensor(ThermannEntity, SensorEntity):
    """Current tank temperature as a standalone sensor."""

    _attr_translation_key = "current_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "current_temperature")

    @property
    def native_value(self) -> float | None:
        return as_float(self.status.get("T_Avg"))


class ThermannAmbientTemperatureSensor(ThermannEntity, SensorEntity):
    """TAmb from /status: the temperature around the heater / its electronics."""

    _attr_translation_key = "ambient_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "ambient_temperature")

    @property
    def native_value(self) -> float | None:
        return as_float(self.status.get("TAmb"))


class ThermannWifiSignalSensor(ThermannEntity, SensorEntity):
    """Wi-Fi signal strength (Rssi). Disabled by default, as is usual for RSSI."""

    _attr_translation_key = "wifi_signal"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "wifi_signal")

    @property
    def native_value(self) -> int | None:
        return as_int(self.status.get("Rssi"))


class ThermannTimeRemainingSensor(ThermannEntity, SensorEntity):
    """TimeRemaining from /status, in minutes.

    Read 20 while heating 40 -> 46 C in manual and 0 when idle, so it looks
    like the heater's estimate of time left to reach the target. Not yet
    confirmed to count down; the name stays neutral until it is.
    """

    _attr_translation_key = "time_remaining"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "time_remaining")

    @property
    def native_value(self) -> int | None:
        return as_int(self.status.get("TimeRemaining"))


class ThermannApiRequestsSensor(ThermannEntity, SensorEntity):
    """Requests this integration sent to the Thermowatt cloud in the last 24 h.

    The account's daily quota (about 500, shared with the app) is the reason
    this exists: compare it with the app's daily-limit warnings. Counted
    locally, so it costs no requests; restarts with Home Assistant.
    """

    _attr_translation_key = "api_requests"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "requests"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-upload-outline"

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "api_requests")

    @property
    def available(self) -> bool:
        # A local count: still meaningful (most of all) while polls fail.
        return True

    @property
    def native_value(self) -> int:
        return self.coordinator.client.request_counts()["total"]

    @property
    def extra_state_attributes(self) -> dict:
        counts = self.coordinator.client.request_counts()
        since = dt_util.utc_from_timestamp(self.coordinator.client.counting_since)
        return {
            **{kind: counts[kind] for kind in REQUEST_KINDS},
            "counting_since": since.isoformat(),
            # False until 24 h have passed since Home Assistant started.
            "full_day": dt_util.utcnow() - since >= timedelta(days=1),
        }


class ThermannEnergySensor(ThermannEntity, RestoreSensor):
    """Estimated cumulative energy use, integrated from heating on/off time * element kW.

    This is an estimate, not a measurement from a real power meter: it assumes
    the element draws a constant `element_kw` (set in the integration's
    Configure/Options screen) for the entire time the cloud reports it as
    heating. Time while polls fail is not counted (it may under-count after an
    outage, never over-count). Suitable for rough tracking, not billing.
    """

    _attr_translation_key = "energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator: ThermowattCoordinator, serial: str, element_kw: float) -> None:
        super().__init__(coordinator, serial, "energy")
        self._element_kw = element_kw
        self._total_kwh = 0.0
        self._last_update = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_sensor_data()
        if last_data is not None and last_data.native_value is not None:
            restored = as_float(last_data.native_value)
            self._total_kwh = restored if restored is not None else 0.0
        self._last_update = dt_util.utcnow()

    @property
    def native_value(self) -> float:
        return round(self._total_kwh, 3)

    @property
    def extra_state_attributes(self) -> dict:
        return {"element_kw": self._element_kw}

    @callback
    def _handle_coordinator_update(self) -> None:
        # Only a successful poll is evidence of the heating state. Failed polls
        # (outage, rate limit) reuse the old data, so they add nothing, and the
        # gap to the next good poll is capped at twice the poll interval: after
        # an hour-long quota backoff one "heating" reading must not count as an
        # hour of heating.
        if self.coordinator.last_update_success:
            now = dt_util.utcnow()
            if self._last_update is not None:
                cap = 2 * self.coordinator.base_interval.total_seconds()
                elapsed = min((now - self._last_update).total_seconds(), cap)
                if elapsed > 0 and (as_int(self.status.get("WaterHeaterSts")) or 0) & 1:
                    self._total_kwh += self._element_kw * elapsed / 3600
            self._last_update = now
        super()._handle_coordinator_update()
