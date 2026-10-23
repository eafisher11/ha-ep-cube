"""Shared pytest fixtures.

Uses `pytest-homeassistant-custom-component` for the HA-aware tests (config
flow, coordinator/entity setup). Pure-Python tests against api.py / shim /
predbat_state don't need any HA fixtures and stay framework-agnostic.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# `enable_custom_integrations` is the canonical fixture from
# pytest-homeassistant-custom-component — it tells HA to look in
# custom_components/ rather than only the core integrations dir.
pytest_plugins = ["pytest_homeassistant_custom_component"]


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# Auto-enable custom integrations for every test (avoids a deprecation
# warning when individual tests forget the fixture).
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):  # noqa: ARG001
    """Activate HA's custom_components discovery for every test."""
    yield


# ----------------------------------------------------------------------
# JSON fixture loaders — used by api/transport + setup tests.
# ----------------------------------------------------------------------
@pytest.fixture
def home_device_info() -> dict[str, Any]:
    """Canonical /api/device/homeDeviceInfo payload (EU mobile shape)."""
    return _load("home_device_info.json")


@pytest.fixture
def get_switch_mode() -> dict[str, Any]:
    """Canonical /api/device/getSwitchMode payload."""
    return _load("get_switch_mode.json")


@pytest.fixture
def device_list() -> list[dict[str, Any]]:
    """Canonical /api/device/deviceList payload."""
    return _load("device_list.json")


@pytest.fixture
def device_list_gen1() -> list[dict[str, Any]]:
    """deviceList from a Gen 1 (EP Cube 1.0) system: no systemCapacity, two
    inverter stacks of 6 battery modules each under snData."""
    return _load("device_list_gen1.json")


