import pandas as pd
from pathlib import Path

BASE = Path("data/raw/vgn_gtfs")

# =========================================================
# CONFIG
# =========================================================

SERVICE_DATE = "2026-10-05"

# X30 prototype requirement
BOARDING_MIN = 2
ONE_WAY_MIN = 18
RETURN_MIN = 18
BUFFER_MIN = 4

REQUIRED_MIN = (
    BOARDING_MIN
    + ONE_WAY_MIN
    + RETURN_MIN
    + BUFFER_MIN
)

MORNING_START = 6 * 60 + 30   # 06:30
MORNING_END = 9 * 60           # 09:00


# =========================================================
# HELPERS
# =========================================================

def gtfs_minutes(value):
    if pd.isna(value):
        return None

    value = str(value)

    h, m, s = map(int, value.split(":"))

    return h * 60 + m


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
# 1. ACTIVE SERVICE ON 2026-10-05
# =========================================================

print("\nFinding active Monday service...")

target = pd.Timestamp(SERVICE_DATE)

weekday_map = {
    0: "monday",
    1: "tuesday",
    2: "wednesday",
    3: "thursday",
    4: "friday",
    5: "saturday",
    6: "sunday"
}

weekday_col = weekday_map[target.weekday()]

date_str = target.strftime("%Y%m%d")

active = calendar[
    (calendar["start_date"] <= date_str)
    & (calendar["end_date"] >= date_str)
    & (calendar[weekday_col] == "1")
]

active_service_ids = set(active["service_id"])


# Apply calendar exceptions
exceptions = calendar_dates[
    calendar_dates["date"] == date_str
]

for _, row in exceptions.iterrows():

    service_id = row["service_id"]
    exception_type = row["exception_type"]

    if exception_type == "1":
        active_service_ids.add(service_id)

    elif exception_type == "2":
        active_service_ids.discard(service_id)


print(
    f"Active service IDs: {len(active_service_ids)}"
)


# =========================================================
# 2. ONLY BUS TRIPS WITH BLOCK_ID
# =========================================================

routes["route_type"] = routes["route_type"].astype(str)

bus_routes = routes[
    routes["route_type"] == "3"
].copy()

bus_route_ids = set(bus_routes["route_id"])

trips["block_id"] = trips["block_id"].replace(
    r"^\s*$",
    pd.NA,
    regex=True
)

trips = trips[
    trips["route_id"].isin(bus_route_ids)
    & trips["service_id"].isin(active_service_ids)
    & trips["block_id"].notna()
].copy()

print("\n==============================")
print("BLOCK DATA")
print("==============================")

print("Active bus trips with block_id:", len(trips))
print("Unique vehicle blocks:", trips["block_id"].nunique())


# =========================================================
# 3. BUILD START / END OF EACH TRIP
# =========================================================

stop_times = stop_times[
    stop_times["trip_id"].isin(trips["trip_id"])
].copy()

stop_times = stop_times.sort_values(
    ["trip_id", "stop_sequence"]
)

trip_bounds = (
    stop_times
    .groupby("trip_id")
    .agg(
        start_time=("departure_time", "first"),
        end_time=("arrival_time", "last"),
        start_stop_id=("stop_id", "first"),
        end_stop_id=("stop_id", "last")
    )
    .reset_index()
)

trip_bounds = trip_bounds.merge(
    trips[
        [
            "trip_id",
            "route_id",
            "block_id",
            "trip_headsign"
        ]
    ],
    on="trip_id",
    how="inner"
)

