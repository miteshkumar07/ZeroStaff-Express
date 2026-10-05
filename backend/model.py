"""Zero-Staff Express optimizer: a 0-1 integer linear program solved with Google OR-Tools CP-SAT.

Question answered: which *existing* drivers can drive which express trips inside the idle gaps of
their *unchanged* shifts - without breaking the break rules and without hiring anyone.

Decision variables (per driver d, idle gap g, candidate express trip t):
    x[d,g,t]        driver d drives trip t during gap g
    a[d,g,t]        t is the first express trip of the gap  -> deadhead in just before it
    b[d,g,t]        t is the last express trip of the gap   -> deadhead out right after it
    c[d,g,t1,t2]    t2 directly follows t1 (layover / short transfer in between)
    cov[t]          trip t is served
and, on an *exact event timeline* per driver (the day is cut at every block edge and every possible
activity edge, so each elementary interval k is either fully worked or fully free - no rounding):
    w[d,k]          driver works during interval k        (w = OR of the activities covering k)
    z[d,k]          a break starts at interval k: the next min_break minutes are free
    q[d,k]          interval k is part of an ArbZG break (free run >= 15 min between two pieces of work)

Hard constraints:
    * Zero-New-Staff lock: only input drivers (+ at most rules.max_new_drivers new hires, default 0).
    * Shifts are never extended and regular-service blocks are never moved (express work lives
      strictly inside idle gaps, deadheads included).
    * Every trip is served at most once:                  sum_{d,g} x[d,g,t] = cov[t] <= 1
    * Per gap one time-feasible chain (connection network, acyclic because arcs go forward in time):
          a[t] + sum_in c = x[t],   b[t] + sum_out c = x[t],   sum_t a[t] <= 1
    * Break rule: never more than L = 240 min of work without a break of >= 15 free minutes.
      Exact form: for every interval k that may be work, a break must start no later than
      start_k + L:     sum_{j : end_k <= start_j <= start_k + L} z[j] >= w[k]
    * ArbZG s.4 break totals: > 6 h work -> >= 30 min of breaks, > 9 h -> >= 45 min, counting only
      free pieces >= 15 min that lie between two pieces of work.
    * Daily working-time cap (ArbZG 10 h), optional express fleet cap, optional headway limits for
      the flexible (co-optimised) timetable.
    * If an input duty already breaks a rule, the optimizer may not make it worse (and reports it).
    * Doing nothing (no express trips) is always feasible, so the model can never be INFEASIBLE.

Objective (lexicographic through weights): demand-weighted trips served >> headway gaps
>> new hires >> deadhead minutes >> number of drivers whose day changes.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model

from .contract import Block, Driver, Gap, Location, Problem, ReplaceOption, Trip, fmt_time, same_place, travel_min

WEIGHTS: dict[str, int] = {
    "trip": 1000,           # per demand-weight unit of a served trip
    "headway_gap": 5000,    # per violated max-headway interval (flexible timetable only)
    "new_driver": 3000,     # per new hire (only possible if rules.max_new_drivers > 0)
    "deadhead_min": 2,      # per minute of empty running / bus changeover
    "driver_touched": 20,   # per existing driver whose day is changed
}
CROSS_WINDOW_REACH_MIN = 60  # a long wait may only lead into the first hour of a later service window


@dataclass
class Activity:
    kind: str                      # "deadhead" | "express"
    start: int
    end: int
    from_loc: Location | None
    to_loc: Location | None
    trip: Trip | None = None


@dataclass
class SolveResult:
    status: str
    objective: float | None
    best_bound: float | None
    wall_time_s: float
    activities: dict[str, list[Activity]]          # driver_id -> express-related activities
    covered: dict[str, str]                        # trip_id -> driver_id
    drivers: list[Driver]
    trips: list[Trip]
    baseline_violations: dict[str, list[int]]      # driver_id -> start minutes of too-long input stretches
    headway_gaps: int = 0
    replaced: dict[str, list[str]] = field(default_factory=dict)   # driver_id -> reallocated option ids
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")


@dataclass
class _GapModel:
    driver: Driver
    gap: Gap
    cand: dict[str, tuple[Trip, int, int]]         # trip_id -> (trip, deadhead_in, deadhead_out)
    x: dict[str, Any]
    a: dict[str, Any]
    b: dict[str, Any]
    c: dict[tuple[str, str], tuple[Any, int]]      # (t1, t2) -> (var, transfer minutes)
    option: Any = None                             # ReplaceOption whose freed window this gap is


# ----------------------------------------------------------------------------------------------
# helpers shared with the reporting code (pure Python, minute exact)
# ----------------------------------------------------------------------------------------------
def stretch_violations(blocks: list[Block], max_cont: int, min_break: int) -> list[int]:
    """Start minutes of input work stretches longer than max_cont (pieces < min_break apart merge)."""
    out, cur = [], None
    for b in sorted(blocks, key=lambda b: b.start):
        if cur and b.start - cur[1] < min_break:
            cur[1] = max(cur[1], b.end)
        else:
            if cur and cur[1] - cur[0] > max_cont:
                out.append(cur[0])
            cur = [b.start, b.end]
    if cur and cur[1] - cur[0] > max_cont:
        out.append(cur[0])
    return out


def interior_break_minutes(blocks: list[Block], min_piece: int) -> int:
    """Free minutes between consecutive blocks, counting only pieces >= min_piece (ArbZG s.4)."""
    bs = sorted(blocks, key=lambda b: b.start)
    return sum(b2.start - b1.end for b1, b2 in zip(bs, bs[1:]) if b2.start - b1.end >= min_piece)


def make_new_hires(problem: Problem, trips: list[Trip], count: int, prefix: str = "NEW") -> list[Driver]:
    """Virtual new drivers on a dedicated split shift covering the express service windows.

    Used for (a) the "dedicated staffing" baseline and (b) what-if runs with max_new_drivers > 0.
    """
    if not trips or count <= 0:
        return []
    rules = problem.rules
    base = problem.routes[0].origin
    pad = int(rules["changeover_min"])
    spans: dict[str, list[int]] = {}
    for t in trips:
        start = t.dep - travel_min(base, t.origin, rules) - pad
        end = t.arr + travel_min(t.dest, base, rules) + pad
        s = spans.setdefault(t.window, [start, end])
        s[0], s[1] = min(s[0], start), max(s[1], end)
    intervals = sorted([s, e] for s, e in spans.values())
    merged: list[list[int]] = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [
        Driver(
            driver_id=f"{prefix}-{i + 1:02d}", name=f"New hire {i + 1}",
            shift_start=merged[0][0], shift_end=merged[-1][1], blocks=[],
            gaps=[Gap(s, e, base, base) for s, e in merged], depot=base, is_new_hire=True,
        )
        for i in range(count)
    ]


# ----------------------------------------------------------------------------------------------
# the model
# ----------------------------------------------------------------------------------------------
def solve(
    problem: Problem,
    drivers: list[Driver],
    trips: list[Trip],
    *,
    time_limit_s: float = 20.0,
    weights: dict[str, int] | None = None,
    new_driver_cap: int | None = 0,
    flexible_routes: set[str] | None = None,
    workers: int = 8,
    log: bool = False,
) -> SolveResult:
    t0 = time.perf_counter()
    rules = problem.rules
    W = {**WEIGHTS, **(weights or {})}
    max_cont = int(rules["max_continuous_work_min"])
    min_break = int(rules["min_break_min"])
    arbzg_piece = int(rules.get("arbzg_min_piece_min", 15))
    layover = int(rules["min_layover_min"])
    change = int(rules["changeover_min"])
    flexible_routes = flexible_routes or set()
    max_wait = rules.get("max_connection_wait_min")
    trip_by_id = {t.trip_id: t for t in trips}
    window_start: dict[tuple[str, str], int] = {}
    for t in trips:
        key = (t.route_id, t.window)
        window_start[key] = min(window_start.get(key, t.dep), t.dep)

    m = cp_model.CpModel()
    x_by_trip: dict[str, list[Any]] = defaultdict(list)
    gap_models: list[_GapModel] = []
    deadhead_terms: list[tuple[Any, int]] = []
    used: dict[str, Any] = {}
    options_all: list[tuple[ReplaceOption, Any]] = []
    baseline_violations: dict[str, list[int]] = {}
    n_conn = n_intervals = 0

    for d in drivers:
        cap = rules.get("max_shift_work_min")
        if cap and d.work_minutes > int(cap):
            problem.warnings.append(
                f"{d.driver_id}: regular duty already exceeds max_shift_work_min ({d.work_minutes} min)")
        viol = stretch_violations(d.blocks, max_cont, min_break)
        if viol:
            baseline_violations[d.driver_id] = viol
        if not d.available:
            continue
        occupancy: list[tuple[Any, int, int]] = []        # (var, start, end) of switchable work
        starts: list[Any] = []
        opt_y: dict[str, Any] = {}
        for o in d.options:                               # trip reallocation decisions
            opt_y[o.option_id] = m.NewBoolVar(f"y_{d.driver_id}_{o.option_id}")
            options_all.append((o, opt_y[o.option_id]))
        driver_gms: list[_GapModel] = []
        gap_specs = [(g, None) for g in d.gaps] + [(Gap(o.start, o.end, o.start_loc, o.end_loc), o) for o in d.options]

        # ---- connection network inside every idle gap (and every freed reallocation window) ----
        for gi, (g, opt) in enumerate(gap_specs):
            cand: dict[str, tuple[Trip, int, int]] = {}
            for t in trips:
                dh_in = travel_min(g.start_loc, t.origin, rules) + change
                dh_out = travel_min(t.dest, g.end_loc, rules) + change
                if g.start + dh_in <= t.dep and t.arr + dh_out <= g.end:
                    cand[t.trip_id] = (t, dh_in, dh_out)
            if not cand:
                continue
            tag = f"{d.driver_id}_{gi}"
            gm = _GapModel(d, g, cand, {}, {}, {}, {}, option=opt)
            for tid, (t, dh_in, dh_out) in cand.items():
                gm.x[tid] = m.NewBoolVar(f"x_{tag}_{tid}")
                gm.a[tid] = m.NewBoolVar(f"a_{tag}_{tid}")
                gm.b[tid] = m.NewBoolVar(f"b_{tag}_{tid}")
                occupancy.append((gm.x[tid], t.dep, t.arr))
                if dh_in:
                    occupancy.append((gm.a[tid], t.dep - dh_in, t.dep))
                    deadhead_terms.append((gm.a[tid], dh_in))
                if dh_out:
                    occupancy.append((gm.b[tid], t.arr, t.arr + dh_out))
                    deadhead_terms.append((gm.b[tid], dh_out))
                x_by_trip[tid].append(gm.x[tid])
            ordered = sorted(cand.values(), key=lambda c: c[0].dep)
            for i, (t1, _, _) in enumerate(ordered):
                for t2, _, _ in ordered[i + 1:]:
                    if t2.dep <= t1.dep:
                        continue
                    move = 0 if same_place(t1.dest, t2.origin) else travel_min(t1.dest, t2.origin, rules)
                    ready = t1.arr + move + layover
                    if ready > t2.dep:
                        continue
                    if max_wait is not None and t2.dep - ready > int(max_wait):
                        # long waits only to the first hour of a *later* service window (AM -> PM peak)
                        start2 = window_start.get((t2.route_id, t2.window), t2.dep)
                        if t2.window == t1.window or t2.dep >= start2 + CROSS_WINDOW_REACH_MIN:
                            continue
                    v = m.NewBoolVar(f"c_{tag}_{t1.trip_id}_{t2.trip_id}")
                    gm.c[(t1.trip_id, t2.trip_id)] = (v, move)
                    if move:
                        occupancy.append((v, t1.arr, t1.arr + move))
                        deadhead_terms.append((v, move))
            n_conn += len(gm.c)
            ins: dict[str, list[Any]] = defaultdict(list)
            outs: dict[str, list[Any]] = defaultdict(list)
            for (i1, i2), (v, _) in gm.c.items():
                outs[i1].append(v)
                ins[i2].append(v)
            for tid in cand:
                m.Add(sum(ins[tid]) + gm.a[tid] == gm.x[tid])
                m.Add(sum(outs[tid]) + gm.b[tid] == gm.x[tid])
            if opt is None:
                m.AddAtMostOne(gm.a.values())
            else:
                m.Add(sum(gm.a.values()) <= opt_y[opt.option_id])   # only if the trips are reallocated
            starts += gm.a.values()
            gap_models.append(gm)
            driver_gms.append(gm)

        # overlapping windows (an idle gap inside a freed window) can host only one chain
        for i, g1 in enumerate(driver_gms):
            for g2 in driver_gms[i + 1:]:
                if g1.gap.start < g2.gap.end and g2.gap.start < g1.gap.end:
                    m.Add(sum(g1.a.values()) + sum(g2.a.values()) <= 1)

        if not starts:
            for y in opt_y.values():
                m.Add(y == 0)
            continue
        u = m.NewBoolVar(f"used_{d.driver_id}")
        for s in starts:
            m.AddImplication(s, u)
        for y in opt_y.values():
            m.AddImplication(y, u)
        used[d.driver_id] = u

        # ---- daily working-time cap ---------------------------------------------------------
        added = sum(v * (e - s) for v, s, e in occupancy) - sum(
            opt_y[o.option_id] * o.minutes for o in d.options)
        if cap:
            m.Add(added <= max(0, int(cap) - d.work_minutes))

        # ---- exact event timeline -------------------------------------------------------------
        tl = _Timeline(m, d, occupancy, opt_y)
        n_intervals += tl.n
        zb = tl.break_starts(min_break)

        # break rule: from every interval that may be work, a break must start within max_cont
        zb_base = tl.base_ok[min_break]
        protect: set[int] = set()
        for k in range(tl.n):
            wk = tl.work[k]
            if wk is False:
                continue
            s_k = tl.pts[k]
            if s_k + max_cont >= d.shift_end:
                continue                                   # the end of the shift is a break
            terms, ok, base_possible, j = [], False, False, k + 1
            while j < tl.n and tl.pts[j] <= s_k + max_cont:
                if zb[j] is True:
                    ok = True
                    break
                if zb[j] is not None:
                    terms.append(zb[j])
                    base_possible |= zb_base[j]
                j += 1
            if ok:
                continue
            if tl.base_work[k] and not base_possible:
                # the input duty itself is too long here: never make it worse - keep its next break
                while j < tl.n and (zb[j] is None or not zb_base[j]):
                    j += 1
                if j < tl.n and zb[j] is not True:
                    protect.add(j)
            elif terms:
                if wk is True:
                    m.AddBoolOr(terms)
                else:
                    m.Add(sum(terms) >= wk)
            else:
                m.Add(wk == 0)
        for j in protect:
            m.Add(zb[j] == 1)

        # ArbZG s.4 break totals (exact minutes, interior free runs >= arbzg_piece)
        if rules.get("arbzg_break_totals", True):
            breaks, const = tl.interior_break_expr(tl.break_starts(arbzg_piece) if arbzg_piece != min_break else zb)
            base_breaks = interior_break_minutes(d.blocks, arbzg_piece)
            span = d.shift_end - d.shift_start
            for threshold, need in ((360, 30), (540, 45)):
                if d.work_minutes > threshold:
                    if base_breaks < need:
                        problem.warnings.append(
                            f"{d.driver_id}: regular duty has only {base_breaks} min of breaks for "
                            f"{d.work_minutes} min of work (ArbZG s.4 needs {need}) - existing breaks protected")
                    if not isinstance(breaks, int):        # (constant case is satisfied by construction)
                        m.Add(breaks + const >= min(need, base_breaks))
                else:
                    over = m.NewBoolVar(f"over{threshold}_{d.driver_id}")
                    m.Add(added + d.work_minutes - threshold <= span * over)
                    m.Add(breaks + const >= need * over)

    # ---- coverage ---------------------------------------------------------------------------
    cov: dict[str, Any] = {}
    for t in trips:
        xs = x_by_trip.get(t.trip_id)
        if not xs:
            continue
        cv = m.NewBoolVar(f"cov_{t.trip_id}")
        m.Add(sum(xs) == cv)
        cov[t.trip_id] = cv

    # ---- express fleet cap (checked at every departure instant) ---------------------------------
    if rules.get("max_express_vehicles"):
        cap_v = int(rules["max_express_vehicles"])
        served = [trip_by_id[tid] for tid in cov]
        for t in served:
            active = [cov[u.trip_id] for u in served if u.dep <= t.dep < u.arr + layover]
            if len(active) > cap_v:
                m.Add(sum(active) <= cap_v)

    # ---- flexible timetable: min headway (hard) and max headway (soft) -------------------------
    slack_vars: list[Any] = []
    for route in problem.routes:
        if route.route_id not in flexible_routes:
            continue
        for win in route.windows:
            for direction in win.directions:
                group = sorted((t for t in trips if t.route_id == route.route_id and t.window == win.label
                                and t.direction == direction), key=lambda t: t.dep)
                for i, t in enumerate(group):
                    close = [cov[u.trip_id] for u in group[i:]
                             if u.dep < t.dep + win.min_headway_min and u.trip_id in cov]
                    if len(close) > 1:
                        m.AddAtMostOne(close)
                tau = win.start
                while tau + win.max_headway_min <= win.end:
                    inside = [cov[u.trip_id] for u in group
                              if tau <= u.dep < tau + win.max_headway_min and u.trip_id in cov]
                    sv = m.NewBoolVar(f"hw_{route.route_id}_{direction}_{tau}")
                    m.AddBoolOr(inside + [sv])
                    slack_vars.append(sv)
                    tau += route.candidate_step_min

    # ---- reallocation limits: never cancel more than k departures in a row on one branch ----------
    kmax = rules.get("max_consecutive_replacements_per_group")
    groups: dict[str, list[tuple[int, Any]]] = defaultdict(list)
    for o, y in options_all:
        if o.group:
            groups[o.group].append((o.start, y))
    if kmax is not None:
        for lst in groups.values():
            ys = [y for _, y in sorted(lst, key=lambda e: e[0])]
            for i in range(len(ys) - int(kmax)):
                m.Add(sum(ys[i:i + int(kmax) + 1]) <= int(kmax))
    if rules.get("max_replacements") is not None and options_all:
        m.Add(sum(y for _, y in options_all) <= int(rules["max_replacements"]))

    # ---- new-hire lock ---------------------------------------------------------------------------
    hires = [used[d.driver_id] for d in drivers if d.is_new_hire and d.driver_id in used]
    if hires and new_driver_cap is not None:
        m.Add(sum(hires) <= int(new_driver_cap))
    for h1, h2 in zip(hires, hires[1:]):     # new hires are identical: break the symmetry
        m.AddImplication(h2, h1)

    # ---- objective ---------------------------------------------------------------------------------
    terms_obj: list[tuple[Any, int]] = []
    for tid, cv in cov.items():
        terms_obj.append((cv, int(round(W["trip"] * max(trip_by_id[tid].weight, 0.01)))))
    for v, minutes in deadhead_terms:
        terms_obj.append((v, -W["deadhead_min"] * minutes))
    by_id = {d.driver_id: d for d in drivers}
    for did, u in used.items():
        terms_obj.append((u, -(W["new_driver"] if by_id[did].is_new_hire else W["driver_touched"])))
    for sv in slack_vars:
        terms_obj.append((sv, -W["headway_gap"]))
    for o, y in options_all:
        terms_obj.append((y, -int(round(float(rules["replacement_penalty"]) * o.penalty))))
    if terms_obj:
        m.Maximize(cp_model.LinearExpr.WeightedSum([v for v, _ in terms_obj], [c for _, c in terms_obj]))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.num_workers = int(workers)
    solver.parameters.random_seed = 7
    solver.parameters.log_search_progress = log
    status = solver.Solve(m)
    status_name = solver.StatusName(status)
    solved = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    activities: dict[str, list[Activity]] = defaultdict(list)
    covered: dict[str, str] = {}
    replaced: dict[str, list[str]] = {}
    headway_gaps = 0
    if solved:
        for gm in gap_models:
            chain = _extract_chain(solver, gm)
            if not chain:
                continue
            did = gm.driver.driver_id
            first_t, dh_in, _ = gm.cand[chain[0]]
            if dh_in:
                activities[did].append(Activity("deadhead", first_t.dep - dh_in, first_t.dep,
                                                gm.gap.start_loc, first_t.origin))
            for i, tid in enumerate(chain):
                t = gm.cand[tid][0]
                activities[did].append(Activity("express", t.dep, t.arr, t.origin, t.dest, trip=t))
                covered[tid] = did
                if i + 1 < len(chain):
                    move = gm.c[(tid, chain[i + 1])][1]
                    if move:
                        nxt = gm.cand[chain[i + 1]][0]
                        activities[did].append(Activity("deadhead", t.arr, t.arr + move, t.dest, nxt.origin))
            last_t, _, dh_out = gm.cand[chain[-1]]
            if dh_out:
                activities[did].append(Activity("deadhead", last_t.arr, last_t.arr + dh_out,
                                                last_t.dest, gm.gap.end_loc))
        for acts in activities.values():
            acts.sort(key=lambda a: a.start)
        headway_gaps = sum(solver.Value(sv) for sv in slack_vars)
        for o, y in options_all:
            if solver.Value(y):
                replaced.setdefault(o.driver_id, []).append(o.option_id)

    proto = m.Proto()
    return SolveResult(
        status=status_name,
        objective=solver.ObjectiveValue() if solved else None,
        best_bound=solver.BestObjectiveBound() if solved else None,
        wall_time_s=round(time.perf_counter() - t0, 3),
        activities=dict(activities), covered=covered, drivers=drivers, trips=trips,
        baseline_violations=baseline_violations, headway_gaps=headway_gaps, replaced=replaced,
        stats={"variables": len(proto.variables), "constraints": len(proto.constraints),
               "connection_arcs": n_conn, "gap_models": len(gap_models), "timeline_intervals": n_intervals,
               "reallocation_options": len(options_all),
               "solver_time_s": round(solver.WallTime(), 3)},
    )


class _Timeline:
    """One driver's shift cut into elementary intervals; each is fully worked or fully free."""

    def __init__(self, m: cp_model.CpModel, d: Driver, occupancy: list[tuple[Any, int, int]],
                 opt_y: dict[str, Any] | None = None):
        self.m, self.d = m, d
        opt_y = opt_y or {}
        pts = {d.shift_start, d.shift_end}
        for b in d.blocks:
            pts.update((b.start, b.end))
        for _, s, e in occupancy:
            pts.update((s, e))
        self.pts = sorted(p for p in pts if d.shift_start <= p <= d.shift_end)
        self.n = len(self.pts) - 1
        self.length = [self.pts[k + 1] - self.pts[k] for k in range(self.n)]

        fixed = [False] * self.n
        optional: list[Any] = [None] * self.n            # reallocation var of a replaceable block
        for b in d.blocks:
            y = opt_y.get(b.replace_id) if b.replace_id else None
            for k in range(bisect_left(self.pts, b.start), bisect_left(self.pts, b.end)):
                if y is None:
                    fixed[k] = True
                else:
                    optional[k] = y
        cover: list[list[Any]] = [[] for _ in range(self.n)]
        for v, s, e in occupancy:
            for k in range(bisect_left(self.pts, s), bisect_left(self.pts, e)):
                cover[k].append(v)
        # work[k]: True = regular duty, False = certainly free, BoolVar = depends on express choice
        self.work: list[Any] = []
        self.base_work = [fixed[k] or optional[k] is not None for k in range(self.n)]   # work if nothing changes
        self.base_ok: dict[int, list[bool]] = {}
        for k in range(self.n):
            if fixed[k]:
                self.work.append(True)
            elif optional[k] is not None:
                y = optional[k]
                w = m.NewBoolVar(f"w_{d.driver_id}_{self.pts[k]}")
                m.AddBoolOr([y, w])                        # not reallocated -> the regular trip runs
                for v in cover[k]:
                    m.AddImplication(v, w)
                m.AddBoolOr([y.Not(), w.Not()] + cover[k])  # reallocated -> work only if express covers it
                self.work.append(w)
            elif not cover[k]:
                self.work.append(False)
            else:
                w = m.NewBoolVar(f"w_{d.driver_id}_{self.pts[k]}")
                for v in cover[k]:
                    m.AddImplication(v, w)
                m.AddBoolOr(cover[k] + [w.Not()])          # w = OR(cover): no phantom work
                self.work.append(w)

    def break_starts(self, piece: int) -> list[Any]:
        """z[k]: the `piece` minutes from the start of interval k are free (time after the shift is free).

        Also records base_ok[piece][k]: that break exists if nothing changes (no express, no reallocation)."""
        z: list[Any] = []
        base_ok: list[bool] = []
        for k in range(self.n):
            ws, possible, base, j = [], True, True, k
            while j < self.n and self.pts[j] < self.pts[k] + piece:
                if self.work[j] is True:
                    possible = False
                    break
                if self.work[j] is not False:
                    ws.append(self.work[j])
                base &= not self.base_work[j]
                j += 1
            base_ok.append(possible and base)
            if not possible:
                z.append(None)
            elif not ws:
                z.append(True)
            else:
                zv = self.m.NewBoolVar(f"z{piece}_{self.d.driver_id}_{self.pts[k]}")
                for wv in ws:
                    self.m.AddImplication(zv, wv.Not())
                z.append(zv)
        self.base_ok[piece] = base_ok
        return z

    def _reach(self, order: range) -> dict[int, Any]:
        """r[k] = 'some work at or before k' walking `order` (upper-bounded: no pretend work)."""
        out: dict[int, Any] = {}
        prev: Any = False
        for k in order:
            wk = self.work[k]
            if wk is True or prev is True:
                prev = True
            elif wk is not False:
                p = self.m.NewBoolVar(f"r_{self.d.driver_id}_{order.step}_{k}")
                self.m.Add(p <= (0 if prev is False else prev) + wk)
                prev = p
            out[k] = prev
        return out

    def interior_break_expr(self, z: list[Any]) -> tuple[Any, int]:
        """(linear expr, constant) = minutes of free time in runs that start a break and lie between work."""
        before = self._reach(range(self.n))
        after = self._reach(range(self.n - 1, -1, -1))
        terms, const, q_prev = [], 0, False
        for k in range(self.n):
            wk, bk, ak = self.work[k], before[k], after[k]
            if wk is True or bk is False or ak is False:
                q_prev = False
                continue
            src_true = z[k] is True or q_prev is True
            srcs = [s for s in (z[k], q_prev) if s is not None and s is not True and s is not False]
            if not src_true and not srcs:
                q_prev = False
                continue
            if src_true and wk is False and bk is True and ak is True:
                const += self.length[k]
                q_prev = True
                continue
            q = self.m.NewBoolVar(f"q_{self.d.driver_id}_{self.pts[k]}")
            if not src_true:
                self.m.AddBoolOr(srcs + [q.Not()])
            if wk is not False:
                self.m.AddImplication(q, wk.Not())
            for p in (bk, ak):
                if p is not True:
                    self.m.AddImplication(q, p)
            terms.append((q, self.length[k]))
            q_prev = q
        expr = sum(q * ln for q, ln in terms) if terms else 0
        return expr, const


def _extract_chain(solver: cp_model.CpSolver, gm: _GapModel) -> list[str]:
    first = [tid for tid, v in gm.a.items() if solver.Value(v)]
    if not first:
        return []
    chain = [first[0]]
    nxt = {i1: i2 for (i1, i2), (v, _) in gm.c.items() if solver.Value(v)}
    while chain[-1] in nxt and len(chain) <= len(gm.cand):
        chain.append(nxt[chain[-1]])
    return chain


def describe_violations(minutes: list[int]) -> str:
    return ", ".join(fmt_time(mm) for mm in minutes[:3]) + (" ..." if len(minutes) > 3 else "")
