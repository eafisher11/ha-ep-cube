"""EP Cube cloud API client.

Speaks the mobile-app contract on `monitoring-eu.epcube.com/api/device/*`
with `Authorization: Bearer <token>` auth. See docs/PHASE_3_2.md for the
wire reference and the spike notes that justify each typing decision.

Auth model: a Bearer token is supplied to the constructor. When a write
returns 403 we call the optional `reauth_callback` (provided at construct
time) to get a fresh token; if that yields a new token we retry once.
The callback hides the captcha-solver login flow from this client — at
the time this refactor landed (Phase 3.2 step 3), no callback exists yet
and 403s simply raise.

Wire-typing rules (the EP Cube cloud is inconsistently strict and returns
misleading 403 "token expired" for some payload-typing errors):
  - day arrays (`activeWeek`, `dayLightActiveWeek*`): list[str]
  - `touType`: native int
  - `dayLightSavingTime`: native bool
  - `weatherWatch`, `onlySave`, reserve SoCs, `workStatus`: str
  - `weatherWatch` always "0" (conflicts with Predbat) and `onlySave`
    always "0" (persisted state on the cube — must be cleared explicitly).

DeviceStatus field names are preserved from the prior implementation so
sensor.py is unchanged. The mobile-app `homeDeviceInfo` returns plain
numbers rather than the web-portal's kW-as-string values, but `_power_to_w`
already tolerates both.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlencode

import aiohttp
from homeassistant.util import dt as dt_util

from .const import (
    EP_CUBE_GEN1_PACK_KWH,
    EP_CUBE_PACK_KWH,
    OPERATING_MODE_BACKUP,
    OPERATING_MODE_SELF_CONSUMPTION,
    OPERATING_MODE_TOU,
    WORK_STATUS_TO_OPERATING_MODE,
)

_LOGGER = logging.getLogger(__name__)

ReauthCallback = Callable[[], Awaitable[str | None]]


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------
class EPCubeError(Exception):
    """Base exception for EP Cube API errors."""


class AuthError(EPCubeError):
    """Bearer token rejected or no token configured."""


class NoDynamicPricesError(EPCubeError):
    """The cloud has no Dynamic-tariff prices for this cube (no Dynamic tariff is set up,
    so `dynamicPrice/calcuPrice` answers with an HTTP-500-style "Server exception")."""


class WriteVerificationError(EPCubeError):
    """Post-write read-back showed the cube did not adopt the requested state."""


class RateLimitError(EPCubeError):
    """Cloud API rate limit hit (HTTP 429)."""


class ServerError(EPCubeError):
    """Cloud returned 5xx."""


# ----------------------------------------------------------------------
# HTTP headers — copied verbatim from the iOS app to maximise the chance
# the cloud serves us identically to a real mobile session.
# ----------------------------------------------------------------------
_DEFAULT_HEADERS = {
    "User-Agent": "ReservoirMonitoring/2.1.0 (iPhone; iOS 18.3.2; Scale/3.00)",
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-GB",
}


# ----------------------------------------------------------------------
# Data model exposed to coordinator / sensor.py — unchanged field names
# ----------------------------------------------------------------------
def _local_today() -> date:
    """Today's date in Home Assistant's configured time zone."""
    return dt_util.now().date()


@dataclass(slots=True)
class DeviceStatus:
    soc_pct: float
    soc_kwh: float | None   # None = cube sent 0 / no reading (sensor goes unavailable)
    capacity_kwh: float
    battery_power_w: float   # derived: solar + grid - load; >0 charging, <0 discharging
    grid_power_w: float      # >0 import, <0 export (verified 2026-05-22: cube returned gridPower:"-17" → -170 W matched EP Cube app's export reading)
    solar_power_w: float
    load_power_w: float      # backUpPower + nonBackUpPower + evPower
    operating_mode: str      # "self_consumption" | "time_of_use" | "backup"
    reserve_soc_pct: float   # per-mode reserve matching current workStatus
    allow_grid_charge: bool  # mirrors cube's allowChargingXiaGrid
    self_consumption_reserve_pct: float  # always-on self-consumption mode reserve
    backup_reserve_pct: float            # always-on backup mode reserve
    dst_active: bool                     # mirrors cube's dayLightSavingTime
    # Daily kWh counters from homeDeviceInfo. Cube's onboard accounting,
    # resets at midnight cube-local. solar_today + backup_today match the EP
    # Cube mobile app's daily totals; for clean grid import vs export split
    # use the Phase 4.2 stats sensors (sensor.ep_cube_grid_import_today /
    # ..._export_today) which read from queryDataElectricityV2.
    # solar_dc / solar_ac are pre-/post-inverter for diagnostic surface only.
    solar_today_kwh: float
    backup_today_kwh: float
    solar_dc_today_kwh: float
    solar_ac_today_kwh: float
    # cube's `selfHelpRate` field. Despite the EP Cube app's labelling, the
    # cube reports (load met from own generation) / load — i.e. self-SUFFICIENCY,
    # not self-consumption. Verified 2026-06-10: live cube showed 99% with
    # 0.23 kWh import on 14.36 kWh load = 98.4% self-sufficiency, while true
    # self-consumption (own use / own generation) on the same day was ~60%.
    # Field renamed in v1.2; sensor's `key` still reads `self_consumption_pct`
    # so existing users' unique_ids + Energy Dashboard wiring + automations
    # all survive the rename — only the display name + translation_key change.
    self_sufficiency_pct: float
    winter_protect_pct: float     # winter SoC floor; read-only (write path unconfirmed)
    # Phase 3.5 additions. Three direct-from-cloud fields piggyback on the
    # existing homeDeviceInfo poll, plus two client-side accumulators that
    # delta-track batteryCurrentElectricity between polls (the cloud
    # doesn't expose pre-split battery flow on this endpoint). Lifetime
    # totals NOT added here — they need queryDataElectricityV2 (Phase 4.2).
    earning_yesterday: float       # £ — cube's own revenue accounting (currency from unitDefault)
    grid_outage_count: int         # gridPowerFailureNum — diagnostic; lifetime count
    off_grid_seconds: int          # offGridPowerSupplyTime — diagnostic; lifetime seconds on backup
    battery_charge_today_kwh: float    # client-side delta-tracker; resets at midnight local
    battery_discharge_today_kwh: float # client-side delta-tracker; resets at midnight local
    # EV charger channels from homeDeviceInfo. `hasEv` gates whether the EV
    # sensors are registered at all, so installs without a charger don't get
    # permanently-zero entities. evPower uses the same 0.01 kW wire units as
    # the other power fields (confirmed on a US cube, 2026-09-30); evElectricity
    # is today's kWh, reset by the cube at its local midnight. Defaults keep
    # older callers (and payloads without these keys) working unchanged.
    has_ev: bool = False
    ev_power_w: float = 0.0
    ev_today_kwh: float = 0.0
    # The Dynamic tariff's buy-below / sell-above prices (pence per kWh), from
    # getSwitchMode. They mean something only while the cube is in Dynamic mode
    # (touType 1); otherwise the cloud leaves placeholder values (0 and 999.99)
    # that would read as real prices, so they are None then.
    dynamic_buy_below: float | None = None
    dynamic_sell_above: float | None = None
    # Running total of the battery's discharged energy since the integration
    # began tracking (or since it was seeded from the recorder). Built from the
    # same SoC deltas as battery_discharge_today_kwh, so it is an estimate.
    battery_discharge_lifetime_kwh: float = 0.0
    # Status fields the cloud already returns, exposed as diagnostics. All None
    # when the payload doesn't carry them (older firmware, other regions).
    # `is_*` come from homeDeviceInfo ("1"/"0" strings); the others are as sent.
    is_online: bool | None = None
    is_fault: bool | None = None
    is_alert: bool | None = None
    fault_warning_type: str | None = None   # a code; "1" on a healthy cube, meaning not documented
    signal_level: int | None = None         # 0-4
    system_status: int | None = None        # 4 = normal
    winter_mode: int | None = None
    # getSwitchMode: the charge limit SoC the cube holds (the shim writes it).
    charging_limit_soc: float | None = None
    # Meaning not known yet; surfaced disabled by default.
    lpp_time_duration: float | None = None
    lpc_time_duration: float | None = None
    backup_loads_mode: int | None = None
    # How the cube reaches the cloud: 1 = Wi-Fi, 3 = Ethernet (seen on real cubes; other
    # codes are not known). A cube on Ethernet reports signal_level 0.
    networking: int | None = None


@dataclass(frozen=True, slots=True)
class FirmwareInfo:
    """Firmware versions from the cloud's `queryFirmwareInfo` route."""

    firmware_version: str | None
    rtu_version: str | None
    auto_update: bool | None


