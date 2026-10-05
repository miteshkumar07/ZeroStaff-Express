import pandas as pd
from pathlib import Path

# =========================================================
# CONFIGURATION
# =========================================================

BASE = Path("data/raw/vgn_gtfs")
OUTPUT = Path("data/processed")

SERVICE_DATE = "2026-10-05"

TARGET_LINES = {
    "30",
    "31",
    "43",
    "44"
}

TIME_START = 6 * 60 + 30   # 06:30
TIME_END = 9 * 60           # 09:00


# =========================================================
# HELPER
# =========================================================

def gtfs_minutes(value):
    """
    Convert GTFS HH:MM:SS to minutes.
    GTFS can legally contain hours >= 24.
    """
    if pd.isna(value):
        return None

    h, m, s = map(int, str(value).split(":"))
    return h * 60 + m


# =========================================================
# PREPARE OUTPUT FOLDER
# =========================================================

OUTPUT.mkdir(parents=True, exist_ok=True)

print("Loading GTFS...")


# =========================================================
# LOAD FILES
# =========================================================

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
# 1. FIND SERVICES ACTIVE ON 2026-10-05
# =========================================================

print("\nFinding active services...")

target_date = pd.Timestamp(SERVICE_DATE)

weekday_names = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday"
]

weekday = weekday_names[target_date.weekday()]
date_str = target_date.strftime("%Y%m%d")

# Regular calendar service
active_calendar = calendar[
    (calendar["start_date"] <= date_str) &
    (calendar["end_date"] >= date_str) &
    (calendar[weekday] == "1")
].copy()

active_service_ids = set(
    active_calendar["service_id"]
)

# Apply calendar exceptions
exceptions = calendar_dates[
    calendar_dates["date"] == date_str
].copy()

for _, row in exceptions.iterrows():

    service_id = row["service_id"]
    exception_type = row["exception_type"]

    if exception_type == "1":
        # Service ADDED
        active_service_ids.add(service_id)

    elif exception_type == "2":
        # Service REMOVED
        active_service_ids.discard(service_id)

print(
    f"Active service IDs: {len(active_service_ids)}"
)


# =========================================================
# 2. SELECT BUS ROUTES 30/31/43/44
# =========================================================

bus_routes = routes[
    routes["route_type"] == "3"
].copy()

target_routes = bus_routes[
    bus_routes["route_short_name"]
    .astype(str)
    .isin(TARGET_LINES)
].copy()

print("\nTarget routes:")

print(
    target_routes[
        [
            "route_id",
            "route_short_name",
            "route_long_name"
        ]
    ].to_string(index=False)
)

target_route_ids = set(
    target_routes["route_id"]
)


# =========================================================
# 3. SELECT ACTIVE TARGET TRIPS
# =========================================================

target_trips = trips[
    trips["service_id"].isin(active_service_ids) &
    trips["route_id"].isin(target_route_ids)
].copy()

print(
    f"\nActive trips on Lines "
    f"{', '.join(sorted(TARGET_LINES))}: "
    f"{len(target_trips)}"
)


# =========================================================
# 4. KEEP ONLY STOP TIMES FOR TARGET TRIPS
# =========================================================

target_stop_times = stop_times[
    stop_times["trip_id"].isin(
        target_trips["trip_id"]
    )
].copy()

target_stop_times = target_stop_times.sort_values(
    [
        "trip_id",
        "stop_sequence"
    ]
)


# =========================================================
# 5. CALCULATE TRIP START / END
# =========================================================

trip_bounds = (
    target_stop_times
    .groupby("trip_id")
    .agg(
        start_time=(
            "departure_time",
            "first"
        ),

        end_time=(
            "arrival_time",
            "last"
        ),

        start_stop_id=(
            "stop_id",
            "first"
        ),

        end_stop_id=(
            "stop_id",
            "last"
        )
    )
    .reset_index()
)


# =========================================================
# 6. CONVERT TIMES
# =========================================================

trip_bounds["start_min"] = (
    trip_bounds["start_time"]
    .apply(gtfs_minutes)
)

