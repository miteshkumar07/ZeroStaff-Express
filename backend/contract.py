"""Input contract (demand_input.json) -> normalised Python objects.

The optimizer never touches raw JSON: everything is parsed here into small dataclasses.
All times are integers = minutes after midnight of the service day (values > 1440 are
allowed for after-midnight service, GTFS style "25:10").

The parser is deliberately forgiving so the data pipeline can evolve without breaking the
solver: common aliases are accepted (see CONTRACT.md) and anything unknown is passed through.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_RULES: dict[str, Any] = {
    # --- labour rules -------------------------------------------------------------------
    "max_continuous_work_min": 240,   # no more than 4 h of work ...
    "min_break_min": 15,              # ... without a break of >= 15 consecutive minutes
    "max_shift_work_min": 600,        # ArbZG daily maximum working time (10 h)
    "arbzg_break_totals": True,       # ArbZG s.4: >6 h work -> 30 min breaks, >9 h -> 45 min ...
    "arbzg_min_piece_min": 15,        # ... counting only break pieces of >= 15 min
    # --- the Zero-New-Staff lock --------------------------------------------------------
    "max_new_drivers": 0,
    # --- trip reallocation (blocks marked with replace_id may be handed over to the express) ---
    "replacement_penalty": 600,       # objective cost per reallocated trip pair (disruption)
    "max_consecutive_replacements_per_group": 1,   # never cancel 2 departures in a row on a branch
    "max_replacements": None,         # optional global cap
    # --- operations ---------------------------------------------------------------------
    "min_layover_min": 5,             # turnaround at an express terminal
    "changeover_min": 5,              # swapping from the regular bus to the express bus
    "deadhead_speed_kmh": 25.0,       # empty transfer speed in city traffic
    "express_speed_kmh": 30.0,        # used only if a route has no travel_time_min
    "road_detour_factor": 1.3,        # straight-line km -> road km
    "default_deadhead_min": 15,       # used when a location has no coordinates
    "max_connection_wait_min": 120,   # longest wait between two express trips of one driver
                                      # (longer waits allowed into the first hour of a later window)
    "max_express_vehicles": None,     # optional fleet cap for the express line
    # --- timetable ----------------------------------------------------------------------
    "time_resolution_min": 5,         # candidate departure step for flexible timetables
    # --- KPI assumptions (only used for reporting) --------------------------------------
    "bus_capacity": 80,
    "annual_cost_per_driver_eur": 60000,
}


class ContractError(ValueError):
    """demand_input.json does not follow the contract."""


# ----------------------------------------------------------------------------------------
# time helpers
# ----------------------------------------------------------------------------------------
def parse_time(value: Any, what: str = "time") -> int:
    """'HH:MM', 'HH:MM:SS', ISO datetime or plain minutes -> minutes after midnight."""
    if isinstance(value, bool) or value is None:
        raise ContractError(f"Missing/invalid {what}: {value!r}")
    if isinstance(value, (int, float)):
        return int(round(value))
    if isinstance(value, str):
        s = value.strip()
        if "T" in s:
            s = s.split("T", 1)[1]
        s = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", s)
        parts = s.split(":")
        try:
            if len(parts) in (2, 3):
                h, m = int(parts[0]), int(parts[1])
                sec = float(parts[2]) if len(parts) == 3 else 0.0
                return h * 60 + m + (1 if sec >= 30 else 0)
            return int(round(float(s)))
        except ValueError:
            pass
    raise ContractError(f"Cannot parse {what}: {value!r} (expected 'HH:MM', 'HH:MM:SS' or minutes)")


def fmt_time(minutes: int) -> str:
    """minutes after midnight -> 'HH:MM' (hours may exceed 23, like GTFS)."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def iso_time(minutes: int, service_date: date) -> str:
    return (datetime.combine(service_date, datetime.min.time()) + timedelta(minutes=minutes)).isoformat()


# ----------------------------------------------------------------------------------------
# geo helpers
# ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Location:
    name: str
    lat: float | None = None
    lon: float | None = None
    stop_id: str | None = None

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lon is not None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "lat": self.lat, "lon": self.lon}
        if self.stop_id:
            out["stop_id"] = self.stop_id
        return out


