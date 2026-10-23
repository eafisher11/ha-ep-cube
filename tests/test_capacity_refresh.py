"""The coordinator refreshes system capacity from deviceList at runtime.

Before this, capacity was read from deviceList only in the config flow, so an
entry that cached 0 kWh (a Gen 1 system set up before Gen 1 support) reported
0 kWh forever: Gen 1 homeDeviceInfo has no batteryPackNum to fall back on.
"""
from __future__ import annotations

import threading
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from custom_components.ep_cube.api import EPCubeClient, ServerError
from custom_components.ep_cube.coordinator import EPCubeCoordinator

_orig_enumerate = threading.enumerate


def _filtered_enumerate():
    return [t for t in _orig_enumerate() if "_run_safe_shutdown_loop" not in t.name]


@pytest.fixture(autouse=True)
def filter_aiohttp_shutdown_thread(monkeypatch):
    monkeypatch.setattr(threading, "enumerate", _filtered_enumerate)


@pytest.fixture(autouse=True)
def no_repair_issue():
    with patch("custom_components.ep_cube.coordinator.evaluate_predbat_priority_issue"):
        yield


def _make(hass, device_list, home_device_info, get_switch_mode, *, capacity_kwh=0.0):
    client = EPCubeClient(
        async_get_clientsession(hass),
        base_url="https://mock.invalid",
        dev_id=str(device_list[0]["id"]),
        sg_sn=device_list[0]["sgSn"],
        bearer_token="t",
        capacity_kwh=capacity_kwh,
    )
    calls = {"deviceList": 0}

    async def _get(path):
        if "deviceList" in path:
            calls["deviceList"] += 1
            return device_list
        if "homeDeviceInfo" in path:
            return home_device_info
        return get_switch_mode

    client._get = AsyncMock(side_effect=_get)
    client.get_switch_mode = AsyncMock(return_value={})
    client.get_pv_strings = AsyncMock(return_value={})
    client.get_warranty = AsyncMock(return_value=None)
    client.get_firmware_info = AsyncMock(return_value=None)
    client.get_device_details = AsyncMock(return_value=None)
    client._lifetime_known = True
    entry = MagicMock()
    entry.entry_id = "test_entry"
    coord = EPCubeCoordinator(hass, entry, client)
    coord._battery_flow_store = MagicMock()
    return coord, client, calls


@pytest.fixture
def gen1_home_device_info(home_device_info):
    # Gen 1 homeDeviceInfo doesn't carry batteryPackNum.
    info = dict(home_device_info)
    info.pop("batteryPackNum", None)
    return info


class TestCapacityRefresh:
    async def test_existing_zero_entry_gets_gen1_capacity(
        self, hass, device_list_gen1, gen1_home_device_info, get_switch_mode
    ):
        coord, _client, _ = _make(hass, device_list_gen1, gen1_home_device_info, get_switch_mode)
        status = await coord._async_update_data()
        assert status.capacity_kwh == 39.6

    async def test_refreshed_daily_not_every_poll(
        self, hass, device_list_gen1, gen1_home_device_info, get_switch_mode
    ):
        coord, _client, calls = _make(hass, device_list_gen1, gen1_home_device_info, get_switch_mode)
        await coord._async_update_data()
        await coord._async_update_data()
        assert calls["deviceList"] == 1

        later = dt_util.utcnow() + timedelta(hours=25)
        with patch("custom_components.ep_cube.coordinator.dt_util.utcnow", return_value=later):
            await coord._async_update_data()
        assert calls["deviceList"] == 2

    async def test_failed_fetch_retries_next_poll(
        self, hass, device_list_gen1, gen1_home_device_info, get_switch_mode
    ):
        coord, client, _ = _make(hass, device_list_gen1, gen1_home_device_info, get_switch_mode)
        real_get = client._get.side_effect
        failed = {"done": False}

        async def _flaky(path):
            if "deviceList" in path and not failed["done"]:
                failed["done"] = True
                raise ServerError("502")
            return await real_get(path)

        client._get.side_effect = _flaky
        status = await coord._async_update_data()
        assert status.capacity_kwh == 0.0     # cached value kept on failure
        status = await coord._async_update_data()
        assert status.capacity_kwh == 39.6    # retried on the next poll

    async def test_gen2_unchanged(self, hass, device_list, home_device_info, get_switch_mode):
        coord, _client, _ = _make(hass, device_list, home_device_info, get_switch_mode)
        status = await coord._async_update_data()
        assert status.capacity_kwh == 20.0
