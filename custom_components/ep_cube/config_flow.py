"""Config flow for EP Cube.

User picks their region (EU / US / JP) and enters EP Cube mobile-app
email + password. The flow runs the 4-POST captcha login (see captcha.py)
to obtain a Bearer token, then fetches the device list to resolve devId /
sgSn / capacity. Credentials are stored in the config entry; HA encrypts
the file at rest. The token is cached too but is treated as best-effort
— the api layer's reauth callback (see __init__.py) silently re-runs the
login on 403.

Region maps to a (base_url, api_prefix) pair from const.REGIONS. The US
region uses a different host AND path prefix ("/app-api" instead of
"/api"), so both are stored on the entry and threaded through api.py +
captcha.py. Picking "Other" lets advanced users supply a custom host +
prefix (mock servers, dev forks).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .api import (
    AuthError,
    EPCubeClient,
    EPCubeError,
    build_dynamic_binding,
    device_capacity_kwh,
)
from .captcha import CaptchaSolveError, LoginError, login as captcha_login
from .const import (
    CONF_API_PREFIX,
    CONF_BASE_URL,
    CONF_BEARER_TOKEN,
    CONF_CHANGE_TARIFF,
    PV_NAME_KEYS,
    PV_NAME_MAX_LENGTH,
    CONF_CAPACITY_KWH,
    CONF_DEV_ID,
    CONF_FORCE_CHARGE_MODE,
    CONF_FORCE_EXPORT_MODE,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_SG_SN,
    CONF_USERNAME,
    DEFAULT_API_PREFIX,
    DEFAULT_FORCE_CHARGE_MODE,
    DEFAULT_FORCE_EXPORT_MODE,
    DEFAULT_REGION,
    DOMAIN,
    FORCE_CHARGE_MODE_DYNAMIC,
    FORCE_CHARGE_MODE_TOU_OFFPEAK,
    FORCE_EXPORT_MODE_DYNAMIC,
    FORCE_EXPORT_MODE_TOU_PEAK,
    REGION_OTHER,
    REGIONS,
)

_LOGGER = logging.getLogger(__name__)

REGION_OPTIONS = [*REGIONS.keys(), REGION_OTHER]

# After saving a tariff the cloud takes a while to show it in getRelation; check a
# few times before saying it did not take.
TARIFF_VERIFY_TRIES = 6
TARIFF_VERIFY_DELAY_SECONDS = 5

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default=DEFAULT_REGION): vol.In(REGION_OPTIONS),
        vol.Optional(CONF_BASE_URL, default=""): str,
        vol.Optional(CONF_API_PREFIX, default=""): str,
        vol.Optional(CONF_DEV_ID, default=""): str,
        vol.Optional(CONF_SG_SN, default=""): str,
    }
)


class EPCubeOptionsFlow(OptionsFlow):
    """Integration options (Configure button on the integration card).

    1. How Predbat's "force export" and "force charge" windows are realised.
       `tou_peak` / `tou_offpeak` (defaults) use a time-of-use slot; `dynamic` uses the
       cube's Dynamic-tariff mode (see docs/ARCHITECTURE.md). The entry is reloaded when
       these change, so a new shim is built with the chosen modes.
    2. Optionally, the Dynamic tariff itself: tick "Change the Dynamic tariff" to pick
       country, region, supplier and import / export tariffs from the lists the cube's
       cloud offers, and save them there with `dynamicPrice/saveBinding` (the same
       setting as in the EP Cube app). Nothing about the mode, prices or schedule changes.
    """

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._relation: dict[str, Any] = {}
        self._choice: dict[str, Any] = {}
        self._names: dict[str, dict[str, str]] = {}
        self._brand_ids: dict[str, str] = {}

    # -- step 1: the modes ----------------------------------------------
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            data = dict(user_input)
            for key in PV_NAME_KEYS:
                data[key] = str(data.get(key) or "").strip()[:PV_NAME_MAX_LENGTH]
            change_tariff = bool(data.pop(CONF_CHANGE_TARIFF, False))
            self._options = data
            if change_tariff:
                return await self.async_step_tariff_country()
            return self.async_create_entry(data=data)

        current = self.config_entry.options.get(
            CONF_FORCE_EXPORT_MODE, DEFAULT_FORCE_EXPORT_MODE
        )
        current_charge = self.config_entry.options.get(
            CONF_FORCE_CHARGE_MODE, DEFAULT_FORCE_CHARGE_MODE
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_FORCE_EXPORT_MODE, default=current): SelectSelector(
                    SelectSelectorConfig(
                        options=[FORCE_EXPORT_MODE_TOU_PEAK, FORCE_EXPORT_MODE_DYNAMIC],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_FORCE_EXPORT_MODE,
                    )
                ),
                vol.Required(CONF_FORCE_CHARGE_MODE, default=current_charge): SelectSelector(
                    SelectSelectorConfig(
                        options=[FORCE_CHARGE_MODE_TOU_OFFPEAK, FORCE_CHARGE_MODE_DYNAMIC],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_FORCE_CHARGE_MODE,
                    )
                ),
                **{
                    vol.Optional(
                        key, default=str(self.config_entry.options.get(key) or "")
                    ): TextSelector(TextSelectorConfig())
                    for key in PV_NAME_KEYS
                },
                vol.Optional(CONF_CHANGE_TARIFF, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    # -- the Dynamic-tariff pickers -------------------------------------
    @property
    def _client(self) -> EPCubeClient | None:
        bucket = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        return bucket.get("client") if bucket else None

    @staticmethod
    def _picker(
        key: str, options: list[tuple[str, str]], default: str | None
    ) -> vol.Schema:
        values = [value for value, _ in options]
        chosen = default if default in values else (values[0] if values else None)
        return vol.Schema(
            {
                vol.Required(key, default=chosen): SelectSelector(
                    SelectSelectorConfig(
                        options=[SelectOptionDict(value=v, label=label) for v, label in options],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )

    async def async_step_tariff_country(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        client = self._client
        if user_input is not None:
            self._choice["country_id"] = user_input["supportCountryId"]
            return await self.async_step_tariff_region()
        if client is None:
            return self.async_abort(reason="tariff_unavailable")
        try:
            self._relation = await client.get_dynamic_relation()
            countries = await client.get_dynamic_countries()
        except EPCubeError as err:
            _LOGGER.warning("options flow: could not read the Dynamic-tariff lists: %s", err)
            return self.async_abort(reason="tariff_unavailable")
        if not countries:
            return self.async_abort(reason="tariff_unsupported")
        self._names["country"] = {
            str(c["supportCountryId"]): c.get("supportCountryName", "") for c in countries
        }
        default = str(self._relation.get("supportCountryId") or "")
        if not default:
            default = next(
                (str(c["supportCountryId"]) for c in countries if c.get("currentCountry")), ""
            )
        return self.async_show_form(
            step_id="tariff_country",
            data_schema=self._picker(
                "supportCountryId",
                [(k, v) for k, v in self._names["country"].items()],
                default,
            ),
        )

    async def async_step_tariff_region(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        client = self._client
        if user_input is not None:
            self._choice["region_code"] = user_input["regionCode"]
            return await self.async_step_tariff_supplier()
        try:
            regions = await client.get_dynamic_regions(self._choice["country_id"])
        except EPCubeError as err:
            _LOGGER.warning("options flow: could not read the regions: %s", err)
            return self.async_abort(reason="tariff_unavailable")
        if not regions:
            return self.async_abort(reason="tariff_unsupported")
        self._names["region"] = {r["regionCode"]: r.get("regionName", r["regionCode"]) for r in regions}
        return self.async_show_form(
            step_id="tariff_region",
            data_schema=self._picker(
                "regionCode",
                list(self._names["region"].items()),
                str(self._relation.get("regionCode") or ""),
            ),
        )

    async def async_step_tariff_supplier(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        client = self._client
        if user_input is not None:
            self._choice["brand_name"] = user_input["brandName"]
            self._choice["brand_id"] = self._brand_ids[user_input["brandName"]]
            return await self.async_step_tariff_products()
        try:
            brands = await client.get_dynamic_brands(
                self._choice["country_id"], self._choice["region_code"]
            )
        except EPCubeError as err:
            _LOGGER.warning("options flow: could not read the suppliers: %s", err)
            return self.async_abort(reason="tariff_unavailable")
        if not brands:
            return self.async_abort(reason="tariff_unsupported")
        self._brand_ids = {b["brandName"]: str(b["id"]) for b in brands}
        return self.async_show_form(
            step_id="tariff_supplier",
            data_schema=self._picker(
                "brandName",
                [(name, name.replace("_", " ").title()) for name in self._brand_ids],
                str(self._relation.get("productBrand") or ""),
            ),
        )

    async def async_step_tariff_products(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        client = self._client
        if user_input is not None:
            self._choice["import_code"] = user_input["importCode"]
            self._choice["export_code"] = user_input["exportCode"]
            return await self.async_step_tariff_confirm()
        try:
            imports = await client.get_dynamic_products(
                self._choice["brand_name"], self._choice["region_code"], 0
            )
            exports = await client.get_dynamic_products(
                self._choice["brand_name"], self._choice["region_code"], 1
            )
        except EPCubeError as err:
            _LOGGER.warning("options flow: could not read the tariffs: %s", err)
            return self.async_abort(reason="tariff_unavailable")
        if not imports or not exports:
            return self.async_abort(reason="tariff_unsupported")
        self._names["import"] = {p["productCode"]: p.get("productFullName") or p["productName"] for p in imports}
        self._names["export"] = {p["productCode"]: p.get("productFullName") or p["productName"] for p in exports}
        schema = vol.Schema(
            {
                **self._picker(
                    "importCode", list(self._names["import"].items()),
                    str(self._relation.get("productCode") or ""),
                ).schema,
                **self._picker(
                    "exportCode", list(self._names["export"].items()),
                    str(self._relation.get("productCodeOut") or ""),
                ).schema,
            }
        )
        return self.async_show_form(step_id="tariff_products", data_schema=schema)

    async def async_step_tariff_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        client = self._client
        choice = self._choice
        placeholders = {
            "import_name": self._names["import"].get(choice["import_code"], choice["import_code"]),
            "export_name": self._names["export"].get(choice["export_code"], choice["export_code"]),
            "supplier": choice["brand_name"].replace("_", " ").title(),
            "region": self._names["region"].get(choice["region_code"], choice["region_code"]),
            "country": self._names["country"].get(choice["country_id"], choice["country_id"]),
        }
        errors: dict[str, str] = {}
        if user_input is not None:
            binding = build_dynamic_binding(
                client.dev_id,
                brand_id=choice["brand_id"],
                brand_name=choice["brand_name"],
                country_id=choice["country_id"],
                region_code=choice["region_code"],
                import_code=choice["import_code"],
                export_code=choice["export_code"],
            )
            try:
                await client.save_dynamic_binding(binding)
            except EPCubeError as err:
                _LOGGER.warning("options flow: saving the tariff failed: %s", err)
                errors["base"] = "save_failed"
            else:
                if await self._saved(client, binding):
                    return self.async_create_entry(data=self._options)
                errors["base"] = "not_confirmed"
        return self.async_show_form(
            step_id="tariff_confirm",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
            errors=errors,
        )

    @staticmethod
    async def _saved(client: EPCubeClient, binding: dict[str, Any]) -> bool:
        """Poll getRelation until it shows the choice (the cloud lags by up to a minute)."""
        wanted = (
            binding["supportCountryId"], binding["regionCode"],
            binding["productCode"], binding["productCodeOut"],
        )
        for attempt in range(TARIFF_VERIFY_TRIES):
            try:
                rel = await client.get_dynamic_relation()
            except EPCubeError:
                rel = {}
            got = (
                str(rel.get("supportCountryId")), rel.get("regionCode") or rel.get("region"),
                rel.get("productCode"), rel.get("productCodeOut"),
            )
            if got == wanted:
                return True
            if attempt < TARIFF_VERIFY_TRIES - 1:
                await asyncio.sleep(TARIFF_VERIFY_DELAY_SECONDS)
        return False


class EPCubeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 5

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EPCubeOptionsFlow:
        """Expose the Configure button."""
        return EPCubeOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            region = user_input[CONF_REGION]
            if region == REGION_OTHER:
                base_url = (user_input.get(CONF_BASE_URL) or "").strip()
                api_prefix = (user_input.get(CONF_API_PREFIX) or "").strip() or DEFAULT_API_PREFIX
                if not base_url:
                    errors["base"] = "base_url_required"
            else:
                base_url = REGIONS[region]["base_url"]
                api_prefix = REGIONS[region]["api_prefix"]

            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            dev_id_hint = user_input.get(CONF_DEV_ID) or ""
            sg_sn_hint = user_input.get(CONF_SG_SN) or ""
            session = async_get_clientsession(self.hass)

            bearer_token: str | None = None
            if not errors:
                try:
                    bearer_token = await captcha_login(
                        session,
                        base_url=base_url,
                        username=username,
                        password=password,
                        api_prefix=api_prefix,
                    )
                except CaptchaSolveError as err:
                    _LOGGER.warning("config_flow captcha solve failed: %s", err)
                    errors["base"] = "captcha_failed"
                except LoginError as err:
                    _LOGGER.warning("config_flow login rejected: %s", err)
                    errors["base"] = "invalid_auth"
                except aiohttp.ClientError as err:
                    _LOGGER.warning("config_flow network error: %s", err)
                    errors["base"] = "cannot_connect"

            if bearer_token:
                client = EPCubeClient(
                    session=session,
                    base_url=base_url,
                    api_prefix=api_prefix,
                    dev_id=dev_id_hint or "0",
                    sg_sn=sg_sn_hint or "0",
                    bearer_token=bearer_token,
                )
                try:
                    devices = await client.get_device_list()
                except AuthError as err:
                    _LOGGER.warning("config_flow fresh-token rejected on deviceList: %s", err)
                    errors["base"] = "invalid_auth"
                except aiohttp.ClientError as err:
                    _LOGGER.warning("config_flow network error: %s", err)
                    errors["base"] = "cannot_connect"
                except EPCubeError as err:
                    _LOGGER.warning("config_flow unknown error: %s", err)
                    errors["base"] = "unknown"
                else:
                    if not devices:
                        errors["base"] = "no_devices"
                    else:
                        chosen = _pick_device(devices, dev_id_hint, sg_sn_hint)
                        if chosen is None:
                            errors["base"] = "multiple_devices"
                        else:
                            dev_id = str(chosen.get("devId") or chosen.get("id"))
                            sg_sn = str(chosen.get("sgSn"))
                            capacity, _ = device_capacity_kwh(chosen)

                            await self.async_set_unique_id(dev_id)
                            self._abort_if_unique_id_configured()

                            entry_data = {
                                CONF_REGION: region,
                                CONF_BASE_URL: base_url,
                                CONF_API_PREFIX: api_prefix,
                                CONF_USERNAME: username,
                                CONF_PASSWORD: password,
                                CONF_BEARER_TOKEN: bearer_token,
                                CONF_DEV_ID: dev_id,
                                CONF_SG_SN: sg_sn,
                                CONF_CAPACITY_KWH: capacity,
                            }
                            return self.async_create_entry(
                                title=f"EP Cube ({dev_id})",
                                data=entry_data,
                            )

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
        )


def _pick_device(
    devices: list[dict[str, Any]],
    dev_id_hint: str,
    sg_sn_hint: str,
) -> dict[str, Any] | None:
    """Resolve which device entry to use. Returns None if ambiguous."""
    if dev_id_hint:
        for d in devices:
            if str(d.get("devId") or d.get("id")) == dev_id_hint:
                return d
        return None
    if sg_sn_hint:
        for d in devices:
            if str(d.get("sgSn")) == sg_sn_hint:
                return d
        return None
    if len(devices) == 1:
        return devices[0]
    return None
