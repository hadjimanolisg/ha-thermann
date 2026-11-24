"""Diagnostics download: useful content, no secrets."""
from __future__ import annotations

import json

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermann.const import DOMAIN
from custom_components.thermann.diagnostics import async_get_config_entry_diagnostics

from .conftest import BASE_DATA

SECRETS = ("user@example.com", "secret-access", "secret-refresh", "dev-uuid", "SN1", "Boiler", "01.02.03")


async def test_diagnostics_redacted(hass, fake_client):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**BASE_DATA, "access_token": "secret-access", "refresh_token": "secret-refresh"},
        options={"poll_interval": 300},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag)
    for secret in SECRETS:
        assert secret not in text, secret

    assert diag["polling"]["configured_interval_s"] == 300
    assert diag["polling"]["rate_limited"] is False
    assert diag["status"]["heater 1"]["T_Avg"] == "55"
    assert diag["heaters"][0]["type"] == "TMEC"
