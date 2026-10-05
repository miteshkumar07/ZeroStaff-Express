"""VAG Express Network Optimizer - dashboard on top of the OR-Tools backend.

    .venv/bin/streamlit run app.py

Default scenarios load instantly from the precomputed data/optimized_schedule*.json files;
"Rerun MILP Optimization" re-solves live with the sidebar settings (backend.run.optimize).
The original static mockup is kept in app_mockup.py.
"""

import json
from pathlib import Path

import folium
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from streamlit_folium import st_folium

from backend.adapters import puls_to_contract
from backend.run import optimize

ROOT = Path(__file__).resolve().parent
SCENARIOS = {
    "Real data · X30 Hbf → Nordostpark (VAG PULS)": (ROOT / "demand_input.json", ROOT / "data/optimized_schedule.real.json"),
    "Synthetic depot · 40 drivers (idle time)": (ROOT / "data/demand_input.synthetic.json",
                                                ROOT / "data/optimized_schedule.synthetic.json"),
    "Dummy · 5 drivers (idle time)": (ROOT / "data/demand_input.dummy.json", ROOT / "data/optimized_schedule.json"),
}
# categorical segment colours - validated (dark surface): lightness band, chroma, CVD & normal-vision separation
SEG = {
    "regular_service": ("Regular service", "#3987e5"),
    "express": ("Express (new)", "#e3000f"),
    "deadhead": ("Changeover / transfer", "#c98500"),
    "break": ("Break", "#199e70"),
    "idle": ("Idle / waiting", "#5f5e5a"),
    "layover": ("Layover < 15 min", "#3d3c39"),
}
SEG_NAME = {k: v[0] for k, v in SEG.items()}
SEG_COLOR = {v[0]: v[1] for v in SEG.values()}
SURFACE = "#0e1117"   # Streamlit dark background: 2px gaps between adjacent bars

st.set_page_config(page_title="VAG Express AI", layout="wide", page_icon="🚍")
st.markdown("""
    <style>
    .main-header { font-size: 32px !important; font-weight: 900; color: #E3000F; }
    .sub-header { font-size: 20px !important; color: #8fb4ff; font-weight: bold; margin-bottom: 6px; }
    .driver-status { background-color: #10B981; color: white; padding: 10px; border-radius: 8px;
                     font-weight: bold; text-align: center; }
    .driver-status.idle { background-color: #3d3c39; }
    </style>
""", unsafe_allow_html=True)


# ==========================================
# DATA
# ==========================================
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


def precomputed(inp: Path, outp: Path) -> dict | None:
    if outp.exists() and inp.exists() and outp.stat().st_mtime >= inp.stat().st_mtime:
        return load_json(str(outp), outp.stat().st_mtime)
    return None


# ==========================================
# SIDEBAR CONTROLS (all of them drive the solver)
# ==========================================
with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/thumb/b/b2/VAG_Logo.svg/330px-VAG_Logo.svg.png", width=150)
    st.markdown("### AI Dispatch Controls")
    scenario = st.selectbox("Scenario / data source", list(SCENARIOS))
    inp, outp = SCENARIOS[scenario]
    if not inp.exists():
        st.error(f"Input file missing: {inp.name}")
        st.stop()
    raw = load_json(str(inp), inp.stat().st_mtime)
    realloc = is_realloc(raw)

    with st.form("controls"):
        if realloc:
            n_opts = len(raw.get("candidate_vehicle_windows") or [])
            defaults = {"x30_headway": 30, "max_realloc": n_opts, "max_in_row": 1}
            x30_headway = st.select_slider("X30 target headway (min)", [20, 30, 40], value=30, key=f"hw_{scenario}",
                                           help="The solver tries to run an X30 at least this often.")
            max_realloc = st.slider("Max Line 44 round trips to reallocate", 0, n_opts, n_opts, key=f"mr_{scenario}",
                                    help="Disruption budget: how many existing round trips may be handed to X30.")
            max_in_row = st.slider("Max cancellations in a row per branch", 1, 3, 1, key=f"row_{scenario}",
                                   help="1 = a branch never loses two departures in a row (headway at most doubles).")
            params = {"x30_headway": x30_headway, "max_realloc": max_realloc, "max_in_row": max_in_row}
        else:
            defaults = {"headway": None, "max_new": 0}
            hw = st.selectbox("Express headway", ["As planned in input", 10, 15, 20, 30], key=f"hw_{scenario}")
            max_new = st.slider("New drivers allowed", 0, 3, 0, help="0 = Zero-New-Staff lock", key=f"new_{scenario}")
            params = {"headway": None if isinstance(hw, str) else int(hw), "max_new": max_new}
        submitted = st.form_submit_button("🔄 Rerun MILP Optimization", type="primary", width="stretch")

