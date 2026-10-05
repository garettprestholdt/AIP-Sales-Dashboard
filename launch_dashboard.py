"""
launch_dashboard.py - one-click start for the Crop Insurance Sales Dashboard.

Started by double-clicking "Open Dashboard.bat" (Windows) or "Open Dashboard.command" (Mac).
1. First run only: creates a private Python environment in .venv and installs the packages
   in requirements.txt (a few minutes). Re-installs automatically if requirements.txt changes.
2. Starts Voila, which runs dashboard.ipynb with the code hidden and opens it in the browser.
   The dashboard checks RMA for new data as it opens, so every launch (or browser refresh)
   shows the latest numbers.
Keep the black console window open while using the dashboard; closing it stops the dashboard.
"""
import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
STAMP = VENV / "requirements.sha256"
REQUIREMENTS = HERE / "requirements.txt"


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


def main() -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/")
        return 1
    try:
        ensure_environment()
    except subprocess.CalledProcessError:
        print("Setup failed while installing packages. Check the internet connection and try again.")
        return 1
    print("Opening the dashboard in your browser (it checks RMA for new data first)...")
    print("Keep this window open while you use the dashboard. Close it to stop.")
    return subprocess.call([str(VENV_PY), "-m", "voila", "dashboard.ipynb",
                            "--theme=light", "--show_tracebacks=False"], cwd=HERE)


if __name__ == "__main__":
    sys.exit(main())
