"""Constants for the EP Cube integration."""
from __future__ import annotations

DOMAIN = "ep_cube"

# Config-entry keys
CONF_BASE_URL = "base_url"           # e.g. https://monitoring-eu.epcube.com — data + auth host (mobile API)
CONF_API_PREFIX = "api_prefix"       # path prefix the cloud expects; "/api" for EU/JP, "/app-api" for US
CONF_REGION = "region"               # one of REGIONS keys (EU/US/JP) or REGION_OTHER for a custom endpoint
CONF_DEV_ID = "dev_id"               # e.g. "5613" — small int, used by most endpoints
CONF_SG_SN = "sg_sn"                 # 21-digit serial, used by homeDeviceInfo
CONF_USERNAME = "username"           # email used to log into EP Cube mobile app
CONF_PASSWORD = "password"           # password used to log into EP Cube mobile app (stored encrypted at rest by HA)
CONF_BEARER_TOKEN = "bearer_token"   # cached Bearer token; refreshed in-memory via captcha login on 403
CONF_CAPACITY_KWH = "capacity_kwh"   # cached fallback; primary source is batteryPackNum × EP_CUBE_PACK_KWH

# Per-pack capacity. EP Cube is a single-module product line; all packs are
# 5.0 kWh. System capacity = batteryPackNum × this. Sourced from the real-cloud
# homeDeviceInfo response which exposes batteryPackNum but not systemCapacity.
EP_CUBE_PACK_KWH = 5.0

# First-generation (EP Cube 1.0) battery modules are 3.3 kWh each. Gen 1
# systems don't send `systemCapacity` in deviceList; instead capacity is
# derived from the battery serials listed under deviceList's
# `snData[].batterySnList` (one entry per module) × this.
EP_CUBE_GEN1_PACK_KWH = 3.3

DEFAULT_BASE_URL = "https://monitoring-eu.epcube.com"
DEFAULT_API_PREFIX = "/api"
DEFAULT_POLL_INTERVAL_SECONDS = 60   # real cloud polls at ~55s; we match
DEFAULT_PRICE_POLL_INTERVAL_SECONDS = 900  # dynamicPrice/calcuPrice: half-hourly prices, published once a day
DEFAULT_STATS_POLL_INTERVAL_SECONDS = 300  # queryDataElectricityV2 today bucket — slow-moving cumulative kWh

# queryDataElectricityV2 scopeType values. Date-string formats below are what
# the cube expects per scope; coordinator builds them with these in mind.
STATS_SCOPE_TOTAL = 0    # YYYY     (lifetime; currently == ANNUAL until year-roll)
STATS_SCOPE_DAILY = 1    # YYYY-MM-DD
STATS_SCOPE_MONTHLY = 2  # YYYY-MM
STATS_SCOPE_ANNUAL = 3   # YYYY

# Region endpoints. The US region notably uses a different hostname AND a
# different path prefix ("/app-api" instead of "/api"), so both pieces are
# stored and threaded through api.py + captcha.py.
REGION_EU = "EU"
REGION_US = "US"
REGION_JP = "JP"
REGION_OTHER = "other"

REGIONS: dict[str, dict[str, str]] = {
    REGION_EU: {"base_url": "https://monitoring-eu.epcube.com", "api_prefix": "/api"},
    REGION_US: {"base_url": "https://epcube-monitoring.com", "api_prefix": "/app-api"},
    REGION_JP: {"base_url": "https://monitoring-jp.epcube.com", "api_prefix": "/api"},
}

DEFAULT_REGION = REGION_EU

# Operating-mode names. Internal API stays in these strings; mapping to
# the cloud's "workStatus" numeric codes happens in api.py.
OPERATING_MODE_SELF_CONSUMPTION = "self_consumption"
OPERATING_MODE_TOU = "time_of_use"
OPERATING_MODE_BACKUP = "backup"

