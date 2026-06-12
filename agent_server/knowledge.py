"""Loads HSE knowledge data at import time."""
import csv
from datetime import datetime, timedelta
from pathlib import Path

import openpyxl

DATA_DIR = Path(__file__).parent / "data"


def _serial_to_date(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return datetime(1899, 12, 30) + timedelta(days=int(v.strip()))
    if isinstance(v, (int, float)):
        return datetime(1899, 12, 30) + timedelta(days=int(v))
    return None


def _load_hse_controls() -> list[dict]:
    seen: set[tuple] = set()
    controls: list[dict] = []
    with open(DATA_DIR / "HSE_Controls.csv", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = (row["Module"].strip(), row["Type"].strip(), row["Checklist"].strip())
            if key not in seen and key[2]:
                seen.add(key)
                controls.append(
                    {
                        "module": row["Module"].strip(),
                        "type": row["Type"].strip(),
                        "checklist": row["Checklist"].strip(),
                        "description": row["Description"].strip(),
                    }
                )
    return controls


def _load_p6_activities() -> list[dict]:
    wb = openpyxl.load_workbook(DATA_DIR / "P6_Lookahead.xlsx", data_only=True)
    ws = wb.active
    headers = None
    activities: list[dict] = []
    for row in ws.iter_rows(values_only=True):
        if headers is None:
            headers = [str(h).strip() if h else "" for h in row]
            continue
        if not any(row):
            continue
        r = dict(zip(headers, row))
        desc = str(r.get("Activity Description") or "").strip()
        if not desc:
            continue
        start = _serial_to_date(r.get("Start Date"))
        end = _serial_to_date(r.get("End_Date"))
        if not start:
            continue
        wbs_parts = [str(r.get(k) or "").strip() for k in ("L1", "L2", "L3", "L4", "L5", "L6")]
        activities.append(
            {
                "source": "P6",
                "activity_code": str(r.get("Activity Code") or "").strip(),
                "description": desc,
                "start_date": start,
                "end_date": end or start,
                "wbs": " > ".join(p for p in wbs_parts if p),
                "status": str(r.get("Activity Status") or "").strip(),
                "critical": str(r.get("Critical") or "").strip(),
            }
        )
    return activities


def _load_aphex_activities() -> list[dict]:
    activities: list[dict] = []
    with open(DATA_DIR / "Aphex_Data.csv", newline="", encoding="utf-8-sig") as f:
        reader = list(csv.DictReader(f))

    all_codes = {r["Folder Code"].strip() for r in reader if r.get("Folder Code")}
    for row in reader:
        name = row.get("Folder Name", "").strip()
        code = row.get("Folder Code", "").strip()
        start_s = row.get("Filtered Group Start Date", "").strip()
        end_s = row.get("Filtered Group End Date", "").strip()
        if not name or not start_s or not code:
            continue
        # Keep only leaf entries (no child has a code starting with this code + ".")
        if any(c.startswith(code + ".") for c in all_codes if c != code):
            continue
        try:
            sd = datetime.strptime(start_s, "%d/%m/%Y")
            ed = datetime.strptime(end_s, "%d/%m/%Y")
        except ValueError:
            continue
        activities.append(
            {
                "source": "Aphex",
                "activity_code": code,
                "description": name,
                "start_date": sd,
                "end_date": ed,
                "wbs": code,
                "status": "",
                "critical": "",
            }
        )
    return activities


HSE_CONTROLS: list[dict] = _load_hse_controls()
P6_ACTIVITIES: list[dict] = _load_p6_activities()
APHEX_ACTIVITIES: list[dict] = _load_aphex_activities()


def get_activities_in_window(
    start_date: datetime,
    days: int = 90,
    keyword_filter: str = "",
    source: str = "P6",
    max_per_horizon: int = 15,
) -> list[dict]:
    """Return activities starting within the window, optionally filtered by keyword."""
    pool = P6_ACTIVITIES if source == "P6" else APHEX_ACTIVITIES
    end_date = start_date + timedelta(days=days)
    kw = keyword_filter.lower() if keyword_filter else ""

    results = []
    for a in pool:
        if not (start_date <= a["start_date"] <= end_date):
            continue
        if kw and kw not in a["description"].lower() and kw not in a["wbs"].lower():
            continue
        results.append(a)

    # Sort by start_date
    results.sort(key=lambda x: x["start_date"])

    # Bucket into horizons and cap per horizon
    h30, h60, h90 = [], [], []
    d30 = start_date + timedelta(days=30)
    d60 = start_date + timedelta(days=60)
    for a in results:
        if a["start_date"] <= d30:
            h30.append(a)
        elif a["start_date"] <= d60:
            h60.append(a)
        else:
            h90.append(a)

    return h30[:max_per_horizon] + h60[:max_per_horizon] + h90[:max_per_horizon]


def get_control(checklist: str) -> dict | None:
    for c in HSE_CONTROLS:
        if c["checklist"] == checklist:
            return c
    return None
