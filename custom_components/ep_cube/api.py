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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import aiohttp

from .const import (
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
@dataclass(slots=True)
class DeviceStatus:
    soc_pct: float
    soc_kwh: float
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
) -> dict[str, Any]:
    """Build the full /api/device/switchMode POST body.

    Every field is required — minimal 3-field calls return
    `500 "The parameter cannot be null ：weatherWatch"` (see PHASE_3_2.md).
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

    return {
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
    )
    if overrides:
        payload.update(overrides)
    return payload


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------
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
        self._battery_flow_last_reset: date = date.today()

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
        await self._refresh_capacity()

    async def _refresh_capacity(self) -> None:
        """Pull capacity from deviceList — also acts as auth probe."""
        try:
            devices = await self.get_device_list()
        except EPCubeError as err:
            _LOGGER.warning("capacity probe failed (%s); keeping configured value %.1f kWh",
                            err, self._capacity_kwh)
            return
        for entry in devices:
            row_dev_id = entry.get("devId") or entry.get("id")
            if str(row_dev_id) == self._dev_id or entry.get("sgSn") == self._sg_sn:
                capacity = _capacity_string_to_kwh(entry.get("systemCapacity"))
                if capacity > 0:
                    self._capacity_kwh = capacity
                    _LOGGER.debug("capacity from deviceList: %.1f kWh", capacity)
                return

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
        long since the last poll)."""
        today = date.today()
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
            self._battery_flow_last_kwh = battery_now_kwh
        # else: jitter — leave anchor unchanged so a slow drift accumulates
        # toward the threshold across polls

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
        if isinstance(pack_num, int) and pack_num > 0:
            self._capacity_kwh = pack_num * EP_CUBE_PACK_KWH

        # Update the client-side battery-flow accumulators before constructing
        # the DeviceStatus so the snapshot contains the freshly-updated values.
        battery_kwh_now = _kwh_str_to_float(info.get("batteryCurrentElectricity"))
        self._update_battery_flow(battery_kwh_now)

        work_status = str(info.get("workStatus", mode.get("workStatus", "1")))
        operating_mode = WORK_STATUS_TO_OPERATING_MODE.get(work_status, "unknown")
        reserve = _reserve_for_mode(operating_mode, mode)
        self_reserve = float(mode.get("selfConsumptioinReserveSoc", 0) or 0)
        backup_reserve = float(mode.get("backupPowerReserveSoc", 0) or 0)
        allow_grid_charge = str(mode.get("allowChargingXiaGrid", "1")) == "1"
        dst_active = bool(mode.get("dayLightSavingTime", False))

        return DeviceStatus(
            soc_pct=float(info.get("batterySoc", 0)),
            soc_kwh=_kwh_str_to_float(info.get("batteryCurrentElectricity")),
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
            has_ev=_truthy(info.get("hasEv")),
            ev_power_w=ev_w,
            ev_today_kwh=_kwh_str_to_float(info.get("evElectricity")),
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
    return str(a) == str(b)
