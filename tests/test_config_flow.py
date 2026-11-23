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
            result["flow_id"], {"element_kw": 3.6, "poll_interval": 5}
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"element_kw": 3.6, "poll_interval": 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["poll_interval"] == 120
