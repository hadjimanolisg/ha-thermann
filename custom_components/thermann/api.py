"""Minimal client for the Thermowatt (MyThermowatt / Thermann Control) cloud API.

Reverse-engineered from the official app's traffic. This is a synchronous
client using `requests`; callers inside Home Assistant must run its methods
via `hass.async_add_executor_job(...)` rather than calling them directly from
the event loop.

Rate-limit safety rules this client follows:
- It never sends a password login on its own. `login()` is only called
  explicitly (setup / config flow / reauth). A 401 during normal use triggers
  at most one token refresh; if that fails, ThermowattAuthError is raised and
  the caller must stop polling.
- Quota exhaustion raises ThermowattRateLimitError so the caller can back
  off. The API reports it either as HTTP 429 or, more often, as HTTP 200 with
  {"success": false, "error": "Daily request limit exceeded"}, so every 200
  body is checked too. Any other `success: false` body is a failure.
- The access token lasts about 30 minutes. When it is a JWT with an `exp`
  claim, it is refreshed shortly before it expires instead of after a 401:
  two requests (refresh + poll) instead of three (failed poll + refresh +
  retried poll). The 401 path stays as the fallback.
"""
from __future__ import annotations

import base64
from collections import deque
import datetime
import json
import logging
import math
import threading
import time
from typing import Any

import requests

from .schedule import ScheduleError, hourly, with_comfort_temperature, with_scheduled_temperature
from .const import API_KEY, BASE_URL

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = 15

# Request kinds counted for the "API requests (24 h)" sensor.
REQUEST_KINDS = ("status", "token_renewal", "login", "command", "device_list", "settings")
# Read-only endpoints for the auto schedule and the heater's settings.
_SETTINGS_READS = {"raw-temperatures", "waterheater-settings"}
_DAY = 86400


def _request_kind(method: str, url: str) -> str:
    endpoint = url.rsplit("/", 1)[-1]
    if endpoint == "status":
        return "status"
    if endpoint == "refresh":
        return "token_renewal"
    if endpoint == "login":
        return "login"
    if endpoint == "user-info":
        return "device_list"
    if method == "GET" and endpoint in _SETTINGS_READS:
        return "settings"
    return "command" if method == "POST" else "device_list"


# Never refresh early more than once per this many seconds, whatever the token
# says (guards against a wrong clock or an odd `exp`).
_MIN_EARLY_REFRESH_SPACING = 600


def _jwt_expiry(token: str | None) -> float | None:
    """The `exp` (Unix time) of a JWT, read without verifying it; else None."""
    if not token or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except (ValueError, TypeError, AttributeError):
        return None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        return None
    return float(exp)


class ThermowattApiError(Exception):
    """Raised for any non-2xx response or network failure."""


class ThermowattAuthError(ThermowattApiError):
    """Credentials/tokens rejected. Polling must stop until the user re-authenticates."""


