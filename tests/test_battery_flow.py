"""Tests for EPCubeClient._update_battery_flow.

Phase 3.5's delta-tracker: the cube doesn't expose signed battery flow on
homeDeviceInfo, so we delta-track `batteryCurrentElectricity` between polls
into per-day charge/discharge accumulators. Threshold (0.05 kWh) filters
API jitter.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock

import pytest

from custom_components.ep_cube import api as api_mod
from custom_components.ep_cube.api import EPCubeClient


@pytest.fixture
def client() -> EPCubeClient:
    return EPCubeClient(
        session=MagicMock(),
        base_url="https://mock.invalid",
        dev_id="5613",
        sg_sn="x",
        bearer_token="t",
    )


class TestBatteryFlowTracker:
    def test_first_call_anchors_no_delta(self, client):
        # First poll after restart — anchor the reference, don't credit any
        # flow (we don't know how long since the last poll).
        client._update_battery_flow(11.0)
        assert client._battery_charge_today_kwh == 0.0
        assert client._battery_discharge_today_kwh == 0.0
        assert client._battery_flow_last_kwh == 11.0

    def test_charge_above_threshold_accumulates(self, client):
        client._update_battery_flow(11.0)
        client._update_battery_flow(11.2)        # +0.2 kWh charge
        assert client._battery_charge_today_kwh == pytest.approx(0.2)
        assert client._battery_discharge_today_kwh == 0.0

    def test_discharge_above_threshold_accumulates_abs(self, client):
        client._update_battery_flow(11.0)
        client._update_battery_flow(10.7)        # -0.3 kWh discharge
        assert client._battery_charge_today_kwh == 0.0
        assert client._battery_discharge_today_kwh == pytest.approx(0.3)

    def test_jitter_below_threshold_preserves_anchor(self, client):
        # Critical: small noise below the 0.05 kWh threshold must leave the
        # anchor alone so a slow real drift eventually crosses the threshold.
        # Otherwise a 0.1 kWh trickle that arrives as 0.01 kWh/poll would
        # never get credited.
        client._update_battery_flow(11.0)
        for tick in (11.01, 11.02, 11.03, 11.04):
            client._update_battery_flow(tick)
        assert client._battery_charge_today_kwh == 0.0
        # Anchor still at the original 11.0 — accumulated drift is now 0.04
        # which is just under threshold; one more 0.02 tick should trigger.
        assert client._battery_flow_last_kwh == 11.0
        client._update_battery_flow(11.06)       # +0.06 from anchor → counts
        assert client._battery_charge_today_kwh == pytest.approx(0.06)

    def test_alternating_charge_and_discharge(self, client):
        client._update_battery_flow(11.0)
        client._update_battery_flow(11.5)        # +0.5 charge
        client._update_battery_flow(11.2)        # -0.3 discharge
        client._update_battery_flow(11.8)        # +0.6 charge
        assert client._battery_charge_today_kwh == pytest.approx(1.1)
        assert client._battery_discharge_today_kwh == pytest.approx(0.3)

    def test_midnight_reset_zeroes_counters(self, client, monkeypatch):
        # Simulate the day rolling over between polls — accumulators reset
        # but the anchor carries forward so the new day's first delta is
        # still measured from the prior poll's value.
        client._update_battery_flow(11.0)
        client._update_battery_flow(11.5)
        assert client._battery_charge_today_kwh == pytest.approx(0.5)

        yesterday = client._battery_flow_last_reset - timedelta(days=1)
        client._battery_flow_last_reset = yesterday    # force "yesterday" anchor

        client._update_battery_flow(11.7)
        # Reset happened first — _battery_charge_today_kwh starts the new day at 0,
        # the delta from the anchored value (11.5 → 11.7 = +0.2) counts toward
        # the new day. Anchor was preserved across midnight.
        assert client._battery_charge_today_kwh == pytest.approx(0.2)
        assert client._battery_flow_last_reset == api_mod._local_today()

    def test_reset_follows_ha_timezone_not_os_clock(self, client, monkeypatch):
        # 21:30 Eastern on Oct 5 is already Oct 6 in UTC. An OS clock in UTC
        # would roll the counters here; HA's local date must not.
        ny = ZoneInfo("America/New_York")
        now = {"t": datetime(2026, 10, 5, 21, 30, tzinfo=ny)}
        monkeypatch.setattr(api_mod.dt_util, "now", lambda *a, **k: now["t"])

        client._battery_flow_last_reset = date(2026, 10, 5)
        client._update_battery_flow(11.0)
        client._update_battery_flow(10.5)        # -0.5 discharge
        assert client._battery_discharge_today_kwh == pytest.approx(0.5)
        assert client._battery_flow_last_reset == date(2026, 10, 5)

        # Local midnight passes → reset, then the new delta counts.
        now["t"] = datetime(2026, 10, 6, 0, 5, tzinfo=ny)
        client._update_battery_flow(10.2)        # -0.3 discharge
        assert client._battery_discharge_today_kwh == pytest.approx(0.3)
        assert client._battery_flow_last_reset == date(2026, 10, 6)

    def test_exactly_threshold_counts(self, client):
        # Boundary check — the threshold is inclusive (>= and <=).
        client._update_battery_flow(11.0)
        client._update_battery_flow(11.05)
        assert client._battery_charge_today_kwh == pytest.approx(0.05)
