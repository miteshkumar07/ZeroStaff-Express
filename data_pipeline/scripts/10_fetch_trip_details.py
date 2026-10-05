import requests
import json
import time
from pathlib import Path

INPUT = Path("data/processed/puls_morning_departures.json")
OUTPUT = Path("data/processed/puls_trip_details.json")

SERVICE_DATE = "2026-10-05"


# ---------------------------------------------------------
# Load PULS departures
# ---------------------------------------------------------

with open(INPUT, "r", encoding="utf-8") as f:
    departures = json.load(f)

print(f"Loaded {len(departures)} departures")


# ---------------------------------------------------------
# Fetch individual trip
# ---------------------------------------------------------

def fetch_trip(fahrtnummer):

    url = (
        "https://start.vag.de/dm/api/"
        f"fahrten.json/bus/"
        f"{SERVICE_DATE}/"
        f"{fahrtnummer}"
    )

    try:

        response = requests.get(
            url,
            timeout=20
        )

        if response.status_code != 200:

            print(
                f"ERROR {response.status_code} "
                f"for Fahrtnummer {fahrtnummer}"
            )

            return None

        return response.json()

    except Exception as e:

        print(
            f"Request failed for "
            f"{fahrtnummer}: {e}"
        )

        return None


# ---------------------------------------------------------
# Fetch all unique trips
# ---------------------------------------------------------

results = []

seen = set()

for i, departure in enumerate(departures, start=1):

    fahrtnummer = departure.get(
        "fahrtnummer"
    )

    if not fahrtnummer:
        continue

    fahrtnummer = str(fahrtnummer)

    if fahrtnummer in seen:
        continue

    seen.add(fahrtnummer)

    print(
        f"[{i}/{len(departures)}] "
        f"Fetching {fahrtnummer}..."
    )

    data = fetch_trip(
        fahrtnummer
    )

    if data is None:
        continue

    # Add our Hbf departure context
    results.append(
        {
            "service_date": SERVICE_DATE,

            "fahrtnummer": fahrtnummer,

            "line_from_departures":
                departure.get("line"),

            "vehicle_from_departures":
                departure.get(
                    "fahrzeugnummer"
                ),

            "hbf_scheduled_departure":
                departure.get(
                    "scheduled_departure"
                ),

            "hbf_predicted_departure":
                departure.get(
                    "predicted_departure"
                ),

            "puls_trip": data
        }
    )

    # Be polite to API
    time.sleep(0.1)


# ---------------------------------------------------------
# Save
# ---------------------------------------------------------

with open(
    OUTPUT,
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        results,
        f,
        indent=2,
        ensure_ascii=False
    )


# ---------------------------------------------------------
# Summary
# ---------------------------------------------------------

print("\n========================================")
print("TRIP DETAILS COMPLETE")
print("========================================")

print(
    f"Requested trips: {len(seen)}"
)

print(
    f"Successful trips: {len(results)}"
)

print(
    f"Failed trips: "
    f"{len(seen) - len(results)}"
)

print(
    f"Saved: {OUTPUT}"
)