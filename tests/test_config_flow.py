"""Config flow tests — user step, region routing, captcha errors, migration."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow

from custom_components.ep_cube.captcha import CaptchaSolveError, LoginError
from custom_components.ep_cube.const import (
    CONF_API_PREFIX,
    CONF_BASE_URL,
    CONF_BEARER_TOKEN,
    CONF_CAPACITY_KWH,
    CONF_DEV_ID,
    CONF_FORCE_EXPORT_MODE,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_SG_SN,
    CONF_USERNAME,
    DOMAIN,
    FORCE_EXPORT_MODE_DYNAMIC,
    FORCE_EXPORT_MODE_TOU_PEAK,
    REGION_EU,
    REGION_OTHER,
    REGION_US,
)


@pytest.fixture
def patch_captcha():
    """Patch captcha.login to return a fake bearer token without touching the network."""
    with patch(
        "custom_components.ep_cube.config_flow.captcha_login",
        new=AsyncMock(return_value="fresh-token"),
    ) as mock:
        yield mock


@pytest.fixture
def patch_device_list(device_list):
    """Patch EPCubeClient.get_device_list to return the canonical fixture."""
    with patch(
        "custom_components.ep_cube.config_flow.EPCubeClient.get_device_list",
        new=AsyncMock(return_value=device_list),
    ) as mock:
        yield mock


# ----------------------------------------------------------------------
# Form rendering + happy path
# ----------------------------------------------------------------------
class TestUserStep:
    async def test_form_shown_on_first_load(self, hass):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] is None or result["errors"] == {}

    async def test_eu_region_creates_entry(self, hass, patch_captcha, patch_device_list):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: "user@example.com",
                CONF_PASSWORD: "hunter2",
                CONF_REGION: REGION_EU,
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        entry = result["data"]
        assert entry[CONF_REGION] == REGION_EU
        assert entry[CONF_BASE_URL] == "https://monitoring-eu.epcube.com"
        assert entry[CONF_API_PREFIX] == "/api"
        assert entry[CONF_BEARER_TOKEN] == "fresh-token"
        assert entry[CONF_DEV_ID] == "5613"
        assert entry[CONF_SG_SN] == "100100007001257120126"
        assert entry[CONF_CAPACITY_KWH] == 20.0

    async def test_us_region_uses_app_api_prefix(
        self, hass, patch_captcha, patch_device_list
    ):
        # US's distinctive trait: different host AND path prefix. Regression
        # against accidentally dropping the prefix routing.
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: "u@e.com",
                CONF_PASSWORD: "x",
                CONF_REGION: REGION_US,
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_REGION] == REGION_US
        assert result["data"][CONF_BASE_URL] == "https://epcube-monitoring.com"
        assert result["data"][CONF_API_PREFIX] == "/app-api"

    async def test_other_region_requires_base_url(self, hass, patch_captcha):
        # REGION_OTHER without base_url should re-render the form with an error.
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: "u@e.com",
                CONF_PASSWORD: "x",
                CONF_REGION: REGION_OTHER,
                CONF_BASE_URL: "",
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["errors"] == {"base": "base_url_required"}
        # Captcha must NOT have been called — input validation happens first.
        patch_captcha.assert_not_called()


# ----------------------------------------------------------------------
# Error paths
# ----------------------------------------------------------------------
class TestErrorMapping:
    async def test_captcha_solve_failure(self, hass):
        with patch(
            "custom_components.ep_cube.config_flow.captcha_login",
            new=AsyncMock(side_effect=CaptchaSolveError("solver gave up")),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    CONF_USERNAME: "u@e.com",
                    CONF_PASSWORD: "x",
                    CONF_REGION: REGION_EU,
                },
            )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["errors"] == {"base": "captcha_failed"}

    async def test_invalid_credentials(self, hass):
        with patch(
            "custom_components.ep_cube.config_flow.captcha_login",
            new=AsyncMock(side_effect=LoginError("bad password")),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    CONF_USERNAME: "u@e.com",
                    CONF_PASSWORD: "wrong",
                    CONF_REGION: REGION_EU,
                },
            )
        assert result["errors"] == {"base": "invalid_auth"}

    async def test_no_devices_in_account(self, hass, patch_captcha):
        with patch(
            "custom_components.ep_cube.config_flow.EPCubeClient.get_device_list",
            new=AsyncMock(return_value=[]),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_USERNAME: "u@e.com", CONF_PASSWORD: "x", CONF_REGION: REGION_EU},
            )
        assert result["errors"] == {"base": "no_devices"}

    async def test_multiple_devices_without_hint_is_ambiguous(self, hass, patch_captcha):
        # Without a dev_id / sg_sn hint and >1 device, can't auto-pick.
        with patch(
            "custom_components.ep_cube.config_flow.EPCubeClient.get_device_list",
            new=AsyncMock(return_value=[
                {"id": "5613", "sgSn": "AAA", "systemCapacity": "20.0kWh"},
                {"id": "9999", "sgSn": "BBB", "systemCapacity": "10.0kWh"},
            ]),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_USERNAME: "u@e.com", CONF_PASSWORD: "x", CONF_REGION: REGION_EU},
            )
        assert result["errors"] == {"base": "multiple_devices"}

    async def test_dev_id_hint_disambiguates(self, hass, patch_captcha):
        with patch(
            "custom_components.ep_cube.config_flow.EPCubeClient.get_device_list",
            new=AsyncMock(return_value=[
                {"id": "5613", "sgSn": "AAA", "systemCapacity": "20.0kWh"},
                {"id": "9999", "sgSn": "BBB", "systemCapacity": "10.0kWh"},
            ]),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    CONF_USERNAME: "u@e.com", CONF_PASSWORD: "x", CONF_REGION: REGION_EU,
                    CONF_DEV_ID: "9999",
                },
            )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_DEV_ID] == "9999"
        assert result["data"][CONF_CAPACITY_KWH] == 10.0

    async def test_gen1_capacity_from_battery_serials(
        self, hass, patch_captcha, device_list_gen1
    ):
        with patch(
            "custom_components.ep_cube.config_flow.EPCubeClient.get_device_list",
            new=AsyncMock(return_value=device_list_gen1),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_USERNAME: "u@e.com", CONF_PASSWORD: "x", CONF_REGION: REGION_EU},
            )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_DEV_ID] == "1234"
        # 12 battery serials × 3.3 kWh
        assert result["data"][CONF_CAPACITY_KWH] == 39.6


# ----------------------------------------------------------------------
# Unique-id guard — don't double-add the same device.
# ----------------------------------------------------------------------
class TestUniqueId:
    async def test_duplicate_dev_id_aborts(
        self, hass, patch_captcha, patch_device_list, mock_config_entry
    ):
        existing = mock_config_entry()
        existing.add_to_hass(hass)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "u@e.com", CONF_PASSWORD: "x", CONF_REGION: REGION_EU},
        )
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "already_configured"


# ----------------------------------------------------------------------
# async_migrate_entry: v4 (pre-region) → v5 (region + api_prefix)
# ----------------------------------------------------------------------
class TestMigration:
    async def test_v4_eu_entry_infers_region(self, hass):
        from pytest_homeassistant_custom_component.common import MockConfigEntry
        from custom_components.ep_cube import async_migrate_entry

        entry = MockConfigEntry(
            domain=DOMAIN,
            version=4,
            data={
                CONF_BASE_URL: "https://monitoring-eu.epcube.com",
                CONF_USERNAME: "u@e.com",
                CONF_PASSWORD: "x",
                CONF_DEV_ID: "5613",
                CONF_SG_SN: "100100007001257120126",
                CONF_BEARER_TOKEN: "old-token",
                CONF_CAPACITY_KWH: 20.0,
            },
        )
        entry.add_to_hass(hass)
        assert await async_migrate_entry(hass, entry) is True
        assert entry.version == 5
        assert entry.data[CONF_REGION] == REGION_EU
        assert entry.data[CONF_API_PREFIX] == "/api"

    async def test_v4_us_entry_infers_app_api_prefix(self, hass):
        from pytest_homeassistant_custom_component.common import MockConfigEntry
        from custom_components.ep_cube import async_migrate_entry

        entry = MockConfigEntry(
            domain=DOMAIN,
            version=4,
            data={
                CONF_BASE_URL: "https://epcube-monitoring.com",
                CONF_USERNAME: "u@e.com",
                CONF_PASSWORD: "x",
                CONF_DEV_ID: "5613",
                CONF_SG_SN: "100100007001257120126",
                CONF_BEARER_TOKEN: "old-token",
                CONF_CAPACITY_KWH: 20.0,
            },
        )
        entry.add_to_hass(hass)
        await async_migrate_entry(hass, entry)
        assert entry.data[CONF_REGION] == REGION_US
        assert entry.data[CONF_API_PREFIX] == "/app-api"

    async def test_v4_unknown_host_falls_back_to_other(self, hass):
        from pytest_homeassistant_custom_component.common import MockConfigEntry
        from custom_components.ep_cube import async_migrate_entry

        entry = MockConfigEntry(
            domain=DOMAIN,
            version=4,
            data={
                CONF_BASE_URL: "https://mock.invalid",  # dev fork / mock server
                CONF_DEV_ID: "5613",
                CONF_SG_SN: "x",
            },
        )
        entry.add_to_hass(hass)
        await async_migrate_entry(hass, entry)
        assert entry.data[CONF_REGION] == REGION_OTHER
        assert entry.data[CONF_API_PREFIX] == "/api"

    async def test_v5_entry_passes_through_unchanged(self, hass, mock_config_entry):
        from custom_components.ep_cube import async_migrate_entry
        entry = mock_config_entry()
        entry.add_to_hass(hass)
        assert await async_migrate_entry(hass, entry) is True
        assert entry.version == 5


# ----------------------------------------------------------------------
# Options flow (Configure button): the force-export mode
# ----------------------------------------------------------------------
class TestOptionsFlow:
    async def test_form_defaults_to_the_legacy_tou_peak_mode(self, hass, mock_config_entry):
        entry = mock_config_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "init"
        defaults = {str(key): key.default() for key in result["data_schema"].schema}
        assert defaults[CONF_FORCE_EXPORT_MODE] == FORCE_EXPORT_MODE_TOU_PEAK

    async def test_choosing_dynamic_is_saved_on_the_entry(self, hass, mock_config_entry):
        entry = mock_config_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_FORCE_EXPORT_MODE: FORCE_EXPORT_MODE_DYNAMIC}
        )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_FORCE_EXPORT_MODE] == FORCE_EXPORT_MODE_DYNAMIC

    async def test_the_saved_mode_is_the_next_default(self, hass, mock_config_entry):
        entry = mock_config_entry()
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(
            entry, options={CONF_FORCE_EXPORT_MODE: FORCE_EXPORT_MODE_DYNAMIC}
        )
        result = await hass.config_entries.options.async_init(entry.entry_id)
        defaults = {str(key): key.default() for key in result["data_schema"].schema}
        assert defaults[CONF_FORCE_EXPORT_MODE] == FORCE_EXPORT_MODE_DYNAMIC

    async def test_an_unknown_mode_is_rejected(self, hass, mock_config_entry):
        entry = mock_config_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        with pytest.raises(data_entry_flow.InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_FORCE_EXPORT_MODE: "not-a-mode"}
            )


class TestChargeModeOption:
    async def test_form_offers_the_charge_mode_defaulting_to_tou(self, hass, mock_config_entry):
        from custom_components.ep_cube.const import (
            CONF_FORCE_CHARGE_MODE,
            FORCE_CHARGE_MODE_TOU_OFFPEAK,
        )

        entry = mock_config_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        defaults = {str(key): key.default() for key in result["data_schema"].schema}
        assert defaults[CONF_FORCE_CHARGE_MODE] == FORCE_CHARGE_MODE_TOU_OFFPEAK

    async def test_choosing_dynamic_charge_is_saved(self, hass, mock_config_entry):
        from custom_components.ep_cube.const import (
            CONF_FORCE_CHARGE_MODE,
            FORCE_CHARGE_MODE_DYNAMIC,
        )

        entry = mock_config_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_FORCE_EXPORT_MODE: FORCE_EXPORT_MODE_DYNAMIC,
             CONF_FORCE_CHARGE_MODE: FORCE_CHARGE_MODE_DYNAMIC},
        )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_FORCE_CHARGE_MODE] == FORCE_CHARGE_MODE_DYNAMIC
        assert entry.options[CONF_FORCE_EXPORT_MODE] == FORCE_EXPORT_MODE_DYNAMIC

    async def test_an_unknown_charge_mode_is_rejected(self, hass, mock_config_entry):
        from custom_components.ep_cube.const import CONF_FORCE_CHARGE_MODE

        entry = mock_config_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        with pytest.raises(data_entry_flow.InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_FORCE_CHARGE_MODE: "not-a-mode"}
            )


class TestTariffSelection:
    """Configure -> tick "Change the Dynamic tariff" -> country, region, supplier,
    tariffs, confirm -> saved through saveBinding."""

    @staticmethod
    async def _start(hass, entry):
        from custom_components.ep_cube.const import CONF_CHANGE_TARIFF

        result = await hass.config_entries.options.async_init(entry.entry_id)
        return await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_FORCE_EXPORT_MODE: FORCE_EXPORT_MODE_TOU_PEAK, CONF_CHANGE_TARIFF: True},
        )

    @pytest.fixture
    def live(self, hass, mock_config_entry, fake_client):
        """The entry with the fake client published where the flow looks for it."""
        from custom_components.ep_cube.const import DOMAIN

        entry = mock_config_entry()
        entry.add_to_hass(hass)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"client": fake_client}
        yield entry
        hass.data[DOMAIN].pop(entry.entry_id, None)

    async def test_the_checkbox_is_off_by_default_and_not_stored(self, hass, mock_config_entry):
        from custom_components.ep_cube.const import CONF_CHANGE_TARIFF

        entry = mock_config_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        defaults = {str(k): k.default() for k in result["data_schema"].schema}
        assert defaults[CONF_CHANGE_TARIFF] is False
        done = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_FORCE_EXPORT_MODE: FORCE_EXPORT_MODE_DYNAMIC}
        )
        assert done["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert CONF_CHANGE_TARIFF not in entry.options

    async def test_each_picker_defaults_to_the_saved_choice(self, hass, live):
        result = await self._start(hass, live)
        assert result["step_id"] == "tariff_country"
        assert {str(k): k.default() for k in result["data_schema"].schema} == {"supportCountryId": "237"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supportCountryId": "237"})
        assert result["step_id"] == "tariff_region"
        assert {str(k): k.default() for k in result["data_schema"].schema} == {"regionCode": "L"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"regionCode": "C"})
        assert result["step_id"] == "tariff_supplier"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"brandName": "OCTOPUS_ENERGY"})
        assert result["step_id"] == "tariff_products"
        defaults = {str(k): k.default() for k in result["data_schema"].schema}
        assert defaults == {"importCode": "AGILE-24-10-01", "exportCode": "AGILE-OUTGOING-19-05-13"}

    async def test_the_full_walk_saves_the_binding_and_the_modes(
        self, hass, live, fake_client, monkeypatch
    ):
        from custom_components.ep_cube import config_flow

        monkeypatch.setattr(config_flow, "TARIFF_VERIFY_DELAY_SECONDS", 0)
        fake_client.get_dynamic_relation.side_effect = [
            # read for the defaults, then (after the save) the cloud shows the new choice
            {"supportCountryId": "237", "regionCode": "L", "productBrand": "OCTOPUS_ENERGY",
             "productCode": "AGILE-24-10-01", "productCodeOut": "AGILE-OUTGOING-19-05-13"},
            {"supportCountryId": "237", "regionCode": "C", "productBrand": "OCTOPUS_ENERGY",
             "productCode": "AGILE-24-04-03", "productCodeOut": "OUTGOING-VAR-24-10-26"},
        ]
        result = await self._start(hass, live)
        flow_id = result["flow_id"]
        for user_input in (
            {"supportCountryId": "237"},
            {"regionCode": "C"},
            {"brandName": "OCTOPUS_ENERGY"},
            {"importCode": "AGILE-24-04-03", "exportCode": "OUTGOING-VAR-24-10-26"},
        ):
            result = await hass.config_entries.options.async_configure(flow_id, user_input)
        assert result["step_id"] == "tariff_confirm"
        assert result["description_placeholders"]["export_name"] == "Outgoing Octopus"
        assert result["description_placeholders"]["region"] == "London"

        result = await hass.config_entries.options.async_configure(flow_id, {})
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        binding = fake_client.save_dynamic_binding.await_args.args[0]
        assert binding["devId"] == "5613" and binding["brandId"] == "99"
        assert binding["productBrand"] == "OCTOPUS_ENERGY" and binding["regionCode"] == "C"
        assert binding["productCode"] == "AGILE-24-04-03"
        assert binding["productCodeOut"] == "OUTGOING-VAR-24-10-26"
        assert live.options[CONF_FORCE_EXPORT_MODE] == FORCE_EXPORT_MODE_TOU_PEAK

    async def _to_confirm(self, hass, live):
        result = await self._start(hass, live)
        flow_id = result["flow_id"]
        for user_input in (
            {"supportCountryId": "237"}, {"regionCode": "L"}, {"brandName": "OCTOPUS_ENERGY"},
            {"importCode": "AGILE-24-10-01", "exportCode": "OUTGOING-VAR-24-10-26"},
        ):
            result = await hass.config_entries.options.async_configure(flow_id, user_input)
        return flow_id, result

    async def test_a_failed_save_stays_on_the_confirm_step(self, hass, live, fake_client):
        from custom_components.ep_cube.api import EPCubeError

        flow_id, _ = await self._to_confirm(hass, live)
        fake_client.save_dynamic_binding.side_effect = EPCubeError("500 Failure")
        result = await hass.config_entries.options.async_configure(flow_id, {})
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "tariff_confirm" and result["errors"] == {"base": "save_failed"}

    async def test_a_save_the_cloud_never_shows_is_reported_not_confirmed(
        self, hass, live, fake_client, monkeypatch
    ):
        from custom_components.ep_cube import config_flow

        monkeypatch.setattr(config_flow, "TARIFF_VERIFY_DELAY_SECONDS", 0)
        flow_id, _ = await self._to_confirm(hass, live)      # getRelation keeps the OLD choice
        result = await hass.config_entries.options.async_configure(flow_id, {})
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["errors"] == {"base": "not_confirmed"}
        assert fake_client.get_dynamic_relation.await_count > config_flow.TARIFF_VERIFY_TRIES

    async def test_unreadable_lists_abort(self, hass, live, fake_client):
        from custom_components.ep_cube.api import EPCubeError

        fake_client.get_dynamic_countries.side_effect = EPCubeError("down")
        result = await self._start(hass, live)
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "tariff_unavailable"

    async def test_nothing_to_choose_for_the_region_aborts(self, hass, live, fake_client):
        flow_id, _ = None, None
        result = await self._start(hass, live)
        flow_id = result["flow_id"]
        fake_client.get_dynamic_regions.return_value = []
        result = await hass.config_entries.options.async_configure(
            flow_id, {"supportCountryId": "56"})
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "tariff_unsupported"

    async def test_no_products_for_the_supplier_aborts(self, hass, live, fake_client):
        result = await self._start(hass, live)
        flow_id = result["flow_id"]
        for user_input in ({"supportCountryId": "237"}, {"regionCode": "L"}):
            result = await hass.config_entries.options.async_configure(flow_id, user_input)
        fake_client.get_dynamic_products.side_effect = None
        fake_client.get_dynamic_products.return_value = []
        result = await hass.config_entries.options.async_configure(
            flow_id, {"brandName": "OCTOPUS_ENERGY"})
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "tariff_unsupported"

    async def test_without_a_loaded_entry_it_aborts(self, hass, mock_config_entry):
        from custom_components.ep_cube.const import CONF_CHANGE_TARIFF

        entry = mock_config_entry()
        entry.add_to_hass(hass)                              # not set up: no client published
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_CHANGE_TARIFF: True})
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "tariff_unavailable"
