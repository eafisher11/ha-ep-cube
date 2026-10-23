"""Data update coordinator for EP Cube."""
from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DeviceStatus,
    EPCubeClient,
    DeviceDetails,
    EPCubeError,
    FirmwareInfo,
    NoDynamicPricesError,
    RateLimitError,
    ServerError,
    WarrantyInfo,
)
from .const import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PRICE_POLL_INTERVAL_SECONDS,
    DEFAULT_STATS_POLL_INTERVAL_SECONDS,
    DOMAIN,
    STATS_SCOPE_ANNUAL,
    STATS_SCOPE_DAILY,
    STATS_SCOPE_MONTHLY,
    STATS_SCOPE_TOTAL,
)
from .repairs import evaluate_predbat_priority_issue

_LOGGER = logging.getLogger(__name__)

# Errors worth riding out for a few polls: cloud 5xx, rate limiting, and
# network / timeout problems. Auth and other 4xx errors still fail at once.
_TRANSIENT_ERRORS = (ServerError, RateLimitError, aiohttp.ClientError, asyncio.TimeoutError)
# Consecutive failed polls (at the 60 s cadence, about 3 minutes) for which the
# last good data is kept before the entities go unavailable.
MAX_TRANSIENT_FAILURES = 3

# The battery charged/discharged-today totals are kept by the client in memory, so
# they are saved here to survive a restart. Saves are throttled; Home Assistant
# also writes any pending save when it stops.
BATTERY_FLOW_STORE_VERSION = 1
BATTERY_FLOW_SAVE_DELAY_SECONDS = 60

# The warranty dates change at most once in a blue moon, so ask for them rarely.
WARRANTY_REFRESH = timedelta(hours=24)
# If the recorder isn't ready (or has no history for the discharged-today sensor),
# try to seed the lifetime total this many times, one per poll, then give up.
LIFETIME_SEED_ATTEMPTS = 10


def battery_flow_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, BATTERY_FLOW_STORE_VERSION, f"{DOMAIN}.battery_flow.{entry_id}")


