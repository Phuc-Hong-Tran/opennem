"""OpenNEM Network Flows

Creates an aggregate table with network flows (imports/exports), emissions and market_value


Changelog

* 2-MAR - fix blank values and cleanup

"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from opennem.db import get_database_engine
from opennem.db.models.opennem import AggregateNetworkFlows
from opennem.pipelines.bulk_insert import build_insert_query
from opennem.pipelines.csv import generate_csv_from_records
from opennem.schema.network import NetworkNEM, NetworkSchema
from opennem.utils.dates import get_last_complete_day_for_network

logger = logging.getLogger("opennem.workers.emission_flows")


class EmissionsWorkerException(Exception):
    pass


def load_interconnector_intervals(date_start: datetime, date_end: datetime) -> pd.DataFrame:
    """Load interconnectors for a date range"""
    engine = get_database_engine()

    query = """
        select
            fs.trading_interval at time zone 'AEST' as trading_interval,
            coalesce(fs.generated, 0) as generated,
            f.interconnector_region_to,
            f.interconnector_region_from
        from facility_scada fs
        left join facility f on f.code = fs.facility_code
        where
            f.interconnector is True
            and f.network_id IN ('NEM')
            and fs.trading_interval >= '{date_start}T00:00:00+10:00'
            and fs.trading_interval < '{date_end}T00:00:00+10:00'
    """.format(
        date_start=date_start.date(), date_end=date_end.date()
    )

    df_gen = pd.read_sql(query, con=engine, index_col="trading_interval")

    logger.debug(query)

    return df_gen


def load_energy_intervals(date_start: datetime, date_end: datetime) -> pd.DataFrame:
    """Fetch all emissions for all stations"""

    engine = get_database_engine()

    query = """
        select
            fs.trading_interval at time zone 'AEST' as trading_interval,
            f.network_id,
            f.network_region,
            f.fueltech_id,
            fs.facility_code as fs_duid,
            f.code as duid,
            coalesce(fs.generated, 0) as power,
            coalesce(fs.eoi_quantity, 0) as energy,
            coalesce(fs.eoi_quantity * f.emissions_factor_co2, 0) as emissions
        from facility_scada fs
        left join facility f on fs.facility_code = f.code
        where
            fs.trading_interval >= '{date_start}T00:00:00+10:00'
            and fs.trading_interval < '{date_end}T00:00:00+10:00'
            and f.network_id IN ('NEM')
            -- and fs.eoi_quantity is not null
            and f.interconnector is False
        order by 1 asc;
    """.format(
        date_start=date_start.date(), date_end=date_end.date()
    )

    logger.debug(query)

    df_gen = pd.read_sql(query, con=engine)

    return df_gen


def interconnector_dict(interconnector_di: pd.DataFrame) -> pd.DataFrame:
    dx = (
        interconnector_di.groupby(["interconnector_region_from", "interconnector_region_to"])
        .generated.sum()
        .reset_index()
    )
    dy = dx.rename(
        columns={
            "interconnector_region_from": "interconnector_region_to",
            "interconnector_region_to": "interconnector_region_from",
        }
    )

    # set indexes
    dy.set_index(["interconnector_region_to", "interconnector_region_from"], inplace=True)
    dx.set_index(["interconnector_region_to", "interconnector_region_from"], inplace=True)

    dy["generated"] *= -1

    #
    dx.loc[dx.generated < 0, "generated"] = 0
    dy.loc[dy.generated < 0, "generated"] = 0

    interconnector_dict = {
        **dx.to_dict()["generated"],
        **dy.to_dict()["generated"],
    }

    return interconnector_dict


def region_flows(interconnector_di: pd.DataFrame, day: datetime) -> pd.DataFrame:
    """Get regional energy flows"""
    dx = (
        interconnector_di.groupby(["interconnector_region_from", "interconnector_region_to"])
        .generated.sum()
        .reset_index()
    )
    dy = dx.rename(
        columns={
            "interconnector_region_from": "interconnector_region_to",
            "interconnector_region_to": "interconnector_region_from",
        }
    )

    # set indexes
    dy.set_index(["interconnector_region_to", "interconnector_region_from"], inplace=True)
    dx.set_index(["interconnector_region_to", "interconnector_region_from"], inplace=True)

    dy["generated"] *= -1

    # sum out imports/exports
    dx.loc[dx.generated < 0, "generated"] = 0
    dy.loc[dy.generated < 0, "generated"] = 0

    f = pd.concat([dx, dy])

    energy_flows = pd.DataFrame(
        {
            "energy_imports": f.groupby("interconnector_region_to").generated.sum(),
            "energy_exports": f.groupby("interconnector_region_from").generated.sum(),
        }
    )

    energy_flows["network_id"] = "NEM"
    energy_flows["trading_interval"] = day

    energy_flows.reset_index(inplace=True)
    energy_flows.rename(columns={"index": "network_region"}, inplace=True)
    energy_flows.set_index(["trading_interval", "network_id", "network_region"], inplace=True)

    return energy_flows


def power(df_emissions: pd.DataFrame, df_ic: pd.DataFrame) -> Dict:
    """ """
    df_emissions = df_emissions.reset_index()
    power_dict = dict(df_emissions.groupby(df_emissions.network_region).energy.sum())
    power_dict.update(interconnector_dict(df_ic))
    return power_dict


def simple_exports(
    emissions_di: pd.DataFrame, power_dict: Dict, from_regionid: str, to_regionid: str
):
    dx = emissions_di[emissions_di.network_region == from_regionid]

    try:
        ic_flow = power_dict[from_regionid, to_regionid]
    except KeyError:
        return 0

    emissions_sum = dx.energy.sum() * dx.emissions.sum()
    export_value = 0

    if emissions_sum and emissions_sum > 0:
        export_value = ic_flow / emissions_sum

    return export_value


def emissions(df_emissions: pd.DataFrame, power_dict: Dict) -> Dict:
    df_emissions = df_emissions.reset_index()
    emissions_dict = dict(df_emissions.groupby(df_emissions.network_region).emissions.sum())

    simple_flows = [["QLD1", "NSW1"], ["SA1", "VIC1"], ["TAS1", "VIC1"]]

    for from_regionid, to_regionid in simple_flows:
        try:
            emissions_dict[(from_regionid, to_regionid)] = simple_exports(
                df_emissions, power_dict, from_regionid, to_regionid
            )
        except Exception as e:
            logger.error(e)

    return emissions_dict


def nem_demand(power_dict: Dict) -> Dict:
    """Calculate demand for NEM"""
    d = {}

    if "NSW1" not in power_dict:
        raise Exception("Missing generation info in {}".format(power_dict))

    d["NSW1"] = (
        power_dict["NSW1"]
        + power_dict[("QLD1", "NSW1")]
        + power_dict[("VIC1", "NSW1")]
        - power_dict[("NSW1", "VIC1")]
        - power_dict[("NSW1", "QLD1")]
    )
    d["QLD1"] = power_dict["QLD1"] + power_dict[("NSW1", "QLD1")] - power_dict[("QLD1", "NSW1")]
    d["SA1"] = power_dict["SA1"] + power_dict[("VIC1", "SA1")] - power_dict[("SA1", "VIC1")]
    d["TAS1"] = power_dict["TAS1"] + power_dict[("VIC1", "TAS1")] - power_dict[("TAS1", "VIC1")]
    d["VIC1"] = (
        power_dict["VIC1"]
        + power_dict[("NSW1", "VIC1")]
        + power_dict[("SA1", "VIC1")]
        + power_dict[("TAS1", "VIC1")]
        - power_dict[("VIC1", "NSW1")]
        - power_dict[("VIC1", "TAS1")]
        - power_dict[("VIC1", "SA1")]
    )
    return d


def fill_row(a, row, pairs, _var_dict) -> None:
    for _var, value in pairs:
        a[row][_var_dict[_var]] = value


def fill_constant(a, _var, value, _var_dict) -> None:
    idx = _var_dict[_var]
    a[idx] = value


def solve_flows(emissions_di, interconnector_di) -> pd.DataFrame:
    #
    power_dict = power(emissions_di, interconnector_di)
    emissions_dict = emissions(emissions_di, power_dict)

    try:
        demand_dict = nem_demand(power_dict)
    except Exception as e:
        print("Error: {}".format(e))
        return None

    a = np.zeros((10, 10))
    _var_dict = dict(zip(["s", "q", "t", "n", "v", "v-n", "n-q", "n-v", "v-s", "v-t"], range(10)))

    # emissions balance equations
    fill_row(a, 0, [["s", 1], ["v-s", -1]], _var_dict)
    fill_row(a, 1, [["q", 1], ["n-q", -1]], _var_dict)
    fill_row(a, 2, [["t", 1], ["v-t", -1]], _var_dict)
    fill_row(a, 3, [["n", 1], ["v-n", -1], ["n-q", 1], ["n-v", 1]], _var_dict)
    fill_row(a, 4, [["v", 1], ["v-n", 1], ["n-v", -1], ["v-s", 1], ["v-t", 1]], _var_dict)

    # emissions intensity equations
    fill_row(
        a, 5, [["n-q", 1], ["n", -power_dict[("NSW1", "QLD1")] / demand_dict["NSW1"]]], _var_dict
    )
    fill_row(
        a, 6, [["n-v", 1], ["n", -power_dict[("NSW1", "VIC1")] / demand_dict["NSW1"]]], _var_dict
    )
    fill_row(
        a, 7, [["v-t", 1], ["v", -power_dict[("VIC1", "TAS1")] / demand_dict["VIC1"]]], _var_dict
    )
    fill_row(
        a, 8, [["v-s", 1], ["v", -power_dict[("VIC1", "SA1")] / demand_dict["VIC1"]]], _var_dict
    )
    fill_row(
        a, 9, [["v-n", 1], ["v", -power_dict[("VIC1", "NSW1")] / demand_dict["VIC1"]]], _var_dict
    )

    # constants
    b = np.zeros((10, 1))
    fill_constant(b, "s", emissions_dict["SA1"] - emissions_dict[("SA1", "VIC1")], _var_dict)
    fill_constant(b, "q", emissions_dict["QLD1"] - emissions_dict[("QLD1", "NSW1")], _var_dict)
    fill_constant(b, "t", emissions_dict["TAS1"] - emissions_dict[("TAS1", "VIC1")], _var_dict)
    fill_constant(b, "n", emissions_dict["NSW1"] + emissions_dict[("QLD1", "NSW1")], _var_dict)
    fill_constant(
        b,
        "v",
        emissions_dict["VIC1"]
        + emissions_dict[("SA1", "VIC1")]
        + emissions_dict[("TAS1", "VIC1")],
        _var_dict,
    )

    # cast nan to 0
    b[np.isnan(b)] = 0

    # get result
    result = None

    try:
        result = np.linalg.solve(a, b)
    except Exception as e:
        logger.warning("Error: for {}".format(e))
        result = None

    # transform into emission flows
    emission_flows = {}

    if result is not None:
        emission_flows["NSW1", "QLD1"] = result[6][0]
        emission_flows["VIC1", "NSW1"] = result[5][0]
        emission_flows["NSW1", "VIC1"] = result[7][0]
        emission_flows["VIC1", "SA1"] = result[8][0]
        emission_flows["VIC1", "TAS1"] = result[9][0]

    emission_flows["QLD1", "NSW1"] = emissions_dict["QLD1", "NSW1"]
    emission_flows["TAS1", "VIC1"] = emissions_dict["TAS1", "VIC1"]
    emission_flows["SA1", "VIC1"] = emissions_dict["SA1", "VIC1"]

    # shape into dataframe
    df = pd.DataFrame.from_dict(emission_flows, orient="index")
    df.columns = ["EMISSIONS"]
    df.reset_index(inplace=True)

    return df


def calc_emissions(df_emissions: pd.DataFrame) -> pd.DataFrame:
    df_gen_em = df_emissions.groupby(["trading_interval", "network_region", "fueltech_id"])[
        ["energy", "emissions"]
    ].sum()
    df_gen_em.reset_index(inplace=True)

    return df_gen_em


def calculate_emission_flows(df_gen: pd.DataFrame, df_ic: pd.DataFrame) -> Dict:

    dx_emissions = calc_emissions(df_gen)
    dx_ic = df_ic

    results = {}
    dt = df_gen.trading_interval.iloc[0]
    while dt <= df_gen.trading_interval.iloc[-1]:
        emissions_di = dx_emissions[dx_emissions.trading_interval == dt]
        interconnector_di = dx_ic[dx_ic.index == dt]

        results[dt] = solve_flows(emissions_di, interconnector_di)
        dt += timedelta(minutes=5)

    return results


def calc_day(day: datetime) -> Optional[pd.DataFrame]:

    day_next = day + timedelta(days=1)

    df_gen = load_energy_intervals(date_start=day, date_end=day_next)

    df_ic = load_interconnector_intervals(date_start=day, date_end=day_next)

    results_dict = calculate_emission_flows(df_gen, df_ic)

    if not results_dict or len(results_dict.keys()) < 1:
        logger.error("No results for day {}".format(day))
        return None

    flow_series: Optional[pd.DataFrame] = None

    try:
        flow_series = pd.concat(results_dict)
        flow_series.reset_index(inplace=True)
    except Exception as e:
        logger.error("Error: {}".format(e))
        # logger.debug(results_dict)
        return None

    # if not flow_series or len(flow_series.index) < 1 or flow_series.shape[0] < 1:
    # logger.error("flow_series is empty or none")
    # logger.debug(flow_series)
    # logger.debug(results_dict)
    # return None

    flow_series.rename(
        columns={"level_0": "trading_interval", "index": "network_region"}, inplace=True
    )
    flow_series["region_from"] = flow_series.apply(lambda x: x.network_region[0], axis=1)
    flow_series["region_to"] = flow_series.apply(lambda x: x.network_region[1], axis=1)

    # build the final data frame with both imports and exports
    flow_series_clean = pd.DataFrame(
        {
            "emissions_exports": flow_series.groupby("region_from").EMISSIONS.sum(),
            "emissions_imports": flow_series.groupby("region_to").EMISSIONS.sum(),
            "network_id": "NEM",
            "trading_interval": day,
        }
    )

    flow_series_clean.reset_index(inplace=True)
    flow_series_clean.rename(columns={"index": "network_region"}, inplace=True)

    flow_series_clean.set_index(["trading_interval", "network_id", "network_region"], inplace=True)

    # Add in the energy flows
    energy_flows = region_flows(df_ic, day=day)

    total_series = flow_series_clean.merge(energy_flows, left_index=True, right_index=True)

    return total_series


def insert_flows(flow_results: pd.DataFrame) -> int:
    """Takes a list of generation values and calculates energies and bulk-inserts
    into the database"""

    flow_results.reset_index(inplace=True)

    # Add metadata
    flow_results["created_by"] = "opennem.worker.emissions"
    flow_results["created_at"] = ""
    flow_results["updated_at"] = datetime.now()
    flow_results["market_value_imports"] = 0.0
    flow_results["market_value_exports"] = 0.0

    # # reorder columns
    columns = [
        "trading_interval",
        "network_id",
        "network_region",
        "energy_imports",
        "energy_exports",
        "emissions_imports",
        "emissions_exports",
        "market_value_imports",
        "market_value_exports",
        "created_by",
        "created_at",
        "updated_at",
    ]
    flow_results = flow_results[columns]

    records_to_store: List[Dict] = flow_results.to_dict("records")

    if len(records_to_store) < 1:
        logger.error("No records returned from energy sum")
        return 0

    # Build SQL + CSV and bulk-insert
    sql_query = build_insert_query(
        AggregateNetworkFlows,  # type: ignore
        [
            "energy_imports",
            "energy_exports",
            "emissions_imports",
            "emissions_exports",
            "market_value_imports",
            "market_value_exports",
        ],
    )
    conn = get_database_engine().raw_connection()
    cursor = conn.cursor()

    csv_content = generate_csv_from_records(
        AggregateNetworkFlows,  # type: ignore
        records_to_store,
        column_names=list(records_to_store[0].keys()),
    )

    try:
        cursor.copy_expert(sql_query, csv_content)
        conn.commit()
    except Exception as e:
        logger.error("Error inserting records: {}".format(e))
        return 0

    logger.info("Inserted {} records".format(len(records_to_store)))

    return len(records_to_store)


def run_and_store_emission_flows(day: datetime) -> None:
    """Runs and stores emission flows into the aggregate table"""
    emissions_day = calc_day(day)

    if not emissions_day or emissions_day.empty:
        logger.warning("No results for {}".format(day))
        return None

    records_to_store: List[Dict] = emissions_day.to_dict("records")

    logger.debug("Got {} records".format(len(records_to_store)))

    insert_flows(emissions_day)


def run_emission_update_day(
    days: int = 1, day: Optional[datetime] = None, offset_days: int = 1
) -> None:
    """Run emission calcs for number of days"""
    # This is Sydney time as the data is published in local time

    if not day:
        day = get_last_complete_day_for_network(NetworkNEM) - timedelta(days=offset_days)

    current_day = day
    date_min = day - timedelta(days=days)

    while current_day >= date_min:
        logger.info("Running emission update for {}".format(current_day))

        run_and_store_emission_flows(current_day)

        current_day -= timedelta(days=1)


# debug entry point
if __name__ == "__main__":
    logger.info("starting")
    run_emission_update_day(days=400)