# ----------------------------------------------------------------------
# A pre-built fake client for tests that exercise sensor/coordinator/shim
# code without mocking HTTP. Returns a MagicMock with AsyncMock methods.
# ----------------------------------------------------------------------
@pytest.fixture
def fake_client(get_switch_mode, device_list):
    from datetime import date

    from custom_components.ep_cube.api import (
        DeviceDetails,
        DeviceStatus,
        FirmwareInfo,
        WarrantyInfo,
    )

    client = MagicMock()
    client.dev_id = "5613"
    client.sg_sn = "100100007001257120126"
    client.device_id = "5613"
    client.capacity_kwh = 20.0
    client.bearer_token = "fake-token"

    # The coordinator saves/restores the battery totals through these; they must
    # return real data so a delayed save never tries to serialise a MagicMock.
    # battery_flow_state is a plain function: HA's storage test helper rejects
    # a Mock object even when it is only the data callback.
    def _battery_flow_state():
        return {
            "date": "2026-01-01",
            "charged_kwh": 1.5,
            "discharged_kwh": 0.5,
            "last_kwh": 10.0,
        }

    client.battery_flow_state = _battery_flow_state
    client.restore_battery_flow = MagicMock(return_value=False)
    # The lifetime total counts as already known, so the coordinator doesn't try to
    # seed it from a recorder these tests don't have.
    client.lifetime_known = True
    client.get_firmware_info = AsyncMock(
        return_value=FirmwareInfo(firmware_version="V1.3.1", rtu_version="V1.1.0.1", auto_update=True)
    )
    client.get_device_details = AsyncMock(
        return_value=DeviceDetails(
            model="EP Cube HES-EU2-S7-20G", serial_number="SN-TEST-1", rtu_serial_number="RTU-TEST-1"
        )
    )
    client.get_warranty = AsyncMock(
        return_value=WarrantyInfo(start=date(2026, 5, 20), end=date(2036, 5, 20))
    )

    client.authenticate = AsyncMock(return_value=None)
    client.refresh_capacity = AsyncMock(return_value=True)
    client.get_device_list = AsyncMock(return_value=device_list)
    client.get_switch_mode = AsyncMock(return_value=dict(get_switch_mode))
    client.switch_mode = AsyncMock(return_value=dict(get_switch_mode))
    async def _dynamic_prices(date_str=None):
        from datetime import date, datetime, timedelta

        from homeassistant.util import dt as dt_util

        day = date.fromisoformat(date_str) if date_str else dt_util.now().date()
        start = datetime.combine(day, datetime.min.time())
        fmt = "%Y-%m-%d %H:%M:%S"
        slots = [
            {"start": (start + timedelta(minutes=30 * n)).strftime(fmt),
             "end": (start + timedelta(minutes=30 * (n + 1))).strftime(fmt)}
            for n in range(48)
        ]
        return {
            "import": [{**s, "price": 30.0} for s in slots],
            "export": [{**s, "price": 15.0} for s in slots],
        }

    client.get_dynamic_prices = AsyncMock(side_effect=_dynamic_prices)
    # Dynamic-tariff pickers (UK / Octopus, as the real EU server returned them).
    client.get_dynamic_relation = AsyncMock(return_value={
        "brandId": "99", "supportCountryId": "237", "productBrand": "OCTOPUS_ENERGY",
        "productCode": "AGILE-24-10-01", "productCodeOut": "AGILE-OUTGOING-19-05-13",
        "regionCode": "L", "supportCountryName": "United Kingdom",
    })
    client.get_dynamic_countries = AsyncMock(return_value=[
        {"supportCountryId": "237", "supportCountryName": "United Kingdom", "currentCountry": True},
        {"supportCountryId": "56", "supportCountryName": "Germany", "currentCountry": False},
    ])
    client.get_dynamic_regions = AsyncMock(return_value=[
        {"regionCode": "L", "regionName": "South Western England"},
        {"regionCode": "C", "regionName": "London"},
    ])
    client.get_dynamic_brands = AsyncMock(return_value=[
        {"id": "99", "brandName": "OCTOPUS_ENERGY"},
    ])

    async def _products(brand, region, product_type):
        if product_type == 1:
            return [
                {"productCode": "AGILE-OUTGOING-19-05-13", "productName": "Agile Outgoing Octopus",
                 "productFullName": "Agile Outgoing Octopus May 2019"},
                {"productCode": "OUTGOING-VAR-24-10-26", "productName": "Outgoing Octopus",
                 "productFullName": "Outgoing Octopus"},
            ]
        return [
            {"productCode": "AGILE-24-10-01", "productName": "Agile Octopus",
             "productFullName": "Agile Octopus October 2024 v1"},
            {"productCode": "AGILE-24-04-03", "productName": "Agile Octopus",
             "productFullName": "Agile Octopus April 2024 v1"},
        ]

    client.get_dynamic_products = AsyncMock(side_effect=_products)
    client.save_dynamic_binding = AsyncMock(return_value=None)
    client.get_pv_strings = AsyncMock(return_value={
        1: {"power_w": 180.0, "voltage_v": 255.5, "current_a": 0.7},
        2: {"power_w": 170.0, "voltage_v": 179.4, "current_a": 0.9},
        3: {"power_w": 0.0, "voltage_v": 0.0, "current_a": 0.0},
        4: {"power_w": 0.0, "voltage_v": 0.0, "current_a": 0.0},
    })
    # Phase 4.2 stats endpoint. Returns a small but representative payload
    # so the stats coordinator's first_refresh succeeds and STATS_SENSORS
    # get registered. Same dict for every scope — tests that care about the
    # cadence logic stub this per-call in test_stats_coordinator.py.
    client.get_stats = AsyncMock(return_value={
        "gridelectricityfrom": 0.44,
        "gridelectricityto": 7.88,
        "solarelectricity": 27.87,
        "backupelectricity": 18.47,
        "selfhelprate": 98,
        "treenum": 1.01585,
        "coal": 7.388,
    })

    client.get_status = AsyncMock(
        return_value=DeviceStatus(
            soc_pct=55.0,
            soc_kwh=11.0,
            capacity_kwh=20.0,
            battery_power_w=400.0,
            grid_power_w=0.0,
            solar_power_w=1200.0,
            load_power_w=800.0,
            operating_mode="self_consumption",
            reserve_soc_pct=20.0,
            allow_grid_charge=True,
            self_consumption_reserve_pct=20.0,
            backup_reserve_pct=100.0,
            dst_active=False,
            solar_today_kwh=0.35,
            backup_today_kwh=1.26,
            solar_dc_today_kwh=0.36,
            solar_ac_today_kwh=0.34,
            self_sufficiency_pct=65.0,
            winter_protect_pct=85.0,
            earning_yesterday=1.23,
            grid_outage_count=2,
            off_grid_seconds=1837,
            battery_charge_today_kwh=0.0,
            battery_discharge_today_kwh=0.0,
        )
    )
    return client


# ----------------------------------------------------------------------
# Config entry factory used by integration-shape tests (config_flow, setup).
# ----------------------------------------------------------------------
@pytest.fixture
def mock_config_entry():
    """Return a builder that materialises a MockConfigEntry on demand."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ep_cube.const import (
        CONF_API_PREFIX,
        CONF_BASE_URL,
        CONF_BEARER_TOKEN,
        CONF_CAPACITY_KWH,
        CONF_DEV_ID,
        CONF_PASSWORD,
        CONF_REGION,
        CONF_SG_SN,
        CONF_USERNAME,
        DOMAIN,
        REGION_EU,
    )

    def _make(**overrides: Any) -> MockConfigEntry:
        data = {
            CONF_REGION: REGION_EU,
            CONF_BASE_URL: "https://monitoring-eu.epcube.com",
            CONF_API_PREFIX: "/api",
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "hunter2",
            CONF_BEARER_TOKEN: "fake-bearer-token",
            CONF_DEV_ID: "5613",
            CONF_SG_SN: "100100007001257120126",
            CONF_CAPACITY_KWH: 20.0,
        }
        data.update(overrides)
        return MockConfigEntry(
            domain=DOMAIN,
            data=data,
            unique_id=data[CONF_DEV_ID],
            version=5,
        )

    return _make