def parse_location(obj: Any, fallback_name: str = "unknown") -> Location | None:
    if obj is None:
        return None
    if isinstance(obj, Location):
        return obj
    if isinstance(obj, str):
        return Location(name=obj)
    if isinstance(obj, (list, tuple)) and len(obj) == 2:
        return Location(name=fallback_name, lat=float(obj[0]), lon=float(obj[1]))
    if isinstance(obj, dict):
        lat = _first(obj, "lat", "latitude", "stop_lat", "y")
        lon = _first(obj, "lon", "lng", "longitude", "stop_lon", "x")
        name = _first(obj, "name", "stop_name", "label") or fallback_name
        stop_id = _first(obj, "stop_id", "id")
        return Location(
            name=str(name),
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            stop_id=str(stop_id) if stop_id is not None else None,
        )
    raise ContractError(f"Cannot parse location: {obj!r}")


def haversine_km(a: Location, b: Location) -> float:
    r = 6371.0088
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def same_place(a: Location | None, b: Location | None, radius_km: float = 0.3) -> bool:
    if a is None or b is None:
        return False
    if a.stop_id and b.stop_id and a.stop_id == b.stop_id:
        return True
    if a.has_coords and b.has_coords:
        return haversine_km(a, b) <= radius_km
    return a.name.strip().lower() == b.name.strip().lower()


def road_km(a: Location | None, b: Location | None, rules: dict[str, Any]) -> float | None:
    if a is None or b is None or not (a.has_coords and b.has_coords):
        return None
    return haversine_km(a, b) * float(rules["road_detour_factor"])


def travel_min(a: Location | None, b: Location | None, rules: dict[str, Any]) -> int:
    """Empty-bus transfer time between two places (0 if they are the same place)."""
    if same_place(a, b):
        return 0
    km = road_km(a, b, rules)
    if km is None:
        return int(rules["default_deadhead_min"])
    return int(math.ceil(km / float(rules["deadhead_speed_kmh"]) * 60))


# ----------------------------------------------------------------------------------------
# normalised problem objects
# ----------------------------------------------------------------------------------------
@dataclass
class Block:
    """Existing (fixed) piece of work, e.g. a run on regular line 36."""
    start: int
    end: int
    label: str
    kind: str = "regular_service"
    start_loc: Location | None = None
    end_loc: Location | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    replace_id: str | None = None       # blocks sharing an id may be reallocated together
    replace_group: str | None = None    # e.g. "44 -> Langwasser Mitte": headway protection group
    replace_penalty: float = 1.0        # relative disruption cost


@dataclass
class ReplaceOption:
    """Existing trips that may be handed over to the express line (trip reallocation)."""
    option_id: str
    driver_id: str
    blocks: list[Block]
    start: int                          # freed window starts with the first replaced block ...
    end: int                            # ... and lasts until the next remaining duty (or shift end)
    start_loc: Location | None
    end_loc: Location | None
    group: str | None
    penalty: float

    @property
    def minutes(self) -> int:
        return sum(b.end - b.start for b in self.blocks)


@dataclass
class Gap:
    """Idle time inside a shift: the raw material the optimizer turns into express service."""
    start: int
    end: int
    start_loc: Location | None = None   # where the driver is when the gap starts
    end_loc: Location | None = None     # where the driver must be when the gap ends

    @property
    def minutes(self) -> int:
        return self.end - self.start


@dataclass
class Driver:
    driver_id: str
    name: str
    shift_start: int
    shift_end: int
    blocks: list[Block]
    gaps: list[Gap]
    depot: Location | None = None
    available: bool = True
    is_new_hire: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    options: list[ReplaceOption] = field(default_factory=list)

    @property
    def work_minutes(self) -> int:
        return sum(b.end - b.start for b in self.blocks)


@dataclass
class ServiceWindow:
    start: int
    end: int
    headway_min: int
    label: str
    directions: list[str]
    weights: dict[str, float]
    max_headway_min: int
    min_headway_min: int


