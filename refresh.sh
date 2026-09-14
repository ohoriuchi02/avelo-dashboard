#!/bin/bash
# refresh.sh
# ----------
# Run this any time you want to (re)start the dashboard. It rebuilds the
# data (build_data.py) first, so data-file changes and pipeline edits
# (like the OD40 scaling) are always picked up -- then launches the app
# with auto-reload on save, so further code/text edits (legend colors,
# etc.) update the browser tab on their own without re-running this
# script again.
#
# Usage:
#   chmod +x refresh.sh      (one-time, makes it runnable)
#   ./refresh.sh

cd "$(dirname "$0")"

# Activate the virtual environment, if you're using one
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Rebuild the pickles from data/Airports.csv, Runways.csv, markets.xlsx.
# If the raw source files aren't there (e.g. you're only using pickles you
# already built), this fails gracefully and the app falls back to whatever
# pickles already exist in data/.
echo "Rebuilding data..."
if python build_data.py; then
    echo "Data rebuilt."
else
    echo "build_data.py did not complete (raw source files missing in data/?)."
    echo "Continuing with whatever pickles are already in data/, if any."
fi

# Stop any dashboard already running on Streamlit's default port, so you
# don't end up with duplicate tabs/processes
PORT=8501
PID=$(lsof -ti:$PORT)
if [ -n "$PID" ]; then
    echo "Stopping existing dashboard on port $PORT..."
    kill -9 $PID
fi

# Launch with auto-rerun-on-save enabled
echo "Starting dashboard at http://localhost:$PORT (auto-refreshes on save)..."
streamlit run app.py --server.runOnSave true
