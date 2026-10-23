"""EP Cube sensor entities."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorEntity,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import DeviceStatus
from .const import BATTERY_WARRANTY_CYCLES, DOMAIN, PV_NAME_KEYS
from .coordinator import (
    EPCubeCoordinator,
    EPCubePriceCoordinator,
    EPCubeStatsCoordinator,
)


@dataclass(frozen=True, kw_only=True)
class EPCubeSensorDescription(SensorEntityDescription):
    value_fn: Callable[[DeviceStatus], float | str | None]
    # Optional per-sensor availability check. When it returns False the
    # entity reports `unavailable` instead of a value, so HA's recorder and
    # long-term statistics skip that poll rather than storing a bogus number.
    available_fn: Callable[[DeviceStatus], bool] | None = None


@dataclass(frozen=True, kw_only=True)
class EPCubeStatsSensorDescription(SensorEntityDescription):
    """Description for sensors fed by EPCubeStatsCoordinator.

    `value_fn` takes the bucket-keyed dict from the stats coordinator
    (`{"today": {...}, "yesterday": {...}, ...}`) and returns the value.
    """

    value_fn: Callable[[dict[str, dict]], float | str | None]


@dataclass(frozen=True, kw_only=True)
class EPCubePVSensorDescription(SensorEntityDescription):
    """Per-string PV sensor; `value_fn` takes {string: {power_w, voltage_v, current_a}}."""

    value_fn: Callable[[dict[int, dict[str, float]]], float | None]


def _pv_sensors() -> tuple[EPCubePVSensorDescription, ...]:
    """PV1-PV4 power / voltage / current. Strings 3 and 4 are off by default
    (most systems have one or two); the cloud reports 0 for unused ones."""
    kinds = (
        ("power", "power_w", SensorDeviceClass.POWER, UnitOfPower.WATT, 0),
        ("voltage", "voltage_v", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, 0),
        ("current", "current_a", SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE, 1),
    )
    return tuple(
        EPCubePVSensorDescription(
            key=f"pv{n}_{kind}",
            translation_key=f"pv{n}_{kind}",
            device_class=dev_class,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=unit,
            suggested_display_precision=precision,
            entity_registry_enabled_default=n <= 2,
            value_fn=lambda pv, n=n, field=field: (pv.get(n) or {}).get(field),
        )
        for n in range(1, 5)
        for kind, field, dev_class, unit, precision in kinds
    )


PV_SENSORS: tuple[EPCubePVSensorDescription, ...] = _pv_sensors()

# The reserve the user chose, without the export target a shim Dynamic export sets
# on the cube. Point Predbat's `reserve:` at this, not at `reserve_soc`: Predbat
# treats the reserve as its battery floor and otherwise plans around the export
# target while an export is running.
USER_RESERVE_SENSOR = EPCubeSensorDescription(
    key="reserve_soc_user",
    translation_key="reserve_soc_user",
    native_unit_of_measurement=PERCENTAGE,
    value_fn=lambda s: s.reserve_soc_pct,
)


@dataclass(frozen=True, kw_only=True)
class EPCubePriceSensorDescription(SensorEntityDescription):
    """The cube's own price for the current half-hour; `side` is "import" or "export".

    This is the price feed the cube uses in Dynamic mode, not what the user pays:
    the cube can only offer a few tariffs (for the UK, Agile Octopus for import),
    so it differs from the real tariff. The user's real prices belong with
    Octopus and Predbat, so these are off by default."""

    side: str


PRICE_SENSORS: tuple[EPCubePriceSensorDescription, ...] = tuple(
    EPCubePriceSensorDescription(
        key=f"dynamic_{side}_price",
        translation_key=f"dynamic_{side}_price",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="p/kWh",
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        side=side,
    )
    for side in ("import", "export")
)


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
        # Cube sends 0 around its midnight rollover; show unavailable, not 0.
        available_fn=lambda s: s.soc_kwh is not None,
    ),
    EPCubeSensorDescription(
        key="battery_capacity_kwh",
        translation_key="battery_capacity_kwh",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # batteryPackNum × 5.0, or for Gen 1 systems (no systemCapacity)
        # battery module serials × 3.3. 1dp matches battery_soc_kwh.
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
    # The Dynamic tariff's thresholds, as set on the cube (by the app or by the
    # shim during a Dynamic export or charge). Unavailable outside Dynamic mode,
    # where the cloud keeps meaningless placeholders.
    EPCubeSensorDescription(
        key="dynamic_buy_below",
        translation_key="dynamic_buy_below",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="p/kWh",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.dynamic_buy_below,
        available_fn=lambda s: s.dynamic_buy_below is not None,
    ),
    EPCubeSensorDescription(
        key="dynamic_sell_above",
        translation_key="dynamic_sell_above",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="p/kWh",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.dynamic_sell_above,
        available_fn=lambda s: s.dynamic_sell_above is not None,
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
    # Lifetime discharged energy and the full-equivalent cycles it amounts to,
    # for tracking the battery warranty (80% capacity for 10 or 15 years or 6,000
    # cycles). Estimates built from the same SoC deltas as the daily counters;
    # saved across restarts and seeded once from the recorder's history.
    EPCubeSensorDescription(
        key="battery_discharge_lifetime",
        translation_key="battery_discharge_lifetime",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery_discharge_lifetime_kwh,
    ),
)

# Cycles (the sensor class below adds the warranty attributes) and the warranty end
# date. Both are diagnostics; the date is unavailable until the cloud returns it.
BATTERY_CYCLES_SENSOR = EPCubeSensorDescription(
    key="battery_cycles",
    translation_key="battery_cycles",
    native_unit_of_measurement="cycles",
    state_class=SensorStateClass.TOTAL_INCREASING,
    suggested_display_precision=1,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda s: (
        round(s.battery_discharge_lifetime_kwh / s.capacity_kwh, 3)
        if s.capacity_kwh and s.capacity_kwh > 0
        else None
    ),
)
# Status fields the cloud already returns, exposed as diagnostics. None sit on a
# default card: they are there for the device page and for automations. The last
# three are disabled by default because we don't yet know what they mean.
STATUS_SENSORS: tuple[EPCubeSensorDescription, ...] = (
    EPCubeSensorDescription(
        key="fault_warning_type",
        translation_key="fault_warning_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Hidden by default: we don't know what the codes mean. Users can unhide it
        # and put it on a card (or use it in an automation) if they want it.
        entity_registry_visible_default=False,
        value_fn=lambda s: s.fault_warning_type,
    ),
    EPCubeSensorDescription(
        key="signal_level",
        translation_key="signal_level",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.signal_level,
    ),
    EPCubeSensorDescription(
        key="system_status",
        translation_key="system_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.system_status,
    ),
    EPCubeSensorDescription(
        key="charging_limit_soc",
        translation_key="charging_limit_soc",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.charging_limit_soc,
    ),
    EPCubeSensorDescription(
        key="winter_mode",
        translation_key="winter_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.winter_mode,
    ),
    EPCubeSensorDescription(
        key="lpp_time_duration",
        translation_key="lpp_time_duration",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.lpp_time_duration,
    ),
    EPCubeSensorDescription(
        key="lpc_time_duration",
        translation_key="lpc_time_duration",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.lpc_time_duration,
    ),
    EPCubeSensorDescription(
        key="backup_loads_mode",
        translation_key="backup_loads_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.backup_loads_mode,
    ),
)

# Firmware versions come from their own cloud route, not the status poll.
# How the cube reaches the cloud, from homeDeviceInfo's `networking` (1 Wi-Fi, 3 Ethernet).
# Any other code reads "unknown", with the code as an attribute.
CONNECTION_TYPE_OPTIONS = ["wifi", "ethernet", "unknown"]
_CONNECTION_CODES = {1: "wifi", 3: "ethernet"}
CONNECTION_TYPE_SENSOR = EPCubeSensorDescription(
    key="connection_type",
    translation_key="connection_type",
    device_class=SensorDeviceClass.ENUM,
    options=CONNECTION_TYPE_OPTIONS,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda s: (
        None if s.networking is None else _CONNECTION_CODES.get(s.networking, "unknown")
    ),
)
MODEL_SENSOR = EPCubeSensorDescription(
    key="model",
    translation_key="model",
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda s: None,
)
SERIAL_NUMBER_SENSOR = EPCubeSensorDescription(
    key="serial_number",
    translation_key="serial_number",
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda s: None,
)
RTU_SERIAL_NUMBER_SENSOR = EPCubeSensorDescription(
    key="rtu_serial_number",
    translation_key="rtu_serial_number",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    value_fn=lambda s: None,
)
FIRMWARE_VERSION_SENSOR = EPCubeSensorDescription(
    key="firmware_version",
    translation_key="firmware_version",
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda s: None,
)
RTU_VERSION_SENSOR = EPCubeSensorDescription(
    key="rtu_version",
    translation_key="rtu_version",
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda s: None,
)
WARRANTY_END_SENSOR = EPCubeSensorDescription(
    key="warranty_end",
    translation_key="warranty_end",
    device_class=SensorDeviceClass.DATE,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda s: None,
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
    # The app's eco figures. Both are fixed multiples of the home's consumption
    # (coal = 0.4 per kWh, trees = 0.055 per kWh, checked against a live
    # payload), so they are the app's estimates rather than measurements. The
    # unit of coal is not stated by the cloud; kg is the standard coal-equivalent
    # figure. There is no device class, so Home Assistant never converts them.
    EPCubeStatsSensorDescription(
        key="coal_today",
        translation_key="coal_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="kg",
        suggested_display_precision=1,
        icon="mdi:molecule-co2",
        value_fn=_bucket_field("today", "coal"),
    ),
    EPCubeStatsSensorDescription(
        key="coal_year",
        translation_key="coal_year",
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="kg",
        suggested_display_precision=1,
        icon="mdi:molecule-co2",
        value_fn=_bucket_field("year", "coal"),
    ),
    EPCubeStatsSensorDescription(
        key="trees_today",
        translation_key="trees_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:tree",
        value_fn=_bucket_field("today", "treenum"),
    ),
    EPCubeStatsSensorDescription(
        key="trees_year",
        translation_key="trees_year",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:tree",
        value_fn=_bucket_field("year", "treenum"),
    ),
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
    entities.extend(
        EPCubePVSensor(
            coordinator, entry.entry_id, desc,
            str(entry.options.get(PV_NAME_KEYS[int(desc.key[2]) - 1]) or "").strip(),
        )
        for desc in PV_SENSORS
    )
    price_coordinator: EPCubePriceCoordinator = stored["price_coordinator"]
    entities.extend(
        EPCubePriceSensor(price_coordinator, entry.entry_id, desc) for desc in PRICE_SENSORS
    )
    entities.append(
        EPCubeUserReserveSensor(coordinator, entry.entry_id, USER_RESERVE_SENSOR, stored["shim"])
    )
    entities.extend(EPCubeSensor(coordinator, entry.entry_id, desc) for desc in STATUS_SENSORS)
    entities.append(
        EPCubeFirmwareSensor(coordinator, entry.entry_id, FIRMWARE_VERSION_SENSOR, "firmware_version")
    )
    entities.append(EPCubeFirmwareSensor(coordinator, entry.entry_id, RTU_VERSION_SENSOR, "rtu_version"))
    entities.append(EPCubeConnectionSensor(coordinator, entry.entry_id, CONNECTION_TYPE_SENSOR))
    entities.append(EPCubeDetailsSensor(coordinator, entry.entry_id, MODEL_SENSOR, "model"))
    entities.append(
        EPCubeDetailsSensor(coordinator, entry.entry_id, SERIAL_NUMBER_SENSOR, "serial_number")
    )
    entities.append(
        EPCubeDetailsSensor(coordinator, entry.entry_id, RTU_SERIAL_NUMBER_SENSOR, "rtu_serial_number")
    )
    entities.append(EPCubeCyclesSensor(coordinator, entry.entry_id, BATTERY_CYCLES_SENSOR))
    entities.append(EPCubeWarrantySensor(coordinator, entry.entry_id, WARRANTY_END_SENSOR))
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
    def available(self) -> bool:
        if not super().available:
            return False
        # The PV-string and user-reserve subclasses use their own description types.
        check = getattr(self.entity_description, "available_fn", None)
        if check is None or self.coordinator.data is None:
            return True
        return check(self.coordinator.data)

    @property
    def native_value(self) -> float | str | None:
        if self.coordinator.data is None:
            return self._restored_value
        return self.entity_description.value_fn(self.coordinator.data)


class EPCubeUserReserveSensor(EPCubeSensor):
    """The reserve the user chose: the live reserve, except while a shim override is
    active, when it is the reserve captured in the baseline (see PredbatShim.user_reserve_pct)."""

    def __init__(self, coordinator, entry_id, description, shim) -> None:
        super().__init__(coordinator, entry_id, description)
        self._shim = shim

    @property
    def native_value(self) -> float | str | None:
        chosen = self._shim.user_reserve_pct()
        if chosen is not None:
            return chosen
        return super().native_value


class EPCubeCyclesSensor(EPCubeSensor):
    """Estimated full-equivalent battery cycles, with the warranty allowance and
    pace as attributes (the warranty dates come from the cloud, so the same code
    covers a 10-year and a 15-year warranty)."""

    @property
    def extra_state_attributes(self) -> dict[str, float | str]:
        attrs: dict[str, float | str] = {"warranty_cycle_limit": BATTERY_WARRANTY_CYCLES}
        cycles = self.native_value
        if isinstance(cycles, (int, float)):
            attrs["percent_of_cycle_allowance"] = round(cycles / BATTERY_WARRANTY_CYCLES * 100, 2)
        warranty = self.coordinator.warranty
        if warranty is not None and warranty.end is not None:
            attrs["warranty_end"] = warranty.end.isoformat()
            if warranty.start is not None:
                days = (warranty.end - warranty.start).days
                if days > 0:
                    attrs["cycle_budget_per_day"] = round(BATTERY_WARRANTY_CYCLES / days, 2)
                elapsed = (dt_util.now().date() - warranty.start).days
                if elapsed > 0 and isinstance(cycles, (int, float)):
                    attrs["average_cycles_per_day"] = round(cycles / elapsed, 2)
        return attrs


class EPCubeFirmwareSensor(EPCubeSensor):
    """A version string from the cloud's queryFirmwareInfo (firmware or comms module)."""

    def __init__(self, coordinator, entry_id, description, field: str) -> None:
        super().__init__(coordinator, entry_id, description)
        self._field = field

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.firmware is not None

    @property
    def native_value(self) -> str | None:
        firmware = self.coordinator.firmware
        return getattr(firmware, self._field, None) if firmware is not None else None


