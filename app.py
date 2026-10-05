"""ZeroStaff Express - results-first dashboard on top of the OR-Tools backend.

    .venv/bin/streamlit run app.py

Default scenarios load instantly from data/optimized_schedule*.json; "Run optimizer" re-solves live.
The original static mockup is kept in app_mockup.py.
"""

import hashlib
import json
from pathlib import Path

import folium
import pandas as pd
import plotly.express as px
import streamlit as st
from folium.plugins import AntPath
from streamlit_folium import st_folium

from backend.adapters import puls_to_contract
from backend.run import optimize

ROOT = Path(__file__).resolve().parent
SCENARIOS = {
    "🚍 Real VAG data · X30 Hbf → Nordostpark": (ROOT / "demand_input.json", ROOT / "data/optimized_schedule.real.json"),
    "🧪 Synthetic depot · 40 drivers": (ROOT / "data/demand_input.synthetic.json",
                                       ROOT / "data/optimized_schedule.synthetic.json"),
    "🧪 Dummy · 5 drivers": (ROOT / "data/demand_input.dummy.json", ROOT / "data/optimized_schedule.json"),
}
SHAPES_FILE = ROOT / "data/route_shapes.json"
RED, BLUE, AMBER, AQUA, GREY = "#e3000f", "#3987e5", "#c98500", "#199e70", "#5f5e5a"
SEG = {"regular_service": ("Regular service", BLUE), "express": ("Express (new)", RED),
       "deadhead": ("Changeover / transfer", AMBER), "break": ("Break", AQUA),
       "idle": ("Waiting / idle", GREY), "layover": ("Short layover", "#3d3c39")}
SURFACE = "#0e1117"

st.set_page_config(page_title="ZeroStaff Express", layout="wide", page_icon="🚍")
st.markdown("""
<style>
.block-container {padding-top: 1.6rem; max-width: 1400px;}
.hero {background: radial-gradient(1200px 300px at 10% -20%, rgba(227,0,15,.55), transparent 60%),
        linear-gradient(135deg, #1a0a0c 0%, #0e1117 60%); border: 1px solid #3a1418;
        border-radius: 22px; padding: 30px 34px; margin-bottom: 18px;}
.hero h1 {font-size: 46px; font-weight: 900; margin: 0; letter-spacing: -1px;}
.hero h1 span {color: #ff2a36;}
.hero .sub {font-size: 21px; color: #e9e7df; margin-top: 8px;}
.hero .sub b {color: #fff;}
.badge {display: inline-block; margin-top: 14px; padding: 6px 14px; border-radius: 999px; font-size: 14px;
        background: rgba(25,158,112,.18); border: 1px solid #199e70; color: #5ee6b0; font-weight: 600;}
.badge.bad {background: rgba(227,0,15,.15); border-color: #e3000f; color: #ff7b82;}
.kpis {display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 8px;}
.kpi {background: #16171c; border: 1px solid #2a2b31; border-radius: 18px; padding: 18px 20px;}
.kpi .l {color: #9a99a3; font-size: 14px; font-weight: 600;}
.kpi .v {font-size: 44px; font-weight: 900; line-height: 1.1; margin-top: 4px;}
.kpi .d {color: #c3c2b7; font-size: 14px; margin-top: 4px;}
.kpi.red .v {color: #ff2a36;} .kpi.green .v {color: #5ee6b0;}
.section {font-size: 26px; font-weight: 800; margin: 26px 0 4px;}
.hint {color: #9a99a3; font-size: 15px; margin-bottom: 10px;}
.maptitle {font-size: 18px; font-weight: 800; margin: 2px 0 6px;}
.maptitle.before {color: #8fb4ff;} .maptitle.after {color: #ff5a63;}
.chips {display: flex; flex-wrap: wrap; gap: 10px; margin: 6px 0 4px;}
.chip {background: linear-gradient(135deg, #e3000f, #9c0009); color: #fff; font-weight: 800; font-size: 22px;
       padding: 10px 18px; border-radius: 14px; box-shadow: 0 6px 18px rgba(227,0,15,.25);}
.chip small {display: block; font-size: 12px; font-weight: 600; opacity: .85;}
.chip.back {background: #22232a; box-shadow: none; border: 1px solid #3a3b42; font-size: 18px;}
@media (max-width: 900px) {.kpis {grid-template-columns: 1fr 1fr;}}
</style>
""", unsafe_allow_html=True)


