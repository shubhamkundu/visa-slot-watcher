"""Data collection for the slot model: sighting log, nudges, and GitHub sync.

Every distinct sighting the watcher reads (for F-1, F-2, J-1 and J-2) is
appended to data/sightings.jsonl, and every check to data/polls.jsonl, so the
model knows both what was seen and when the phone was actually watching.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or ROOT / "data")
SIGHTINGS = DATA_DIR / "sightings.jsonl"
POLLS = DATA_DIR / "polls.jsonl"
MODEL_FILE = ROOT / "model.json"
DEVICE = os.environ.get("DEVICE_NAME", "phone")

# The visa types that share India's student/exchange (F/M/J) appointment pool.
FJ_TYPES = {"f-1-regular", "f-2-regular", "j-1-regular", "j-2-regular"}

BURST_WINDOW_MIN = int(os.environ.get("BURST_WINDOW_MIN") or 30)
BURST_MIN_REPORTS = int(os.environ.get("BURST_MIN_REPORTS") or 2)
BURST_COOLDOWN_MIN = int(os.environ.get("BURST_COOLDOWN_MIN") or 60)
SYNC_MINUTES = int(os.environ.get("SYNC_MINUTES") or 30)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

Notify = Callable[[str, str, int, str], None]
_SEEN_RE = re.compile(r"(\d{1,2} [A-Za-z]{3} \d{4}, \d{1,2}:\d{2} [AP]M)")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_last_seen(text: str) -> dt.datetime | None:
    """'16 Oct 2026, 03:56 AM' (the site prints UTC) -> aware datetime."""
    match = _SEEN_RE.search(text or "")
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%d %b %Y, %I:%M %p").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _append(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def record(slug: str, rows: list[dict], state: dict, now: dt.datetime | None = None) -> list[dict]:
    """Log sightings not logged before. Returns the new ones."""
    now = now or utcnow()
    logged: dict[str, str] = state.setdefault("logged", {})
    new: list[dict] = []
    for row in rows:
        seen = parse_last_seen(row.get("last_seen", ""))
        if seen is None:
            continue
        key = f"{slug}|{row['location']}|{seen.isoformat()}|{row['earliest'].isoformat()}"
        if key in logged:
            continue
        logged[key] = now.isoformat(timespec="seconds")
        new.append({
            "type": slug.replace("-regular", "").upper(),
            "location": row["location"],
            "city": row["location"].removesuffix(" VAC").strip(),
            "kind": "vac" if row["location"].endswith("VAC") else "interview",
            "earliest": row["earliest"].isoformat(),
            "slots": row.get("slots"),
            "total": row.get("total"),
            "seen_utc": seen.isoformat(),
            "logged_utc": now.isoformat(timespec="seconds"),
            "src": DEVICE,
        })
    _append(SIGHTINGS, new)
    # Keep the dedupe map small.
    cutoff = now - dt.timedelta(days=45)
    state["logged"] = {k: v for k, v in logged.items() if dt.datetime.fromisoformat(v) > cutoff}
    return new


def record_poll(ok: bool, n_rows: int, now: dt.datetime | None = None) -> None:
    now = now or utcnow()
    _append(POLLS, [{"t": now.isoformat(timespec="seconds"), "ok": ok, "rows": n_rows, "src": DEVICE}])


# ---------------------------------------------------------------- nudges
def check_burst(new: list[dict], state: dict, notify: Notify, now: dt.datetime | None = None) -> bool:
    """Several independent F/J sightings in a short window usually means a release.

    Independent = distinct report minutes (one person reporting 3 cities at once
    is a single report). Fires on BURST_MIN_REPORTS distinct report times, any city.
    """
    now = now or utcnow()
    recent: list[dict] = state.setdefault("recent_fj", [])
    for s in new:
        if s["type"].lower() + "-regular" in FJ_TYPES:
            recent.append({"t": s["seen_utc"], "city": s["city"], "type": s["type"], "kind": s["kind"],
                           "earliest": s["earliest"]})
    window_start = now - dt.timedelta(minutes=BURST_WINDOW_MIN)
    recent[:] = [r for r in recent if dt.datetime.fromisoformat(r["t"]) >= window_start]
    state["recent_fj"] = recent
    minutes = {r["t"][:16] for r in recent}
    cities = {r["city"] for r in recent}
    is_burst = len(minutes) >= BURST_MIN_REPORTS
    last = state.get("last_burst")
    cooled = last is None or now - dt.datetime.fromisoformat(last) >= dt.timedelta(minutes=BURST_COOLDOWN_MIN)
    if not (is_burst and cooled and new):
        return False
    lines = sorted({f"{r['type']} {r['city'].title()} {r['kind']}: {r['earliest'][5:]}" for r in recent})
    notify(
        "Check now: slots are being released",
        f"{len(minutes)} separate reports across {', '.join(sorted(c.title() for c in cities))} "
        f"in the last {BURST_WINDOW_MIN} min.\n" + "\n".join(lines[:8])
        + "\n\nLog in and check every city for J-2.",
        4, "zap",
    )
    state["last_burst"] = now.isoformat(timespec="seconds")
    return True


def load_model() -> dict:
    try:
        return json.loads(MODEL_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def check_peak_window(state: dict, notify: Notify, now: dt.datetime | None = None) -> bool:
    """Heads-up at the start of each top window from model.json (UTC hours)."""
    now = now or utcnow()
    model = load_model()
    if not model.get("ready"):
        return False
    for w in model.get("windows", []):
        if now.hour != w.get("start_utc_hour"):
            continue
        key = f"{now.date().isoformat()}T{now.hour:02d}"
        if state.get("last_window") == key:
            return False
        state["last_window"] = key
        notify(
            f"Peak window starting: {w.get('label', '')}",
            w.get("why", "Historically one of the busiest hours for F/J slot sightings.")
            + "\n\nCheck your J-2 calendar a few times in the next hour.",
            3, "alarm_clock",
        )
        return True
    return False


# ---------------------------------------------------------------- sync
def _git(*args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=slot-watcher-phone", "-c", "user.email=slot-watcher@users.noreply.github.com", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout,
    )


def _auth_url() -> str | None:
    url = _git("remote", "get-url", "origin").stdout.strip()
    match = re.match(r"https://(?:[^@/]+@)?github\.com/([^/]+)/(.+?)(?:\.git)?$", url)
    if not match:
        return None
    owner, repo = match.groups()
    return f"https://{owner}:{GITHUB_TOKEN}@github.com/{owner}/{repo}.git"


def maybe_sync(state: dict, now: dt.datetime | None = None) -> str:
    """Every SYNC_MINUTES: commit the data files, pull (gets model.json), push."""
    now = now or utcnow()
    if not GITHUB_TOKEN:
        return "skipped: no GITHUB_TOKEN"
    last = state.get("last_sync")
    if last and now - dt.datetime.fromisoformat(last) < dt.timedelta(minutes=SYNC_MINUTES):
        return "skipped: not due"
    state["last_sync"] = now.isoformat(timespec="seconds")
    auth = _auth_url()
    if not auth:
        return "failed: origin is not a GitHub https URL"
    try:
        _git("add", "data/sightings.jsonl", "data/polls.jsonl")
        if _git("diff", "--cached", "--quiet").returncode != 0:
            _git("commit", "-m", f"data: {DEVICE} sync")
        pull = _git("pull", "--rebase", "--autostash", auth, "main")
        if pull.returncode != 0:
            _git("rebase", "--abort")
            return "failed: pull " + pull.stderr.replace(GITHUB_TOKEN, "***")[-200:]
        push = _git("push", auth, "HEAD:main")
        if push.returncode != 0:
            return "failed: push " + push.stderr.replace(GITHUB_TOKEN, "***")[-200:]
    except (OSError, subprocess.SubprocessError) as exc:
        return f"failed: {exc}"
    return "ok"
