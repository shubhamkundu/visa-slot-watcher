# Refreshing the J-2 Slot Radar

Run by the daily scheduled task (or on request):

1. `git pull` this repo (phone data lands in `data/` every ~30 min).
2. `python model.py` → writes `model.json` and `dashboard.json`.
3. Commit and push `model.json` if it changed (the phone pulls it for peak-window nudges).
4. Write `dashboard.json` to the dashboard's database: document `dash/summary`
   in https://claude.ai/artifact/5fRsDe6TBdrh4NPNL9sgi2 (read it first for its
   version, then `set` with `file_path` and `if_version`).
