# 🚍 ZeroStaff Express

**Launch new express buses in Nuremberg without hiring a single new driver.**

An optimizer built on Google OR-Tools that schedules new express trips using the drivers and buses
Nuremberg already has. It takes real VAG operational data as input. Every schedule it produces is
checked against German labour rules by an independent validator.

> Built at the **Claude Impact Lab Hackathon** (Fraunhofer IIS, Nuremberg, 5 Oct 2026),
> Track 1 *"Express Buses & Driver Shortage"*, with Claude Code.

---

## The problem

Nuremberg has enough vehicles to run more express services, but not enough drivers. Hiring is slow and
expensive, and every new line usually means new duties. The challenge question:

> *How can AI help optimise timetables, routes, rotas and available resources so extra express buses can
> launch with as little extra staff as possible, or with the staff already there?*

## Our answer

The optimizer treats the express line as a puzzle laid over the **existing** network. Two modes are
supported:

| Mode | When it applies | What the optimizer does |
|---|---|---|
| **Idle time** | Drivers have idle gaps: split shifts, long layovers, standby/reserve drivers | Fills the gaps with express trips, including empty transfers to and from the route, without extending any shift |
| **Trip reallocation** | Circulations are tight, so there is no idle time (the real VAG data) | Hands selected existing round trips over to the express line. The bus is back for its next scheduled departure, and no branch loses two departures in a row |

Both modes enforce the same hard rules:

- **Zero-New-Staff lock:** only existing drivers, no new hires and no new buses.
- **Shifts and regular service unchanged,** except for trips that are explicitly reallocated.
- **Breaks:**
  - never more than **4 h of work without a ≥ 15-min break**;
  - **ArbZG §4** break totals: ≥ 30 min of breaks after 6 h of work, ≥ 45 min after 9 h;
  - a **10 h** daily working-time cap.
- **Feasible operations:** each trip has exactly one driver, transfer times come from real distances, and
  layovers are respected.

## Results on real data: X30 Nürnberg Hbf → Nordostpark

Input: the VAG PULS API for the morning of 5 Oct 2026, covering 30 departures on Lines 43/44 and 12 observed
vehicles. 29 of 30 vehicle returns were verified against the bus's actual next trip. The X30 is a proposed
18-min express from Hbf to the Nordostpark employment cluster, running 06:30–09:00.

The data shows that buses are **tightly scheduled**. Terminal layovers are 9–13 min, while an X30 round trip
needs about 40 min. So the optimizer runs in **trip-reallocation** mode.

| | Result |
|---|---|
| New drivers / new buses | **0 / 0** |
| X30 departures from Hbf | **06:45, 07:15, 07:45, 08:10** (returns from Nordostpark at 07:08, 07:38, 08:08, 08:33) |
| Line 44 round trips reallocated | **4 of 8** candidates (buses 248, 281, and 3960 twice) |
| Line 44 impact | max headway **20 → 40 min** per branch (Langwasser Mitte, Zerzabelshof Ost); never two cancellations in a row |
| Driver rest | **+36 min** in total (Langwasser cycles: 20 → 40 min of rest each) |
| Traditional approach (dedicated X30 duties) | would need **2 new drivers + 2 buses** (≈ €120k per year in driver costs) |
| Solver | OPTIMAL in about 3 s, **12/12 validator checks passed** |

**Trade-off** (each point is a full optimization run with a different limit on how many Line 44 round trips
may be given up):

| Line 44 round trips handed over | 0 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| X30 departures | 0 | 1 | 2 | 3 | 4 |
| Longest wait for an X30 (min) | 150 | 103 | 71 | 61 | 50 |

> The finding is **not** "we found free buses". We used real vehicle circulation to show where an express
> connection can be created by reallocating existing service, and exactly what it costs.

### Idle-time mode (synthetic and dummy data)

| Dataset | Drivers | Express trips with 0 new hires | Traditional need | What today's staff can run |
|---|---|---|---|---|
| Synthetic depot (`data/demand_input.synthetic.json`) | 40 | 26 / 44 at a 15-min headway (59 %) | 4 new drivers | **100 % at a 30-min headway**; +2 hires → 100 % at 15 min |
| Hand-typed dummy (`data/demand_input.dummy.json`) | 5 | 13 / 32 at a 20-min headway (41 %) | 3 new drivers | 65 % at a 30-min headway |

---

## Quick start

```bash
git clone https://github.com/miteshkumar07/ZeroStaff-Express.git
cd ZeroStaff-Express
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# dashboard (opens on the real-data scenario)
.venv/bin/streamlit run app.py              # http://localhost:8501

# optimizer CLI
.venv/bin/python -m backend.run -i demand_input.json -o data/optimized_schedule.real.json
.venv/bin/python -m backend.run             # data/demand_input.json -> data/optimized_schedule.json

# tests (22)
.venv/bin/python -m pytest -q
```

