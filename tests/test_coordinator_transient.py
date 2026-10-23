"""EPCubeCoordinator rides out short cloud blips instead of going unavailable.

A single HTTP 502 from the cloud at 03:52 on 2026-10-16 made every EP Cube
entity unavailable for about a minute. The coordinator now keeps the last good
data through up to MAX_TRANSIENT_FAILURES consecutive transient errors (5xx,
429, network, timeout); auth and other errors still fail at once, and so does
a failure on the very first refresh (there is no data to keep).
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.ep_cube.api import AuthError, EPCubeError, RateLimitError, ServerError
from custom_components.ep_cube.coordinator import MAX_TRANSIENT_FAILURES, EPCubeCoordinator

# Same aiohttp daemon-thread filter as the other coordinator tests.
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


def _make(hass, outcomes):
    """Coordinator whose get_status yields `outcomes` in turn (exception or value)."""
    client = MagicMock()
    client.get_status = AsyncMock(side_effect=list(outcomes))
    client.get_switch_mode = AsyncMock(return_value={})
    client.get_pv_strings = AsyncMock(return_value={})
    client.get_warranty = AsyncMock(return_value=None)
    client.get_firmware_info = AsyncMock(return_value=None)
    client.get_device_details = AsyncMock(return_value=None)
    client.refresh_capacity = AsyncMock(return_value=True)
    client.lifetime_known = True
    entry = MagicMock()
    entry.entry_id = "test_entry"
    coord = EPCubeCoordinator(hass, entry, client)
    # These tests poll directly; a real delayed save would leave a timer behind.
    coord._battery_flow_store = MagicMock()
    return coord, client


GOOD = object()  # stands in for a DeviceStatus


class TestTransientErrors:
    async def test_single_502_keeps_last_data(self, hass):
        coord, _ = _make(hass, [GOOD, ServerError("GET x returned 502: <html>")])
        coord.data = await coord._async_update_data()
        assert coord.data is GOOD
        assert await coord._async_update_data() is GOOD

    async def test_failures_up_to_the_limit_are_tolerated_then_it_fails(self, hass):
        errs = [ServerError("502")] * (MAX_TRANSIENT_FAILURES + 1)
        coord, _ = _make(hass, [GOOD, *errs])
        coord.data = await coord._async_update_data()
        for _ in range(MAX_TRANSIENT_FAILURES):
            assert await coord._async_update_data() is GOOD
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()

    async def test_success_resets_the_failure_count(self, hass):
        err = ServerError("502")
        coord, _ = _make(hass, [GOOD, err, err, GOOD, err, err, err])
        coord.data = await coord._async_update_data()
        for _ in range(2):
            await coord._async_update_data()
        assert coord._transient_failures == 2
        await coord._async_update_data()  # success
        assert coord._transient_failures == 0
        # A fresh run of MAX failures is tolerated again.
        for _ in range(MAX_TRANSIENT_FAILURES):
            assert await coord._async_update_data() is GOOD

    @pytest.mark.parametrize(
        "err",
        [
            ServerError("503"),
            RateLimitError("429"),
            aiohttp.ClientConnectionError("reset"),
            asyncio.TimeoutError(),
        ],
    )
    async def test_other_transient_errors_are_tolerated(self, hass, err):
        coord, _ = _make(hass, [GOOD, err])
        coord.data = await coord._async_update_data()
        assert await coord._async_update_data() is GOOD


class TestNonTransientErrors:
    async def test_auth_error_fails_immediately(self, hass):
        coord, _ = _make(hass, [GOOD, AuthError("token rejected")])
        coord.data = await coord._async_update_data()
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()

    async def test_other_cloud_error_fails_immediately(self, hass):
        coord, _ = _make(hass, [GOOD, EPCubeError("GET x returned 404")])
        coord.data = await coord._async_update_data()
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()

    async def test_first_refresh_failure_has_nothing_to_keep(self, hass):
        coord, _ = _make(hass, [ServerError("502")])
        assert coord.data is None
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