@dataclass
class Trip:
    trip_id: str
    route_id: str
    direction: str            # "outbound" (origin -> destination) | "inbound"
    dep: int
    arr: int
    origin: Location
    dest: Location
    weight: float             # demand weight used in the objective
    window: str

    @property
    def minutes(self) -> int:
        return self.arr - self.dep


@dataclass
class ExpressRoute:
    route_id: str
    name: str
    origin: Location
    destination: Location
    travel_time_min: int
    distance_km: float | None
    bidirectional: bool
    timetable_mode: str       # "fixed" | "flexible"
    candidate_step_min: int
    windows: list[ServiceWindow]
    trips: list[Trip]
    raw: dict[str, Any]


@dataclass
class Problem:
    meta: dict[str, Any]
    service_date: date
    rules: dict[str, Any]
    drivers: list[Driver]
    routes: list[ExpressRoute]
    warnings: list[str]
    map_layers: dict[str, Any]
    raw: dict[str, Any]

    @property
    def trips(self) -> list[Trip]:
        return [t for r in self.routes for t in r.trips]

    @property
    def trip_by_id(self) -> dict[str, Trip]:
        return {t.trip_id: t for t in self.trips}


# ----------------------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------------------
def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _weights(raw: Any, directions: list[str]) -> dict[str, float]:
    if raw is None:
        return {d: 1.0 for d in directions}
    if isinstance(raw, (int, float)):
        return {d: float(raw) for d in directions}
    if isinstance(raw, dict):
        return {d: float(raw.get(d, 1.0)) for d in directions}
    raise ContractError(f"demand_weight must be a number or {{direction: number}}, got {raw!r}")


