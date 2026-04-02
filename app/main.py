"""
BV Benchmark Business Case — Streamlit App

Run with:
    streamlit run app/main.py
"""

import sys
from pathlib import Path

# Ensure the project root is on sys.path regardless of how Streamlit is invoked.
# Streamlit adds the script's directory (app/) to sys.path, not the project root,
# which breaks absolute imports like 'from app.pages import ...'.
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

st.set_page_config(
    page_title="BV Benchmark Business Case",
    page_icon="📊",
    layout="wide",
)

# ---- Sidebar navigation ----
st.sidebar.title("Navigation")
page = st.sidebar.radio(
    "Go to",
    ["⚡ Agent Intake", "1 · Client Intake", "2 · Consumption Plan", "3 · Benchmarks", "4 · Results", "5 · Export"],
)

if page == "⚡ Agent Intake":
    from app.pages import agent_intake
    agent_intake.render()
elif page == "1 · Client Intake":
    from app.pages import intake
    intake.render()
elif page == "2 · Consumption Plan":
    from app.pages import consumption
    consumption.render()
elif page == "3 · Benchmarks":
    from app.pages import benchmarks
    benchmarks.render()
elif page == "4 · Results":
    from app.pages import results
    results.render()
elif page == "5 · Export":
    from app.pages import export_page
    export_page.render()
