"""Shared fixtures."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.thermann import api

# Shaped like a real /status response (every value is a string).
STATUS = {
    "VolatileErrors": "0",
    "NonVolatileErrors": "0",
    "T_SetPoint": "60",
    "Cmd": "9",
    "WaterHeaterSts": "1",
    "T_dsrd": "60",
    "T_Avg": "55",
    "TimeRemaining": "0",
    "T_set_min": "20",
    "T_set_max": "70",
    "T_set_off": "16",
    "Tmec_SW_Version": "07.00.03.04",
    "Wifi_SW_Version": "01.02.03",
    "TAmb": "33",
    "Rssi": "-80",
    "Drm_status": "0",
    "Tmsch": "45",
    "TBoost": "70",
}

BASE_DATA = {"email": "user@example.com", "app_version": "3.14", "device_uuid": "dev-uuid"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeClient:
    """Stands in for ThermowattClient. Records every call in `calls`."""

    calls: list[str] = []
    behaviour: dict = {}
    instances: list["FakeClient"] = []

    def __init__(self, email, password, device_uuid, app_version, access_token=None, refresh_token=None):
        self.password = password
        self.device_uuid = device_uuid
        self._t = (access_token, refresh_token)
        self.session = MagicMock()
        FakeClient.instances.append(self)

    counting_since = 0.0

    def request_counts(self):
        kinds = {"status": "status", "list": "device_list", "login": "login",
                 "set_temperature": "command", "set_mode": "command", "settings": "settings",
                 "set_scheduled_temperature": "command", "set_comfort_temperature": "command"}
        counts = dict.fromkeys(api.REQUEST_KINDS, 0)
        for call in FakeClient.calls:
            if call in kinds:
                counts[kinds[call]] += 1
        counts["total"] = sum(counts.values())
        return counts

    @property
    def has_tokens(self):
        return all(self._t)

    @property
    def tokens(self):
        return self._t

    def _run(self, name, default):
        FakeClient.calls.append(name)
        fn = FakeClient.behaviour.get(name)
        return fn(self) if fn else default

    def login(self):
        if not self.password:
            FakeClient.calls.append("login")
            raise api.ThermowattAuthError("no password")
        self._run("login", None)
        self._t = ("new-access", "new-refresh")

    def list_devices(self):
        return self._run("list", [{"seriale": "SN1", "nome": "Boiler", "type": "TMEC", "idModello": 3}])

    def get_status(self, serial):
        return self._run("status", dict(STATUS))

    def set_temperature(self, serial, temp):
        self._run("set_temperature", None)

    def set_mode(self, serial, mode, setpoint=60):
        self._run("set_mode", None)

    def get_settings(self, serial):
        return self._run("settings", {"TBoost": None, "BoostDuration": None, "TComfort": 20})

    def set_scheduled_temperature(self, serial, temp):
        FakeClient.sent.append(("scheduled", temp))
        return self._run("set_scheduled_temperature", None)

    def set_comfort_temperature(self, serial, temp):
        FakeClient.sent.append(("comfort", temp))
        return self._run("set_comfort_temperature", None)


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.calls = []
    FakeClient.behaviour = {}
    FakeClient.instances = []
    FakeClient.sent = []
    monkeypatch.setattr("custom_components.thermann.ThermowattClient", FakeClient)
    monkeypatch.setattr("custom_components.thermann.config_flow.ThermowattClient", FakeClient)
    return FakeClient
