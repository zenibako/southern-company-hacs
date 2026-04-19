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
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_TARIFFS
from .const import DEFAULT_TARIFF_NAME
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class AccountData:
    """Per-account data surfaced to sensors."""

    monthly: southern_company_api.account.MonthlyUsage
    cumulative_kwh: float
    cumulative_cost: float


def match_tariff(tariffs: list[dict], when: datetime.datetime) -> str:
    """Return the first matching tariff name for ``when``, else the default."""
    if not tariffs:
        return DEFAULT_TARIFF_NAME
    weekday = when.weekday()
    hour = when.hour
    for tariff in tariffs:
        days = tariff.get("days", [])
        start_hour = tariff.get("start_hour", 0)
        end_hour = tariff.get("end_hour", 24)
        name = tariff.get("name")
        if name and weekday in days and start_hour <= hour < end_hour:
            return name
    return DEFAULT_TARIFF_NAME


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
                    _LOGGER.debug("Updating sensor data for %s", account.number)
                    monthly_by_account[account.number] = await account.get_month_data(
                        await self._southern_company_connection.jwt
                    )
                # Note: insert statistics can be somewhat slow on first setup.
                await self._insert_statistics()
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

    async def _insert_statistics(self) -> None:
        """Insert Southern Company statistics."""
        if await self._southern_company_connection.jwt is None:
            raise UpdateFailed("Jwt is None")
        tariffs = self._get_tariffs()
        for account in await self._southern_company_connection.accounts:
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
                    tariff_name = match_tariff(tariffs, data.time)
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
                    tariff_cost_sums[tariff_name] += data.cost
                    tariff_usage_sums[tariff_name] += data.usage
                    tariff_cost_stats[tariff_name].append(
                        StatisticData(
                            start=from_time,
                            state=data.cost,
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
                name=f"Southern Company {account.name} cost",
                source=DOMAIN,
                statistic_id=cost_statistic_id,
                unit_of_measurement=None,
            )
            usage_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"Southern Company {account.name} usage",
                source=DOMAIN,
                statistic_id=usage_statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )

            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)
            async_add_external_statistics(self.hass, usage_metadata, usage_statistics)

            for tariff_name, stats in tariff_cost_stats.items():
                async_add_external_statistics(
                    self.hass,
                    StatisticMetaData(
                        has_mean=False,
                        has_sum=True,
                        name=f"Southern Company {account.name} cost ({tariff_name})",
                        source=DOMAIN,
                        statistic_id=f"{DOMAIN}:energy_cost_{tariff_name}_{account.number}",
                        unit_of_measurement=None,
                    ),
                    stats,
                )
            for tariff_name, stats in tariff_usage_stats.items():
                async_add_external_statistics(
                    self.hass,
                    StatisticMetaData(
                        has_mean=False,
                        has_sum=True,
                        name=f"Southern Company {account.name} usage ({tariff_name})",
                        source=DOMAIN,
                        statistic_id=f"{DOMAIN}:energy_usage_{tariff_name}_{account.number}",
                        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                    ),
                    stats,
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
