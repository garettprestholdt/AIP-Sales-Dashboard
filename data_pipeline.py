"""
data_pipeline.py
================
ETL for the USDA Risk Management Agency (RMA) Summary of Business (SOB) "YYsumdat" files.

    Extract  : discover files on the SOB index page, download (cached), unzip when needed
    Transform: parse the fixed-width records using the official record layout (.docx),
               clean keys, add derived metrics, attach human-readable names
    Load     : write a tidy fact table (parquet, CSV fallback) + metadata JSON that the
               dashboard notebook reads via `load_processed()`

Source facts (verified against the RMA directory, Oct 2026)
----------------------------------------------------------
* Index:   https://pubfs-rma.fpac.usda.gov/pub/Reports/Summary_of_Business/index.html
* Per-year files are UNCOMPRESSED and have NO extension: "11SUMDAT" ... "14SUMDAT",
  "15sumdat" ... "27sumdat" (case varies by year).  Only "allsumdat.zip" (every year)
  is a zip archive.  This script handles both, plus any per-year file that turns out
  to be zipped (detected by magic bytes, not by name).
* Records are fixed-width, 119 characters (layout below).  Older files use CRLF line
  endings, newer files LF; pandas handles both.
* The files do NOT contain an AIP / insurance company field.  The closest public
  dimension is `delivery_sys` (reinsured = AIP-delivered vs. federally delivered).
  See `--aip-file` for plugging in your own AIP-level extract.
* Names (commodity, plan, county) are not in sumdat.  They are looked up from the
  companion pipe-delimited "sobcov_YYYY.zip" files in
  pub/Web_Data_Files/Summary_of_Business/state_county_crop/.

Usage
-----
    python data_pipeline.py                      # latest complete crop year
    python data_pipeline.py --year 2026          # a specific crop year
    python data_pipeline.py --year 2024 2025 2026
    python data_pipeline.py --all                # every year, via allsumdat.zip
    python data_pipeline.py --year 2026 --aip-file my_aip_extract.csv
    python data_pipeline.py --refresh            # ignore the download cache

Requirements: pandas, requests, pyarrow (optional, for parquet), python-docx (optional,
for reading the layout .docx programmatically; a hard-coded copy is used otherwise).
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, quote

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
SOB_BASE_URL = "https://pubfs-rma.fpac.usda.gov/pub/Reports/Summary_of_Business/"
SOB_INDEX_URL = SOB_BASE_URL + "index.html"
LAYOUT_URL = SOB_BASE_URL + quote("YYsumdat Record layout.docx")
ALL_YEARS_ZIP = "allsumdat.zip"
SOBCOV_BASE_URL = (
    "https://pubfs-rma.fpac.usda.gov/pub/Web_Data_Files/Summary_of_Business/state_county_crop/"
)

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
FACT_PARQUET = PROCESSED_DIR / "sob_fact.parquet"
FACT_CSV = PROCESSED_DIR / "sob_fact.csv.gz"
META_JSON = PROCESSED_DIR / "meta.json"

HTTP_TIMEOUT = 120  # seconds
USER_AGENT = "sob-dashboard-etl/1.0 (python-requests)"

log = logging.getLogger("sob_pipeline")


# --------------------------------------------------------------------------------------
# Record layout
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    name: str
    start: int        # 1-based, inclusive (as written in the .docx "Data Range")
    end: int          # 1-based, inclusive
    fmt: str          # COBOL-style picture: 9999, 99, X, 9(10)
    description: str

    @property
    def width(self) -> int:
        return self.end - self.start + 1

    @property
    def colspec(self) -> tuple[int, int]:
        # pandas.read_fwf wants 0-based, half-open intervals
        return (self.start - 1, self.end)


# MANUAL COPY of "YYsumdat Record layout.docx" (RMA, 07/31/2024).
# Used as the fallback when the .docx cannot be downloaded/parsed, and as a sanity check
# against the programmatic parse.  If RMA ever changes the layout, the docx parse wins
# (with a logged warning) as long as it passes validation.
DEFAULT_LAYOUT: list[Field] = [
    Field("crop_yr",        1,   4,   "9999",  "Crop Year"),
    Field("state_cd",       5,   6,   "99",    "State Code"),
    Field("county_cd",      7,   9,   "999",   "County Code"),
    Field("crop_cd",        10,  13,  "9999",  "Crop Code"),
    Field("ins_plan",       14,  15,  "99",    "Insurance Plan"),
    Field("cov_lvl",        16,  17,  "99",    "Coverage Level"),
    Field("coverage_flag",  18,  18,  "X",     "Coverage Flag"),
    Field("delivery_sys",   19,  19,  "X",     "Delivery System"),
    Field("pol_sold_cnt",   20,  29,  "9(10)", "Policies Sold"),
    Field("pol_prem_cnt",   30,  39,  "9(10)", "Policies Earning Premium"),
    Field("pol_indem_cnt",  40,  49,  "9(10)", "Policies Indemnified"),
    Field("unit_prem_cnt",  50,  59,  "9(10)", "Units Earning Premium"),
    Field("unit_indem_cnt", 60,  69,  "9(10)", "Units Indemnified"),
    Field("net_acre_qty",   70,  79,  "9(10)", "Net Acres"),
    Field("liability_amt",  80,  89,  "9(10)", "Liability"),
    Field("total_prem",     90,  99,  "9(10)", "Total Premium"),
    Field("subsidy",        100, 109, "9(10)", "Subsidy"),
    Field("indem_amt",      110, 119, "9(10)", "Indemnity"),
]

# The first 8 fields (data column #1 in the docx) are identifiers: keep them as zero-padded
# strings so codes like state "01" or crop "0041" don't lose their leading zeros.
KEY_FIELDS = ["crop_yr", "state_cd", "county_cd", "crop_cd", "ins_plan",
              "cov_lvl", "coverage_flag", "delivery_sys"]
METRIC_FIELDS = ["pol_sold_cnt", "pol_prem_cnt", "pol_indem_cnt", "unit_prem_cnt",
                 "unit_indem_cnt", "net_acre_qty", "liability_amt", "total_prem",
                 "subsidy", "indem_amt"]
KEY_WIDTHS = {"state_cd": 2, "county_cd": 3, "crop_cd": 4, "ins_plan": 2, "cov_lvl": 2}


def validate_layout(layout: list[Field]) -> list[str]:
    """Return a list of problems (empty == valid): contiguous, non-overlapping, all fields present."""
    problems = []
    ordered = sorted(layout, key=lambda f: f.start)
    if ordered[0].start != 1:
        problems.append(f"layout starts at column {ordered[0].start}, expected 1")
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start != prev.end + 1:
            problems.append(f"gap/overlap between {prev.name} (ends {prev.end}) and {cur.name} (starts {cur.start})")
    missing = set(KEY_FIELDS + METRIC_FIELDS) - {f.name for f in layout}
    if missing:
        problems.append(f"missing expected fields: {sorted(missing)}")
    return problems


def parse_layout_docx(docx_bytes: bytes) -> list[Field]:
    """
    PROGRAMMATIC layout parse.  Reads the first table of the RMA .docx with python-docx.
    Expected columns: Data Column # | Data Range | Field Name | Field Size | Data Description.
    The header row in the docx contains stray formatting ("**Data ****Description**"), so we
    locate columns by position, not by header text.
    """
    from docx import Document  # optional dependency: pip install python-docx

    doc = Document(io.BytesIO(docx_bytes))
    if not doc.tables:
        raise ValueError("no tables found in layout .docx")
    fields = []
    for row in doc.tables[0].rows[1:]:  # skip header row
        cells = [c.text.strip() for c in row.cells]
        if len(cells) < 5:
            continue
        m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", cells[1])
        if not m or not cells[2]:
            continue
        fields.append(Field(cells[2].lower(), int(m.group(1)), int(m.group(2)), cells[3], cells[4]))
    if not fields:
        raise ValueError("could not read any field rows from the layout table")
    return fields


def resolve_layout(session: requests.Session, use_docx: bool = True) -> tuple[list[Field], str]:
    """
    Try the live .docx first (so layout changes are picked up automatically), fall back to
    DEFAULT_LAYOUT.  MANUAL OVERRIDE: run with --no-docx, or edit DEFAULT_LAYOUT above.
    """
    if not use_docx:
        return DEFAULT_LAYOUT, "hard-coded (docx skipped)"
    try:
        resp = session.get(LAYOUT_URL, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        layout = parse_layout_docx(resp.content)
        problems = validate_layout(layout)
        if problems:
            log.warning("Layout from .docx failed validation (%s); using hard-coded layout.", "; ".join(problems))
            return DEFAULT_LAYOUT, "hard-coded (docx invalid)"
        if [(f.name, f.start, f.end) for f in layout] != [(f.name, f.start, f.end) for f in DEFAULT_LAYOUT]:
            log.warning("RMA layout .docx DIFFERS from the hard-coded copy - using the .docx version. "
                        "Review DEFAULT_LAYOUT in data_pipeline.py.")
        return layout, "parsed from RMA .docx"
    except ImportError:
        log.info("python-docx not installed; using hard-coded layout (pip install python-docx to parse the .docx).")
    except Exception as exc:  # network error, malformed docx, ...
        log.warning("Could not read layout .docx (%s); using hard-coded layout.", exc)
    return DEFAULT_LAYOUT, "hard-coded (docx unavailable)"


# --------------------------------------------------------------------------------------
# Extract: discovery + download
# --------------------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "HEAD"))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = USER_AGENT
    return s


def list_available_files(session: requests.Session) -> dict[int, str]:
    """Scrape the SOB index for per-year files. Returns {crop_year: absolute_url}."""
    resp = session.get(SOB_INDEX_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    files: dict[int, str] = {}
    # hrefs look like ".../27sumdat" or "11SUMDAT" - match the 2-digit year prefix, any case
    for href in re.findall(r'href="([^"]+)"', resp.text, flags=re.I):
        name = href.rstrip("/").rsplit("/", 1)[-1]
        m = re.fullmatch(r"(\d{2})sumdat(\.zip|\.txt)?", name, flags=re.I)
        if m:
            files[2000 + int(m.group(1))] = urljoin(SOB_INDEX_URL, href)
    if not files:
        raise RuntimeError(f"No YYsumdat files found on {SOB_INDEX_URL} - has the page layout changed?")
    log.info("Found %d yearly files on the SOB index: %d-%d", len(files), min(files), max(files))
    return dict(sorted(files.items()))


def remote_size(session: requests.Session, url: str) -> int | None:
    try:
        r = session.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        return int(r.headers.get("Content-Length", 0)) or None
    except Exception:
        return None


def pick_latest_complete_year(session: requests.Session, files: dict[int, str]) -> int:
    """
    The newest file is usually an *early-season* crop year (e.g. in Oct 2026 the 2027 file holds
    fall-planted crops only, ~30% of a normal year).  If the newest file is under half the size of
    the year before it, treat it as partial and default to the prior year.  Override with --year.
    """
    years = sorted(files)
    latest = years[-1]
    if len(years) < 2:
        return latest
    s_latest, s_prev = remote_size(session, files[latest]), remote_size(session, files[years[-2]])
    if s_latest and s_prev and s_latest < 0.5 * s_prev:
        log.info("%d file looks early-season (%.0f%% of %d); defaulting to %d. Use --year %d to force it.",
                 latest, 100 * s_latest / s_prev, years[-2], years[-2], latest)
        return years[-2]
    return latest


def download(session: requests.Session, url: str, dest: Path, refresh: bool = False) -> Path:
    """Stream to disk with a size-based cache check; writes to *.part then renames (atomic)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not refresh:
        size = remote_size(session, url)
        if size is None or size == dest.stat().st_size:
            log.info("Cached: %s", dest.name)
            return dest
        log.info("Remote size changed for %s; re-downloading.", dest.name)
    log.info("Downloading %s", url)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with session.get(url, stream=True, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(dest)
    return dest


def extract_text_files(path: Path) -> list[Path]:
    """
    Return the text file(s) to parse.  Zips (detected by magic bytes, so a misnamed file still
    works) are extracted next to the archive; plain files are returned as-is.
    """
    with open(path, "rb") as fh:
        is_zip = fh.read(4) == b"PK\x03\x04"
    if not is_zip:
        return [path]
    out_dir = path.parent / (path.stem + "_extracted")
    out_dir.mkdir(exist_ok=True)
    extracted = []
    with zipfile.ZipFile(path) as zf:
        for member in zf.infolist():
            name = Path(member.filename).name
            if member.is_dir() or name.lower().endswith((".docx", ".doc", ".pdf")):
                continue
            target = out_dir / name
            if not target.exists() or target.stat().st_size != member.file_size:
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
            extracted.append(target)
    log.info("Extracted %d file(s) from %s", len(extracted), path.name)
    return extracted


# --------------------------------------------------------------------------------------
# Transform
# --------------------------------------------------------------------------------------
def _to_number(s: pd.Series) -> pd.Series:
    """Fields are 9(10) per layout, but be tolerant of blanks, embedded signs and decimals."""
    cleaned = s.astype(str).str.strip()
    neg = cleaned.str.contains("-", regex=False)
    cleaned = cleaned.str.replace(r"[^0-9.]", "", regex=True)
    out = pd.to_numeric(cleaned.replace("", "0"), errors="coerce")
    return out.where(~neg, -out)


def parse_sumdat(path: Path, layout: list[Field]) -> pd.DataFrame:
    """Parse one YYsumdat file. Auto-detects pipe-delimited files as a safety net."""
    record_len = max(f.end for f in layout)
    with open(path, "rb") as fh:
        first = fh.readline().decode("latin-1").rstrip("\r\n")
    names = [f.name for f in layout]

    if "|" in first:  # not expected for sumdat, but RMA's sobcov files are pipe-delimited
        log.warning("%s looks pipe-delimited; parsing as delimited with layout column order.", path.name)
        df = pd.read_csv(path, sep="|", header=None, names=names, dtype=str,
                         encoding="latin-1", keep_default_na=False, usecols=range(len(names)))
    else:
        if len(first) != record_len:
            log.warning("%s: first record is %d chars, layout expects %d - check the layout.",
                        path.name, len(first), record_len)
        df = pd.read_fwf(path, colspecs=[f.colspec for f in layout], names=names, header=None,
                         dtype=str, encoding="latin-1", keep_default_na=False)

    for col in METRIC_FIELDS:
        df[col] = _to_number(df[col])
    bad = df[METRIC_FIELDS].isna().any(axis=1)
    if bad.any():
        log.warning("%s: %d row(s) had unparseable numbers (set to 0).", path.name, int(bad.sum()))
        df[METRIC_FIELDS] = df[METRIC_FIELDS].fillna(0)

    df["crop_yr"] = pd.to_numeric(df["crop_yr"], errors="coerce").astype("Int64")
    for col, width in KEY_WIDTHS.items():
        df[col] = df[col].astype(str).str.strip().str.zfill(width)
    for col in ("coverage_flag", "delivery_sys"):
        df[col] = df[col].astype(str).str.strip().str.upper()
    df = df.dropna(subset=["crop_yr"])
    log.info("Parsed %s: %s rows, crop years %s", path.name, f"{len(df):,}",
             sorted(df["crop_yr"].unique().tolist()))
    return df


# Verify these against your data: the sumdat layout doesn't publish the code lists. The values
# below follow the sobcov documentation (A/C/E/L coverage categories; Reinsured vs Federal delivery).
COVERAGE_FLAG_NAMES = {"A": "Buy-up", "C": "CAT", "E": "Existing Coverage Policy", "L": "Limited Coverage"}
DELIVERY_NAMES = {"R": "Reinsured (AIP-delivered)", "F": "Federal (RMA/FSA-delivered)"}

STATE_FIPS = {  # stable FIPS -> USPS abbreviation (used for maps)
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "72": "PR",
}


def fetch_name_lookups(session: requests.Session, years: list[int], refresh: bool = False) -> dict[str, pd.DataFrame]:
    """
    Build code->name lookups (commodity, insurance plan, county) from RMA's pipe-delimited
    sobcov_YYYY.zip files.  Column positions per "SOB_State_County_Crop_with_Coverage_Level_1989_Forward.pdf":
    0 year | 1 state cd | 2 state abbr | 3 county cd | 4 county name | 5 commodity cd |
    6 commodity name | 7 plan cd | 8 plan abbreviation | ...
    Failure here is non-fatal: the dashboard falls back to raw codes.
    """
    frames = []
    for yr in sorted(set(years)):
        url = f"{SOBCOV_BASE_URL}sobcov_{yr}.zip"
        try:
            path = download(session, url, RAW_DIR / "lookups" / f"sobcov_{yr}.zip", refresh)
            for txt in extract_text_files(path):
                frames.append(pd.read_csv(txt, sep="|", header=None, usecols=range(9), dtype=str,
                                          encoding="latin-1", keep_default_na=False))
        except Exception as exc:
            log.warning("Name lookup for %d unavailable (%s); codes will be shown instead.", yr, exc)
    if not frames:
        return {}
    raw = pd.concat(frames, ignore_index=True).apply(lambda c: c.str.strip())
    raw[1] = raw[1].str.zfill(2)
    raw[3] = raw[3].str.zfill(3)
    raw[5] = raw[5].str.zfill(4)
    raw[7] = raw[7].str.zfill(2)
    # later years listed last -> keep="last" keeps the most recent spelling of each name
    return {
        "commodity": raw[[5, 6]].drop_duplicates(5, keep="last").set_axis(["crop_cd", "commodity_name"], axis=1),
        "plan": raw[[7, 8]].drop_duplicates(7, keep="last").set_axis(["ins_plan", "plan_abbr"], axis=1),
        "county": raw[[1, 3, 4]].drop_duplicates([1, 3], keep="last").set_axis(["state_cd", "county_cd", "county_name"], axis=1),
    }


def enrich(df: pd.DataFrame, lookups: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Attach names + derived metrics. Every label column has a code fallback, so nothing is blank."""
    out = df.copy()
    if lookups:
        out = (out.merge(lookups["commodity"], on="crop_cd", how="left")
                  .merge(lookups["plan"], on="ins_plan", how="left")
                  .merge(lookups["county"], on=["state_cd", "county_cd"], how="left"))
    for col in ("commodity_name", "plan_abbr", "county_name"):
        if col not in out:
            out[col] = pd.NA

    out["state_abbr"] = out["state_cd"].map(STATE_FIPS).fillna(out["state_cd"])
    out["commodity"] = (out["commodity_name"].fillna("Crop " + out["crop_cd"]).str.title()
                        + " (" + out["crop_cd"] + ")")
    out["insurance_plan"] = out["plan_abbr"].fillna("Plan") + " (" + out["ins_plan"] + ")"
    out["product"] = out["commodity"].str.replace(r" \(\d+\)$", "", regex=True) + " - " + out["insurance_plan"]
    out["coverage_type"] = out["coverage_flag"].map(COVERAGE_FLAG_NAMES).fillna(out["coverage_flag"])
    out["delivery_channel"] = out["delivery_sys"].map(DELIVERY_NAMES).fillna("Delivery code " + out["delivery_sys"])
    out["cov_lvl_pct"] = pd.to_numeric(out["cov_lvl"], errors="coerce")
    if "aip" not in out:  # public SOB: no company field -> delivery channel is the AIP dimension
        out["aip"] = out["delivery_channel"]

    out["farmer_paid_prem"] = out["total_prem"] - out["subsidy"]
    return out.drop(columns=["commodity_name", "plan_abbr"])


def load_aip_extract(path: Path) -> pd.DataFrame:
    """
    OPTIONAL: your own AIP-level extract (e.g. internal policy data or a licensed market-share
    file) in CSV/parquet.  Required columns: aip, crop_yr, state_cd, county_cd, crop_cd, ins_plan,
    plus any of METRIC_FIELDS (missing metrics become 0).  Optional: cov_lvl, coverage_flag,
    delivery_sys.  Codes may be numeric; they are zero-padded here.
    """
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path, dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]
    required = {"aip", "crop_yr", "state_cd", "county_cd", "crop_cd", "ins_plan"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"AIP extract {path} is missing columns: {sorted(missing)}")
    for col, default in (("cov_lvl", "00"), ("coverage_flag", "A"), ("delivery_sys", "R")):
        if col not in df:
            df[col] = default
    for col in METRIC_FIELDS:
        df[col] = _to_number(df[col]) if col in df else 0
    df["crop_yr"] = pd.to_numeric(df["crop_yr"], errors="coerce").astype("Int64")
    for col, width in KEY_WIDTHS.items():
        df[col] = df[col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(width)
    df["aip"] = df["aip"].astype(str).str.strip()
    return df


def reconcile(aip_df: pd.DataFrame, public_df: pd.DataFrame) -> pd.DataFrame:
    """Compare an AIP extract against public SOB totals by year/commodity/plan (sanity check)."""
    keys = ["crop_yr", "crop_cd", "ins_plan"]
    m = ["pol_sold_cnt", "liability_amt", "total_prem"]
    a = aip_df.groupby(keys)[m].sum().add_suffix("_aip")
    p = public_df[public_df["delivery_sys"].eq("R")].groupby(keys)[m].sum().add_suffix("_sob_reinsured")
    rec = a.join(p, how="outer").fillna(0)
    rec["prem_share_of_sob"] = rec["total_prem_aip"] / rec["total_prem_sob_reinsured"].where(rec["total_prem_sob_reinsured"] != 0)
    return rec.reset_index()


# --------------------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------------------
def save(df: pd.DataFrame, meta: dict) -> Path:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(FACT_PARQUET, index=False)
        out = FACT_PARQUET
        FACT_CSV.unlink(missing_ok=True)
    except ImportError:
        log.info("pyarrow not installed; writing compressed CSV instead.")
        df.to_csv(FACT_CSV, index=False)
        out = FACT_CSV
        FACT_PARQUET.unlink(missing_ok=True)
    meta["output_file"] = out.name
    META_JSON.write_text(json.dumps(meta, indent=2, default=str))
    log.info("Wrote %s (%s rows) and %s", out, f"{len(df):,}", META_JSON.name)
    return out


def load_processed(processed_dir: Path | str = PROCESSED_DIR) -> tuple[pd.DataFrame, dict]:
    """Entry point for the notebook: returns (fact_df, metadata)."""
    processed_dir = Path(processed_dir)
    meta_path = processed_dir / META_JSON.name
    if not meta_path.exists():
        raise FileNotFoundError(f"No processed data in {processed_dir}. Run `python data_pipeline.py` first.")
    meta = json.loads(meta_path.read_text())
    path = processed_dir / meta["output_file"]
    str_cols = {c: str for c in KEY_FIELDS if c != "crop_yr"}
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path, dtype=str_cols)
    return df, meta


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------
def run_pipeline(years: list[int] | None = None, all_years: bool = False, aip_file: str | None = None,
                 refresh: bool = False, use_docx: bool = True, with_names: bool = True) -> pd.DataFrame:
    session = make_session()
    layout, layout_source = resolve_layout(session, use_docx)
    log.info("Record layout: %s (%d fields, %d chars)", layout_source, len(layout), max(f.end for f in layout))

    if all_years:
        zpath = download(session, SOB_BASE_URL + ALL_YEARS_ZIP, RAW_DIR / ALL_YEARS_ZIP, refresh)
        df = pd.concat([parse_sumdat(p, layout) for p in extract_text_files(zpath)], ignore_index=True)
        if years:
            df = df[df["crop_yr"].isin(years)]
    else:
        available = list_available_files(session)
        if not years:
            years = [pick_latest_complete_year(session, available)]
        unknown = sorted(set(years) - set(available))
        if unknown:
            raise ValueError(f"Crop year(s) {unknown} not on the SOB index. Available: {min(available)}-{max(available)}")
        frames = []
        for yr in years:
            url = available[yr]
            raw = download(session, url, RAW_DIR / url.rsplit("/", 1)[-1], refresh)
            frames += [parse_sumdat(p, layout) for p in extract_text_files(raw)]
        df = pd.concat(frames, ignore_index=True)

    if df.empty:
        raise RuntimeError("No rows parsed - check the year selection and the layout warnings above.")
    years_loaded = sorted(int(y) for y in df["crop_yr"].unique())
    lookups = fetch_name_lookups(session, years_loaded, refresh) if with_names else {}

    aip_source = "delivery_channel"
    if aip_file:
        aip_df = load_aip_extract(Path(aip_file))
        rec = reconcile(aip_df, df)
        rec_path = PROCESSED_DIR / "aip_reconciliation.csv"
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        rec.to_csv(rec_path, index=False)
        log.info("AIP extract: %s rows, %d AIPs. Reconciliation vs public SOB -> %s",
                 f"{len(aip_df):,}", aip_df["aip"].nunique(), rec_path.name)
        df = aip_df
        aip_source = "aip_extract"

    fact = enrich(df, lookups)
    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "crop_years": years_loaded,
        "rows": len(fact),
        "layout_source": layout_source,
        "names_source": "RMA sobcov lookups" if lookups else "codes only",
        "aip_source": aip_source,
        "aip_file": str(aip_file) if aip_file else None,
        "source_url": SOB_INDEX_URL,
        "layout": [asdict(f) for f in layout],
    }
    save(fact, meta)
    return fact


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Download and parse RMA Summary of Business (YYsumdat) files.")
    p.add_argument("--year", type=int, nargs="+", help="crop year(s), e.g. --year 2025 2026 (default: latest complete)")
    p.add_argument("--all", action="store_true", help="use allsumdat.zip (every year); combine with --year to subset")
    p.add_argument("--aip-file", help="optional AIP-level extract (CSV/parquet) to enable a true AIP filter")
    p.add_argument("--refresh", action="store_true", help="re-download even if cached")
    p.add_argument("--no-docx", action="store_true", help="skip the .docx and use the hard-coded layout")
    p.add_argument("--no-names", action="store_true", help="skip commodity/plan name lookups")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        run_pipeline(a.year, a.all, a.aip_file, a.refresh, not a.no_docx, not a.no_names)
    except Exception as exc:
        log.error("Pipeline failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
