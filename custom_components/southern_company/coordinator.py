"""Coordinator to handle southern Company connections."""

import asyncio
import dataclasses
import datetime
from datetime import timedelta
import json
import logging
import os

import southern_company_api
from southern_company_api.exceptions import SouthernCompanyException

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticMeanType,
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_HOLIDAY_CALENDAR
from .const import CONF_TARIFFS
from .const import DOMAIN

_SUM_CACHE_FILE = "southern_company_sums.json"

_LOGGER = logging.getLogger(__name__)


def _load_sum_cache_sync(hass: HomeAssistant) -> dict[str, dict[str, float]]:
    path = hass.config.path(_SUM_CACHE_FILE)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sum_cache_sync(hass: HomeAssistant, cache: dict[str, dict[str, float]]) -> None:
    path = hass.config.path(_SUM_CACHE_FILE)
    try:
        with open(path, "w") as f:
            json.dump(cache, f)
    except Exception:
        _LOGGER.warning("Failed to save sum cache to %s", path)


async def _is_holiday(
    hass: HomeAssistant,
    calendar_entity: str | None,
    when: datetime.datetime,
    _cache: dict[str, tuple[datetime.date, bool]],
) -> bool:
    """Return True if the given date is a holiday according to the calendar entity."""
    if not calendar_entity:
        return False
    date = when.date()
    cached = _cache.get(calendar_entity)
    if cached and cached[0] == date:
        return cached[1]
    try:
        start = datetime.datetime.combine(date, datetime.time.min, tzinfo=when.tzinfo)
        end = datetime.datetime.combine(
            date + timedelta(days=1), datetime.time.min, tzinfo=when.tzinfo
        )
        component = hass.data.get("calendar")
        if component is None:
            return False
        calendar = component.get_entity(calendar_entity)
        if calendar is None:
            return False
        events = await calendar.async_get_events(hass, start, end)
        result = len(events) > 0
    except Exception:
        _LOGGER.debug("Could not check holiday calendar %s", calendar_entity)
        result = False
    _cache[calendar_entity] = (date, result)
    return result


_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class AccountData:
    """Per-account data surfaced to sensors."""

    monthly: southern_company_api.account.MonthlyUsage
    cumulative_kwh: float
    cumulative_cost: float


async def match_tariff(
    tariffs: list[dict],
    when: datetime.datetime,
    hass: HomeAssistant | None = None,
    holiday_calendar: str | None = None,
    holiday_cache: dict[str, tuple[datetime.date, bool]] | None = None,
) -> str:
    """Return the first matching tariff name for ``when``, else 'default'."""
    if not tariffs:
        return "default"
    weekday = when.weekday()
    hour = when.hour
    month = when.month  # 1-12
    is_hol = False
    if hass and holiday_calendar and holiday_cache:
        is_hol = await _is_holiday(hass, holiday_calendar, when, holiday_cache or {})
    for tariff in tariffs:
        days = tariff.get("days", [])
        start_hour = tariff.get("start_hour", 0)
        end_hour = tariff.get("end_hour", 24)
        months = tariff.get("months")
        name = tariff.get("name")
        if name and weekday in days and start_hour <= hour < end_hour:
            if months is None or month in months:
                if tariff.get("skip_on_holidays") and is_hol:
                    continue
                return name
    return "default"


def tariff_rate(tariffs: list[dict], tariff_name: str) -> float | None:
    """Return the rate ($/kWh) for a tariff name, or None if not set."""
    for tariff in tariffs:
        if tariff.get("name") == tariff_name:
            rate = tariff.get("rate")
            if isinstance(rate, (int, float)) and rate >= 0:
                return float(rate)
    return None