# ============================================================ data
def is_realloc(raw: dict) -> bool:
    return "candidate_vehicle_windows" in raw and "drivers" not in raw


@st.cache_data(show_spinner=False)
def load_json(path: str, mtime: float) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@st.cache_data(show_spinner=False, max_entries=32)
def run_optimizer(path: str, mtime: float, params_json: str, full: bool) -> dict:
    p = json.loads(params_json)
    raw = load_json(path, mtime)
    if is_realloc(raw):
        src = puls_to_contract(raw, max_headway_min=p["x30_headway"])
        src["rules"].update({"max_replacements": p["max_realloc"],
                             "max_consecutive_replacements_per_group": p["max_in_row"]})
        return optimize(src, sweep=None, baseline=True, curve=full, time_limit_s=15, sweep_time_limit_s=6)
    return optimize(path, rule_overrides={"max_new_drivers": p["max_new"]}, headway_min=p["headway"],
                    sweep=(10, 15, 20, 30) if full else None, baseline=True, curve=full,
                    time_limit_s=15, sweep_time_limit_s=5)


def precomputed(inp: Path, outp: Path):
    if outp.exists() and inp.exists() and outp.stat().st_mtime >= inp.stat().st_mtime:
        return load_json(str(outp), outp.stat().st_mtime)
    return None


shapes = load_json(str(SHAPES_FILE), SHAPES_FILE.stat().st_mtime) if SHAPES_FILE.exists() else {}

# ============================================================ sidebar
with st.sidebar:
    st.markdown("## 🚍 ZeroStaff Express")
    scenario = st.selectbox("Data", list(SCENARIOS))
    inp, outp = SCENARIOS[scenario]
    raw = load_json(str(inp), inp.stat().st_mtime)
    realloc = is_realloc(raw)
    with st.form("controls"):
        st.markdown("**Optimizer settings**")
        if realloc:
            n_opts = len(raw.get("candidate_vehicle_windows") or [])
            defaults = {"x30_headway": 30, "max_realloc": n_opts, "max_in_row": 1}
            params = {
                "x30_headway": st.select_slider("X30 every … min (target)", [20, 30, 40], 30, key=f"hw_{scenario}"),
                "max_realloc": st.slider("Line 44 trips we may hand over", 0, n_opts, n_opts, key=f"mr_{scenario}"),
                "max_in_row": st.slider("Max missing Line 44 buses in a row", 1, 3, 1, key=f"row_{scenario}"),
            }
        else:
            defaults = {"headway": None, "max_new": 0}
            hw = st.selectbox("Express every … min", ["as planned", 10, 15, 20, 30], key=f"hw_{scenario}")
            params = {"headway": None if isinstance(hw, str) else int(hw),
                      "max_new": st.slider("New drivers allowed", 0, 3, 0, key=f"new_{scenario}")}
        submitted = st.form_submit_button("🚀 Run optimizer", type="primary", width="stretch")
    st.caption("Google OR-Tools CP-SAT · every plan re-checked by an independent validator")

if "defaults" not in st.session_state:
    st.session_state.defaults, st.session_state.results = {}, {}
if scenario not in st.session_state.defaults:
    with st.spinner("Loading optimized schedule…"):
        st.session_state.defaults[scenario] = precomputed(inp, outp) or run_optimizer(
            str(inp), inp.stat().st_mtime, json.dumps(defaults, sort_keys=True), True)
if submitted:
    with st.spinner("🧠 Optimizing with OR-Tools CP-SAT…"):
        res = st.session_state.defaults[scenario] if params == defaults else run_optimizer(
            str(inp), inp.stat().st_mtime, json.dumps(params, sort_keys=True), False)
    st.session_state.results[scenario] = res
    st.toast(f"Optimized: {res['meta']['status']} in {res['meta']['solve_time_s']} s", icon="✅")
out = st.session_state.results.get(scenario, st.session_state.defaults[scenario])
default_out = st.session_state.defaults[scenario]
k, meta = out["kpis"], out["meta"]
routes, drivers = out["express_routes"], out["drivers"]
run_id = hashlib.md5(json.dumps([k.get("express_trips_covered"), k.get("existing_trips_reallocated"),
                                 [t["trip_id"] for r in routes for t in r["trips"] if t["covered"]]]).encode()).hexdigest()[:8]
