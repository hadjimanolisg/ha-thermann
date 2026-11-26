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


def jwt(exp):
    import base64, json

    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256'})}.{part({'exp': exp})}.sig"


def jwt_client(responses, expires_in, margin=360):
    import time

    c = client(responses, tokens=(jwt(time.time() + expires_in), "r"))
    c.refresh_margin = margin
    return c


STATUS_OK = {"result": {"T_Avg": "40"}}


def test_expiring_token_refreshed_before_the_poll():
    c = jwt_client([resp(200, TOKENS), resp(200, STATUS_OK)], expires_in=200)
    c.get_status("S")
    assert endpoints(c) == ["refresh", "status"]  # 2 requests instead of 3
    assert c.tokens[0] == "a2"


def test_fresh_token_not_refreshed():
    c = jwt_client([resp(200, STATUS_OK)], expires_in=1500)
    c.get_status("S")
    assert endpoints(c) == ["status"]


def test_already_expired_token_refreshed_first():
    c = jwt_client([resp(200, TOKENS), resp(200, STATUS_OK)], expires_in=-30)
    c.get_status("S")
    assert endpoints(c) == ["refresh", "status"]


def test_non_jwt_token_falls_back_to_401_path():
    c = client([resp(401), resp(200, TOKENS), resp(200, STATUS_OK)])  # token "a"
    c.get_status("S")
    assert endpoints(c) == ["status", "refresh", "status"]


def test_early_refresh_at_most_once_per_ten_minutes():
    c = jwt_client([resp(401), resp(200, STATUS_OK), resp(200, STATUS_OK)], expires_in=100)
    c.get_status("S")  # early refresh attempt fails (401), poll goes ahead with old token
    c.get_status("S")  # still near expiry, but no second early attempt
    assert endpoints(c) == ["refresh", "status", "status"]


def test_quota_during_early_refresh_sends_no_poll():
    c = jwt_client([resp(200, QUOTA)], expires_in=100)
    with pytest.raises(api.ThermowattRateLimitError):
        c.get_status("S")
    assert endpoints(c) == ["refresh"]


def test_odd_exp_values_are_ignored():
    import base64, json

    for exp in ("soon", True, float("inf"), None):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
        assert api._jwt_expiry(f"h.{payload}.s") is None
    assert api._jwt_expiry("not-a-jwt") is None
    assert api._jwt_expiry("a.%%%.c") is None


def test_request_counts_by_kind():
    c = client([resp(401), resp(200, TOKENS), resp(200, STATUS_OK), resp(200)])
    c.get_status("S")  # failed poll + renewal + poll
    c.set_mode("S", "eco")
    counts = c.request_counts()
    assert counts["status"] == 2
    assert counts["token_renewal"] == 1
    assert counts["command"] == 1
    assert counts["total"] == 4


def test_request_counts_forget_after_a_day(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(api.time, "time", lambda: now[0])
    c = client([resp(200, STATUS_OK), resp(200, STATUS_OK)])
    c.get_status("S")
    now[0] += 86400 - 1
    c.get_status("S")
    assert c.request_counts()["status"] == 2
    now[0] += 2  # first request is now older than 24 h
    assert c.request_counts()["status"] == 1


def test_failed_transport_still_counted():
    import requests as rq

    c = client([rq.ConnectionError("x")])
    with pytest.raises(api.ThermowattApiError):
        c.get_status("S")
    assert c.request_counts()["total"] == 1


WEEKDAY = "20" * 6 + "45" + "20" * 5 + "45" * 6 + "20" * 6
SCHEDULE = {"Time_prg": "0", **{f"DayPrg_{d}": WEEKDAY for d in range(7)}}


def test_scheduled_temperature_reads_then_posts_the_whole_schedule():
    c = client([resp(200, {"success": True, "result": SCHEDULE}), resp(200, {"success": True})])
    sent = c.set_scheduled_temperature("S", 50)
    calls = c.session.request.call_args_list
    assert [(call.args[0], call.args[1].split("/api/v1")[1]) for call in calls] == [
        ("GET", "/auto/raw-temperatures"), ("POST", "/auto")]
    assert calls[1].kwargs["json"] == sent
    assert sent["DayPrg_0"] == WEEKDAY.replace("45", "50")
    assert calls[1].kwargs["headers"]["seriale"] == "S"
    counts = c.request_counts()
    assert (counts["settings"], counts["command"]) == (1, 1)


def test_bad_schedule_is_never_posted():
    c = client([resp(200, {"success": True, "result": {**SCHEDULE, "DayPrg_2": "oops"}})])
    with pytest.raises(api.ThermowattApiError, match="format"):
        c.set_scheduled_temperature("S", 50)
    assert endpoints(c) == ["raw-temperatures"]


def test_invalid_temperature_is_refused_before_posting():
    from custom_components.thermann.schedule import ScheduleError

    c = client([resp(200, {"success": True, "result": SCHEDULE})])
    with pytest.raises(ScheduleError):
        c.set_scheduled_temperature("S", 20)
    assert endpoints(c) == ["raw-temperatures"]


def test_comfort_and_settings_calls():
    c = client([resp(200, {"success": True, "result": {"TComfort": 20}}), resp(200, {"success": True})])
    assert c.get_settings("S") == {"TComfort": 20}
    c.set_comfort_temperature("S", 22)
    calls = c.session.request.call_args_list
    assert calls[0].args[0] == "GET" and calls[0].args[1].endswith("/waterheater-settings")
    assert calls[1].args[0] == "POST" and calls[1].args[1].endswith("/tcomfort")
    assert calls[1].kwargs["json"] == {"tcomfort": 22}
