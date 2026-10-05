import json
import requests
import time
from pathlib import Path
from datetime import datetime


TRIPS_FILE = Path(
    "data/processed/puls_trip_details.json"
)

HBF_FILE = Path(
    "data/processed/puls_morning_departures.json"
)

OUTPUT_FILE = Path(
    "data/processed/puls_return_legs_verified.json"
)

SERVICE_DATE = "2026-10-05"

API_BASE = "https://start.vag.de/dm/api"

TIMESPAN = 180
TIMEDELAY = -516
LIMITCOUNT = 100


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def parse_dt(value):
    if not value:
        return None

    return datetime.fromisoformat(value)


def is_hbf(stop):
    if not stop:
        return False

    name = str(
        stop.get("Haltestellenname", "")
    ).lower()

    vag = str(
        stop.get("VAGKennung", "")
    ).upper()

    vgn = str(
        stop.get("VGNKennung", "")
    )

    return (
        "hauptbahnhof" in name
        or vag == "HBF"
        or vgn == "510"
    )


def extract_departure_records(raw):
    """
    PULS may return either:

      - a top-level list
      - a dict containing the departure list

    Find all departure-like dicts recursively.
    """

    found = []

    def walk(obj):

        if isinstance(obj, list):

            for item in obj:
                walk(item)

        elif isinstance(obj, dict):

            # This itself looks like a departure record
            if (
                "fahrtnummer" in obj
                or "Fahrtnummer" in obj
            ):

                if (
                    "fahrzeugnummer" in obj
                    or "Fahrzeugnummer" in obj
                ):
                    found.append(obj)

            for value in obj.values():
                walk(value)

    walk(raw)

    # Deduplicate
    unique = {}

    for dep in found:

        fahrtnummer = (
            dep.get("fahrtnummer")
            or dep.get("Fahrtnummer")
        )

        if fahrtnummer is not None:

            unique[str(fahrtnummer)] = dep

    return list(unique.values())


def get_value(dep, *keys):

    for key in keys:

        if key in dep:
            return dep[key]

    return None


# ---------------------------------------------------------
# Load source data
# ---------------------------------------------------------

with open(
    TRIPS_FILE,
    "r",
    encoding="utf-8"
) as f:

    trips = json.load(f)


with open(
    HBF_FILE,
    "r",
    encoding="utf-8"
) as f:

    hbf_departures = json.load(f)


print(
    f"Loaded trip details: {len(trips)}"
)

print(
    f"Loaded Hbf departures: "
    f"{len(hbf_departures)}"
)


# ---------------------------------------------------------
# Vehicles observed at Hbf
# ---------------------------------------------------------

vehicles = set()

hbf_by_vehicle = {}

for dep in hbf_departures:

    vehicle = dep.get("fahrzeugnummer")

    scheduled = dep.get(
        "scheduled_departure"
    )

    if not vehicle or not scheduled:
        continue

    vehicle = str(vehicle)

    vehicles.add(vehicle)

    hbf_by_vehicle.setdefault(
        vehicle,
        []
    ).append(dep)


for vehicle in hbf_by_vehicle:

    hbf_by_vehicle[vehicle].sort(
        key=lambda x: parse_dt(
            x["scheduled_departure"]
        )
    )


print(
    f"Observed Hbf vehicles: "
    f"{sorted(vehicles)}"
)


# ---------------------------------------------------------
# Discover terminal from each trip
# ---------------------------------------------------------

terminal_queries = {}

for rec in trips:

    trip = rec["puls_trip"]

    line = str(
        trip.get("Linienname")
    )

    vehicle = str(
        trip.get("Fahrzeugnummer")
    )

    fahrverlauf = trip.get(
        "Fahrtverlauf",
        []
    )

    if not fahrverlauf:
        continue

    last_stop = fahrverlauf[-1]

    stop_id = last_stop.get(
        "VGNKennung"
    )

    stop_name = last_stop.get(
        "Haltestellenname"
    )

    if stop_id is None:
        continue

    terminal_queries[
        (line, str(stop_id))
    ] = {
        "line": line,
        "vgn_stop_id": str(stop_id),
        "terminal_name": stop_name
    }


print("\n========================================")
print("TERMINALS")
print("========================================")

for q in sorted(
    terminal_queries.values(),
    key=lambda x: (
        x["line"],
        x["vgn_stop_id"]
    )
):

    print(
        f"Line {q['line']} | "
        f"{q['terminal_name']} | "
        f"VGN {q['vgn_stop_id']}"
    )


# ---------------------------------------------------------
# Fetch terminal departures
# ---------------------------------------------------------

terminal_departures = []