# Add route names
trip_bounds = trip_bounds.merge(
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


# =========================================================
# 4. ADD STOP NAMES
# =========================================================

stop_lookup = stops[
    [
        "stop_id",
        "stop_name",
        "stop_lat",
        "stop_lon"
    ]
].drop_duplicates("stop_id")


trip_bounds = trip_bounds.merge(
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

trip_bounds = trip_bounds.merge(
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
# 5. IDENTIFY NÜRNBERG HBF STOPS
# =========================================================

# We specifically use the Nürnberg stop-code prefix
# rather than every stop containing "Hbf".

hbf_stops = stops[
    stops["stop_id"].str.startswith("de:09564:510")
].copy()

hbf_stop_ids = set(hbf_stops["stop_id"])

print("\n==============================")
print("NÜRNBERG HBF")
print("==============================")

print("Hbf stop IDs:", len(hbf_stop_ids))

print(
    hbf_stops[
        [
            "stop_id",
            "stop_name",
            "stop_lat",
            "stop_lon"
        ]
    ].to_string(index=False)
)


# =========================================================
# 6. CONVERT TIMES
# =========================================================

trip_bounds["start_min"] = (
    trip_bounds["start_time"].apply(gtfs_minutes)
)

trip_bounds["end_min"] = (
    trip_bounds["end_time"].apply(gtfs_minutes)
)


# =========================================================
# 7. CHAIN VEHICLE BLOCKS
# =========================================================

trip_bounds = trip_bounds.sort_values(
    ["block_id", "start_min"]
)

candidate_windows = []

for block_id, group in trip_bounds.groupby("block_id"):

    group = group.reset_index(drop=True)

    for i in range(len(group) - 1):

        prev = group.iloc[i]
        nxt = group.iloc[i + 1]

        # VERY IMPORTANT:
        # previous trip must END at Hbf
        # AND next trip must START at Hbf

        previous_at_hbf = (
            prev["end_stop_id"] in hbf_stop_ids
        )

        next_at_hbf = (
            nxt["start_stop_id"] in hbf_stop_ids
        )

        if not previous_at_hbf:
            continue

        if not next_at_hbf:
            continue

        # Morning window
        if not (
            MORNING_START
            <= prev["end_min"]
            <= MORNING_END
        ):
            continue

        gap = (
            nxt["start_min"]
            - prev["end_min"]
        )

        if gap < REQUIRED_MIN:
            continue

        candidate_windows.append(
            {
                "service_date": SERVICE_DATE,

                "vehicle_block_id": str(block_id),

                "location": "Nürnberg Hauptbahnhof",

                "available_from": prev["end_time"],
                "available_until": nxt["start_time"],

                "gap_minutes": int(gap),

                "required_x30_minutes": REQUIRED_MIN,

                "remaining_buffer_minutes":
                    int(gap - REQUIRED_MIN),

                "preceding_trip_id":
                    str(prev["trip_id"]),

                "preceding_route":
                    str(prev["route_short_name"]),

                "following_trip_id":
                    str(nxt["trip_id"]),

                "following_route":
                    str(nxt["route_short_name"]),

                "preceding_headsign":
                    str(prev["trip_headsign"]),

                "following_headsign":
                    str(nxt["trip_headsign"]),

                "confidence":
                    "GTFS_vehicle_block_candidate"
            }
        )


# =========================================================
# 8. RESULTS
# =========================================================

print("\n==============================")
print("RESULT")
print("==============================")

print(
    "Hbf → Hbf X30-compatible windows:",
    len(candidate_windows)
)

for item in candidate_windows:

    print(
        f"\nBlock: {item['vehicle_block_id']}"
    )

    print(
        f"Route before: {item['preceding_route']}"
    )

    print(
        f"Route after: {item['following_route']}"
    )

    print(
        f"Window: "
        f"{item['available_from']} → "
        f"{item['available_until']}"
    )

    print(
        f"Gap: {item['gap_minutes']} min"
    )

    print(
        f"Remaining buffer: "
        f"{item['remaining_buffer_minutes']} min"
    )


# =========================================================
# 9. SAVE
# =========================================================

output_df = pd.DataFrame(candidate_windows)

output_df.to_csv(
    "data/processed/candidate_hbf_windows.csv",
    index=False
)

print(
    "\nSaved:"
    " data/processed/candidate_hbf_windows.csv"
)