if "results" not in st.session_state:
    st.session_state.results, st.session_state.defaults = {}, {}
if scenario not in st.session_state.defaults:
    with st.spinner("Loading optimized schedule…"):
        st.session_state.defaults[scenario] = precomputed(inp, outp) or run_optimizer(
            str(inp), inp.stat().st_mtime, json.dumps(defaults, sort_keys=True), True)
if submitted:
    with st.spinner("Solving with Google OR-Tools CP-SAT…"):
        res = (st.session_state.defaults[scenario] if params == defaults else
               run_optimizer(str(inp), inp.stat().st_mtime, json.dumps(params, sort_keys=True), False))
    st.session_state.results[scenario] = (params, res)
used_params, out = st.session_state.results.get(scenario, (defaults, st.session_state.defaults[scenario]))
default_out = st.session_state.defaults[scenario]
k, meta = out["kpis"], out["meta"]

drivers = out["drivers"]
order = sorted(drivers, key=lambda d: (not d["changed"], d["driver_id"]))
with st.sidebar:
    st.markdown("---")
    st.markdown("### Driver Selector")
    labels = {d["driver_id"]: f"{'✳ ' if d['changed'] else ''}{d['name']}" for d in drivers}
    pick = st.selectbox("View Shift For:", [d["driver_id"] for d in order], key=f"driver_{scenario}",
                        format_func=lambda i: labels.get(i, i))
    if used_params != params:
        st.caption("Settings changed - press **Rerun** to re-optimize.")

# ==========================================
# HEADER + KPIs
# ==========================================
st.markdown("<div class='main-header'>VAG Express Network Optimizer</div>", unsafe_allow_html=True)
st.markdown(f"**{meta.get('scenario_name') or scenario}** · {meta['solver']} · status **{meta['status']}** "
            f"in {meta['solve_time_s']} s")
comp = out.get("compliance") or {}
n_pass = sum(c["passed"] for c in comp.get("checks", []))
n_all = len(comp.get("checks", []))
if comp.get("passed"):
    st.success(f"✅ Independently verified schedule: {n_pass}/{n_all} checks passed "
               "(zero new staff, break rules, regular service, timetable, locations)")
else:
    st.error(f"❌ Validator: {n_pass}/{n_all} checks passed - see Optimizer Insights")

c1, c2, c3, c4 = st.columns(4)
ded = k.get("dedicated_drivers_needed")
if realloc:
    out_deps = sum(1 for r in out["express_routes"] for t in r["trips"] if t["direction"] == "outbound")
    in_deps = sum(1 for r in out["express_routes"] for t in r["trips"] if t["direction"] == "inbound")
    bh = k.get("branch_headways") or []
    worst_before = max((b["max_headway_before_min"] for b in bh), default=0)
    worst_after = max((b["max_headway_after_min"] for b in bh), default=0)
    c1.metric("New drivers · new buses", f"{k['new_drivers_required']} · 0", "Zero-New-Staff lock held", border=True)
    c2.metric("X30 departures 06:30–09:00", f"{out_deps} out · {in_deps} back",
              f"longest wait {k['max_passenger_wait_min']} min", delta_color="off", border=True)
    c3.metric("Line 44 round trips reallocated", f"{k['existing_trips_reallocated']} of {n_opts}",
              f"branch headway {worst_before} → {worst_after} min", delta_color="inverse", border=True)
    c4.metric("Traditional way: new drivers + buses", f"{ded} + {ded}" if ded is not None else "–",
              f"~€{k.get('annual_cost_avoided_eur', 0):,}/yr avoided", border=True)