# Wire-side workStatus codes (strings on the wire).
# Warranty allowance for the battery modules: 80% of capacity until the earlier
# of the warranty end date or this many full-equivalent charge-discharge cycles
# (EP Cube Limited Warranty Statement V2.0).
BATTERY_WARRANTY_CYCLES = 6000

WORK_STATUS_SELF_CONSUMPTION = "1"
WORK_STATUS_TOU = "2"
WORK_STATUS_BACKUP = "3"

OPERATING_MODE_TO_WORK_STATUS: dict[str, str] = {
    OPERATING_MODE_SELF_CONSUMPTION: WORK_STATUS_SELF_CONSUMPTION,
    OPERATING_MODE_TOU: WORK_STATUS_TOU,
    OPERATING_MODE_BACKUP: WORK_STATUS_BACKUP,
}
WORK_STATUS_TO_OPERATING_MODE: dict[str, str] = {
    v: k for k, v in OPERATING_MODE_TO_WORK_STATUS.items()
}

# Synthetic prices for the shim's tier translation. The real cloud's TOU is
# price-tier based — we tell it "off-peak" / "mid-peak" / "peak" with prices,
# and the inverter optimises. Predbat's mental model is "charge/hold/discharge"
# with explicit slots; the shim maps that onto tiers using these synthetic
# prices, spaced wide enough that there's no ambiguity in the inverter's
# decisions.
#
# Values chosen to be obviously synthetic: above Agile's 100p/kWh daily cap so
# no realistic UK tariff hits them, and the repeating-digit pattern
# (2.22/3.33/4.44) makes shim slots visually identifiable if a user spots one
# in the EP Cube mobile app's schedule view.
#
# Legacy values were 0.01 / 0.20 / 1.00 — replaced in v0.6.3 because 1.00
# (peak) collided with realistic fixed-tariff peak prices, causing the
# strip-shim-slots logic to silently drop genuine user slots from the editor
# card's hydrated view. The v0.6.3 migration window kept the legacy values
# in the strip set for one release; v0.7 dropped them since any in-flight
# pre-v0.6.3 overrides had rotated out by then.
SHIM_PRICE_OFF_PEAK = 2.22
SHIM_PRICE_MID_PEAK = 3.33
SHIM_PRICE_PEAK = 4.44

# touType values (only meaningful with workStatus "2"):
#   0 = Time-of-use with fixed slots
#   1 = Dynamic tariff — the cube follows an Octopus price feed and acts on
#       the buy-below / sell-above thresholds below.
TOU_TYPE_STANDARD = 0
TOU_TYPE_DYNAMIC = 1

# How the shim realises Predbat's "force export" (discharge) windows.
#   tou_peak — legacy: TOU peak slot, battery drains to the house only; the
#              cloud has no command that pushes battery energy to the grid.
#   dynamic  — Dynamic-tariff mode with a low sell-above threshold: the cube
#              itself exports (observed 2026-10-14: ~6.4 kW to the grid) until
#              the Reserve SoC, which the shim sets to Predbat's target SoC.
CONF_FORCE_EXPORT_MODE = "force_export_mode"
FORCE_EXPORT_MODE_TOU_PEAK = "tou_peak"
FORCE_EXPORT_MODE_DYNAMIC = "dynamic"
DEFAULT_FORCE_EXPORT_MODE = FORCE_EXPORT_MODE_TOU_PEAK