class ThermowattRateLimitError(ThermowattApiError):
    """HTTP 429, or a 200 body saying the daily request limit is exceeded."""

    def __init__(self, message: str, retry_after: float | None = None, daily: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        # True for the "Daily request limit exceeded" quota (reset time unknown).
        self.daily = daily


def _body_error(resp: requests.Response) -> str | None:
    """The error a 200 response carries in its body, or None if it's a success.

    Some command endpoints reply with an empty or non-JSON body; that is fine.
    """
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if body.get("success") is False or body.get("error"):
        return str(body.get("error") or "unsuccessful response")
    return None


def _is_quota_error(error: str) -> bool:
    return "limit exceeded" in error.lower()


def _retry_after(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class ThermowattClient:
    """Thin wrapper around the Thermowatt REST API."""

    def __init__(
        self,
        email: str,
        password: str | None,
        device_uuid: str,
        app_version: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._device_uuid = device_uuid
        self._app_version = app_version
        self._access_token = access_token
        self._refresh_token = refresh_token
        # A single session is intentional: it preserves cookies set by the
        # backend's load balancer between requests. Headers are passed per
        # request (never stored on the session) so concurrent calls from
        # different executor threads can't clobber each other.
        self.session = requests.Session()
        # Serialises requests so a poll and a user command can't both hit a
        # 401 and race to refresh the same refresh token.
        self._lock = threading.Lock()
        # Refresh this many seconds before the token expires. The coordinator
        # sets it to a bit more than the poll interval, so at least one poll
        # lands inside the window.
        self.refresh_margin: float = 120
        self._last_early_refresh: float = 0.0
        # (time, kind) of every request attempted in the last 24 h. Counted
        # before sending, so attempts that failed in transit are included.
        # In memory only: it restarts with Home Assistant (see counting_since).
        self._sent: deque[tuple[float, str]] = deque()
        self._sent_lock = threading.Lock()  # read from the event loop, written from workers
        self.counting_since: float = time.time()

    # ------------------------------------------------------------------ tokens

    @property
    def tokens(self) -> tuple[str | None, str | None]:
        return self._access_token, self._refresh_token

    @property
    def has_tokens(self) -> bool:
        return bool(self._access_token and self._refresh_token)

    def _base_headers(self, *, api_key: bool = False, serial: str | None = None) -> dict[str, str]:
        headers = {
            "app": "MyThermowatt",
            "platform": "iOS",
            "version": self._app_version,
            "lang": "en",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if api_key:
            headers["x-api-key"] = API_KEY
        if serial:
            headers["seriale"] = serial
        return headers

    def _prune_sent(self, now: float) -> None:
        """Drop entries older than 24 h. Caller holds _sent_lock."""
        while self._sent and self._sent[0][0] <= now - _DAY:
            self._sent.popleft()

    def request_counts(self) -> dict[str, int]:
        """Requests attempted in the last 24 h, by kind, plus "total"."""
        with self._sent_lock:
            self._prune_sent(time.time())
            kinds = [kind for _, kind in self._sent]
        counts = dict.fromkeys(REQUEST_KINDS, 0)
        for kind in kinds:
            counts[kind] = counts.get(kind, 0) + 1
        counts["total"] = sum(counts.values())
        return counts

    def _send(self, method: str, url: str, headers: dict[str, str], **kwargs) -> requests.Response:
        now = time.time()
        with self._sent_lock:
            self._prune_sent(now)
            self._sent.append((now, _request_kind(method, url)))
        try:
            return self.session.request(method, url, headers=headers, verify=True, timeout=_TIMEOUT, **kwargs)
        except requests.RequestException as err:
            raise ThermowattApiError(f"Network error calling {url}: {err}") from err

    @staticmethod
    def _parse_tokens(resp: requests.Response) -> tuple[str, str]:
        result = resp.json()["result"]
        return result["accessToken"], result["refreshToken"]

    # ------------------------------------------------------------------ auth

    def login(self) -> None:
        """Password login. Only call this from setup, config flow or reauth."""
        if not self._password:
            raise ThermowattAuthError("No password available; re-authentication required")
        with self._lock:
            payload = {
                "username": self._email,
                "password": self._password,
                "device_id": self._device_uuid,
            }
            resp = self._send("POST", f"{BASE_URL}/login", self._base_headers(api_key=True), json=payload)

            if resp.status_code == 429:
                raise ThermowattRateLimitError("Login rate limited (HTTP 429)", _retry_after(resp))
            if resp.status_code == 200 and (error := _body_error(resp)):
                if _is_quota_error(error):
                    raise ThermowattRateLimitError(f"Login refused: {error}", daily=True)
                raise ThermowattAuthError(f"Login refused: {error}")
            if resp.status_code in (400, 401, 403):
                raise ThermowattAuthError(
                    f"Login rejected with HTTP {resp.status_code}. Check email/password, or "
                    "whether the Thermann Control / MyThermowatt app has a newer version "
                    "than the app_version configured for this integration."
                )
            if resp.status_code != 200:
                raise ThermowattApiError(f"Login failed with HTTP {resp.status_code}")
            try:
                self._access_token, self._refresh_token = self._parse_tokens(resp)
            except (KeyError, ValueError, TypeError) as err:
                raise ThermowattApiError(f"Unexpected login response shape: {err}") from err

    def _refresh_locked(self) -> bool:
        """Try to refresh the access token once. Caller must hold the lock."""
        if not self._refresh_token:
            return False
        payload = {"username": self._email, "refreshToken": self._refresh_token}
        resp = self._send("POST", f"{BASE_URL}/refresh", self._base_headers(api_key=True), json=payload)
        if resp.status_code == 429:
            raise ThermowattRateLimitError("Token refresh rate limited (HTTP 429)", _retry_after(resp))
        if resp.status_code == 200 and (error := _body_error(resp)):
            if _is_quota_error(error):
                raise ThermowattRateLimitError(f"Token refresh refused: {error}", daily=True)
            _LOGGER.debug("Token refresh refused: %s", error)
            return False
        if resp.status_code != 200:
            _LOGGER.debug("Token refresh failed: HTTP %s", resp.status_code)
            return False
        try:
            self._access_token, self._refresh_token = self._parse_tokens(resp)
        except (KeyError, ValueError, TypeError):
            return False
        return True

    # ------------------------------------------------------------------ requests

    def _refresh_early_locked(self) -> None:
        """Refresh before expiry if the token says it's due. Caller holds the lock.

        A failed refresh is not fatal: the request goes ahead with the current
        token and the 401 path handles it. A rate limit propagates, so nothing
        more is sent while the quota is exhausted.
        """
        expiry = _jwt_expiry(self._access_token)
        if expiry is None:
            return
        now = time.time()
        if expiry - now > self.refresh_margin:
            return
        if now - self._last_early_refresh < _MIN_EARLY_REFRESH_SPACING:
            return
        self._last_early_refresh = now
        if self._refresh_locked():
            _LOGGER.debug("Refreshed the access token %.0f s before it expired", expiry - now)
        else:
            _LOGGER.debug("Early token refresh failed; continuing with the current token")

    def _request(self, method: str, endpoint: str, serial: str | None = None, **kwargs) -> requests.Response:
        """Send an authenticated request; raise on any non-200 response."""
        url = f"{BASE_URL}{endpoint}"
        extra_headers = kwargs.pop("headers", None) or {}

        with self._lock:
            if not self.has_tokens:
                raise ThermowattAuthError("Not logged in")

            self._refresh_early_locked()

            resp = self._send(method, url, {**self._base_headers(serial=serial), **extra_headers}, **kwargs)

            if resp.status_code == 401:
                # One refresh, one retry. Never fall back to a password login
                # here: a loop of logins is what gets accounts locked.
                if not self._refresh_locked():
                    raise ThermowattAuthError("Session expired and token refresh failed")
                resp = self._send(method, url, {**self._base_headers(serial=serial), **extra_headers}, **kwargs)
                if resp.status_code == 401:
                    raise ThermowattAuthError("Still unauthorised after token refresh")

        if resp.status_code == 429:
            raise ThermowattRateLimitError(f"{method} {endpoint} rate limited (HTTP 429)", _retry_after(resp))
        if resp.status_code != 200:
            raise ThermowattApiError(f"{method} {endpoint} failed: HTTP {resp.status_code}")
        if error := _body_error(resp):
            if _is_quota_error(error):
                raise ThermowattRateLimitError(f"{method} {endpoint} refused: {error}", daily=True)
            raise ThermowattApiError(f"{method} {endpoint} refused: {error}")
        return resp

    @staticmethod
    def _result(resp: requests.Response) -> Any:
        try:
            return resp.json().get("result", {})
        except ValueError as err:
            raise ThermowattApiError(f"Invalid JSON from {resp.url}") from err

    def list_devices(self) -> list[dict[str, Any]]:
        """Return the list of thermostats/boilers on this account."""
        result = self._result(self._request("GET", "/user-info"))
        return result.get("termostati", []) if isinstance(result, dict) else []

    def get_status(self, serial: str) -> dict[str, Any]:
        result = self._result(self._request("GET", "/status", serial=serial))
        # An empty or partial result is not a reading; never show it as one.
        if not isinstance(result, dict) or "T_Avg" not in result:
            raise ThermowattApiError(f"/status for {serial} returned no readings")
        return result

    def get_schedule(self, serial: str) -> dict[str, Any]:
        """The auto mode's weekly schedule (GET /auto/raw-temperatures)."""
        result = self._result(self._request("GET", "/auto/raw-temperatures", serial=serial))
        if not isinstance(result, dict):
            raise ThermowattApiError(f"Schedule for {serial} has an unexpected format")
        try:
            hourly(result)
        except ScheduleError as err:
            raise ThermowattApiError(f"Schedule for {serial}: {err}") from err
        return result

    def _post_schedule(self, serial: str, schedule: dict[str, Any]) -> None:
        # The app's save. A bare POST /auto is only "switch to auto" and
        # silently ignores a schedule sent with it.
        self._request("POST", "/auto/raw-temperatures", serial=serial, json=schedule)

    def set_scheduled_temperature(self, serial: str, temp_c: int) -> dict[str, Any]:
        """Change the temperature of the auto mode's scheduled hours, as the app does.

        Reads the schedule, replaces its highest temperature and posts the
        whole schedule back: 2 requests. The mode is not changed. Raises
        ScheduleError, before anything is sent, if the value can't be
        applied. Returns the schedule that was sent.
        """
        new = with_scheduled_temperature(self.get_schedule(serial), temp_c)
        self._post_schedule(serial, new)
        return new

    def get_settings(self, serial: str) -> dict[str, Any]:
        """Comfort and boost settings (GET /waterheater-settings), e.g. TComfort.

        Read once at setup; set_comfort_temperature keeps it current.
        """
        result = self._result(self._request("GET", "/waterheater-settings", serial=serial))
        if not isinstance(result, dict):
            raise ThermowattApiError(f"Settings for {serial} have an unexpected format")
        return result


    def set_comfort_temperature(self, serial: str, temp_c: int) -> dict[str, Any]:
        """The auto mode's temperature outside scheduled hours ("T comfort auto").

        As the app does: store the setting (POST /tcomfort), then put it in
        the schedule's other hours, which is what the heater follows: 3
        requests. The schedule is checked before the setting is sent, so a
        value that can't be applied changes nothing. Returns the schedule sent.
        """
        new = with_comfort_temperature(self.get_schedule(serial), temp_c)
        self._request("POST", "/tcomfort", serial=serial, json={"tcomfort": temp_c})
        self._post_schedule(serial, new)
        return new

    def set_temperature(self, serial: str, temp_c: int) -> None:
        self._request("POST", "/manual", serial=serial, json={"T_SetPoint": temp_c})

    def set_mode(self, serial: str, mode: str, current_setpoint: int = 60) -> None:
        text_plain = {"headers": {"Content-Type": "text/plain"}, "data": ""}
        if mode == "manual":
            self._request("POST", "/manual", serial=serial, json={"T_SetPoint": current_setpoint})
        elif mode == "eco":
            self._request("POST", "/eco", serial=serial, **text_plain)
        elif mode == "auto":
            self._request("POST", "/auto", serial=serial, **text_plain)
        elif mode == "holiday":
            future_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
            self._request("POST", "/holiday", serial=serial, json={"end_date": future_date})
        elif mode == "off":
            self._request("POST", "/off", serial=serial, **text_plain)
        else:
            raise ValueError(f"Unknown mode: {mode}")
