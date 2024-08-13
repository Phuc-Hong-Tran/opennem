"""
RecordReactor engine
"""

import logging
from datetime import datetime, timedelta

from opennem import settings
from opennem.recordreactor.buckets import get_period_start_end, is_end_of_period
from opennem.recordreactor.processors.demand import run_price_demand_milestone_for_interval
from opennem.recordreactor.processors.generation import run_generation_energy_emissions_milestones
from opennem.recordreactor.schema import MilestoneMetric, MilestonePeriod
from opennem.schema.network import NetworkNEM, NetworkSchema, NetworkWEM, NetworkWEMDE
from opennem.utils.dates import get_last_completed_interval_for_network

logger = logging.getLogger("opennem.recordreactor.engine")

# Engine configs
_DEFAULT_NUM_TASKS = 14

_DEFAULT_METRICS = [
    MilestoneMetric.demand,
    MilestoneMetric.price,
    MilestoneMetric.power,
    MilestoneMetric.energy,
    MilestoneMetric.emissions,
]

_DEFAULT_BUCKET_SIZES = [
    MilestonePeriod.interval,
    MilestonePeriod.day,
    MilestonePeriod.week,
    MilestonePeriod.month,
    MilestonePeriod.year,
]

_DEFAULT_NETWORKS = [NetworkNEM, NetworkWEM, NetworkWEMDE]


async def run_milestone_engine(
    start_interval: datetime,
    end_interval: datetime | None = None,
    metrics: list[MilestoneMetric] | None = None,
    networks: list[NetworkSchema] | None = None,
    periods: list[MilestonePeriod] | None = None,
    num_tasks: int | None = None,
):
    if not num_tasks:
        num_tasks = _DEFAULT_NUM_TASKS

    if not metrics:
        metrics = _DEFAULT_METRICS

    if not networks:
        networks = _DEFAULT_NETWORKS

    if not periods:
        periods = _DEFAULT_BUCKET_SIZES

    for network in networks:
        if not network.interval_size:
            logger.info(f"Skipping {network.code} as it has no interval size")
            continue

        if not network.data_first_seen:
            logger.info(f"Skipping {network.code} as it has no data first seen")
            continue

        logger.info(f"Processing price and demand milestone data network {network.code}")

        if not end_interval:
            end_interval = get_last_completed_interval_for_network(network)
            logger.info(f"Set end interval for {network.code} to {end_interval}")

        current_interval = start_interval
        tasks = []

        while current_interval <= end_interval:
            # don't process the network data if it is after the last data seen
            if network.data_last_seen and current_interval > network.data_last_seen.replace(tzinfo=None):
                logger.info(f"Breaking at {current_interval} as it is after the last data seen for {network.code}")
                break

            # don't process the network data if it is before the first data seen
            if current_interval < network.data_first_seen.replace(tzinfo=None):
                logger.info(f"Skipping {current_interval} as it is before the first data seen for {network.code}")
                current_interval += timedelta(minutes=network.interval_size)
                continue

            for bucket_size in periods:
                if not is_end_of_period(current_interval, bucket_size):
                    continue

                period_start, period_end = get_period_start_end(dt=current_interval, bucket_size=bucket_size, network=network)
                logger.debug(f"Processing {current_interval} {bucket_size} {period_start} to {period_end} for {network.code}")

                if settings.dry_run:
                    continue

                if MilestoneMetric.demand in metrics or MilestoneMetric.price in metrics:
                    task = run_price_demand_milestone_for_interval(
                        network=network,
                        bucket_size=bucket_size,
                        period_start=period_start,
                        period_end=period_end,
                    )
                    tasks.append(task)

                if MilestoneMetric.power in metrics or MilestoneMetric.energy in metrics or MilestoneMetric.emissions in metrics:
                    task = run_generation_energy_emissions_milestones(
                        network=network,
                        bucket_size=bucket_size,
                        period_start=period_start,
                        period_end=period_end,
                    )
                    tasks.append(task)

            # Move to the next interval
            current_interval += timedelta(minutes=network.interval_size)

            if len(tasks) >= num_tasks:
                await asyncio.gather(*tasks)
                tasks = []

        # Process any remaining tasks
        if tasks:
            await asyncio.gather(*tasks)


# debug entry point
if __name__ == "__main__":
    import asyncio

    start_interval = datetime.fromisoformat("2020-01-01 00:00:00")
    end_interval = datetime.fromisoformat("2024-08-07 00:00:00")
    asyncio.run(
        run_milestone_engine(
            start_interval=start_interval,
            end_interval=end_interval,
        )
    )