class EPCubeConnectionSensor(EPCubeSensor):
    """Wi-Fi / Ethernet, with the raw `networking` code as an attribute."""

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        data = self.coordinator.data
        code = getattr(data, "networking", None) if data is not None else None
        return {"networking_code": code} if code is not None else {}


class EPCubeDetailsSensor(EPCubeSensor):
    """Model or serial number from the cloud's userDeviceInfo."""

    def __init__(self, coordinator, entry_id, description, field: str) -> None:
        super().__init__(coordinator, entry_id, description)
        self._field = field

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.details is not None

    @property
    def native_value(self) -> str | None:
        details = self.coordinator.details
        return getattr(details, self._field, None) if details is not None else None


class EPCubeWarrantySensor(EPCubeSensor):
    """The warranty end date from the cloud's getWarranty route."""

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.warranty is not None

    @property
    def native_value(self) -> date | None:
        warranty = self.coordinator.warranty
        return warranty.end if warranty is not None else None


class EPCubePVSensor(EPCubeSensor):
    """Per-string PV reading, fed by the main coordinator's getSolarPvPower cache."""

    entity_description: EPCubePVSensorDescription  # type: ignore[assignment]

    def __init__(self, coordinator, entry_id, description, custom_name: str = "") -> None:
        super().__init__(coordinator, entry_id, description)
        # The sensor names read "{pv_name} power" etc.; the placeholder is the name the
        # user gave the string in Configure, or "PV1"-"PV4" when none was given.
        string = int(description.key[2])
        self._attr_translation_placeholders = {"pv_name": custom_name or f"PV{string}"}

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.pv_strings:
            return self._restored_value
        return self.entity_description.value_fn(self.coordinator.pv_strings)


