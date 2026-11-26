"""Comfort temperature (auto mode, outside scheduled hours)."""
from __future__ import annotations

import pytest
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermann import api
from custom_components.thermann.const import DOMAIN
from custom_components.thermann.schedule import ScheduleError

from .conftest import BASE_DATA, STATUS

COMFORT = "number.boiler_comfort_temperature"
HEATER = "water_heater.boiler"
AUTO = {**STATUS, "Cmd": "17", "T_dsrd": "45", "Tmsch": "45"}


async def _setup(hass, fake_client, status=AUTO):
    fake_client.behaviour["status"] = lambda c: dict(status)
    entry = MockConfigEntry(domain=DOMAIN, data={**BASE_DATA, "access_token": "a", "refresh_token": "r"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _set(hass, value):
    await hass.services.async_call(
        NUMBER_DOMAIN, "set_value", {"entity_id": COMFORT, "value": value}, blocking=True
    )
    await hass.async_block_till_done()


async def test_value_and_range(hass, fake_client):
    await _setup(hass, fake_client)
    state = hass.states.get(COMFORT)
    assert state.state == "20.0"
    assert (state.attributes["min"], state.attributes["max"]) == (20, 44)  # below scheduled 45
    assert fake_client.calls.count("settings") == 1  # read once at setup


@pytest.mark.parametrize("cmd", ["9", "3", "65", "16"])
async def test_only_available_in_auto(hass, fake_client, cmd):
    await _setup(hass, fake_client, {**AUTO, "Cmd": cmd})
    assert hass.states.get(COMFORT).state == "unavailable"


async def test_set_inside_scheduled_hours(hass, fake_client):
    await _setup(hass, fake_client)
    polls = fake_client.calls.count("status")
    await _set(hass, 25)
    assert fake_client.sent == [("comfort", 25)]
    assert hass.states.get(COMFORT).state == "25.0"
    attrs = hass.states.get(HEATER).attributes
    assert attrs["comfort_temperature"] == 25
    assert attrs["min_temp"] == 26  # the scheduled temperature stays above it
    assert attrs["desired_temperature"] == 45  # inside scheduled hours: unchanged
    assert fake_client.calls.count("status") == polls


async def test_set_outside_scheduled_hours_moves_current_target(hass, fake_client):
    await _setup(hass, fake_client, {**AUTO, "T_dsrd": "20"})
    await _set(hass, 25)
    assert hass.states.get(HEATER).attributes["desired_temperature"] == 25


async def test_must_stay_below_scheduled(hass, fake_client):
    await _setup(hass, fake_client)
    with pytest.raises(ServiceValidationError):
        await _set(hass, 45)
    assert fake_client.sent == []


async def test_schedule_problem_becomes_validation_error(hass, fake_client):
    await _setup(hass, fake_client)

    def refuse(c):
        raise ScheduleError("The schedule has no scheduled hours to change")

    fake_client.behaviour["set_comfort_temperature"] = refuse
    with pytest.raises(ServiceValidationError, match="no scheduled hours"):
        await _set(hass, 25)
    assert hass.states.get(COMFORT).state == "20.0"


async def test_api_failure_leaves_value(hass, fake_client):
    await _setup(hass, fake_client)

    def fail(c):
        raise api.ThermowattApiError("500")

    fake_client.behaviour["set_comfort_temperature"] = fail
    with pytest.raises(HomeAssistantError, match="command failed"):
        await _set(hass, 25)
    assert hass.states.get(COMFORT).state == "20.0"


async def test_refused_while_rate_limited(hass, fake_client):
    entry = await _setup(hass, fake_client)
    hass.data[DOMAIN][entry.entry_id].rate_limited = True
    with pytest.raises(HomeAssistantError, match="rate limiting"):
        await _set(hass, 25)
    assert fake_client.sent == []


async def test_unknown_when_settings_read_failed(hass, fake_client):
    def quota(c):
        raise api.ThermowattRateLimitError("limit", daily=True)

    fake_client.behaviour["settings"] = quota
    await _setup(hass, fake_client)
    assert hass.states.get(COMFORT).state == "unknown"
