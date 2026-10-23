"""Lifetime battery discharge, estimated cycles and the warranty end date."""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.ep_cube import api as api_mod
from custom_components.ep_cube.api import EPCubeClient, EPCubeError, WarrantyInfo
from custom_components.ep_cube.const import BATTERY_WARRANTY_CYCLES, DOMAIN
from custom_components.ep_cube.coordinator import (
    LIFETIME_SEED_ATTEMPTS,
    WARRANTY_REFRESH,
    EPCubeCoordinator,
)
from custom_components.ep_cube.sensor import (
    BATTERY_CYCLES_SENSOR,
    WARRANTY_END_SENSOR,
    EPCubeCyclesSensor,
    EPCubeWarrantySensor,
)


def _new_client() -> EPCubeClient:
    return EPCubeClient(
        session=MagicMock(), base_url="https://mock.invalid",
        dev_id="5613", sg_sn="x", bearer_token="t",
    )


@pytest.fixture
def client() -> EPCubeClient:
    return _new_client()


def _coordinator(hass, client) -> EPCubeCoordinator:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    coord = EPCubeCoordinator(hass, entry, client)
    coord._battery_flow_store = MagicMock()
    return coord


class TestLifetimeTracking:
    def test_discharge_adds_to_the_lifetime_total_and_charge_does_not(self, client):
        client._update_battery_flow(12.0)
        client._update_battery_flow(11.0)   # 1.0 kWh out
        client._update_battery_flow(11.8)   # 0.8 kWh in
        client._update_battery_flow(10.8)   # 1.0 kWh out
        assert client.battery_discharge_lifetime_kwh == pytest.approx(2.0)
        assert client._battery_discharge_today_kwh == pytest.approx(2.0)

    def test_jitter_below_the_threshold_is_not_counted(self, client):
        client._update_battery_flow(12.0)
        client._update_battery_flow(11.97)
        assert client.battery_discharge_lifetime_kwh == 0.0

    def test_midnight_resets_the_daily_total_but_not_the_lifetime(self, client):
        client._update_battery_flow(12.0)
        client._update_battery_flow(11.0)
        client._battery_flow_last_reset = api_mod._local_today() - timedelta(days=1)
        client._update_battery_flow(10.0)
        assert client._battery_discharge_today_kwh == pytest.approx(1.0)
        assert client.battery_discharge_lifetime_kwh == pytest.approx(2.0)


class TestLifetimePersistence:
    def test_saved_with_the_daily_state(self, client):
        client._update_battery_flow(12.0)
        client._update_battery_flow(11.0)
        assert client.battery_flow_state()["lifetime_discharged_kwh"] == pytest.approx(1.0)

    def test_round_trip_into_a_fresh_client(self, client):
        client._update_battery_flow(12.0)
        client._update_battery_flow(10.5)
        fresh = _new_client()
        assert fresh.lifetime_known is False
        fresh.restore_battery_flow(client.battery_flow_state())
        assert fresh.battery_discharge_lifetime_kwh == pytest.approx(1.5)
        assert fresh.lifetime_known is True

    def test_restored_even_when_the_daily_part_is_from_yesterday(self, client):
        state = {
            "date": "2020-01-01", "charged_kwh": 1.0, "discharged_kwh": 1.0,
            "last_kwh": 10.0, "lifetime_discharged_kwh": 321.5,
        }
        assert client.restore_battery_flow(state) is False   # the day's totals are stale
        assert client.battery_discharge_lifetime_kwh == pytest.approx(321.5)
        assert client.lifetime_known is True

    def test_a_state_saved_before_this_feature_leaves_it_unknown(self, client):
        state = {
            "date": api_mod._local_today().isoformat(), "charged_kwh": 1.0,
            "discharged_kwh": 1.0, "last_kwh": 10.0,
        }
        assert client.restore_battery_flow(state) is True
        assert client.lifetime_known is False
        assert client.battery_discharge_lifetime_kwh == 0.0

    @pytest.mark.parametrize("bad", ["x", -1, float("nan"), float("inf"), None])
    def test_a_bad_saved_total_is_ignored(self, client, bad):
        state = {
            "date": api_mod._local_today().isoformat(), "charged_kwh": 1.0,
            "discharged_kwh": 1.0, "last_kwh": 10.0, "lifetime_discharged_kwh": bad,
        }
        client.restore_battery_flow(state)
        assert client.lifetime_known is False
        assert client.battery_discharge_lifetime_kwh == 0.0