for q in terminal_queries.values():

    url = (
        f"{API_BASE}/abfahrten.json/"
        f"vgn/{q['vgn_stop_id']}/"
        f"{q['line']}"
    )

    params = {
        "produkt": "bus",
        "timespan": TIMESPAN,
        "timedelay": TIMEDELAY,
        "limitcount": LIMITCOUNT
    }

    print("\n----------------------------------------")
    print(
        f"Fetching "
        f"Line {q['line']} "
        f"{q['terminal_name']}"
    )

    response = requests.get(
        url,
        params=params,
        timeout=20
    )

    print(
        f"HTTP: {response.status_code}"
    )

    if response.status_code != 200:
        continue

    raw = response.json()

    print(
        f"Response type: "
        f"{type(raw).__name__}"
    )

    departures = extract_departure_records(
        raw
    )

    print(
        f"Departure records found: "
        f"{len(departures)}"
    )

    for dep in departures:

        vehicle = get_value(
            dep,
            "fahrzeugnummer",
            "Fahrzeugnummer"
        )

        fahrtnummer = get_value(
            dep,
            "fahrtnummer",
            "Fahrtnummer"
        )

        scheduled = get_value(
            dep,
            "scheduled_departure",
            "AbfahrtszeitSoll"
        )

        if not vehicle or not fahrtnummer:
            continue

        vehicle = str(vehicle)
        fahrtnummer = str(fahrtnummer)

        # Only care about vehicles we already
        # observed at Nürnberg Hbf.
        if vehicle not in vehicles:
            continue

        terminal_departures.append(
            {
                "line": q["line"],
                "terminal_name":
                    q["terminal_name"],
                "terminal_vgn_stop_id":
                    q["vgn_stop_id"],
                "fahrtnummer":
                    fahrtnummer,
                "fahrzeugnummer":
                    vehicle,
                "scheduled_departure":
                    scheduled,
                "raw_departure":
                    dep
            }
        )

    time.sleep(0.2)


# ---------------------------------------------------------
# Deduplicate terminal departures
# ---------------------------------------------------------

unique_terminal = {}

for dep in terminal_departures:

    unique_terminal[
        dep["fahrtnummer"]
    ] = dep


terminal_departures = list(
    unique_terminal.values()
)


print("\n========================================")
print("TERMINAL CANDIDATES")
print("========================================")

print(
    f"Candidate return trips: "
    f"{len(terminal_departures)}"
)


# ---------------------------------------------------------
# Fetch specific trip details for candidates
# ---------------------------------------------------------

verified_returns = []

for i, dep in enumerate(
    terminal_departures,
    start=1
):

    fahrtnummer = dep["fahrtnummer"]

    url = (
        f"{API_BASE}/fahrten.json/"
        f"bus/{SERVICE_DATE}/"
        f"{fahrtnummer}"
    )

    print(
        f"[{i}/{len(terminal_departures)}] "
        f"Checking trip {fahrtnummer} "
        f"(vehicle {dep['fahrzeugnummer']})"
    )

    try:

        response = requests.get(
            url,
            timeout=20
        )

        if response.status_code != 200:

            print(
                f"  HTTP {response.status_code}"
            )

            continue

        trip = response.json()

        fahrverlauf = trip.get(
            "Fahrtverlauf",
            []
        )

        if not fahrverlauf:

            continue

        vehicle = str(
            trip.get(
                "Fahrzeugnummer",
                dep["fahrzeugnummer"]
            )
        )

        # Check entire trip for Hbf
        hbf_stops = [
            stop
            for stop in fahrverlauf
            if is_hbf(stop)
        ]

        if not hbf_stops:

            print(
                "  Does NOT contain Hbf"
            )

            continue

        hbf_stop = hbf_stops[0]

        hbf_arrival = (
            hbf_stop.get(
                "AnkunftszeitSoll"
            )
            or hbf_stop.get(
                "AbfahrtszeitSoll"
            )
        )

        terminal_departure = (
            fahrverlauf[0].get(
                "AbfahrtszeitSoll"
            )
            or fahrverlauf[0].get(
                "AnkunftszeitSoll"
            )
        )

        terminal_name = (
            fahrverlauf[0].get(
                "Haltestellenname"
            )
        )

        verified_returns.append(
            {
                "line":
                    str(
                        trip.get(
                            "Linienname",
                            dep["line"]
                        )
                    ),

                "vehicle_number":
                    vehicle,

                "return_fahrtnummer":
                    fahrtnummer,

                "return_origin":
                    terminal_name,

                "return_origin_departure":
                    terminal_departure,

                "hbf_arrival":
                    hbf_arrival,

                "hbf_stop":
                    hbf_stop.get(
                        "Haltestellenname"
                    ),

                "verified_return_to_hbf":
                    True,

                "direction":
                    trip.get(
                        "Richtungstext"
                    )
            }
        )

        print(
            f"  VERIFIED → "
            f"Hbf {hbf_arrival}"
        )

    except Exception as e:

        print(
            f"  ERROR: {e}"
        )

    time.sleep(0.1)


