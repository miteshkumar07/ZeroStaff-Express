"""demand_input.json -> optimized_schedule.json

CLI:
    python -m backend.run --input data/demand_input.json --output data/optimized_schedule.json

Python API (e.g. live re-optimisation from a Streamlit slider):
    from backend.run import optimize
    schedule = optimize("data/demand_input.json", headway_min=10, sweep=None)
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from .contract import ContractError, Problem, Trip, generate_trips, load_problem
from .model import make_new_hires, solve
from .schedule import _branch_headways, build_output, max_waits, peak_vehicles
from .validator import validate

DEFAULT_SWEEP = (10, 15, 20, 30)


def dedicated_baseline(problem: Problem, trips: list[Trip], time_limit_s: float = 10.0) -> dict[str, Any]:
    """How many *new* drivers the traditional approach (dedicated express duties) would need
    to run exactly the trips we serve with zero new staff. Same rules, same solver."""
    if not trips:
        return {"drivers_needed": 0, "trips": 0, "trips_covered": 0, "complete": True, "status": "TRIVIAL"}
    k = min(len(trips), 2 * peak_vehicles(trips, int(problem.rules["min_layover_min"])) + 2)
    hires = make_new_hires(problem, trips, k, prefix="DED")
    res = solve(problem, hires, trips, time_limit_s=time_limit_s, new_driver_cap=None,
                weights={"trip": 1_000_000, "new_driver": 1000, "deadhead_min": 1, "driver_touched": 0})
    return {
        "drivers_needed": len(res.activities),
        "trips": len(trips),
        "trips_covered": len(res.covered),
        "complete": len(res.covered) == len(trips),
        "status": res.status,
        "method": "Same CP-SAT model with only new hires on dedicated split shifts covering the express "
                  "service windows; minimises the number of hires.",
    }


def headway_sweep(problem: Problem, headways: Iterable[int], time_limit_s: float = 8.0) -> list[dict[str, Any]]:
    """What express frequency can today's staff run? One zero-new-staff solve per headway."""
    rows = []
    max_new = int(problem.rules["max_new_drivers"])
    real_ids = {d.driver_id for d in problem.drivers}
    for h in headways:
        trips = [t for r in problem.routes for t in generate_trips(r, headway_override=int(h))]
        hires = make_new_hires(problem, trips, max_new)
        res = solve(problem, problem.drivers + hires, trips, time_limit_s=time_limit_s, new_driver_cap=max_new)
        rows.append({
            "headway_min": int(h),
            "trips_target": len(trips),
            "trips_covered": len(res.covered),
            "coverage_pct": round(100 * len(res.covered) / len(trips), 1) if trips else 0.0,
            "drivers_used": sum(1 for d in res.activities if d in real_ids),
            "new_drivers_required": sum(1 for d in res.activities if d not in real_ids),
            "deadhead_minutes": sum(a.end - a.start for acts in res.activities.values()
                                    for a in acts if a.kind == "deadhead"),
            "status": res.status,
        })
    return rows


def reallocation_curve(problem: Problem, time_limit_s: float = 8.0) -> list[dict[str, Any]]:
    """Trade-off: X30 service gained vs existing trips handed over (0, 1, 2, ... reallocations)."""
    n_opts = sum(len(d.options) for d in problem.drivers)
    if not n_opts:
        return []
    flexible = {r.route_id for r in problem.routes if r.timetable_mode == "flexible"}
    rows, prev = [], None
    for k in range(0, n_opts + 1):
        p = copy.copy(problem)
        p.rules = {**problem.rules, "max_replacements": k}
        res = solve(p, problem.drivers, problem.trips, time_limit_s=time_limit_s, flexible_routes=flexible)
        n_rep = sum(len(v) for v in res.replaced.values())
        out = {"max_reallocations": k, "reallocated": n_rep, "express_trips": len(res.covered),
               "express_departures_outbound": sum(1 for t in res.covered if "-OUT-" in t),
               "max_passenger_wait_min": max((h["max_wait_min"] for h in max_waits(problem, problem.trips,
                                                                                  set(res.covered))), default=None),
               "branch_headways": _branch_headways(problem, res), "status": res.status}
        rows.append(out)
        if prev is not None and n_rep == prev:      # rule limits reached: more allowance changes nothing
            break
        prev = n_rep
    return rows


