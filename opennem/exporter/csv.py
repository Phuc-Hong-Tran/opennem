import csv
from io import StringIO

from opennem.api.stations import get_stations


def stations_csv_records():
    stations = get_stations()

    records = []

    for station in stations:
        for facility in station.facilities:
            rec = {
                "name": station.name,
                # "oid": station.oid,
                "ocode": station.ocode,
                "code": station.code,
                "network": facility.network_code,
                "region": facility.network_region,
                "status": facility.status_id,
                "fueltech": facility.fueltech_id,
                "unit_id": facility.unit_id,
                "unit_num": facility.unit_number,
                "unit_cap": facility.capacity_aggregate,
                "station_cap": station.capacity_aggregate,
                "added_by": facility.created_by,
                "updated_by": facility.created_by,
            }
            records.append(rec)

    return records


def stations_csv_serialize(csv_stream=None):

    if not csv_stream:
        csv_stream = StringIO()

    csv_records = stations_csv_records()

    csv_fieldnames = csv_records[0].keys()

    csvwriter = csv.DictWriter(csv_stream, fieldnames=csv_fieldnames)
    csvwriter.writeheader()
    csvwriter.writerows(csv_records)

    return csv_stream
