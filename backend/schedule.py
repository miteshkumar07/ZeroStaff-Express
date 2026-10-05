"""Solver result -> optimized_schedule.json (the output contract the dashboard reads)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import ortools

from .contract import Driver, Location, Problem, Trip, fmt_time, iso_time, same_place
from .model import Activity, SolveResult, describe_violations

CONTRACT_VERSION = "1.0"
LONG_IDLE_MIN = 60          # interior free time longer than this is shown as "idle" instead of "break"
WORK_TYPES = ("regular_service", "express", "deadhead")


def _loc(loc: Location | None) -> dict[str, Any] | None:
    return loc.to_json() if loc else None


def _seg(problem: Problem, kind: str, start: int, end: int, label: str, **extra: Any) -> dict[str, Any]:
    seg = {
        "type": kind, "label": label,
        "start": fmt_time(start), "end": fmt_time(end),
        "start_min": start, "end_min": end, "duration_min": end - start,
        "start_iso": iso_time(start, problem.service_date), "end_iso": iso_time(end, problem.service_date),
    }
    seg.update({k: v for k, v in extra.items() if v is not None})
    return seg


def _timeline(problem: Problem, d: Driver, added: list[Activity],
              replaced: set[str] | None = None) -> list[dict[str, Any]]:
    """Regular blocks (minus reallocated ones) + express activities, free time in between classified."""
    min_break = int(problem.rules["min_break_min"])
    replaced = replaced or set()
    items: list[tuple[int, int, dict[str, Any]]] = []
    for b in d.blocks:
        if b.replace_id and b.replace_id in replaced:
            continue
        items.append((b.start, b.end, _seg(problem, "regular_service", b.start, b.end, b.label, duty_type=b.kind,
                                            **{"from": _loc(b.start_loc), "to": _loc(b.end_loc),
                                               **({"meta": b.meta} if b.meta else {})})))
    for a in added:
        if a.kind == "express":
            t = a.trip
            label = f"{t.route_id} {t.origin.name} -> {t.dest.name}"
            extra = {"trip_id": t.trip_id, "route_id": t.route_id, "direction": t.direction}
        else:
            to_name = a.to_loc.name if a.to_loc else "next duty"
            label = f"Bus changeover at {to_name}" if same_place(a.from_loc, a.to_loc) else f"Transfer to {to_name}"
            extra = {}
        items.append((a.start, a.end, _seg(problem, a.kind, a.start, a.end, label,
                                            **{"from": _loc(a.from_loc), "to": _loc(a.to_loc)}, **extra)))
    items.sort(key=lambda it: (it[0], it[1]))

    out: list[dict[str, Any]] = []
    cursor = d.shift_start
    window_only = bool(d.meta.get("observation_window_only"))
    where = d.depot.name if d.depot else None
    for idx, (s, e, seg) in enumerate(items):
        if s > cursor:
            out.append(_free(problem, cursor, s, leading=not any(o["type"] in WORK_TYPES for o in out),
                             trailing=False, min_break=min_break, window_only=window_only, where=where))
        out.append(seg)
        cursor = max(cursor, e)
    if d.shift_end > cursor:
        out.append(_free(problem, cursor, d.shift_end, leading=not items, trailing=True, min_break=min_break,
                         window_only=window_only, where=where))
    return out


def _free(problem: Problem, s: int, e: int, *, leading: bool, trailing: bool, min_break: int,
          window_only: bool = False, where: str | None = None) -> dict[str, Any]:
    if (leading or trailing) and window_only:
        # duty continues outside the observed window: this is waiting time, not "no duty"
        return _seg(problem, "layover" if e - s < min_break else "idle", s, e,
                    f"Waiting at {where}" if where else "Waiting", counts_as_break=False)
    if leading or trailing:
        return _seg(problem, "idle", s, e, "Standby (no duty)", counts_as_break=False)
    if e - s < min_break:
        return _seg(problem, "layover", s, e, "Layover", counts_as_break=False)
    if e - s <= LONG_IDLE_MIN:
        return _seg(problem, "break", s, e, f"Break ({e - s} min)", counts_as_break=True)
    return _seg(problem, "idle", s, e, f"Idle time ({e - s} min)", counts_as_break=True)


def _driver_stats(segments: list[dict[str, Any]], d: Driver, max_cont: int, min_break: int) -> dict[str, Any]:
    work = [(s["start_min"], s["end_min"]) for s in segments if s["type"] in WORK_TYPES]
    stretches = continuous_work(work, min_break)
    by_type: dict[str, int] = {}
    for s in segments:
        by_type[s["type"]] = by_type.get(s["type"], 0) + s["duration_min"]
    shift = d.shift_end - d.shift_start
    work_min = sum(e - s for s, e in work)
    return {
        "shift_minutes": shift,
        "work_minutes": work_min,
        "idle_minutes": shift - work_min,
        "regular_minutes": by_type.get("regular_service", 0),
        "express_minutes": by_type.get("express", 0),
        "deadhead_minutes": by_type.get("deadhead", 0),
        "break_minutes": sum(s["duration_min"] for s in segments if s.get("counts_as_break")),
        "express_trips": sum(1 for s in segments if s["type"] == "express"),
        "max_continuous_work_min": max((e - s for s, e in stretches), default=0),
        "utilisation_pct": round(100 * work_min / shift, 1) if shift else 0.0,
        "_stretches": stretches,
    }


def continuous_work(work: list[tuple[int, int]], min_break: int) -> list[tuple[int, int]]:
    """Merge work pieces separated by less than min_break minutes -> continuous working blocks."""
    out: list[list[int]] = []
    for s, e in sorted(work):
        if out and s - out[-1][1] < min_break:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _itinerary(segments: list[dict[str, Any]]) -> list[str]:
    """Plain-language day plan for the driver companion app."""
    lines = []
    for s in segments:
        when = f"{s['start']}-{s['end']}"
        if s["type"] == "regular_service":
            frm, to = (s.get("from") or {}).get("name"), (s.get("to") or {}).get("name")
            lines.append(f"{when}  {s['label']}" + (f" ({frm} -> {to})" if frm and to else ""))
        elif s["type"] == "express":
            lines.append(f"{when}  EXPRESS {s['label']}")
        elif s["type"] == "deadhead":
            lines.append(f"{when}  {s['label']} (no passengers)")
        elif s.get("counts_as_break"):
            lines.append(f"{when}  BREAK {s['duration_min']} min - protected")
        elif s["type"] == "idle":
            lines.append(f"{when}  {s['label']}")
    return lines


def _trip_json(problem: Problem, t: Trip, driver_id: str | None) -> dict[str, Any]:
    return {
        "trip_id": t.trip_id, "direction": t.direction, "window": t.window,
        "departure": fmt_time(t.dep), "arrival": fmt_time(t.arr),
        "departure_min": t.dep, "arrival_min": t.arr,
        "departure_iso": iso_time(t.dep, problem.service_date), "arrival_iso": iso_time(t.arr, problem.service_date),
        "from": t.origin.name, "to": t.dest.name, "demand_weight": t.weight,
        "covered": driver_id is not None, "driver_id": driver_id,
    }


def peak_vehicles(trips: list[Trip], layover: int) -> int:
    events = sorted([(t.dep, 1) for t in trips] + [(t.arr + layover, -1) for t in trips], key=lambda e: (e[0], e[1]))
    cur = best = 0
    for _, delta in events:
        cur += delta
        best = max(best, cur)
    return best


def max_waits(problem: Problem, trips: list[Trip], served: set[str]) -> list[dict[str, Any]]:
    """Longest wait between consecutive served departures (incl. window edges), per window & direction."""
    rows = []
    for r in problem.routes:
        for w in r.windows:
            for direction in w.directions:
                deps = sorted(t.dep for t in trips if t.route_id == r.route_id and t.window == w.label
                              and t.direction == direction and t.trip_id in served)
                points = [w.start] + deps + [w.end]
                worst = max(b - a for a, b in zip(points, points[1:]))
                rows.append({"route_id": r.route_id, "window": w.label, "direction": direction,
                             "departures": len(deps), "max_wait_min": worst,
                             "target_headway_min": w.max_headway_min,
                             "target_met": bool(deps) and worst <= w.max_headway_min})
    return rows


def target_trip_count(problem: Problem) -> dict[str, int]:
    """Trips each route *should* run: fixed mode = candidates; flexible = one per max headway."""
    out = {}
    for r in problem.routes:
        if r.timetable_mode == "flexible":
            out[r.route_id] = sum(-(-(w.end - w.start) // w.max_headway_min) * len(w.directions) for w in r.windows)
        else:
            out[r.route_id] = len(r.trips)
    return out


def build_output(
    problem: Problem,
    result: SolveResult,
    *,
    dedicated: dict[str, Any] | None = None,
    scenarios: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rules = problem.rules
    max_cont, min_break = int(rules["max_continuous_work_min"]), int(rules["min_break_min"])
    trips = result.trips
    by_trip = {t.trip_id: t for t in trips}
    covered_trips = [by_trip[tid] for tid in result.covered]

    drivers_json: list[dict[str, Any]] = []
    gantt: list[dict[str, Any]] = []
    totals = {"shift": 0, "work_before": 0, "work_after": 0, "idle_before": 0, "idle_after": 0, "deadhead": 0}
    all_drivers = list(problem.drivers) + [d for d in result.drivers
                                           if d.is_new_hire and d.driver_id in result.activities]
    reallocations: list[dict[str, Any]] = []
    for d in all_drivers:
        added = result.activities.get(d.driver_id, [])
        replaced_ids = set(result.replaced.get(d.driver_id, []))
        before = _timeline(problem, d, [])
        after = _timeline(problem, d, added, replaced_ids)
        for o in d.options:
            if o.option_id not in replaced_ids:
                continue
            in_win = [a for a in added if o.start <= a.start and a.end <= o.end]
            busy = sum(a.end - a.start for a in in_win)
            reallocations.append({
                "driver_id": d.driver_id, "option_id": o.option_id, "group": o.group,
                "cancelled": [{"label": b.label, "start": fmt_time(b.start), "end": fmt_time(b.end),
                               "start_min": b.start, "end_min": b.end, **b.meta} for b in o.blocks],
                "freed_window": {"start": fmt_time(o.start), "end": fmt_time(o.end),
                                 "start_min": o.start, "end_min": o.end},
                "express_trips": [a.trip.trip_id for a in in_win if a.kind == "express"],
                "rest_before_min": (o.end - o.start) - o.minutes,
                "rest_after_min": (o.end - o.start) - busy,
            })
        sb = _driver_stats(before, d, max_cont, min_break)
        sa = _driver_stats(after, d, max_cont, min_break)
        stretches = sa.pop("_stretches")
        sb.pop("_stretches")
        if not d.is_new_hire:
            totals["shift"] += sa["shift_minutes"]
            totals["work_before"] += sb["work_minutes"]
            totals["work_after"] += sa["work_minutes"]
            totals["idle_before"] += sb["idle_minutes"]
            totals["idle_after"] += sa["idle_minutes"]
        totals["deadhead"] += sa["deadhead_minutes"]
        breaks = [{"start": s["start"], "end": s["end"], "start_min": s["start_min"], "end_min": s["end_min"],
                   "duration_min": s["duration_min"]} for s in after if s.get("counts_as_break")]
        drivers_json.append({
            "driver_id": d.driver_id,
            "name": d.name,
            "is_new_hire": d.is_new_hire,
            "available_for_express": d.available,
            "changed": bool(added) or bool(replaced_ids),
            "reallocated_option_ids": sorted(replaced_ids),
            "shift_start": fmt_time(d.shift_start), "shift_end": fmt_time(d.shift_end),
            "shift_start_min": d.shift_start, "shift_end_min": d.shift_end,
            "depot": _loc(d.depot),
            "before": before,
            "after": after,
            "breaks": breaks,
            "itinerary": _itinerary(after),
            "continuous_work_blocks": [{"start": fmt_time(s), "end": fmt_time(e), "start_min": s, "end_min": e,
                                        "duration_min": e - s, "within_limit": e - s <= max_cont}
                                       for s, e in stretches],
            "stats": {
                "before": sb, "after": sa,
                "express_trips": sa["express_trips"],
                "idle_minutes_reclaimed": sb["idle_minutes"] - sa["idle_minutes"],
            },
            **({"meta": d.meta} if d.meta else {}),
        })
        for phase, segs in (("before", before), ("after", after)):
            for s in segs:
                gantt.append({"driver_id": d.driver_id, "phase": phase, "type": s["type"], "label": s["label"],
                              "start_iso": s["start_iso"], "end_iso": s["end_iso"],
                              "start_min": s["start_min"], "end_min": s["end_min"]})

    targets = target_trip_count(problem)
    waits = max_waits(problem, trips, set(result.covered))
    routes_json = []
    for r in problem.routes:
        r_trips = [t for t in trips if t.route_id == r.route_id]
        listed = r_trips if r.timetable_mode == "fixed" else [t for t in r_trips if t.trip_id in result.covered]
        n_cov = sum(1 for t in r_trips if t.trip_id in result.covered)
        routes_json.append({
            "route_id": r.route_id, "name": r.name,
            "origin": _loc(r.origin), "destination": _loc(r.destination),
            "travel_time_min": r.travel_time_min, "distance_km": r.distance_km,
            "timetable_mode": r.timetable_mode,
            "service_windows": [{"label": w.label, "start": fmt_time(w.start), "end": fmt_time(w.end),
                                 "headway_min": w.headway_min, "max_headway_min": w.max_headway_min,
                                 "directions": w.directions} for w in r.windows],
            "trips": [_trip_json(problem, t, result.covered.get(t.trip_id)) for t in listed],
            "stats": {"trips_target": targets[r.route_id], "trips_covered": n_cov,
                      "coverage_pct": min(100.0, _pct(n_cov, targets[r.route_id])),
                      "headways": [h for h in waits if h["route_id"] == r.route_id]},
            **({"stops": r.raw["stops"]} if r.raw.get("stops") else {}),
        })

    real_ids = {d.driver_id for d in problem.drivers}
    hires_used = sorted(did for did in result.activities if did not in real_ids)
    target_total = sum(targets.values())
    weight_all = sum(t.weight for t in trips) or 1.0
    express_minutes = sum(t.minutes for t in covered_trips)
    distance = {r.route_id: r.distance_km for r in problem.routes}
    layover = int(rules["min_layover_min"])
    kpis = {
        "existing_drivers": len(problem.drivers),
        "new_drivers_required": len(hires_used),
        "new_drivers_allowed": int(rules["max_new_drivers"]),
        "drivers_used_for_express": sum(1 for did in result.activities if did in real_ids),
        "express_trips_target": target_total,
        "express_trips_covered": len(covered_trips),
        "express_trips_uncovered": max(0, target_total - len(covered_trips)),
        "coverage_pct": min(100.0, _pct(len(covered_trips), target_total)),
        "max_passenger_wait_min": max((h["max_wait_min"] for h in waits), default=None),
        "headway_targets_met": sum(h["target_met"] for h in waits),
        "headway_targets_total": len(waits),
        "demand_weighted_coverage_pct": _pct(sum(t.weight for t in covered_trips), weight_all)
        if all(r.timetable_mode == "fixed" for r in problem.routes) else None,
        "express_service_minutes": express_minutes,
        "express_vehicle_km": round(sum((distance.get(t.route_id) or 0) for t in covered_trips), 1),
        "seats_added": len(covered_trips) * int(rules["bus_capacity"]),
        "peak_express_vehicles": peak_vehicles(covered_trips, layover),
        "deadhead_minutes": totals["deadhead"],
        "idle_minutes_before": totals["idle_before"],
        "idle_minutes_after": totals["idle_after"],
        "idle_minutes_reclaimed": totals["idle_before"] - totals["idle_after"],
        "idle_reduction_pct": _pct(totals["idle_before"] - totals["idle_after"], totals["idle_before"]),
        "utilisation_before_pct": _pct(totals["work_before"], totals["shift"]),
        "utilisation_after_pct": _pct(totals["work_after"], totals["shift"]),
        "existing_trips_reallocated": len(reallocations),
        "regular_trip_legs_cancelled": sum(len(r["cancelled"]) for r in reallocations),
        "rest_minutes_gained": sum(r["rest_after_min"] - r["rest_before_min"] for r in reallocations),
        "branch_headways": _branch_headways(problem, result),
        "break_rule_violations": None,      # filled in by the validator
        "pre_existing_violations": len(result.baseline_violations),
    }
    if dedicated:
        kpis["dedicated_drivers_needed"] = dedicated.get("drivers_needed")
        kpis["new_hires_avoided"] = (dedicated.get("drivers_needed") or 0) - len(hires_used)
        kpis["annual_cost_avoided_eur"] = kpis["new_hires_avoided"] * int(rules["annual_cost_per_driver_eur"])
    if scenarios:
        best = [s for s in scenarios if s.get("coverage_pct", 0) >= 95.0]
        kpis["recommended_headway_min"] = min((s["headway_min"] for s in best), default=None)

    warnings = list(problem.warnings)
    for did, mins in result.baseline_violations.items():
        warnings.append(f"{did}: input duty already exceeds {max_cont} min without a {min_break}-min break "
                        f"(around {describe_violations(mins)}) - left unchanged, not caused by the optimizer")

    map_layers = {"express_routes": [{"route_id": r["route_id"], "name": r["name"], "origin": r["origin"],
                                      "destination": r["destination"], **({"stops": r["stops"]} if r.get("stops") else {})}
                                     for r in routes_json]}
    map_layers.update(problem.map_layers)

    return {
        "meta": {
            "contract_version": CONTRACT_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scenario_name": problem.meta.get("scenario_name"),
            "mode": problem.meta.get("mode") or ("trip_reallocation" if any(d.options for d in problem.drivers)
                                                 else "idle_time"),
            "assumptions": problem.meta.get("assumptions"),
            "validation_required": problem.meta.get("validation_required"),
            "input_source": problem.meta.get("source"),
            "service_date": problem.service_date.isoformat(),
            "solver": f"Google OR-Tools CP-SAT {ortools.__version__} (0-1 integer linear program)",
            "status": result.status,
            "objective": result.objective,
            "best_bound": result.best_bound,
            "solve_time_s": result.wall_time_s,
            "model_size": result.stats,
        },
        "rules": {k: v for k, v in rules.items()},
        "kpis": kpis,
        "express_routes": routes_json,
        "reallocations": reallocations,
        "drivers": drivers_json,
        "gantt": gantt,
        "dedicated_staffing_baseline": dedicated,
        "scenarios": scenarios or [],
        "map": map_layers,
        "warnings": warnings,
        "compliance": None,                 # filled in by the validator
    }


def _branch_headways(problem: Problem, result: SolveResult) -> list[dict[str, Any]]:
    """Largest gap between remaining departures per protected branch, before vs after reallocation."""
    groups: dict[str, list[tuple[int, bool]]] = {}
    for d in problem.drivers:
        rep_ids = set(result.replaced.get(d.driver_id, []))
        for o in d.options:
            if o.group:
                groups.setdefault(o.group, []).append((o.start, o.option_id in rep_ids))
    out = []
    for g, lst in sorted(groups.items()):
        lst.sort()
        allt = [t for t, _ in lst]
        n_candidates = len(allt)
        diffs = sorted(b - a for a, b in zip(allt, allt[1:]))
        if diffs:   # the departures just before/after the candidates are not replaceable: add them
            h = diffs[len(diffs) // 2]
            allt = [allt[0] - h] + allt + [allt[-1] + h]
            lst = [(allt[0], False)] + lst + [(allt[-1], False)]
        kept = [t for t, r in lst if not r]
        gap = lambda ts: max((b - a for a, b in zip(ts, ts[1:])), default=0)
        out.append({"group": g, "departures": n_candidates, "reallocated": sum(r for _, r in lst),
                    "max_headway_before_min": gap(allt), "max_headway_after_min": gap(kept)})
    return out


def _pct(num: float, den: float) -> float:
    return round(100.0 * num / den, 1) if den else 0.0