comp = out.get("compliance") or {}
n_pass, n_all = sum(c["passed"] for c in comp.get("checks", [])), len(comp.get("checks", []))
covered = [t for r in routes for t in r["trips"] if t["covered"]]
outbound = sorted((t for t in covered if t["direction"] == "outbound"), key=lambda t: t["departure_min"])
inbound = sorted((t for t in covered if t["direction"] == "inbound"), key=lambda t: t["departure_min"])
r0 = routes[0]
ded = k.get("dedicated_drivers_needed") or 0

# ============================================================ hero + KPIs
if realloc:
    headline = (f"The new <b>{r0['route_id']}</b> runs <b>{len(outbound)}×</b> this morning with "
                f"<b>0 new drivers</b> and <b>0 new buses</b>")
else:
    headline = (f"<b>{k['express_trips_covered']}</b> express trips run with <b>{k['new_drivers_required']} new drivers</b>, "
                f"using idle time of today's staff")
badge = (f"<span class='badge'>✅ Verified · {n_pass}/{n_all} safety checks passed · {meta['status']} in "
         f"{meta['solve_time_s']} s</span>" if comp.get("passed") else
         f"<span class='badge bad'>❌ {n_pass}/{n_all} checks passed</span>")
st.markdown(f"<div class='hero'><h1>ZeroStaff <span>Express</span></h1>"
            f"<div class='sub'>{headline}.</div>{badge}</div>", unsafe_allow_html=True)

if realloc:
    bh = k.get("branch_headways") or []
    wb = max((b["max_headway_before_min"] for b in bh), default=0)
    wa = max((b["max_headway_after_min"] for b in bh), default=0)
    cards = [("green", "New drivers · new buses", "0 · 0", "Zero-New-Staff lock"),
             ("red", f"New {r0['route_id']} trips", f"{len(outbound)} + {len(inbound)}",
              f"out + back · longest wait {k['max_passenger_wait_min']} min"),
             ("", "Line 44 trips handed over", f"{k['existing_trips_reallocated']} / {n_opts}",
              f"bus every {wb} → max {wa} min on a branch"),
             ("", "Hiring the usual way", f"{ded} + {ded}", f"drivers + buses · ≈ €{k.get('annual_cost_avoided_eur', 0):,}/yr saved")]
else:
    cards = [("green", "New drivers", str(k["new_drivers_required"]),
              "Zero-New-Staff lock" if not k["new_drivers_allowed"] else f"what-if ≤ {k['new_drivers_allowed']}"),
             ("red", "Express trips running", f"{k['express_trips_covered']}/{k['express_trips_target']}",
              f"{k['coverage_pct']}% of the timetable"),
             ("", "Idle time put to work", f"{k['idle_minutes_reclaimed'] / 60:.1f} h",
              f"utilisation {k['utilisation_before_pct']}% → {k['utilisation_after_pct']}%"),
             ("", "Hiring the usual way", f"{ded} drivers", f"≈ €{k.get('annual_cost_avoided_eur', 0):,}/yr saved")]
st.markdown("<div class='kpis'>" + "".join(
    f"<div class='kpi {c}'><div class='l'>{l}</div><div class='v'>{v}</div><div class='d'>{d}</div></div>"
    for c, l, v, d in cards) + "</div>", unsafe_allow_html=True)


# ============================================================ maps: before vs after
def label(loc, text, color):
    return folium.Marker(loc, icon=folium.DivIcon(icon_size=(0, 0), html=(
        f"<div style='transform:translate(-50%,-140%);white-space:nowrap;background:{color};color:#fff;"
        f"font:700 12px sans-serif;padding:4px 8px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.5)'>{text}</div>")))


def stop(fmap, loc, name, color):
    folium.CircleMarker(loc, radius=8, color="#fff", weight=2, fill=True, fill_color=color, fill_opacity=1,
                        tooltip=name).add_to(fmap)


