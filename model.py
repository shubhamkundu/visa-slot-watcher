#!/usr/bin/env python3
"""Slot-timing model built from the phone's sighting log.

Reads data/sightings.jsonl and data/polls.jsonl and writes:
  model.json      - top check windows for the phone's peak-window nudges
  dashboard.json  - aggregates for the dashboard (one small document)

Method, in short:
- Sightings only count for timing when the phone was watching that hour
  (coverage from polls.jsonl), so phone downtime doesn't fake a quiet hour.
- Hour bins are UTC hours, which map to fixed India times (IST = UTC+5:30);
  the dashboard labels them in ET for the viewer's current date.
- Rates are smoothed toward the overall average (Gamma-Poisson shrinkage),
  so thin bins don't swing wildly. J-2 is shrunk toward the pooled F/J
  shape, because J-2 reports are rare but share the F/M/J appointment pool.
- Odds use a Poisson rate of recent qualifying J-2 sightings, with the J-1
  rate as a prior.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
UTC = dt.timezone.utc
IST = ZoneInfo("Asia/Kolkata")
ET = ZoneInfo("America/New_York")

ALPHA_HOURS = 3.0        # shrinkage strength for hour bins (in covered hours)
J2_PRIOR_HOURS = 24.0    # how strongly J-2 leans on the pooled F/J shape
READY_COVER_HOURS = 72   # need ~3 days of watching ...
READY_EVENTS = 20        # ... and this many F/J sightings before trusting windows
RECENT_DAYS = 14
TARGETS = ["2026-11-15", "2026-11-20", "2026-11-30", "2026-12-15", "2027-01-15"]


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def hour_floor(t: dt.datetime) -> dt.datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def smooth(counts: list[float], cover: list[float], alpha: float) -> list[float | None]:
    total_n, total_c = sum(counts), sum(cover)
    if total_c <= 0:
        return [None] * len(counts)
    base = total_n / total_c
    return [(n + alpha * base) / (c + alpha) for n, c in zip(counts, cover)]


def build(sightings: list[dict], polls: list[dict], now: dt.datetime) -> tuple[dict, dict]:
    # ---- dedupe sightings across devices / re-logs
    uniq: dict[str, dict] = {}
    for s in sightings:
        key = f"{s['type']}|{s['location']}|{s['seen_utc']}|{s['earliest']}"
        if key not in uniq or uniq[key].get("src") == "seed":
            uniq[key] = s
    events = sorted(uniq.values(), key=lambda s: s["seen_utc"])
    for s in events:
        s["_t"] = dt.datetime.fromisoformat(s["seen_utc"])

    # ---- coverage: UTC hours in which the phone had a successful check
    covered: set[dt.datetime] = set()
    for p in polls:
        if p.get("ok"):
            covered.add(hour_floor(dt.datetime.fromisoformat(p["t"])))
    # A sighting reported late in hour h is usually picked up early in h+1.
    def observed(t: dt.datetime) -> bool:
        h = hour_floor(t)
        return h in covered or (h + dt.timedelta(hours=1)) in covered

    timed = [s for s in events if s.get("src") != "seed" and observed(s["_t"])]

    cover_h = [0.0] * 24
    cover_dow = [0.0] * 7
    cover_how = [0.0] * 168
    for h in covered:
        dow = h.astimezone(IST).weekday()
        cover_h[h.hour] += 1
        cover_dow[dow] += 1
        cover_how[dow * 24 + h.hour] += 1

    def counts(pred) -> tuple[list[float], list[float], list[float]]:
        ch, cd, cw = [0.0] * 24, [0.0] * 7, [0.0] * 168
        for s in timed:
            if pred(s):
                h = s["_t"].hour
                d = s["_t"].astimezone(IST).weekday()
                ch[h] += 1
                cd[d] += 1
                cw[d * 24 + h] += 1
        return ch, cd, cw

    groups = {
        "FJ": lambda s: True,
        "J": lambda s: s["type"] in ("J-1", "J-2"),
        "J-2": lambda s: s["type"] == "J-2",
    }
    by_hour, by_dow, heat = {}, {}, {}
    raw = {}
    for name, pred in groups.items():
        ch, cd, cw = counts(pred)
        raw[name] = (ch, cd, cw)
        by_hour[name] = {"n": ch, "rate": smooth(ch, cover_h, ALPHA_HOURS)}
        by_dow[name] = {"n": cd, "rate": smooth(cd, cover_dow, ALPHA_HOURS * 4)}
        heat[name] = {"n": cw}

    # J-2 leans on the pooled F/J shape scaled by J-2's share.
    n_fj, n_j2 = sum(raw["FJ"][0]), sum(raw["J-2"][0])
    share = (n_j2 + 1) / (n_fj + 10)
    fj_rate = by_hour["FJ"]["rate"]
    if fj_rate[0] is not None:
        by_hour["J-2"]["rate"] = [
            (n + J2_PRIOR_HOURS * share * r) / (c + J2_PRIOR_HOURS)
            for n, c, r in zip(raw["J-2"][0], cover_h, fj_rate)
        ]

    total_cover = sum(cover_h)
    ready = total_cover >= READY_COVER_HOURS and n_fj >= READY_EVENTS

    # ---- top windows (UTC hours), non-adjacent
    windows = []
    if fj_rate[0] is not None:
        mean_rate = sum(fj_rate) / 24
        order = sorted(range(24), key=lambda h: fj_rate[h], reverse=True)
        picked: list[int] = []
        for h in order:
            if fj_rate[h] < 1.2 * mean_rate:
                break  # only hours clearly busier than average count as windows
            if all(min((h - p) % 24, (p - h) % 24) > 1 for p in picked):
                picked.append(h)
            if len(picked) == 3:
                break
        for h in sorted(picked):
            start = now.replace(hour=h, minute=0, second=0, microsecond=0)
            et, ist = start.astimezone(ET), start.astimezone(IST)
            lift = fj_rate[h] / mean_rate if mean_rate else 0
            windows.append({
                "start_utc_hour": h,
                "label": f"{et:%-I %p} ET ({ist:%-I:%M %p} IST)",
                "lift": round(lift, 2),
                "why": f"F/J slot sightings run about {lift:.1f}x the daily average in this hour "
                       f"({int(raw['FJ'][0][h])} seen over {int(cover_h[h])} watched hours).",
            })

    # ---- cities (last RECENT_DAYS)
    since = now - dt.timedelta(days=RECENT_DAYS)
    recent_cover_days = max(1e-9, len([h for h in covered if h >= since]) / 24)
    cities = []
    for city in sorted({s["city"] for s in events}):
        rows = [s for s in events if s["city"] == city and s["_t"] >= since]
        interview = [s for s in rows if s["kind"] == "interview"]
        leads = [(dt.date.fromisoformat(s["earliest"]) - s["_t"].date()).days for s in interview]
        last3 = [s for s in interview if s["_t"] >= now - dt.timedelta(days=3)]
        cities.append({
            "city": city,
            "fj_interview_per_day": round(len([s for s in interview if s.get("src") != "seed"]) / recent_cover_days, 2),
            "interview_sightings": len(interview),
            "vac_sightings": len(rows) - len(interview),
            "j2_sightings": len([s for s in rows if s["type"] == "J-2"]),
            "j1_sightings": len([s for s in rows if s["type"] == "J-1"]),
            "median_lead_days": statistics.median(leads) if leads else None,
            "best_interview_date_3d": min((s["earliest"] for s in last3), default=None),
            "last_seen_utc": max((s["seen_utc"] for s in rows), default=None),
        })
    cities.sort(key=lambda c: (-(c["fj_interview_per_day"] or 0), -c["interview_sightings"]))

    # ---- odds of a J-2 interview sighting on/before each target in the next 7 days
    def rate_before(typ: str, target: dt.date) -> float:
        n = len([s for s in timed if s["type"] == typ and s["kind"] == "interview"
                 and s["_t"] >= since and dt.date.fromisoformat(s["earliest"]) <= target])
        return n / recent_cover_days if recent_cover_days > 0.01 else 0.0

    odds = []
    enough = recent_cover_days >= 1.0
    for target_s in TARGETS:
        target = dt.date.fromisoformat(target_s)
        if target < now.date():
            continue
        lam_j1 = rate_before("J-1", target)
        n_j2 = rate_before("J-2", target) * recent_cover_days
        prior_days = 3.0
        lam_j2 = (n_j2 + prior_days * 0.5 * lam_j1) / (recent_cover_days + prior_days)
        odds.append({
            "target": target_s,
            "j2_rate_per_day": round(lam_j2, 3) if enough else None,
            "j2_p7": round(1 - math.exp(-7 * lam_j2), 3) if enough else None,
            "j1_p7": round(1 - math.exp(-7 * lam_j1), 3) if enough else None,
        })

    # ---- recent list + meta
    recent = [
        {k: s.get(k) for k in ("type", "city", "kind", "earliest", "slots", "total", "seen_utc", "src")}
        for s in sorted(events, key=lambda s: s["seen_utc"], reverse=True)[:60]
    ]
    ok_polls = [p for p in polls if p.get("ok")]
    last_poll = max((p["t"] for p in polls), default=None)
    first_poll = min((p["t"] for p in ok_polls), default=None)
    span_h = ((now - dt.datetime.fromisoformat(first_poll)).total_seconds() / 3600) if first_poll else 0
    meta = {
        "generated_utc": now.isoformat(timespec="seconds"),
        "ready": ready,
        "watching_since_utc": first_poll,
        "last_poll_utc": last_poll,
        "covered_hours": int(total_cover),
        "uptime_pct": round(100 * total_cover / span_h, 1) if span_h >= 1 else None,
        "sightings_total": len(events),
        "sightings_timed": len(timed),
        "by_type": dict(Counter(s["type"] for s in events)),
        "ready_needs": {"covered_hours": READY_COVER_HOURS, "fj_sightings": READY_EVENTS},
    }

    # Every report time seen so far (incl. seed), before coverage correction.
    raw_hour = [0] * 24
    for s in events:
        if s["_t"] >= now - dt.timedelta(days=30):
            raw_hour[s["_t"].hour] += 1

    dashboard = {
        "meta": meta,
        "raw_hour": raw_hour,
        "cover_hour": cover_h,
        "cover_dow": cover_dow,
        "by_hour": by_hour,
        "by_dow": by_dow,
        "heat": {"cover": cover_how, **{k: v["n"] for k, v in heat.items()}},
        "windows": windows,
        "cities": cities,
        "odds": odds,
        "recent": recent,
    }
    model = {"generated_utc": meta["generated_utc"], "ready": ready, "windows": windows}
    return model, dashboard


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--model-out", default=str(ROOT / "model.json"))
    ap.add_argument("--dash-out", default=str(ROOT / "dashboard.json"))
    args = ap.parse_args()
    data = Path(args.data)
    model, dash = build(load_jsonl(data / "sightings.jsonl"), load_jsonl(data / "polls.jsonl"),
                        dt.datetime.now(UTC))
    Path(args.model_out).write_text(json.dumps(model, indent=2) + "\n")
    Path(args.dash_out).write_text(json.dumps(dash, separators=(",", ":")) + "\n")
    m = dash["meta"]
    print(f"sightings={m['sightings_total']} timed={m['sightings_timed']} covered_h={m['covered_hours']} "
          f"ready={m['ready']} windows={[w['label'] for w in model['windows']]}")


if __name__ == "__main__":
    main()
