"""Constants for the Thermann Smart integration."""

DOMAIN = "thermann"

CONF_APP_VERSION = "app_version"
DEFAULT_APP_VERSION = "3.14"

# Extracted from the MyThermowatt / Thermann Control app. Identifies requests
# as coming from "the app" - not a user secret, shared across all app users.
API_KEY = "YVjArWssxKH631jv1dnnWOTr6gijsSAGz7rQJ4hJoUNRffxYvbQaMbePBEZalena"
BASE_URL = "https://myapp-connectivity.com/api/v1"

# The vendor's daily request quota is undocumented. Other projects measured a
# "Daily request limit exceeded" rejection after ~469 requests in a UTC day,
# and two rejections in three days at 60 s polling (~1,500/day). The app
# probably shares the same quota. 300 s is ~290 polls/day plus token
# refreshes; the 240 s minimum keeps even worst-case volume under ~460/day.
# Configurable in the options flow; saved values below the minimum are raised.
CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_INTERVAL = 300  # seconds
MIN_POLL_INTERVAL = 240
MAX_POLL_INTERVAL = 3600
# Backoff ceilings: transient errors / HTTP 429, and an exhausted daily quota
# (reset time unknown; one probe an hour costs at most 24 requests a day).
MAX_BACKOFF_INTERVAL = 900
QUOTA_BACKOFF_INTERVAL = 3600

# Keys under which tokens are persisted in the config entry, so a restart
# refreshes the session instead of sending a new password login.
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_DEVICE_UUID = "device_uuid"
# Heater list (serial, name, type) from the last successful /user-info, so a
# restart while rate limited can still set up and wait instead of reauth.
CONF_DEVICES = "devices"

# Cmd is a bit field: bit 0 = on, the other bits = mode family. The same
# family with bit 0 cleared means the heater is off (e.g. 8 = turned off from
# manual, 16 = from auto, 64 = from holiday; 8 and 16 seen on real heaters).
CMD_ON_BIT = 1
CMD_FAMILIES = {
    2: "eco",  # 3 when on
    8: "manual",  # 9 when on
    16: "auto",  # 17 when on
    64: "holiday",  # 65 when on
}

OPERATION_MODES = ["off", "eco", "manual", "auto", "holiday"]

# Fallback limits only: the heater reports its own in /status as T_set_min /
# T_set_max (observed 20 / 70 on a Thermann Smart Electric).
MIN_TEMP = 20
MAX_TEMP = 70
DEFAULT_TEMP = 60

CONF_ELEMENT_KW = "element_kw"
DEFAULT_ELEMENT_KW = 3.6  # typical AU electric HWS element size; user-configurable