trip_bounds["end_min"] = (
    trip_bounds["end_time"]
    .apply(gtfs_minutes)
)


# =========================================================
# 7. KEEP TRIPS THAT OPERATE DURING 06:30–09:00
# =========================================================

morning_trips = trip_bounds[
    (trip_bounds["end_min"] >= TIME_START) &
    (trip_bounds["start_min"] <= TIME_END)
].copy()

print(
    f"Morning trips 06:30–09:00: "
    f"{len(morning_trips)}"
)


# =========================================================
# 8. ADD ROUTE + TRIP INFORMATION
# =========================================================

morning_trips = morning_trips.merge(
    target_trips[
        [
            "trip_id",
            "route_id",
            "service_id",
            "trip_headsign",
            "direction_id",
            "block_id"
        ]
    ],
    on="trip_id",
    how="left"
)

morning_trips = morning_trips.merge(
    target_routes[
        [
            "route_id",
            "route_short_name",
            "route_long_name"
        ]
    ],
    on="route_id",
    how="left"
)


# =========================================================
# 9. ADD STOP NAMES + COORDINATES
# =========================================================

stop_lookup = stops[
    [
        "stop_id",
        "stop_name",
        "stop_lat",
        "stop_lon"
    ]
].drop_duplicates(
    "stop_id"
)

# Start stop
morning_trips = morning_trips.merge(
    stop_lookup.rename(
        columns={
            "stop_id": "start_stop_id",
            "stop_name": "start_stop_name",
            "stop_lat": "start_lat",
            "stop_lon": "start_lon"
        }
    ),
    on="start_stop_id",
    how="left"
)

# End stop
morning_trips = morning_trips.merge(
    stop_lookup.rename(
        columns={
            "stop_id": "end_stop_id",
            "stop_name": "end_stop_name",
            "stop_lat": "end_lat",
            "stop_lon": "end_lon"
        }
    ),
    on="end_stop_id",
    how="left"
)


# =========================================================
# 10. SORT
# =========================================================

morning_trips = morning_trips.sort_values(
    [
        "route_short_name",
        "direction_id",
        "start_min"
    ]
)


# =========================================================
# 11. SAVE CLEAN TRIP DATA
# =========================================================

trip_columns = [
    "trip_id",
    "route_id",
    "route_short_name",
    "route_long_name",
    "trip_headsign",
    "direction_id",
    "service_id",
    "block_id",

    "start_stop_id",
    "start_stop_name",
    "start_lat",
    "start_lon",
    "start_time",

    "end_stop_id",
    "end_stop_name",
    "end_lat",
    "end_lon",
    "end_time",

    "start_min",
    "end_min"
]

morning_trips[
    trip_columns
].to_csv(
    OUTPUT / "target_trips.csv",
    index=False,
    encoding="utf-8"
)


# =========================================================
# 12. SAVE CORRESPONDING STOP-TIME DATA
# =========================================================

target_trip_ids = set(
    morning_trips["trip_id"]
)

morning_stop_times = target_stop_times[
    target_stop_times["trip_id"].isin(
        target_trip_ids
    )
].copy()

morning_stop_times.to_csv(
    OUTPUT / "target_stop_times.csv",
    index=False,
    encoding="utf-8"
)


# =========================================================
# 13. SUMMARY
# =========================================================

print("\n======================================")
print("TARGET GTFS EXTRACTION COMPLETE")
print("======================================")

print(
    f"Service date: {SERVICE_DATE}"
)

print(
    f"Time window: 06:30–09:00"
)

print(
    f"Routes: "
    f"{', '.join(sorted(TARGET_LINES))}"
)

print(
    f"Target morning trips: "
    f"{len(morning_trips)}"
)

print(
    f"Target stop-time rows: "
    f"{len(morning_stop_times)}"
)

print("\nTrips by line:")

print(
    morning_trips[
        "route_short_name"
    ]
    .value_counts()
    .sort_index()
    .to_string()
)

print("\nSaved:")

print(
    OUTPUT / "target_trips.csv"
)

print(
    OUTPUT / "target_stop_times.csv"
)