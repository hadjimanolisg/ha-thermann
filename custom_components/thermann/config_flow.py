"""Config flow for Thermann Smart."""
from __future__ import annotations

from collections.abc import Mapping
import uuid
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback

from .api import ThermowattApiError, ThermowattAuthError, ThermowattClient, ThermowattRateLimitError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_APP_VERSION,
    CONF_DEVICE_UUID,
    CONF_DEVICES,
    CONF_ELEMENT_KW,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    DEFAULT_APP_VERSION,
    DEFAULT_ELEMENT_KW,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_APP_VERSION, default=DEFAULT_APP_VERSION): str,
    }
)


async def _login(
    hass: HomeAssistant,
    email: str,
    password: str,
    app_version: str,
    device_uuid: str,
    serials: list[str] | None = None,
) -> dict[str, str]:
    """Do one real login. Returns the token fields to store. Raises on failure.

    With `serials` (a list), also fetches the account's heaters (one
    /user-info request) and fills the list with their serial numbers.
    """
    client = ThermowattClient(
        email=email, password=password, device_uuid=device_uuid, app_version=app_version
    )

    def _run() -> None:
        client.login()
        if serials is not None:
            serials.extend(d["seriale"] for d in client.list_devices() if d.get("seriale"))

    try:
        await hass.async_add_executor_job(_run)
    finally:
        await hass.async_add_executor_job(client.session.close)
    access, refresh = client.tokens
    return {CONF_ACCESS_TOKEN: access, CONF_REFRESH_TOKEN: refresh}


def _error_key(err: Exception) -> str:
    if isinstance(err, ThermowattRateLimitError):
        return "rate_limited"
    if isinstance(err, ThermowattAuthError):
        return "invalid_auth"
    if isinstance(err, ThermowattApiError):
        return "cannot_connect"
    return "unknown"


class ThermowattSmartConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Thermann Smart."""

    VERSION = 1

    def _update_and_finish(self, entry: config_entries.ConfigEntry, reason: str, **changes: Any):
        """Save the new account details and end the flow, reloading the entry.

        A loaded entry has this integration's update listener, which reloads
        it on such a change; Home Assistant 2026.12 requires leaving the
        reload to it. An entry that never loaded has no listener, so Home
        Assistant reloads it here.
        """
        if entry.update_listeners:
            self.hass.config_entries.async_update_entry(entry, **changes)
            return self.async_abort(reason=reason)
        return self.async_update_reload_and_abort(entry, reason=reason, **changes)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "ThermannOptionsFlow":
        return ThermannOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()

            device_uuid = str(uuid.uuid4())
            try:
                tokens = await _login(
                    self.hass,
                    user_input[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    user_input[CONF_APP_VERSION],
                    device_uuid,
                )
            except Exception as err:  # noqa: BLE001
                errors["base"] = _error_key(err)
            else:
                # Store the tokens so setup reuses this session instead of
                # sending a second login straight away.
                # The password is used for this one login and never stored:
                # HA keeps config entries as plain JSON on disk and in backups.
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_APP_VERSION: user_input[CONF_APP_VERSION],
                        CONF_DEVICE_UUID: device_uuid,
                        **tokens,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        """Triggered when the session can't be refreshed or the account is rate limited."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            app_version = user_input.get(CONF_APP_VERSION, entry.data[CONF_APP_VERSION])
            try:
                tokens = await _login(
                    self.hass,
                    entry.data[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    app_version,
                    # Keep the same device id so the account doesn't
                    # accumulate a new "phone" on every reauth.
                    entry.data[CONF_DEVICE_UUID],
                )
            except Exception as err:  # noqa: BLE001
                errors["base"] = _error_key(err)
            else:
                data = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
                return self._update_and_finish(
                    entry,
                    "reauth_successful",
                    data={**data, CONF_APP_VERSION: app_version, **tokens},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_APP_VERSION, default=entry.data.get(CONF_APP_VERSION, DEFAULT_APP_VERSION)
                    ): str,
                }
            ),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )


    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Sign in with another account (or a new password) in place.

        Keeps the entry, so entities, names, areas, options and the energy
        total stay. The new account must have at least one of the heaters
        already set up: entities are tied to the heater's serial number.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            unique_id = email.lower()
            if unique_id != entry.unique_id:
                await self.async_set_unique_id(unique_id)
                # Another entry already uses this account.
                self._abort_if_unique_id_configured()
            serials: list[str] = []
            try:
                tokens = await _login(
                    self.hass,
                    email,
                    user_input[CONF_PASSWORD],
                    user_input[CONF_APP_VERSION],
                    # Same device id: this is still the same Home Assistant.
                    entry.data[CONF_DEVICE_UUID],
                    serials,
                )
            except Exception as err:  # noqa: BLE001
                errors["base"] = _error_key(err)
            else:
                current = {d.get("seriale") for d in entry.data.get(CONF_DEVICES, [])}
                if current and not current & set(serials):
                    errors["base"] = "heater_not_shared"
                else:
                    data = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
                    return self._update_and_finish(
                        entry,
                        "reconfigure_successful",
                        unique_id=unique_id,
                        title=email,
                        data={
                            **data,
                            CONF_EMAIL: email,
                            CONF_APP_VERSION: user_input[CONF_APP_VERSION],
                            **tokens,
                        },
                    )

        defaults = user_input or {}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL, default=defaults.get(CONF_EMAIL, entry.data[CONF_EMAIL])
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_APP_VERSION,
                        default=defaults.get(
                            CONF_APP_VERSION, entry.data.get(CONF_APP_VERSION, DEFAULT_APP_VERSION)
                        ),
                    ): str,
                }
            ),
            errors=errors,
        )


class ThermannOptionsFlow(config_entries.OptionsFlow):
    """Element power rating (energy estimate) and poll interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # `self.config_entry` only exists on options flows from HA 2024.12;
        # the handler is the entry ID in every supported version.
        entry = self.hass.config_entries.async_get_entry(self.handler)
        options = entry.options if entry else {}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ELEMENT_KW, default=options.get(CONF_ELEMENT_KW, DEFAULT_ELEMENT_KW)
                ): vol.Coerce(float),
                vol.Required(
                    CONF_POLL_INTERVAL,
                    # Older versions allowed lower values; start the form at
                    # the value actually in use so it can be submitted as is.
                    default=max(
                        options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL), MIN_POLL_INTERVAL
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
