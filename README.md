# Thermann Smart for Home Assistant

A native Home Assistant **integration** (not an add-on) for Thermowatt-based
smart electric water heaters, including the Australian **Thermann Smart
Electric** range. Talks directly to the Thermowatt cloud API used by the
MyThermowatt / Thermann Control apps - no separate MQTT broker or Docker
add-on required.

Based on earlier community work (see [Credits](#credits)), restructured as a proper `custom_components` integration with:

- Config flow (UI setup, no YAML)
- Your password is used once to sign in and is **not stored**. Only the session tokens are kept (in `.storage/core.config_entries`, which is plain JSON - treat HA backups as sensitive)
- No dependency on Mosquitto/MQTT - polling happens inside HA itself via a `DataUpdateCoordinator`
- TLS certificate verification enabled on all requests (the original add-on had it disabled)

## Installation

### Via HACS (custom repository)
1. HACS -> Integrations -> ⋮ -> **Custom repositories**
2. Add `https://github.com/m4dm4xcy/ha-thermann`, category **Integration**
3. Search for "Thermann Smart", install, restart Home Assistant. HACS installs the latest [release](https://github.com/m4dm4xcy/ha-thermann/releases) and notifies you of new ones.

### Manual
Copy `custom_components/thermann/` into your `config/custom_components/` directory and restart Home Assistant.

## Setup

Settings -> Devices & Services -> Add Integration -> "Thermann Smart".
Enter your Thermann Control / MyThermowatt email and password.

If setup fails with an auth error, expand "App version" in the form and set
it to match your installed app's current version number (Thermowatt's
backend has been observed to reject requests claiming an old app version).

### A separate account for Home Assistant (recommended)

The app can share a heater with another account. Giving Home Assistant its
own account keeps a lockout or rate limit caused by Home Assistant away from
the account you use in the app, and probably gives Home Assistant its own
daily request quota.

1. Create a second account in the app and share the heater with it. Sign in
   with it once and check it can change the mode and temperature.
2. In Home Assistant: Settings -> Devices & Services -> Thermann Smart ->
   the three dots -> **Reconfigure**, then sign in with the new account.

Reconfigure keeps everything: entities, names, areas, settings and the
energy total. It refuses an account that doesn't have the heater. You can
also use it to enter a new password for the same account.

## What you get

One device per heater on your account, with:

**Water heater**
- Current and target temperature, with the allowed range read from the heater itself (20-70 °C on a Thermann Smart Electric)
- Operation modes: Off, Eco, Manual, Auto, Holiday (`off`, `eco`, `manual`, `auto`, `holiday` in automations). "Off" is recognised whichever mode the heater was turned off from.
- Target temperature shows what the heater is aiming for **right now**: your setpoint in manual; in auto, the schedule temperature during scheduled hours and the "T comfort auto" temperature outside them
- The temperature control is only offered in **manual** mode. The Thermowatt API sets the temperature through manual mode, so changing it in any other mode would also switch the heater to manual and off its schedule. To change it, switch to manual first. In auto mode, use the two auto temperatures below instead.
- Mode and temperature changes show in Home Assistant as soon as the heater's cloud accepts them; the next poll then shows what the heater actually reports. In eco, auto and holiday the target temperature updates at that poll, because it comes from the heater.
- Attributes: `desired_temperature` (current target), `manual_setpoint`, `schedule_comfort_temperature` (auto, during scheduled hours), `boost_temperature`, `time_remaining`, `frost_protection_temperature`, `demand_response_status`

**Sensors**
- `Temperature` - current water temperature
- `Energy` - estimated kWh, from heating time x element power (set the element's kW under Configure). Only time between successful polls counts, so after an outage it can under-count but never over-count.
- `Ambient temperature` (diagnostic)
- `Wi-Fi signal` in dBm (diagnostic, disabled by default; enable it in the entity settings)
- `API requests (24 h)` (diagnostic) - requests this integration sent to the Thermowatt cloud in the last 24 hours, split into status polls, token renewals, logins, commands and device-list fetches. Counted locally (costs nothing); it starts again when Home Assistant restarts (`counting_since`, `full_day`). Compare it with the app's daily-limit warnings.
- `Time remaining` in minutes (diagnostic) - appears to be the heater's estimate of time left to reach the target while heating; 0 when idle

**Auto mode temperatures** (number controls)
- `Scheduled temperature` - what the heater keeps during the schedule's hours (45 °C in the app's default). Changing it works like the app's dial in auto: the schedule is read, its highest temperature replaced, and the schedule sent back (2 requests). Only in auto mode, because sending the schedule also selects auto. The hours themselves never change; edit those in the app.
- `Comfort temperature` - what the heater keeps outside those hours (the app's "T comfort auto", e.g. 20 °C). Must stay below the scheduled temperature. 1 request per change; its current value is read once when Home Assistant starts (1 request per heater), so a change made in the app shows here after the next restart.

**Binary sensors**
- `Heating` - the element is on right now; raw device flags in the `status_flags` attribute
- `Fault` (diagnostic) - on when the heater reports an error code; codes in the attributes
- `Anti-legionella cycle` (diagnostic) - on while the heater runs its sanitation cycle (inferred from another project's observations)
- `High ambient pause` (diagnostic) - heating is paused because it's too hot around the heater; it resumes by itself, so this isn't counted as a fault
- `Boost` - boost is running (the same status bit the app uses)

On Home Assistant 2026.3 and later the integration shows the Thermowatt logo, with a light version for dark themes. The device page also shows the heater and Wi-Fi module firmware versions, and offers **Download diagnostics** for bug reports (email, tokens, serial numbers and names are removed).

Everything comes from one `/status` request per heater per poll; extra entities don't add requests. The only exceptions are the one-off settings read at startup and the requests a change itself needs.

## Polling and rate limits

Thermowatt's cloud has an undocumented **daily request quota** per account, shared with the Thermann app. Other projects measured a "Daily request limit exceeded" rejection after about **470 requests in a day**, and repeated rejections at 60 s polling (~1,500 requests a day). This integration:

- Polls `/status` once per heater every **300 s (5 min)** by default: about 290 requests a day plus token refreshes, leaving room for the app. Configurable under Configure, **240-3600 s**; a value saved by an older version below 240 s is raised to 240 s, and the Configure form starts at 240 s. Recommended: 300 s or more. If the form shows old text such as "30-3600", refresh the browser page.
- Recognises quota exhaustion however the API reports it: HTTP 429, or the more common HTTP 200 reply `{"success": false, "error": "Daily request limit exceeded"}`. Any other `success: false` reply counts as a failure too, never as a reading or a successful command.
- After the daily quota runs out, checks **once an hour** until it resets (the reset time is unknown), logs a warning and shows a notice under **Settings → Repairs**, which clears itself when readings resume. Other errors back off by doubling up to 15 min, honouring `Retry-After`. Polling returns to normal after the next good reading.
- Doesn't poll after a command: the cloud still reports the old state for a while, so a poll would cost a request and show stale values. The change is shown straight away instead.
- Refuses commands while the account is rate limited (nothing is queued or retried later), so they don't spend more of the quota.
- If Home Assistant restarts while the account is rate limited, the integration still starts (using the heater list from the last successful start), shows the entities as unavailable and waits for the backoff. It doesn't ask for your password again.
- Never retries a password login on its own. If the session can't be refreshed, polling stops and Home Assistant asks you to re-authenticate.
- Saves the session tokens, so restarting Home Assistant doesn't send a new login.
- Renews the session token shortly **before** it expires (it lasts about 30 minutes), instead of waiting for a request to be rejected. At 300 s polling that's about 340 requests a day instead of about 385. If the token's expiry can't be read, it falls back to renewing after a rejection.
- Does not store your password. If the session can't be refreshed (for example after a long outage), Home Assistant shows a **Re-authenticate** prompt and you enter it again.

If your account was locked, wait until you can sign in to the app again before re-authenticating. If you see the app's daily-limit warning, raise the poll interval and keep the app closed when you're not using it: it refreshes about every 20 s while open.

## Known limitations

- Cloud-dependent: if Thermowatt's backend or your internet is down, this integration can't reach your heater (same as the official app).
- The API was reverse-engineered from app traffic, not officially documented or supported by Thermowatt/Thermann. Endpoint or auth changes on their end can break this without notice - if that happens, check whether the app version bumped and update the `app_version` option first.
- Not affiliated with or endorsed by Thermowatt, Thermann, Rheem, or Ariston.

## Credits

This project builds on:

- [waterheater-dev/ha-thermowatt-heater](https://github.com/waterheater-dev/ha-thermowatt-heater) - the original Home Assistant add-on that reverse-engineered the Thermowatt cloud API.
- [MitchellSaunders/ha-thermann](https://github.com/MitchellSaunders/ha-thermann) - the port of that add-on to a native integration, which this repository was forked from.

Thermowatt and its logo are trademarks of Thermowatt S.p.A., used only to identify the heaters this integration works with. This is an independent project, not made or endorsed by Thermowatt.

Since then it has been largely reworked: rate-limit-safe polling and login handling, no stored password, re-authentication, device-reported limits, fault and diagnostic sensors, Home Assistant 2026.11 support, and tests. Report problems in [this repository's issues](https://github.com/m4dm4xcy/ha-thermann/issues), not upstream.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

The tests run the integration inside a real Home Assistant core with the cloud API mocked. CI runs them on the oldest supported Home Assistant (2024.11), a recent one and the newest the test harness supports.

To see what the real API returns for your account (for example after a firmware or app update), run the read-only probe from a terminal. It asks for your credentials at a hidden prompt, sends one login, one `/user-info` and one `/status` per heater (and, if you say yes, two more reads per heater: the auto schedule and the heater settings), and writes a redacted `tools/thermowatt_probe.json` (git-ignored). It uses part of the daily quota, so run it only when needed:

```bash
python3 -I tools/thermowatt_probe.py
``` GitHub Actions also runs `hassfest` and HACS validation on every push and weekly.
