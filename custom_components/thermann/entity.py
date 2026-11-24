"""Base entity shared by all Thermann Smart platforms."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ThermowattCoordinator
from .const import DOMAIN


def as_int(value: Any) -> int | None:
    """The API sends every value as a string ("38"); convert, or None."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ThermannEntity(CoordinatorEntity[ThermowattCoordinator]):
    """One entity of one heater. Status comes from the shared /status poll."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ThermowattCoordinator, serial: str, key: str | None) -> None:
        super().__init__(coordinator)
        self._serial = serial
        if key is not None:
            self._attr_unique_id = f"thermann_{serial}_{key}"
        status = coordinator.data.get(serial, {}) if coordinator.data else {}
        firmware = status.get("Tmec_SW_Version")
        wifi_firmware = status.get("Wifi_SW_Version")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="Thermowatt",
            name=coordinator.devices[serial],
            model="Thermann Smart Electric",
            model_id=coordinator.device_types.get(serial),
            sw_version=firmware,
            hw_version=f"Wi-Fi module {wifi_firmware}" if wifi_firmware else None,
            serial_number=serial,
        )

    @property
    def status(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._serial, {})