def staffing_curve(problem: Problem, max_extra: int = 4, time_limit_s: float = 8.0) -> list[dict[str, Any]]:
    """Coverage of the configured timetable with 0, 1, 2, ... new hires (stops at 100%)."""
    rows = []
    trips = problem.trips
    real_ids = {d.driver_id for d in problem.drivers}
    flexible = {r.route_id for r in problem.routes if r.timetable_mode == "flexible"}
    if flexible:
        return rows                       # coverage is not meaningful for a co-optimised timetable
    for n in range(0, max_extra + 1):
        hires = make_new_hires(problem, trips, n)
        res = solve(problem, problem.drivers + hires, trips, time_limit_s=time_limit_s, new_driver_cap=n,
                    weights={"new_driver": 0})
        cov = len(res.covered)
        rows.append({
            "new_drivers": n,
            "new_drivers_used": sum(1 for d in res.activities if d not in real_ids),
            "trips_covered": cov, "trips_target": len(trips),
            "coverage_pct": round(100 * cov / len(trips), 1) if trips else 0.0,
            "status": res.status,
        })
        if cov == len(trips):
            break
    return rows


def optimize(
    source: str | Path | dict[str, Any],
    *,
    rule_overrides: dict[str, Any] | None = None,
    headway_min: int | None = None,
    time_limit_s: float = 20.0,
    sweep: Iterable[int] | None = DEFAULT_SWEEP,
    sweep_time_limit_s: float = 8.0,
    baseline: bool = True,
    curve: bool = True,
    log: bool = False,
) -> dict[str, Any]:
    problem = load_problem(source, rule_overrides)
    if headway_min:
        for r in problem.routes:
            r.timetable_mode = "fixed"
            for w in r.windows:
                w.headway_min = w.max_headway_min = int(headway_min)
            r.trips = generate_trips(r, headway_override=int(headway_min))

    max_new = int(problem.rules["max_new_drivers"])
    hires = make_new_hires(problem, problem.trips, max_new)
    flexible = {r.route_id for r in problem.routes if r.timetable_mode == "flexible"}
    result = solve(problem, problem.drivers + hires, problem.trips, time_limit_s=time_limit_s,
                   new_driver_cap=max_new, flexible_routes=flexible, log=log)

    if not result.ok:
        problem.warnings.append(f"Solver returned {result.status} within {time_limit_s:.0f} s - no express trips "
                                "assigned; increase --time-limit or check the input")
    by_id = problem.trip_by_id
    dedicated = dedicated_baseline(problem, [by_id[t] for t in result.covered], sweep_time_limit_s) \
        if baseline else None
    realloc_mode = any(d.options for d in problem.drivers)
    scenarios = headway_sweep(problem, sweep, sweep_time_limit_s) if sweep and not realloc_mode else None

    out = build_output(problem, result, dedicated=dedicated, scenarios=scenarios)
    out["staffing_curve"] = staffing_curve(problem, time_limit_s=sweep_time_limit_s) if curve else []
    out["reallocation_curve"] = reallocation_curve(problem, sweep_time_limit_s) if curve and realloc_mode else []
    report = validate(problem, out)
    out["compliance"] = report
    out["kpis"]["break_rule_violations"] = report["violations_after"]
    out["warnings"] = list(dict.fromkeys(out["warnings"] + problem.warnings))
    return out