class TestSeed:
    def test_seeds_an_unknown_total(self, client):
        assert client.seed_battery_discharge_lifetime(1475.2) is True
        assert client.battery_discharge_lifetime_kwh == pytest.approx(1475.2)
        assert client.lifetime_known is True

    def test_never_overrides_a_known_total(self, client):
        client.restore_battery_flow({"date": "2020-01-01", "lifetime_discharged_kwh": 10.0})
        assert client.seed_battery_discharge_lifetime(999.0) is False
        assert client.battery_discharge_lifetime_kwh == pytest.approx(10.0)

    def test_keeps_what_has_been_counted_since_start_up(self, client):
        client._update_battery_flow(12.0)
        client._update_battery_flow(8.0)    # 4 kWh counted this session
        assert client.seed_battery_discharge_lifetime(2.0) is True
        assert client.battery_discharge_lifetime_kwh == pytest.approx(4.0)

    @pytest.mark.parametrize("bad", [0, -5, "x", None, float("nan"), float("inf")])
    def test_bad_seeds_are_ignored(self, client, bad):
        assert client.seed_battery_discharge_lifetime(bad) is False
        assert client.lifetime_known is False


class TestGetWarranty:
    async def test_parses_the_dates(self, client):
        client._get = AsyncMock(return_value={
            "activationDate": "2026-05-20", "usageEndDate": "2036-05-20",
            "sgSn": "x", "name": "y",
        })
        assert await client.get_warranty() == WarrantyInfo(date(2026, 5, 20), date(2036, 5, 20))

    async def test_a_single_usable_date_is_enough(self, client):
        client._get = AsyncMock(return_value={"usageEndDate": "2041-07-02 00:00:00"})
        assert await client.get_warranty() == WarrantyInfo(None, date(2041, 7, 2))

    @pytest.mark.parametrize(
        "payload",
        [None, [], "x", {}, {"activationDate": "soon"}, {"message": "[/device/getWarranty] Not Found", "status": 404}],
    )
    async def test_no_usable_dates_gives_none(self, client, payload):
        client._get = AsyncMock(return_value=payload)
        assert await client.get_warranty() is None


class TestCoordinatorWarranty:
    async def test_fetched_once_then_cached_for_a_day(self, hass):
        client = MagicMock()
        info = WarrantyInfo(date(2026, 5, 20), date(2036, 5, 20))
        client.get_warranty = AsyncMock(return_value=info)
        coord = _coordinator(hass, client)
        await coord._async_refresh_warranty()
        await coord._async_refresh_warranty()
        assert coord.warranty == info
        assert client.get_warranty.await_count == 1
        coord._warranty_fetched -= WARRANTY_REFRESH + timedelta(minutes=1)
        await coord._async_refresh_warranty()
        assert client.get_warranty.await_count == 2

    async def test_a_failed_fetch_keeps_the_previous_value(self, hass):
        client = MagicMock()
        info = WarrantyInfo(date(2026, 5, 20), date(2036, 5, 20))
        client.get_warranty = AsyncMock(side_effect=[info, EPCubeError("boom"), None])
        coord = _coordinator(hass, client)
        await coord._async_refresh_warranty()
        for _ in range(2):
            coord._warranty_fetched -= WARRANTY_REFRESH + timedelta(minutes=1)
            await coord._async_refresh_warranty()
        assert coord.warranty == info


class TestSeedOrder:
    async def test_the_first_status_carries_the_seeded_total(self, hass):
        """The seed must happen before the poll builds its status, or the sensors
        read 0 for a minute after every restart."""
        order: list[str] = []
        client = MagicMock()
        client.get_status = AsyncMock(side_effect=lambda: order.append("status") or object())
        client.get_switch_mode = AsyncMock(return_value={})
        client.get_pv_strings = AsyncMock(return_value={})
        client.get_warranty = AsyncMock(return_value=None)
        client.get_firmware_info = AsyncMock(return_value=None)
        client.get_device_details = AsyncMock(return_value=None)
        client.refresh_capacity = AsyncMock(return_value=True)
        coord = _coordinator(hass, client)
        coord._async_seed_lifetime = AsyncMock(side_effect=lambda: order.append("seed"))
        with patch("custom_components.ep_cube.coordinator.evaluate_predbat_priority_issue"):
            await coord._async_update_data()
        assert order == ["seed", "status"]


