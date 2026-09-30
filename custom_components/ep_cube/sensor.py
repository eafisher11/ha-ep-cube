"""EP Cube sensor entities."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DeviceStatus
from .const import DOMAIN
from .coordinator import EPCubeCoordinator, EPCubeStatsCoordinator


@dataclass(frozen=True, kw_only=True)
class EPCubeSensorDescription(SensorEntityDescription):
    value_fn: Callable[[DeviceStatus], float | str | None]


@dataclass(frozen=True, kw_only=True)
class EPCubeStatsSensorDescription(SensorEntityDescription):
    """Description for sensors fed by EPCubeStatsCoordinator.

    `value_fn` takes the bucket-keyed dict from the stats coordinator
    (`{"today": {...}, "yesterday": {...}, ...}`) and returns the value.
    """

    value_fn: Callable[[dict[str, dict]], float | str | None]


# Instant-KPI helpers (v1.2). Operate over DeviceStatus power-W readings
# from the existing homeDeviceInfo poll — no new cloud calls. Below these
# noise floors the ratios are meaningless (overnight, empty house, or the
# cube's own sub-W jitter on the power channels), so return None ⇒ HA
# renders `unknown` rather than flapping the gauge between 0 and 100.
_INSTANT_MIN_POWER_W = 50.0
# Bands tighter than ±200 W on grid_flow surface a lot of in-house load
# transients to the gauge (kettle, microwave, fridge cycle) that aren't
# really "import vs export" — they're momentary mismatches the battery is
# about to absorb. Round to zero inside the band to keep the tile stable.
_INSTANT_GRID_FLOW_DEADBAND_W = 200.0


def _instant_self_consumption_pct(s: DeviceStatus) -> float | None:
    """(solar - export) / solar × 100, derived from current power readings.

    Export at the current moment = max(0, -grid_power_w) — cube reports grid
    as signed (>0 import, <0 export). Returns None below the sub-noise solar
    threshold so the gauge reads `unknown` overnight rather than ±inf.
    """
    if s.solar_power_w < _INSTANT_MIN_POWER_W:
        return None
    export_w = max(0.0, -s.grid_power_w)
    return max(0.0, min(100.0, (s.solar_power_w - export_w) / s.solar_power_w * 100))


def _instant_self_sufficiency_pct(s: DeviceStatus) -> float | None:
    """(load - import) / load × 100, derived from current power readings.

    Import = max(0, grid_power_w). Returns None when load is below the noise
    floor (empty house / off-grid) — the gauge reads `unknown` rather than
    surfacing a meaningless 100% from a 5W idle reading.
    """
    if s.load_power_w < _INSTANT_MIN_POWER_W:
        return None
    import_w = max(0.0, s.grid_power_w)
    return max(0.0, min(100.0, (s.load_power_w - import_w) / s.load_power_w * 100))


def _instant_grid_flow_w(s: DeviceStatus) -> float:
    """Signed grid flow with ±200 W dead-band rounded to 0 for display stability.

    The cube already exposes `grid_power` raw; this sensor is the user-facing
    "is the house importing or exporting right now?" tile. Dead-banding past
    the kettle / fridge / microwave transient range keeps it from oscillating
    between +/- values during normal in-house load shifts.
    """
    if abs(s.grid_power_w) < _INSTANT_GRID_FLOW_DEADBAND_W:
        return 0.0
    return s.grid_power_w


SENSORS: tuple[EPCubeSensorDescription, ...] = (
    EPCubeSensorDescription(
        key="battery_soc",
        translation_key="battery_soc",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.soc_pct,
    ),
    EPCubeSensorDescription(
        key="battery_soc_kwh",
        translation_key="battery_soc_kwh",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # Cube reports SoC as integer percent, so kWh resolution is
        # ~capacity/100 (≈0.2 kWh on a 20 kWh stack). 1dp is plenty;
        # default 2dp implies misleading 10 Wh precision.
        suggested_display_precision=1,
        value_fn=lambda s: s.soc_kwh,
    ),
    EPCubeSensorDescription(
        key="battery_capacity_kwh",
        translation_key="battery_capacity_kwh",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # Always batteryPackNum × 5.0 (integer pack count, fixed pack
        # capacity). 1dp matches battery_soc_kwh for dashboard consistency.
        suggested_display_precision=1,
        # Static device spec, not a live metric — keep it off the primary
        # dashboard surface.
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.capacity_kwh,
    ),
    EPCubeSensorDescription(
        key="battery_power",
        translation_key="battery_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda s: s.battery_power_w,
    ),
    EPCubeSensorDescription(
        key="grid_power",
        translation_key="grid_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda s: s.grid_power_w,
    ),
    EPCubeSensorDescription(
        key="solar_power",
        translation_key="solar_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda s: s.solar_power_w,
    ),
    EPCubeSensorDescription(
        key="load_power",
        translation_key="load_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda s: s.load_power_w,
    ),
    # Instant-KPI tiles (v1.2). Three sensors derived from the same poll the
    # raw power channels above feed, so updates land on the same 30s cadence
    # the user already sees in the power-flow card. Dashboard pairs each
    # with a type: gauge card so users get an at-a-glance "what's the house
    # doing right now?" view alongside the Energy Dashboard's daily totals.
    EPCubeSensorDescription(
        key="instant_self_consumption_pct",
        translation_key="instant_self_consumption_pct",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=_instant_self_consumption_pct,
    ),
    EPCubeSensorDescription(
        key="instant_self_sufficiency_pct",
        translation_key="instant_self_sufficiency_pct",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=_instant_self_sufficiency_pct,
    ),
    EPCubeSensorDescription(
        key="instant_grid_flow_w",
        translation_key="instant_grid_flow_w",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=_instant_grid_flow_w,
    ),
    EPCubeSensorDescription(
        key="operating_mode",
        translation_key="operating_mode",
        # Duplicated by select.ep_cube_operating_mode since session 16; kept
        # as DIAGNOSTIC for read-only templates / automations referencing the
        # mode string without going through the select.
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.operating_mode,
    ),
    EPCubeSensorDescription(
        key="reserve_soc",
        translation_key="reserve_soc",
        native_unit_of_measurement=PERCENTAGE,
        # Duplicated by number.ep_cube_{self_consumption,backup}_reserve since
        # session 16; this sensor reports whichever reserve the current
        # operating mode uses, so it's still useful as a read-only summary.
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.reserve_soc_pct,
    ),
    # ------------------------------------------------------------------
    # Daily kWh counters from homeDeviceInfo (Phase 3.4 (i)).
    # Cube's onboard accounting — matches the EP Cube mobile app's daily
    # totals. TOTAL_INCREASING handles the midnight reset. Layered into
    # monthly + yearly rollups via utility_meter in ha_config/packages/.
    # ------------------------------------------------------------------
    EPCubeSensorDescription(
        key="solar_today",
        translation_key="solar_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda s: s.solar_today_kwh,
    ),
    # `grid_today` deleted in v1.1.0: the homeDeviceInfo `gridElectricity`
    # field is direction-ambiguous (equals import on import-heavy days,
    # export on export-heavy days). Replaced by `grid_import_today` +
    # `grid_export_today` stats sensors sourced from queryDataElectricityV2,
    # which split the directions cleanly.
    # `nonbackup_today` deleted in v1.1.0: the cube reports 0 across all
    # rollups on EU firmware (verified 2026-06-04). Field was misleading.
    EPCubeSensorDescription(
        key="backup_today",
        translation_key="backup_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda s: s.backup_today_kwh,
    ),
    # The cube's `selfHelpRate` field — actually self-SUFFICIENCY (load met
    # from own generation), historically mislabeled in v0.5-v1.1 as "Self-
    # consumption" because the EP Cube app uses that name. Display name +
    # translation_key corrected in v1.2; `key` left at the legacy value so
    # the unique_id stays stable across the rename (existing installs keep
    # their Energy Dashboard wiring, automations, and history). Fresh
    # installs get a `sensor.ep_cube_self_sufficiency` entity_id slug; legacy
    # installs keep `sensor.ep_cube_self_consumption` as a sticky alias.
    EPCubeSensorDescription(
        key="self_consumption_pct",
        translation_key="self_sufficiency_pct",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=lambda s: s.self_sufficiency_pct,
    ),
    # Pre-/post-inverter daily kWh — exposes inverter losses over time.
    # DIAGNOSTIC because solar_today already covers the headline number;
    # most users don't need DC/AC split.
    EPCubeSensorDescription(
        key="solar_dc_today",
        translation_key="solar_dc_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.solar_dc_today_kwh,
    ),
    EPCubeSensorDescription(
        key="solar_ac_today",
        translation_key="solar_ac_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.solar_ac_today_kwh,
    ),
    # Read-only DIAGNOSTIC for now — winterProtect appears in homeDeviceInfo
    # but is NOT in getSwitchMode payload, so the write path is unconfirmed.
    # Revisit promoting to a writable number entity after a future capture
    # session reveals the write endpoint.
    EPCubeSensorDescription(
        key="winter_protect",
        translation_key="winter_protect",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.winter_protect_pct,
    ),
    # ------------------------------------------------------------------
    # Phase 3.5 metric additions. All read-only, all piggybacking the
    # existing homeDeviceInfo poll except battery_*_today which are
    # computed client-side (cube doesn't expose signed flow).
    # ------------------------------------------------------------------
    # Yesterday's revenue per cube's own accounting. Currency-typed (£ on EU
    # firmware via unitDefault). Most users on Predbat will get richer data
    # via BottlecapDave's Octopus integration anyway — this is for users
    # who configured tariff prices directly in the EP Cube app.
    EPCubeSensorDescription(
        key="earning_yesterday",
        translation_key="earning_yesterday",
        device_class=SensorDeviceClass.MONETARY,
        # GBP hard-coded for now — non-EU users (US/JP/Other) will need a
        # locale-aware unit. Deferred; cube returns the unit string in
        # `unitDefault` on the same payload, but currency-class sensors
        # don't tolerate runtime unit changes well in HA. Revisit when a
        # non-GBP user files an issue.
        native_unit_of_measurement="GBP",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda s: s.earning_yesterday,
    ),
    # Lifetime count of grid power failures the cube has observed. Useful
    # for "how often is my grid actually going down?" automations. Diagnostic
    # because it's a lifetime monotonic counter, not a live metric.
    EPCubeSensorDescription(
        key="grid_outage_count",
        translation_key="grid_outage_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.grid_outage_count,
    ),
    # Lifetime seconds the cube has spent supplying backup loads off-grid.
    # Duration-typed so HA can format as "X days Y hours" in the UI.
    EPCubeSensorDescription(
        key="off_grid_seconds",
        translation_key="off_grid_seconds",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.off_grid_seconds,
    ),
    # Client-side delta-tracker on batteryCurrentElectricity. Cube doesn't
    # expose signed battery flow — these are computed in EPCubeClient via
    # successive-poll deltas above a 0.05 kWh jitter threshold. Resets at
    # local midnight. Pair with utility_meter in ha_config/packages/ for
    # monthly/yearly rollups (see Phase 3.5 commit). Note: lifetime totals
    # are intentionally not surfaced — they would only persist across HA
    # restarts via RestoreSensor + state seeding, which we'd implement in
    # Phase 4.2 alongside the queryDataElectricityV2 capture work.
    EPCubeSensorDescription(
        key="battery_charge_today",
        translation_key="battery_charge_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda s: s.battery_charge_today_kwh,
    ),
    EPCubeSensorDescription(
        key="battery_discharge_today",
        translation_key="battery_discharge_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda s: s.battery_discharge_today_kwh,
    ),
)


# EV charger sensors. Registered only when homeDeviceInfo reports
# `hasEv: true` (see async_setup_entry), so installs without a charger don't
# carry two permanently-zero entities. Both read from the same 60 s poll.
EV_SENSORS: tuple[EPCubeSensorDescription, ...] = (
    EPCubeSensorDescription(
        key="ev_power",
        translation_key="ev_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda s: s.ev_power_w,
    ),
    # Cube-side daily total, reset at the cube's local midnight.
    # TOTAL_INCREASING treats that drop as a meter reset, so this can be
    # added to the Energy Dashboard as an individual device.
    EPCubeSensorDescription(
        key="ev_today",
        translation_key="ev_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda s: s.ev_today_kwh,
    ),
)


# ----------------------------------------------------------------------
# Stats sensors (Phase 4.2). Fed by EPCubeStatsCoordinator which polls the
# cube's queryDataElectricityV2 endpoint on a 5-min cadence for today + slower
# cadences for the wider rollups. Today's pair closes the v0.5.0 direction-
# ambiguity gap (`grid_today` was net-ish; now we get explicit import + export).
# Yesterday's quartet is genuinely new — useful for daily-summary automations.
# ----------------------------------------------------------------------
def _bucket_field(bucket: str, field: str) -> Callable[[dict[str, dict]], float | None]:
    """Returns a value_fn that pulls `field` from `bucket` of the stats dict.

    Both lowercase. Returns None if the bucket hasn't populated yet (e.g.
    early in startup before the first scope=1 fetch lands) so the sensor
    reads `unknown` rather than crashing.
    """
    def _get(data: dict[str, dict]) -> float | None:
        if not data:
            return None
        value = data.get(bucket, {}).get(field)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return _get


# Below the cube's own jitter threshold (battery_charge_today uses the same
# 0.05 kWh floor), the ratio becomes meaningless and tiny numerator noise
# blows up the percentage. Read `unknown` rather than show 0 / 100 / ±inf.
_MIN_DENOMINATOR_KWH = 0.05


def _grid_net(bucket: str) -> Callable[[dict[str, dict]], float | None]:
    """gridelectricityfrom - gridelectricityto from `bucket` — signed kWh.

    Positive = net importer for the period; negative = net exporter. Returns
    None for genuine missing-data states (no stats fetched yet / bucket keys
    absent / malformed values). Mirrors the Now-tab Grid gauge's sign
    convention so the Today-tab gauge can show export/import as one signed
    value rather than the two monotonic sensors the Energy Dashboard needs.
    """
    def _get(data: dict[str, dict]) -> float | None:
        if not data:
            return None
        b = data.get(bucket, {})
        imp = b.get("gridelectricityfrom")
        exp = b.get("gridelectricityto")
        if imp is None or exp is None:
            return None
        try:
            return float(imp) - float(exp)
        except (TypeError, ValueError):
            return None
    return _get


def _self_consumption_pct(bucket: str) -> Callable[[dict[str, dict]], float | None]:
    """(solar - export) / solar × 100 — share of generated kWh consumed onsite.

    Returns 0.0 when solar generation in `bucket` is below the jitter
    threshold so dashboards show a stable 0 % rather than `unknown` (HA's
    gauge card surfaces `unknown` as an "Entity is non-numeric" error
    overlay). Returns None only for genuine missing-data states (no stats
    fetched yet / bucket keys absent / malformed values).
    Clamps to [0, 100] — a cube that briefly reports export > solar (rare,
    seen during boot-up) shouldn't surface negative values to dashboards.
    """
    def _get(data: dict[str, dict]) -> float | None:
        if not data:
            return None
        b = data.get(bucket, {})
        solar = b.get("solarelectricity")
        export = b.get("gridelectricityto")
        if solar is None or export is None:
            return None
        try:
            solar = float(solar)
            export = float(export)
        except (TypeError, ValueError):
            return None
        if solar < _MIN_DENOMINATOR_KWH:
            return 0.0
        return max(0.0, min(100.0, (solar - export) / solar * 100))
    return _get


def _self_sufficiency_pct(bucket: str) -> Callable[[dict[str, dict]], float | None]:
    """(load - import) / load × 100 — share of consumed kWh met from own gen.

    Uses `backupelectricity` as the load proxy — accurate for installs where
    the cube's backup output feeds the whole-house breaker (the standard UK
    domestic wiring). Installs that wired only critical loads behind the
    backup terminal will see a fraction of true house load here; surface the
    sensor as a known-imperfect indicator rather than a billing figure.
    Returns 0.0 when load in `bucket` is below the jitter threshold so
    dashboards show a stable 0 % rather than `unknown` (HA's gauge card
    surfaces `unknown` as an "Entity is non-numeric" error overlay).
    Returns None only for genuine missing-data states (no stats fetched
    yet / bucket keys absent / malformed values).
    """
    def _get(data: dict[str, dict]) -> float | None:
        if not data:
            return None
        b = data.get(bucket, {})
        load = b.get("backupelectricity")
        imp = b.get("gridelectricityfrom")
        if load is None or imp is None:
            return None
        try:
            load = float(load)
            imp = float(imp)
        except (TypeError, ValueError):
            return None
        if load < _MIN_DENOMINATOR_KWH:
            return 0.0
        return max(0.0, min(100.0, (load - imp) / load * 100))
    return _get


STATS_SENSORS: tuple[EPCubeStatsSensorDescription, ...] = (
    EPCubeStatsSensorDescription(
        key="grid_import_today",
        translation_key="grid_import_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("today", "gridelectricityfrom"),
    ),
    EPCubeStatsSensorDescription(
        key="grid_export_today",
        translation_key="grid_export_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("today", "gridelectricityto"),
    ),
    # Signed grid net (import - export) for the day. state_class=TOTAL (not
    # MEASUREMENT — HA rejects MEASUREMENT with device_class=ENERGY; not
    # TOTAL_INCREASING because the value can decrease within a day as export
    # accumulates faster than import). Energy Dashboard wiring uses the
    # monotonic _import / _export pair above. Powers the Today-tab signed
    # Grid gauge (mirrors the Now-tab convention).
    EPCubeStatsSensorDescription(
        key="grid_net_today",
        translation_key="grid_net_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_grid_net("today"),
    ),
    # Yesterday's frozen snapshots. STATE_CLASS=TOTAL with no last_reset —
    # the value steps once per midnight roll, never accumulates within a day.
    # HA's statistics engine handles this correctly: the daily change shows up
    # as one delta on the day-roll, no spurious counter-reset detection.
    EPCubeStatsSensorDescription(
        key="grid_import_yesterday",
        translation_key="grid_import_yesterday",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("yesterday", "gridelectricityfrom"),
    ),
    EPCubeStatsSensorDescription(
        key="grid_export_yesterday",
        translation_key="grid_export_yesterday",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("yesterday", "gridelectricityto"),
    ),
    EPCubeStatsSensorDescription(
        key="grid_net_yesterday",
        translation_key="grid_net_yesterday",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_grid_net("yesterday"),
    ),
    EPCubeStatsSensorDescription(
        key="solar_yesterday",
        translation_key="solar_yesterday",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("yesterday", "solarelectricity"),
    ),
    EPCubeStatsSensorDescription(
        key="backup_yesterday",
        translation_key="backup_yesterday",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("yesterday", "backupelectricity"),
    ),
    # ------------------------------------------------------------------
    # Cube-native monthly + annual rollups (v1.2). Direct from
    # queryDataElectricityV2 scope=2 (month) and scope=3 (year). More
    # accurate than the utility_meter helpers we shipped in examples
    # pre-v1.2: they don't drift if HA is down at month/year roll, since
    # the cube does its own accounting and we just read the snapshot.
    # state_class=TOTAL (not TOTAL_INCREASING) because these values reset
    # at the month / year boundary — HA's statistics engine handles the
    # snap-back as a normal delta rather than a spurious counter reset.
    # ------------------------------------------------------------------
    EPCubeStatsSensorDescription(
        key="grid_import_month",
        translation_key="grid_import_month",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("month", "gridelectricityfrom"),
    ),
    EPCubeStatsSensorDescription(
        key="grid_export_month",
        translation_key="grid_export_month",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("month", "gridelectricityto"),
    ),
    EPCubeStatsSensorDescription(
        key="solar_month",
        translation_key="solar_month",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("month", "solarelectricity"),
    ),
    EPCubeStatsSensorDescription(
        key="backup_month",
        translation_key="backup_month",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("month", "backupelectricity"),
    ),
    EPCubeStatsSensorDescription(
        key="grid_import_year",
        translation_key="grid_import_year",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("year", "gridelectricityfrom"),
    ),
    EPCubeStatsSensorDescription(
        key="grid_export_year",
        translation_key="grid_export_year",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("year", "gridelectricityto"),
    ),
    EPCubeStatsSensorDescription(
        key="solar_year",
        translation_key="solar_year",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("year", "solarelectricity"),
    ),
    EPCubeStatsSensorDescription(
        key="backup_year",
        translation_key="backup_year",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_bucket_field("year", "backupelectricity"),
    ),
    # Derived percentages from the stats coordinator's today/yesterday
    # buckets (v1.2). All four pull from a single bucket dict so they share
    # the coordinator's existing update cadence — today's pair refreshes
    # every 5 min, yesterday's pair shifts once per midnight roll. Replaces
    # the cube's mislabeled `selfHelpRate` for the today + yesterday surface
    # with mathematically correct splits.
    EPCubeStatsSensorDescription(
        key="self_consumption_today",
        translation_key="self_consumption_today",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=_self_consumption_pct("today"),
    ),
    EPCubeStatsSensorDescription(
        key="self_consumption_yesterday",
        translation_key="self_consumption_yesterday",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=_self_consumption_pct("yesterday"),
    ),
    EPCubeStatsSensorDescription(
        key="self_sufficiency_today",
        translation_key="self_sufficiency_today",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=_self_sufficiency_pct("today"),
    ),
    EPCubeStatsSensorDescription(
        key="self_sufficiency_yesterday",
        translation_key="self_sufficiency_yesterday",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=_self_sufficiency_pct("yesterday"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator: EPCubeCoordinator = stored["coordinator"]
    stats_coordinator: EPCubeStatsCoordinator = stored["stats_coordinator"]
    entities: list[CoordinatorEntity] = []
    entities.extend(
        EPCubeSensor(coordinator, entry.entry_id, desc) for desc in SENSORS
    )
    # EV sensors only when the cube reports a charger. The first refresh has
    # already succeeded by the time platforms are set up, so data is present.
    if coordinator.data is not None and coordinator.data.has_ev:
        entities.extend(
            EPCubeSensor(coordinator, entry.entry_id, desc) for desc in EV_SENSORS
        )
    entities.extend(
        EPCubeStatsSensor(stats_coordinator, entry.entry_id, desc)
        for desc in STATS_SENSORS
    )
    async_add_entities(entities)


class EPCubeSensor(CoordinatorEntity[EPCubeCoordinator], RestoreSensor):
    """EP Cube sensor with persisted last value across HA restarts.

    RestoreSensor: HA's recorder briefly shows the last-known state at
    restart, but our sensor would otherwise override that with `None`
    until the first coordinator poll lands (~30-60s), causing a flicker
    where users see either "Unavailable" or a value that doesn't match
    the cube. Persisting native_value via async_get_last_sensor_data()
    keeps the displayed value stable across restarts until fresh data
    arrives.
    """

    _attr_has_entity_name = True
    entity_description: EPCubeSensorDescription

    def __init__(
        self,
        coordinator: EPCubeCoordinator,
        entry_id: str,
        description: EPCubeSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._restored_value: float | str | None = None
        # Device name is intentionally stable across devIds so downstream
        # consumers (Predbat apps.yaml, ha_config/packages/ep_cube.yaml) can
        # reference entity IDs like `sensor.ep_cube_battery_soc` without
        # needing to hard-code a per-account devId slug. Multi-account / dual
        # mock+cloud users disambiguate via the config-entry title
        # (`EP Cube ({dev_id})`) and HA's auto-appended `_2` suffix.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="EP Cube",
            manufacturer="Canadian Solar",
            model="EP Cube",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._restored_value = last.native_value

    @property
    def native_value(self) -> float | str | None:
        if self.coordinator.data is None:
            return self._restored_value
        return self.entity_description.value_fn(self.coordinator.data)


class EPCubeStatsSensor(CoordinatorEntity[EPCubeStatsCoordinator], RestoreSensor):
    """Sensor backed by the stats coordinator (queryDataElectricityV2).

    Mirrors EPCubeSensor's restore-on-restart semantics so wider rollups
    (yesterday/month/year/total) don't flicker to `unknown` for the 5-min
    window between HA restart and the first stats fetch.
    """

    _attr_has_entity_name = True
    entity_description: EPCubeStatsSensorDescription

    def __init__(
        self,
        coordinator: EPCubeStatsCoordinator,
        entry_id: str,
        description: EPCubeStatsSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._restored_value: float | str | None = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="EP Cube",
            manufacturer="Canadian Solar",
            model="EP Cube",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._restored_value = last.native_value

    @property
    def native_value(self) -> float | str | None:
        if self.coordinator.data is None:
            return self._restored_value
        return self.entity_description.value_fn(self.coordinator.data)
