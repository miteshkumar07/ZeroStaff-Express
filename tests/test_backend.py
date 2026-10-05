"""Run:  .venv/bin/python -m pytest -q"""

import copy
import json
from pathlib import Path

import pytest

from backend.contract import ContractError, load_problem, parse_time
from backend.run import optimize
from backend.synthetic import generate
from backend.validator import check_driver

DATA = Path(__file__).resolve().parents[1] / "data"
DUMMY = json.loads((DATA / "demand_input.dummy.json").read_text())


def tiny(driver: dict, headway: int = 30, **route_kw) -> dict:
    """One driver + a short express route between two points 1 km apart."""
    route = {
        "route_id": "X1", "origin": {"name": "A", "lat": 49.40, "lon": 11.10},
        "destination": {"name": "B", "lat": 49.41, "lon": 11.10}, "travel_time_min": 25,
        "service_windows": [{"start": "06:00", "end": "14:00", "headway_min": headway}],
    }
    route.update(route_kw)
    return {"meta": {"service_date": "2026-10-06"}, "express_route": route, "drivers": [driver]}


def run(raw: dict, **kw) -> dict:
    return optimize(raw, sweep=None, baseline=False, curve=False, time_limit_s=10, **kw)


def test_parse_time_formats():
    assert parse_time("07:05") == 425
    assert parse_time("25:10:00") == 1510          # GTFS after-midnight
    assert parse_time(90) == 90
    assert parse_time("2026-10-06T06:30:00+02:00") == 390


def test_dummy_end_to_end_is_compliant():
    out = optimize(DUMMY, sweep=(20, 30), time_limit_s=10)
    assert out["meta"]["status"] == "OPTIMAL"
    assert out["compliance"]["passed"], out["compliance"]
    k = out["kpis"]
    assert k["new_drivers_required"] == 0
    assert k["express_trips_covered"] > 0
    assert k["dedicated_drivers_needed"] >= 1
    assert out["dedicated_staffing_baseline"]["complete"]
    assert {d["driver_id"] for d in out["drivers"]} == {d["driver_id"] for d in DUMMY["drivers"]}
    # regular duties untouched
    for d_in, d_out in zip(DUMMY["drivers"], out["drivers"]):
        n_blocks = len(d_in.get("work_blocks", []))
        assert sum(s["type"] == "regular_service" for s in d_out["after"]) == n_blocks


def test_break_rule_forces_breaks_for_reserve_driver():
    """8 h standby shift, trips every 30 min A<->B: the driver cannot just drive 8 h in a row."""
    raw = tiny({"driver_id": "R1", "shift_start": "06:00", "shift_end": "14:00",
                "idle_gaps": [{"start": "06:00", "end": "14:00", "location": {"name": "A", "lat": 49.40, "lon": 11.10}}]},
               headway=15)
    out = run(raw)
    assert out["compliance"]["passed"], out["compliance"]
    d = out["drivers"][0]
    assert d["stats"]["after"]["express_trips"] >= 8
    assert d["stats"]["after"]["max_continuous_work_min"] <= 240
    assert d["stats"]["after"]["break_minutes"] >= 30          # ArbZG s.4 (> 6 h work would need 30)


def test_without_break_rule_driver_would_work_longer():
    raw = tiny({"driver_id": "R1", "shift_start": "06:00", "shift_end": "14:00",
                "idle_gaps": [{"start": "06:00", "end": "14:00", "location": {"name": "A", "lat": 49.40, "lon": 11.10}}]},
               headway=15)
    strict = run(raw)["drivers"][0]["stats"]["after"]["max_continuous_work_min"]
    loose = run(raw, rule_overrides={"max_continuous_work_min": 600, "arbzg_break_totals": False})
    assert loose["drivers"][0]["stats"]["after"]["max_continuous_work_min"] > strict


def test_zero_new_staff_lock_and_what_if():
    raw = copy.deepcopy(DUMMY)
    locked = run(raw)
    assert locked["kpis"]["new_drivers_required"] == 0
    relaxed = run(raw, rule_overrides={"max_new_drivers": 2})
    assert relaxed["kpis"]["new_drivers_required"] <= 2
    assert relaxed["kpis"]["express_trips_covered"] >= locked["kpis"]["express_trips_covered"]
    assert relaxed["compliance"]["passed"]


def test_trip_never_in_two_places():
    out = run(copy.deepcopy(DUMMY))
    owners = {}
    for d in out["drivers"]:
        for s in d["after"]:
            if s["type"] == "express":
                assert s["trip_id"] not in owners
                owners[s["trip_id"]] = d["driver_id"]
    assert len(owners) == out["kpis"]["express_trips_covered"]


def test_gap_too_short_gives_nothing():
    raw = tiny({"driver_id": "D1", "shift_start": "06:00", "shift_end": "14:00",
                "work_blocks": [{"start": "06:00", "end": "09:50", "line": "1"},
                                {"start": "10:20", "end": "14:00", "line": "1"}]})
    out = run(raw)
    assert out["kpis"]["express_trips_covered"] == 0
    assert out["compliance"]["passed"]


