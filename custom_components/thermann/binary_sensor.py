"""Binary sensors for Thermann Smart: element heating, heater fault, high ambient pause."""
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

# NonVolatileErrors bit 0x02 coincided with the app's "heating paused, high
# ambient temperature" notice (observed by another project on firmware
# 07.00.09.06). The heater resumes by itself, so it isn't treated as a fault.
HIGH_AMBIENT_PAUSE_BIT = 0x02


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ThermowattCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []
    for serial in coordinator.devices:
        entities += [
            ThermannHeatingBinarySensor(coordinator, serial),
            ThermannFaultBinarySensor(coordinator, serial),
            ThermannHighAmbientPauseBinarySensor(coordinator, serial),
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
        volatile, non_volatile = self._codes()
        if volatile is None and non_volatile is None:
            return None
        # The high-ambient pause bit is reported by its own sensor instead.
        return bool(volatile or ((non_volatile or 0) & ~HIGH_AMBIENT_PAUSE_BIT))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        volatile, non_volatile = self._codes()
        return {"volatile_errors": volatile, "non_volatile_errors": non_volatile}


class ThermannHighAmbientPauseBinarySensor(ThermannEntity, BinarySensorEntity):
    """On while heating is paused because the ambient temperature is too high.

    The heater resumes by itself as it cools down; don't force heating.
    """

    _attr_translation_key = "high_ambient_pause"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ThermowattCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "high_ambient_pause")

    @property
    def is_on(self) -> bool | None:
        non_volatile = as_int(self.status.get("NonVolatileErrors"))
        return bool(non_volatile & HIGH_AMBIENT_PAUSE_BIT) if non_volatile is not None else None
