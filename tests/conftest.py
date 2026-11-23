"""Shared fixtures."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.thermann import api

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
        return self._run("list", [{"seriale": "SN1", "nome": "Boiler"}])

    def get_status(self, serial):
        return self._run("status", {"T_Avg": 55, "T_SetPoint": 60, "Cmd": 9, "WaterHeaterSts": 1})

    def set_temperature(self, serial, temp):
        self._run("set_temperature", None)

    def set_mode(self, serial, mode, setpoint=60):
        self._run("set_mode", None)


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.calls = []
    FakeClient.behaviour = {}
    FakeClient.instances = []
    monkeypatch.setattr("custom_components.thermann.ThermowattClient", FakeClient)
    monkeypatch.setattr("custom_components.thermann.config_flow.ThermowattClient", FakeClient)
    return FakeClient
