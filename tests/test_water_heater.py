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
    assert attrs["scheduled_temperature"] == 45
    assert attrs["manual_setpoint"] == 60
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


async def test_manual_mode_offers_temperature_control(hass, loaded):
    from homeassistant.components.water_heater import WaterHeaterEntityFeature as F

    features = hass.states.get(ENTITY).attributes["supported_features"]
    assert features == F.TARGET_TEMPERATURE | F.OPERATION_MODE


async def test_auto_mode_follows_schedule_and_hides_control(hass, loaded, fake_client):
    """Auto outside scheduled hours: heater aims for T comfort auto (20)."""
    from homeassistant.components.water_heater import WaterHeaterEntityFeature as F
    from homeassistant.exceptions import ServiceValidationError

    fake_client.behaviour["status"] = lambda c: {**STATUS, "Cmd": "17", "T_SetPoint": "45", "T_dsrd": "20"}
    await hass.data[DOMAIN][loaded.entry_id].async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY)
    assert state.state == "auto"
    assert state.attributes["temperature"] == 20
    assert state.attributes["manual_setpoint"] == 45
    assert state.attributes["supported_features"] == F.OPERATION_MODE

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            WH_DOMAIN, "set_temperature", {"entity_id": ENTITY, "temperature": 50}, blocking=True
        )
    assert "set_temperature" not in fake_client.calls