class EPCubeCoordinator(DataUpdateCoordinator[DeviceStatus]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: EPCubeClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=DEFAULT_POLL_INTERVAL_SECONDS),
        )
        self.client = client
        self._entry_id = entry.entry_id
        # Cached raw getSwitchMode response. Used by the v1.0 Predbat-priority
        # repair flow to detect user-painted (non-shim) TOU slots that
        # conflict with Predbat's control of the cube. Best-effort: a failed
        # schedule fetch doesn't fail the whole poll (homeDeviceInfo is the
        # critical path); we keep the previous snapshot so a transient
        # network blip doesn't flip the repair flow on and off.
        self._switch_mode: dict[str, Any] | None = None
        # Latest per-string PV readings (getSolarPvPower), best-effort like
        # the schedule: a failed fetch keeps the previous values.
        self._pv_strings: dict[int, dict[str, float]] = {}
        # Consecutive transient poll failures (reset on any success).
        self._transient_failures = 0
        self._battery_flow_store = battery_flow_store(hass, entry.entry_id)
        # Warranty dates (getWarranty), refreshed daily and best-effort: a failed
        # fetch keeps the previous value.
        self._warranty: WarrantyInfo | None = None
        self._warranty_fetched: datetime | None = None
        # Firmware versions (queryFirmwareInfo), same cadence and rules.
        self._firmware: FirmwareInfo | None = None
        self._firmware_fetched: datetime | None = None
        # Model and serial numbers (userDeviceInfo), same cadence and rules.
        self._details: DeviceDetails | None = None
        self._details_fetched: datetime | None = None
        # System capacity from deviceList (systemCapacity, or Gen 1 battery
        # serials × 3.3 kWh). Refreshed daily; a failed fetch retries next poll.
        self._capacity_fetched: datetime | None = None
        self._lifetime_seed_attempts = 0

    def _battery_flow_data(self) -> dict:
        return self.client.battery_flow_state()

    async def async_restore_battery_flow(self) -> None:
        """Load the saved charged/discharged totals before the first poll."""
        saved = await self._battery_flow_store.async_load()
        if saved and self.client.restore_battery_flow(saved):
            _LOGGER.debug("restored the battery charged/discharged totals for today")

    @property
    def warranty(self) -> WarrantyInfo | None:
        """The cube's warranty dates, or None until the cloud has returned them."""
        return self._warranty

    @property
    def details(self) -> DeviceDetails | None:
        """The cube's model and serial numbers, or None until the cloud returns them."""
        return self._details

    async def _async_refresh_details(self) -> None:
        now = dt_util.utcnow()
        if self._details_fetched is not None and now - self._details_fetched < WARRANTY_REFRESH:
            return
        self._details_fetched = now
        try:
            details = await self.client.get_device_details()
        except EPCubeError as err:
            _LOGGER.debug("get_device_details failed (keeping cached): %s", err)
            return
        if details is not None:
            self._details = details

    def _async_update_device_registry(self) -> None:
        """Show the model, serial number and firmware on the device page."""
        registry = dr.async_get(self.hass)
        # Look the device up through our own config entry: HA deprecated finding a
        # device by identifiers alone, since they are no longer unique across entries.
        device = next(
            (
                d
                for d in dr.async_entries_for_config_entry(registry, self._entry_id)
                if (DOMAIN, self._entry_id) in d.identifiers
            ),
            None,
        )
        if device is None:
            return
        changes: dict[str, str] = {}
        if self._details is not None:
            if self._details.model and device.model != self._details.model:
                changes["model"] = self._details.model
            if self._details.serial_number and device.serial_number != self._details.serial_number:
                changes["serial_number"] = self._details.serial_number
        if self._firmware is not None:
            version = self._firmware.firmware_version
            if version and device.sw_version != version:
                changes["sw_version"] = version
        if changes:
            registry.async_update_device(device.id, **changes)

    @property
    def firmware(self) -> FirmwareInfo | None:
        """The cube's firmware versions, or None until the cloud has returned them."""
        return self._firmware

    async def _async_refresh_capacity(self) -> None:
        now = dt_util.utcnow()
        if self._capacity_fetched is not None and now - self._capacity_fetched < WARRANTY_REFRESH:
            return
        if await self.client.refresh_capacity():
            self._capacity_fetched = now

    async def _async_refresh_firmware(self) -> None:
        now = dt_util.utcnow()
        if self._firmware_fetched is not None and now - self._firmware_fetched < WARRANTY_REFRESH:
            return
        self._firmware_fetched = now
        try:
            firmware = await self.client.get_firmware_info()
        except EPCubeError as err:
            _LOGGER.debug("get_firmware_info failed (keeping cached): %s", err)
            return
        if firmware is not None:
            self._firmware = firmware

    async def _async_refresh_warranty(self) -> None:
        now = dt_util.utcnow()
        if self._warranty_fetched is not None and now - self._warranty_fetched < WARRANTY_REFRESH:
            return
        self._warranty_fetched = now
        try:
            warranty = await self.client.get_warranty()
        except EPCubeError as err:
            _LOGGER.debug("get_warranty failed (keeping cached): %s", err)
            return
        if warranty is not None:
            self._warranty = warranty

    async def _async_seed_lifetime(self) -> None:
        """Start the lifetime discharged total from the recorder's history of the
        discharged-today sensor, once, when nothing was saved. Best-effort: the
        recorder may not be ready yet, so a few polls may retry."""
        if self.client.lifetime_known or self._lifetime_seed_attempts >= LIFETIME_SEED_ATTEMPTS:
            return
        self._lifetime_seed_attempts += 1
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import get_last_statistics
            from homeassistant.helpers import entity_registry as er

            entity_id = er.async_get(self.hass).async_get_entity_id(
                "sensor", DOMAIN, f"{self._entry_id}_battery_discharge_today"
            )
            if entity_id is None:
                return
            stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, entity_id, True, {"sum"}
            )
            rows = stats.get(entity_id) or []
            total = rows[0].get("sum") if rows else None
        except Exception as err:  # noqa: BLE001 - recorder state is outside our control
            _LOGGER.debug("could not seed the lifetime discharged total (%s)", err)
            return
        if total is not None and self.client.seed_battery_discharge_lifetime(total):
            _LOGGER.info("seeded the lifetime discharged total from history: %.0f kWh", total)

    @property
    def pv_strings(self) -> dict[int, dict[str, float]]:
        """Latest per-string PV readings, {} until the first success."""
        return self._pv_strings

    @property
    def switch_mode(self) -> dict[str, Any] | None:
        """Latest getSwitchMode snapshot, or None if it's never succeeded."""
        return self._switch_mode

    async def _async_update_data(self) -> DeviceStatus:
        # Seed the lifetime discharged total first, so the very first status
        # already carries it (otherwise the lifetime and cycles sensors read 0
        # until the second poll).
        await self._async_seed_lifetime()
        # Before get_status, so the first status already has the capacity and
        # a Gen 1 capacity is known before batteryPackNum is considered.
        await self._async_refresh_capacity()
        try:
            status = await self.client.get_status()
        except _TRANSIENT_ERRORS as err:
            # A cloud 5xx / rate-limit / network blip. Keep the last good data
            # for a few polls rather than flipping every entity to unavailable
            # (a single 502 did that for a minute at 03:52 on 2026-10-16).
            self._transient_failures += 1
            if self.data is not None and self._transient_failures <= MAX_TRANSIENT_FAILURES:
                _LOGGER.warning(
                    "EP Cube cloud error, keeping last data (%d/%d): %s",
                    self._transient_failures,
                    MAX_TRANSIENT_FAILURES,
                    err,
                )
                return self.data
            raise UpdateFailed(str(err)) from err
        except EPCubeError as err:
            raise UpdateFailed(str(err)) from err
        self._transient_failures = 0
        self._battery_flow_store.async_delay_save(
            self._battery_flow_data, BATTERY_FLOW_SAVE_DELAY_SECONDS
        )

        # Schedule fetch is non-critical — log + keep previous cache on failure.
        try:
            self._switch_mode = await self.client.get_switch_mode()
        except EPCubeError as err:
            _LOGGER.debug("get_switch_mode failed (keeping cached): %s", err)

        try:
            self._pv_strings = await self.client.get_pv_strings()
        except EPCubeError as err:
            _LOGGER.debug("get_pv_strings failed (keeping cached): %s", err)

        await self._async_refresh_warranty()
        await self._async_refresh_firmware()
        await self._async_refresh_details()
        self._async_update_device_registry()

        # Raise / clear the Predbat-priority repair issue based on the
        # latest snapshot. Idempotent — safe to call every tick.
        evaluate_predbat_priority_issue(self.hass, self._entry_id, self._switch_mode)

        return status


