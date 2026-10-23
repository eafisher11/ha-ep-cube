"""Pure-helper tests for api.py.

These cover the wire-coercion + comparator helpers that are exercised on
every poll. They're framework-free so they run in <100ms even from a cold
pytest start.
"""
from __future__ import annotations

import pytest

from custom_components.ep_cube.api import (
    _battery_kwh_or_none,
    _battery_module_count,
    _capacity_string_to_kwh,
    _kwh_str_to_float,
    _power_to_w,
    _reserve_for_mode,
    _values_equal,
    _verify_write,
    WriteVerificationError,
    device_capacity_kwh,
)
from custom_components.ep_cube.const import (
    OPERATING_MODE_BACKUP,
    OPERATING_MODE_SELF_CONSUMPTION,
    OPERATING_MODE_TOU,
)


class TestPowerToW:
    """`_power_to_w` × 10 — the cube's centi-kW quirk."""

    @pytest.mark.parametrize(
        ("wire", "expected_w"),
        [
            (64, 640.0),         # 0.64 kW (canonical mobile API shape)
            (120, 1200.0),       # 1.20 kW
            (0, 0.0),
            (-17, -170.0),       # negative = export, confirmed 2026-05-22
            ("64", 640.0),       # string tolerance — older web-portal shape
        ],
    )
    def test_known_values(self, wire, expected_w):
        assert _power_to_w(wire) == expected_w

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_is_zero(self, value):
        # Treat absent fields as 0.0 W — never propagate None into arithmetic.
        assert _power_to_w(value) == 0.0


class TestKwhStrToFloat:
    @pytest.mark.parametrize(
        ("wire", "expected"),
        [(0.35, 0.35), ("0.35", 0.35), (11.0, 11.0), (0, 0.0)],
    )
    def test_known_values(self, wire, expected):
        assert _kwh_str_to_float(wire) == expected

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_is_zero(self, value):
        assert _kwh_str_to_float(value) == 0.0


class TestBatteryKwhOrNone:
    @pytest.mark.parametrize(
        ("wire", "expected"),
        [(11.0, 11.0), ("18.98", 18.98), (0.4, 0.4)],
    )
    def test_real_readings_pass_through(self, wire, expected):
        assert _battery_kwh_or_none(wire) == expected

    @pytest.mark.parametrize("value", [None, "", 0, 0.0, "0", "0.0", -1, "n/a"])
    def test_zero_missing_or_garbage_is_none(self, value):
        # The cloud sends 0 / drops the field around midnight rollover —
        # that must read as "no reading", never 0 kWh.
        assert _battery_kwh_or_none(value) is None


class TestCapacityStringToKwh:
    """`systemCapacity` arrives as `"20.0kWh"` from deviceList."""

    @pytest.mark.parametrize(
        ("wire", "expected"),
        [
            ("20.0kWh", 20.0),
            ("5.0kWh", 5.0),
            ("20.0", 20.0),     # tolerate missing unit
            ("20", 20.0),
            (None, 0.0),
            ("garbage", 0.0),   # don't raise on bad data
            ("", 0.0),
        ],
    )
    def test_parses(self, wire, expected):
        assert _capacity_string_to_kwh(wire) == expected