async def test_target_falls_back_to_setpoint(hass, loaded, fake_client):
    status = {k: v for k, v in STATUS.items() if k != "T_dsrd"}
    fake_client.behaviour["status"] = lambda c: dict(status)
    await hass.data[DOMAIN][loaded.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["temperature"] == 60


async def test_switching_to_manual_restores_setpoint(hass, loaded, fake_client):
    seen = []
    fake_client.behaviour["status"] = lambda c: {**STATUS, "Cmd": "17", "T_SetPoint": "47", "T_dsrd": "20"}
    await hass.data[DOMAIN][loaded.entry_id].async_refresh()
    await hass.async_block_till_done()
    original = fake_client.set_mode
    fake_client.set_mode = lambda self, serial, mode, setpoint=60: seen.append((mode, setpoint))
    try:
        await hass.services.async_call(
            WH_DOMAIN, "set_operation_mode", {"entity_id": ENTITY, "operation_mode": "manual"}, blocking=True
        )
    finally:
        fake_client.set_mode = original
    assert seen == [("manual", 47)]


@pytest.mark.parametrize(
    ("cmd", "mode"),
    [("9", "manual"), ("3", "eco"), ("17", "auto"), ("65", "holiday"),
     ("8", "off"), ("16", "off"), ("64", "off"), ("2", "off")],
)
async def test_cmd_bit_field_decoding(hass, loaded, fake_client, cmd, mode):
    fake_client.behaviour["status"] = lambda c: {**STATUS, "Cmd": cmd}
    await hass.data[DOMAIN][loaded.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == mode


async def test_unknown_cmd_is_not_guessed(hass, loaded, fake_client):
    fake_client.behaviour["status"] = lambda c: {**STATUS, "Cmd": "0"}
    await hass.data[DOMAIN][loaded.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unknown"


async def test_commands_refused_while_rate_limited(hass, loaded, fake_client):
    hass.data[DOMAIN][loaded.entry_id].rate_limited = True
    with pytest.raises(HomeAssistantError, match="rate limiting"):
        await hass.services.async_call(
            WH_DOMAIN, "set_operation_mode", {"entity_id": ENTITY, "operation_mode": "eco"}, blocking=True
        )
    assert "set_mode" not in fake_client.calls


async def test_set_temperature_shows_at_once_without_a_poll(hass, loaded, fake_client):
    polls = fake_client.calls.count("status")
    await hass.services.async_call(
        WH_DOMAIN, "set_temperature", {"entity_id": ENTITY, "temperature": 65}, blocking=True
    )
    await hass.async_block_till_done()
    attrs = hass.states.get(ENTITY).attributes
    assert attrs["temperature"] == 65
    assert attrs["manual_setpoint"] == 65
    # The cloud reports the old state right after a command; don't poll for it.
    assert fake_client.calls.count("status") == polls


@pytest.mark.parametrize(
    ("mode", "temperature"),
    [("eco", 60), ("auto", 60), ("holiday", 60), ("off", 60)],
)
async def test_mode_change_shows_at_once(hass, loaded, fake_client, mode, temperature):
    polls = fake_client.calls.count("status")
    await hass.services.async_call(
        WH_DOMAIN, "set_operation_mode", {"entity_id": ENTITY, "operation_mode": mode}, blocking=True
    )
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == mode
    # Target in other modes comes from the heater; unchanged until the next poll.
    assert state.attributes["temperature"] == temperature
    assert fake_client.calls.count("status") == polls


async def test_switch_to_manual_shows_restored_setpoint(hass, loaded, fake_client):
    fake_client.behaviour["status"] = lambda c: {**STATUS, "Cmd": "17", "T_SetPoint": "47", "T_dsrd": "20"}
    await hass.data[DOMAIN][loaded.entry_id].async_refresh()
    await hass.async_block_till_done()
    await hass.services.async_call(
        WH_DOMAIN, "set_operation_mode", {"entity_id": ENTITY, "operation_mode": "manual"}, blocking=True
    )
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "manual"
    assert state.attributes["temperature"] == 47


async def test_off_from_unknown_mode_is_not_guessed(hass, loaded, fake_client):
    fake_client.behaviour["status"] = lambda c: {**STATUS, "Cmd": "0"}
    await hass.data[DOMAIN][loaded.entry_id].async_refresh()
    await hass.async_block_till_done()
    await hass.services.async_call(
        WH_DOMAIN, "set_operation_mode", {"entity_id": ENTITY, "operation_mode": "off"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unknown"


async def test_next_poll_replaces_the_commanded_state(hass, loaded, fake_client):
    await hass.services.async_call(
        WH_DOMAIN, "set_operation_mode", {"entity_id": ENTITY, "operation_mode": "eco"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "eco"
    # The heater didn't apply it: the next poll still says manual.
    await hass.data[DOMAIN][loaded.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "manual"


async def test_failed_command_leaves_state_alone(hass, loaded, fake_client):
    def fail(c):
        raise api.ThermowattApiError("500")

    fake_client.behaviour["set_mode"] = fail
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            WH_DOMAIN, "set_operation_mode", {"entity_id": ENTITY, "operation_mode": "eco"}, blocking=True
        )
    assert hass.states.get(ENTITY).state == "manual"


async def test_every_mode_has_a_display_name(hass, loaded):
    from homeassistant.helpers.translation import async_get_translations

    from custom_components.thermann.const import OPERATION_MODES

    strings = await async_get_translations(hass, "en", "entity", [DOMAIN])
    base = f"component.{DOMAIN}.entity.water_heater.heater"
    for mode in OPERATION_MODES:
        assert strings[f"{base}.state.{mode}"] == mode.capitalize()
        assert strings[f"{base}.state_attributes.operation_mode.state.{mode}"] == mode.capitalize()
    # The entity keeps the device's name.
    assert hass.states.get(ENTITY).name == "Boiler"


async def test_old_numbers_are_removed(hass, fake_client):
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(domain=DOMAIN, data={**BASE_DATA, "access_token": "a", "refresh_token": "r"})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in ("scheduled_temperature", "comfort_temperature"):
        registry.async_get_or_create("number", DOMAIN, f"thermann_SN1_{key}", config_entry=entry)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not [e for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                if e.domain == "number"]
    # Setup still makes only the device list and status requests.
    assert fake_client.calls == ["list", "status"]


@pytest.mark.parametrize(("sts", "state"), [("32", "off"), ("48", "on"), ("49", "on"), ("1", "off")])
async def test_boost_sensor(hass, loaded, fake_client, sts, state):
    fake_client.behaviour["status"] = lambda c: {**STATUS, "WaterHeaterSts": sts}
    await hass.data[DOMAIN][loaded.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.boiler_boost").state == state
