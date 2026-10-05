# Data Pipeline

Data-engineering pipeline for ZeroStaff-Express - Track 1:
Express buses despite a driver shortage.

## Purpose

This pipeline combines VGN GTFS timetable data with VAG PULS
operational data to generate structured inputs for the express-bus
optimization stage.

## Pipeline

VGN GTFS
    |
    v
07_extract_target_gtfs.py
    |
    v
Target timetable
    |
    v
09_fetch_puls_morning.py
    |
    v
Real VAG vehicle assignments
    |
    v
10_fetch_trip_details.py
    |
    v
Complete trip trajectories
    |
    v
11_verify_return_legs.py
    |
    v
Verified vehicle circulation
    |
    v
12_build_demand_input.py
    |
    v
demand_input.json

## Main scripts

### 07_extract_target_gtfs.py

Extracts the relevant timetable information from the VGN GTFS feed
for the target Nuremberg routes and service date.

### 09_fetch_puls_morning.py

Queries the VAG PULS API for morning departures at Nuremberg Hbf
and records real Fahrtnummer and Fahrzeugnummer values.

### 10_fetch_trip_details.py

Uses each Fahrtnummer to retrieve the complete PULS Fahrtverlauf,
including stops and scheduled or observed times.

### 11_verify_return_legs.py

Checks whether the same Fahrzeugnummer returns from the terminal
back to Nuremberg Hbf.

### 12_build_demand_input.py

Transforms the validated observations into a compact machine-readable
input for the optimization stage.

## Current result

The sampled morning data contains:

- 30 PULS departures
- 12 observed vehicles
- 29 verified vehicle returns

## Important limitation

Candidate vehicle windows are reallocation or replacement scenarios.
They are not assumed to be idle vehicles or free driver time.

Operational circulation, driver duties, legal requirements and
passenger-service impact require further validation.

## Data sources

- VGN GTFS Open Data
- VAG PULS API
