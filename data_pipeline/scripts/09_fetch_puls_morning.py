import requests
import json
from pathlib import Path

OUTPUT = Path("data/processed")
OUTPUT.mkdir(parents=True, exist_ok=True)

# =========================================================
# CONFIG
# =========================================================

SERVICE_DATE = "2026-10-05"

# Nürnberg Hbf VGN stop identifier
HBF_VGN_ID = "510"

TARGET_LINES = ["43", "44"]

TARGET_START = "06:30"
TARGET_END = "09:00"


# =========================================================
# HELPERS
# =========================================================

def iso_minutes(value):
    """
    Convert ISO timestamp to minutes after midnight.
    """
    if not value:
        return None

    # Example:
    # 2026-10-05T09:08:00+02:00

    time_part = value.split("T")[1]

    hour = int(time_part[0:2])
    minute = int(time_part[3:5])

    return hour * 60 + minute


def hhmm_to_minutes(value):
    h, m = map(int, value.split(":"))
    return h * 60 + m


target_start_min = hhmm_to_minutes(TARGET_START)
target_end_min = hhmm_to_minutes(TARGET_END)


# =========================================================
# GET SERVER TIME
# =========================================================

def get_probe():

    url = (
        "https://start.vag.de/dm/api/"
        f"abfahrten.json/vgn/"
        f"{HBF_VGN_ID}/43"
    )

    response = requests.get(
        url,
        params={
            "produkt": "bus",
            "timespan": 1,
            "timedelay": 0,
            "limitcount": 5
        },
        timeout=20
    )

    response.raise_for_status()

    return response.json()


probe = get_probe()

server_timestamp = (
    probe
    .get("Metadata", {})
    .get("Timestamp")
)

print("VAG server timestamp:")
print(server_timestamp)

if not server_timestamp:
    raise RuntimeError(
        "Could not read VAG server timestamp."
    )


# =========================================================
# CALCULATE TIME SHIFT
# =========================================================

# Extract current server clock
server_time = server_timestamp.split("T")[1]

server_hour = int(server_time[0:2])
server_minute = int(server_time[3:5])

server_minutes = (
    server_hour * 60
    + server_minute
)

# We want approximately 06:30
timedelay = target_start_min - server_minutes

print(
    f"\nCalculated timedelay: {timedelay} minutes"
)

# Get a little extra on both sides
timespan = (
    target_end_min
    - target_start_min
    + 30
)

print(
    f"Timespan: {timespan} minutes"
)


# =========================================================
# FETCH EACH LINE
# =========================================================

all_departures = []

for line in TARGET_LINES:

    print("\n========================================")
    print(f"Fetching Line {line}")
    print("========================================")

    url = (
        "https://start.vag.de/dm/api/"
        f"abfahrten.json/vgn/"
        f"{HBF_VGN_ID}/{line}"
    )

    params = {
        "produkt": "bus",
        "timespan": timespan,
        "timedelay": timedelay,
        "limitcount": 100
    }

    print(url)
    print(params)

    response = requests.get(
        url,
        params=params,
        timeout=20
    )

    print(
        "HTTP:",
        response.status_code
    )

    response.raise_for_status()

    data = response.json()

    departures = data.get(
        "Abfahrten",
        []
    )

    print(
        "Raw departures:",
        len(departures)
    )

    for item in departures:

        scheduled = item.get(
            "AbfahrtszeitSoll"
        )

        if not scheduled:
            continue

        minutes = iso_minutes(
            scheduled
        )

        if minutes is None:
            continue

        # Keep only 06:30–09:00
        if not (
            target_start_min
            <= minutes
            <= target_end_min
        ):
            continue

        record = {
            "service_date":
                SERVICE_DATE,

            "line":
                item.get("Linienname"),

            "direction":
                item.get("Richtung"),

            "scheduled_departure":
                item.get("AbfahrtszeitSoll"),

            "predicted_departure":
                item.get("AbfahrtszeitIst"),

            "fahrtnummer":
                item.get("Fahrtnummer"),

            "fahrzeugnummer":
                item.get("Fahrzeugnummer"),

            "stop_point":
                item.get("Haltepunkt"),

            "vag_stop_id":
                item.get("VAGKennung"),

            "vgn_stop_id":
                item.get("VGNKennung"),

            "prediction_available":
                item.get("Prognose"),

            "longitude":
                item.get("Longitude"),

            "latitude":
                item.get("Latitude")
        }

        all_departures.append(record)


# =========================================================
# SAVE RAW RESULTS
# =========================================================

with open(
    OUTPUT / "puls_morning_departures.json",
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        all_departures,
        f,
        indent=2,
        ensure_ascii=False
    )


# =========================================================
# SUMMARY
# =========================================================

print("\n========================================")
print("PULS MORNING DATA COMPLETE")
print("========================================")

print(
    "Total departures:",
    len(all_departures)
)

for line in TARGET_LINES:

    subset = [
        x for x in all_departures
        if str(x["line"]) == line
    ]

    vehicles = {
        str(x["fahrzeugnummer"])
        for x in subset
        if x["fahrzeugnummer"] not in [None, ""]
    }

    print(
        f"\nLine {line}:"
    )

    print(
        "  departures:",
        len(subset)
    )

    print(
        "  unique vehicles:",
        len(vehicles)
    )

    print(
        "  vehicles:",
        sorted(vehicles)
    )

print(
    "\nSaved:"
    " data/processed/puls_morning_departures.json"
)