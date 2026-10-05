# Crop Insurance Sales Dashboard

Sales dashboard built on USDA RMA Summary of Business (SOB) data: premium, indemnity, loss ratio,
liability, policies and acres, filterable by crop year, commodity, insurance plan and state.

## Opening the dashboard (no technical steps)

1. **One-time:** install Python 3.10 or newer from <https://www.python.org/downloads/>
   (on Windows, tick **"Add Python to PATH"** during install).
2. **Double-click `Open Dashboard.bat`** (Windows) or **`Open Dashboard.command`** (Mac).
   - The first launch takes a few minutes while it sets itself up. Later launches take seconds.
   - Your browser opens the dashboard. It checks RMA for new data as it opens, so you always see
     the latest numbers. The **Check RMA now** button checks again at any time.
   - Double-clicking again while it's running just reopens the browser tab.
3. Keep the small black window open while you use the dashboard. Close it when you're done.

Tip: right-click `Open Dashboard.bat` → *Send to* → *Desktop (create shortcut)* for a desktop icon.
On a Mac, run `chmod +x "Open Dashboard.command"` once in Terminal so it can be double-clicked.

## Project files

| File | Purpose |
|---|---|
| `Open Dashboard.bat` / `.command` | One-click start (Windows / Mac) |
| `launch_dashboard.py` | Sets up a private Python environment (`.venv`) on first run, then starts the dashboard and opens the browser |
| `data_pipeline.py` | Downloads RMA files, parses them, writes `data/processed/sob_fact.parquet` |
| `dashboard.py` | The dashboard (a Streamlit app) |
| `.streamlit/config.toml` | Dashboard settings: light theme, no usage statistics, no first-run prompt |
| `requirements.txt` | Python packages |
| `data/raw/` | One zip per downloaded RMA file (nothing else is kept) |
| `data/processed/` | `sob_fact.parquet` + `meta.json` read by the dashboard |
| `data/reference/` | RMA record layout `.docx` (downloaded automatically) |

## How data stays current

* The dashboard runs `data_pipeline.run_pipeline()` when it starts, again every 60 minutes while open
  (`RECHECK_MINUTES`), and whenever **Check RMA now** is clicked.
* Default years: the newest crop year on RMA (usually partial, early season) plus the 3 before it.
* Older years are read from their stored zips and not re-downloaded. Only the newest year is checked
  on RMA (one quick request) and replaced if it changed. If nothing changed, the existing data is
  reused and the check takes about a second.
* **RMA revises prior years** as late losses come in (indemnities especially). To pick those up,
  set `CHECK_PRIOR_YEARS = True` at the top of `dashboard.py` for one run, or run
  `python data_pipeline.py --check-all`.
* If RMA can't be reached, the dashboard shows the last data it has and says so at the top.

## Developer notes

* Run the pipeline directly: `python data_pipeline.py [--year 2025 2026] [--history N] [--check-all] [--refresh]`.
* Run the dashboard directly: `streamlit run dashboard.py` (inside `.venv`). Settings are at the top of the file.
* Record layout: read from the RMA `.docx` (local copy in `data/reference/`, downloaded if missing),
  checked for gaps/overlaps, with a built-in copy as fallback. Delete the local copy to pick up a new layout.
* Names: commodity and plan names come from RMA's `sobcov_YYYY.zip` files; codes are shown if unavailable.
* Insurance plan descriptions shown on the dashboard live in `PLAN_DESCRIPTIONS` in `dashboard.py`.
* Colors are fixed per measure (green premium, red indemnity, blue liability, violet policies,
  orange acres) and defined once in the design-tokens section of `dashboard.py`.
* Dollar figures are whole dollars in the source and shown rounded to 1 decimal ($M in charts and table).
  Ratios are computed after aggregation.
* **AIP:** no public RMA file breaks sales out by AIP/company; `delivery_sys` only separates reinsured
  from federally delivered business. An AIP view needs a non-public data source.
