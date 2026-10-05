import pandas as pd
from pathlib import Path

BASE = Path("data/raw/vgn_gtfs")

for file in ["routes.txt", "trips.txt", "stop_times.txt", "stops.txt", "calendar.txt"]:
    path = BASE / file
    df = pd.read_csv(path, nrows=5)

    print("\n==============================")
    print(file)
    print("==============================")
    print(df.columns.tolist())
    print(df.head())