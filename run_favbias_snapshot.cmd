@echo off
cd /d "c:\Users\ojoca\Documents\polybot"
".venv\Scripts\python.exe" -m research.favorite_bias_collector snapshot >> "data\edge_latency\favorite_bias\collector.log" 2>&1
