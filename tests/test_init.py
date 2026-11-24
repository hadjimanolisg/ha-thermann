"""Setup, migration, polling and backoff against a real Home Assistant core."""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermann import api
from custom_components.thermann.const import DOMAIN

from .conftest import BASE_DATA

WITH_TOKENS = {**BASE_DATA, "access_token": "a", "refresh_token": "r"}


async def setup(hass: HomeAssistant, data: dict, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options or {}, unique_id=data["email"])
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def reauth_started(hass: HomeAssistant) -> bool:
    return any(f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress())


async def test_stored_tokens_mean_no_login(hass, fake_client):
    entry = await setup(hass, WITH_TOKENS)
    assert entry.state is ConfigEntryState.LOADED
    assert fake_client.calls == ["list", "status"]
    assert hass.states.get("water_heater.boiler").attributes["current_temperature"] == 55


async def test_legacy_password_used_once_then_deleted(hass, fake_client):
    entry = await setup(hass, {**BASE_DATA, "password": "secret"})
    assert entry.state is ConfigEntryState.LOADED
    assert fake_client.calls == ["login", "list", "status"]
    assert "password" not in entry.data
    assert entry.data["access_token"] == "new-access"
    assert entry.data["device_uuid"] == "dev-uuid"


async def test_dead_tokens_without_password_go_to_reauth(hass, fake_client):
    def dead(c):
        raise api.ThermowattAuthError("expired")

    fake_client.behaviour["list"] = dead
    entry = await setup(hass, WITH_TOKENS)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert fake_client.calls == ["list", "login"]  # login refused locally, nothing sent
    assert reauth_started(hass)


async def test_rate_limited_login_goes_to_reauth_not_retry(hass, fake_client):
    def limited(c):
        raise api.ThermowattRateLimitError("429")

    fake_client.behaviour["login"] = limited
    entry = await setup(hass, {**BASE_DATA, "password": "secret"})
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert reauth_started(hass)


async def test_network_error_is_not_ready(hass, fake_client):
    def down(c):
        raise api.ThermowattApiError("timeout")

    fake_client.behaviour["list"] = down
    entry = await setup(hass, WITH_TOKENS)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_backoff_doubles_caps_and_resets(hass, fake_client):
    entry = await setup(hass, WITH_TOKENS)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.update_interval == timedelta(seconds=300)

    errors = [api.ThermowattRateLimitError("429")] * 5 + [api.ThermowattApiError("500")]

    def failing(c):
        raise errors.pop(0)

    fake_client.behaviour["status"] = failing
    seen = []
    for _ in range(6):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        seen.append(int(coordinator.update_interval.total_seconds()))
    assert seen == [600, 900, 900, 900, 900, 900]

    fake_client.behaviour.pop("status")
    await coordinator._async_update_data()
    assert coordinator.update_interval == timedelta(seconds=300)
    assert coordinator.rate_limited is False


async def test_retry_after_is_respected(hass, fake_client):
    entry = await setup(hass, WITH_TOKENS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    def limited(c):
        raise api.ThermowattRateLimitError("429", retry_after=600)

    fake_client.behaviour["status"] = limited
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == timedelta(seconds=600)


async def test_auth_error_while_polling_stops_and_reauths(hass, fake_client):
    entry = await setup(hass, WITH_TOKENS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    def expired(c):
        raise api.ThermowattAuthError("expired")

    fake_client.behaviour["status"] = expired
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert reauth_started(hass)
    assert "login" not in fake_client.calls


async def test_token_rotation_saved_without_reload(hass, fake_client):
    entry = await setup(hass, WITH_TOKENS)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    fake_client.instances[-1]._t = ("rotated", "rotated-r")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert entry.data["access_token"] == "rotated"
    assert hass.data[DOMAIN][entry.entry_id] is coordinator  # not reloaded


async def test_options_change_reloads_with_new_interval(hass, fake_client):
    entry = await setup(hass, WITH_TOKENS)
    hass.config_entries.async_update_entry(entry, options={"poll_interval": 600, "element_kw": 3.6})
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id].update_interval == timedelta(seconds=600)


async def test_daily_quota_backs_off_to_hourly_and_flags(hass, fake_client):
    entry = await setup(hass, WITH_TOKENS)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    def exhausted(c):
        raise api.ThermowattRateLimitError("quota", daily=True)

    fake_client.behaviour["status"] = exhausted
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == timedelta(seconds=3600)
    assert coordinator.rate_limited is True

    fake_client.behaviour.pop("status")
    await coordinator._async_update_data()
    assert coordinator.rate_limited is False
    assert coordinator.update_interval == timedelta(seconds=300)


async def test_saved_interval_below_minimum_is_raised(hass, fake_client, caplog):
    entry = await setup(hass, WITH_TOKENS, options={"poll_interval": 60})
    assert hass.data[DOMAIN][entry.entry_id].update_interval == timedelta(seconds=240)
    assert "below the 240 s minimum" in caplog.text
