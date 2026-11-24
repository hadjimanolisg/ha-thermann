"""ThermowattClient against mocked HTTP responses."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from custom_components.thermann import api


def resp(code, body=None, headers=None):
    r = MagicMock()
    r.status_code = code
    r.headers = headers or {}
    r.json.return_value = body or {}
    r.url = "https://example.invalid"
    return r


TOKENS = {"result": {"accessToken": "a2", "refreshToken": "r2"}}


def client(responses, tokens=("a", "r"), password="pw"):
    c = api.ThermowattClient("e", password, "uuid", "3.14", *tokens)
    c.session = MagicMock()
    c.session.request.side_effect = responses
    return c


def endpoints(c):
    return [call.args[1].rsplit("/", 1)[-1] for call in c.session.request.call_args_list]


def test_401_refreshes_once_and_retries():
    c = client([resp(401), resp(200, TOKENS), resp(200, {"result": {"T_Avg": 50}})])
    assert c.get_status("S") == {"T_Avg": 50}
    assert endpoints(c) == ["status", "refresh", "status"]
    assert c.tokens == ("a2", "r2")
    assert c.session.request.call_args.kwargs["headers"]["Authorization"] == "Bearer a2"


def test_failed_refresh_never_falls_back_to_password_login():
    c = client([resp(401), resp(401)])
    with pytest.raises(api.ThermowattAuthError):
        c.get_status("S")
    assert endpoints(c) == ["status", "refresh"]


def test_401_after_refresh_raises_auth_error():
    c = client([resp(401), resp(200, TOKENS), resp(401)])
    with pytest.raises(api.ThermowattAuthError):
        c.get_status("S")


def test_429_carries_retry_after():
    c = client([resp(429, headers={"Retry-After": "120"})])
    with pytest.raises(api.ThermowattRateLimitError) as err:
        c.get_status("S")
    assert err.value.retry_after == 120


def test_no_tokens_sends_nothing():
    c = client([], tokens=(None, None))
    with pytest.raises(api.ThermowattAuthError):
        c.get_status("S")
    assert c.session.request.call_count == 0


def test_login_without_password_sends_nothing():
    c = client([], tokens=(None, None), password=None)
    with pytest.raises(api.ThermowattAuthError):
        c.login()
    assert c.session.request.call_count == 0


def test_login_429_is_rate_limit_not_auth():
    c = client([resp(429)], tokens=(None, None))
    with pytest.raises(api.ThermowattRateLimitError):
        c.login()


def test_api_key_only_on_auth_calls():
    c = client([resp(200, TOKENS), resp(200, {"result": {"T_Avg": "40"}})], tokens=(None, None))
    c.login()
    c.get_status("S")
    login_h, status_h = (call.kwargs["headers"] for call in c.session.request.call_args_list)
    assert "x-api-key" in login_h
    assert "x-api-key" not in status_h
    assert status_h["seriale"] == "S"


def test_set_mode_raises_on_http_error():
    c = client([resp(500)])
    with pytest.raises(api.ThermowattApiError):
        c.set_mode("S", "eco")


def test_network_error_is_wrapped():
    c = client([requests.ConnectionError("boom")])
    with pytest.raises(api.ThermowattApiError):
        c.get_status("S")


QUOTA = {"success": False, "error": "Daily request limit exceeded"}


def test_quota_in_200_body_is_a_daily_rate_limit():
    c = client([resp(200, QUOTA)])
    with pytest.raises(api.ThermowattRateLimitError) as err:
        c.get_status("S")
    assert err.value.daily is True


def test_quota_on_a_command_is_not_reported_as_success():
    c = client([resp(200, QUOTA)])
    with pytest.raises(api.ThermowattRateLimitError):
        c.set_mode("S", "eco")


def test_other_unsuccessful_body_is_an_error():
    c = client([resp(200, {"success": False, "error": "Wrong mode"})])
    with pytest.raises(api.ThermowattApiError) as err:
        c.get_status("S")
    assert not isinstance(err.value, api.ThermowattRateLimitError)


def test_empty_status_is_not_a_reading():
    c = client([resp(200, {"success": True, "result": {}})])
    with pytest.raises(api.ThermowattApiError):
        c.get_status("S")


def test_command_with_empty_body_is_fine():
    r = resp(200)
    r.json.side_effect = ValueError("no body")
    c = client([r])
    c.set_mode("S", "off")


def test_quota_during_refresh_and_login():
    c = client([resp(401), resp(200, QUOTA)])
    with pytest.raises(api.ThermowattRateLimitError):
        c.get_status("S")
    c = client([resp(200, QUOTA)], tokens=(None, None))
    with pytest.raises(api.ThermowattRateLimitError) as err:
        c.login()
    assert err.value.daily is True
