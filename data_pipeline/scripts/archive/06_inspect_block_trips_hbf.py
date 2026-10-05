import pandas as pd
from pathlib import Path

BASE = Path("data/raw/vgn_gtfs")

SERVICE_DATE = "2026-10-05"


# =========================================================
# LOAD
# =========================================================

print("Loading GTFS...")

routes = pd.read_csv(
    BASE / "routes.txt",
    dtype=str
)

trips = pd.read_csv(
    BASE / "trips.txt",
    dtype=str
)

stop_times = pd.read_csv(
    BASE / "stop_times.txt",
    dtype=str
)

stops = pd.read_csv(
    BASE / "stops.txt",
    dtype=str
)

calendar = pd.read_csv(
    BASE / "calendar.txt",
    dtype=str
)

calendar_dates = pd.read_csv(
    BASE / "calendar_dates.txt",
    dtype=str
)


# =========================================================
# ACTIVE SERVICE
# =========================================================

date = pd.Timestamp(SERVICE_DATE)
date_str = date.strftime("%Y%m%d")

weekday_names = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday"
]

weekday = weekday_names[date.weekday()]

active = calendar[
    (calendar["start_date"] <= date_str) &
    (calendar["end_date"] >= date_str) &
    (calendar[weekday] == "1")
]

active_service_ids = set(active["service_id"])

exceptions = calendar_dates[
    calendar_dates["date"] == date_str
]

for _, row in exceptions.iterrows():

    sid = row["service_id"]

    if row["exception_type"] == "1":
        active_service_ids.add(sid)

    elif row["exception_type"] == "2":
        active_service_ids.discard(sid)


# =========================================================
# BLOCK-TAGGED BUS TRIPS
# =========================================================

bus_routes = routes[
    routes["route_type"].astype(str) == "3"
].copy()

trips["block_id"] = trips["block_id"].replace(
    r"^\s*$",
    pd.NA,
    regex=True
)

blocked_trips = trips[
    trips["service_id"].isin(active_service_ids) &
    trips["route_id"].isin(bus_routes["route_id"]) &
    trips["block_id"].notna()
].copy()

print(
    f"Active block-tagged bus trips: {len(blocked_trips)}"
)

print(
    f"Vehicle blocks: {blocked_trips['block_id'].nunique()}"
)


# =========================================================
# NÜRNBERG HBF STOP IDS
# =========================================================

hbf_stops = stops[
    stops["stop_id"].str.startswith(
        "de:09564:510"
    )
].copy()

hbf_ids = set(hbf_stops["stop_id"])

print(
    f"Nürnberg Hbf stop IDs: {len(hbf_ids)}"
)


# =========================================================
# ONLY STOP TIMES FOR BLOCK TRIPS
# =========================================================

stop_times = stop_times[
    stop_times["trip_id"].isin(
        blocked_trips["trip_id"]
    )
].copy()

# Find actual Hbf observations
hbf_times = stop_times[
    stop_times["stop_id"].isin(hbf_ids)
].copy()

print(
    f"\nHbf stop-time records on block-tagged trips: "
    f"{len(hbf_times)}"
)


# =========================================================
# ADD TRIP / ROUTE INFO
# =========================================================

hbf_times = hbf_times.merge(
    blocked_trips[
        [
            "trip_id",
            "route_id",
            "block_id",
            "trip_headsign"
        ]
    ],
    on="trip_id",
    how="left"
)

hbf_times = hbf_times.merge(
    bus_routes[
        [
            "route_id",
            "route_short_name",
            "route_long_name"
        ]
    ],
    on="route_id",
    how="left"
)

hbf_times = hbf_times.merge(
    stops[
        [
            "stop_id",
            "stop_name"
        ]
    ],
    on="stop_id",
    how="left"
)

# Sort
hbf_times = hbf_times.sort_values(
    [
        "block_id",
        "departure_time",
        "trip_id"
    ]
)


# =========================================================
# RESULT
# =========================================================

print("\n========================================")
print("BLOCK-TAGGED TRIPS TOUCHING NÜRNBERG HBF")
print("========================================")

if hbf_times.empty:

    print(
        "NO block-tagged trip visits Nürnberg Hbf."
    )

else:

    summary = (
        hbf_times[
            [
                "block_id",
                "route_short_name",
                "trip_id",
                "trip_headsign",
                "stop_id",
                "stop_name",
                "arrival_time",
                "departure_time"
            ]
        ]
        .drop_duplicates()
        .sort_values(
            [
                "block_id",
                "arrival_time"
            ]
        )
    )

    print(
        summary.to_string(index=False)
    )

    print(
        "\nUnique vehicle blocks touching Hbf:",
        hbf_times["block_id"].nunique()
    )

    print(
        "Unique trips touching Hbf:",
        hbf_times["trip_id"].nunique()
    )

    print(
        "\nRoutes touching Hbf:"
    )

    route_summary = (
        hbf_times[
            [
                "route_short_name",
                "route_id",
                "route_long_name",
                "block_id"
            ]
        ]
        .drop_duplicates()
        .sort_values("route_short_name")
    )

    print(
        route_summary.to_string(index=False)
    )

    summary.to_csv(
        "data/processed/block_trips_touching_hbf.csv",
        index=False
    )

    print(
        "\nSaved:"
        " data/processed/block_trips_touching_hbf.csv"
    )