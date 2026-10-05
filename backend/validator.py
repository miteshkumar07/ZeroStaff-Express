"""Independent minute-level checker for optimized_schedule.json.

It re-derives everything from the *output JSON* (plus the parsed input) and shares no logic with
the CP-SAT model, so the dashboard can show "every schedule independently verified".
Rules always come from the input (demand_input.json + overrides), never from the file being checked.
Run standalone:  python -m backend.validator demand_input.json optimized_schedule.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .contract import Problem, fmt_time, load_problem, parse_location, same_place, travel_min

WORK = {"regular_service", "express", "deadhead"}


def _stretches(work: list[tuple[int, int]], min_break: int) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s, e in sorted(work):
        if out and s - out[-1][1] < min_break:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _interior_break_minutes(work: list[tuple[int, int]], min_piece: int) -> int:
    merged: list[list[int]] = []
    for s, e in sorted(work):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return sum(s2 - e1 for (_, e1), (s2, _) in zip(merged, merged[1:]) if s2 - e1 >= min_piece)


def check_driver(segments: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    """Labour-rule check of one driver's day (list of segments with start_min/end_min/type)."""
    max_cont, min_break = int(rules["max_continuous_work_min"]), int(rules["min_break_min"])
    piece = int(rules.get("arbzg_min_piece_min", 15))
    work = [(s["start_min"], s["end_min"]) for s in segments if s["type"] in WORK]
    stretches = _stretches(work, min_break)
    worked = sum(e - s for s, e in work)
    rest = _interior_break_minutes(work, piece)
    longest = max((e - s for s, e in stretches), default=0)
    need = 0
    if rules.get("arbzg_break_totals", True):
        need = 45 if worked > 540 else 30 if worked > 360 else 0
    cap = int(rules.get("max_shift_work_min") or 0)
    issues = []
    if longest > max_cont:
        s, e = max(stretches, key=lambda x: x[1] - x[0])
        issues.append(f"{longest} min continuous work {fmt_time(s)}-{fmt_time(e)} (> {max_cont} without a "
                      f"{min_break}-min break)")
    if rest < need:
        issues.append(f"ArbZG s.4: {worked} min work needs {need} min of breaks (pieces >= {piece} min), has {rest}")
    if cap and worked > cap:
        issues.append(f"{worked} min work exceeds daily cap {cap}")
    return {"worked_min": worked, "break_min": rest, "max_continuous_work_min": longest,
            "excess_continuous": max(0, longest - max_cont), "break_deficit": max(0, need - rest),
            "excess_work": max(0, worked - cap) if cap else 0, "issues": issues}


def _not_worse(after: dict[str, Any], before: dict[str, Any]) -> bool:
    """A non-compliant day is only acceptable if the input day was at least as bad on every rule."""
    return (after["excess_continuous"] <= before["excess_continuous"]
            and after["break_deficit"] <= before["break_deficit"]
            and after["excess_work"] <= before["excess_work"])


