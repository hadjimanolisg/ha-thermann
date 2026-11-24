"""The Thermann Smart integration (Thermowatt-based smart electric water heaters)."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ThermowattApiError, ThermowattAuthError, ThermowattClient, ThermowattRateLimitError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_APP_VERSION,
    CONF_DEVICE_UUID,
    CONF_DEVICES,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_BACKOFF_INTERVAL,
    MIN_POLL_INTERVAL,
    QUOTA_BACKOFF_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
RATE_LIMIT_ISSUE = "rate_limited"
PLATFORMS = [Platform.WATER_HEATER, Platform.SENSOR, Platform.BINARY_SENSOR]


@callback
def async_save_tokens(hass: HomeAssistant, entry: ConfigEntry, client: ThermowattClient) -> None:
    """Persist the client's current tokens in the entry if they changed."""
    access, refresh = client.tokens
    if not access or not refresh:
        return
    if (entry.data.get(CONF_ACCESS_TOKEN), entry.data.get(CONF_REFRESH_TOKEN)) == (access, refresh):
        return
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: access, CONF_REFRESH_TOKEN: refresh}
    )


class ThermowattCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Polls /status for every device on the account, with backoff on errors."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ThermowattClient,
        devices: list[dict],
    ) -> None:
        configured = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        if configured < MIN_POLL_INTERVAL:
            _LOGGER.warning(
                "Poll interval %s s is below the %s s minimum (the account's daily "
                "request quota is about 500); using %s s",
                configured, MIN_POLL_INTERVAL, MIN_POLL_INTERVAL,
            )
        self._base_interval = timedelta(seconds=max(configured, MIN_POLL_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=self._base_interval,
        )
        self.client = client
        # Refresh the token early enough that one poll lands before expiry.
        client.refresh_margin = self._base_interval.total_seconds() + 60
        self.devices = {d["seriale"]: d.get("nome", "Boiler") for d in devices if d.get("seriale")}
        # /user-info "type", e.g. "TMEC"; shown as the model ID on the device page.
        self.device_types = {d["seriale"]: d.get("type") for d in devices if d.get("seriale")}
        # Options at setup time; lets the update listener tell an options
        # change (needs reload) from a token save (must NOT reload).
        self.options_snapshot = dict(entry.options)
        # True from a rate-limit/quota rejection until the next good poll.
        # Commands are refused meanwhile, so they don't spend more quota.
        self.rate_limited = False

    @property
    def base_interval(self) -> timedelta:
        """The configured poll interval, without any backoff."""
        return self._base_interval

    @property
    def _issue_id(self) -> str:
        return f"{RATE_LIMIT_ISSUE}_{self.config_entry.entry_id}"

    @callback
    def _async_rate_limit_issue(self, err: ThermowattRateLimitError | None) -> None:
        """Show a Repairs notice while rate limited; remove it when polling recovers."""
        if err is None:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return
        minutes = round((self.update_interval or self._base_interval).total_seconds() / 60)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=RATE_LIMIT_ISSUE,
            translation_placeholders={
                "retry": "hourly" if err.daily else f"in about {minutes} minutes",
                "interval": str(int(self._base_interval.total_seconds())),
            },
        )

    @callback
    def async_clear_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    @callback
    def async_start_rate_limited(self, err: ThermowattRateLimitError) -> None:
        """Start after a rate limit at setup: no data yet, first poll after the backoff."""
        self.rate_limited = True
        self._back_off(err.retry_after, quota=err.daily)
        self._async_rate_limit_issue(err)
        self.async_set_update_error(UpdateFailed(f"{err}; first poll in {self.update_interval}"))

    def _poll_all(self) -> dict[str, dict]:
        # One /status call per device. /user-info is account-level and only
        # fetched once at setup.
        return {serial: self.client.get_status(serial) for serial in self.devices}

    def _back_off(self, retry_after: float | None, quota: bool = False) -> None:
        if quota:
            # Daily quota exhausted: the reset time is unknown, so check hourly
            # rather than spending requests that will only be refused.
            self.update_interval = max(
                timedelta(seconds=QUOTA_BACKOFF_INTERVAL),
                timedelta(seconds=retry_after or 0),
                self._base_interval,
            )
            return
        current = self.update_interval or self._base_interval
        wanted = max(current * 2, timedelta(seconds=retry_after or 0))
        ceiling = max(timedelta(seconds=MAX_BACKOFF_INTERVAL), self._base_interval)
        self.update_interval = min(wanted, ceiling)

    async def _async_update_data(self) -> dict[str, dict]:
        try:
            data = await self.hass.async_add_executor_job(self._poll_all)
        except ThermowattAuthError as err:
            # Stops polling and asks the user to re-authenticate. We never
            # retry a password login automatically.
            raise ConfigEntryAuthFailed(str(err)) from err
        except ThermowattRateLimitError as err:
            if err.daily and not self.rate_limited:
                _LOGGER.warning(
                    "Thermowatt daily request limit reached; checking hourly until it resets. "
                    "If this repeats, raise the poll interval under Configure."
                )
            self.rate_limited = True
            self._back_off(err.retry_after, quota=err.daily)
            self._async_rate_limit_issue(err)
            raise UpdateFailed(f"{err}; backing off to {self.update_interval}") from err
        except ThermowattApiError as err:
            self._back_off(None)
            raise UpdateFailed(f"{err}; backing off to {self.update_interval}") from err
        finally:
            # A refresh may have rotated the tokens even if the poll failed.
            async_save_tokens(self.hass, self.config_entry, self.client)

        if self.rate_limited:
            self._async_rate_limit_issue(None)
        self.rate_limited = False
        if self.update_interval != self._base_interval:
            _LOGGER.info("Thermowatt poll recovered; back to %s interval", self._base_interval)
            self.update_interval = self._base_interval
        return data


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Thermann Smart from a config entry."""
    client = ThermowattClient(
        email=entry.data[CONF_EMAIL],
        # Only present on entries created before v0.3.0; removed below once
        # we hold a working session.
        password=entry.data.get(CONF_PASSWORD),
        device_uuid=entry.data[CONF_DEVICE_UUID],
        app_version=entry.data[CONF_APP_VERSION],
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
    )

    def _connect() -> list[dict]:
        # Reuse the stored session when possible; a restart should not cost a
        # password login. Fall back to exactly one login if it is dead.
        if client.has_tokens:
            try:
                return client.list_devices()
            except ThermowattAuthError:
                _LOGGER.debug("Stored Thermowatt session is no longer valid")
        # Raises ThermowattAuthError (-> reauth) when no password is stored,
        # which is the normal case from v0.3.0 on.
        client.login()
        return client.list_devices()

    rate_limited: ThermowattRateLimitError | None = None
    try:
        devices = await hass.async_add_executor_job(_connect)
    except ThermowattAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ThermowattRateLimitError as err:
        stored = entry.data.get(CONF_DEVICES)
        if not (stored and client.has_tokens):
            # Nothing to start with. Don't let HA's setup retry keep hammering
            # a rate-limited account; reauth puts the next try in the user's hands.
            _LOGGER.warning(
                "Thermowatt is rate limiting this account (%s). Polling is paused; "
                "wait for the lockout to clear, then re-authenticate from Devices & Services.",
                err,
            )
            raise ConfigEntryAuthFailed(str(err)) from err
        # The session is fine, only the quota is spent: start with the heaters
        # from the last successful setup and let the coordinator's backoff wait.
        _LOGGER.warning(
            "Thermowatt is rate limiting this account (%s); starting with the saved "
            "heater list and waiting before the first poll", err,
        )
        devices = stored
        rate_limited = err
    except ThermowattApiError as err:
        raise ConfigEntryNotReady(str(err)) from err
    finally:
        async_save_tokens(hass, entry, client)

    if CONF_PASSWORD in entry.data and client.has_tokens:
        # Older versions stored the password in plain text in
        # .storage/core.config_entries. Tokens are enough from here on; if the
        # session ever dies, the reauth flow asks for the password again.
        hass.config_entries.async_update_entry(
            entry, data={k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
        )

    if not devices:
        raise ConfigEntryNotReady("Thermowatt account returned zero devices")

    if rate_limited is None:
        heaters = [
            {"seriale": d["seriale"], "nome": d.get("nome", "Boiler"), "type": d.get("type")}
            for d in devices
            if d.get("seriale")
        ]
        if entry.data.get(CONF_DEVICES) != heaters:
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICES: heaters})

    coordinator = ThermowattCoordinator(hass, entry, client, devices)
    if rate_limited is None:
        await coordinator.async_config_entry_first_refresh()
    else:
        coordinator.async_start_rate_limited(rate_limited)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change. Token saves also fire this; ignore those."""
    coordinator: ThermowattCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and coordinator.options_snapshot == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: ThermowattCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_clear_issue()
        await hass.async_add_executor_job(coordinator.client.session.close)
    return unloaded
