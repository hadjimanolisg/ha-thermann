"""Read-only look at what the Thermowatt cloud API returns for your account.

Makes exactly these requests, one second apart, and nothing else:
  1. POST /login        (one login)
  2. GET  /user-info
  3. GET  /status       (once per heater)
No commands are sent; nothing on the heater changes.

Run it from a terminal (not inside Home Assistant):
    python3 -I tools/thermowatt_probe.py

It spends 3+ requests of the account's daily quota (about 500, shared with
Home Assistant and the app), so run it only when you need to see the raw API,
for example after a firmware or app update. It stops at the first rate-limit
or quota reply and never retries.

Writes thermowatt_probe.json next to this script with secrets removed:
tokens, email, password, serials, MAC/IP/Wi-Fi, names, locations and IDs
are replaced by placeholders. Field names and all other values are kept so
the payload can be compared with what the integration uses. The file is
git-ignored; review it before sharing it anywhere.

Standard library only (Python 3.9+). Your password is read with getpass and
never written anywhere.
"""
from __future__ import annotations

import getpass
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "https://myapp-connectivity.com/api/v1"
API_KEY = "YVjArWssxKH631jv1dnnWOTr6gijsSAGz7rQJ4hJoUNRffxYvbQaMbePBEZalena"  # the app's shared key
OUT = pathlib.Path(__file__).with_name("thermowatt_probe.json")

SENSITIVE_KEY = re.compile(
    r"token|passw|e?mail|^user(name)?$|serial|^mac|ssid|wifi|^ip(addr(ess)?)?$|"
    r"^lat|^lon|^lng|latitude|longitude|address|indirizzo|^via$|citta|city|^cap$|zip|phone|telefono|"
    r"nome|name|cognome|surname|uuid|device_id|^id$|_id$|owner|account",
    re.I,
)
DROP_HEADERS = {"set-cookie", "authorization", "cookie"}


def headers(app_version, token=None, api_key=False, serial=None):
    h = {"app": "MyThermowatt", "platform": "iOS", "version": app_version, "lang": "en",
         "Content-Type": "application/json", "Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if api_key:
        h["x-api-key"] = API_KEY
    if serial:
        h["seriale"] = serial
    return h


def call(method, path, hdrs, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status, rh, raw = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        status, rh, raw = e.code, dict(e.headers), e.read()
    try:
        payload = json.loads(raw or b"null")
    except ValueError:
        payload = {"_non_json_body": raw[:500].decode(errors="replace")}
    time.sleep(1)
    return status, {k: v for k, v in rh.items() if k.lower() not in DROP_HEADERS}, payload


def redact(obj, secrets, counters):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if SENSITIVE_KEY.search(str(k)) and not isinstance(v, (dict, list)):
                out[k] = f"<redacted {type(v).__name__}>" if v not in (None, "") else v
            else:
                out[k] = redact(v, secrets, counters)
        return out
    if isinstance(obj, list):
        return [redact(v, secrets, counters) for v in obj]
    if isinstance(obj, str):
        for secret, label in secrets.items():
            if secret and secret in obj:
                obj = obj.replace(secret, label)
    return obj


def main():
    print(__doc__.split("\n\n")[1])
    email = input("Thermowatt email: ").strip()
    password = getpass.getpass("Thermowatt password (hidden): ")
    app_version = input("App version [3.14]: ").strip() or "3.14"
    if input("Send these requests now? [y/N]: ").strip().lower() != "y":
        sys.exit("Cancelled, nothing sent.")

    report = {"app_version": app_version, "calls": []}
    secrets = {email: "<email>"}

    status, rh, body = call("POST", "/login", headers(app_version, api_key=True),
                            {"username": email, "password": password, "device_id": str(uuid.uuid4())})
    password = None
    report["calls"].append({"call": "POST /login", "status": status, "headers": rh, "body": body})
    login_ok = (
        status == 200
        and isinstance(body, dict)
        and body.get("success") is not False
        and isinstance(body.get("result"), dict)
        and "accessToken" in body["result"]
    )
    if not login_ok:
        error = body.get("error") if isinstance(body, dict) else None
        print(f"Login failed (HTTP {status}{', ' + str(error) if error else ''}); stopping (no retry).")
    else:
        token = body["result"]["accessToken"]
        secrets[token] = "<access token>"
        secrets[body["result"].get("refreshToken", "")] = "<refresh token>"

        status, rh, info = call("GET", "/user-info", headers(app_version, token))
        report["calls"].append({"call": "GET /user-info", "status": status, "headers": rh, "body": info})

        devices = (info or {}).get("result", {}).get("termostati", []) if status == 200 else []
        for i, dev in enumerate(devices, 1):
            serial = dev.get("seriale")
            if not serial:
                continue
            secrets[serial] = f"<serial {i}>"
            status, rh, st = call("GET", "/status", headers(app_version, token, serial=serial))
            report["calls"].append({"call": f"GET /status (heater {i})", "status": status, "headers": rh, "body": st})
            error = st.get("error") if isinstance(st, dict) else None
            if status == 429 or (error and "limit exceeded" in str(error).lower()):
                print("Rate limited or daily quota reached; stopping.")
                break

    counters = {}
    OUT.write_text(json.dumps(redact(report, secrets, counters), indent=2, ensure_ascii=False))
    print(f"\nDone: {len(report['calls'])} requests. Redacted output written to {OUT.name}.")
    print("Check it before sharing it.")


if __name__ == "__main__":
    main()