# ----------------------------------------------------------------------
# Stats coordinator (Phase 4.2)
# ----------------------------------------------------------------------
# Tick cadence per bucket. The wider windows change much more slowly than
# `today`, so refreshing them every tick would burn cloud calls for no
# user-visible benefit. `yesterday` is special — once frozen, it never
# changes within the day, so we re-fetch only when the HA-local date rolls.
_MONTH_REFRESH = timedelta(hours=1)
_YEAR_REFRESH = timedelta(hours=6)
_TOTAL_REFRESH = timedelta(hours=12)


def _bucket_dates(now: datetime) -> dict[str, str]:
    """Format the queryDateStr values for each bucket from a single timestamp.

    Daily / yesterday use YYYY-MM-DD, monthly YYYY-MM, annual + total YYYY.
    All anchored to HA-local time — the cube's own rollups are presumed
    local-clock-based (it's a homeowner appliance, not a UTC server).
    """
    today = now.date()
    yesterday = today - timedelta(days=1)
    return {
        "today": today.strftime("%Y-%m-%d"),
        "yesterday": yesterday.strftime("%Y-%m-%d"),
        "month": today.strftime("%Y-%m"),
        "year": today.strftime("%Y"),
        "total": today.strftime("%Y"),
    }


class EPCubeStatsCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator for queryDataElectricityV2 rollups.

    Polls today every tick (5 min) and the wider buckets at slower cadences
    to keep the cloud-call budget reasonable. `yesterday` is fetched once on
    startup and once per HA-local date change.

    State shape: a dict keyed by bucket name (`today` / `yesterday` /
    `month` / `year` / `total`), each holding the lowercased fields from
    the cube. Sensors plug in via a `value_fn` that takes the whole dict
    and pulls the right key.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: EPCubeClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_stats_{entry.entry_id}",
            update_interval=timedelta(seconds=DEFAULT_STATS_POLL_INTERVAL_SECONDS),
        )
        self.client = client
        # Per-bucket last-fetch timestamps. Empty on first refresh so each
        # bucket fetches once at startup; subsequent ticks consult these.
        self._last_fetched: dict[str, datetime] = {}
        # Date the cached `yesterday` bucket covers, so we refetch only when
        # the HA-local date rolls. Set after the first successful fetch.
        self._yesterday_date: str | None = None
        # Working state — populated incrementally by each tick. Returned to
        # the coordinator's `data` attribute on every refresh; missing buckets
        # default to {} so sensors can `.get(bucket, {}).get(field)` cleanly.
        self._state: dict[str, dict[str, Any]] = {
            "today": {},
            "yesterday": {},
            "month": {},
            "year": {},
            "total": {},
        }

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        now = dt_util.now()
        dates = _bucket_dates(now)

        # Decide which buckets need a refresh this tick. Always refresh
        # `today`; the rest follow their cadence.
        to_fetch: list[tuple[str, int, str]] = [
            ("today", STATS_SCOPE_DAILY, dates["today"]),
        ]
        if self._yesterday_date != dates["yesterday"]:
            to_fetch.append(("yesterday", STATS_SCOPE_DAILY, dates["yesterday"]))
        if self._is_stale("month", now, _MONTH_REFRESH):
            to_fetch.append(("month", STATS_SCOPE_MONTHLY, dates["month"]))
        if self._is_stale("year", now, _YEAR_REFRESH):
            to_fetch.append(("year", STATS_SCOPE_ANNUAL, dates["year"]))
        if self._is_stale("total", now, _TOTAL_REFRESH):
            to_fetch.append(("total", STATS_SCOPE_TOTAL, dates["total"]))

        # Sequential — concurrent would 4-5× the burst rate against the
        # cube and there's no latency benefit to the user (this is a
        # background coordinator, not a request path).
        for bucket, scope, date_str in to_fetch:
            try:
                result = await self.client.get_stats(scope, date_str)
            except EPCubeError as err:
                # `today` failing is the only critical case — without it the
                # primary sensors go stale. Raise so HA flags the integration.
                # Other buckets are best-effort: keep the prior cached value.
                if bucket == "today":
                    raise UpdateFailed(f"stats today fetch failed: {err}") from err
                _LOGGER.debug("stats %s fetch failed (keeping cached): %s", bucket, err)
                continue
            self._state[bucket] = result
            self._last_fetched[bucket] = now
            if bucket == "yesterday":
                self._yesterday_date = dates["yesterday"]

        return self._state

    def _is_stale(self, bucket: str, now: datetime, max_age: timedelta) -> bool:
        last = self._last_fetched.get(bucket)
        return last is None or (now - last) >= max_age


