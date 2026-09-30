"""Transport-level tests for EPCubeClient.

Uses PHCC's `aioclient_mock` fixture (the canonical HA testing pattern —
patches HA's aiohttp client at the request layer, no real socket bind, no
threading complaints) to mock the cloud surface and exercise:
  - envelope unwrapping (outer + double-wrapped inner)
  - Bearer header presence
  - 403 → reauth_callback retry path
  - 429 RateLimitError + 5xx ServerError mapping
  - get_status: merges homeDeviceInfo + getSwitchMode into one DeviceStatus
  - api_prefix routing (EU /api vs US /app-api)
  - switch_mode verify path (success + WriteVerificationError)
"""
from __future__ import annotations

import re
import threading
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)


# aiohttp 3.13+ spawns a `_run_safe_shutdown_loop` daemon thread on
# ClientSession creation. PHCC's verify_cleanup flags this as a lingering
# thread on the first test that creates a session. Filter it out — the
# thread is daemonised and won't outlive the process.
_orig_enumerate = threading.enumerate


def _filtered_enumerate():
    return [
        t for t in _orig_enumerate()
        if "_run_safe_shutdown_loop" not in t.name
    ]


@pytest.fixture(autouse=True)
def filter_aiohttp_shutdown_thread(monkeypatch):
    monkeypatch.setattr(threading, "enumerate", _filtered_enumerate)

from custom_components.ep_cube.api import (
    AuthError,
    EPCubeClient,
    EPCubeError,
    RateLimitError,
    ServerError,
    WriteVerificationError,
    build_switch_mode_payload,
)


def _envelope(data, *, status: int = 200, message: str = "OK") -> dict:
    """Wrap a body in the cloud's outer envelope."""
    return {"timestamp": "2026-05-28 10:00:00", "message": message,
            "status": status, "data": data}


def _url_re(path: str) -> re.Pattern:
    """Match the path with or without a querystring (cube appends ?devId=...)."""
    return re.compile(rf"^https://mock\.invalid{re.escape(path)}(\?.*)?$")


@pytest.fixture
def client_factory(hass):
    """Builds an EPCubeClient against hass's aiohttp session.

    aioclient_mock intercepts requests on that session, so no real network
    fires and no extra ClientSession threads spawn.
    """
    def _make(**overrides) -> EPCubeClient:
        session = async_get_clientsession(hass)
        kwargs = dict(
            session=session,
            base_url="https://mock.invalid",
            dev_id="5613",
            sg_sn="100100007001257120126",
            bearer_token="initial-token",
            api_prefix="/api",
        )
        kwargs.update(overrides)
        return EPCubeClient(**kwargs)
    return _make


# ----------------------------------------------------------------------
# Envelope unwrapping
# ----------------------------------------------------------------------
class TestEnvelopeHandling:
    async def test_unwraps_data_field(self, aioclient_mock, client_factory, device_list):
        aioclient_mock.get(
            _url_re("/api/device/deviceList"),
            json=_envelope(device_list),
        )
        result = await client_factory().get_device_list()
        assert result == device_list

    async def test_outer_envelope_error_raises(self, aioclient_mock, client_factory):
        aioclient_mock.get(
            _url_re("/api/device/deviceList"),
            json=_envelope(None, status=500, message="boom"),
        )
        with pytest.raises(EPCubeError, match="boom"):
            await client_factory().get_device_list()

    async def test_double_wrapped_inner_envelope_error(
        self, aioclient_mock, client_factory
    ):
        # Cube returns outer status:200 with inner data.status:404 for some
        # missing-endpoint paths. See api._request: only flags 4xx/5xx inner.
        inner = {"status": 404, "message": "not found"}
        aioclient_mock.get(
            _url_re("/api/device/getSwitchMode"),
            json=_envelope(inner),
        )
        with pytest.raises(EPCubeError, match="not found"):
            await client_factory().get_switch_mode()

    async def test_inner_status_1_is_domain_field_not_error(
        self, aioclient_mock, client_factory, home_device_info
    ):
        # homeDeviceInfo uses data.status="1" to mean "device online". This
        # MUST NOT be flagged as a transport error.
        aioclient_mock.get(
            _url_re("/api/device/homeDeviceInfo"),
            json=_envelope(home_device_info),
        )
        client = client_factory()
        info = await client._get("/api/device/homeDeviceInfo?sgSn=x")
        assert info["status"] == "1"


