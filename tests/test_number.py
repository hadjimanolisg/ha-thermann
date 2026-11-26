"""Auto mode temperatures (number entities) and the boost sensor."""
from __future__ import annotations

import pytest
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermann import api
from custom_components.thermann.const import DOMAIN
from custom_components.thermann.schedule import ScheduleError

from .conftest import BASE_DATA, STATUS

SCHEDULED = "number.boiler_scheduled_temperature"
COMFORT = "number.boiler_comfort_temperature"
AUTO = {**STATUS, "Cmd": "17", "T_dsrd": "45", "Tmsch": "45"}


async def _setup(hass, fake_client, status=None):
    if status is not None:
        fake_client.behaviour["status"] = lambda c: dict(status)
    entry = MockConfigEntry(domain=DOMAIN, data={**BASE_DATA, "access_token": "a", "refresh_token": "r"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _set(hass, entity_id, value):
    await hass.services.async_call(
        NUMBER_DOMAIN, "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    await hass.async_block_till_done()


async def test_values_come_from_status_and_settings(hass, fake_client):
    await _setup(hass, fake_client, AUTO)
    assert hass.states.get(SCHEDULED).state == "45.0"
    assert hass.states.get(COMFORT).state == "20.0"
    assert hass.states.get(SCHEDULED).attributes["min"] == 20
    assert hass.states.get(SCHEDULED).attributes["max"] == 70
    # Settings are read once at setup, not on every poll.
    assert fake_client.calls.count("settings") == 1
    await hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))].async_refresh()
    assert fake_client.calls.count("settings") == 1


async def test_scheduled_temperature_in_auto_shows_at_once(hass, fake_client):
    await _setup(hass, fake_client, AUTO)
    polls = fake_client.calls.count("status")
    await _set(hass, SCHEDULED, 55)
    assert fake_client.sent == [("scheduled", 55)]
    assert hass.states.get(SCHEDULED).state == "55.0"
    # Inside scheduled hours the water heater's target follows.
    assert hass.states.get("water_heater.boiler").attributes["temperature"] == 55
    assert fake_client.calls.count("status") == polls


async def test_scheduled_temperature_outside_hours_keeps_target(hass, fake_client):
    await _setup(hass, fake_client, {**AUTO, "T_dsrd": "20"})
    await _set(hass, SCHEDULED, 55)
    assert hass.states.get("water_heater.boiler").attributes["temperature"] == 20


@pytest.mark.parametrize("cmd", ["9", "3", "65", "16"])
async def test_scheduled_temperature_refused_outside_auto(hass, fake_client, cmd):
    await _setup(hass, fake_client, {**AUTO, "Cmd": cmd})
    with pytest.raises(ServiceValidationError, match="auto mode"):
        await _set(hass, SCHEDULED, 55)
    assert fake_client.sent == []


async def test_schedule_refusal_becomes_a_validation_error(hass, fake_client):
    await _setup(hass, fake_client, AUTO)

    def refuse(c):
        raise ScheduleError("The scheduled temperature must be above the comfort temperature (20 °C)")

    fake_client.behaviour["set_scheduled_temperature"] = refuse
    with pytest.raises(ServiceValidationError, match="above the comfort"):
        await _set(hass, SCHEDULED, 21)
    assert hass.states.get(SCHEDULED).state == "45.0"


async def test_api_failure_leaves_value_alone(hass, fake_client):
    await _setup(hass, fake_client, AUTO)

    def fail(c):
        raise api.ThermowattApiError("500")

    fake_client.behaviour["set_scheduled_temperature"] = fail
    with pytest.raises(HomeAssistantError, match="command failed"):
        await _set(hass, SCHEDULED, 55)
    assert hass.states.get(SCHEDULED).state == "45.0"


async def test_comfort_temperature(hass, fake_client):
    await _setup(hass, fake_client, AUTO)
    await _set(hass, COMFORT, 25)
    assert fake_client.sent == [("comfort", 25)]
    assert hass.states.get(COMFORT).state == "25.0"


async def test_comfort_must_stay_below_scheduled(hass, fake_client):
    await _setup(hass, fake_client, AUTO)
    with pytest.raises(ServiceValidationError, match="below the scheduled"):
        await _set(hass, COMFORT, 45)
    assert fake_client.sent == []


async def test_commands_refused_while_rate_limited(hass, fake_client):
    entry = await _setup(hass, fake_client, AUTO)
    hass.data[DOMAIN][entry.entry_id].rate_limited = True
    for entity_id in (SCHEDULED, COMFORT):
        with pytest.raises(HomeAssistantError, match="rate limiting"):
            await _set(hass, entity_id, 30)
    assert fake_client.sent == []


async def test_settings_read_failure_is_not_fatal(hass, fake_client):
    def quota(c):
        raise api.ThermowattRateLimitError("limit", daily=True)

    fake_client.behaviour["settings"] = quota
    entry = await _setup(hass, fake_client, AUTO)
    assert entry.state.name == "LOADED"
    assert hass.states.get(COMFORT).state == "unknown"
    assert hass.states.get(SCHEDULED).state == "45.0"


@pytest.mark.parametrize(("sts", "state"), [("32", "off"), ("48", "on"), ("49", "on"), ("1", "off")])
async def test_boost_sensor(hass, fake_client, sts, state):
    await _setup(hass, fake_client, {**STATUS, "WaterHeaterSts": sts})
    assert hass.states.get("binary_sensor.boiler_boost").state == state
