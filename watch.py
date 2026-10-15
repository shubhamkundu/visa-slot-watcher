#!/usr/bin/env python3
"""Visa slot watcher.

Reads checkvisaslots.com's PUBLIC availability pages (crowd-reported sightings)
and sends a loud phone push through ntfy.sh when a date you care about appears.

It never logs in to, or sends a single request to, usvisascheduling.com.
You still book every slot yourself, in your own account.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PAGE_URL = "https://checkvisaslots.com/latest-us-visa-availability/{slug}/"
BOOKING_URL = "https://www.usvisascheduling.com/"
USER_AGENT = "Mozilla/5.0 (personal visa slot watcher; polls every 5 min)"
DATE_FORMATS = ("%d %b, %y", "%d %b %Y", "%d %b, %Y", "%d %b %y")
DATE_RE = re.compile(r"\b\d{1,2} [A-Za-z]{3},? \d{2,4}\b")


def _env(name: str, default: str) -> str:
    """Read an env var, treating empty strings (unset GitHub vars) as missing."""
    value = os.environ.get(name, "").strip()
    return value if value else default


# ---------------------------------------------------------------- config
NTFY_SERVER = _env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = _env("NTFY_TOPIC", "")
VISA_TYPES = [s.strip() for s in _env("VISA_TYPES", "j-2-regular,j-1-regular").split(",") if s.strip()]
CUTOFF_DATE = dt.date.fromisoformat(_env("CUTOFF_DATE", "2026-11-20"))
CITIES = {c.strip().upper() for c in _env("CITIES", "").split(",") if c.strip()}  # empty = all
INCLUDE_VAC = _env("INCLUDE_VAC", "true").lower() == "true"
MAX_AGE_HOURS = float(_env("MAX_AGE_HOURS", "24"))
HEALTH_ALERT_AFTER = int(_env("HEALTH_ALERT_AFTER", "6"))  # consecutive failed runs
STATE_FILE = Path(_env("STATE_FILE", str(Path(__file__).with_name("state.json"))))
LOOP_SECONDS = int(_env("LOOP_SECONDS", "0"))  # 0 = run once (GitHub Actions)
DRY_RUN = _env("DRY_RUN", "false").lower() == "true"
TEST_ALERT = _env("TEST_ALERT", "false").lower() == "true"


# ---------------------------------------------------------------- parsing
def parse_date(text: str) -> dt.date | None:
    match = DATE_RE.search(text or "")
    if not match:
        return None
    raw = match.group(0)
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


_AGE_UNITS = {
    "sec": 1 / 3600, "min": 1 / 60, "hr": 1, "hour": 1,
    "day": 24, "wk": 168, "week": 168, "month": 720, "mo": 720, "yr": 8760, "year": 8760,
}


def parse_age_hours(text: str) -> float | None:
    """'18 hrs 6 mins ago' -> 18.1 ; 'just now' -> 0 ; unknown -> None."""
    text = (text or "").lower()
    if not text:
        return None
    if "just now" in text or "moments" in text:
        return 0.0
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+)\s*([a-z]+)", text):
        for key, hours in _AGE_UNITS.items():
            if unit.startswith(key):
                total += int(num) * hours
                found = True
                break
    return total if found else None


def _int(text: str) -> int | None:
    match = re.search(r"\d+", text or "")
    return int(match.group(0)) if match else None


def parse_page(html: str) -> list[dict]:
    """Extract sighting rows from the availability table."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    for table in soup.find_all("table"):
        header_cells = table.find("tr")
        if header_cells is None:
            continue
        headers = [c.get_text(" ", strip=True).lower() for c in header_cells.find_all(["th", "td"])]

        def col(*keys: str) -> int | None:
            for i, h in enumerate(headers):
                if all(k in h for k in keys):
                    return i
            return None

        i_loc = col("location")
        i_early = col("earliest")
        i_slots = col("slots")
        i_total = col("total")
        i_seen = col("last seen")
        i_rel = col("relative")

        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            get = lambda i: cells[i] if i is not None and i < len(cells) else ""  # noqa: E731
            location = get(i_loc) if i_loc is not None else cells[0]
            earliest = parse_date(get(i_early)) if i_early is not None else None
            if earliest is None:  # fallback: first cell that looks like a date
                earliest = next((d for d in map(parse_date, cells) if d), None)
            if not location or earliest is None:
                continue
            rows.append({
                "location": " ".join(location.upper().split()),
                "earliest": earliest,
                "slots": _int(get(i_slots)),
                "total": _int(get(i_total)),
                "last_seen": get(i_seen),
                "age_hours": parse_age_hours(get(i_rel)),
            })
    return rows


# ---------------------------------------------------------------- logic
def visa_label(slug: str) -> str:
    """'j-2-regular' -> 'J-2'."""
    return slug.replace("-regular", "").upper()


