import pandas as pd
from pathlib import Path

BASE = Path("data/raw/vgn_gtfs")

SERVICE_DATE = "2026-10-05"

MORNING_START = 6 * 60 + 30
MORNING_END = 10 * 60


def gtfs_minutes(value):
    if pd.isna(value):
        return None

    h, m, s = map(int, str(value).split(":"))
    return h * 60 + m


# =========================================================
# LOAD
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

print("GTFS loaded.")


# =========================================================
# ACTIVE SERVICE DATE
# =========================================================

target = pd.Timestamp(SERVICE_DATE)
date_str = target.strftime("%Y%m%d")

weekday_map = {
    0: "monday",
    1: "tuesday",
    2: "wednesday",
    3: "thursday",
    4: "friday",
    5: "saturday",
    6: "sunday"
}

weekday = weekday_map[target.weekday()]

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

    if row["exception_type"] == "1":
        active_service_ids.add(row["service_id"])

    elif row["exception_type"] == "2":
        active_service_ids.discard(row["service_id"])

print("Active service IDs:", len(active_service_ids))


# =========================================================
# BUS + BLOCK-ID TRIPS ONLY
# =========================================================

bus_routes = routes[
    routes["route_type"].astype(str) == "3"
].copy()

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

print("Active bus trips with block_id:", len(trips))


# =========================================================
# TRIP START / END
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
# IDENTIFY NÜRNBERG HBF STOP CLUSTER
# =========================================================

HBF_PARENT = "Parentde:09564:510"

hbf_stops = stops[
    (stops["parent_station"] == HBF_PARENT) |
    (stops["stop_id"].str.startswith("de:09564:510")) |
    (
        stops["stop_name"].str.strip().str.lower()
        == "nürnberg hbf"
    )
].copy()

hbf_stop_ids = set(hbf_stops["stop_id"])

print("Nürnberg Hbf stop IDs:", len(hbf_stop_ids))


# =========================================================
# ADD TIME
# =========================================================

trip_bounds["start_min"] = (
    trip_bounds["start_time"].apply(gtfs_minutes)
)

trip_bounds["end_min"] = (
    trip_bounds["end_time"].apply(gtfs_minutes)
)


# =========================================================
# SORT VEHICLE BLOCKS
# =========================================================

trip_bounds = trip_bounds.sort_values(
    ["block_id", "start_min"]
)


# =========================================================
# FIND ALL HBF → HBF GAPS
# =========================================================

gaps = []

for block_id, group in trip_bounds.groupby("block_id"):

    group = group.reset_index(drop=True)

    for i in range(len(group) - 1):

        prev = group.iloc[i]
        nxt = group.iloc[i + 1]

        prev_hbf = prev["end_stop_id"] in hbf_stop_ids
        next_hbf = nxt["start_stop_id"] in hbf_stop_ids

        if not prev_hbf or not next_hbf:
            continue

        if not (
            MORNING_START
            <= prev["end_min"]
            <= MORNING_END
        ):
            continue

        gap = nxt["start_min"] - prev["end_min"]

        if gap <= 0:
            continue

        gaps.append({
            "block_id": str(block_id),

            "previous_route":
                str(prev["route_short_name"]),

            "previous_trip":
                str(prev["trip_id"]),

            "previous_end":
                str(prev["end_time"]),

            "next_route":
                str(nxt["route_short_name"]),

            "next_trip":
                str(nxt["trip_id"]),

            "next_start":
                str(nxt["start_time"]),

            "gap_minutes":
                int(gap)
        })


# =========================================================
# RESULTS
# =========================================================

gaps_df = pd.DataFrame(gaps)

print("\n======================================")
print("HBF SLACK ANALYSIS")
print("======================================")

print("Total Hbf → Hbf gaps:", len(gaps_df))

if gaps_df.empty:
    print("NO block-based Hbf → Hbf gaps found.")
else:

    print("\nGap statistics:")
    print(gaps_df["gap_minutes"].describe())

    print("\nTop 30 longest gaps:")

    print(
        gaps_df
        .sort_values("gap_minutes", ascending=False)
        .head(30)
        .to_string(index=False)
    )

    print("\nGaps >= 20 minutes:",
          (gaps_df["gap_minutes"] >= 20).sum())

    print("Gaps >= 30 minutes:",
          (gaps_df["gap_minutes"] >= 30).sum())

    print("Gaps >= 35 minutes:",
          (gaps_df["gap_minutes"] >= 35).sum())

    print("Gaps >= 40 minutes:",
          (gaps_df["gap_minutes"] >= 40).sum())

    print("Gaps >= 42 minutes:",
          (gaps_df["gap_minutes"] >= 42).sum())

    gaps_df.to_csv(
        "data/processed/hbf_all_block_gaps.csv",
        index=False
    )

    print(
        "\nSaved:"
        " data/processed/hbf_all_block_gaps.csv"
    )