class TestDeviceCapacityKwh:
    """Gen 1 rows omit `systemCapacity`; capacity = battery serials × 3.3."""

    def test_gen1_counts_battery_serials(self, device_list_gen1):
        row = device_list_gen1[0]
        assert _battery_module_count(row) == 12
        assert device_capacity_kwh(row) == (39.6, True)

    def test_system_capacity_wins_when_present(self, device_list):
        assert device_capacity_kwh(device_list[0]) == (20.0, False)

    def test_system_capacity_wins_over_sn_data(self, device_list_gen1):
        row = {**device_list_gen1[0], "systemCapacity": "30.0kWh"}
        assert device_capacity_kwh(row) == (30.0, False)

    def test_sn_items_inverters_and_bases_not_counted(self):
        # Only batterySnList entries are modules; snItems also holds the
        # inverter and base serials.
        row = {"snItems": "a,b,c,d", "snData": [{"inverterSn": "a", "baseSn": "b",
                                               "batterySnList": ["c", "d"]}]}
        assert device_capacity_kwh(row) == (6.6, True)

    def test_duplicate_serials_counted_once(self):
        row = {"snData": [{"batterySnList": ["x", "y"]}, {"batterySnList": ["y", " "]}]}
        assert _battery_module_count(row) == 2

    def test_comma_string_battery_list(self):
        row = {"snData": [{"batterySnList": "x,y,z"}]}
        assert device_capacity_kwh(row) == (9.9, True)

    @pytest.mark.parametrize(
        "row",
        [{}, {"snData": None}, {"snData": "junk"}, {"snData": [None, {}]},
         {"snData": [{"batterySnList": []}]}, {"systemCapacity": "", "snData": []}],
    )
    def test_nothing_usable_is_zero(self, row):
        assert device_capacity_kwh(row) == (0.0, False)


class TestValuesEqual:
    """The cube normalises some fields on read (str↔int, list[str]↔list[int])."""

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("1", 1),                  # str/int interchangeable
            (1, "1"),
            (["1", "2"], [1, 2]),      # list normalisation
            ([1, 2, 3], ["1", "2", "3"]),
            ("00:30_04:30_0.05", "00:30_04:30_0.05"),  # slot byte-equal
            (None, None),
            (True, True),
            (False, False),
        ],
    )
    def test_equal(self, a, b):
        assert _values_equal(a, b) is True

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("1", "2"),
            (["1"], ["2"]),
            (["1", "2"], ["1", "2", "3"]),  # length mismatch
            (None, "1"),
            (True, False),                  # bool path: explicit mismatch
        ],
    )
    def test_not_equal(self, a, b):
        assert _values_equal(a, b) is False


class TestVerifyWrite:
    def test_match_passes(self):
        # No-op when every verify_key matches across str/int boundary.
        _verify_write(
            request={"workStatus": "2", "peakTimeList": ["16:00_19:00_1.00"]},
            readback={"workStatus": 2, "peakTimeList": ["16:00_19:00_1.00"]},
            keys=("workStatus", "peakTimeList"),
        )

    def test_mismatch_raises(self):
        with pytest.raises(WriteVerificationError) as exc:
            _verify_write(
                request={"workStatus": "2"},
                readback={"workStatus": "1"},
                keys=("workStatus",),
            )
        assert "workStatus" in str(exc.value)
        # Error message includes requested + got for diagnosis.
        assert "requested='2'" in str(exc.value)
        assert "got='1'" in str(exc.value)

    def test_only_checks_requested_keys(self):
        # Extra fields on either side don't trigger a mismatch.
        _verify_write(
            request={"workStatus": "2", "extra": "ignored"},
            readback={"workStatus": "2", "selfConsumptioinReserveSoc": "30"},
            keys=("workStatus",),
        )


class TestReserveForMode:
    """Per-mode reserve selection from a getSwitchMode payload."""

    SAMPLE = {
        "selfConsumptioinReserveSoc": "20",
        "backupPowerReserveSoc": "100",
    }

    def test_self_consumption(self):
        assert _reserve_for_mode(OPERATING_MODE_SELF_CONSUMPTION, self.SAMPLE) == 20.0

    def test_backup(self):
        assert _reserve_for_mode(OPERATING_MODE_BACKUP, self.SAMPLE) == 100.0

    def test_tou_uses_self_consumption(self):
        # TOU has no per-mode reserve in the cube's model; we surface the
        # self-consumption value as a sane proxy.
        assert _reserve_for_mode(OPERATING_MODE_TOU, self.SAMPLE) == 20.0

    def test_unknown_mode_zero(self):
        assert _reserve_for_mode("garbage", self.SAMPLE) == 0.0

    def test_missing_field_zero(self):
        assert _reserve_for_mode(OPERATING_MODE_BACKUP, {}) == 0.0