Useful CLI options:

| Option | Effect |
|---|---|
| `--headway 15` | force a fixed express headway |
| `--max-new-drivers 2` | what-if: relax the zero-staff lock |
| `--time-limit 30` | longer solve |
| `--sweep none --no-baseline --no-curve` | main solve only (fastest) |
| `--log` | show the CP-SAT search log |

The exit code is non-zero if the validator rejects the schedule. Any output file can also be checked on its
own:

```bash
.venv/bin/python -m backend.validator demand_input.json data/optimized_schedule.real.json
```

## Dashboard

`app.py` is a Streamlit app running on the real optimizer. It loads instantly from the precomputed
`data/optimized_schedule*.json` files, and **🔄 Rerun MILP Optimization** re-solves live (about 1–4 s).

- **Scenarios:**
  - real VAG data (X30)
  - synthetic 40-driver depot
  - 5-driver dummy
- **Controls** (in the sidebar):
  - real data: the X30 headway target, the disruption budget (how many Line 44 round trips may be
    reallocated), and how many cancellations in a row a branch may take
  - idle-time scenarios: the express headway, and how many new drivers are allowed
- **🗺️ System Dispatch:**
  - map of the corridor and demand hubs
  - before/after departures at Hbf
  - the express timetable
  - the reallocated trips, with their *Fahrtnummer*
- **📱 Driver Companion App:**
  - before/after Gantt chart for any duty
  - a plain-language plan for the day
  - work and break figures
  - shift download
- **📊 Optimizer Insights:**
  - the trade-off curve, or the headway sweep and staffing curve in idle-time mode
  - impact per branch
  - fleet Gantt chart
  - the validator's report
  - assumptions and open validation items

`app_mockup.py` is the original static UI mockup, kept for reference.

---

## How it works

```text
VGN GTFS + VAG PULS API ──▶ demand_input.json ──▶ backend (OR-Tools CP-SAT) ──▶ optimized_schedule.json ──▶ app.py
     (data pipeline)          (input contract)      model + validator              (output contract)        (dashboard)
```

### The model (`backend/model.py`)

A **0-1 integer linear program** solved with Google OR-Tools CP-SAT.

- **Candidate express trips** come from the route's service windows, weighted by demand (e.g. the morning
  peak towards the jobs cluster). In *flexible* mode the solver designs the timetable itself: it guarantees
  a maximum headway and respects a minimum headway.
- **Connection network per idle gap or freed window:**
  - a transfer in, then a chain of express trips with layovers or transfers between them, then a transfer back
    to wherever the next duty starts;
  - variables `x` (trip driven), `a`/`b` (first/last trip) and `c` (trip-to-trip connection);
  - flow conservation keeps each chain feasible in time.
- **Reallocation decisions:** `y` (hand an existing round trip over). The freed window can host express
  trips only if `y = 1`. A sliding limit stops any branch losing more than *k* departures in a row.
- **Exact event timeline:** each driver's day is cut at every block edge and every possible activity edge,
  so each interval is fully worked or fully free. Breaks are therefore modelled to the minute, with no
  rounding. Off-grid duties such as 09:02–09:32 are handled correctly.
  - **4-hour rule:** for every interval *k* that may be work, `Σ z[j] ≥ w[k]` over break starts *j* with
    `end_k ≤ start_j ≤ start_k + 240`.
  - **ArbZG §4:** interior free time in pieces of ≥ 15 min must total ≥ 30 or ≥ 45 min once work exceeds 6 or 9 h.
- **Objective,** in priority order:
  1. demand-weighted express trips
  2. headway regularity
  3. fewer new hires
  4. fewer reallocations
  5. fewer deadhead minutes
  6. fewer drivers whose day changes
- **Always feasible:** doing nothing is a valid solution. Input duties that already break a rule are
  reported and never made worse.

### Analyses (`backend/run.py`)

Every analysis below reuses the same model:

- **Dedicated-staffing baseline:** how many new drivers (and buses) the traditional approach would need to run
  the same express trips.
- **Headway sweep:** which express frequency today's staff can run (idle-time mode).
- **Staffing curve:** coverage with +0, +1, +2 … new hires.
- **Reallocation curve:** express service gained against existing trips given up (reallocation mode).

### Independent validator (`backend/validator.py`)

It re-checks the **output JSON** minute by minute and shares no logic with the model. The 12 checks:

| Check | What it confirms |
|---|---|
| Solver status | the solver actually produced a schedule |
| Zero new staff | no drivers beyond the input (and no hires beyond the allowed number) |
| All drivers reported | every input driver appears in the output |
| Reallocation limits | no branch loses more departures in a row than allowed |
| Timeline integrity | no overlapping activities, nothing outside a shift |
| Regular service unchanged | every regular trip is still there, except declared reallocations |
| Break rules | the 4-hour rule, ArbZG §4 totals and the daily cap |
| Location continuity | no driver jumps between places without a transfer |
| Transfer times | each transfer is long enough for the distance |
| Timetable match | express trips run at their timetabled times |
| One driver per trip | each express trip is driven by exactly one driver |
| KPI consistency | the headline numbers match the schedule |