# ----------------------------------------------------------------------
# Dynamic-tariff prices
# ----------------------------------------------------------------------
# The cloud publishes the next day's prices late in the afternoon (Agile:
# about 16:00 UK time), so ask for tomorrow only after this hour.
_TOMORROW_FROM_HOUR = 15


def _to_aware(naive: str) -> datetime | None:
    """Parse the cloud's UK-local "YYYY-MM-DD HH:MM:SS" into an aware datetime."""
    parsed = dt_util.parse_datetime(naive)
    if parsed is None:
        return None
    return dt_util.as_utc(parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))


class EPCubePriceCoordinator(DataUpdateCoordinator[dict[str, list[dict[str, Any]]]]):
    """Half-hourly import and export prices used by the cube's Dynamic mode.

    State: {"import": [...], "export": [...]}; each slot is
    {"start": aware datetime, "end": aware datetime, "price": float p/kWh},
    sorted by start. Covers today, plus tomorrow once it is published.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: EPCubeClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_prices_{entry.entry_id}",
            update_interval=timedelta(seconds=DEFAULT_PRICE_POLL_INTERVAL_SECONDS),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, list[dict[str, Any]]]:
        now = dt_util.now()
        try:
            # Always ask for the local date explicitly: with no date the cloud
            # answers for its own "today", which is the UTC day, so just after
            # local midnight in summer time it returns yesterday's prices.
            raw = await self.client.get_dynamic_prices(now.strftime("%Y-%m-%d"))
        except NoDynamicPricesError as err:
            # No Dynamic tariff is set up for this cube: that is a normal state, not a
            # failure, so keep the price sensors empty and say nothing above debug level.
            _LOGGER.debug("no Dynamic-tariff prices for this cube: %s", err)
            return {"import": [], "export": []}
        except EPCubeError as err:
            raise UpdateFailed(f"dynamic prices fetch failed: {err}") from err

        raws = [raw]
        if now.hour >= _TOMORROW_FROM_HOUR:
            tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                raws.append(await self.client.get_dynamic_prices(tomorrow))
            except EPCubeError as err:
                _LOGGER.debug("tomorrow's dynamic prices not available: %s", err)

        out: dict[str, list[dict[str, Any]]] = {"import": [], "export": []}
        for part in raws:
            for side in out:
                for slot in part.get(side, []):
                    start, end = _to_aware(slot["start"]), _to_aware(slot["end"])
                    if start is None or end is None:
                        continue
                    out[side].append({"start": start, "end": end, "price": slot["price"]})
        for side in out:
            out[side].sort(key=lambda s: s["start"])
        return out
