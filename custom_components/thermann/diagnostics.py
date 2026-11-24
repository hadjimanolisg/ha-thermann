"""Diagnostics download for Thermann Smart, with secrets removed."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import ThermowattCoordinator
from .const import CONF_ACCESS_TOKEN, CONF_DEVICE_UUID, CONF_DEVICES, CONF_REFRESH_TOKEN, DOMAIN

REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_DEVICE_UUID,
    "seriale",
    "nome",
    "Wifi_SW_Version",
    "Country",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Everything useful for a bug report; no credentials, serials or names."""
    coordinator: ThermowattCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    data = {k: v for k, v in entry.data.items() if k != CONF_DEVICES}
    result: dict[str, Any] = {
        "entry": async_redact_data(data, REDACT),
        "options": dict(entry.options),
        "heaters": async_redact_data(entry.data.get(CONF_DEVICES, []), REDACT),
    }
    if coordinator is None:
        return result
    serials = list(coordinator.devices)
    result["polling"] = {
        "configured_interval_s": coordinator.base_interval.total_seconds(),
        "current_interval_s": (coordinator.update_interval or coordinator.base_interval).total_seconds(),
        "rate_limited": coordinator.rate_limited,
        "last_update_success": coordinator.last_update_success,
    }
    # Heaters are numbered instead of named by serial.
    result["status"] = {
        f"heater {i}": async_redact_data((coordinator.data or {}).get(serial, {}), REDACT)
        for i, serial in enumerate(serials, 1)
    }
    return result
