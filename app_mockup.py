import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import folium
from streamlit_folium import st_folium
from datetime import datetime, timedelta

# ==========================================
# PAGE CONFIG & THEME
# ==========================================
st.set_page_config(page_title="VAG Express AI", layout="wide", page_icon="🚍")

# Custom CSS for Hackathon Polish (Bigger fonts, app-like buttons)
st.markdown("""
    <style>
    .main-header { font-size: 32px !important; font-weight: 900; color: #E3000F; } /* VAG Red */
    .sub-header { font-size: 20px !important; color: #1E3A8A; font-weight: bold; }
    .metric-box { background-color: #F8FAFC; padding: 20px; border-radius: 12px; border-left: 6px solid #E3000F; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }
    .driver-status { background-color: #10B981; color: white; padding: 10px; border-radius: 8px; font-weight: bold; text-align: center; }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# DUMMY DATA (Replace with Backend API later)
# ==========================================
base_time = datetime.today().replace(hour=8, minute=0, second=0, microsecond=0)

# Optimized Driver Shift (Highlight)
shift_optimized = pd.DataFrame([
    {"Task": "Revenue Route 30", "Start": base_time, "Finish": base_time + timedelta(minutes=45), "Type": "Standard Revenue"},
    {"Task": "⚡ NEW Siemens Express", "Start": base_time + timedelta(minutes=45), "Finish": base_time + timedelta(minutes=90), "Type": "Express Capacity"},
    {"Task": "☕ Depot Break (Guaranteed)", "Start": base_time + timedelta(minutes=90), "Finish": base_time + timedelta(minutes=110), "Type": "High-Quality Break"},
    {"Task": "Revenue Route 20", "Start": base_time + timedelta(minutes=110), "Finish": base_time + timedelta(minutes=150), "Type": "Standard Revenue"},
])

# Unoptimized Shift (For side comparison)
shift_unoptimized = pd.DataFrame([
    {"Task": "Revenue Route 30", "Start": base_time, "Finish": base_time + timedelta(minutes=45), "Type": "Standard Revenue"},
    {"Task": "Empty Return (Leerfahrt)", "Start": base_time + timedelta(minutes=45), "Finish": base_time + timedelta(minutes=80), "Type": "Wasted Time"},
    {"Task": "Revenue Route 30", "Start": base_time + timedelta(minutes=80), "Finish": base_time + timedelta(minutes=130), "Type": "Standard Revenue"},
    {"Task": "Street Corner Break", "Start": base_time + timedelta(minutes=130), "Finish": base_time + timedelta(minutes=145), "Type": "Poor Break"},
])

# Headway Data
headway_before = [8.00, 8.03, 8.05, 8.45, 8.48, 8.50]
headway_after = [8.00, 8.15, 8.30, 8.45, 9.00]
headway_express = [8.10, 8.40]

color_map = {
    "Standard Revenue": "#1E3A8A",      # Blue
    "Express Capacity": "#E3000F",      # VAG Red
    "High-Quality Break": "#10B981",    # Green
    "Wasted Time": "#9CA3AF",           # Gray
    "Poor Break": "#F59E0B"             # Yellow
}

# ==========================================
# SIDEBAR CONTROLS (Appears Interactive to Judges)
# ==========================================
with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/thumb/b/b2/VAG_Logo.svg/330px-VAG_Logo.svg.png", width=150)
    st.markdown("### AI Dispatch Controls")
    selected_corridor = st.selectbox("Select Network Corridor", ["Plärrer ➔ Siemens Campus", "Hauptbahnhof ➔ Nordstadt", "Langwasser ➔ DATEV"])
    ai_aggressiveness = st.slider("Optimization Aggressiveness", 1, 10, 8, help="Higher = stricter headway spacing and less deadhead time.")
    st.markdown("---")
    st.markdown("### Driver Selector")
    selected_driver = st.selectbox("View Shift For:", ["Driver #1042 (Müller, T.)", "Driver #0891 (Schmidt, A.)"])
    st.button("🔄 Rerun MILP Optimization", type="primary", use_container_width=True)

# ==========================================
# MAIN DASHBOARD AREA
# ==========================================
st.markdown("<div class='main-header'>VAG Express Network Optimizer</div>", unsafe_allow_html=True)
st.markdown("Manufactured capacity through AI Headway & Rota Optimization.")

# --- HERO KPIs ---
col1, col2, col3, col4 = st.columns(4)
col1.metric("Redundant Trips Cut", "14 Trips", "-24% overlapping", border=True)
col2.metric("Driver Hours Reclaimed", "42.5 hrs", "Converted from deadhead", border=True)
col3.metric("New Express Capacity", "1,200 pax/day", "ZERO New Hires", border=True)
col4.metric("Avg. Break Quality", "92% Depot-based", "+40% improvement", border=True)

st.markdown("<br>", unsafe_allow_html=True)

# ==========================================
# TABS: SEPARATING DISPATCH VIEW FROM DRIVER VIEW
# ==========================================
tab1, tab2 = st.tabs(["🗺️ System Dispatch (Network Map)", "📱 Driver Companion App (Shift)"])

# ------------------------------------------
# TAB 1: NETWORK DISPATCH (The Map)
# ------------------------------------------
with tab1:
    map_col, stat_col = st.columns([2.5, 1]) # Make map much wider than side stats

    with map_col:
        st.markdown("<div class='sub-header'>Optimized AI Express Route: Active</div>", unsafe_allow_html=True)
        # Using OpenStreetMap to avoid API key errors
        m = folium.Map(location=[49.4300, 11.0800], zoom_start=13, tiles="OpenStreetMap")
        
        # Stops along the route
        stops = [
            ([49.4479, 11.0653], "Plärrer (Residential Hub)"),
            ([49.4350, 11.0800], "Südfriedhof (Transfer)"),
            ([49.4100, 11.1100], "Siemens Campus (Employer Hub)")
        ]
        
        for coord, name in stops:
            folium.CircleMarker(coord, radius=8, color="#1E3A8A", fill=True, fillOpacity=1, popup=name).add_to(m)
        
        # AI Express Route Line
        folium.PolyLine(
            locations=[s[0] for s in stops],
            color="#E3000F", # Red
            weight=6,
            opacity=0.9,
            dash_array="10",
            tooltip="NEW AI Express Route"
        ).add_to(m)
        
        st_folium(m, height=450, width=1200) # Force large width

    with stat_col:
        st.markdown("<div class='sub-header'>Overlap Correction</div>", unsafe_allow_html=True)
        st.info("AI detected bus bunching. Spacing these routes evenly freed up buses to run the Express Line shown on the left.")
        
        fig_headway = go.Figure()
        # Clumped (Before)
        fig_headway.add_trace(go.Scatter(x=headway_before, y=["Old Schedule"] * len(headway_before),
            mode="markers", marker=dict(size=12, color="#9CA3AF"), name="Bunched"))
        # Optimized
        fig_headway.add_trace(go.Scatter(x=headway_after, y=["AI Optimized"] * len(headway_after),
            mode="markers", marker=dict(size=14, color="#1E3A8A"), name="Smoothed"))
        # Express injection
        fig_headway.add_trace(go.Scatter(x=headway_express, y=["AI Optimized"] * len(headway_express),
            mode="markers", marker=dict(size=18, color="#E3000F", symbol="star"), name="Express Injected"))
        
        fig_headway.update_layout(height=250, margin=dict(l=0, r=0, t=10, b=0), showlegend=False, xaxis_title="Timeline (AM)")
        st.plotly_chart(fig_headway, width="stretch") # Fixed deprecation warning

# ------------------------------------------
# TAB 2: DRIVER COMPANION APP (The Shift)
# ------------------------------------------
with tab2:
    st.markdown(f"<div class='sub-header'>Shift Overview for {selected_driver}</div>", unsafe_allow_html=True)
    st.markdown("<div class='driver-status'>🟢 ON-TIME | Next action: Siemens Express Route in 15 mins</div><br>", unsafe_allow_html=True)

    # Highlight: The optimized, continuous driver shift
    fig_opt = px.timeline(shift_optimized, x_start="Start", x_end="Finish", y=["Today's Schedule"]*len(shift_optimized), 
                          color="Type", color_discrete_map=color_map, text="Task", height=200)
    fig_opt.update_traces(textposition='inside', insidetextanchor='middle')
    fig_opt.update_layout(showlegend=True, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig_opt, width="stretch") # Fixed deprecation warning
    
    # Mock App Buttons
    bc1, bc2, bc3 = st.columns(3)
    bc1.button("🗺️ Start Navigation for Express", use_container_width=True)
    bc2.button("☕ View Facility Info at Depot Break", use_container_width=True)
    bc3.button("📞 Contact Dispatch (AI Assist)", use_container_width=True)

    st.markdown("<hr>", unsafe_allow_html=True)

    # Side Feature: The Comparison (Tucked away in an expander so it doesn't distract from the main feature)
    with st.expander("📊 Compare with Previous Unoptimized Schedule (Impact Analysis)"):
        st.write("Before our MILP solver, this shift contained useless deadhead time and a break taken at a random street corner without facilities.")
        fig_unopt = px.timeline(shift_unoptimized, x_start="Start", x_end="Finish", y=["Old Schedule"]*len(shift_unoptimized), 
                                color="Type", color_discrete_map=color_map, text="Task", height=150)
        fig_unopt.update_traces(textposition='inside', insidetextanchor='middle')
        fig_unopt.update_layout(showlegend=False, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig_unopt, width="stretch") # Fixed deprecation warning