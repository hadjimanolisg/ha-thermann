"""Binary sensors for Thermann Smart: element heating, and heater fault."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ThermowattCoordinator
from .const import DOMAIN
from .entity import ThermannEntity, as_int


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ThermowattCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []
    for serial in coordinator.devices:
        entities += [
            ThermannHeatingBinarySensor(coordinator, serial),
            ThermannFaultBinarySensor(coordinator, serial),
        ]
    async_add_entities(entities)


class ThermannHeatingBinarySensor(ThermannEntity, BinarySensorEntity):
    """True while the heating element is actively on (WaterHeaterSts bit 0)."""

    _attr_translation_key = "heating"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "heating")

    @property
    def is_on(self) -> bool | None:
        sts = as_int(self.status.get("WaterHeaterSts"))
        return bool(sts & 1) if sts is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Raw value kept visible: only bit 0 is understood so far.
        return {"status_flags": as_int(self.status.get("WaterHeaterSts"))}


class ThermannFaultBinarySensor(ThermannEntity, BinarySensorEntity):
    """On when the heater reports any error code."""

    _attr_translation_key = "fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "fault")

    def _codes(self) -> tuple[int | None, int | None]:
        return (
            as_int(self.status.get("VolatileErrors")),
            as_int(self.status.get("NonVolatileErrors")),
        )

    @property
    def is_on(self) -> bool | None:
        codes = self._codes()
        if all(code is None for code in codes):
            return None
        return any(code for code in codes)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        volatile, non_volatile = self._codes()
        return {"volatile_errors": volatile, "non_volatile_errors": non_volatile}
