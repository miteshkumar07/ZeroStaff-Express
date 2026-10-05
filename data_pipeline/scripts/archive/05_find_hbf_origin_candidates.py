import pandas as pd
from pathlib import Path

BASE = Path("data/raw/vgn_gtfs")

SERVICE_DATE = "2026-10-05"

MORNING_START = 6 * 60 + 30
MORNING_END = 9 * 60


def gtfs_minutes(value):
    if pd.isna(value):
        return None

    h, m, s = map(int, str(value).split(":"))
    return h * 60 + m


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

# ---------------------------------------------------------
# ACTIVE SERVICE
# ---------------------------------------------------------

target = pd.Timestamp(SERVICE_DATE)
date_str = target.strftime("%Y%m%d")

weekday_names = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday"
]

weekday = weekday_names[target.weekday()]

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


# ---------------------------------------------------------
# BUS + BLOCK TRIPS
# ---------------------------------------------------------

bus_routes = routes[
    routes["route_type"].astype(str) == "3"
]

trips["block_id"] = trips["block_id"].replace(
    r"^\s*$",
    pd.NA,
    regex=True
)

trips = trips[
    trips["service_id"].isin(active_service_ids) &
    trips["route_id"].isin(bus_routes["route_id"]) &
    trips["block_id"].notna()
].copy()

print("Active block-tagged bus trips:", len(trips))


# ---------------------------------------------------------
# TRIP BOUNDS
# ---------------------------------------------------------

stop_times = stop_times[
    stop_times["trip_id"].isin(trips["trip_id"])
].copy()

stop_times = stop_times.sort_values(
    ["trip_id", "stop_sequence"]
)

bounds = (
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

bounds = bounds.merge(
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

bounds = bounds.merge(
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

# ---------------------------------------------------------
# STOP NAMES
# ---------------------------------------------------------

stop_lookup = stops[
    [
        "stop_id",
        "stop_name",
        "stop_lat",
        "stop_lon"
    ]
].drop_duplicates("stop_id")

bounds = bounds.merge(
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

bounds = bounds.merge(
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

bounds["start_min"] = bounds["start_time"].apply(gtfs_minutes)
bounds["end_min"] = bounds["end_time"].apply(gtfs_minutes)


# ---------------------------------------------------------
# NÜRNBERG HBF
# ---------------------------------------------------------

hbf_ids = set(
    stops[
        stops["stop_id"].str.startswith("de:09564:510")
    ]["stop_id"]
)

# ---------------------------------------------------------
# BUILD BLOCK CHAINS
# ---------------------------------------------------------

bounds = bounds.sort_values(
    ["block_id", "start_min"]
)

candidates = []

for block_id, group in bounds.groupby("block_id"):

    group = group.reset_index(drop=True)

    for i in range(len(group) - 1):

        current = group.iloc[i]
        nxt = group.iloc[i + 1]

        # Current trip MUST end at Nürnberg Hbf
        if current["end_stop_id"] not in hbf_ids:
            continue

        # Morning peak
        if not (
            MORNING_START
            <= current["end_min"]
            <= MORNING_END
        ):
            continue

        gap = nxt["start_min"] - current["end_min"]

        if gap <= 0:
            continue

        candidates.append({
            "service_date": SERVICE_DATE,
            "vehicle_block_id": str(block_id),

            "hbf_arrival": str(current["end_time"]),
            "next_trip_start": str(nxt["start_time"]),

            "gap_minutes": int(gap),

            "previous_route":
                str(current["route_short_name"]),

            "previous_trip":
                str(current["trip_id"]),

            "next_route":
                str(nxt["route_short_name"]),

            "next_trip":
                str(nxt["trip_id"]),

            "next_start_stop_id":
                str(nxt["start_stop_id"]),

            "next_start_stop":
                str(nxt["start_stop_name"]),

            "next_trip_headsign":
                str(nxt["trip_headsign"])
        })


# ---------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------

df = pd.DataFrame(candidates)

print("\n================================")
print("HBF ORIGIN CANDIDATES")
print("================================")

print(
    "Vehicle blocks reaching Hbf:",
    df["vehicle_block_id"].nunique()
    if not df.empty else 0
)

print(
    "Candidate windows:",
    len(df)
)

if not df.empty:

    print("\nTop candidates:")

    print(
        df.sort_values(
            "gap_minutes",
            ascending=False
        )
        .head(30)
        .to_string(index=False)
    )

    print("\nGap counts:")

    for threshold in [10, 15, 20, 25, 30, 35, 40]:
        count = (df["gap_minutes"] >= threshold).sum()

        print(
            f">= {threshold} minutes: {count}"
        )

df.to_csv(
    "data/processed/hbf_origin_candidates.csv",
    index=False
)

print(
    "\nSaved:"
    " data/processed/hbf_origin_candidates.csv"
)