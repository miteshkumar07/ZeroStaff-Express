"""Generate a realistic-sized synthetic demand_input.json (stand-in until the real GTFS-based one lands).

    python -m backend.synthetic --drivers 40 --out data/demand_input.synthetic.json

Duty mix mirrors a typical German city-bus depot: early / late / middle shifts with a 30-min break,
split shifts with a long midday gap, peak-reinforcement duties and reserve (standby) drivers.
"""

from __future__ import annotations

import argparse
import json
import random

from .contract import fmt_time

STOPS = {
    "plaerrer": {"name": "Plaerrer", "lat": 49.4482, "lon": 11.0675},
    "doku": {"name": "Doku-Zentrum", "lat": 49.4313, "lon": 11.1120},
    "roethenbach": {"name": "Roethenbach", "lat": 49.4209, "lon": 11.0316},
    "nordostbf": {"name": "Nordostbahnhof", "lat": 49.4700, "lon": 11.1030},
    "depot": {"name": "Bus depot", "lat": 49.4300, "lon": 11.0450},
}
LINES = {  # line -> (terminal A, terminal B, one-way minutes)
    "36": ("plaerrer", "doku", 28),
    "65": ("roethenbach", "nordostbf", 38),
}


def _piece(start: int, end: int, line: str, at_a: bool) -> tuple[dict, bool]:
    a, b, one_way = LINES[line]
    legs = max(1, round((end - start) / (one_way + 6)))
    first, other = (a, b) if at_a else (b, a)
    last = first if legs % 2 == 0 else other
    blk = {"start": fmt_time(start), "end": fmt_time(end), "line": line,
           "start_location": STOPS[first], "end_location": STOPS[last]}
    return blk, (last == a)


def _duty(rng: random.Random, pieces: list[tuple[int, int]], line: str) -> list[dict]:
    blocks, at_a = [], rng.random() < 0.5
    for s, e in pieces:
        blk, at_a = _piece(s, e, line, at_a)
        blocks.append(blk)
    return blocks


def _r(rng: random.Random, lo: int, hi: int, step: int = 5) -> int:
    return rng.randrange(lo, hi + 1, step)


def generate(n_drivers: int = 40, seed: int = 42, headway: int = 15) -> dict:
    rng = random.Random(seed)
    mix = [("early", 0.25), ("late", 0.25), ("split", 0.15), ("middle", 0.15), ("peak", 0.1), ("reserve", 0.1)]
    kinds = []
    for kind, share in mix:
        kinds += [kind] * max(1, round(share * n_drivers))
    kinds = (kinds * 2)[:n_drivers]
    rng.shuffle(kinds)

    drivers = []
    for i, kind in enumerate(kinds):
        line = rng.choice(list(LINES))
        did = f"D{i + 1:02d}"
        if kind == "early":
            s = _r(rng, 285, 360)
            p1 = _r(rng, 150, 230)
            gap = _r(rng, 30, 60) if rng.random() < 0.7 else _r(rng, 60, 150)   # some long layovers
            p2 = _r(rng, 150, 220)
            pieces = [(s, s + p1), (s + p1 + gap, s + p1 + gap + p2)]
        elif kind == "late":
            s = _r(rng, 780, 900)
            p1 = _r(rng, 120, 200)
            gap = _r(rng, 30, 60) if rng.random() < 0.6 else _r(rng, 60, 140)
            p2 = _r(rng, 150, 230)
            pieces = [(s, s + p1), (s + p1 + gap, s + p1 + gap + p2)]
        elif kind == "middle":
            s = _r(rng, 510, 600)
            p1 = _r(rng, 160, 220)
            gap = _r(rng, 30, 45)
            p2 = _r(rng, 150, 220)
            pieces = [(s, s + p1), (s + p1 + gap, s + p1 + gap + p2)]
        elif kind == "split":
            s1 = _r(rng, 330, 390)
            e1 = s1 + _r(rng, 150, 210)
            s2 = _r(rng, 840, 930)
            e2 = s2 + _r(rng, 150, 200)
            pieces = [(s1, e1), (s2, e2)]
        elif kind == "peak":
            s1 = _r(rng, 345, 390)
            e1 = s1 + _r(rng, 90, 150)
            s2 = _r(rng, 870, 930)
            e2 = s2 + _r(rng, 90, 150)
            pieces = [(s1, e1), (s2, e2)]
        else:  # reserve / standby
            s = rng.choice([_r(rng, 330, 390), _r(rng, 780, 840)])
            drivers.append({"driver_id": did, "name": f"Reserve {did}", "duty_type": kind,
                            "shift_start": fmt_time(s), "shift_end": fmt_time(s + 480),
                            "idle_gaps": [{"start": fmt_time(s), "end": fmt_time(s + 480), "location": STOPS["depot"]}]})
            continue
        drivers.append({
            "driver_id": did, "name": f"{kind.title()} shift {did}, line {line}", "duty_type": kind,
            "shift_start": fmt_time(pieces[0][0]), "shift_end": fmt_time(pieces[-1][1]),
            "depot": STOPS["depot"], "work_blocks": _duty(rng, pieces, line),
        })

    return {
        "meta": {"scenario_name": f"Synthetic depot - {n_drivers} drivers, lines 36 & 65",
                 "service_date": "2026-10-06", "source": f"backend.synthetic seed={seed}",
                 "timezone": "Europe/Berlin", "contract_version": "1.0"},
        "rules": {"max_new_drivers": 0},
        "express_route": {
            "route_id": "X1", "name": "Hero Express X1: Langwasser Mitte <-> Nordostpark",
            "origin": {"name": "Langwasser Mitte", "lat": 49.4046, "lon": 11.1319},
            "destination": {"name": "Nordostpark", "lat": 49.4866, "lon": 11.1285},
            "travel_time_min": 25, "bidirectional": True, "timetable_mode": "fixed",
            "service_windows": [
                {"label": "AM peak", "start": "06:30", "end": "09:00", "headway_min": headway,
                 "demand_weight": {"outbound": 3, "inbound": 1}},
                {"label": "PM peak", "start": "15:30", "end": "18:30", "headway_min": headway,
                 "demand_weight": {"outbound": 1, "inbound": 3}},
            ],
        },
        "drivers": drivers,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drivers", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--headway", type=int, default=15)
    ap.add_argument("--out", default="data/demand_input.synthetic.json")
    a = ap.parse_args()
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(generate(a.drivers, a.seed, a.headway), fh, indent=2)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