# ----------------------------------------------------------------------
# Auth header + 403 reauth path
# ----------------------------------------------------------------------
class TestAuthHeaders:
    async def test_bearer_header_sent(self, aioclient_mock, client_factory, device_list):
        aioclient_mock.get(
            _url_re("/api/device/deviceList"),
            json=_envelope(device_list),
        )
        await client_factory(bearer_token="my-token").get_device_list()
        # aioclient_mock records every call — mock_calls is a list of
        # (method, url, data, headers) tuples.
        assert aioclient_mock.call_count == 1
        method, url, data, headers = aioclient_mock.mock_calls[0]
        assert headers["Authorization"] == "Bearer my-token"

    async def test_missing_token_raises_auth_error(self, client_factory):
        client = client_factory(bearer_token=None)
        with pytest.raises(AuthError):
            await client.get_device_list()

    async def test_403_triggers_reauth_callback_and_retries(
        self, aioclient_mock, client_factory, device_list
    ):
        # First call 403s; reauth_callback returns a fresh token; retry succeeds.
        # aioclient_mock matches the first registered handler always — so
        # sequence via a counter on a single mock.
        calls = {"n": 0}

        async def handler(method, url, data):
            # PHCC's match_request awaits side_effect with (method, url, data).
            calls["n"] += 1
            if calls["n"] == 1:
                return AiohttpClientMockResponse(
                    method=method, url=url, status=403, response=b"token expired",
                )
            return AiohttpClientMockResponse(
                method=method, url=url, status=200, json=_envelope(device_list),
            )

        aioclient_mock.get(_url_re("/api/device/deviceList"), side_effect=handler)
        reauth = AsyncMock(return_value="refreshed-token")
        client = client_factory(reauth_callback=reauth)

        result = await client.get_device_list()

        reauth.assert_awaited_once()
        assert result == device_list
        assert client.bearer_token == "refreshed-token"
        assert calls["n"] == 2

    async def test_403_without_reauth_raises(self, aioclient_mock, client_factory):
        aioclient_mock.get(
            _url_re("/api/device/deviceList"),
            status=403, text="token expired",
        )
        with pytest.raises(AuthError):
            await client_factory().get_device_list()

    async def test_403_reauth_returning_none_raises(
        self, aioclient_mock, client_factory
    ):
        # If reauth callback declines (None), don't retry — propagate AuthError.
        aioclient_mock.get(
            _url_re("/api/device/deviceList"),
            status=403, text="token expired",
        )
        client = client_factory(reauth_callback=AsyncMock(return_value=None))
        with pytest.raises(AuthError):
            await client.get_device_list()

    async def test_403_reauth_loop_caps_at_one_retry(
        self, aioclient_mock, client_factory
    ):
        # Even if the refreshed token is also rejected, don't loop forever.
        # Single mock returning 403 for every call (no FIFO needed).
        aioclient_mock.get(
            _url_re("/api/device/deviceList"),
            status=403, text="token expired",
        )
        reauth = AsyncMock(return_value="still-bad")
        client = client_factory(reauth_callback=reauth)
        with pytest.raises(AuthError):
            await client.get_device_list()
        reauth.assert_awaited_once()


# ----------------------------------------------------------------------
# Status-code mapping
# ----------------------------------------------------------------------
class TestStatusCodes:
    async def test_429_rate_limit(self, aioclient_mock, client_factory):
        aioclient_mock.get(_url_re("/api/device/deviceList"), status=429)
        with pytest.raises(RateLimitError):
            await client_factory().get_device_list()

    async def test_500_server_error(self, aioclient_mock, client_factory):
        aioclient_mock.get(
            _url_re("/api/device/deviceList"), status=500, text="boom"
        )
        with pytest.raises(ServerError):
            await client_factory().get_device_list()

    async def test_400_other_error(self, aioclient_mock, client_factory):
        # Anything between 400-499 that's not 401/403/429 lands here.
        aioclient_mock.get(
            _url_re("/api/device/deviceList"), status=400, text="bad request"
        )
        with pytest.raises(EPCubeError) as exc:
            await client_factory().get_device_list()
        # Sanity: not a more specific subclass.
        assert not isinstance(exc.value, (AuthError, RateLimitError, ServerError))