else:
    c1.metric("New drivers required", k["new_drivers_required"],
              "Zero-New-Staff lock" if k["new_drivers_allowed"] == 0 else f"what-if: ≤ {k['new_drivers_allowed']}",
              border=True)
    c2.metric("Express trips covered", f"{k['express_trips_covered']} / {k['express_trips_target']}",
              f"{k['coverage_pct']}% of timetable", delta_color="off", border=True)
    c3.metric("Idle time turned into service", f"{k['idle_minutes_reclaimed'] / 60:.1f} h",
              f"utilisation {k['utilisation_before_pct']}% → {k['utilisation_after_pct']}%", border=True)
    c4.metric("Traditional approach would need", f"{ded} new drivers" if ded is not None else "–",
              f"~€{k.get('annual_cost_avoided_eur', 0):,}/yr avoided", border=True)

st.markdown("<br>", unsafe_allow_html=True)


# ==========================================
# CHART HELPERS
# ==========================================
def seg_frame(segments: list, row: str) -> pd.DataFrame:
    return pd.DataFrame([{
        "Row": row, "Type": SEG_NAME.get(s["type"], s["type"]), "What": s["label"],
        "Start": pd.to_datetime(s["start_iso"]), "End": pd.to_datetime(s["end_iso"]),
        "Time": f"{s['start']}–{s['end']}", "Minutes": s["duration_min"]} for s in segments])


def gantt(df: pd.DataFrame, rows: list, height: int) -> go.Figure:
    fig = px.timeline(df, x_start="Start", x_end="End", y="Row", color="Type", color_discrete_map=SEG_COLOR,
                      hover_name="What", hover_data={"Time": True, "Minutes": True, "Start": False, "End": False,
                                                     "Row": False, "Type": True},
                      category_orders={"Row": rows, "Type": [v[0] for v in SEG.values()]}, height=height)
    fig.update_traces(marker_line_color=SURFACE, marker_line_width=2)
    fig.update_yaxes(autorange="reversed", title=None)
    fig.update_xaxes(tickformat="%H:%M", title=None, gridcolor="#2a2a28")
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", y=-0.25, title=None))
    return fig


# ==========================================
# TABS
# ==========================================
tab1, tab2, tab3 = st.tabs(["🗺️ System Dispatch (Network Map)", "📱 Driver Companion App (Shift)",
                            "📊 Optimizer Insights"])