def matches(row: dict, today: dt.date) -> bool:
    location = row["location"]
    is_vac = location.endswith(" VAC") or location == "VAC"
    city = location.removesuffix(" VAC").strip()
    if is_vac and not INCLUDE_VAC:
        return False
    if CITIES and city not in CITIES:
        return False
    if not (today <= row["earliest"] <= CUTOFF_DATE):
        return False
    if row["age_hours"] is not None and row["age_hours"] > MAX_AGE_HOURS:
        return False
    return True


def sighting_key(slug: str, row: dict) -> str:
    return f"{slug}|{row['location']}|{row['earliest'].isoformat()}|{row['last_seen']}"


def describe(slug: str, row: dict) -> str:
    kind = "biometrics (VAC)" if row["location"].endswith("VAC") else "interview"
    city = row["location"].removesuffix(" VAC").title()
    count = f", {row['total']} date(s)" if row["total"] else ""
    age = f", seen {row['age_hours']:.0f}h ago" if row["age_hours"] is not None else ""
    return f"{visa_label(slug)} {city} {kind}: {row['earliest']:%a %d %b}{count}{age}"


# ---------------------------------------------------------------- io
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def fetch(slug: str) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(PAGE_URL.format(slug=slug), headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"could not fetch {slug}: {last_error}")


def notify(title: str, message: str, priority: int, tags: str) -> None:
    if DRY_RUN or not NTFY_TOPIC:
        print(f"[notify p{priority}] {title}\n{message}\n")
        return
    resp = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": str(priority),
            "Tags": tags,
            "Click": BOOKING_URL,
        },
        timeout=20,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------- run
def run_once(today: dt.date | None = None) -> None:
    state = load_state()
    seen: dict[str, str] = state.get("seen", {})
    today = today or dt.date.today()

    if TEST_ALERT:
        notify("Slot watcher test", "If your phone rang, alerts work. Book at usvisascheduling.com.", 5, "white_check_mark")

    new_hits: list[tuple[str, dict]] = []
    failures: list[str] = []
    for slug in VISA_TYPES:
        try:
            rows = parse_page(fetch(slug))
        except Exception as exc:  # network error, blocked, etc.
            failures.append(str(exc))
            continue
        if not rows:
            failures.append(f"{slug}: page loaded but no table rows parsed (layout may have changed)")
            continue
        print(f"{slug}: {len(rows)} rows")
        for row in rows:
            key = sighting_key(slug, row)
            if matches(row, today) and key not in seen:
                new_hits.append((slug, row))
                seen[key] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    if new_hits:
        # J-2 interview sightings are what matter most; J-1 is a hint only.
        new_hits.sort(key=lambda h: (h[0] != "j-2-regular", h[1]["earliest"]))
        has_j2 = any(slug == "j-2-regular" for slug, _ in new_hits)
        first_slug, first = new_hits[0]
        title = f"Slot seen: {describe(first_slug, first).split(':')[0]} {first['earliest']:%d %b}"
        body = "\n".join(describe(s, r) for s, r in new_hits)
        body += "\n\nCheck YOUR calendar now at usvisascheduling.com (J-1 sightings are only a hint for J-2)."
        notify(title, body, 5 if has_j2 else 4, "rotating_light" if has_j2 else "eyes")
        print(f"alerted {len(new_hits)} new sighting(s)")
    else:
        print("no new matching sightings")

    # Health check: warn once if the source keeps failing, so silence never means "broken".
    streak = state.get("fail_streak", 0)
    if failures and len(failures) == len(VISA_TYPES):
        streak += 1
        print("all sources failed:", *failures, sep="\n  ")
        if streak == HEALTH_ALERT_AFTER:
            notify("Slot watcher is NOT working", "\n".join(failures) + "\n\nCheck the slot page manually until fixed.", 4, "warning")
    else:
        if streak >= HEALTH_ALERT_AFTER:
            notify("Slot watcher recovered", "Data is flowing again.", 2, "white_check_mark")
        streak = 0

    # Forget sightings older than 30 days so the state file stays small.
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    seen = {k: v for k, v in seen.items() if dt.datetime.fromisoformat(v) > cutoff}
    save_state({"seen": seen, "fail_streak": streak})


def main() -> int:
    if not NTFY_TOPIC and not DRY_RUN:
        print("NTFY_TOPIC is not set; printing alerts instead of sending them.")
    print(f"watching {VISA_TYPES} | dates up to {CUTOFF_DATE} | cities: {', '.join(sorted(CITIES)) or 'all'}")
    if LOOP_SECONDS <= 0:
        run_once()
        return 0
    while True:  # local mode: python watch.py with LOOP_SECONDS=300
        try:
            run_once()
        except Exception as exc:
            print("run failed:", exc)
        time.sleep(max(LOOP_SECONDS, 120))


if __name__ == "__main__":
    sys.exit(main())