def print_summary(out: dict[str, Any]) -> None:
    k, m = out["kpis"], out["meta"]
    print(f"\nZero-Staff Express - {m['status']} in {m['solve_time_s']} s  ({m['model_size']['variables']} vars)")
    print(f"  Existing drivers        : {k['existing_drivers']}   New drivers required: {k['new_drivers_required']}")
    print(f"  Express trips covered   : {k['express_trips_covered']} / {k['express_trips_target']} "
          f"({k['coverage_pct']}%)  by {k['drivers_used_for_express']} drivers")
    if m.get("mode") == "trip_reallocation":
        print(f"  Trips reallocated       : {k['existing_trips_reallocated']} round trips "
              f"({k['regular_trip_legs_cancelled']} legs) handed over; driver rest {k['rest_minutes_gained']:+d} min")
        for b in k["branch_headways"]:
            print(f"     {b['group']:<30}: max headway {b['max_headway_before_min']} -> {b['max_headway_after_min']} min")
        print(f"  Express max wait        : {k['max_passenger_wait_min']} min")
    else:
        print(f"  Idle time reclaimed     : {k['idle_minutes_reclaimed']} min  "
              f"(utilisation {k['utilisation_before_pct']}% -> {k['utilisation_after_pct']}%)")
    print(f"  Deadhead                : {k['deadhead_minutes']} min   Peak express buses: {k['peak_express_vehicles']}")
    if k.get("dedicated_drivers_needed") is not None:
        print(f"  Dedicated staffing need : {k['dedicated_drivers_needed']} new drivers  -> avoided "
              f"(~EUR {k['annual_cost_avoided_eur']:,}/yr)")
    if out.get("reallocation_curve"):
        print("  Reallocation curve      : " + " | ".join(
            f"{c['reallocated']} -> {c['express_departures_outbound']} deps, wait {c['max_passenger_wait_min']}"
            for c in out["reallocation_curve"]))
    if out.get("staffing_curve"):
        print("  Staffing curve          : " + " | ".join(
            f"+{c['new_drivers']} -> {c['coverage_pct']}%" for c in out["staffing_curve"]))
    if out["scenarios"]:
        print("  Headway sweep           : " + " | ".join(
            f"{s['headway_min']} min -> {s['coverage_pct']}%" for s in out["scenarios"]))
    c = out["compliance"]
    print(f"  Compliance              : {'ALL CHECKS PASSED' if c['passed'] else 'FAILED'}")
    for chk in c["checks"]:
        if not chk["passed"]:
            print(f"     FAIL {chk['name']}: {chk['detail']}")
    for w in out["warnings"][:8]:
        print(f"  ! {w}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Zero-Staff Express optimizer (OR-Tools CP-SAT)")
    ap.add_argument("--input", "-i", default="data/demand_input.json")
    ap.add_argument("--output", "-o", default="data/optimized_schedule.json")
    ap.add_argument("--time-limit", type=float, default=20.0, help="seconds for the main solve")
    ap.add_argument("--headway", type=int, default=None, help="override headway of every service window (min)")
    ap.add_argument("--sweep", default=",".join(map(str, DEFAULT_SWEEP)),
                    help="comma-separated headways for the scenario sweep, or 'none'")
    ap.add_argument("--sweep-time-limit", type=float, default=8.0)
    ap.add_argument("--max-new-drivers", type=int, default=None, help="what-if: relax the zero-new-staff lock")
    ap.add_argument("--no-baseline", action="store_true", help="skip the dedicated-staffing comparison")
    ap.add_argument("--no-curve", action="store_true", help="skip the coverage-vs-new-hires curve")
    ap.add_argument("--log", action="store_true", help="print CP-SAT search log")
    args = ap.parse_args(argv)

    overrides = {}
    if args.max_new_drivers is not None:
        overrides["max_new_drivers"] = args.max_new_drivers
    sweep = None if args.sweep.strip().lower() in ("", "none", "0") else [int(x) for x in args.sweep.split(",")]
    try:
        out = optimize(args.input, rule_overrides=overrides, headway_min=args.headway,
                       time_limit_s=args.time_limit, sweep=sweep, sweep_time_limit_s=args.sweep_time_limit,
                       baseline=not args.no_baseline, curve=not args.no_curve, log=args.log)
    except (ContractError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print_summary(out)
    print(f"\nWrote {args.output}")
    return 0 if out["compliance"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