# ----------------------------------------------------------------------
# api_prefix routing — US uses /app-api, EU/JP use /api.
# ----------------------------------------------------------------------
class TestApiPrefix:
    async def test_us_prefix_routes_to_app_api(
        self, aioclient_mock, client_factory, device_list
    ):
        aioclient_mock.get(
            _url_re("/app-api/device/deviceList"),
            json=_envelope(device_list),
        )
        client = client_factory(api_prefix="/app-api")
        result = await client.get_device_list()
        assert result == device_list

    async def test_eu_prefix_normalises_leading_slash(
        self, aioclient_mock, client_factory, device_list
    ):
        # Whether the caller passes "/api" or "api", the constructor normalises.
        aioclient_mock.get(
            _url_re("/api/device/deviceList"),
            json=_envelope(device_list),
        )
        client = client_factory(api_prefix="api")
        result = await client.get_device_list()
        assert result == device_list


# ----------------------------------------------------------------------
# get_status merges two reads into one DeviceStatus.
# ----------------------------------------------------------------------
class TestGetStatus:
    async def test_merges_home_device_info_and_switch_mode(
        self, aioclient_mock, client_factory, home_device_info, get_switch_mode
    ):
        # aioclient_mock dispatches by URL pattern — no ordering risk on the
        # parallel asyncio.gather inside get_status.
        aioclient_mock.get(
            _url_re("/api/device/homeDeviceInfo"),
            json=_envelope(home_device_info),
        )
        aioclient_mock.get(
            _url_re("/api/device/getSwitchMode"),
            json=_envelope(get_switch_mode),
        )

        status = await client_factory().get_status()

        # Power conservation: battery = solar + grid - load
        # = 1200 + 0 - (80 + 0)*10 W = 1200 - 800 = 400 W
        assert status.solar_power_w == 1200.0
        assert status.grid_power_w == 0.0
        assert status.load_power_w == 800.0
        assert status.battery_power_w == 400.0

        # SoC + capacity (4 packs × 5 kWh = 20 kWh)
        assert status.soc_pct == 55.0
        assert status.soc_kwh == 11.0
        assert status.capacity_kwh == 20.0

        # Mode + reserves from getSwitchMode
        assert status.operating_mode == "self_consumption"
        assert status.self_consumption_reserve_pct == 20.0
        assert status.backup_reserve_pct == 100.0
        assert status.reserve_soc_pct == 20.0       # active mode = self_consumption
        assert status.allow_grid_charge is True
        assert status.dst_active is False

        # Daily kWh totals from homeDeviceInfo (grid_today + nonbackup_today
        # deleted in v1.1.0 — see sensor.py STATS_SENSORS for replacements).
        assert status.solar_today_kwh == 0.35
        assert status.backup_today_kwh == 1.26

        # Phase 3.5 fields
        assert status.earning_yesterday == 1.23
        assert status.grid_outage_count == 2
        assert status.off_grid_seconds == 1837

        # Battery flow tracker primed on first call — no delta credited yet.
        assert status.battery_charge_today_kwh == 0.0
        assert status.battery_discharge_today_kwh == 0.0


# ----------------------------------------------------------------------
# switch_mode — POST + verify
# ----------------------------------------------------------------------
class TestSwitchMode:
    async def test_verify_success(
        self, aioclient_mock, client_factory, get_switch_mode
    ):
        # POST returns an envelope with no body.
        aioclient_mock.post(
            _url_re("/api/device/switchMode"),
            json=_envelope(None),
        )
        # Readback GET returns the new state.
        readback = dict(get_switch_mode)
        readback["workStatus"] = "2"
        aioclient_mock.get(
            _url_re("/api/device/getSwitchMode"),
            json=_envelope(readback),
        )

        payload = build_switch_mode_payload(dev_id="5613", work_status="2")
        result = await client_factory().switch_mode(payload)
        assert result["workStatus"] == "2"

    async def test_verify_failure_raises(
        self, aioclient_mock, client_factory, get_switch_mode
    ):
        aioclient_mock.post(
            _url_re("/api/device/switchMode"),
            json=_envelope(None),
        )
        # Cube acknowledged the POST but adopted a different mode (or none).
        aioclient_mock.get(
            _url_re("/api/device/getSwitchMode"),
            json=_envelope(get_switch_mode),  # workStatus still 1
        )

        payload = build_switch_mode_payload(dev_id="5613", work_status="2")
        with pytest.raises(WriteVerificationError):
            await client_factory().switch_mode(payload)


