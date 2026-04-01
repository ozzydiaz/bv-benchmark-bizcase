"""
BV Benchmark Business Case — Streamlit App

Run with:
    streamlit run app/main.py
"""

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
    ["1 · Client Intake", "2 · Consumption Plan", "3 · Benchmarks", "4 · Results", "5 · Export"],
)

if page == "1 · Client Intake":
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
