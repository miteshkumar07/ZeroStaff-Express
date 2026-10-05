import pandas as pd
from pathlib import Path

BASE = Path("data/raw/vgn_gtfs")

print("Loading GTFS...")

routes = pd.read_csv(BASE / "routes.txt", dtype=str)
trips = pd.read_csv(BASE / "trips.txt", dtype=str)
stops = pd.read_csv(BASE / "stops.txt", dtype=str)
stop_times = pd.read_csv(BASE / "stop_times.txt", dtype=str)

# --------------------------------------------------
# 1. BLOCK_ID HEALTH
# --------------------------------------------------

print("\n========== BLOCK ID ==========")

trips["block_id"] = trips["block_id"].replace(
    r"^\s*$",
    pd.NA,
    regex=True
)

total_trips = len(trips)
with_block = trips["block_id"].notna().sum()
without_block = total_trips - with_block

print(f"Total trips: {total_trips}")
print(f"Trips with block_id: {with_block}")
print(f"Trips without block_id: {without_block}")

if total_trips > 0:
    print(f"Block coverage: {with_block / total_trips * 100:.2f}%")

print(f"Unique block_ids: {trips['block_id'].nunique()}")

# --------------------------------------------------
# 2. BUS ROUTES
# --------------------------------------------------

print("\n========== BUS ROUTES ==========")

bus_routes = routes[
    routes["route_type"] == "3"
].copy()

print(f"Total bus routes in feed: {len(bus_routes)}")

print(
    bus_routes[
        bus_routes["route_short_name"].astype(str).isin(["30", "31", "43", "44"])
    ][
        [
            "route_id",
            "route_short_name",
            "route_long_name",
            "route_type"
        ]
    ].to_string(index=False)
)

# --------------------------------------------------
# 3. FIND HBF STOPS
# --------------------------------------------------

print("\n========== HBF STOPS ==========")

hbf = stops[
    stops["stop_name"].str.contains(
        "Hauptbahnhof|Hbf",
        case=False,
        na=False
    )
].copy()

print(f"Possible Hbf stops found: {len(hbf)}")

print(
    hbf[
        [
            "stop_id",
            "stop_name",
            "stop_lat",
            "stop_lon",
            "location_type",
            "parent_station"
        ]
    ].to_string(index=False)
)

# --------------------------------------------------
# 4. FIND NORDOSTPARK STOPS
# --------------------------------------------------

print("\n========== NORDOSTPARK STOPS ==========")

nordost = stops[
    stops["stop_name"].str.contains(
        "Nordostpark|Nordost|Herrnhütte",
        case=False,
        na=False
    )
].copy()

print(f"Possible Nordostpark/Herrnhütte stops: {len(nordost)}")

print(
    nordost[
        [
            "stop_id",
            "stop_name",
            "stop_lat",
            "stop_lon"
        ]
    ].to_string(index=False)
)

# --------------------------------------------------
# 5. ROUTES 30 / 31 DETAILS
# --------------------------------------------------

print("\n========== ROUTES 30 / 31 ==========")

target_routes = bus_routes[
    bus_routes["route_short_name"].astype(str).isin(["30", "31"])
].copy()

print(
    target_routes[
        [
            "route_id",
            "route_short_name",
            "route_long_name"
        ]
    ].to_string(index=False)
)

# --------------------------------------------------
# 6. TRIPS ON ROUTES 30 / 31
# --------------------------------------------------

target_route_ids = set(target_routes["route_id"])

target_trips = trips[
    trips["route_id"].isin(target_route_ids)
].copy()

print("\n========== TARGET TRIPS ==========")
print(f"Trips on 30/31: {len(target_trips)}")

print(
    target_trips[
        [
            "route_id",
            "service_id",
            "trip_id",
            "trip_headsign",
            "direction_id",
            "block_id"
        ]
    ].head(20).to_string(index=False)
)