# ------------------------------------------
# TAB 1: NETWORK DISPATCH
# ------------------------------------------
with tab1:
    map_col, stat_col = st.columns([2.3, 1])
    routes = out["express_routes"]
    with map_col:
        r0 = routes[0]
        st.markdown(f"<div class='sub-header'>{r0['name']} · {r0['travel_time_min']} min one way</div>",
                    unsafe_allow_html=True)
        o, dst = r0["origin"], r0["destination"]
        center = [(o["lat"] + dst["lat"]) / 2, (o["lon"] + dst["lon"]) / 2] if o.get("lat") and dst.get("lat") \
            else [49.45, 11.08]
        fmap = folium.Map(location=center, zoom_start=13, tiles="OpenStreetMap")
        for hub in (out.get("map") or {}).get("demand_hubs", []):
            if hub.get("lat") is None:
                continue
            w = hub.get("demand_weight") or (hub.get("employees") or 0) / 5000 or 0.6
            folium.CircleMarker([hub["lat"], hub["lon"]], radius=6 + 8 * min(float(w), 1.5), color="#3987e5",
                                weight=2, fill=True, fill_opacity=0.35,
                                tooltip=f"{hub['name']} · {hub.get('type') or 'demand hub'} · weight {w}").add_to(fmap)
        for z in (out.get("map") or {}).get("residential_zones", []):
            folium.CircleMarker([z["lat"], z["lon"]], radius=8, color="#199e70", fill=True, fill_opacity=0.35,
                                tooltip=f"{z['name']} · residential").add_to(fmap)
        for r in routes:
            pts = [r["origin"]] + (r.get("stops") or []) + [r["destination"]]
            pts = [[p["lat"], p["lon"]] for p in pts if p.get("lat") is not None]
            if len(pts) >= 2:
                folium.PolyLine(pts, color="#E3000F", weight=6, opacity=0.9, dash_array="10",
                                tooltip=f"{r['route_id']} express (schematic straight line)").add_to(fmap)
            for p, role in ((r["origin"], "origin"), (r["destination"], "destination")):
                if p.get("lat") is not None:
                    folium.CircleMarker([p["lat"], p["lon"]], radius=9, color="white", weight=2, fill=True,
                                        fill_color="#E3000F", fill_opacity=1,
                                        tooltip=f"{r['route_id']} {role}: {p['name']}").add_to(fmap)
        st_folium(fmap, height=460, use_container_width=True, returned_objects=[])

    with stat_col:
        if realloc:
            st.markdown("<div class='sub-header'>Departures at Hbf: before → after</div>", unsafe_allow_html=True)
            st.caption("Each X30 round trip replaces one Line 44 round trip; the bus is back at Hbf for its next run.")
            rows = []
            replaced = {(r["driver_id"], r["option_id"]) for r in out.get("reallocations", [])}
            for d in drivers:
                for s in d["before"]:
                    if s["type"] == "regular_service" and (s.get("from") or {}).get("name", "").endswith("Hbf"):
                        oid = (s.get("meta") or {}).get("fahrtnummer")
                        branch = s["label"].replace(" Hbf -> ", " → ")
                        gone = (d["driver_id"], oid) in replaced
                        rows.append({"Row": f"{branch} · before", "Time": pd.to_datetime(s["start_iso"]),
                                     "Status": "Line 44 runs", "Bus": d["driver_id"]})
                        rows.append({"Row": f"{branch} · after", "Time": pd.to_datetime(s["start_iso"]),
                                     "Status": "Reallocated to X30" if gone else "Line 44 runs", "Bus": d["driver_id"]})
            for r in routes:
                for t in r["trips"]:
                    if t["direction"] == "outbound":
                        rows.append({"Row": f"{r['route_id']} → {r['destination']['name'].replace('Nürnberg ', '')} (new)",
                                     "Time": pd.to_datetime(t["departure_iso"]), "Status": "X30 departs",
                                     "Bus": t["driver_id"]})
            df = pd.DataFrame(rows)
            if not df.empty:
                fig = px.scatter(df, x="Time", y="Row", color="Status", symbol="Status", hover_data={"Bus": True},
                                 color_discrete_map={"Line 44 runs": "#3987e5", "Reallocated to X30": "#5f5e5a",
                                                     "X30 departs": "#e3000f"},
                                 symbol_map={"Line 44 runs": "circle", "Reallocated to X30": "x-open",
                                             "X30 departs": "star"}, height=330)
                fig.update_traces(marker=dict(size=13, line=dict(width=2, color=SURFACE)))
                row_order = sorted({r["Row"] for r in rows if "·" in r["Row"]},
                                   key=lambda x: (x.split(" · ")[0], x.endswith("after")))
                row_order += sorted({r["Row"] for r in rows if "·" not in r["Row"]})
                fig.update_yaxes(title=None, categoryorder="array", categoryarray=row_order[::-1])
                fig.update_xaxes(tickformat="%H:%M", title=None, gridcolor="#2a2a28")
                fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", y=-0.3, title=None))
                st.plotly_chart(fig, width="stretch")
            for b in k.get("branch_headways", []):
                st.markdown(f"- **{b['group']}**: {b['reallocated']} of {b['departures']} reallocated · "
                            f"max headway {b['max_headway_before_min']} → **{b['max_headway_after_min']} min**")
        else:
            st.markdown("<div class='sub-header'>Express timetable coverage</div>", unsafe_allow_html=True)
            rows = [{"Row": f"{t['direction'].title()} ({r['route_id']})", "Time": pd.to_datetime(t["departure_iso"]),
                     "Status": "Driven by existing staff" if t["covered"] else "Not covered",
                     "Driver": t["driver_id"] or "–"} for r in routes for t in r["trips"]]
            df = pd.DataFrame(rows)
            fig = px.scatter(df, x="Time", y="Row", color="Status", symbol="Status", hover_data={"Driver": True},
                             color_discrete_map={"Driven by existing staff": "#e3000f", "Not covered": "#5f5e5a"},
                             symbol_map={"Driven by existing staff": "circle", "Not covered": "circle-open"},
                             height=260)
            fig.update_traces(marker=dict(size=11))
            fig.update_yaxes(title=None)
            fig.update_xaxes(tickformat="%H:%M", title=None, gridcolor="#2a2a28")
            fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", y=-0.35, title=None))
            st.plotly_chart(fig, width="stretch")
            st.markdown(f"- Longest wait for an express: **{k.get('max_passenger_wait_min')} min**\n"
                        f"- Seats added: **{k['seats_added']:,}** · peak express buses: **{k['peak_express_vehicles']}**\n"
                        f"- Deadhead: **{k['deadhead_minutes']} min**")

    st.markdown("<div class='sub-header'>Express timetable</div>", unsafe_allow_html=True)
    tt = pd.DataFrame([{"Departure": t["departure"], "Arrival": t["arrival"], "From": t["from"], "To": t["to"],
                        "Driven by": t["driver_id"] or "– not covered –", "Trip": t["trip_id"]}
                       for r in routes for t in r["trips"]])
    st.dataframe(tt, hide_index=True, width="stretch", height=min(400, 38 + 35 * max(1, len(tt))))
    if realloc and out.get("reallocations"):
        st.markdown("<div class='sub-header'>Reallocated Line 44 round trips</div>", unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([{
            "Bus": r["driver_id"], "Branch": r["group"],
            "Cancelled trips": " + ".join(f"{c['start']}–{c['end']} ({c.get('fahrtnummer', '')})" for c in r["cancelled"]),
            "Freed window": f"{r['freed_window']['start']}–{r['freed_window']['end']}",
            "X30 trips": ", ".join(r["express_trips"]),
            "Driver rest (min)": f"{r['rest_before_min']} → {r['rest_after_min']}"} for r in out["reallocations"]]),
            hide_index=True, width="stretch")