@dataclass(frozen=True, slots=True)
class DeviceDetails:
    """Static identity of the cube from the cloud's `userDeviceInfo` route (the
    address and owner fields in that response are deliberately not kept)."""

    model: str | None
    serial_number: str | None
    rtu_serial_number: str | None


@dataclass(frozen=True, slots=True)
class WarrantyInfo:
    """The cube's warranty dates from the cloud's `getWarranty` route."""

    start: date | None
    end: date | None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _power_to_w(value: Any) -> float:
    """Coerce a power-typed value to float watts.

    The mobile-app `/api/device/homeDeviceInfo` returns power fields as a
    fixed-point integer representing centi-kilowatts (0.01 kW units), e.g.
    `solarPower:64` means 0.64 kW = 640 W. Confirmed empirically 2026-05-21:
    cube returned `solarPower:64` while the EP Cube mobile app displayed
    0.63 kW for the same poll, and `loadPower:66` displayed as 0.66 kW.
    Multiplier to W is therefore ×10, NOT ×1000 (which was correct for the
    web-portal's "5.01kW" string format but is a footgun on this surface).
    Tolerates string/number input + None/empty → 0.0."""
    if value is None or value == "":
        return 0.0
    return float(value) * 10.0


def _kwh_str_to_float(value: Any) -> float:
    """Coerce a kWh-typed value to float kWh. Same tolerance as `_power_to_w`."""
    if value is None or value == "":
        return 0.0
    return float(value)


def _battery_kwh_or_none(value: Any) -> float | None:
    """Parse `batteryCurrentElectricity`, treating 0 / missing / garbage as
    "no reading" (None) rather than 0.0 kWh.

    The cloud briefly reports 0 (or drops the field) around its midnight
    day-rollover. Passing that through as 0.0 made `battery_soc_kwh` plunge
    to zero, and the charge/discharge delta-tracker then booked the whole
    battery as discharged and recharged within two polls. A genuinely
    empty pack still holds its reserve, so an exact 0 is never a real
    reading worth recording.
    """
    if value is None or value == "":
        return None
    try:
        kwh = float(value)
    except (TypeError, ValueError):
        return None
    return kwh if kwh > 0 else None


