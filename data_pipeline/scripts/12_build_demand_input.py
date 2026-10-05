import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime


# =========================================================
# INPUT FILES
# =========================================================

PULS_RETURNS = Path(
    "data/processed/puls_return_legs_verified.json"
)

PULS_MORNING = Path(
    "data/processed/puls_morning_departures.json"
)


# =========================================================
# OUTPUT
# =========================================================

OUTPUT = Path(
    "data/processed/demand_input.json"
)


# =========================================================
# CONFIGURATION
# =========================================================

SERVICE_DATE = "2026-10-05"

X30_ONE_WAY_MINUTES = 18

MORNING_START = "06:30"
MORNING_END = "09:00"

# We do NOT call this "free capacity".
# We use it only to identify existing trips worth
# testing as replacement/reallocation candidates.
CANDIDATE_MIN_TERMINAL_TO_NEXT_HBF = 35


# =========================================================
# HELPERS
# =========================================================

def parse_dt(value):
    if not value:
        return None

    return datetime.fromisoformat(value)


def minutes_between(start, end):
    a = parse_dt(start)
    b = parse_dt(end)

    if not a or not b:
        return None

    return (b - a).total_seconds() / 60


# =========================================================
# LOAD DATA
# =========================================================

with open(
    PULS_RETURNS,
    "r",
    encoding="utf-8"
) as f:
    return_rows = json.load(f)


with open(
    PULS_MORNING,
    "r",
    encoding="utf-8"
) as f:
    morning_rows = json.load(f)


print("========================================")
print("BUILDING FINAL DEMAND INPUT")
print("========================================")

print(
    f"Return-leg records: {len(return_rows)}"
)

print(
    f"Morning PULS departures: "
    f"{len(morning_rows)}"
)


# =========================================================
# OBSERVED VEHICLES
# =========================================================

vehicles = sorted(
    {
        str(r["vehicle_number"])
        for r in return_rows
        if r.get("vehicle_number")
    }
)

print(
    f"Observed vehicles: {len(vehicles)}"
)


# =========================================================
# VERIFIED RETURNS
# =========================================================

verified_returns = [
    r
    for r in return_rows
    if r.get("return_leg_verified") is True
]

print(
    f"Verified returns: "
    f"{len(verified_returns)}"
)


# =========================================================
# OPERATIONAL PATTERNS
# =========================================================

pattern_groups = defaultdict(list)

for row in verified_returns:

    line = str(row["line"])
    terminal = row.get("terminal", "")

    if line == "43":

        key = "43_passauer"

        label = (
            "Line 43 "
            "Nürnberg Hbf – Passauer Straße"
        )

    elif (
        line == "44"
        and "Zerzabelshof Ost" in terminal
    ):

        key = "44_zerzabelshof_ost"

        label = (
            "Line 44 "
            "Nürnberg Hbf – Zerzabelshof Ost"
        )

    elif (
        line == "44"
        and "Langwasser Mitte" in terminal
    ):

        key = "44_langwasser_mitte"

        label = (
            "Line 44 "
            "Nürnberg Hbf – Langwasser Mitte"
        )

    else:

        key = f"{line}_{terminal}"

        label = (
            f"Line {line} – {terminal}"
        )

    pattern_groups[key].append(row)


operational_patterns = []


for key, rows in pattern_groups.items():

    full_cycles = []
    turnarounds = []
    outbound_times = []

    for row in rows:

        outbound = row.get(
            "hbf_departure"
        )

        terminal = row.get(
            "terminal_arrival"
        )

        return_departure = row.get(
            "return_departure"
        )

        return_hbf = row.get(
            "return_hbf_arrival"
        )

        if outbound and return_hbf:

            cycle = minutes_between(
                outbound,
                return_hbf
            )

            if cycle is not None:
                full_cycles.append(cycle)

        if terminal and return_departure:

            turnaround = minutes_between(
                terminal,
                return_departure
            )

            if turnaround is not None:
                turnarounds.append(
                    turnaround
                )

        if outbound and terminal:

            outbound_time = minutes_between(
                outbound,
                terminal
            )

            if outbound_time is not None:
                outbound_times.append(
                    outbound_time
                )

    operational_patterns.append(
        {
            "pattern_id": key,
            "label": (
                rows[0].get("line"),
                rows[0].get("terminal")
            ),
            "observations": len(rows),
            "observed_outbound_minutes": (
                sorted(set(outbound_times))
            ),
            "observed_terminal_turnaround_minutes": (
                sorted(set(turnarounds))
            ),
            "observed_full_cycle_minutes": (
                sorted(set(full_cycles))
            )
        }
    )


# =========================================================
# CANDIDATE REPLACEMENT WINDOWS
# =========================================================

candidate_windows = []


for row in return_rows:

    if row.get("return_leg_verified") is not True:
        continue

    gap = row.get(
        "terminal_to_next_hbf_minutes"
    )

    if gap is None:
        continue

    if gap < CANDIDATE_MIN_TERMINAL_TO_NEXT_HBF:
        continue

    candidate_windows.append(
        {
            "line":
                row["line"],

            "vehicle_number":
                str(row["vehicle_number"]),

            "existing_fahrtnummer":
                row["outbound_fahrtnummer"],

            "hbf_departure":
                row["hbf_departure"],

            "terminal":
                row["terminal"],

            "terminal_arrival":
                row["terminal_arrival"],

            "verified_return_fahrtnummer":
                row["return_fahrtnummer"],

            "verified_return_departure":
                row["return_departure"],

            "verified_return_hbf_arrival":
                row["return_hbf_arrival"],

            "terminal_to_next_hbf_minutes":
                gap,

            "terminal_to_verified_return_minutes":
                row[
                    "terminal_to_verified_return_minutes"
                ],

            "candidate_reason":
                (
                    "Observed same-vehicle schedule has "
                    f"{gap:.0f} minutes from terminal arrival "
                    "to next scheduled Hbf departure; "
                    "test as a trip-replacement/reallocation "
                    "candidate rather than spare capacity."
                ),

            "status":
                "candidate",

            "requires_validation":
                [
                    "VAG operational approval",
                    "driver duty feasibility",
                    "vehicle circulation feasibility",
                    "impact on replaced service"
                ]
        }
    )


