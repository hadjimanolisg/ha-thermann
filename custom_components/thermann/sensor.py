"""Sensor platform for Thermann Smart: temperatures, estimated energy, Wi-Fi signal."""
from __future__ import annotations

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
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

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
        ]
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


class ThermannEnergySensor(ThermannEntity, RestoreSensor):
    """Estimated cumulative energy use, integrated from heating on/off time * element kW.

    This is an estimate, not a measurement from a real power meter: it assumes
    the element draws a constant `element_kw` (set in the integration's
    Configure/Options screen) for the entire time the cloud reports it as
    heating. Suitable for rough tracking / the Energy dashboard, not billing.
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
        now = dt_util.utcnow()
        if self._last_update is not None:
            elapsed_hours = (now - self._last_update).total_seconds() / 3600
            if (as_int(self.status.get("WaterHeaterSts")) or 0) & 1:
                self._total_kwh += self._element_kw * elapsed_hours
        self._last_update = now
        super()._handle_coordinator_update()