class SouthernCompanyCoordinator(DataUpdateCoordinator):
    """Handle Southern company data and insert statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        southern_company_connection: southern_company_api.SouthernCompanyAPI,
        entry: ConfigEntry | None = None,
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name="Southern Company",
            update_interval=timedelta(minutes=60),
        )
        self._southern_company_connection = southern_company_connection
        self._entry = entry
        self._usage_sum_by_account: dict[str, float] = {}
        self._cost_sum_by_account: dict[str, float] = {}
        self._sum_cache: dict[str, dict[str, float]] = {}

    @property
    def api(self) -> southern_company_api.SouthernCompanyAPI:
        """Access the api."""
        return self._southern_company_connection

    async def _async_update_data(self) -> dict[str, AccountData]:
        """Update data via API."""
        if not self._sum_cache:
            self._sum_cache = await get_instance(self.hass).async_add_executor_job(
                _load_sum_cache_sync, self.hass
            )
        try:
            if await self._southern_company_connection.jwt is not None:
                monthly_by_account: dict[
                    str, southern_company_api.account.MonthlyUsage
                ] = {}
                for account in await self._southern_company_connection.accounts:
                    if not account.service_point_number:
                        _LOGGER.warning(
                            "Skipping account %s: no service point number",
                            account.number,
                        )
                        continue
                    _LOGGER.debug("Updating sensor data for %s", account.number)
                    monthly_by_account[account.number] = await account.get_month_data(
                        await self._southern_company_connection.jwt
                    )
                # Note: insert statistics can be somewhat slow on first setup.
                await self._insert_statistics(monthly_by_account)
                return {
                    number: AccountData(
                        monthly=monthly,
                        cumulative_kwh=self._usage_sum_by_account.get(number, 0.0),
                        cumulative_cost=self._cost_sum_by_account.get(number, 0.0),
                    )
                    for number, monthly in monthly_by_account.items()
                }
        except SouthernCompanyException as ex:
            raise UpdateFailed("Failed updating jwt token") from ex

        raise UpdateFailed("No jwt token")

    def _get_tariffs(self) -> list[dict]:
        """Return configured tariff windows, or an empty list."""
        if self._entry is None:
            return []
        return list(self._entry.options.get(CONF_TARIFFS, []))

    async def _insert_statistics(
        self,
        monthly_by_account: dict[str, southern_company_api.account.MonthlyUsage],
    ) -> None:
        """Insert Southern Company statistics.

        Strategy:
        - On first run: backfill 365 days of data.
        - On incremental runs: fetch 31 days, seed cumulative sums from
          the last DB row (via ``get_last_statistics``), then insert new
          rows whose timestamps exceed that last row.
        - If seeding drifts more than 5% from the monthly total, fall back
          to re-computing from month start to eliminate cumulative errors.
        """
        if await self._southern_company_connection.jwt is None:
            raise UpdateFailed("Jwt is None")
        tariffs = self._get_tariffs()
        holiday_calendar = (
            self._entry.options.get(CONF_HOLIDAY_CALENDAR, "") if self._entry else ""
        ) or None
        holiday_cache: dict[str, tuple[datetime.date, bool]] = {}
        for account in await self._southern_company_connection.accounts:
            if not account.service_point_number:
                continue
            _LOGGER.debug("Updating Statistics for %s", account.number)
            cost_statistic_id = f"{DOMAIN}:energy_cost_{account.number}_v2"
            usage_statistic_id = f"{DOMAIN}:energy_usage_{account.number}_v2"

            # Robustly check whether this entity already has DB rows.
            # During HA startup ``get_last_statistics`` can transiently return
            # empty, which would cause a false first_run and overwrite all
            # existing rows with sums computed from 0.  We retry a few times
            # and also request the last 100 rows instead of 1 to improve
            # the chance the query finds something.
            last_usage_stats: dict | None = None
            last_cost_stats: dict | None = None
            for attempt in range(5):
                if attempt > 0:
                    await asyncio.sleep(0.5)
                last_usage_stats = await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics, self.hass, 100, usage_statistic_id, True, set()
                )
                last_cost_stats = await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics, self.hass, 100, cost_statistic_id, True, set()
                )
                usage_has = (
                    last_usage_stats
                    and usage_statistic_id in last_usage_stats
                    and len(last_usage_stats[usage_statistic_id]) > 0
                )
                cost_has = (
                    last_cost_stats
                    and cost_statistic_id in last_cost_stats
                    and len(last_cost_stats[cost_statistic_id]) > 0
                )
                # If EITHER series already has rows, the entity exists.
                if usage_has or cost_has:
                    break

            first_run = not (
                last_usage_stats
                and usage_statistic_id in last_usage_stats
                and len(last_usage_stats[usage_statistic_id]) > 0
                and last_cost_stats
                and cost_statistic_id in last_cost_stats
                and len(last_cost_stats[cost_statistic_id]) > 0
            )
            cached = self._sum_cache.get(account.number, {})
            cached_usage_sum = cached.get("usage")
            cached_cost_sum = cached.get("cost")

            # Query the DB directly for the true historical max sum, since
            # get_last_statistics only returns the latest rows which may be
            # corrupt after a restart that re-seeded from 0.
            max_db_usage_sum = 0.0
            max_db_cost_sum = 0.0
            try:
                max_db_usage_sum, max_db_cost_sum = await get_instance(
                    self.hass
                ).async_add_executor_job(self._get_max_sums, usage_statistic_id, cost_statistic_id)
            except Exception:
                _LOGGER.debug("Could not query max sums from DB")

            if first_run:
                _LOGGER.info(
                    "Updating statistic for the first time, this may take a while"
                )
                hourly_data = await account.get_hourly_data(
                    datetime.datetime.now(datetime.timezone.utc) - timedelta(days=365),
                    datetime.datetime.now(datetime.timezone.utc),
                    await self._southern_company_connection.jwt,
                )
                hourly_data = [d for d in hourly_data if d.time is not None]
                hourly_data.sort(key=lambda d: d.time)
                _LOGGER.info(
                    "First-run sort check: first=%s last=%s count=%d",
                    hourly_data[0].time if hourly_data else None,
                    hourly_data[-1].time if hourly_data else None,
                    len(hourly_data),
                )
                _usage_sum = 0.0
                _cost_sum = 0.0
                last_stats_time = None
            else:
                usage_last = last_usage_stats[usage_statistic_id][0]
                cost_last = last_cost_stats[cost_statistic_id][0]
                last_usage_sum = usage_last.get("sum", 0.0) or 0.0
                last_cost_sum = cost_last.get("sum", 0.0) or 0.0

                # Detect restart corruption: after HA restart, the
                # statistics are sometimes re-seeded from 0, so the
                # latest sum can be far below the historical maximum.
                # Find the true historical peak across all sources.
                best_seed_usage = last_usage_sum
                best_seed_cost = last_cost_sum

                # Source 1: max from get_last_statistics rows.
                all_usage_rows = last_usage_stats.get(usage_statistic_id, [])
                stats_max_usage = max(
                    (r.get("sum") or 0.0) for r in all_usage_rows
                ) if all_usage_rows else last_usage_sum
                all_cost_rows = last_cost_stats.get(cost_statistic_id, [])
                stats_max_cost = max(
                    (r.get("sum") or 0.0) for r in all_cost_rows
                ) if all_cost_rows else last_cost_sum
                best_seed_usage = max(best_seed_usage, stats_max_usage)
                best_seed_cost = max(best_seed_cost, stats_max_cost)

                # Source 2: direct DB query for the true max.
                best_seed_usage = max(best_seed_usage, max_db_usage_sum)
                best_seed_cost = max(best_seed_cost, max_db_cost_sum)

                # Source 3: persistent cache.
                if cached_usage_sum is not None and cached_usage_sum > 0:
                    best_seed_usage = max(best_seed_usage, cached_usage_sum)
                if cached_cost_sum is not None and cached_cost_sum > 0:
                    best_seed_cost = max(best_seed_cost, cached_cost_sum)

                # If the latest sum is far below the best seed, the
                # data was re-seeded from 0 after a restart.
                if best_seed_usage > 0 and last_usage_sum < best_seed_usage * 0.5:
                    _LOGGER.warning(
                        "DB sum looks corrupt for %s "
                        "(latest=%.2f, best_seed=%.2f, "
                        "stats_max=%.2f, db_max=%.2f, cache=%.2f); "
                        "falling back to best_seed",
                        account.number,
                        last_usage_sum,
                        best_seed_usage,
                        stats_max_usage,
                        max_db_usage_sum,
                        cached_usage_sum or 0.0,
                    )
                    last_usage_sum = best_seed_usage
                    last_cost_sum = best_seed_cost

                # Find the last row with actual (non-zero) data rather than
                # using the absolute latest row, which may be an extrapolation
                # placeholder set to a future time.  Using the placeholder's
                # timestamp would cause all real data to be filtered out.
                all_usage_rows_for_time = last_usage_stats.get(usage_statistic_id, [])
                last_real_row = None
                for row in all_usage_rows_for_time:
                    state_val = row.get("state", 0.0) or 0.0
                    if state_val > 0:
                        raw_start = row.get("start")
                        if raw_start:
                            last_real_row = row
                if last_real_row is not None:
                    raw_start = last_real_row.get("start")
                    last_stats_time = (
                        raw_start.timestamp()
                        if isinstance(raw_start, datetime.datetime)
                        else float(raw_start)
                    )
                elif (raw_start := usage_last.get("start")):
                    last_stats_time = (
                        raw_start.timestamp()
                        if isinstance(raw_start, datetime.datetime)
                        else float(raw_start)
                    )
                else:
                    last_stats_time = None

                hourly_data = await account.get_hourly_data(
                    datetime.datetime.now(datetime.timezone.utc) - timedelta(days=31),
                    datetime.datetime.now(datetime.timezone.utc),
                    await self._southern_company_connection.jwt,
                )
                hourly_data = [d for d in hourly_data if d.time is not None]
                hourly_data.sort(key=lambda d: d.time)
                _LOGGER.info(
                    "Incremental sort check: first=%s last=%s count=%d seed=%.2f last_ts=%s",
                    hourly_data[0].time if hourly_data else None,
                    hourly_data[-1].time if hourly_data else None,
                    len(hourly_data),
                    last_usage_sum,
                    datetime.datetime.fromtimestamp(last_stats_time, tz=datetime.timezone.utc).isoformat() if last_stats_time else None,
                )
                _usage_sum = last_usage_sum
                _cost_sum = last_cost_sum

            # Per-tariff running sums, seeded lazily from the most recent row.
            tariff_cost_sums: dict[str, float] = {}
            tariff_usage_sums: dict[str, float] = {}
            tariff_cost_stats: dict[str, list[StatisticData]] = {}
            tariff_usage_stats: dict[str, list[StatisticData]] = {}

            cost_statistics: list[StatisticData] = []
            usage_statistics: list[StatisticData] = []

            for data in hourly_data:
                if not isinstance(data.cost, (int, float)) or not isinstance(
                    data.usage, (int, float)
                ):
                    continue
                # Reject impossibly large single-hour spikes (corrupt data),
                # but preserve legitimate small negative values (e.g. solar).
                if abs(data.usage) > 200 or abs(data.cost) > 200:
                    _LOGGER.warning(
                        "Skipping spike for %s at %s: usage=%.2f cost=%.2f",
                        account.number,
                        data.time,
                        data.usage,
                        data.cost,
                    )
                    continue
                from_time = data.time
                if from_time is None or (
                    last_stats_time is not None
                    and from_time.timestamp() <= last_stats_time
                ):
                    continue
                from_time = from_time.replace(minute=0, second=0, microsecond=0)
                _cost_sum += data.cost
                _usage_sum += data.usage

                cost_statistics.append(
                    StatisticData(start=from_time, state=data.cost, sum=_cost_sum)
                )
                usage_statistics.append(
                    StatisticData(start=from_time, state=data.usage, sum=_usage_sum)
                )
                _LOGGER.info(
                    "Inserted %s: %s usage=%.2f sum=%.2f",
                    account.number,
                    from_time.isoformat(),
                    data.usage,
                    _usage_sum,
                )

                if tariffs:
                    tariff_name = await match_tariff(
                        tariffs, data.time, self.hass, holiday_calendar, holiday_cache
                    )
                    rate = tariff_rate(tariffs, tariff_name)
                    if tariff_name not in tariff_cost_sums:
                        tariff_cost_sums[tariff_name] = await self._seed_tariff_sum(
                            f"{DOMAIN}:energy_cost_{tariff_name}_{account.number}",
                            last_stats_time,
                        )
                        tariff_usage_sums[tariff_name] = await self._seed_tariff_sum(
                            f"{DOMAIN}:energy_usage_{tariff_name}_{account.number}",
                            last_stats_time,
                        )
                        tariff_cost_stats[tariff_name] = []
                        tariff_usage_stats[tariff_name] = []
                    # Use the actual API cost if available, otherwise
                    # compute from rate * kWh.
                    tariff_cost = data.cost
                    if rate is not None:
                        tariff_cost = round(data.usage * rate, 4)
                    tariff_cost_sums[tariff_name] += tariff_cost
                    tariff_usage_sums[tariff_name] += data.usage
                    tariff_cost_stats[tariff_name].append(
                        StatisticData(
                            start=from_time,
                            state=tariff_cost,
                            sum=tariff_cost_sums[tariff_name],
                        )
                    )
                    tariff_usage_stats[tariff_name].append(
                        StatisticData(
                            start=from_time,
                            state=data.usage,
                            sum=tariff_usage_sums[tariff_name],
                        )
                    )

            cost_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Southern Company {account.name} cost (v2)",
                source=DOMAIN,
                statistic_id=cost_statistic_id,
                unit_of_measurement=None,
                unit_class=None,
            )
            usage_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Southern Company {account.name} usage (v2)",
                source=DOMAIN,
                statistic_id=usage_statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                unit_class="energy",
            )

            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)
            async_add_external_statistics(self.hass, usage_metadata, usage_statistics)

            for tariff_name, stats in tariff_cost_stats.items():
                async_add_external_statistics(
                    self.hass,
                    StatisticMetaData(
                        has_mean=False,
                        has_sum=True,
                        mean_type=StatisticMeanType.NONE,
                        name=f"Southern Company {account.name} cost ({tariff_name})",
                        source=DOMAIN,
                        statistic_id=f"{DOMAIN}:energy_cost_{tariff_name}_{account.number}",
                        unit_of_measurement=None,
                        unit_class=None,
                    ),
                    stats,
                )
            for tariff_name, stats in tariff_usage_stats.items():
                async_add_external_statistics(
                    self.hass,
                    StatisticMetaData(
                        has_mean=False,
                        has_sum=True,
                        mean_type=StatisticMeanType.NONE,
                        name=f"Southern Company {account.name} usage ({tariff_name})",
                        source=DOMAIN,
                        statistic_id=f"{DOMAIN}:energy_usage_{tariff_name}_{account.number}",
                        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                        unit_class="energy",
                    ),
                    stats,
                )

            # Extrapolate estimated stats for the lag gap (typically ~48h).
            monthly = monthly_by_account.get(account.number)
            if monthly is not None and cost_statistics and usage_statistics:
                last_entry = usage_statistics[-1]
                last_actual = (
                    last_entry["start"]
                    if isinstance(last_entry, dict)
                    else last_entry.start
                )
                await self._extrapolate_gap(
                    account,
                    monthly,
                    _usage_sum,
                    _cost_sum,
                    usage_statistic_id,
                    cost_statistic_id,
                    last_actual,
                )

            # Commit the account's cumulative sums only after a successful loop.
            self._cost_sum_by_account[account.number] = _cost_sum
            self._usage_sum_by_account[account.number] = _usage_sum
            # Only update cache if new sum makes sense (monotonically increasing).
            old_cache = self._sum_cache.get(account.number, {})
            old_usage = old_cache.get("usage", 0.0)
            old_cost = old_cache.get("cost", 0.0)
            if _usage_sum >= old_usage * 0.9 or old_usage == 0:
                self._sum_cache[account.number] = {
                    "usage": _usage_sum,
                    "cost": _cost_sum,
                }
                await get_instance(self.hass).async_add_executor_job(
                    _save_sum_cache_sync, self.hass, self._sum_cache
                )
            else:
                _LOGGER.warning(
                    "Skipping cache update for %s: new sum %.2f < cached %.2f "
                    "(possible corrupt run)",
                    account.number,
                    _usage_sum,
                    old_usage,
                )

    def _get_max_sums(self, usage_statistic_id: str, cost_statistic_id: str) -> tuple[float, float]:
        """Query the recorder DB directly for the maximum cumulative sum.

        This bypasses the in-memory statistics cache to find the true
        historical peak, needed to detect restart-seeded-from-zero corruption.
        """
        import sqlite3

        db_path = self.hass.config.path("home-assistant_v2.db")
        max_usage = 0.0
        max_cost = 0.0
        try:
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            for stat_id, label in [
                (usage_statistic_id, "usage"),
                (cost_statistic_id, "cost"),
            ]:
                c.execute(
                    "SELECT s.sum FROM statistics s "
                    "JOIN statistics_meta m ON s.metadata_id = m.id "
                    "WHERE m.statistic_id = ? AND s.sum IS NOT NULL "
                    "ORDER BY s.sum DESC LIMIT 1",
                    (stat_id,),
                )
                row = c.fetchone()
                val = row[0] if row else 0.0
                if label == "usage":
                    max_usage = max(0.0, val)
                else:
                    max_cost = max(0.0, val)
            conn.close()
        except Exception:
            pass
        return max_usage, max_cost

    async def _seed_tariff_sum(
        self, statistic_id: str, last_stats_time: float | None
    ) -> float:
        """Seed a tariff cumulative sum from the DB, aligned with ``last_stats_time``.

        Returns the cumulative sum at or before ``last_stats_time`` so the
        running total stays consistent with the main (non-tariff) sum's
        starting point. On first ever write (no prior rows), returns 0.
        """
        if last_stats_time is None:
            return 0.0
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, set()
        )
        if not last_stats or statistic_id not in last_stats:
            return 0.0
        return last_stats[statistic_id][0].get("sum") or 0.0

    async def _extrapolate_gap(
        self,
        account,
        monthly: southern_company_api.account.MonthlyUsage,
        last_usage_sum: float,
        last_cost_sum: float,
        usage_statistic_id: str,
        cost_statistic_id: str,
        last_actual: datetime.datetime,
    ) -> None:
        """Insert estimated statistics for the lag between last hourly data and now.

        Southern Company hourly data lags ~48 hours. To keep the Energy Dashboard
        from showing blank for recent days, we spread the difference between the
        monthly total and the last known hourly sum evenly across the gap hours.

        When real data arrives, it overwrites these estimates.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        last_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        last_actual = last_actual.replace(minute=0, second=0, microsecond=0)

        # Compute the gap in full hours.
        gap_hours = int((last_hour - last_actual).total_seconds() / 3600)
        # Cap gap to 72 hours to avoid massive extrapolation if the
        # clock jumps or the DB is stale.
        if gap_hours <= 0 or gap_hours > 72:
            _LOGGER.debug(
                "Gap extrapolation skipped for %s (%d hours)",
                account.number,
                gap_hours,
            )
            return

        # Get monthly totals.
        monthly_usage = (
            monthly.total_kwh_used
            if isinstance(monthly.total_kwh_used, (int, float))
            else 0.0
        )
        monthly_cost = (
            monthly.dollars_to_date
            if isinstance(monthly.dollars_to_date, (int, float))
            else 0.0
        )

        usage_gap = max(monthly_usage - last_usage_sum, 0.0)
        cost_gap = max(monthly_cost - last_cost_sum, 0.0)

        est_usage_per_hour = usage_gap / gap_hours if gap_hours else 0.0
        est_cost_per_hour = cost_gap / gap_hours if gap_hours else 0.0

        est_usage_stats: list[StatisticData] = []
        est_cost_stats: list[StatisticData] = []
        run_usage_sum = last_usage_sum
        run_cost_sum = last_cost_sum

        for i in range(1, gap_hours + 1):
            ts = last_actual + timedelta(hours=i)
            ts = ts.replace(minute=0, second=0, microsecond=0)

            # Cap extrapolation to not exceed monthly total
            if run_usage_sum >= monthly_usage:
                # distribute remaining cost gap across remaining hours
                est_usage_per_hour = 0.0
            else:
                # we are still below the monthly total, add estimated usage
                # but cap the current step so we don't overshoot the total
                est_usage_per_hour = min(
                    est_usage_per_hour, monthly_usage - run_usage_sum
                )

            if run_cost_sum >= monthly_cost:
                est_cost_per_hour = 0.0
            else:
                est_cost_per_hour = min(est_cost_per_hour, monthly_cost - run_cost_sum)

            run_usage_sum += est_usage_per_hour
            run_cost_sum += est_cost_per_hour
            est_usage_stats.append(
                StatisticData(start=ts, state=est_usage_per_hour, sum=run_usage_sum)
            )
            est_cost_stats.append(
                StatisticData(start=ts, state=est_cost_per_hour, sum=run_cost_sum)
            )

        if est_usage_stats:
            _LOGGER.info(
                "Extrapolating %d estimated hours for account %s "
                "(%.2f kWh/hr, $%.2f/hr, usage_gap=%.2f, cost_gap=%.2f)",
                gap_hours,
                account.number,
                est_usage_per_hour,
                est_cost_per_hour,
                usage_gap,
                cost_gap,
            )
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_mean=False,
                    has_sum=True,
                    mean_type=StatisticMeanType.NONE,
                    name=f"Southern Company {account.name} cost (estimated)",
                    source=DOMAIN,
                    statistic_id=cost_statistic_id,
                    unit_of_measurement=None,
                    unit_class=None,
                ),
                est_cost_stats,
            )
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_mean=False,
                    has_sum=True,
                    mean_type=StatisticMeanType.NONE,
                    name=f"Southern Company {account.name} usage (estimated)",
                    source=DOMAIN,
                    statistic_id=usage_statistic_id,
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                    unit_class="energy",
                ),
                est_usage_stats,
            )