def _truthy(value: Any) -> bool:
    """Coerce the cube's mixed boolean encodings (True, "1", 1, "true") to
    bool. Anything absent or unrecognised is False."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes")


def _capacity_string_to_kwh(value: Any) -> float:
    """Parse `deviceList`'s `systemCapacity: "20.0kWh"` into float kWh.
    Tolerates absent units."""
    if value is None:
        return 0.0
    s = str(value).strip().lower().removesuffix("kwh").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _battery_module_count(device: dict[str, Any]) -> int:
    """Count the battery modules in a deviceList row.

    Gen 1 rows carry `snData`: one entry per inverter stack, each with a
    `batterySnList` of module serials. `snItems` is NOT used — it also holds
    the inverter and base serials. Serials are de-duplicated so a module
    listed twice is only counted once."""
    sn_data = device.get("snData")
    if not isinstance(sn_data, list):
        return 0
    serials: set[str] = set()
    for stack in sn_data:
        if not isinstance(stack, dict):
            continue
        battery_sns = stack.get("batterySnList") or []
        if isinstance(battery_sns, str):
            battery_sns = battery_sns.split(",")
        if not isinstance(battery_sns, list):
            continue
        serials.update(str(sn).strip() for sn in battery_sns if sn and str(sn).strip())
    return len(serials)


def device_capacity_kwh(device: dict[str, Any]) -> tuple[float, bool]:
    """System capacity (kWh) for one deviceList row, plus whether it was
    derived from Gen 1 module serials.

    Prefers the cloud's own `systemCapacity` ("20.0kWh"). Gen 1 systems
    don't send that field, so fall back to the number of battery serials in
    `snData[].batterySnList` × EP_CUBE_GEN1_PACK_KWH (3.3 kWh). Returns
    (0.0, False) if neither is available."""
    capacity = _capacity_string_to_kwh(device.get("systemCapacity"))
    if capacity > 0:
        return capacity, False
    modules = _battery_module_count(device)
    if modules > 0:
        return round(modules * EP_CUBE_GEN1_PACK_KWH, 1), True
    return 0.0, False


# ----------------------------------------------------------------------
# Slot string + switchMode payload builder
# ----------------------------------------------------------------------
def build_slot(start_hhmm: str, end_hhmm: str, price: float) -> str:
    """Build a single TOU slot wire-string."""
    return f"{start_hhmm}_{end_hhmm}_{price:.2f}"


def _normalise_slots(slots: list[str] | None) -> list[str]:
    """Pass through a list of pre-built wire strings (or build empty)."""
    return list(slots or [])


def build_switch_mode_payload(
    dev_id: str,
    *,
    work_status: str,
    self_consumption_reserve_soc: int = 20,
    backup_reserve_soc: int = 100,
    allow_grid_charge: bool = True,
    # TOU schedule (used regardless of work_status — cube stores it for when
    # TOU mode is active; sending the baseline schedule on a self-consumption
    # switch is safe and matches the mobile-app behaviour).
    peak_slots: list[str] | None = None,
    mid_peak_slots: list[str] | None = None,
    off_peak_slots: list[str] | None = None,
    peak_slots_weekend: list[str] | None = None,
    mid_peak_slots_weekend: list[str] | None = None,
    off_peak_slots_weekend: list[str] | None = None,
    weekday_days: list[int | str] | None = None,
    weekend_days: list[int | str] | None = None,
    dst_active: bool = False,
    dst_peak_slots: list[str] | None = None,
    dst_mid_peak_slots: list[str] | None = None,
    dst_off_peak_slots: list[str] | None = None,
    dst_peak_slots_weekend: list[str] | None = None,
    dst_mid_peak_slots_weekend: list[str] | None = None,
    dst_off_peak_slots_weekend: list[str] | None = None,
    dst_weekday_days: list[int | str] | None = None,
    dst_weekend_days: list[int | str] | None = None,
    tou_type: int = 0,
    dynamic_charge_price: float | None = None,
    dynamic_discharge_price: float | None = None,
) -> dict[str, Any]:
    """Build the full /api/device/switchMode POST body.

    Every field is required — minimal 3-field calls return
    `500 "The parameter cannot be null ：weatherWatch"` (see PHASE_3_2.md).

    `dynamic_charge_price` / `dynamic_discharge_price` (pence/kWh, "buy below"
    / "sell above") are the Dynamic-tariff thresholds the cube reports in
    getSwitchMode when `touType` is 1. They are only added to the body when
    given, so ordinary TOU writes stay byte-identical to before.
    `weatherWatch` is forced to "0" because the forecast-driven pre-charge
    feature conflicts with Predbat's economic optimisation. `onlySave` is
    forced to "0" because it is persisted state on the cube — silently
    leaving the cube in save-only mode would be a footgun.

    Day arrays are coerced to list[str] — the cube rejects int arrays with
    a misleading `403 "token expired"`. The cube normalises them back to
    list[int] on read.
    """
    weekday_str = [str(d) for d in (weekday_days or [1, 2, 3, 4, 5])]
    weekend_str = [str(d) for d in (weekend_days or [6, 7])]
    dst_weekday_str = [str(d) for d in (dst_weekday_days or weekday_days or [1, 2, 3, 4, 5])]
    dst_weekend_str = [str(d) for d in (dst_weekend_days or weekend_days or [6, 7])]

    payload = {
        "devId": str(dev_id),
        "workStatus": str(work_status),
        "allowChargingXiaGrid": "1" if allow_grid_charge else "0",
        "weatherWatch": "0",
        "onlySave": "0",
        "selfConsumptioinReserveSoc": str(int(self_consumption_reserve_soc)),
        "backupPowerReserveSoc": str(int(backup_reserve_soc)),
        "touType": int(tou_type),
        "dayLightSavingTime": bool(dst_active),
        "peakTimeList": _normalise_slots(peak_slots),
        "midPeakTimeList": _normalise_slots(mid_peak_slots),
        "offPeakTimeList": _normalise_slots(off_peak_slots),
        "activeWeek": weekday_str,
        "peakTimeListNonWorkDay": _normalise_slots(peak_slots_weekend),
        "midPeakTimeListNonWorkDay": _normalise_slots(mid_peak_slots_weekend),
        "offPeakTimeListNonWorkDay": _normalise_slots(off_peak_slots_weekend),
        "activeWeekNonWorkDay": weekend_str,
        "dayLightPeakTimeList": _normalise_slots(dst_peak_slots),
        "dayLightMidPeakTimeList": _normalise_slots(dst_mid_peak_slots),
        "dayLightOffPeakTimeList": _normalise_slots(dst_off_peak_slots),
        "dayLightActiveWeek": dst_weekday_str,
        "dayLightPeakTimeListNonWorkDay": _normalise_slots(dst_peak_slots_weekend),
        "dayLightMidPeakTimeListNonWorkDay": _normalise_slots(dst_mid_peak_slots_weekend),
        "dayLightOffPeakTimeListNonWorkDay": _normalise_slots(dst_off_peak_slots_weekend),
        "dayLightActiveWeekNonWorkDay": dst_weekend_str,
    }
    if dynamic_charge_price is not None:
        payload["dynamicChargePrice"] = float(dynamic_charge_price)
    if dynamic_discharge_price is not None:
        payload["dynamicDischargePrice"] = float(dynamic_discharge_price)
    return payload


def payload_from_switch_mode_read(
    dev_id: str,
    read_state: dict[str, Any],
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a switchMode write payload by mirroring a getSwitchMode response.

    Used by the shim to construct revert-to-baseline writes from a captured
    snapshot, and to construct minimum-diff overrides where most fields
    track the live state.

    `overrides` is shallow-merged after the base payload is constructed —
    e.g. `{"workStatus": "2", "peakTimeList": [...]}` to apply a TOU change.
    """
    def slots(key: str) -> list[str]:
        return list(read_state.get(key, []) or [])

    def as_str_list(key: str) -> list[str]:
        return [str(v) for v in (read_state.get(key, []) or [])]

    payload = build_switch_mode_payload(
        dev_id=dev_id,
        work_status=str(read_state.get("workStatus", "1")),
        self_consumption_reserve_soc=int(read_state.get("selfConsumptioinReserveSoc") or 20),
        backup_reserve_soc=int(read_state.get("backupPowerReserveSoc") or 100),
        allow_grid_charge=str(read_state.get("allowChargingXiaGrid", "1")) == "1",
        peak_slots=slots("peakTimeList"),
        mid_peak_slots=slots("midPeakTimeList"),
        off_peak_slots=slots("offPeakTimeList"),
        peak_slots_weekend=slots("peakTimeListNonWorkDay"),
        mid_peak_slots_weekend=slots("midPeakTimeListNonWorkDay"),
        off_peak_slots_weekend=slots("offPeakTimeListNonWorkDay"),
        weekday_days=as_str_list("activeWeek"),
        weekend_days=as_str_list("activeWeekNonWorkDay"),
        dst_active=bool(read_state.get("dayLightSavingTime", False)),
        dst_peak_slots=slots("dayLightPeakTimeList"),
        dst_mid_peak_slots=slots("dayLightMidPeakTimeList"),
        dst_off_peak_slots=slots("dayLightOffPeakTimeList"),
        dst_peak_slots_weekend=slots("dayLightPeakTimeListNonWorkDay"),
        dst_mid_peak_slots_weekend=slots("dayLightMidPeakTimeListNonWorkDay"),
        dst_off_peak_slots_weekend=slots("dayLightOffPeakTimeListNonWorkDay"),
        dst_weekday_days=as_str_list("dayLightActiveWeek") or as_str_list("activeWeek"),
        dst_weekend_days=as_str_list("dayLightActiveWeekNonWorkDay") or as_str_list("activeWeekNonWorkDay"),
        tou_type=int(read_state.get("touType") or 0),
        dynamic_charge_price=_float_or_none(read_state.get("dynamicChargePrice")),
        dynamic_discharge_price=_float_or_none(read_state.get("dynamicDischargePrice")),
    )
    if overrides:
        payload.update(overrides)
    return payload