def test_preexisting_violation_is_reported_not_blamed():
    raw = tiny({"driver_id": "D1", "shift_start": "06:00", "shift_end": "14:00",
                "work_blocks": [{"start": "06:00", "end": "11:00", "line": "1"}]})
    out = run(raw)
    assert any("input duty already exceeds" in w for w in out["warnings"])
    assert out["compliance"]["passed"]


def test_flexible_timetable_respects_min_headway():
    raw = copy.deepcopy(DUMMY)
    r = raw["express_route"]
    r["timetable_mode"] = "flexible"
    for w in r["service_windows"]:
        w["max_headway_min"], w["min_headway_min"] = 20, 10
    out = run(raw)
    assert out["compliance"]["passed"]
    deps = {}
    for t in out["express_routes"][0]["trips"]:
        deps.setdefault((t["window"], t["direction"]), []).append(t["departure_min"])
    for times in deps.values():
        times.sort()
        assert all(b - a >= 10 for a, b in zip(times, times[1:]))


def test_fleet_cap():
    raw = generate(40, seed=3)
    out = run(raw, rule_overrides={"max_express_vehicles": 2})
    assert out["kpis"]["peak_express_vehicles"] <= 2
    assert out["compliance"]["passed"]


def test_synthetic_scale_is_fast():
    out = optimize(generate(80, seed=5), sweep=None, time_limit_s=30)
    assert out["meta"]["status"] in ("OPTIMAL", "FEASIBLE")
    assert out["compliance"]["passed"]


def test_contract_errors_are_clear():
    with pytest.raises(ContractError):
        load_problem({"drivers": []})
    bad = copy.deepcopy(DUMMY)
    bad["drivers"][0]["work_blocks"][0]["end"] = "04:00"
    with pytest.raises(ContractError):
        load_problem(bad)


def test_validator_catches_long_stretch():
    segs = [{"type": "regular_service", "start_min": 300, "end_min": 600}]
    assert check_driver(segs, {"max_continuous_work_min": 240, "min_break_min": 15})["issues"]


# ---- regressions from the code review ---------------------------------------------------------
A = {"name": "A", "lat": 49.40, "lon": 11.10}
B = {"name": "B", "lat": 49.41, "lon": 11.10}


def _route(tt=5, headway=5, start="06:00", end="16:00"):
    return {"route_id": "X1", "origin": A, "destination": B, "travel_time_min": tt,
            "service_windows": [{"start": start, "end": end, "headway_min": headway}]}


def test_off_grid_break_does_not_make_model_infeasible():
    """A legal 30-min break at 09:02-09:32 must not wipe out everyone's express trips."""
    raw = {"meta": {"service_date": "2026-10-06"}, "rules": {"changeover_min": 0, "min_layover_min": 0},
           "express_route": _route(),
           "drivers": [
               {"driver_id": "OFFGRID", "shift_start": "06:00", "shift_end": "12:52",
                "work_blocks": [{"start": "06:00", "end": "09:02", "start_location": A, "end_location": A},
                                {"start": "09:32", "end": "12:40", "start_location": A, "end_location": A}]},
               {"driver_id": "R1", "shift_start": "06:00", "shift_end": "14:00",
                "idle_gaps": [{"start": "06:00", "end": "14:00", "location": A}]}]}
    out = run(raw)
    assert out["meta"]["status"] in ("OPTIMAL", "FEASIBLE")
    assert out["kpis"]["express_trips_covered"] > 20
    assert out["compliance"]["passed"], out["compliance"]
    assert not any("already exceeds" in w for w in out["warnings"])


def test_noncompliant_input_driver_does_not_block_others():
    raw = {"meta": {"service_date": "2026-10-06"}, "express_route": _route(tt=25, headway=30),
           "drivers": [
               {"driver_id": "BAD", "shift_start": "06:00", "shift_end": "12:35",
                "work_blocks": [{"start": "06:00", "end": "12:10", "start_location": A, "end_location": A}]},
               {"driver_id": "R1", "shift_start": "06:00", "shift_end": "14:00",
                "idle_gaps": [{"start": "06:00", "end": "14:00", "location": A}]}]}
    out = run(raw)
    assert out["meta"]["status"] == "OPTIMAL"
    assert out["kpis"]["express_trips_covered"] > 0
    assert out["compliance"]["passed"], out["compliance"]


def test_express_never_fills_an_off_grid_break():
    """16-min break at 09:02-09:18: a 10-min trip would fit, but would destroy the only break."""
    raw = {"meta": {"service_date": "2026-10-06"},
           "rules": {"changeover_min": 0, "min_layover_min": 0, "arbzg_break_totals": False},
           "express_route": _route(tt=10, headway=1, start="09:00", end="09:30"),
           "drivers": [{"driver_id": "D1", "shift_start": "06:00", "shift_end": "12:00",
                        "work_blocks": [{"start": "06:00", "end": "09:02", "start_location": A, "end_location": A},
                                        {"start": "09:18", "end": "12:00", "start_location": A, "end_location": A}]}]}
    out = run(raw)
    assert out["kpis"]["express_trips_covered"] == 0
    assert out["compliance"]["passed"], out["compliance"]