def build_map(after: bool) -> folium.Map:
    o, dst = r0["origin"], r0["destination"]
    express = shapes.get(r0["route_id"], {}).get("coords") or [[o["lat"], o["lon"]], [dst["lat"], dst["lon"]]]
    lats = [p[0] for p in express]
    lons = [p[1] for p in express]
    branches = {g: v for g, v in shapes.items() if g.startswith("Line ")} if realloc else {}
    for v in branches.values():
        lats += [p[0] for p in v["coords"]]
        lons += [p[1] for p in v["coords"]]
    center = [(min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2 + 0.012]
    fmap = folium.Map(location=center, zoom_start=12, zoom_control=True,
                      tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
                      attr="Esri, OpenStreetMap contributors")
    folium.TileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
                     attr="Esri", overlay=True, control=False).add_to(fmap)
    by_group = {b["group"]: b for b in (k.get("branch_headways") or [])}
    for g, v in branches.items():
        b = by_group.get(g, {})
        total, gone = b.get("departures", 0), b.get("reallocated", 0) if after else 0
        name = g.replace("Line 44 -> ", "")
        folium.PolyLine(v["coords"], color=BLUE, weight=7 if not gone else 4, opacity=0.95 if not gone else 0.55,
                        tooltip=f"{g}: {total - gone} of {total} trips run").add_to(fmap)
        txt = f"44 → {name} · {total} trips" if not after else f"44 → {name} · {total - gone}/{total} trips"
        label(v["coords"][-1], txt, "#1f4f8f").add_to(fmap)
        stop(fmap, v["coords"][-1], name, BLUE)
    if after and covered:
        AntPath(express, color=RED, pulse_color="#ffd0d3", weight=8, delay=600, dash_array=[18, 26],
                tooltip=f"{r0['route_id']} express · {len(covered)} trips").add_to(fmap)
        txt = f"{r0['route_id']} NEW · {len(outbound)} departures"
        label(express[len(express) // 2], txt, RED).add_to(fmap)
    elif not after:
        folium.PolyLine(express, color="#8a897f", weight=3, dash_array="4 10", opacity=0.8,
                        tooltip=f"{r0['route_id']} corridor: no direct bus today").add_to(fmap)
        label(express[len(express) // 2], "no direct bus today", "#3d3c39").add_to(fmap)
    stop(fmap, express[0], o["name"], RED if after else "#8a897f")
    stop(fmap, express[-1], dst["name"], RED if after else "#8a897f")
    label(express[0], o["name"].replace("Nürnberg ", ""), "#26272e").add_to(fmap)
    label(express[-1], dst["name"].replace("Nürnberg ", ""), "#26272e").add_to(fmap)
    return fmap


st.markdown("<div class='section'>🗺️ Before vs after</div>", unsafe_allow_html=True)
st.markdown("<div class='hint'>Left: today's network. Right: the optimizer's plan. Run the optimizer with "
            "different settings in the sidebar and the right map updates.</div>", unsafe_allow_html=True)
m1, m2 = st.columns(2)
with m1:
    st.markdown("<div class='maptitle before'>BEFORE · today</div>", unsafe_allow_html=True)
    st_folium(build_map(False), height=480, use_container_width=True, returned_objects=[], key=f"mb_{scenario}")
with m2:
    st.markdown("<div class='maptitle after'>AFTER · optimized</div>", unsafe_allow_html=True)
    st_folium(build_map(True), height=480, use_container_width=True, returned_objects=[], key=f"ma_{run_id}")

# ============================================================ new timetable
st.markdown(f"<div class='section'>🕒 New {r0['route_id']} timetable</div>", unsafe_allow_html=True)
if outbound:
    st.markdown(f"<div class='hint'>{r0['origin']['name']} → {r0['destination']['name']} · "
                f"{r0['travel_time_min']} min</div>", unsafe_allow_html=True)
    st.markdown("<div class='chips'>" + "".join(
        f"<div class='chip'>{t['departure']}<small>{t['driver_id'].replace('BUS-', 'bus ')}</small></div>"
        for t in outbound[:14]) + "</div>", unsafe_allow_html=True)
    if inbound:
        st.markdown(f"<div class='hint' style='margin-top:10px'>Back: {r0['destination']['name']} → "
                    f"{r0['origin']['name']}</div>", unsafe_allow_html=True)
        st.markdown("<div class='chips'>" + "".join(f"<div class='chip back'>{t['departure']}</div>"
                                                   for t in inbound[:14]) + "</div>", unsafe_allow_html=True)
else:
    st.info("No express trips with these settings. Allow more trips to be handed over and run again.")

if realloc and out.get("reallocations"):
    st.markdown("<div class='hint' style='margin-top:14px'>Paid for by handing over these Line 44 round trips "
                "(same bus, same driver, back at Hbf for the next run):</div>", unsafe_allow_html=True)
    st.dataframe(pd.DataFrame([{
        "Bus": r["driver_id"].replace("BUS-", ""), "Branch": r["group"].replace("Line 44 -> ", "44 → "),
        "Line 44 trip handed over": " + ".join(f"{c['start']}–{c['end']}" for c in r["cancelled"]),
        "Now runs X30": ", ".join(t.split("-")[-1][:2] + ":" + t.split("-")[-1][2:] for t in r["express_trips"]),
        "Driver rest": f"{r['rest_before_min']} → {r['rest_after_min']} min"} for r in out["reallocations"]]),
        hide_index=True, width="stretch")

# ============================================================ details
st.markdown("<div class='section'>🔍 Look closer</div>", unsafe_allow_html=True)
t1, t2, t3 = st.tabs(["👤 A driver's day", "📈 Trade-off", "🛡️ Safety checks"])

with t1:
    order = sorted(drivers, key=lambda d: (not d["changed"], d["driver_id"]))
    names = {d["driver_id"]: f"{'⭐ ' if d['changed'] else ''}{d['name']}" for d in order}
    pick = st.selectbox("Driver / bus", list(names), format_func=lambda i: names.get(i, i), key=f"drv_{scenario}")
    d = next((x for x in drivers if x["driver_id"] == pick), order[0])
    rows = []
    for phase, segs in (("Before", d["before"]), ("After", d["after"])):
        for s in segs:
            nm, _ = SEG.get(s["type"], (s["type"], GREY))
            rows.append({"Row": phase, "Type": nm, "What": s["label"], "Start": pd.to_datetime(s["start_iso"]),
                         "End": pd.to_datetime(s["end_iso"]), "Time": f"{s['start']}–{s['end']}"})
    fig = px.timeline(pd.DataFrame(rows), x_start="Start", x_end="End", y="Row", color="Type",
                      color_discrete_map={v[0]: v[1] for v in SEG.values()}, hover_name="What",
                      hover_data={"Time": True, "Start": False, "End": False, "Row": False},
                      category_orders={"Row": ["Before", "After"]}, height=230)
    fig.update_traces(marker_line_color=SURFACE, marker_line_width=2)
    fig.update_yaxes(autorange="reversed", title=None)
    fig.update_xaxes(tickformat="%H:%M", title=None, gridcolor="#2a2a28")
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", y=-0.3, title=None))
    st.plotly_chart(fig, width="stretch")
    st.markdown("\n".join(f"- `{line[:11]}` {line[11:].strip()}" for line in d["itinerary"]))

with t2:
    if realloc:
        curve = out.get("reallocation_curve") or default_out.get("reallocation_curve") or []
        if curve:
            cdf = pd.DataFrame([{"Line 44 trips handed over": c["reallocated"],
                                 "Longest wait for an X30 (min)": c["max_passenger_wait_min"]} for c in curve]
                               ).drop_duplicates("Line 44 trips handed over")
            fig = px.line(cdf, x="Line 44 trips handed over", y="Longest wait for an X30 (min)", markers=True, height=320)
            fig.update_traces(line=dict(color=RED, width=3), marker=dict(size=12, line=dict(width=2, color=SURFACE)))
            fig.update_xaxes(dtick=1, gridcolor="#2a2a28")
            fig.update_yaxes(gridcolor="#2a2a28", rangemode="tozero")
            fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, width="stretch")
            st.caption("Each point is a full optimization run. The city picks the trade-off; the AI shows the exact cost.")
    else:
        sweep = out.get("scenarios") or default_out.get("scenarios") or []
        if sweep:
            sdf = pd.DataFrame([{"Express every (min)": str(s["headway_min"]), "Trips covered (%)": s["coverage_pct"]}
                                for s in sweep])
            fig = px.bar(sdf, x="Express every (min)", y="Trips covered (%)", text="Trips covered (%)", height=320)
            fig.update_traces(marker_color=RED)
            fig.update_yaxes(range=[0, 105], gridcolor="#2a2a28")
            fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, width="stretch")
            st.caption("Which express frequency today's staff can run, with zero new drivers.")

with t3:
    st.dataframe(pd.DataFrame([{"Check": c["name"].replace("_", " ").capitalize(),
                                "Result": "✅" if c["passed"] else "❌", "Detail": c["detail"]}
                               for c in comp.get("checks", [])]), hide_index=True, width="stretch")
    assumptions = (default_out.get("meta") or {}).get("assumptions")
    if assumptions:
        with st.expander("Assumptions"):
            st.markdown("\n".join(f"- {a}" for a in assumptions))
