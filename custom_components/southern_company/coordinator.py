"""Coordinator to handle southern Company connections."""

import dataclasses
import datetime
from datetime import timedelta
import logging

import southern_company_api
from southern_company_api.exceptions import SouthernCompanyException

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticMeanType,
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_HOLIDAY_CALENDAR
from .const import CONF_TARIFFS
from .const import DOMAIN


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

    @property
    def api(self) -> southern_company_api.SouthernCompanyAPI:
        """Access the api."""
        return self._southern_company_connection

    async def _async_update_data(self) -> dict[str, AccountData]:
        """Update data via API."""
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
        """Insert Southern Company statistics."""
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
            cost_statistic_id = f"{DOMAIN}:energy_cost_{account.number}"
            usage_statistic_id = f"{DOMAIN}:energy_usage_{account.number}"

            last_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, usage_statistic_id, True, set()
            )
            if not last_stats:
                # First time we insert 1 year of data (if available)
                _LOGGER.info(
                    "Updating statistic for the first time, this may take a while"
                )
                hourly_data = await account.get_hourly_data(
                    datetime.datetime.now() - timedelta(days=365),
                    datetime.datetime.now(),
                    await self._southern_company_connection.jwt,
                )
                _cost_sum = 0.0
                _usage_sum = 0.0
                last_stats_time = None
            else:
                # Fetch the last 31 days and overwrite any revisions.
                hourly_data = await account.get_hourly_data(
                    datetime.datetime.now() - timedelta(days=31),
                    datetime.datetime.now(),
                    await self._southern_company_connection.jwt,
                )

                from_time = hourly_data[0].time
                start = from_time - timedelta(hours=1)
                cost_stat = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start,
                    None,
                    [cost_statistic_id],
                    "hour",
                    None,
                    {"sum"},
                )
                if cost_statistic_id not in cost_stat:
                    _LOGGER.warning(
                        "Missing cost statistic window; re-backfilling one year"
                    )
                    hourly_data = await account.get_hourly_data(
                        datetime.datetime.now() - timedelta(days=365),
                        datetime.datetime.now(),
                        await self._southern_company_connection.jwt,
                    )
                    _cost_sum = 0.0
                    _usage_sum = 0.0
                    last_stats_time = None
                else:
                    _cost_sum = cost_stat[cost_statistic_id][0]["sum"] or 0.0
                    _raw_start = cost_stat[cost_statistic_id][0]["start"]
                    last_stats_time = (
                        _raw_start.timestamp()
                        if isinstance(_raw_start, datetime.datetime)
                        else float(_raw_start)
                    )
                    usage_stat = await get_instance(self.hass).async_add_executor_job(
                        statistics_during_period,
                        self.hass,
                        start,
                        None,
                        [usage_statistic_id],
                        "hour",
                        None,
                        {"sum"},
                    )
                    _usage_sum = usage_stat[usage_statistic_id][0]["sum"] or 0.0

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
                name=f"Southern Company {account.name} cost",
                source=DOMAIN,
                statistic_id=cost_statistic_id,
                unit_of_measurement=None,
                unit_class=None,
            )
            usage_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Southern Company {account.name} usage",
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
                await self._extrapolate_gap(
                    account,
                    monthly,
                    _usage_sum,
                    _cost_sum,
                    usage_statistic_id,
                    cost_statistic_id,
                )

            # Commit the account's cumulative sums only after a successful loop.
            self._cost_sum_by_account[account.number] = _cost_sum
            self._usage_sum_by_account[account.number] = _usage_sum

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
        ts = (
            last_stats_time
            if isinstance(last_stats_time, float)
            else last_stats_time.timestamp()
        )
        start = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        stat = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - timedelta(hours=1),
            start + timedelta(hours=1),
            [statistic_id],
            "hour",
            None,
            {"sum"},
        )
        if statistic_id not in stat or not stat[statistic_id]:
            return 0.0
        return stat[statistic_id][0]["sum"] or 0.0

    async def _extrapolate_gap(
        self,
        account,
        monthly: southern_company_api.account.MonthlyUsage,
        last_usage_sum: float,
        last_cost_sum: float,
        usage_statistic_id: str,
        cost_statistic_id: str,
    ) -> None:
        """Insert estimated statistics for the lag between last hourly data and now.

        Southern Company hourly data lags ~48 hours. To keep the Energy Dashboard
        from showing blank for recent days, we spread the difference between the
        monthly total and the last known hourly sum evenly across the gap hours.

        When real data arrives, it overwrites these estimates.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        last_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)

        # Find the latest actual statistic timestamp.
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, usage_statistic_id, True, set()
        )
        if not last_stats:
            return
        last_row = last_stats[usage_statistic_id][0]
        raw_start = last_row["start"]
        last_actual_ts = (
            raw_start.timestamp()
            if isinstance(raw_start, datetime.datetime)
            else float(raw_start)
        )
        last_actual = datetime.datetime.fromtimestamp(
            last_actual_ts, tz=datetime.timezone.utc
        )

        # Compute the gap in full hours.
        gap_hours = int((last_hour - last_actual).total_seconds() / 3600)
        if gap_hours <= 0:
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
            _LOGGER.debug(
                "Extrapolating %d estimated hours for account %s "
                "(%.2f kWh/hr, $%.2f/hr)",
                gap_hours,
                account.number,
                est_usage_per_hour,
                est_cost_per_hour,
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
