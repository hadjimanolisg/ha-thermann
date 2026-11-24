"""Sensors and binary sensors built from /status."""
from __future__ import annotations

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermann.const import DOMAIN

from .conftest import BASE_DATA, STATUS


@pytest.fixture
async def loaded(hass, fake_client):
    entry = MockConfigEntry(domain=DOMAIN, data={**BASE_DATA, "access_token": "a", "refresh_token": "r"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def refresh_with(hass, entry, fake_client, **overrides):
    fake_client.behaviour["status"] = lambda c: {**STATUS, **overrides}
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()


async def test_existing_entity_ids_unchanged(hass, loaded):
    registry = er.async_get(hass)
    for unique_id in ("thermowatt_SN1", "thermann_SN1_current_temperature", "thermann_SN1_energy", "thermann_SN1_heating"):
        assert any(e.unique_id == unique_id for e in registry.entities.values()), unique_id


async def test_temperature_and_ambient(hass, loaded):
    assert hass.states.get("sensor.boiler_temperature").state == "55.0"
    assert hass.states.get("sensor.boiler_ambient_temperature").state == "33.0"


async def test_wifi_signal_disabled_by_default(hass, loaded):
    entry = er.async_get(hass).async_get("sensor.boiler_wi_fi_signal")
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_fault_off_then_on(hass, loaded, fake_client):
    state = hass.states.get("binary_sensor.boiler_fault")
    assert state.state == "off"
    assert state.attributes["volatile_errors"] == 0

    await refresh_with(hass, loaded, fake_client, NonVolatileErrors="12")
    state = hass.states.get("binary_sensor.boiler_fault")
    assert state.state == "on"
    assert state.attributes["non_volatile_errors"] == 12


async def test_heating_uses_bit_zero(hass, loaded, fake_client):
    assert hass.states.get("binary_sensor.boiler_heating").state == "on"
    await refresh_with(hass, loaded, fake_client, WaterHeaterSts="32")
    state = hass.states.get("binary_sensor.boiler_heating")
    assert state.state == "off"
    assert state.attributes["status_flags"] == 32


async def test_high_ambient_pause_is_not_a_fault(hass, loaded, fake_client):
    assert hass.states.get("binary_sensor.boiler_high_ambient_pause").state == "off"
    await refresh_with(hass, loaded, fake_client, NonVolatileErrors="2")
    assert hass.states.get("binary_sensor.boiler_high_ambient_pause").state == "on"
    assert hass.states.get("binary_sensor.boiler_fault").state == "off"

    # Pause plus a real error bit: both on.
    await refresh_with(hass, loaded, fake_client, NonVolatileErrors="6")
    assert hass.states.get("binary_sensor.boiler_high_ambient_pause").state == "on"
    assert hass.states.get("binary_sensor.boiler_fault").state == "on"
