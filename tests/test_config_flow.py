"""Config, reauth and options flows."""
from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermann import api
from custom_components.thermann.const import DOMAIN

from .conftest import BASE_DATA

USER_INPUT = {"email": "user@example.com", "password": "secret", "app_version": "3.14"}


async def test_user_flow_does_not_store_password(hass, fake_client):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "password" not in result["data"]
    assert result["data"]["access_token"] == "new-access"
    # One login in the flow; setup reuses the tokens instead of logging in again.
    assert fake_client.calls.count("login") == 1


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (api.ThermowattAuthError("bad"), "invalid_auth"),
        (api.ThermowattRateLimitError("429"), "rate_limited"),
        (api.ThermowattApiError("down"), "cannot_connect"),
    ],
)
async def test_user_flow_errors(hass, fake_client, error, key):
    def fail(c):
        raise error

    fake_client.behaviour["login"] = fail
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}


async def test_reauth_keeps_device_and_drops_password(hass, fake_client):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**BASE_DATA, "password": "old", "access_token": "dead", "refresh_token": "dead"},
        unique_id=BASE_DATA["email"],
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "new", "app_version": "3.15"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert "password" not in entry.data
    assert entry.data["access_token"] == "new-access"
    assert entry.data["app_version"] == "3.15"
    assert fake_client.instances[0].device_uuid == "dev-uuid"


async def test_options_flow_validates_interval(hass, fake_client):
    entry = MockConfigEntry(domain=DOMAIN, data={**BASE_DATA, "access_token": "a", "refresh_token": "r"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    from homeassistant.data_entry_flow import InvalidData

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"element_kw": 3.6, "poll_interval": 120}
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"element_kw": 3.6, "poll_interval": 600}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["poll_interval"] == 600


async def test_options_form_starts_at_minimum_for_old_low_value(hass, fake_client):
    """An entry saved with 60 s (allowed before 0.6.0) can be submitted unchanged."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**BASE_DATA, "access_token": "a", "refresh_token": "r"},
        options={"element_kw": 3.0, "poll_interval": 60},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Leaving the field as shown uses the form's default.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"element_kw": 3.0})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["poll_interval"] == 240


async def _loaded_entry(hass, **options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**BASE_DATA, "access_token": "a", "refresh_token": "r"},
        options=options,
        unique_id=BASE_DATA["email"],
        title=BASE_DATA["email"],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _start_reconfigure(hass, entry):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )


async def test_reconfigure_switches_account_and_keeps_everything(hass, fake_client):
    from homeassistant.helpers import entity_registry as er

    entry = await _loaded_entry(hass, poll_interval=600, element_kw=3.0)
    unique_ids = {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    logins = fake_client.calls.count("login")

    result = await _start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "ha@example.com", "password": "pw", "app_version": "3.14"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == "ha@example.com"
    assert entry.title == "ha@example.com"
    assert entry.data["email"] == "ha@example.com"
    assert entry.data["access_token"] == "new-access"
    assert entry.data["device_uuid"] == BASE_DATA["device_uuid"]
    assert "password" not in entry.data
    assert dict(entry.options) == {"poll_interval": 600, "element_kw": 3.0}
    assert fake_client.calls.count("login") == logins + 1
    # Same entities after the reload: history, names and automations keep working.
    assert entry.state is config_entries.ConfigEntryState.LOADED
    after = {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    assert after == unique_ids
    assert hass.states.get("water_heater.boiler") is not None


async def test_reconfigure_same_account_new_password(hass, fake_client):
    entry = await _loaded_entry(hass)
    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": BASE_DATA["email"], "password": "pw", "app_version": "3.15"}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == BASE_DATA["email"]
    assert entry.data["app_version"] == "3.15"


async def test_reconfigure_refuses_account_without_the_heater(hass, fake_client):
    entry = await _loaded_entry(hass)
    fake_client.behaviour["list"] = lambda c: [{"seriale": "OTHER", "nome": "Neighbour"}]
    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "ha@example.com", "password": "pw", "app_version": "3.14"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "heater_not_shared"}
    assert entry.unique_id == BASE_DATA["email"]
    assert entry.data["access_token"] == "a"


async def test_reconfigure_refuses_account_used_by_another_entry(hass, fake_client):
    entry = await _loaded_entry(hass)
    MockConfigEntry(domain=DOMAIN, data=dict(BASE_DATA), unique_id="ha@example.com").add_to_hass(hass)
    logins = fake_client.calls.count("login")
    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "HA@example.com", "password": "pw", "app_version": "3.14"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # Refused before any login.
    assert fake_client.calls.count("login") == logins
    assert entry.unique_id == BASE_DATA["email"]


async def test_reconfigure_login_error_shows_form(hass, fake_client):
    entry = await _loaded_entry(hass)

    def fail(c):
        raise api.ThermowattAuthError("bad")

    fake_client.behaviour["login"] = fail
    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "ha@example.com", "password": "bad", "app_version": "3.14"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data["access_token"] == "a"
