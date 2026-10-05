"""Adapters from data-pipeline exports to the optimizer contract (CONTRACT.md, v1.0).

puls_to_contract: the real VAG PULS export (X30 / Nordostpark, built by 12_build_demand_input.py):
  * every observed bus becomes one duty (bus + its driver), built from its verified circulation;
  * every candidate vehicle window (Hbf -> terminal -> Hbf with a verified same-vehicle return) becomes
    a pair of *replaceable* blocks: the optimizer may hand that round trip over to the express line if
    the bus is back at Hbf for its next scheduled departure (trip reallocation, not "idle time");
  * branches (line + terminal) are protection groups: never cancel two departures in a row.

    python -m backend.adapters demand_input.json -o data/demand_input.real.json
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from .contract import fmt_time, parse_time

# counter-peak (Nordostpark -> Hbf) trips in the morning mostly reposition the bus
INBOUND_WEIGHT_FACTOR = 0.25


def _loc(raw: dict[str, Any] | None, name: str | None = None) -> dict[str, Any] | None:
    if not raw and not name:
        return None
    raw = raw or {}
    out = {"name": raw.get("name") or name}
    lat = raw.get("lat", raw.get("latitude"))
    lon = raw.get("lon", raw.get("longitude"))
    if lat is not None and lon is not None:
        out["lat"], out["lon"] = float(lat), float(lon)
    sid = raw.get("stop_id") or raw.get("vgn_stop_id") or raw.get("gtfs_parent_stop_id")
    if sid is not None:
        out["stop_id"] = str(sid)
    return out


def _short(name: str) -> str:
    return name.replace(" (Nürnberg)", "").replace("Nürnberg ", "")


def puls_to_contract(raw: dict[str, Any], *, max_headway_min: int = 30, min_headway_min: int = 10) -> dict[str, Any]:
    hero = raw["hero_route"]
    cons = raw.get("optimizer_constraints") or {}
    hubs = (raw.get("demand_input") or {}).get("hubs") or []
    origin, dest = _loc(hero["origin"]), _loc(hero["destination"])
    dest_weight = next((float(h.get("demand_weight", 1.0)) for h in hubs
                        if h.get("id") == "NORDOSTPARK" or h.get("name") == dest["name"]), 1.0)
    one_way = int(hero.get("prototype_one_way_minutes") or cons.get("x30_one_way_minutes") or 18)
    window = hero.get("service_window") or {"start": "06:30", "end": "09:00"}

    by_bus: dict[str, list[dict[str, Any]]] = {}
    for c in raw.get("candidate_vehicle_windows") or []:
        if c.get("status", "candidate") != "candidate":
            continue
        by_bus.setdefault(str(c["vehicle_number"]), []).append(c)

    drivers = []
    for bus, cands in sorted(by_bus.items()):
        cands.sort(key=lambda c: parse_time(c["hbf_departure"]))
        blocks, next_hbf = [], []
        for c in cands:
            line, term = str(c["line"]), c["terminal"]
            term_loc = _loc(None, term)
            group = f"Line {line} -> {_short(term)}"
            rid = str(c["existing_fahrtnummer"])
            arr = parse_time(c["terminal_arrival"])
            next_hbf.append(arr + int(round(float(c["terminal_to_next_hbf_minutes"]))))
            blocks.append({"start": fmt_time(parse_time(c["hbf_departure"])), "end": fmt_time(arr), "line": line,
                           "label": f"Line {line} Hbf -> {_short(term)}", "start_location": origin,
                           "end_location": term_loc, "fahrtnummer": rid,
                           "replace_id": rid, "replace_group": group})
            blocks.append({"start": fmt_time(parse_time(c["verified_return_departure"])),
                           "end": fmt_time(parse_time(c["verified_return_hbf_arrival"])), "line": line,
                           "label": f"Line {line} {_short(term)} -> Hbf", "start_location": term_loc,
                           "end_location": origin, "fahrtnummer": str(c.get("verified_return_fahrtnummer")),
                           "replace_id": rid, "replace_group": group})
        lines = sorted({str(c["line"]) for c in cands})
        drivers.append({
            "driver_id": f"BUS-{bus}",
            "name": f"Bus {bus} (Line {'/'.join(lines)}) + its driver",
            "shift_start": blocks[0]["start"],
            "shift_end": fmt_time(max(next_hbf)),      # back at Hbf for the next scheduled departure
            "depot": origin,
            "work_blocks": blocks,
            "vehicle_number": bus,
            "observation_window_only": True,           # duty outside this window is not in the data
        })

    patterns = (raw.get("observed_operations") or {}).get("operational_patterns") or []
    return {
        "meta": {
            "scenario_name": f"{hero['route_id']} {hero.get('name', '')} - real VAG PULS data",
            "service_date": raw.get("service_date"),
            "source": "VAG PULS API (morning departures, Fahrtverlauf, verified return legs) + VGN GTFS",
            "contract_version": "1.0",
            "mode": "trip_reallocation",
            "observed_operations": {k: v for k, v in (raw.get("observed_operations") or {}).items()
                                    if k != "operational_patterns"},
            "operational_patterns": patterns,
            "objective": raw.get("objective"),
            "validation_required": cons.get("validation_required"),
            "assumptions": [
                "Each bus keeps its own driver; driver duty outside the observed window is not in the data.",
                "A reallocated round trip must bring the bus back to Hbf before its next scheduled departure.",
                f"X30 one-way {one_way} min, 5 min turnaround at Nordostpark, 2 min to change destination.",
                f"Counter-peak trips (Nordostpark -> Hbf) weighted {INBOUND_WEIGHT_FACTOR} of peak direction.",
                f"X30 target: a departure at least every {max_headway_min} min, never closer than "
                f"{min_headway_min} min.",
                "Demand weights are structural proxies, not measured passenger OD counts.",
            ],
        },
        "rules": {
            "max_new_drivers": int(cons.get("new_drivers_assumed", 0)),
            "changeover_min": 2,
            "min_layover_min": 5,
            "max_consecutive_replacements_per_group": 1,
        },
        "express_route": {
            "route_id": hero["route_id"],
            "name": hero.get("name") or hero["route_id"],
            "origin": origin, "destination": dest,
            "travel_time_min": one_way,
            "bidirectional": True,
            "timetable_mode": "flexible",
            "candidate_step_min": 1,
            "service_windows": [{
                "label": "AM peak", "start": window["start"], "end": window["end"],
                "headway_min": max_headway_min, "max_headway_min": max_headway_min,
                "min_headway_min": min_headway_min,
                "demand_weight": {"outbound": dest_weight, "inbound": round(dest_weight * INBOUND_WEIGHT_FACTOR, 3)},
            }],
        },
        "drivers": drivers,
        "demand_hubs": [{**_loc(h), "id": h.get("id"), "type": h.get("type"), "demand_weight": h.get("demand_weight")}
                        for h in hubs],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert the VAG PULS export to the optimizer contract")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="data/demand_input.real.json")
    ap.add_argument("--max-headway", type=int, default=30)
    a = ap.parse_args()
    with open(a.input, encoding="utf-8") as fh:
        raw = json.load(fh)
    out = puls_to_contract(raw, max_headway_min=a.max_headway)
    with open(a.output, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print(f"wrote {a.output}: {len(out['drivers'])} buses, "
          f"{sum(1 for d in out['drivers'] for b in d['work_blocks']) // 2} reallocation candidates")


if __name__ == "__main__":
    main()
