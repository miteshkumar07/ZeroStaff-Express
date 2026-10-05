import pandas as pd
import re
import json
import urllib.request
import urllib.error
from pathlib import Path

BASE = Path("data/processed")

SERVICE_DATE = "2026-10-05"

TARGET_ROUTES = {"30", "31", "43", "44"}


# =========================================================
# LOAD OUR CLEAN DATA
# =========================================================

trips = pd.read_csv(
    BASE / "target_trips.csv",
    dtype=str
)

stop_times = pd.read_csv(
    BASE / "target_stop_times.csv",
    dtype=str
)


# =========================================================
# FIND TRIPS THAT ACTUALLY VISIT NÜRNBERG HBF
# =========================================================

hbf_stop_times = stop_times[
    stop_times["stop_id"].str.startswith(
        "de:09564:510",
        na=False
    )
].copy()

hbf_trip_ids = set(
    hbf_stop_times["trip_id"]
)

test_trips = trips[
    trips["trip_id"].isin(hbf_trip_ids) &
    trips["route_short_name"].isin(TARGET_ROUTES)
].copy()

print("\n========================================")
print("GTFS TRIPS AT NÜRNBERG HBF")
print("========================================")

print(
    test_trips[
        [
            "trip_id",
            "route_short_name",
            "trip_headsign",
            "start_time",
            "end_time"
        ]
    ].head(10).to_string(index=False)
)

print(
    f"\nTrips available for PULS testing: "
    f"{len(test_trips)}"
)


# =========================================================
# EXTRACT POSSIBLE FAHRTNUMMER
# =========================================================

def extract_fahrtnummer(trip_id):
    """
    Example:
        103.T0.13-43-j26-2.6.R
        -> 103
    """

    match = re.match(
        r"^(\d+)\.",
        str(trip_id)
    )

    if match:
        return match.group(1)

    return None


test_trips["fahrtnummer"] = (
    test_trips["trip_id"]
    .apply(extract_fahrtnummer)
)


# =========================================================
# QUERY VAG PULS
# =========================================================

def query_puls(fahrtnummer):

    url = (
        "https://start.vag.de/dm/api/"
        f"fahrten.json/bus/"
        f"{SERVICE_DATE}/"
        f"{fahrtnummer}"
    )

    print("\nRequest:")
    print(url)

    try:

        with urllib.request.urlopen(
            url,
            timeout=15
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            return json.loads(raw)

    except urllib.error.HTTPError as e:

        print(
            f"HTTP error: {e.code}"
        )

        try:
            print(
                e.read().decode("utf-8")
            )
        except:
            pass

        return None

    except Exception as e:

        print(
            f"Request failed: {e}"
        )

        return None


# =========================================================
# TEST UP TO 5 REAL TRIPS
# =========================================================

results = []

for _, row in test_trips.head(5).iterrows():

    fahrtnummer = row["fahrtnummer"]

    if not fahrtnummer:
        continue

    data = query_puls(
        fahrtnummer
    )

    if data is None:
        continue

    result = {
        "gtfs_trip_id":
            row["trip_id"],

        "gtfs_route":
            row["route_short_name"],

        "fahrtnummer":
            fahrtnummer,

        "puls_linie":
            data.get("Linienname"),

        "puls_fahrzeugnummer":
            data.get("Fahrzeugnummer"),

        "puls_betriebstag":
            data.get("Betriebstag"),

        "puls_richtung":
            data.get("Richtung"),

        "puls_fahrtverlauf_stops":
            len(data.get("Fahrtverlauf", []))
    }

    results.append(result)

    print("\n--------------------------------")
    print("PULS RESULT")
    print("--------------------------------")

    for key, value in result.items():
        print(
            f"{key}: {value}"
        )


# =========================================================
# SAVE
# =========================================================

with open(
    BASE / "puls_test_results.json",
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        results,
        f,
        indent=2,
        ensure_ascii=False
    )


print("\n========================================")
print("PULS TEST COMPLETE")
print("========================================")

print(
    f"Successful responses: "
    f"{len(results)}"
)

print(
    "Saved: "
    "data/processed/puls_test_results.json"
)