class EPCubePriceSensor(CoordinatorEntity[EPCubePriceCoordinator], SensorEntity):
    """The price of the half-hour slot we are in, on the cube's own tariff.

    The coordinator polls every 15 minutes; this entity also re-evaluates at
    every :00 and :30 so the value changes with the slot. Attributes carry the
    whole schedule for charts. Not a RestoreSensor: a stale price is worse
    than an unknown one.
    """

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"slots"})
    entity_description: EPCubePriceSensorDescription

    def __init__(
        self,
        coordinator: EPCubePriceCoordinator,
        entry_id: str,
        description: EPCubePriceSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="EP Cube",
            manufacturer="Canadian Solar",
            model="EP Cube",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_change(self.hass, self._slot_changed, minute=(0, 30), second=3)
        )

    @callback
    def _slot_changed(self, _now: datetime) -> None:
        self.async_write_ha_state()

    def _slots(self) -> list[dict[str, Any]]:
        data = self.coordinator.data or {}
        return data.get(self.entity_description.side, [])

    def _current(self) -> dict[str, Any] | None:
        now = dt_util.utcnow()
        for slot in self._slots():
            if slot["start"] <= now < slot["end"]:
                return slot
        return None

    @property
    def native_value(self) -> float | None:
        current = self._current()
        return current["price"] if current else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slots = self._slots()
        now = dt_util.utcnow()
        upcoming = next((s for s in slots if s["start"] > now), None)
        return {
            "next_start": dt_util.as_local(upcoming["start"]).isoformat() if upcoming else None,
            "next_price": upcoming["price"] if upcoming else None,
            "slots": [
                {"start": dt_util.as_local(s["start"]).isoformat(),
                 "end": dt_util.as_local(s["end"]).isoformat(),
                 "price": s["price"]}
                for s in slots
            ],
        }


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