# ------------------------------------------
# TAB 2: DRIVER COMPANION APP
# ------------------------------------------
with tab2:
    d = next(x for x in drivers if x["driver_id"] == pick)
    st.markdown(f"<div class='sub-header'>Shift overview · {d['name']} ({d['shift_start']}–{d['shift_end']})</div>",
                unsafe_allow_html=True)
    nxt = next((s for s in d["after"] if s["type"] == "express"), None)
    if nxt:
        st.markdown(f"<div class='driver-status'>🟢 NEW TODAY | First express: {nxt['label']} at {nxt['start']}"
                    f" · {d['stats']['express_trips']} express trips · labour rules verified</div><br>",
                    unsafe_allow_html=True)
    else:
        st.markdown("<div class='driver-status idle'>No change to this shift today</div><br>", unsafe_allow_html=True)

    df = pd.concat([seg_frame(d["before"], "Before"), seg_frame(d["after"], "After (optimized)")])
    st.plotly_chart(gantt(df, ["Before", "After (optimized)"], 260), width="stretch")

    left, right = st.columns([1.4, 1])
    with left:
        st.markdown("**Today's plan**")
        st.markdown("\n".join(f"- `{line[:11]}` {line[11:].strip()}" for line in d["itinerary"]))
    with right:
        sb, sa = d["stats"]["before"], d["stats"]["after"]
        m1, m2 = st.columns(2)
        m1.metric("Express trips", sa["express_trips"])
        m2.metric("Longest work stretch", f"{sa['max_continuous_work_min']} min",
                  f"limit {out['rules']['max_continuous_work_min']} min", delta_color="off")
        m1.metric("Work time", f"{sa['work_minutes']} min", f"{sa['work_minutes'] - sb['work_minutes']:+d} vs before",
                  delta_color="off")
        m2.metric("Break time", f"{sa['break_minutes']} min", f"{sa['break_minutes'] - sb['break_minutes']:+d} vs before",
                  delta_color="off")
        if d["breaks"]:
            st.caption("Protected breaks: " + ", ".join(f"{b['start']}–{b['end']}" for b in d["breaks"]))
        st.download_button("⬇️ Download my shift (JSON)", json.dumps(d, indent=2, ensure_ascii=False),
                           file_name=f"shift_{d['driver_id']}.json", mime="application/json", width="stretch")

