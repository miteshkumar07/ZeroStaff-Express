import pandas as pd
from pathlib import Path

BASE = Path("data/raw/vgn_gtfs")

routes = pd.read_csv(
    BASE / "routes.txt",
    dtype=str
)

trips = pd.read_csv(
    BASE / "trips.txt",
    dtype=str
)

# Clean block_id
trips["block_id"] = trips["block_id"].replace(
    r"^\s*$",
    pd.NA,
    regex=True
)

# Only trips with block_id
blocked = trips[
    trips["block_id"].notna()
].copy()

# Add route information
blocked = blocked.merge(
    routes[
        [
            "route_id",
            "route_short_name",
            "route_long_name",
            "route_type"
        ]
    ],
    on="route_id",
    how="left"
)

print("\n========== BLOCK-ID TRIPS ==========")
print(f"Trips with block_id: {len(blocked)}")
print(f"Unique block_ids: {blocked['block_id'].nunique()}")

print("\n========== ROUTE TYPES ==========")
print(
    blocked["route_type"]
    .value_counts()
    .sort_index()
)

print("\nRoute type meanings:")
print("1 = U-Bahn")
print("3 = Bus")

# Bus only
bus_blocked = blocked[
    blocked["route_type"] == "3"
].copy()

print("\n========== BUS TRIPS WITH BLOCK_ID ==========")
print(f"Bus trips with block_id: {len(bus_blocked)}")
print(f"Bus vehicle blocks: {bus_blocked['block_id'].nunique()}")

# Summary by route
route_summary = (
    bus_blocked
    .groupby(
        [
            "route_short_name",
            "route_id",
            "route_long_name"
        ],
        dropna=False
    )
    .agg(
        block_trips=("trip_id", "count"),
        unique_blocks=("block_id", "nunique")
    )
    .reset_index()
    .sort_values(
        ["block_trips", "unique_blocks"],
        ascending=False
    )
)

print("\n========== BUS ROUTES WITH BLOCK DATA ==========")

print(
    route_summary
    .head(50)
    .to_string(index=False)
)

# Check our intended routes
print("\n========== CHECK 30 / 31 / 43 / 44 ==========")

target = route_summary[
    route_summary["route_short_name"]
    .astype(str)
    .isin(["30", "31", "43", "44"])
]

if target.empty:
    print("NONE of 30/31/43/44 have block_id data.")
else:
    print(
        target.to_string(index=False)
    )

# Export
route_summary.to_csv(
    "data/processed/routes_with_block_data.csv",
    index=False
)

print(
    "\nSaved: "
    "data/processed/routes_with_block_data.csv"
)