def _flag_or_none(value: Any) -> bool | None:
    """A cube "1"/"0" flag as bool, or None when the key is absent or blank."""
    if value is None or value == "":
        return None
    return _truthy(value)


def _int_or_none(value: Any) -> int | None:
    number = _float_or_none(value)
    return None if number is None else int(number)


def _float_or_none(value: Any) -> float | None:
    """Coerce a wire number/str to float; None for missing/blank/garbage."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------
def build_dynamic_binding(
    dev_id: str,
    *,
    brand_id: str,
    brand_name: str,
    country_id: str,
    region_code: str,
    import_code: str,
    export_code: str,
) -> dict[str, Any]:
    """The body of POST dynamicPrice/saveBinding (the Dynamic-tariff choice).

    Found in the Android app 3.2.0 (`ApiService.saveBinding(@Body)`); verified on a
    real cube 2026-10-15: `getRelation` follows within about a minute and the mode,
    thresholds and schedule are untouched. The `ratio` block is for countries that
    price from fixed factors instead of a product list; products-based regions
    (the UK) send zeros."""
    return {
        "devId": str(dev_id),
        "brandId": str(brand_id),
        "productBrand": brand_name,
        "supportCountryId": str(country_id),
        "productCode": import_code,
        "productCodeOut": export_code,
        "regionCode": region_code,
        "ratio": {"fraunhofer": {
            "standardImportRatio": 0, "importServiceCharge": 0,
            "standardExportRatio": 0, "exportServiceCharge": 0,
        }},
    }


class EPCubeClient:
    """Async client for the EP Cube cloud mobile-app surface."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        dev_id: str,
        sg_sn: str,
        bearer_token: str | None = None,
        capacity_kwh: float = 0.0,
        reauth_callback: ReauthCallback | None = None,
        api_prefix: str = "/api",
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        # Path prefix the cloud expects. EU/JP use "/api"; US uses "/app-api".
        # Stored without a trailing slash so the f-string call sites stay
        # symmetric: f"{prefix}/device/..." regardless of region.
        self._api_prefix = "/" + api_prefix.strip("/")
        self._dev_id = dev_id
        self._sg_sn = sg_sn
        self._bearer_token = bearer_token
        self._capacity_kwh = capacity_kwh
        # True once deviceList shows a Gen 1 system (no systemCapacity; capacity
        # counted from battery module serials). homeDeviceInfo's batteryPackNum
        # assumes 5.0 kWh packs, so it must not overwrite a Gen 1 capacity.
        self._gen1_capacity = False
        self._reauth_callback = reauth_callback
        # Battery flow delta-tracker state (Phase 3.5). Cube exposes
        # batteryCurrentElectricity (current stored energy, kWh) but not
        # signed charge/discharge flow. We delta-track between successive
        # polls — positive delta = charge, negative = discharge — into
        # per-day accumulators that reset on local midnight. Threshold
        # (0.05 kWh) filters API jitter.
        self._battery_flow_last_kwh: float | None = None
        self._battery_charge_today_kwh: float = 0.0
        self._battery_discharge_today_kwh: float = 0.0
        # Lifetime discharged energy (kWh): the same deltas, never reset. Saved
        # with the daily totals; `lifetime_known` says whether a saved value (or a
        # seed from the recorder) has been applied, so the coordinator knows if it
        # still has to seed it.
        self._battery_discharge_lifetime_kwh: float = 0.0
        self._lifetime_known: bool = False
        self._battery_flow_last_reset: date = _local_today()

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    @property
    def dev_id(self) -> str:
        return self._dev_id

    @property
    def sg_sn(self) -> str:
        return self._sg_sn

    @property
    def device_id(self) -> str:
        """Backwards-compat alias — services.py / sensor.py still reference this."""
        return self._dev_id

    @property
    def capacity_kwh(self) -> float:
        return self._capacity_kwh

    @property
    def bearer_token(self) -> str | None:
        return self._bearer_token

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    async def authenticate(self) -> None:
        """Probe the configured token by fetching deviceList. Also refreshes
        the cached capacity. Raises AuthError if the token is missing or
        rejected."""
        if self._bearer_token is None:
            raise AuthError(
                "no bearer_token configured — supply one from the EP Cube "
                "mobile-app login (or epcube-token.streamlit.app while the "
                "in-integration captcha login is not yet implemented)"
            )
        await self.refresh_capacity()

    async def refresh_capacity(self) -> bool:
        """Pull capacity from deviceList — also acts as auth probe.

        Called by the coordinator at startup and daily, so an entry whose
        cached capacity is stale (e.g. 0 kWh on a Gen 1 system set up before
        Gen 1 support) gets corrected without re-adding the integration.
        Returns True if deviceList was fetched, False if the call failed (the
        current capacity is kept)."""
        try:
            devices = await self.get_device_list()
        except EPCubeError as err:
            _LOGGER.warning("capacity probe failed (%s); keeping configured value %.1f kWh",
                            err, self._capacity_kwh)
            return False
        for entry in devices:
            row_dev_id = entry.get("devId") or entry.get("id")
            if str(row_dev_id) == self._dev_id or entry.get("sgSn") == self._sg_sn:
                capacity, gen1 = device_capacity_kwh(entry)
                if capacity > 0:
                    self._capacity_kwh = capacity
                    self._gen1_capacity = gen1
                    _LOGGER.debug(
                        "capacity from deviceList: %.1f kWh (%s)",
                        capacity,
                        f"Gen 1: {_battery_module_count(entry)} modules × "
                        f"{EP_CUBE_GEN1_PACK_KWH} kWh" if gen1 else "systemCapacity",
                    )
                else:
                    _LOGGER.warning(
                        "deviceList row for devId %s has neither systemCapacity nor "
                        "battery serials in snData; keeping %.1f kWh",
                        self._dev_id, self._capacity_kwh,
                    )
                return True
        _LOGGER.warning(
            "devId %s / sgSn %s not found in deviceList; keeping %.1f kWh",
            self._dev_id, self._sg_sn, self._capacity_kwh,
        )
        return True

    # ------------------------------------------------------------------
    # Battery flow tracker
    # ------------------------------------------------------------------
    # Noise threshold (kWh). Below this, treat as measurement jitter and
    # leave _battery_flow_last_kwh anchored so a slow real drift still
    # eventually crosses the threshold and gets counted.
    _BATTERY_FLOW_THRESHOLD_KWH = 0.05

    def _update_battery_flow(self, battery_now_kwh: float) -> None:
        """Delta-track batteryCurrentElectricity into daily charge/discharge
        accumulators. Reset at local midnight. First call after restart just
        anchors the reference value — no delta is counted (we don't know how
        long since the last poll).

        "Midnight" is Home Assistant's configured time zone, not the OS clock:
        HA containers often run with the system clock in UTC, which would
        roll these counters at 8pm Eastern instead of local midnight."""
        today = _local_today()
        if today != self._battery_flow_last_reset:
            self._battery_charge_today_kwh = 0.0
            self._battery_discharge_today_kwh = 0.0
            self._battery_flow_last_reset = today

        if self._battery_flow_last_kwh is None:
            self._battery_flow_last_kwh = battery_now_kwh
            return

        delta = battery_now_kwh - self._battery_flow_last_kwh
        if delta >= self._BATTERY_FLOW_THRESHOLD_KWH:
            self._battery_charge_today_kwh += delta
            self._battery_flow_last_kwh = battery_now_kwh
        elif delta <= -self._BATTERY_FLOW_THRESHOLD_KWH:
            self._battery_discharge_today_kwh += abs(delta)
            self._battery_discharge_lifetime_kwh += abs(delta)
            self._battery_flow_last_kwh = battery_now_kwh
        # else: jitter — leave anchor unchanged so a slow drift accumulates
        # toward the threshold across polls

    def battery_flow_state(self) -> dict[str, Any]:
        """The tracker's state as plain data, for saving across restarts."""
        return {
            "date": self._battery_flow_last_reset.isoformat(),
            "charged_kwh": round(self._battery_charge_today_kwh, 4),
            "discharged_kwh": round(self._battery_discharge_today_kwh, 4),
            "last_kwh": self._battery_flow_last_kwh,
            "lifetime_discharged_kwh": round(self._battery_discharge_lifetime_kwh, 4),
        }

    @property
    def battery_discharge_lifetime_kwh(self) -> float:
        return self._battery_discharge_lifetime_kwh

    @property
    def lifetime_known(self) -> bool:
        """True once the lifetime total came from a saved state or a seed."""
        return self._lifetime_known

    def seed_battery_discharge_lifetime(self, kwh: float) -> bool:
        """Start the lifetime total from `kwh` (the recorder's history) if it has no
        saved value yet. Anything below what has been counted since start-up is
        ignored, and so is a value that is not a finite, positive number."""
        if self._lifetime_known:
            return False
        try:
            value = float(kwh)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value <= 0:
            return False
        self._battery_discharge_lifetime_kwh = max(value, self._battery_discharge_lifetime_kwh)
        self._lifetime_known = True
        return True

    def restore_battery_flow(self, state: Any) -> bool:
        """Restore a state saved by `battery_flow_state`; True if it was used.

        Without this the daily totals start from zero after every Home Assistant
        restart. Only today's saved state is accepted (the totals reset at local
        midnight anyway), only before the tracker has seen a reading, and
        anything malformed is ignored. The saved anchor is restored too, so the
        first poll after a restart counts the net change in stored energy since
        the last saved reading, including anything that happened while Home
        Assistant was off."""
        if self._battery_flow_last_kwh is not None or not isinstance(state, dict):
            return False
        # The lifetime total is not tied to the day, so restore it even when the
        # daily part below is stale or unusable.
        try:
            lifetime = float(state["lifetime_discharged_kwh"])
        except (KeyError, TypeError, ValueError):
            lifetime = None
        if lifetime is not None and math.isfinite(lifetime) and lifetime >= 0 and not self._lifetime_known:
            self._battery_discharge_lifetime_kwh = lifetime
            self._lifetime_known = True
        try:
            saved_on = date.fromisoformat(str(state["date"]))
            charged = float(state["charged_kwh"])
            discharged = float(state["discharged_kwh"])
            last = state.get("last_kwh")
            last_kwh = None if last is None else float(last)
        except (KeyError, TypeError, ValueError):
            return False
        if saved_on != _local_today():
            return False
        if not (math.isfinite(charged) and math.isfinite(discharged)) or charged < 0 or discharged < 0:
            return False
        if last_kwh is not None and not (math.isfinite(last_kwh) and last_kwh > 0):
            return False
        self._battery_charge_today_kwh = charged
        self._battery_discharge_today_kwh = discharged
        self._battery_flow_last_kwh = last_kwh
        self._battery_flow_last_reset = saved_on
        return True

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get_status(self) -> DeviceStatus:
        """Merge a live `homeDeviceInfo` poll with the current `getSwitchMode`
        snapshot into a single DeviceStatus."""
        info, mode = await asyncio.gather(
            self._get(
                f"{self._api_prefix}/device/homeDeviceInfo?sgSn={self._sg_sn}&dayMonthYearFormat=YYYY-MM-DD"
            ),
            self._get(f"{self._api_prefix}/device/getSwitchMode?devId={self._dev_id}"),
        )

        solar_w = _power_to_w(info.get("solarPower"))
        backup_w = _power_to_w(info.get("backUpPower"))
        nonbackup_w = _power_to_w(info.get("nonBackUpPower"))
        # EV charging is reported on its own channel, NOT inside backUpPower /
        # nonBackUpPower. Verified on a US cube 2026-09-30: backUpPower:77 +
        # evPower:162 == smartHomePower:239 (and the same for the kWh
        # counters). Leaving it out under-reports load and makes the derived
        # battery power look ~EV-draw too low while a car charges. Installs
        # without a charger report evPower 0, so this is a no-op for them.
        ev_w = _power_to_w(info.get("evPower"))
        load_w = backup_w + nonbackup_w + ev_w
        grid_w = _power_to_w(info.get("gridPower"))
        # battery_power: power conservation. >0 = charging.
        battery_w = solar_w + grid_w - load_w

        # Diagnostic dump of every key in the response + the values we
        # computed from them. Enable via:
        #   logger:
        #     logs:
        #       custom_components.ep_cube.api: debug
        # in configuration.yaml. Cheap (one line per poll, gated at debug
        # level) but invaluable when load/solar/grid don't match the
        # app — the mobile endpoint shape was reverse-engineered piecemeal
        # and we have no saved HAR to cross-check against.
        _LOGGER.debug(
            "homeDeviceInfo raw=%s | computed: solar_w=%.0f grid_w=%.0f "
            "backup_w=%.0f nonbackup_w=%.0f ev_w=%.0f load_w=%.0f battery_w=%.0f",
            {k: v for k, v in info.items() if not isinstance(v, (dict, list))},
            solar_w, grid_w, backup_w, nonbackup_w, ev_w, load_w, battery_w,
        )
        # Companion dump of the getSwitchMode fields that DeviceStatus depends
        # on (reserves + mode + grid-charge flag). homeDeviceInfo doesn't carry
        # these, so without this line a reserve-side bug is invisible in logs.
        _LOGGER.debug(
            "getSwitchMode reserves: workStatus=%s self=%s backup=%s "
            "evCharger=%s allowChargingXiaGrid=%s dst=%s",
            mode.get("workStatus"),
            mode.get("selfConsumptioinReserveSoc"),
            mode.get("backupPowerReserveSoc"),
            mode.get("evChargerReserveSoc"),
            mode.get("allowChargingXiaGrid"),
            mode.get("dayLightSavingTime"),
        )

        pack_num = info.get("batteryPackNum")
        if isinstance(pack_num, int) and pack_num > 0 and not self._gen1_capacity:
            self._capacity_kwh = pack_num * EP_CUBE_PACK_KWH

        # Update the client-side battery-flow accumulators before constructing
        # the DeviceStatus so the snapshot contains the freshly-updated values.
        # A 0 / missing reading is treated as unavailable: skip the tracker
        # entirely so the anchor stays on the last good value and no phantom
        # discharge/charge pair gets booked (see _battery_kwh_or_none).
        battery_kwh_now = _battery_kwh_or_none(info.get("batteryCurrentElectricity"))
        if battery_kwh_now is not None:
            self._update_battery_flow(battery_kwh_now)
        else:
            _LOGGER.debug(
                "batteryCurrentElectricity=%r treated as unavailable",
                info.get("batteryCurrentElectricity"),
            )

        work_status = str(info.get("workStatus", mode.get("workStatus", "1")))
        operating_mode = WORK_STATUS_TO_OPERATING_MODE.get(work_status, "unknown")
        reserve = _reserve_for_mode(operating_mode, mode)
        self_reserve = float(mode.get("selfConsumptioinReserveSoc", 0) or 0)
        backup_reserve = float(mode.get("backupPowerReserveSoc", 0) or 0)
        allow_grid_charge = str(mode.get("allowChargingXiaGrid", "1")) == "1"
        dst_active = bool(mode.get("dayLightSavingTime", False))
        dynamic_mode = str(mode.get("touType", 0)) == "1"

        return DeviceStatus(
            soc_pct=float(info.get("batterySoc", 0)),
            soc_kwh=battery_kwh_now,
            capacity_kwh=self._capacity_kwh,
            battery_power_w=battery_w,
            grid_power_w=grid_w,
            solar_power_w=solar_w,
            load_power_w=load_w,
            operating_mode=operating_mode,
            reserve_soc_pct=reserve,
            allow_grid_charge=allow_grid_charge,
            self_consumption_reserve_pct=self_reserve,
            backup_reserve_pct=backup_reserve,
            dst_active=dst_active,
            solar_today_kwh=_kwh_str_to_float(info.get("solarElectricity")),
            backup_today_kwh=_kwh_str_to_float(info.get("backUpElectricity")),
            solar_dc_today_kwh=_kwh_str_to_float(info.get("solarDcElectricity")),
            solar_ac_today_kwh=_kwh_str_to_float(info.get("solarAcElectricity")),
            self_sufficiency_pct=float(info.get("selfHelpRate", 0) or 0),
            winter_protect_pct=float(info.get("winterProtect", 0) or 0),
            earning_yesterday=float(info.get("earningYesterday", 0) or 0),
            grid_outage_count=int(info.get("gridPowerFailureNum", 0) or 0),
            off_grid_seconds=int(info.get("offGridPowerSupplyTime", 0) or 0),
            battery_charge_today_kwh=self._battery_charge_today_kwh,
            battery_discharge_today_kwh=self._battery_discharge_today_kwh,
            battery_discharge_lifetime_kwh=self._battery_discharge_lifetime_kwh,
            # US cubes report `hasEv`; the EU payload seen so far carries `hasEvse`
            # (False here, no `hasEv` key), so either one counts.
            has_ev=_truthy(info.get("hasEv")) or _truthy(info.get("hasEvse")),
            ev_power_w=ev_w,
            ev_today_kwh=_kwh_str_to_float(info.get("evElectricity")),
            dynamic_buy_below=(
                _float_or_none(mode.get("dynamicChargePrice")) if dynamic_mode else None
            ),
            dynamic_sell_above=(
                _float_or_none(mode.get("dynamicDischargePrice")) if dynamic_mode else None
            ),
            is_online=_flag_or_none(info.get("isOnline")),
            is_fault=_flag_or_none(info.get("isFault")),
            is_alert=_flag_or_none(info.get("isAlert")),
            fault_warning_type=(
                None if info.get("faultWarningType") in (None, "") else str(info["faultWarningType"])
            ),
            signal_level=_int_or_none(info.get("signalLevel")),
            system_status=_int_or_none(info.get("systemStatus")),
            winter_mode=_int_or_none(info.get("winterMode")),
            charging_limit_soc=_float_or_none(mode.get("chargingLimitSoc")),
            lpp_time_duration=_float_or_none(info.get("lppTimeDuration")),
            lpc_time_duration=_float_or_none(info.get("lpcTimeDuration")),
            backup_loads_mode=_int_or_none(info.get("backupLoadsMode")),
            networking=_int_or_none(info.get("networking")),
        )

    async def get_switch_mode(self) -> dict[str, Any]:
        """Raw getSwitchMode — used by the shim for baseline snapshot."""
        return await self._get(f"{self._api_prefix}/device/getSwitchMode?devId={self._dev_id}")

    async def get_tou_schedule(self) -> dict[str, Any]:
        """Backwards-compat alias — getSwitchMode contains all schedule fields."""
        return await self.get_switch_mode()

    async def get_device_list(self) -> list[dict[str, Any]]:
        """Returns the array of devices on the account."""
        data = await self._get(f"{self._api_prefix}/device/deviceList")
        if isinstance(data, list):
            return data
        return []

    async def get_stats(self, scope_type: int, date_str: str) -> dict[str, Any]:
        """Fetch the cube's own electricity rollup for a window.

        scope_type per STATS_SCOPE_* constants in const.py. date_str format
        depends on scope: daily=YYYY-MM-DD, monthly=YYYY-MM, annual/total=YYYY.

        Keys in the returned dict are normalised to lowercase — cube's wire
        keys are camelCase but lowering them once at the boundary keeps
        every consumer's lookup code consistent.
        """
        path = (
            f"{self._api_prefix}/device/queryDataElectricityV2"
            f"?devId={self._dev_id}&queryDateStr={date_str}&scopeType={scope_type}"
        )
        data = await self._get(path)
        if not isinstance(data, dict):
            return {}
        return {k.lower(): v for k, v in data.items()}

    async def get_pv_strings(self) -> dict[int, dict[str, float]]:
        """Per-string PV telemetry from getSolarPvPower.

        The cloud returns a one-element list of string-typed fields
        (pv1Power in kW, pv1Voltage in V, pv1Current in A, for strings 1-4).
        Returns {string_number: {"power_w", "voltage_v", "current_a"}}; a
        string whose fields are missing or non-numeric is left out.
        """
        data = await self._get(f"{self._api_prefix}/device/getSolarPvPower?devId={self._dev_id}")
        row = data[0] if isinstance(data, list) and data else data
        if not isinstance(row, dict):
            return {}
        out: dict[int, dict[str, float]] = {}
        for n in range(1, 5):
            try:
                out[n] = {
                    "power_w": round(float(row[f"pv{n}Power"]) * 1000.0, 1),
                    "voltage_v": float(row[f"pv{n}Voltage"]),
                    "current_a": float(row[f"pv{n}Current"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def get_firmware_info(self) -> FirmwareInfo | None:
        """Firmware and comms-module versions plus the auto-update flag, from
        `user/user/queryFirmwareInfo` (`firmwareVersion`, `rtuVersion`,
        `aotuUpdateFirmware` - the cloud's own spelling). None if the route has
        none of them."""
        data = await self._get(f"{self._api_prefix}/user/user/queryFirmwareInfo?devId={self._dev_id}")
        if not isinstance(data, dict):
            return None

        def _text(key: str) -> str | None:
            value = data.get(key)
            return None if value in (None, "") else str(value)

        firmware, rtu = _text("firmwareVersion"), _text("rtuVersion")
        auto = _flag_or_none(data.get("aotuUpdateFirmware"))
        if firmware is None and rtu is None and auto is None:
            return None
        return FirmwareInfo(firmware_version=firmware, rtu_version=rtu, auto_update=auto)

    async def get_device_details(self) -> DeviceDetails | None:
        """Model type and serial numbers from userDeviceInfo (`modelType`, `sgSn`,
        `rtuSn`). The same response carries the owner's name and address; only
        these three fields are read. None if none of them is present."""
        data = await self._get(f"{self._api_prefix}/device/userDeviceInfo?devId={self._dev_id}")
        if not isinstance(data, dict):
            return None

        def _text(key: str) -> str | None:
            value = data.get(key)
            return None if value in (None, "") else str(value)

        model, serial, rtu = _text("modelType"), _text("sgSn"), _text("rtuSn")
        if model is None and serial is None and rtu is None:
            return None
        return DeviceDetails(model=model, serial_number=serial, rtu_serial_number=rtu)

    async def get_warranty(self) -> WarrantyInfo | None:
        """The warranty start and end dates from getWarranty (`activationDate` /
        `usageEndDate`, both YYYY-MM-DD). Returns None if the route has no usable
        dates, so callers can skip the warranty entities rather than guess."""
        data = await self._get(f"{self._api_prefix}/device/getWarranty?devId={self._dev_id}")
        if not isinstance(data, dict):
            return None

        def _day(key: str) -> date | None:
            try:
                return date.fromisoformat(str(data[key])[:10])
            except (KeyError, TypeError, ValueError):
                return None

        start, end = _day("activationDate"), _day("usageEndDate")
        if start is None and end is None:
            return None
        return WarrantyInfo(start=start, end=end)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def switch_mode(
        self,
        payload: dict[str, Any],
        *,
        verify_keys: tuple[str, ...] = ("workStatus",),
    ) -> dict[str, Any]:
        """POST /api/device/switchMode and verify the cube adopted it.

        `payload` must be a full switchMode body — use `build_switch_mode_payload`
        or `payload_from_switch_mode_read` to construct one. `verify_keys` is
        the subset of fields the post-write read-back checks; the default is
        just `workStatus` because that's the field a Predbat override actually
        cares about. Pass more keys (e.g. `("workStatus", "peakTimeList")`)
        when you want stronger verification.

        Returns the post-write read-back state. Raises WriteVerificationError
        if the cube's state does not match the request.
        """
        # Ensure devId is present.
        body = {**payload, "devId": str(self._dev_id)}
        await self._post(f"{self._api_prefix}/device/switchMode", json=body)
        readback = await self.get_switch_mode()
        _verify_write(body, readback, verify_keys)
        return readback

    async def get_dynamic_prices(self, date_str: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Half-hourly prices the cube uses for Dynamic-tariff mode.

        GET dynamicPrice/calcuPrice (found in the Android app's ApiService).
        `date_str` is a plain YYYY-MM-DD; without it the cloud answers for
        today. Prices are pence per kWh including VAT (`priceInc`); times are
        UK local, as naive "YYYY-MM-DD HH:MM:SS" strings. A day that is not
        published yet comes back with no price lists, which gives empty lists.

        Returns {"import": [...], "export": [...]}, each slot
        {"start": str, "end": str, "price": float}.
        """
        path = f"{self._api_prefix}/dynamicPrice/calcuPrice?devId={self._dev_id}"
        if date_str:
            path += f"&checkTime={date_str}"
        try:
            data = await self._get(path)
        except ServerError:
            raise   # a real outage, not "no Dynamic tariff": let the caller treat it as a failure
        except EPCubeError as err:
            if "envelope error status=500" in str(err):
                raise NoDynamicPricesError(str(err)) from err
            raise
        if not isinstance(data, dict):
            return {"import": [], "export": []}

        def slots(rows: Any) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for row in rows if isinstance(rows, list) else []:
                try:
                    out.append({
                        "start": str(row["startTime"]),
                        "end": str(row["endTime"]),
                        "price": float(row["priceInc"]),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            return out

        return {"import": slots(data.get("prices")), "export": slots(data.get("pricesExport"))}

    # ------------------------------------------------------------------
    # Dynamic-tariff choice (country / region / supplier / products)
    # ------------------------------------------------------------------
    async def get_dynamic_relation(self) -> dict[str, Any]:
        """The saved choice: country, region, supplier, import and export product."""
        data = await self._get(f"{self._api_prefix}/dynamicPrice/getRelation?devId={self._dev_id}")
        return data if isinstance(data, dict) else {}

    async def get_dynamic_countries(self) -> list[dict[str, Any]]:
        data = await self._get(f"{self._api_prefix}/dynamicPrice/getCountry?devId={self._dev_id}")
        return data if isinstance(data, list) else []

    async def get_dynamic_regions(self, country_id: str) -> list[dict[str, Any]]:
        data = await self._get(
            f"{self._api_prefix}/dynamicPrice/getRegionList"
            f"?devId={self._dev_id}&supportCountryId={country_id}"
        )
        return data if isinstance(data, list) else []

    async def get_dynamic_brands(self, country_id: str, region_code: str) -> list[dict[str, Any]]:
        data = await self._get(
            f"{self._api_prefix}/dynamicPrice/getBrands"
            f"?devId={self._dev_id}&supportCountryId={country_id}&regionCode={region_code}"
        )
        return data if isinstance(data, list) else []

    async def get_dynamic_products(
        self, brand_name: str, region_code: str, product_type: int
    ) -> list[dict[str, Any]]:
        """Tariffs on offer for a supplier and region; `product_type` 0 is import, 1 export."""
        data = await self._get(
            f"{self._api_prefix}/dynamicPrice/getProducts"
            f"?devId={self._dev_id}&brandName={brand_name}&region={region_code}"
            f"&productType={product_type}"
        )
        return data if isinstance(data, list) else []

    async def save_dynamic_binding(self, binding: dict[str, Any]) -> None:
        """Save the Dynamic-tariff choice (see build_dynamic_binding)."""
        await self._post(f"{self._api_prefix}/dynamicPrice/saveBinding", json=binding)

    async def probe(self, route: str, params: dict[str, str] | None = None) -> Any:
        """GET an arbitrary read-only route under the API prefix (diagnostics).

        The caller is responsible for restricting `route`; see the
        debug_probe service, which whitelists it.
        """
        query = urlencode(params or {})
        path = f"{self._api_prefix}/{route.strip('/')}"
        return await self._get(f"{path}?{query}" if query else path)

    async def probe_post(self, route: str, body: dict[str, Any]) -> Any:
        """POST `body` to an arbitrary route under the API prefix (diagnostics).

        Writes to the cloud. Only the debug_probe_write service uses it, with
        a whitelisted route and a body built from the cube's current state.
        """
        return await self._post(f"{self._api_prefix}/{route.strip('/')}", json=body)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, *, json: dict[str, Any] | None = None) -> Any:
        return await self._request("POST", path, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        _retry_after_reauth: bool = False,
    ) -> Any:
        if self._bearer_token is None:
            raise AuthError("no bearer token — call authenticate() first")

        url = f"{self._base_url}{path}"
        headers = {**_DEFAULT_HEADERS, "Authorization": f"Bearer {self._bearer_token}"}

        if json is not None:
            _LOGGER.debug("%s %s body: %s", method, path, json)

        async with self._session.request(
            method, url, headers=headers, json=json, allow_redirects=False,
        ) as resp:
            if resp.status in (401, 403):
                # Validation errors come back as 500 with the field named —
                # so a 403 here genuinely means the token is unhappy. Try a
                # single re-auth cycle if a callback is wired up.
                if not _retry_after_reauth and self._reauth_callback is not None:
                    new_token = await self._reauth_callback()
                    if new_token:
                        self._bearer_token = new_token
                        _LOGGER.info("%s %s 403 → re-auth succeeded, retrying", method, path)
                        return await self._request(
                            method, path, json=json, _retry_after_reauth=True,
                        )
                raise AuthError(f"{method} {path} returned {resp.status} (token rejected)")
            if resp.status == 429:
                raise RateLimitError(f"{method} {path} rate-limited")
            if resp.status >= 500:
                body_text = await resp.text()
                raise ServerError(f"{method} {path} returned {resp.status}: {body_text[:300]}")
            if resp.status >= 400:
                body_text = await resp.text()
                raise EPCubeError(f"{method} {path} returned {resp.status}: {body_text[:300]}")

            body = await resp.json()

        # Outer envelope: {timestamp, message, status, data}. Some responses
        # have no data field — that's fine. The cube also double-wraps some
        # errors (outer status:200, inner data.status:404) so check both.
        if not isinstance(body, dict):
            return body
        envelope_status = body.get("status", 200)
        if envelope_status != 200:
            msg = body.get("message", "unknown error")
            raise EPCubeError(f"{method} {path} envelope error status={envelope_status}: {msg}")
        data = body.get("data")
        # Double-wrapped errors: outer status:200 but inner data.status is an
        # HTTP-shaped code (e.g. 404 on missing-endpoint paths). We deliberately
        # only flag 4xx/5xx-shaped inner codes because `data.status` is also
        # used by some payloads (e.g. homeDeviceInfo) as a domain field
        # ("1" = online) — see PHASE_3_2.md.
        if isinstance(data, dict):
            inner_status = data.get("status")
            try:
                inner_code = int(inner_status) if inner_status is not None else 0
            except (TypeError, ValueError):
                inner_code = 0
            if inner_code >= 400:
                inner_msg = data.get("message") or data.get("msg") or "unknown inner error"
                raise EPCubeError(
                    f"{method} {path} inner envelope error status={inner_status}: {inner_msg}"
                )
        return data


# ----------------------------------------------------------------------
# Helpers used by get_status to extract the per-mode reserve
# ----------------------------------------------------------------------
def _reserve_for_mode(mode: str, switch_mode: dict[str, Any]) -> float:
    """Return the active mode's reserve SoC. Real cloud carries three
    per-mode reserves; we expose only the one matching current workStatus."""
    if mode == OPERATING_MODE_SELF_CONSUMPTION:
        return float(switch_mode.get("selfConsumptioinReserveSoc", 0) or 0)
    if mode == OPERATING_MODE_BACKUP:
        return float(switch_mode.get("backupPowerReserveSoc", 0) or 0)
    if mode == OPERATING_MODE_TOU:
        # TOU has no per-mode reserve — surface the self-consumption value
        # as a sane proxy (it kicks in outside any TOU window).
        return float(switch_mode.get("selfConsumptioinReserveSoc", 0) or 0)
    return 0.0


def _verify_write(
    request: dict[str, Any],
    readback: dict[str, Any],
    keys: tuple[str, ...],
) -> None:
    """Compare requested keys against the post-write readback. The cube
    normalises some fields on read (e.g. day arrays return as list[int]
    even though writes require list[str]) — we accept any
    string/int representation equivalence."""
    mismatches: list[str] = []
    for key in keys:
        want = request.get(key)
        got = readback.get(key)
        if not _values_equal(want, got):
            mismatches.append(f"{key}: requested={want!r} got={got!r}")
    if mismatches:
        raise WriteVerificationError(
            "switchMode write not reflected in read-back: " + "; ".join(mismatches)
        )


def _values_equal(a: Any, b: Any) -> bool:
    """Loose equality that tolerates the cube's read-side normalisation:
    str/int interchangeable for scalars, list[str]/list[int] interchangeable
    for arrays, slot strings byte-equal."""
    if a is None and b is None:
        return True
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if str(a) == str(b):
        return True
    # Numeric equivalence ("5" vs 5.0, "10.0" vs 10): the cube's price
    # thresholds come back as JSON floats whatever form we wrote them in.
    fa, fb = _float_or_none(a), _float_or_none(b)
    return fa is not None and fb is not None and fa == fb