# ------------------------------------------
# TAB 3: OPTIMIZER INSIGHTS
# ------------------------------------------
with tab3:
    a, b = st.columns(2)
    if realloc:
        curve = out.get("reallocation_curve") or default_out.get("reallocation_curve") or []
        with a:
            st.markdown("<div class='sub-header'>Trade-off: X30 service vs Line 44 trips handed over</div>",
                        unsafe_allow_html=True)
            if curve:
                cdf = pd.DataFrame([{"Line 44 round trips reallocated": c["reallocated"],
                                     "Longest wait for an X30 (min)": c["max_passenger_wait_min"],
                                     "X30 departures": c["express_departures_outbound"]} for c in curve])
                cdf = cdf.drop_duplicates("Line 44 round trips reallocated")
                fig = px.line(cdf, x="Line 44 round trips reallocated", y="Longest wait for an X30 (min)", markers=True,
                              hover_data={"X30 departures": True}, height=300)
                fig.update_traces(line=dict(color="#e3000f", width=2), marker=dict(size=10, line=dict(width=2, color=SURFACE)))
                fig.update_xaxes(dtick=1, gridcolor="#2a2a28")
                fig.update_yaxes(gridcolor="#2a2a28", rangemode="tozero")
                fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(fig, width="stretch")
                st.caption("Each point is a full CP-SAT solve with that disruption budget"
                           + ("" if out.get("reallocation_curve") else " (computed with default rules)") + ".")
        with b:
            st.markdown("<div class='sub-header'>Line 44 impact per branch</div>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame(k.get("branch_headways", [])).rename(columns={
                "group": "Branch", "departures": "Candidates", "reallocated": "Reallocated",
                "max_headway_before_min": "Max wait before", "max_headway_after_min": "Max wait after"}),
                hide_index=True, width="stretch")
            if (default_out.get("meta") or {}).get("assumptions"):
                st.markdown("**Assumptions**")
                st.markdown("\n".join(f"- {x}" for x in default_out["meta"]["assumptions"]))
    else:
        sweep = out.get("scenarios") or default_out.get("scenarios") or []
        curve = out.get("staffing_curve") or default_out.get("staffing_curve") or []
        with a:
            st.markdown("<div class='sub-header'>Which frequency can today's staff run?</div>", unsafe_allow_html=True)
            if sweep:
                sdf = pd.DataFrame([{"Express every (min)": str(s["headway_min"]), "Trips covered (%)": s["coverage_pct"],
                                     "Trips": f"{s['trips_covered']}/{s['trips_target']}"} for s in sweep])
                fig = px.bar(sdf, x="Express every (min)", y="Trips covered (%)", text="Trips", height=300)
                fig.update_traces(marker_color="#e3000f", marker_line_color=SURFACE, marker_line_width=2)
                fig.update_yaxes(range=[0, 105], gridcolor="#2a2a28")
                fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(fig, width="stretch")
                st.caption("Each bar: a zero-new-staff CP-SAT solve at that headway.")
        with b:
            st.markdown("<div class='sub-header'>Coverage vs new hires</div>", unsafe_allow_html=True)
            if curve:
                cdf = pd.DataFrame([{"New drivers": str(c["new_drivers"]), "Trips covered (%)": c["coverage_pct"],
                                     "Trips": f"{c['trips_covered']}/{c['trips_target']}"} for c in curve])
                fig = px.bar(cdf, x="New drivers", y="Trips covered (%)", text="Trips", height=300)
                fig.update_traces(marker_color="#3987e5", marker_line_color=SURFACE, marker_line_width=2)
                fig.update_yaxes(range=[0, 105], gridcolor="#2a2a28")
                fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(fig, width="stretch")
                st.caption(f"Traditional dedicated staffing for the same trips: {ded} new drivers.")

    st.markdown("<div class='sub-header'>Fleet view · all changed duties after optimization</div>",
                unsafe_allow_html=True)
    changed = [x for x in order if x["changed"]][:25]
    if changed:
        fdf = pd.concat([seg_frame(x["after"], x["driver_id"]) for x in changed])
        st.plotly_chart(gantt(fdf, [x["driver_id"] for x in changed], 90 + 34 * len(changed)), width="stretch")
    else:
        st.info("No duty changed in this scenario.")

    st.markdown("<div class='sub-header'>Independent validator</div>", unsafe_allow_html=True)
    st.dataframe(pd.DataFrame([{"Check": c["name"].replace("_", " "), "Result": "✅ pass" if c["passed"] else "❌ fail",
                                "Detail": c["detail"]} for c in comp.get("checks", [])]),
                 hide_index=True, width="stretch")
    with st.expander("Warnings, open validation items & raw output"):
        for w in out.get("warnings", []):
            st.warning(w)
        for v in (default_out.get("meta") or {}).get("validation_required") or []:
            st.markdown(f"- ⏳ needs validation: {v}")
        st.download_button("⬇️ Download optimized_schedule.json", json.dumps(out, indent=2, ensure_ascii=False),
                           file_name="optimized_schedule.json", mime="application/json")