def validate(problem: Problem, out: dict[str, Any]) -> dict[str, Any]:
    rules = problem.rules
    checks: list[dict[str, Any]] = []
    issues: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    # 0. the solver actually produced a schedule --------------------------------------------------
    status = (out.get("meta") or {}).get("status")
    check("solver_status", status in ("OPTIMAL", "FEASIBLE"), f"status={status}")

    # 1. zero new staff --------------------------------------------------------------------------
    in_ids = {d.driver_id for d in problem.drivers}
    out_drivers = out.get("drivers", [])
    out_real = {d["driver_id"] for d in out_drivers if not d.get("is_new_hire")}
    hires = [d["driver_id"] for d in out_drivers if d.get("is_new_hire")]
    unknown = out_real - in_ids
    missing = in_ids - out_real
    allowed = int(rules.get("max_new_drivers", 0))
    check("zero_new_staff", not unknown and len(hires) <= allowed
          and out["kpis"]["new_drivers_required"] == len(hires),
          f"{len(in_ids)} existing drivers, {len(hires)} new hires (allowed {allowed})"
          + (f"; unknown ids {sorted(unknown)}" if unknown else ""))
    check("all_drivers_reported", not missing, f"missing {sorted(missing)}" if missing else "")

    # 2. per-driver timeline integrity + labour rules ----------------------------------------------
    by_id = {d.driver_id: d for d in problem.drivers}
    listed = {t["trip_id"]: t for r in out.get("express_routes", []) for t in r["trips"]}
    source_trips = problem.trip_by_id
    timeline_bad, rules_bad, regular_bad, teleports, transfer_bad, timetable_bad = [], [], [], [], [], []
    trip_owner: dict[str, list[str]] = {}
    realloc_by_group: dict[Any, set[str]] = {}
    pre_existing = 0
    for d in out_drivers:
        did = d["driver_id"]
        after = sorted(d["after"], key=lambda s: (s["start_min"], s["end_min"]))
        for s1, s2 in zip(after, after[1:]):
            if s2["start_min"] < s1["end_min"]:
                timeline_bad.append(f"{did}: {s1['type']} and {s2['type']} overlap at {s2['start']}")
        if after and (after[0]["start_min"] < d["shift_start_min"] or after[-1]["end_min"] > d["shift_end_min"]):
            timeline_bad.append(f"{did}: activity outside shift {d['shift_start']}-{d['shift_end']}")
        if did in by_id:
            src = by_id[did]
            if (d["shift_start_min"], d["shift_end_min"]) != (src.shift_start, src.shift_end):
                timeline_bad.append(f"{did}: shift changed")
            declared = set(d.get("reallocated_option_ids") or [])
            replaceable = {o.option_id for o in src.options}
            if declared - replaceable:
                regular_bad.append(f"{did}: cancelled trips {sorted(declared - replaceable)} were not replaceable")
            for o in src.options:
                if o.option_id in declared:
                    realloc_by_group.setdefault(o.group, set()).add(o.option_id)
            want = sorted((b.start, b.end) for b in src.blocks if not (b.replace_id and b.replace_id in declared))
            got = sorted((s["start_min"], s["end_min"]) for s in after if s["type"] == "regular_service")
            if want != got:
                regular_bad.append(f"{did}: regular-service blocks differ from input")
            before_segs = [{"type": "regular_service", "start_min": b.start, "end_min": b.end} for b in src.blocks]
        else:
            before_segs = []                         # a new hire starts from an empty day
        res = check_driver(after, rules)
        base = check_driver(before_segs, rules)
        if res["issues"]:
            if base["issues"] and _not_worse(res, base):
                pre_existing += 1          # the input duty was already non-compliant; we did not worsen it
            else:
                rules_bad += [f"{did}: {i}" for i in res["issues"]]
        work = [s for s in after if s["type"] in WORK]
        for s1, s2 in zip(work, work[1:]):
            if "express" not in (s1["type"], s2["type"]) and "deadhead" not in (s1["type"], s2["type"]):
                continue
            a, b = parse_location(s1.get("to")), parse_location(s2.get("from"))
            if a and b and not same_place(a, b):
                teleports.append(f"{did}: {a.name} -> {b.name} at {s2['start']} without transfer")
        for s in after:
            if s["type"] == "deadhead":
                a, b = parse_location(s.get("from")), parse_location(s.get("to"))
                need = travel_min(a, b, rules)
                if s["duration_min"] < need:
                    transfer_bad.append(f"{did}: transfer at {s['start']} takes {s['duration_min']} min, needs {need}")
            if s["type"] == "express":
                tid = s.get("trip_id")
                trip_owner.setdefault(tid, []).append(did)
                ref = source_trips.get(tid)
                lt = listed.get(tid)
                if ref is not None and (s["start_min"], s["end_min"]) != (ref.dep, ref.arr):
                    timetable_bad.append(f"{did}: {tid} driven {s['start']}-{s['end']}, timetable says "
                                         f"{fmt_time(ref.dep)}-{fmt_time(ref.arr)}")
                elif lt is not None and "departure_min" in lt \
                        and (s["start_min"], s["end_min"]) != (lt["departure_min"], lt.get("arrival_min")):
                    timetable_bad.append(f"{did}: {tid} times differ from the published timetable")
    # reallocation limits: never more than k cancelled departures in a row on one branch
    kmax = rules.get("max_consecutive_replacements_per_group")
    limit_bad = []
    if kmax is not None:
        groups: dict[Any, list[tuple[int, str]]] = {}
        for src in problem.drivers:
            for o in src.options:
                if o.group:
                    groups.setdefault(o.group, []).append((o.start, o.option_id))
        for g, lst in groups.items():
            flags = [oid in realloc_by_group.get(g, set()) for _, oid in sorted(lst)]
            run = best = 0
            for f in flags:
                run = run + 1 if f else 0
                best = max(best, run)
            if best > int(kmax):
                limit_bad.append(f"{g}: {best} consecutive departures reallocated (max {kmax})")
    n_realloc = sum(len(v) for v in realloc_by_group.values())
    check("reallocation_limits", not limit_bad,
          "; ".join(limit_bad) or f"{n_realloc} trip pairs reallocated, max {kmax} in a row per branch")
    issues += limit_bad
    check("timeline_integrity", not timeline_bad, "; ".join(timeline_bad[:5]))
    check("regular_service_unchanged", not regular_bad, "; ".join(regular_bad[:5]))
    check("break_rules", not rules_bad,
          "; ".join(rules_bad[:5]) or f"<= {rules['max_continuous_work_min']} min continuous work, "
                                      f">= {rules['min_break_min']}-min breaks, ArbZG s.4 totals, daily cap")
    check("location_continuity", not teleports, "; ".join(teleports[:5]))
    check("transfer_times", not transfer_bad, "; ".join(transfer_bad[:5]))
    check("timetable_match", not timetable_bad, "; ".join(timetable_bad[:5]))
    issues += timeline_bad + regular_bad + rules_bad + teleports + transfer_bad + timetable_bad

    # 3. trips -------------------------------------------------------------------------------------
    double = {tid: o for tid, o in trip_owner.items() if len(o) > 1}
    mismatch = []
    n_cov = 0
    for tid, t in listed.items():
        if t["covered"]:
            n_cov += 1
            if trip_owner.get(tid, [None])[0] != t["driver_id"]:
                mismatch.append(tid)
        elif tid in trip_owner:
            mismatch.append(tid)
    orphan = set(trip_owner) - set(listed)
    check("each_trip_one_driver", not double and not mismatch and not orphan,
          f"double={sorted(double)[:5]} mismatch={mismatch[:5]} orphan={sorted(orphan)[:5]}"
          if (double or mismatch or orphan) else f"{n_cov} trips, each driven by exactly one driver")
    check("kpi_consistency", out["kpis"]["express_trips_covered"] == len(trip_owner) == n_cov,
          f"kpi={out['kpis']['express_trips_covered']} segments={len(trip_owner)} listed={n_cov}")
    issues += [f"trip {t} driven by {o}" for t, o in double.items()] + [f"trip {t} owner mismatch" for t in mismatch]

    passed = all(c["passed"] for c in checks)
    return {
        "passed": passed,
        "checks": checks,
        "issues": issues,
        "violations_after": len(rules_bad),
        "pre_existing_noncompliant_drivers": pre_existing,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m backend.validator demand_input.json optimized_schedule.json")
        return 2
    with open(argv[2], encoding="utf-8") as fh:
        out = json.load(fh)
    problem = load_problem(argv[1])
    lock = problem.rules["max_new_drivers"]
    # the output may come from a what-if run (e.g. --headway / --max-new-drivers): re-apply its rules
    # but report clearly if the zero-new-staff lock was relaxed
    problem = load_problem(argv[1], rule_overrides=out.get("rules"))
    if problem.rules["max_new_drivers"] != lock:
        print(f"NOTE: output was produced with max_new_drivers={problem.rules['max_new_drivers']} (input: {lock})")
    rep = validate(problem, out)
    for c in rep["checks"]:
        print(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['name']}: {c['detail']}")
    print("ALL CHECKS PASSED" if rep["passed"] else "VALIDATION FAILED")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
