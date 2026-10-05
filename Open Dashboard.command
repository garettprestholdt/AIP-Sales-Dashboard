#!/bin/bash
# Mac: double-click to open the dashboard. (One-time: chmod +x "Open Dashboard.command")
cd "$(dirname "$0")"
python3 launch_dashboard.py || read -r -p "Press Enter to close"
