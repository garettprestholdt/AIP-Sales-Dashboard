"""
data_pipeline.py - ETL for USDA RMA Summary of Business (SOB) "YYsumdat" files.

    Extract   : find the yearly files on the SOB index, download them, store each as a zip
    Transform : slice the fixed-width records using the RMA record layout, attach names
    Load      : write data/processed/sob_fact.parquet + meta.json for the dashboard

Source facts (checked against the RMA site, Oct 2026)
* Yearly files live at SOB_BASE_URL as plain text with no extension ("26sumdat",
  older years "11SUMDAT"). Each record is 119 characters (layout below).
* There is no AIP / company field in any public SOB file; `delivery_sys` only separates
  reinsured (AIP-delivered) from federally delivered business.
* Commodity and plan names come from the companion "sobcov_YYYY.zip" files.

Storage and refresh rules
* data/raw/ holds one zip per downloaded file and nothing else. A stored zip means
  "already downloaded": older years are never re-fetched; only the newest crop year is
  checked on RMA (cheap HEAD request) and replaced when it changed.
* If nothing changed, the existing parquet is kept and the run takes about a second.
* RMA also revises prior years as late losses arrive; --check-all picks those up.

Usage
    python data_pipeline.py                 # newest crop year (partial) + 3 prior years
    python data_pipeline.py --history 5     # newest + 5 prior years
    python data_pipeline.py --year 2025 2026
    python data_pipeline.py --check-all     # also re-check prior years on RMA
    python data_pipeline.py --refresh       # re-download and rebuild everything
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------------
SOB_BASE_URL = "https://pubfs-rma.fpac.usda.gov/pub/Reports/Summary_of_Business/"
SOB_INDEX_URL = SOB_BASE_URL + "index.html"
LAYOUT_URL = SOB_BASE_URL + quote("YYsumdat Record layout.docx")
SOBCOV_BASE_URL = "https://pubfs-rma.fpac.usda.gov/pub/Web_Data_Files/Summary_of_Business/state_county_crop/"

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
LAYOUT_DOCX = DATA_DIR / "reference" / "YYsumdat Record layout.docx"
FACT_PARQUET = PROCESSED_DIR / "sob_fact.parquet"
META_JSON = PROCESSED_DIR / "meta.json"

HTTP_TIMEOUT = 120
SCHEMA_VERSION = 2  # bump when the parquet columns change, to force a rebuild
log = logging.getLogger("sob_pipeline")

# ----------------------------------------------------------------------------------------
# Record layout: (field name, first column, last column), 1-based inclusive as in the .docx
# ----------------------------------------------------------------------------------------
# Manual copy of "YYsumdat Record layout.docx" (RMA, 07/31/2024). Used when the .docx can't
# be read; when it can, the .docx wins (with a warning if it differs from this copy).
DEFAULT_LAYOUT = [
    ("crop_yr", 1, 4), ("state_cd", 5, 6), ("county_cd", 7, 9), ("crop_cd", 10, 13),
    ("ins_plan", 14, 15), ("cov_lvl", 16, 17), ("coverage_flag", 18, 18), ("delivery_sys", 19, 19),
    ("pol_sold_cnt", 20, 29), ("pol_prem_cnt", 30, 39), ("pol_indem_cnt", 40, 49),
    ("unit_prem_cnt", 50, 59), ("unit_indem_cnt", 60, 69), ("net_acre_qty", 70, 79),
    ("liability_amt", 80, 89), ("total_prem", 90, 99), ("subsidy", 100, 109), ("indem_amt", 110, 119),
]
KEY_WIDTHS = {"state_cd": 2, "county_cd": 3, "crop_cd": 4, "ins_plan": 2, "cov_lvl": 2}  # zero-pad codes
METRIC_FIELDS = [name for name, start, _ in DEFAULT_LAYOUT if start >= 20]

STATE_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "72": "PR",
}


def _layout_problems(layout) -> list[str]:
    problems = [f"gap/overlap at {b[0]}" for a, b in zip(layout, layout[1:]) if b[1] != a[2] + 1]
    if layout[0][1] != 1:
        problems.append("does not start at column 1")
    missing = {n for n, *_ in DEFAULT_LAYOUT} - {n for n, *_ in layout}
    return problems + ([f"missing fields {sorted(missing)}"] if missing else [])


def load_layout(session: requests.Session) -> tuple[list, str]:
    """Read the layout table from the RMA .docx (local copy first, else download and keep a copy).
    Falls back to DEFAULT_LAYOUT if the .docx is unavailable or doesn't validate."""
    try:
        from docx import Document
        if not LAYOUT_DOCX.exists():
            r = session.get(LAYOUT_URL, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            LAYOUT_DOCX.parent.mkdir(parents=True, exist_ok=True)
            LAYOUT_DOCX.write_bytes(r.content)
        layout = []
        for row in Document(LAYOUT_DOCX).tables[0].rows[1:]:  # columns: #, range, name, size, description
            cells = [c.text.strip() for c in row.cells]
            m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", cells[1]) if len(cells) >= 3 else None
            if m and cells[2]:
                layout.append((cells[2].lower(), int(m[1]), int(m[2])))
        layout.sort(key=lambda f: f[1])
        if problems := _layout_problems(layout):
            log.warning("Layout .docx failed checks (%s); using built-in layout.", "; ".join(problems))
            return DEFAULT_LAYOUT, "built-in (docx invalid)"
        if layout != DEFAULT_LAYOUT:
            log.warning("RMA layout .docx differs from the built-in copy; using the .docx version.")
        return layout, "RMA .docx"
    except Exception as exc:  # no python-docx, offline, malformed file...
        log.warning("Layout .docx unavailable (%s); using built-in layout.", exc)
        return DEFAULT_LAYOUT, "built-in"


# ----------------------------------------------------------------------------------------
# Extract
# ----------------------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "HEAD"))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = "sob-dashboard-etl/2.0"
    return s


def list_sumdat_files(session: requests.Session) -> dict[int, str]:
    """{crop year: url} for every YYsumdat file on the SOB index page."""
    r = session.get(SOB_INDEX_URL, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    files = {}
    for href in re.findall(r'href="([^"]+)"', r.text, flags=re.I):
        if m := re.fullmatch(r"(\d{2})sumdat", href.rsplit("/", 1)[-1], flags=re.I):
            files[2000 + int(m[1])] = urljoin(SOB_INDEX_URL, href)
    if not files:
        raise RuntimeError(f"No YYsumdat files found on {SOB_INDEX_URL}; has the page changed?")
    return files


def remote_size(session: requests.Session, url: str) -> int | None:
    try:
        r = session.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        return int(r.headers.get("Content-Length", 0)) or None
    except Exception:
        return None


def stored_size(path: Path) -> int | None:
    """Size of the original remote file, recorded in the zip comment when we stored it."""
    try:
        with zipfile.ZipFile(path) as zf:
            m = re.search(rb"remote_size=(\d+)", zf.comment)
        return int(m[1]) if m else None
    except (FileNotFoundError, zipfile.BadZipFile):
        return None


def fetch_zip(session, url: str, dest: Path, check_remote: bool, refresh: bool = False) -> bool:
    """Make sure `dest` (a zip) holds the file at `url`. Returns True if it was (re)downloaded.
    A stored zip is reused as-is unless check_remote is set and RMA's file size has changed."""
    if dest.exists() and not refresh:
        if not check_remote:
            return False
        remote = remote_size(session, url)
        if remote is None or remote == stored_size(dest):
            return False
    log.info("Downloading %s", url.rsplit("/", 1)[-1])
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    if r.content[:4] == b"PK\x03\x04":  # already a zip
        tmp.write_bytes(r.content)
        with zipfile.ZipFile(tmp, "a") as zf:
            zf.comment = f"remote_size={len(r.content)}".encode()
    else:                               # plain text: compress it
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(url.rsplit("/", 1)[-1], r.content)
            zf.comment = f"remote_size={len(r.content)}".encode()
    tmp.replace(dest)
    return True


def read_zip_text(path: Path) -> list[str]:
    """Contents of every data file in a zip (documentation files skipped), decoded."""
    with zipfile.ZipFile(path) as zf:
        return [zf.read(m).decode("latin-1") for m in zf.namelist()
                if not m.endswith("/") and not m.lower().endswith((".doc", ".docx", ".pdf"))]


def cleanup_legacy_files() -> None:
    """Remove extracted folders / unzipped copies left by earlier versions of this script."""
    for p in list(RAW_DIR.rglob("*_extracted")) + list(RAW_DIR.glob("[0-9][0-9][sS][uU][mM][dD][aA][tT]")):
        shutil.rmtree(p) if p.is_dir() else p.unlink()


# ----------------------------------------------------------------------------------------
# Transform
# ----------------------------------------------------------------------------------------
def parse_sumdat(path: Path, layout) -> pd.DataFrame:
    """Slice fixed-width records straight from the stored zip."""
    record_len = layout[-1][2]
    frames = []
    for text in read_zip_text(path):
        lines = pd.Series(text.splitlines())
        lines = lines[lines.str.strip() != ""]
        if (n_bad := int((lines.str.len() != record_len).sum())):
            log.warning("%s: %d record(s) are not %d characters long.", path.name, n_bad, record_len)
        frames.append(pd.DataFrame({name: lines.str.slice(start - 1, end).str.strip()
                                    for name, start, end in layout}))
    df = pd.concat(frames, ignore_index=True)

    for col in METRIC_FIELDS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if (n_bad := int(df[METRIC_FIELDS].isna().any(axis=1).sum())):
        log.warning("%s: %d row(s) had unreadable numbers (counted as 0).", path.name, n_bad)
    df[METRIC_FIELDS] = df[METRIC_FIELDS].fillna(0)
    df["crop_yr"] = pd.to_numeric(df["crop_yr"], errors="coerce")
    df = df.dropna(subset=["crop_yr"]).astype({"crop_yr": int})
    for col, width in KEY_WIDTHS.items():
        df[col] = df[col].str.zfill(width)
    return df


def name_lookups(session, years: list[int], check_years: set[int], refresh: bool) -> tuple[dict, bool]:
    """Commodity and plan names from RMA's pipe-delimited sobcov files (columns 5-8:
    commodity code, commodity name, plan code, plan abbreviation). Non-fatal if unavailable."""
    frames, changed = [], False
    for yr in years:
        path = RAW_DIR / "lookups" / f"sobcov_{yr}.zip"
        try:
            changed |= fetch_zip(session, f"{SOBCOV_BASE_URL}sobcov_{yr}.zip", path, yr in check_years, refresh)
            frames += [pd.read_csv(io.StringIO(t), sep="|", header=None, usecols=[5, 6, 7, 8], dtype=str,
                                   keep_default_na=False) for t in read_zip_text(path)]
        except Exception as exc:
            log.warning("Names for %d unavailable (%s); codes will be shown.", yr, exc)
    if not frames:
        return {}, changed
    raw = pd.concat(frames, ignore_index=True).apply(lambda c: c.str.strip())
    return {
        "commodity": dict(zip(raw[5].str.zfill(4), raw[6])),  # later years win
        "plan": dict(zip(raw[7].str.zfill(2), raw[8])),
    }, changed


def enrich(df: pd.DataFrame, names: dict) -> pd.DataFrame:
    """Readable labels (with code fallbacks) and derived metrics used by the dashboard."""
    commodity = df["crop_cd"].map(names.get("commodity", {})).fillna("Unknown")
    plan = df["ins_plan"].map(names.get("plan", {})).fillna("Unknown")
    return df.assign(
        commodity=commodity.str.title() + " (" + df["crop_cd"] + ")",
        insurance_plan=plan + " (" + df["ins_plan"] + ")",
        state_abbr=df["state_cd"].map(STATE_FIPS).fillna(df["state_cd"]),
        cov_lvl_pct=pd.to_numeric(df["cov_lvl"], errors="coerce"),
        farmer_paid_prem=df["total_prem"] - df["subsidy"],
    )


# ----------------------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------------------
def run_pipeline(years: list[int] | None = None, history: int = 3,
                 check_all: bool = False, refresh: bool = False) -> dict:
    """Bring data/processed up to date with RMA. Returns the metadata dict."""
    session = make_session()
    cleanup_legacy_files()
    available = list_sumdat_files(session)
    newest = max(available)
    years = sorted(years or [y for y in range(newest - history, newest + 1) if y in available])
    if unknown := sorted(set(years) - set(available)):
        raise ValueError(f"Crop year(s) {unknown} not on RMA (available {min(available)}-{newest}).")

    check = set(years) if check_all else {newest}
    zips = {y: RAW_DIR / f"{y % 100:02d}sumdat.zip" for y in years}
    changed = any([fetch_zip(session, available[y], zips[y], y in check, refresh) for y in years])
    names, names_changed = name_lookups(session, years, check, refresh)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    old = json.loads(META_JSON.read_text()) if META_JSON.exists() else {}
    if not (changed or names_changed or refresh) and FACT_PARQUET.exists() \
            and old.get("crop_years") == years and old.get("schema") == SCHEMA_VERSION:
        log.info("RMA data unchanged; keeping existing dashboard data.")
        old["checked_at"] = now
        META_JSON.write_text(json.dumps(old, indent=2))
        return old

    layout, layout_source = load_layout(session)
    fact = enrich(pd.concat([parse_sumdat(zips[y], layout) for y in years], ignore_index=True), names)
    # The newest year is "partial" (early season) when its file is under half the size of the year before.
    prev_size = stored_size(zips[newest - 1]) if newest - 1 in zips else None
    new_size = stored_size(zips[newest]) if newest in zips else None
    meta = {
        "schema": SCHEMA_VERSION, "built_at": now, "checked_at": now, "crop_years": years, "rows": len(fact),
        "partial_years": [newest] if prev_size and new_size and new_size < 0.5 * prev_size else [],
        "layout_source": layout_source, "names_source": "RMA sobcov" if names else "codes only",
    }
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    fact.to_parquet(FACT_PARQUET, index=False)
    META_JSON.write_text(json.dumps(meta, indent=2))
    log.info("Built %s rows for crop years %s.", f"{len(fact):,}", years)
    return meta


def load_processed() -> tuple[pd.DataFrame, dict]:
    """Entry point for the dashboard: (fact table, metadata)."""
    if not META_JSON.exists():
        raise FileNotFoundError("No processed data yet. Run `python data_pipeline.py` first.")
    return pd.read_parquet(FACT_PARQUET), json.loads(META_JSON.read_text())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Download and parse RMA Summary of Business (YYsumdat) files.")
    p.add_argument("--year", type=int, nargs="+", help="crop year(s) to load (default: newest + --history prior)")
    p.add_argument("--history", type=int, default=3, help="prior years to include with the newest (default 3)")
    p.add_argument("--check-all", action="store_true", help="re-check every year on RMA, not just the newest")
    p.add_argument("--refresh", action="store_true", help="re-download and rebuild everything")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        run_pipeline(a.year, a.history, a.check_all, a.refresh)
    except Exception as exc:
        log.error("Pipeline failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
