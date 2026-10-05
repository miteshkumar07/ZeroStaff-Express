import requests
import json
from pathlib import Path

OUTPUT = Path("data/processed")
OUTPUT.mkdir(parents=True, exist_ok=True)

# Nürnberg Hbf VGN stop code
HBF_VGN_ID = "510"

TARGET_LINES = ["43", "44"]

# Try to look around the morning period.
# PULS is a real-time service, so we will test a shifted window.
TIME_DELAY = -360     # minutes relative to current time
TIME_SPAN = 120       # 2-hour window
LIMIT = 100


def get_departures(line):
    url = (
        "https://start.vag.de/dm/api/"
        f"abfahrten.json/vgn/"
        f"{HBF_VGN_ID}/"
        f"{line}"
    )

    params = {
        "produkt": "bus",
        "timespan": TIME_SPAN,
        "timedelay": TIME_DELAY,
        "limitcount": LIMIT
    }

    print("\n========================================")
    print(f"LINE {line}")
    print("========================================")

    print("URL:")
    print(url)

    print("\nParameters:")
    print(params)

    try:

        response = requests.get(
            url,
            params=params,
            timeout=20
        )

        print("\nHTTP status:", response.status_code)

        response.raise_for_status()

        data = response.json()

        return data

    except Exception as e:

        print("\nERROR:")
        print(e)

        return None


all_results = []

for line in TARGET_LINES:

    data = get_departures(line)

    if not data:
        continue

    departures = data.get(
        "Abfahrten",
        []
    )

    print(
        f"\nDepartures returned: "
        f"{len(departures)}"
    )

    for departure in departures[:20]:

        result = {
            "line": departure.get(
                "Linienname"
            ),

            "direction": departure.get(
                "Richtung"
            ),

            "scheduled_departure":
                departure.get(
                    "AbfahrtszeitSoll"
                ),

            "predicted_departure":
                departure.get(
                    "AbfahrtszeitIst"
                ),

            "fahrtnummer":
                departure.get(
                    "Fahrtnummer"
                ),

            "fahrzeugnummer":
                departure.get(
                    "Fahrzeugnummer"
                ),

            "stop_name":
                departure.get(
                    "Haltestellenname"
                ),

            "stop_point":
                departure.get(
                    "Haltepunkt"
                ),

            "prediction_available":
                departure.get(
                    "Prognose"
                )
        }

        all_results.append(result)

        print(
            "\n--------------------------------"
        )

        for key, value in result.items():

            print(
                f"{key}: {value}"
            )


with open(
    OUTPUT / "puls_departures_test.json",
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        all_results,
        f,
        indent=2,
        ensure_ascii=False
    )


print("\n========================================")
print("TEST COMPLETE")
print("========================================")

print(
    f"Total records: {len(all_results)}"
)

print(
    "Saved:"
    " data/processed/puls_departures_test.json"
)