"""Water heater entity: values, commands, and no deprecated-API warnings."""
from __future__ import annotations

import logging

import pytest
from homeassistant.components.water_heater import DOMAIN as WH_DOMAIN
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermann import api
from custom_components.thermann.const import DOMAIN

from .conftest import BASE_DATA, STATUS

ENTITY = "water_heater.boiler"


@pytest.fixture
async def loaded(hass, fake_client, caplog):
    caplog.set_level(logging.WARNING)
    entry = MockConfigEntry(
        domain=DOMAIN, data={**BASE_DATA, "access_token": "a", "refresh_token": "r"}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_state_and_attributes(hass, loaded):
    state = hass.states.get(ENTITY)
    assert state.state == "manual"
    assert state.attributes["current_temperature"] == 55
    assert state.attributes["temperature"] == 60
    assert state.attributes["min_temp"] == 20
    assert state.attributes["max_temp"] == 70


async def test_no_deprecation_warnings(hass, loaded, caplog):
    hass.states.get(ENTITY)  # state already written during setup
    deprecated = [r.getMessage() for r in caplog.records if "deprecated" in r.getMessage()]
    assert deprecated == []


async def test_set_temperature_calls_api(hass, loaded, fake_client):
    await hass.services.async_call(
        WH_DOMAIN, "set_temperature", {"entity_id": ENTITY, "temperature": 65}, blocking=True
    )
    assert "set_temperature" in fake_client.calls


async def test_failed_command_raises(hass, loaded, fake_client):
    def fail(c):
        raise api.ThermowattApiError("500")

    fake_client.behaviour["set_mode"] = fail
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            WH_DOMAIN, "set_operation_mode", {"entity_id": ENTITY, "operation_mode": "eco"}, blocking=True
        )


async def test_limits_come_from_the_device(hass, loaded, fake_client):
    fake_client.behaviour["status"] = lambda c: {
        **STATUS, "T_set_min": "30", "T_set_max": "65"
    }
    await hass.data[DOMAIN][loaded.entry_id].async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert (state.attributes["min_temp"], state.attributes["max_temp"]) == (30, 65)


async def test_out_of_range_setpoint_rejected_without_api_call(hass, loaded, fake_client):
    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            WH_DOMAIN, "set_temperature", {"entity_id": ENTITY, "temperature": 75}, blocking=True
        )
    assert "set_temperature" not in fake_client.calls


async def test_extra_attributes(hass, loaded):
    attrs = hass.states.get(ENTITY).attributes
    assert attrs["desired_temperature"] == 60
    assert attrs["schedule_temperature"] == 45
    assert attrs["boost_temperature"] == 70
    assert attrs["frost_protection_temperature"] == 16
    assert attrs["demand_response_status"] == 0


async def test_device_page_shows_firmware(hass, loaded):
    from homeassistant.helpers import device_registry as dr

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), loaded.entry_id)
    assert device.sw_version == "07.00.03.04"
    assert device.hw_version == "Wi-Fi module 01.02.03"
    assert device.model_id == "TMEC"
    assert device.serial_number == "SN1"