# ---------------------------------------------------------
# Match verified returns to outbound trips
# ---------------------------------------------------------

results = []

for rec in trips:

    trip = rec["puls_trip"]

    vehicle = str(
        trip.get("Fahrzeugnummer")
    )

    outbound_fahrtnummer = str(
        trip.get("Fahrtnummer")
    )

    line = str(
        trip.get("Linienname")
    )

    fv = trip.get(
        "Fahrtverlauf",
        []
    )

    if not fv:
        continue

    outbound_terminal = fv[-1]

    terminal_arrival = (
        outbound_terminal.get(
            "AnkunftszeitSoll"
        )
        or outbound_terminal.get(
            "AbfahrtszeitSoll"
        )
    )

    terminal_arrival_dt = parse_dt(
        terminal_arrival
    )

    # Find first verified return for same vehicle
    # occurring after this outbound terminal arrival.
    candidates = []

    for ret in verified_returns:

        if ret["vehicle_number"] != vehicle:
            continue

        ret_departure_dt = parse_dt(
            ret["return_origin_departure"]
        )

        if (
            ret_departure_dt
            and terminal_arrival_dt
            and ret_departure_dt > terminal_arrival_dt
        ):

            candidates.append(ret)

    candidates.sort(
        key=lambda x: parse_dt(
            x["return_origin_departure"]
        )
    )

    matched_return = (
        candidates[0]
        if candidates
        else None
    )

    # Next Hbf departure of this same vehicle
    next_hbf = None

    for hbf_dep in hbf_by_vehicle.get(
        vehicle,
        []
    ):

        hbf_dt = parse_dt(
            hbf_dep["scheduled_departure"]
        )

        outbound_hbf_dt = parse_dt(
            rec["hbf_scheduled_departure"]
        )

        if hbf_dt > outbound_hbf_dt:

            next_hbf = hbf_dep
            break

    terminal_to_return = None

    if matched_return:

        terminal_to_return = (
            parse_dt(
                matched_return[
                    "return_origin_departure"
                ]
            )
            - terminal_arrival_dt
        ).total_seconds() / 60

    terminal_to_next_hbf = None

    if next_hbf:

        terminal_to_next_hbf = (
            parse_dt(
                next_hbf[
                    "scheduled_departure"
                ]
            )
            - terminal_arrival_dt
        ).total_seconds() / 60

    results.append(
        {
            "service_date":
                rec["service_date"],

            "line":
                line,

            "vehicle_number":
                vehicle,

            "outbound_fahrtnummer":
                outbound_fahrtnummer,

            "hbf_departure":
                rec["hbf_scheduled_departure"],

            "terminal":
                outbound_terminal.get(
                    "Haltestellenname"
                ),

            "terminal_arrival":
                terminal_arrival,

            "next_hbf_departure":
                (
                    next_hbf[
                        "scheduled_departure"
                    ]
                    if next_hbf
                    else None
                ),

            "terminal_to_next_hbf_minutes":
                terminal_to_next_hbf,

            "return_leg_verified":
                matched_return is not None,

            "return_fahrtnummer":
                (
                    matched_return[
                        "return_fahrtnummer"
                    ]
                    if matched_return
                    else None
                ),

            "return_departure":
                (
                    matched_return[
                        "return_origin_departure"
                    ]
                    if matched_return
                    else None
                ),

            "return_hbf_arrival":
                (
                    matched_return[
                        "hbf_arrival"
                    ]
                    if matched_return
                    else None
                ),

            "terminal_to_verified_return_minutes":
                terminal_to_return,

            "validation":
                "requires_VAG_operational_confirmation"
        }
    )


# ---------------------------------------------------------
# Save
# ---------------------------------------------------------

OUTPUT_FILE.parent.mkdir(
    parents=True,
    exist_ok=True
)

with open(
    OUTPUT_FILE,
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

verified_count = sum(
    1
    for r in results
    if r["return_leg_verified"]
)

verified_intervals = [
    r["terminal_to_verified_return_minutes"]
    for r in results
    if r["terminal_to_verified_return_minutes"]
    is not None
]

print("\n========================================")
print("RETURN LEG VERIFICATION COMPLETE")
print("========================================")

print(
    f"Outbound trips analyzed: {len(results)}"
)

print(
    f"Verified vehicle returns to Hbf: "
    f"{verified_count}"
)

if verified_intervals:

    print(
        f"Min terminal→return: "
        f"{min(verified_intervals):.1f} min"
    )

    print(
        f"Max terminal→return: "
        f"{max(verified_intervals):.1f} min"
    )

print(
    f"Saved: {OUTPUT_FILE}"
)