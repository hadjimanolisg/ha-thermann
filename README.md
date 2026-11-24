# Thermowatt Smart for Home Assistant

A native Home Assistant **integration** (not an add-on) for Thermowatt-based
smart electric water heaters, including the Australian **Thermann Smart
Electric** range. Talks directly to the Thermowatt cloud API used by the
MyThermowatt / Thermann Control apps - no separate MQTT broker or Docker
add-on required.

This started as a rewrite of
[waterheater-dev/ha-thermowatt-heater](https://github.com/waterheater-dev/ha-thermowatt-heater)
(an add-on), restructured as a proper `custom_components` integration with:

- Config flow (UI setup, no YAML)
- Your password is used once to sign in and is **not stored**. Only the session tokens are kept (in `.storage/core.config_entries`, which is plain JSON - treat HA backups as sensitive)
- No dependency on Mosquitto/MQTT - polling happens inside HA itself via a `DataUpdateCoordinator`
- TLS certificate verification enabled on all requests (the original add-on had it disabled)

## Installation

### Via HACS (custom repository)
1. HACS -> Integrations -> ⋮ -> **Custom repositories**
2. Add this repo's URL, category **Integration**
3. Search for "Thermowatt Smart", install, restart Home Assistant

### Manual
Copy `custom_components/thermann/` into your `config/custom_components/` directory and restart Home Assistant.

## Setup

Settings -> Devices & Services -> Add Integration -> "Thermowatt Smart".
Enter your Thermann Control / MyThermowatt email and password.

If setup fails with an auth error, expand "App version" in the form and set
it to match your installed app's current version number (Thermowatt's
backend has been observed to reject requests claiming an old app version).

## What you get

One device per heater on your account, with:

**Water heater**
- Current and target temperature, with the allowed range read from the heater itself (20-70 °C on a Thermann Smart Electric)
- Operation modes: `off`, `eco`, `manual`, `auto`, `holiday`
- Target temperature shows what the heater is aiming for **right now**: your setpoint in manual; in auto, the schedule temperature during scheduled hours and the "T comfort auto" temperature outside them
- The temperature control is only offered in **manual** mode. The Thermowatt API sets the temperature through manual mode, so changing it in any other mode would also switch the heater to manual and off its schedule. To change it, switch to manual first.
- Attributes: `desired_temperature` (current target), `manual_setpoint`, `schedule_comfort_temperature` (auto, during scheduled hours), `boost_temperature`, `time_remaining`, `frost_protection_temperature`, `demand_response_status`

**Sensors**
- `Temperature` - current water temperature
- `Energy` - estimated kWh, from heating time x element power (set the element's kW under Configure)
- `Ambient temperature` (diagnostic)
- `Wi-Fi signal` in dBm (diagnostic, disabled by default; enable it in the entity settings)

**Binary sensors**
- `Heating` - the element is on right now; raw device flags in the `status_flags` attribute
- `Fault` (diagnostic) - on when the heater reports an error code; codes in the attributes

The device page also shows the heater and Wi-Fi module firmware versions.

Everything comes from one `/status` request per heater per poll; extra entities don't add requests.

## Polling and rate limits

Thermowatt's cloud locks accounts that make too many requests. This integration:

- Polls `/status` once per heater every **60 s** by default (configurable, 30-3600 s, under Configure).
- Backs off (doubling, up to 15 min, honouring `Retry-After`) after HTTP 429 or other errors, and returns to the normal interval after a success.
- Never retries a password login on its own. If the session can't be refreshed, polling stops and Home Assistant asks you to re-authenticate.
- Saves the session tokens, so restarting Home Assistant doesn't send a new login.
- Does not store your password. If the session can't be refreshed (for example after a long outage), Home Assistant shows a **Re-authenticate** prompt and you enter it again.

If your account was locked, wait until you can sign in to the app again before re-authenticating.

## Known limitations

- Cloud-dependent: if Thermowatt's backend or your internet is down, this integration can't reach your heater (same as the official app).
- The API was reverse-engineered from app traffic, not officially documented or supported by Thermowatt/Thermann. Endpoint or auth changes on their end can break this without notice - if that happens, check whether the app version bumped and update the `app_version` option first.
- Not affiliated with or endorsed by Thermowatt, Thermann, Rheem, or Ariston.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

The tests run the integration inside a real Home Assistant core with the cloud API mocked. GitHub Actions also runs `hassfest` and HACS validation on every push and weekly.
