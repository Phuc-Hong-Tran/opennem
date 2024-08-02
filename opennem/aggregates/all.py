"""Runs all aggregates"""

from opennem.aggregates.facility_daily import run_aggregate_facility_all_by_year, run_aggregate_facility_days
from opennem.aggregates.network_demand import run_aggregates_demand_network, run_aggregates_demand_network_days
from opennem.aggregates.network_flows import run_emission_update_day, run_flow_updates_all_for_network
from opennem.api.export.tasks import export_all_daily, export_all_monthly, export_energy, export_power
from opennem.schema.network import NetworkAEMORooftop, NetworkAPVI, NetworkNEM, NetworkWEM
from opennem.workers.energy import run_energy_update_all


def run_aggregates_all_days(days: int = 1) -> None:
    """Runs all aggregates for all networks back to days"""
    for network in [NetworkNEM, NetworkWEM, NetworkAPVI, NetworkAEMORooftop]:
        run_aggregate_facility_days(days=days, network=network)

    run_aggregates_demand_network_days(days=days)
    run_emission_update_day(days=days)


async def run_aggregates_all() -> None:
    """Run every aggregate for every network"""
    for network in [NetworkNEM]:
        run_energy_update_all(network=network)

    for network in [NetworkNEM, NetworkWEM, NetworkAPVI, NetworkAEMORooftop]:
        run_aggregate_facility_all_by_year(network=network)

        if network.has_interconnectors:
            run_flow_updates_all_for_network(network=network)

    run_aggregates_demand_network()

    # run the exports for all
    await export_power(latest=False)
    await export_energy(latest=False)

    await export_all_daily()
    await export_all_monthly()