class TestCoordinatorSeed:
    def _register(self, hass, entry_id="test_entry"):
        return er.async_get(hass).async_get_or_create(
            "sensor", DOMAIN, f"{entry_id}_battery_discharge_today",
            suggested_object_id="battery_discharged_today",
        ).entity_id

    async def test_seeds_from_the_recorder_sum(self, hass, client):
        entity_id = self._register(hass)
        coord = _coordinator(hass, client)
        fake_recorder = MagicMock()
        fake_recorder.async_add_executor_job = AsyncMock(
            return_value={entity_id: [{"sum": 1475.2}]}
        )
        with patch("homeassistant.components.recorder.get_instance", return_value=fake_recorder):
            await coord._async_seed_lifetime()
        assert client.battery_discharge_lifetime_kwh == pytest.approx(1475.2)
        assert client.lifetime_known is True

    async def test_skipped_when_the_total_is_already_known(self, hass, client):
        client.restore_battery_flow({"date": "2020-01-01", "lifetime_discharged_kwh": 5.0})
        coord = _coordinator(hass, client)
        with patch("homeassistant.components.recorder.get_instance") as get_instance:
            await coord._async_seed_lifetime()
        get_instance.assert_not_called()

    async def test_a_missing_recorder_is_survived_and_retried_a_few_times(self, hass, client):
        self._register(hass)
        coord = _coordinator(hass, client)
        with patch(
            "homeassistant.components.recorder.get_instance", side_effect=KeyError("recorder")
        ) as get_instance:
            for _ in range(LIFETIME_SEED_ATTEMPTS + 5):
                await coord._async_seed_lifetime()
        assert client.lifetime_known is False
        assert get_instance.call_count == LIFETIME_SEED_ATTEMPTS

    async def test_no_registered_sensor_means_nothing_to_seed(self, hass, client):
        coord = _coordinator(hass, client)
        await coord._async_seed_lifetime()
        assert client.lifetime_known is False


def _sensor_coordinator(lifetime_kwh, capacity_kwh, warranty):
    return SimpleNamespace(
        data=SimpleNamespace(
            battery_discharge_lifetime_kwh=lifetime_kwh, capacity_kwh=capacity_kwh,
        ),
        warranty=warranty,
        last_update_success=True,
        async_add_listener=MagicMock(),
    )


class TestSensors:
    def test_cycles_is_lifetime_energy_over_capacity(self):
        coord = _sensor_coordinator(1475.0, 20.0, None)
        sensor = EPCubeCyclesSensor(coord, "e", BATTERY_CYCLES_SENSOR)
        assert sensor.native_value == pytest.approx(73.75)

    def test_cycles_unknown_without_a_capacity(self):
        coord = _sensor_coordinator(100.0, 0.0, None)
        assert EPCubeCyclesSensor(coord, "e", BATTERY_CYCLES_SENSOR).native_value is None

    def test_attributes_follow_the_warranty_dates_ten_year(self):
        start = dt_util.now().date() - timedelta(days=100)
        end = start + timedelta(days=3653)
        coord = _sensor_coordinator(1000.0, 20.0, WarrantyInfo(start, end))
        attrs = EPCubeCyclesSensor(coord, "e", BATTERY_CYCLES_SENSOR).extra_state_attributes
        assert attrs["warranty_cycle_limit"] == BATTERY_WARRANTY_CYCLES
        assert attrs["percent_of_cycle_allowance"] == pytest.approx(50 / 6000 * 100, abs=0.01)
        assert attrs["warranty_end"] == end.isoformat()
        assert attrs["cycle_budget_per_day"] == pytest.approx(1.64, abs=0.01)
        assert attrs["average_cycles_per_day"] == pytest.approx(0.5, abs=0.01)

    def test_a_longer_warranty_lowers_the_daily_budget(self):
        start = dt_util.now().date() - timedelta(days=10)
        coord = _sensor_coordinator(100.0, 20.0, WarrantyInfo(start, start + timedelta(days=5479)))
        attrs = EPCubeCyclesSensor(coord, "e", BATTERY_CYCLES_SENSOR).extra_state_attributes
        assert attrs["cycle_budget_per_day"] == pytest.approx(1.1, abs=0.01)

    def test_attributes_without_warranty_dates_still_give_the_allowance(self):
        coord = _sensor_coordinator(100.0, 20.0, None)
        attrs = EPCubeCyclesSensor(coord, "e", BATTERY_CYCLES_SENSOR).extra_state_attributes
        assert attrs == {"warranty_cycle_limit": 6000, "percent_of_cycle_allowance": pytest.approx(0.08)}

    def test_warranty_end_sensor_reads_the_date(self):
        end = date(2036, 5, 20)
        coord = _sensor_coordinator(0.0, 20.0, WarrantyInfo(date(2026, 5, 20), end))
        sensor = EPCubeWarrantySensor(coord, "e", WARRANTY_END_SENSOR)
        assert sensor.native_value == end
        assert sensor.available is True

    def test_warranty_end_sensor_unavailable_until_the_cloud_answers(self):
        coord = _sensor_coordinator(0.0, 20.0, None)
        sensor = EPCubeWarrantySensor(coord, "e", WARRANTY_END_SENSOR)
        assert sensor.native_value is None
        assert sensor.available is False