def test_touching_idle_gaps_are_merged_with_locations():
    raw = {"meta": {"service_date": "2026-10-06"}, "express_route": _route(tt=25, headway=30),
           "drivers": [{"driver_id": "D1", "shift_start": "06:00", "shift_end": "14:00",
                        "idle_gaps": [{"start": "06:00", "end": "10:00", "location": A},
                                      {"start": "10:00", "end": "14:00", "location": B}]}]}
    out = run(raw)
    assert out["compliance"]["passed"], out["compliance"]


def test_no_phantom_work_for_arbzg_credit():
    """Idle time before the first / after the last work must never count as an ArbZG break."""
    raw = {"meta": {"service_date": "2026-10-06"}, "express_route": _route(tt=25, headway=30, start="06:00", end="16:30"),
           "drivers": [{"driver_id": "R1", "shift_start": "06:00", "shift_end": "16:30",
                        "idle_gaps": [{"start": "06:00", "end": "16:30", "location": A}]}]}
    out = run(raw)
    assert out["compliance"]["passed"], out["compliance"]
    st = out["drivers"][0]["stats"]["after"]
    if st["work_minutes"] > 360:
        assert st["break_minutes"] >= 30


def test_failed_solve_is_not_reported_as_compliant():
    from backend.validator import validate
    from backend.contract import load_problem
    out = run(copy.deepcopy(DUMMY))
    out["meta"]["status"] = "INFEASIBLE"
    assert not validate(load_problem(copy.deepcopy(DUMMY)), out)["passed"]


# ---- real VAG PULS data: trip reallocation mode -------------------------------------------------
REAL = Path(__file__).resolve().parents[1] / "demand_input.json"
needs_real = pytest.mark.skipif(not REAL.exists(), reason="real demand_input.json not present")


@needs_real
def test_real_data_reallocation_end_to_end():
    raw = json.loads(REAL.read_text())
    out = optimize(raw, sweep=None, baseline=False, curve=False, time_limit_s=20)
    k = out["kpis"]
    assert out["meta"]["status"] == "OPTIMAL"
    assert out["meta"]["mode"] == "trip_reallocation"
    assert out["compliance"]["passed"], out["compliance"]
    assert k["new_drivers_required"] == 0
    assert k["existing_trips_reallocated"] >= 1
    assert k["express_trips_covered"] == 2 * k["existing_trips_reallocated"]     # one X30 round trip each
    for b in k["branch_headways"]:
        assert b["max_headway_after_min"] <= 2 * b["max_headway_before_min"]     # never 2 cancellations in a row
    # every X30 trip is driven by a bus inside the window freed by its own reallocation
    windows = {(r["driver_id"]): [] for r in out["reallocations"]}
    for r in out["reallocations"]:
        windows[r["driver_id"]].append((r["freed_window"]["start_min"], r["freed_window"]["end_min"]))
    for t in out["express_routes"][0]["trips"]:
        assert any(s <= t["departure_min"] and t["arrival_min"] <= e for s, e in windows[t["driver_id"]])


@needs_real
def test_real_data_without_reallocation_runs_nothing():
    """Terminal layovers (9-13 min) are not idle time: without reallocation no X30 trip fits."""
    raw = json.loads(REAL.read_text())
    out = optimize(raw, sweep=None, baseline=False, curve=False, time_limit_s=20,
                   rule_overrides={"max_replacements": 0})
    assert out["kpis"]["express_trips_covered"] == 0
    assert out["compliance"]["passed"]


def test_consecutive_replacement_rule():
    def bus(i, dep):
        h = f"{dep // 60:02d}:{dep % 60:02d}"
        e = dep + 31
        return {"driver_id": f"B{i}", "depot": A, "shift_start": h, "shift_end": f"{(dep + 80) // 60:02d}:{(dep + 80) % 60:02d}",
                "work_blocks": [
                    {"start": h, "end": f"{e // 60:02d}:{e % 60:02d}", "start_location": A, "end_location": {"name": "T"},
                     "replace_id": f"R{i}", "replace_group": "G"},
                    {"start": f"{(e + 11) // 60:02d}:{(e + 11) % 60:02d}", "end": f"{(e + 40) // 60:02d}:{(e + 40) % 60:02d}",
                     "start_location": {"name": "T"}, "end_location": A, "replace_id": f"R{i}", "replace_group": "G"}]}
    raw = {"meta": {"service_date": "2026-10-06"}, "rules": {"changeover_min": 2},
           "express_route": {"route_id": "X", "origin": A, "destination": B, "travel_time_min": 18,
                             "service_windows": [{"start": "06:30", "end": "09:00", "headway_min": 10}]},
           "drivers": [bus(i, 393 + 20 * i) for i in range(4)]}
    out = run(raw)
    assert out["compliance"]["passed"], out["compliance"]
    rep = sorted(r["driver_id"] for r in out["reallocations"])
    assert rep and all(f"B{i}" not in rep or f"B{i + 1}" not in rep for i in range(3))