# =========================================================
# SORT CANDIDATES
# =========================================================

candidate_windows.sort(
    key=lambda x: (
        -x["terminal_to_next_hbf_minutes"],
        x["hbf_departure"]
    )
)


# =========================================================
# DEMAND HUBS
# =========================================================
#
# These are STRUCTURAL demand hubs, not passenger counts.
# Keep them as weights for the optimizer.
#
# Coordinates come from the GTFS stops dataset.
# =========================================================

demand_hubs = [

    {
        "id": "HBF",
        "name": "Nürnberg Hbf",
        "type": "regional_transit_gateway",
        "vgn_stop_id": 510,
        "latitude": 49.44725111,
        "longitude": 11.08189,
        "demand_weight": 1.00
    },

    {
        "id": "NORDOSTPARK",
        "name": "Nürnberg Nordostpark",
        "type": "employment_cluster",
        "gtfs_parent_stop_id":
            "de:09564:1372",
        "latitude": 49.48914307,
        "longitude": 11.12585039,
        "demand_weight": 1.00
    },

    {
        "id": "HERRNHUETTE",
        "name": "Nürnberg Herrnhütte",
        "type": "feeder_transit_node",
        "latitude": 49.47876082,
        "longitude": 11.11012987,
        "demand_weight": 0.75
    },

    {
        "id": "NORDOSTBAHNHOF",
        "name": "Nürnberg Nordostbahnhof",
        "type": "feeder_transit_node",
        "latitude": 49.47273122,
        "longitude": 11.10403930,
        "demand_weight": 0.70
    }

]


# =========================================================
# FINAL JSON
# =========================================================

demand_input = {

    "schema_version": "1.0",

    "service_date":
        SERVICE_DATE,

    "problem":
        "Express bus service under driver shortage",

    "hero_route": {

        "route_id":
            "X30",

        "name":
            "Nürnberg Hbf – Nordostpark Express",

        "origin":
            {
                "name": "Nürnberg Hbf",
                "vgn_stop_id": 510,
                "latitude": 49.44725111,
                "longitude": 11.08189
            },

        "destination":
            {
                "name": "Nürnberg Nordostpark",
                "gtfs_parent_stop_id":
                    "de:09564:1372",
                "latitude": 49.48914307,
                "longitude": 11.12585039
            },

        "prototype_one_way_minutes":
            X30_ONE_WAY_MINUTES,

        "service_window":
            {
                "start": MORNING_START,
                "end": MORNING_END
            },

        "design_principle":
            (
                "Do not assume additional vehicle or driver. "
                "Test reallocation or replacement of existing "
                "scheduled trips."
            )
    },


    "demand_input": {

        "method":
            "structural employment/transit proxy",

        "warning":
            (
                "These are prioritization weights, not "
                "measured passenger origin-destination counts."
            ),

        "hubs":
            demand_hubs
    },


    "observed_operations": {

        "morning_puls_departures":
            len(morning_rows),

        "observed_vehicles":
            len(vehicles),

        "verified_vehicle_returns":
            len(verified_returns),

        "verification_rate":
            round(
                len(verified_returns)
                / len(return_rows),
                3
            ),

        "operational_patterns":
            operational_patterns
    },


    "candidate_vehicle_windows":
        candidate_windows,


    "optimizer_constraints": {

        "new_drivers_assumed":
            0,

        "new_vehicles_assumed":
            0,

        "x30_one_way_minutes":
            X30_ONE_WAY_MINUTES,

        "do_not_treat_terminal_gap_as_idle":
            True,

        "preserve_existing_service":
            True,

        "validation_required":
            [
                "VAG vehicle circulation approval",
                "VAG driver duty approval",
                "legal/contractual break validation",
                "operational turnaround validation",
                "passenger impact of any replaced trip"
            ]
    },


    "objective": {

        "primary":
            "maximize access to Nordostpark during morning peak",

        "secondary":
            "minimize disruption to existing Line 43/44 service",

        "resource_constraint":
            "use existing operational resources"
    },


    "source_files": {

        "puls_morning":
            "data/processed/puls_morning_departures.json",

        "puls_trip_details":
            "data/processed/puls_trip_details.json",

        "puls_verified_returns":
            "data/processed/puls_return_legs_verified.json"

    }

}


# =========================================================
# WRITE FILE
# =========================================================

OUTPUT.parent.mkdir(
    parents=True,
    exist_ok=True
)

with open(
    OUTPUT,
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        demand_input,
        f,
        indent=2,
        ensure_ascii=False
    )


# =========================================================
# SUMMARY
# =========================================================

print("\n========================================")
print("DEMAND INPUT CREATED")
print("========================================")

print(
    f"Verified returns: "
    f"{len(verified_returns)}"
)

print(
    f"Candidate windows: "
    f"{len(candidate_windows)}"
)

print(
    f"Operational patterns: "
    f"{len(operational_patterns)}"
)

print(
    f"Demand hubs: "
    f"{len(demand_hubs)}"
)

print(
    f"Output: {OUTPUT}"
)