# How the shim realises Predbat's "force charge" windows.
#   tou_offpeak - legacy: a time-of-use off-peak slot. The cloud's TOU model has no
#                 per-slot SoC target, so the cube charges until the slot ends.
#   dynamic     - Dynamic-tariff mode with buy-below at the maximum: the cube charges
#                 from the grid up to the Charging Limit SoC, which the shim sets to
#                 Predbat's target (verified 2026-10-15: ~6.5 kW, stopped at the limit).
CONF_FORCE_CHARGE_MODE = "force_charge_mode"
# One-shot checkbox in Configure: walk through the cloud's Dynamic-tariff pickers
# (country, region, supplier, tariffs) and save the choice with saveBinding.
CONF_CHANGE_TARIFF = "change_tariff"
# Optional names for the four PV strings, set in Configure and used in the sensor names
# ("East roof power" instead of "PV1 power"). Blank keeps the default. Entity and unique
# IDs never change with a name.
PV_NAME_KEYS = ("pv1_name", "pv2_name", "pv3_name", "pv4_name")
PV_NAME_MAX_LENGTH = 40
FORCE_CHARGE_MODE_TOU_OFFPEAK = "tou_offpeak"
FORCE_CHARGE_MODE_DYNAMIC = "dynamic"
DEFAULT_FORCE_CHARGE_MODE = FORCE_CHARGE_MODE_TOU_OFFPEAK
# A Dynamic charge is checked this many minutes after it starts: if the battery is
# not charging from the grid then, the shim says so and re-sends the write once.
DYNAMIC_CHARGE_CHECK_MINUTES = 5

# Dynamic-tariff thresholds used for a forced export, in pence/kWh. The cube
# compares them with the export price of the tariff chosen in the app: sell-above
# 0.00 means "export at any price above zero", whatever the tariff or the time of
# day (verified 2026-10-15 on a real cube: 6.19 kW to the grid with buy 0 / sell
# 0, so 0 is not read as "selling off"); buy-below 0 means "never buy from the
# grid" (an import price below 0p, i.e. paid-to-consume, is the only exception).
DYNAMIC_EXPORT_SELL_ABOVE_P = 0.0
DYNAMIC_EXPORT_BUY_BELOW_P = 0.0
# Earlier builds wrote a sell-above of up to 5p. A cube found in Dynamic mode with
# buy 0 and a sell-above in 0..this is still recognised as a leftover shim export.
DYNAMIC_EXPORT_LEGACY_SELL_ABOVE_MAX_P = 5.0

# Dynamic-tariff thresholds for a forced grid CHARGE (an experiment, see
# PredbatShim.debug_dynamic_charge): buy-below at the app's maximum so the cube
# always buys, sell-above at the maximum so it never exports. The Charging Limit
# SoC (backupPowerReserveSoc) is the stopping point. Both at 999.99 is also the
# shim's fingerprint for a charge it left behind; the user's hold setting (buy 0,
# sell 999.99) does not match it.
DYNAMIC_CHARGE_BUY_BELOW_P = 999.99
# The safe "hold" thresholds a Dynamic revert leaves stored in the cloud: never
# buy (the app's minimum is 0.00), never sell. The cloud keeps the last Dynamic
# thresholds after a switch back to Self-Consumption / TOU, so without this a
# later switch to Dynamic would resume a forced export or charge.
DYNAMIC_HOLD_BUY_BELOW_P = 0.0
DYNAMIC_HOLD_SELL_ABOVE_P = 999.99
DYNAMIC_CHARGE_SELL_ABOVE_P = 999.99
# Charging Limit SoC the cube ships with; restored when a stale shim charge is found.
DEFAULT_CHARGING_LIMIT_PCT = 100

# The cube's self-consumption reserve default. When a stale shim Dynamic export
# is found at baseline time (HA restarted mid-window) the reserve it left
# behind is the export target, not something the user chose, so the recovered
# baseline falls back to this.
DEFAULT_SELF_CONSUMPTION_RESERVE_PCT = 20

# After a dynamic export window should have ended, check the cube really left
# Dynamic mode this many minutes later and retry the revert if not. A cube
# stuck in Dynamic mode would not take the cheap-rate charge.
DYNAMIC_REVERT_WATCHDOG_MINUTES = 5
DYNAMIC_REVERT_RETRY_SECONDS = 60
DYNAMIC_REVERT_MAX_RETRIES = 5

