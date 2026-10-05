"""
launch_dashboard.py - one-click start for the Crop Insurance Sales Dashboard.

Started by double-clicking "Open Dashboard.bat" (Windows) or "Open Dashboard.command" (Mac).
1. First run only: creates a private Python environment in .venv and installs requirements.txt
   (a few minutes). Re-installs automatically whenever requirements.txt changes.
2. Starts the dashboard (Streamlit) and opens it in the browser once it's ready. If the
   dashboard is already running, it just opens the browser to it.
Keep the console window open while using the dashboard; closing it stops the dashboard.
"""
import hashlib
import os
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
STAMP = VENV / "requirements.sha256"
REQUIREMENTS = HERE / "requirements.txt"
PORT = 8501
URL = f"http://localhost:{PORT}"
# Light theme passed on the command line so it applies even if .streamlit/config.toml is missing
# or misplaced (otherwise Streamlit follows the computer's dark mode and dark text disappears).
LIGHT_THEME = ["--theme.base", "light", "--theme.primaryColor", "#52514e",
               "--theme.backgroundColor", "#fcfcfb", "--theme.secondaryBackgroundColor", "#f3f2ee",
               "--theme.textColor", "#0b0b0b"]


def ensure_environment() -> None:
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if not VENV_PY.exists():
        print("First-time setup: preparing the dashboard (one time only, a few minutes)...")
        venv.create(VENV, with_pip=True)
    if not STAMP.exists() or STAMP.read_text() != digest:
        print("Installing dashboard components...")
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)])
        STAMP.write_text(digest)


def dashboard_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/_stcore/health", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def main() -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/")
        return 1
    if dashboard_ready():
        print("The dashboard is already running - opening it in your browser.")
        webbrowser.open(URL)
        return 0
    try:
        ensure_environment()
    except subprocess.CalledProcessError:
        print("Setup failed while installing packages. Check the internet connection and try again.")
        return 1

    print("Starting the dashboard...")
    server = subprocess.Popen([str(VENV_PY), "-m", "streamlit", "run", "dashboard.py",
                               "--server.port", str(PORT), "--server.headless", "true",
                               "--browser.gatherUsageStats", "false", *LIGHT_THEME], cwd=HERE)
    for _ in range(120):                      # wait up to ~2 minutes for the server to come up
        if dashboard_ready():
            webbrowser.open(URL)
            print(f"\nDashboard open at {URL}")
            print("Keep this window open while you use it. Close the window to stop the dashboard.")
            break
        if server.poll() is not None:         # server exited during startup
            print("The dashboard failed to start; see the messages above.")
            return 1
        time.sleep(1)
    try:
        return server.wait()
    except KeyboardInterrupt:
        server.terminate()
        return 0


if __name__ == "__main__":
    sys.exit(main())