The model also went through a fuzzing review: more than 1,500 random inputs, edge cases included, with the
validator checking every result. The bugs it found are now regression tests.

---

## Repository structure

```text
app.py                     Streamlit dashboard (live optimizer)
app_mockup.py              original static UI mockup
backend/
  contract.py              input contract parsing, defaults, time/geo helpers
  adapters.py              VAG PULS export -> contract (trip-reallocation mode)
  model.py                 CP-SAT model (the math)
  schedule.py              output JSON builder + KPIs
  validator.py             independent minute-level checker (also a CLI)
  run.py                   orchestration, analyses, CLI, optimize() Python API
  synthetic.py             realistic synthetic depot generator
data/
  demand_input.dummy.json        5 hand-typed drivers (idle-time mode)
  demand_input.synthetic.json    40-driver synthetic depot
  demand_input.real.json         real data converted to the contract (for inspection)
  demand_input.json              default CLI input (= dummy)
  optimized_schedule*.json       precomputed results the dashboard loads
demand_input.json          REAL VAG PULS export (X30 / Nordostpark) from the data pipeline
tests/test_backend.py      22 pytest tests (real data, break rules, reallocation, fuzz regressions)
.streamlit/config.toml     dark theme (validated chart palette)
```

### Data contracts

- **`demand_input.json`** (input): drivers or buses with duty blocks and/or idle gaps, plus one or more
  express routes with service windows. Blocks may carry `replace_id` / `replace_group` to make them
  reallocatable. The real PULS export (`candidate_vehicle_windows`, …) is detected and converted
  automatically.
- **`optimized_schedule.json`** (output): `meta`, `kpis`, `drivers[]` (before/after segments, breaks,
  itinerary), `gantt[]`, `express_routes[]`, `reallocations[]`, `scenarios[]`, `staffing_curve[]`,
  `reallocation_curve[]`, `map`, `compliance`, `warnings`.

The field-by-field specification is in `CONTRACT.md`.

Python API, e.g. for notebooks or other front ends:

```python
from backend.run import optimize
schedule = optimize("demand_input.json", sweep=None, curve=False)
schedule["kpis"]["new_drivers_required"]   # 0
```

---

## Data sources

- **VGN GTFS static feed:** https://www.vgn.de/opendata/GTFS.zip.
  © VGN – Verkehrsverbund Großraum Nürnberg GmbH, licensed under **CC BY 3.0 DE**.
- **VAG PULS API:** real-time departures, *Fahrtverlauf* and vehicle numbers. The morning of 5 Oct 2026 was
  collected with pipeline scripts `09_fetch_puls_morning.py`, `10_fetch_trip_details.py`,
  `11_verify_return_legs.py` and `12_build_demand_input.py`.
- **City of Nuremberg:**
  - *Wirtschaftsstandort Nürnberg – Positionsbestimmung 2026* (economic context);
  - Geoportal Bayern / OpenStreetMap for coordinates.

## Assumptions & limitations

We keep these explicit, because the judges ask for a clear line between what works and what is stubbed.

- **Duty data.** Real driver duties are not public. The real-data run covers only the observed morning
  circulation (bus and driver as a unit). Duty-level break validation, VAG operational approval and the
  passenger impact of cancelled trips still need checking (listed in `meta.validation_required`).
- **Demand.** Demand weights are structural proxies (employment cluster, transit nodes), not measured
  passenger origin–destination counts. Counter-peak X30 trips are weighted 0.25.
- **Parameters.** The X30 is planned with an 18-min one-way trip, 5-min turnaround and 2-min changeover, with a
  target of a departure at least every 30 min and never closer than every 10 min. All of these are
  parameters.
- **Break rules.**
  - The model enforces the team rule (15 min every 4 h), ArbZG §4 totals and the 10 h cap.
  - City-bus operation under FPersV §1(3) instead counts scheduled breaks (*Wendezeiten*) of ≥ 10 min,
    totalling ≥ 1/6 of driving time, and TV-N collective agreements add their own rules.
  - All break rules are configurable, and the validator enforces whatever is configured.
- **Transfers and map.** Transfer times are straight-line distance × 1.3 at 25 km/h. The map draws the X30
  as a schematic straight line.
- **Cost figure.** The €60k per driver per year cost is an assumption (`annual_cost_per_driver_eur`).

## Tech stack

Python 3.13, Google OR-Tools 9.15 (CP-SAT), Streamlit, Plotly, Folium, pandas, pytest. Built with
Claude Code.