# ----------------------------------------------------------------------
# queryDataElectricityV2 — Phase 4.2 stats endpoint
# ----------------------------------------------------------------------
class TestGetStats:
    # The cube returns camelCase keys; get_stats lowercases at the boundary
    # so consumers don't carry the camelCase noise. Field set picked to match
    # what the live EU cube returns on scope=1 (verified 2026-06-04).
    _LIVE_KEYS = {
        "gridElectricity": 0.44,
        "gridElectricityFrom": 0.44,
        "gridElectricityTo": 7.88,
        "solarElectricity": 27.87,
        "backUpElectricity": 18.47,
        "selfHelpRate": 98,
        "treeNum": 1.01585,
        "coal": 7.388,
    }

    async def test_daily_scope_url_and_lowercase_keys(
        self, aioclient_mock, client_factory
    ):
        aioclient_mock.get(
            _url_re("/api/device/queryDataElectricityV2"),
            json=_envelope(dict(self._LIVE_KEYS)),
        )
        result = await client_factory().get_stats(1, "2026-06-04")

        method, url, data, headers = aioclient_mock.mock_calls[0]
        url_s = str(url)
        assert "devId=5613" in url_s
        assert "queryDateStr=2026-06-04" in url_s
        assert "scopeType=1" in url_s
        # Keys lowercased so downstream lookups don't carry camelCase noise.
        assert result["gridelectricityfrom"] == 0.44
        assert result["gridelectricityto"] == 7.88
        assert result["selfhelprate"] == 98
        assert "gridElectricityFrom" not in result

    async def test_monthly_scope_takes_yyyy_mm(self, aioclient_mock, client_factory):
        aioclient_mock.get(
            _url_re("/api/device/queryDataElectricityV2"),
            json=_envelope({"gridElectricityFrom": 16.75}),
        )
        await client_factory().get_stats(2, "2026-06")
        _, url, _, _ = aioclient_mock.mock_calls[0]
        url_s = str(url)
        assert "queryDateStr=2026-06" in url_s
        assert "scopeType=2" in url_s

    async def test_annual_scope_takes_yyyy(self, aioclient_mock, client_factory):
        aioclient_mock.get(
            _url_re("/api/device/queryDataElectricityV2"),
            json=_envelope({"gridElectricityFrom": 33.19}),
        )
        await client_factory().get_stats(3, "2026")
        _, url, _, _ = aioclient_mock.mock_calls[0]
        url_s = str(url)
        assert "queryDateStr=2026" in url_s
        assert "scopeType=3" in url_s

    async def test_total_scope_zero(self, aioclient_mock, client_factory):
        aioclient_mock.get(
            _url_re("/api/device/queryDataElectricityV2"),
            json=_envelope({}),
        )
        await client_factory().get_stats(0, "2026")
        _, url, _, _ = aioclient_mock.mock_calls[0]
        assert "scopeType=0" in str(url)

    async def test_empty_data_returns_empty_dict(
        self, aioclient_mock, client_factory
    ):
        aioclient_mock.get(
            _url_re("/api/device/queryDataElectricityV2"),
            json=_envelope({}),
        )
        result = await client_factory().get_stats(1, "2026-06-04")
        assert result == {}

    async def test_non_dict_data_returns_empty_dict(
        self, aioclient_mock, client_factory
    ):
        # Defensive: cube has been known to return null/list shapes on the
        # weirder endpoints; the helper should normalise to {} so consumers
        # can .get() without crashing.
        aioclient_mock.get(
            _url_re("/api/device/queryDataElectricityV2"),
            json=_envelope(None),
        )
        result = await client_factory().get_stats(1, "2026-06-04")
        assert result == {}

    async def test_us_region_uses_app_api_prefix(
        self, aioclient_mock, client_factory
    ):
        aioclient_mock.get(
            _url_re("/app-api/device/queryDataElectricityV2"),
            json=_envelope({"gridElectricityFrom": 1.0}),
        )
        await client_factory(api_prefix="/app-api").get_stats(1, "2026-06-04")
        _, url, _, _ = aioclient_mock.mock_calls[0]
        assert "/app-api/device/queryDataElectricityV2" in str(url)

    async def test_403_triggers_reauth_callback(
        self, aioclient_mock, client_factory
    ):
        # Reauth flow already covered for other endpoints — verify it kicks in
        # on the stats path too (uses the shared _request transport).
        calls = {"n": 0}

        async def handler(method, url, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return AiohttpClientMockResponse(
                    method=method, url=url, status=403, response=b"token expired",
                )
            return AiohttpClientMockResponse(
                method=method, url=url, status=200,
                json=_envelope({"gridElectricityFrom": 1.0}),
            )

        aioclient_mock.get(
            _url_re("/api/device/queryDataElectricityV2"), side_effect=handler,
        )
        reauth = AsyncMock(return_value="refreshed-token")
        client = client_factory(reauth_callback=reauth)
        result = await client.get_stats(1, "2026-06-04")
        assert calls["n"] == 2
        assert reauth.await_count == 1
        assert result["gridelectricityfrom"] == 1.0


# ----------------------------------------------------------------------
# EV charger fields (homeDeviceInfo evPower / evElectricity / hasEv)
# ----------------------------------------------------------------------
class TestEvFields:
    async def test_ev_fields_parsed_when_charger_present(
        self, aioclient_mock, client_factory, home_device_info, get_switch_mode
    ):
        # Shape taken from a live US cube (2026-09-30): float power values
        # and a native-bool hasEv.
        info = {
            **home_device_info,
            "hasEv": True,
            "evPower": 72.0,
            "evElectricity": 6.4,
        }
        aioclient_mock.get(_url_re("/api/device/homeDeviceInfo"), json=_envelope(info))
        aioclient_mock.get(_url_re("/api/device/getSwitchMode"), json=_envelope(get_switch_mode))

        status = await client_factory().get_status()

        assert status.has_ev is True
        assert status.ev_power_w == 720.0   # 0.01 kW wire units, same as solarPower
        assert status.ev_today_kwh == 6.4
        # EV draw is part of load: 800 W house + 720 W EV.
        assert status.load_power_w == 1520.0

    async def test_load_and_battery_include_ev_draw(
        self, aioclient_mock, client_factory, get_switch_mode
    ):
        # Live US cube reading taken mid-charge (2026-09-30 17:17 ET).
        # backUpPower + evPower == smartHomePower (77 + 162 == 239).
        info = {
            "workStatus": "1", "batterySoc": 95, "batteryCurrentElectricity": 18.98,
            "gridPower": 0.0, "solarPower": 41.0,
            "backUpPower": 77.0, "nonBackUpPower": 0.0,
            "evPower": 162.0, "evElectricity": 0.1, "hasEv": True,
            "smartHomePower": 239.0, "batteryPower": 0,
        }
        aioclient_mock.get(_url_re("/api/device/homeDeviceInfo"), json=_envelope(info))
        aioclient_mock.get(_url_re("/api/device/getSwitchMode"), json=_envelope(get_switch_mode))

        status = await client_factory().get_status()

        assert status.ev_power_w == 1620.0
        assert status.load_power_w == 2390.0   # == smartHomePower × 10
        # 410 W solar + 0 W grid − 2390 W load → battery discharging ~2 kW.
        # (The cube's own batteryPower field reads 0 here, so it's not used.)
        assert status.battery_power_w == -1980.0

    async def test_ev_absent_defaults_to_no_charger(
        self, aioclient_mock, client_factory, home_device_info, get_switch_mode
    ):
        # EU fixture has evPower/evElectricity zeros and no hasEv key.
        aioclient_mock.get(_url_re("/api/device/homeDeviceInfo"), json=_envelope(home_device_info))
        aioclient_mock.get(_url_re("/api/device/getSwitchMode"), json=_envelope(get_switch_mode))

        status = await client_factory().get_status()

        assert status.has_ev is False
        assert status.ev_power_w == 0.0
        assert status.ev_today_kwh == 0.0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(True, True), (False, False), ("1", True), ("0", False),
         ("true", True), (1, True), (None, False), ("", False)],
    )
    def test_has_ev_encodings(self, raw, expected):
        from custom_components.ep_cube.api import _truthy

        assert _truthy(raw) is expected