def _parse_window(raw: dict[str, Any], bidirectional: bool, step: int) -> ServiceWindow:
    start = parse_time(_first(raw, "start", "from"), "service window start")
    end = parse_time(_first(raw, "end", "to"), "service window end")
    if end <= start:
        raise ContractError(f"Service window ends before it starts: {raw!r}")
    headway = int(_first(raw, "headway_min", "headway") or 15)
    directions = raw.get("directions") or (["outbound", "inbound"] if bidirectional else ["outbound"])
    max_hw = int(raw.get("max_headway_min") or headway)
    min_hw = int(raw.get("min_headway_min") or max(step, headway // 2))
    return ServiceWindow(
        start=start, end=end, headway_min=headway,
        label=str(raw.get("label") or f"{fmt_time(start)}-{fmt_time(end)}"),
        directions=list(directions),
        weights=_weights(_first(raw, "demand_weight", "weight"), list(directions)),
        max_headway_min=max_hw, min_headway_min=min_hw,
    )


def make_trip(route: ExpressRoute, direction: str, dep: int, weight: float, window: str) -> Trip:
    o, d = (route.origin, route.destination) if direction == "outbound" else (route.destination, route.origin)
    tag = "OUT" if direction == "outbound" else "IN"
    return Trip(
        trip_id=f"{route.route_id}-{tag}-{fmt_time(dep).replace(':', '')}",
        route_id=route.route_id, direction=direction, dep=dep, arr=dep + route.travel_time_min,
        origin=o, dest=d, weight=weight, window=window,
    )


def generate_trips(route: ExpressRoute, headway_override: int | None = None) -> list[Trip]:
    """Candidate express departures. Fixed mode: one every headway. Flexible: one every step."""
    explicit = route.raw.get("trips")
    if explicit and headway_override is None:
        trips = []
        for t in explicit:
            direction = t.get("direction", "outbound")
            trip = make_trip(route, direction, parse_time(t["departure"], "trip departure"),
                             float(t.get("demand_weight", 1.0)), str(t.get("window", "custom")))
            if t.get("trip_id"):
                trip.trip_id = str(t["trip_id"])
            trips.append(trip)
        unique: dict[str, Trip] = {}
        for trip in sorted(trips, key=lambda x: (x.dep, x.direction)):
            unique.setdefault(trip.trip_id, trip)
        return list(unique.values())

    trips: list[Trip] = []
    for w in route.windows:
        if route.timetable_mode == "flexible" and headway_override is None:
            step = route.candidate_step_min
        else:
            step = headway_override or w.headway_min
        for direction in w.directions:
            for dep in range(w.start, w.end, step):
                trips.append(make_trip(route, direction, dep, w.weights[direction], w.label))
    # de-duplicate (overlapping windows) while keeping order
    seen: dict[str, Trip] = {}
    for t in sorted(trips, key=lambda x: (x.dep, x.direction)):
        seen.setdefault(t.trip_id, t)
    return list(seen.values())


def _parse_route(raw: dict[str, Any], idx: int, rules: dict[str, Any]) -> ExpressRoute:
    route_id = str(_first(raw, "route_id", "id") or f"X{idx + 1}")
    origin = parse_location(_first(raw, "origin", "start", "from"), "Express origin")
    dest = parse_location(_first(raw, "destination", "end", "to"), "Express destination")
    if origin is None or dest is None:
        raise ContractError(f"express route {route_id}: origin and destination are required")
    dist = road_km(origin, dest, rules)
    tt = _first(raw, "travel_time_min", "run_time_min")
    if tt is None:
        if dist is None:
            raise ContractError(f"express route {route_id}: give travel_time_min or coordinates")
        tt = math.ceil(dist / float(rules["express_speed_kmh"]) * 60)
    bidirectional = bool(raw.get("bidirectional", True))
    mode = str(raw.get("timetable_mode", "fixed")).lower()
    if mode not in ("fixed", "flexible"):
        raise ContractError(f"express route {route_id}: timetable_mode must be 'fixed' or 'flexible'")
    step = int(raw.get("candidate_step_min") or rules["time_resolution_min"])
    windows_raw = raw.get("service_windows") or raw.get("windows") or []
    if not windows_raw and not raw.get("trips"):
        raise ContractError(f"express route {route_id}: needs service_windows (or explicit trips)")
    route = ExpressRoute(
        route_id=route_id,
        name=str(raw.get("name") or f"Express {route_id}"),
        origin=origin, destination=dest, travel_time_min=int(tt),
        distance_km=round(dist, 2) if dist is not None else raw.get("distance_km"),
        bidirectional=bidirectional, timetable_mode=mode, candidate_step_min=step,
        windows=[_parse_window(w, bidirectional, step) for w in windows_raw],
        trips=[], raw=raw,
    )
    seen_labels: dict[str, int] = {}
    for w in route.windows:
        n = seen_labels.get(w.label, 0)
        seen_labels[w.label] = n + 1
        if n:
            w.label = f"{w.label} ({n + 1})"
    route.trips = generate_trips(route)
    return route


def _parse_block(raw: dict[str, Any], driver_id: str) -> Block:
    start = parse_time(raw.get("start"), f"{driver_id} work block start")
    end = parse_time(raw.get("end"), f"{driver_id} work block end")
    if end <= start:
        raise ContractError(f"{driver_id}: work block ends before it starts: {raw!r}")
    line = _first(raw, "line", "route", "route_short_name")
    label = raw.get("label") or (f"Line {line}" if line is not None else "Regular service")
    known = {"start", "end", "label", "type", "kind", "start_location", "end_location",
             "from", "to", "start_stop", "end_stop", "replace_id", "replace_group", "replace_penalty"}
    return Block(
        start=start, end=end, label=str(label),
        kind=str(_first(raw, "type", "kind") or "regular_service"),
        start_loc=parse_location(_first(raw, "start_location", "from", "start_stop"), "block start"),
        end_loc=parse_location(_first(raw, "end_location", "to", "end_stop"), "block end"),
        meta={k: v for k, v in raw.items() if k not in known},
        replace_id=str(raw["replace_id"]) if raw.get("replace_id") is not None else None,
        replace_group=str(raw["replace_group"]) if raw.get("replace_group") is not None else None,
        replace_penalty=float(raw.get("replace_penalty", 1.0)),
    )


def _parse_driver(raw: dict[str, Any], idx: int, warnings: list[str]) -> Driver:
    driver_id = str(_first(raw, "driver_id", "id") or f"D{idx + 1:02d}")
    depot = parse_location(_first(raw, "depot", "home_location", "base"), "depot")
    blocks_raw = _first(raw, "work_blocks", "duties", "blocks") or []
    gaps_raw = _first(raw, "idle_gaps", "idle_times", "gaps") or []

    blocks = sorted((_parse_block(b, driver_id) for b in blocks_raw), key=lambda b: b.start)
    raw_gaps = []
    for g in gaps_raw:
        loc = parse_location(g.get("location"), "gap location")
        raw_gaps.append(Gap(
            start=parse_time(g.get("start"), f"{driver_id} idle gap start"),
            end=parse_time(g.get("end"), f"{driver_id} idle gap end"),
            start_loc=parse_location(_first(g, "start_location", "from"), "gap start") or loc,
            end_loc=parse_location(_first(g, "end_location", "to"), "gap end") or loc,
        ))
    raw_gaps.sort(key=lambda g: g.start)

    candidates = [b.start for b in blocks] + [g.start for g in raw_gaps]
    candidates_end = [b.end for b in blocks] + [g.end for g in raw_gaps]
    ss = raw.get("shift_start")
    se = raw.get("shift_end")
    shift_start = parse_time(ss, f"{driver_id} shift_start") if ss is not None else (min(candidates) if candidates else None)
    shift_end = parse_time(se, f"{driver_id} shift_end") if se is not None else (max(candidates_end) if candidates_end else None)
    if shift_start is None or shift_end is None or shift_end <= shift_start:
        raise ContractError(f"{driver_id}: needs shift_start/shift_end (or work_blocks / idle_gaps)")
    if candidates and min(candidates) < shift_start:
        warnings.append(f"{driver_id}: activity before shift_start - shift start moved to {fmt_time(min(candidates))}")
        shift_start = min(candidates)
    if candidates_end and max(candidates_end) > shift_end:
        warnings.append(f"{driver_id}: activity after shift_end - shift end moved to {fmt_time(max(candidates_end))}")
        shift_end = max(candidates_end)

    if blocks:
        merged: list[Block] = []
        for b in blocks:
            if merged and b.start < merged[-1].end:
                warnings.append(f"{driver_id}: overlapping work blocks at {fmt_time(b.start)} - merged")
                merged[-1].end = max(merged[-1].end, b.end)
                merged[-1].end_loc = b.end_loc or merged[-1].end_loc
                continue
            merged.append(b)
        blocks = merged
        if raw_gaps:
            warnings.append(f"{driver_id}: both work_blocks and idle_gaps given - gaps derived from work_blocks")
        gaps = []
        cursor, loc = shift_start, depot or blocks[0].start_loc
        for b in blocks:
            if b.start > cursor:
                gaps.append(Gap(cursor, b.start, loc, b.start_loc or loc))
            cursor, loc = max(cursor, b.end), b.end_loc or b.start_loc or loc
        if shift_end > cursor:
            gaps.append(Gap(cursor, shift_end, loc, depot or loc))
    else:
        gaps = []
        for g in raw_gaps:
            g.start, g.end = max(g.start, shift_start), min(g.end, shift_end)
            if g.end <= g.start:
                continue
            if gaps and g.start <= gaps[-1].end:
                if g.start < gaps[-1].end:
                    warnings.append(f"{driver_id}: overlapping idle gaps at {fmt_time(g.start)} - merged")
                if g.end > gaps[-1].end:
                    gaps[-1].end = g.end
                    gaps[-1].end_loc = g.end_loc or gaps[-1].end_loc
                continue
            gaps.append(g)
        if not gaps:  # reserve / standby driver: the whole shift is available
            gaps = [Gap(shift_start, shift_end, depot, depot)]
        blocks = []
        cursor, prev = shift_start, None
        for g in gaps:
            if g.start > cursor:
                blocks.append(Block(cursor, g.start, "Regular service",
                                    start_loc=prev.end_loc if prev else depot, end_loc=g.start_loc))
            cursor, prev = g.end, g
        if shift_end > cursor:
            blocks.append(Block(cursor, shift_end, "Regular service",
                                start_loc=prev.end_loc if prev else depot, end_loc=depot))

    return Driver(
        driver_id=driver_id,
        name=str(raw.get("name") or driver_id),
        shift_start=shift_start, shift_end=shift_end,
        blocks=blocks, gaps=gaps, depot=depot,
        options=_replace_options(driver_id, blocks, shift_end, depot, warnings),
        available=bool(raw.get("available_for_express", True)),
        meta={k: v for k, v in raw.items() if k not in {
            "driver_id", "id", "name", "shift_start", "shift_end", "work_blocks", "duties", "blocks",
            "idle_gaps", "idle_times", "gaps", "depot", "home_location", "base", "available_for_express"}},
    )


def _replace_options(driver_id: str, blocks: list[Block], shift_end: int, depot: Location | None,
                     warnings: list[str]) -> list[ReplaceOption]:
    """Group consecutive blocks that share a replace_id into one reallocation option."""
    by_id: dict[str, list[int]] = {}
    for i, b in enumerate(blocks):
        if b.replace_id:
            by_id.setdefault(b.replace_id, []).append(i)
    options = []
    for rid, idx in by_id.items():
        if idx != list(range(idx[0], idx[-1] + 1)):
            warnings.append(f"{driver_id}: replace_id {rid} blocks are not consecutive - option ignored")
            for i in idx:
                blocks[i].replace_id = None
            continue
        first, last = blocks[idx[0]], blocks[idx[-1]]
        nxt = blocks[idx[-1] + 1] if idx[-1] + 1 < len(blocks) else None
        options.append(ReplaceOption(
            option_id=rid, driver_id=driver_id, blocks=[blocks[i] for i in idx],
            start=first.start, end=nxt.start if nxt else shift_end,
            start_loc=first.start_loc, end_loc=(nxt.start_loc if nxt else depot) or last.end_loc,
            group=first.replace_group, penalty=first.replace_penalty,
        ))
    return options


MAP_LAYER_KEYS = ("demand_hubs", "employers", "job_hubs", "residential_zones", "regular_lines", "stops", "map")


def load_problem(source: str | Path | dict[str, Any], rule_overrides: dict[str, Any] | None = None) -> Problem:
    """Parse demand_input.json (path or already-loaded dict) into a Problem."""
    if isinstance(source, (str, Path)):
        with open(source, encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        raw = source
    if not isinstance(raw, dict):
        raise ContractError("demand_input.json must contain a JSON object")
    if "candidate_vehicle_windows" in raw and "drivers" not in raw:
        from .adapters import puls_to_contract   # real VAG PULS export -> contract v1
        raw = puls_to_contract(raw)

    rules = dict(DEFAULT_RULES)
    rules.update(raw.get("rules") or {})
    rules.update(rule_overrides or {})

    meta = dict(raw.get("meta") or {})
    try:
        service_date = date.fromisoformat(str(meta.get("service_date") or date.today().isoformat()))
    except ValueError as exc:
        raise ContractError(f"meta.service_date must be YYYY-MM-DD: {exc}") from exc

    routes_raw = raw.get("express_routes") or ([raw["express_route"]] if raw.get("express_route") else [])
    if not routes_raw:
        raise ContractError("demand_input.json needs 'express_route' (object) or 'express_routes' (list)")
    drivers_raw = raw.get("drivers") or []
    if not drivers_raw:
        raise ContractError("demand_input.json needs a non-empty 'drivers' list")

    warnings: list[str] = []
    routes = [_parse_route(r, i, rules) for i, r in enumerate(routes_raw)]
    drivers = [_parse_driver(d, i, warnings) for i, d in enumerate(drivers_raw)]
    ids = [d.driver_id for d in drivers]
    if len(set(ids)) != len(ids):
        raise ContractError("driver_id values must be unique")
    route_ids = [r.route_id for r in routes]
    if len(set(route_ids)) != len(route_ids):
        raise ContractError("express route_id values must be unique")

    return Problem(
        meta=meta, service_date=service_date, rules=rules, drivers=drivers, routes=routes,
        warnings=warnings, map_layers={k: raw[k] for k in MAP_LAYER_KEYS if k in raw}, raw=raw,
    )
