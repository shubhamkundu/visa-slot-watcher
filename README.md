# Visa slot watcher

Sends a loud push to your phone when someone reports a J-2 (or J-1) appointment date in India before your cutoff.

**What it reads:** checkvisaslots.com's public availability pages, every 5 minutes, using GitHub Actions for free.

**What it never touches:** usvisascheduling.com. Your account and password are never involved, so there's no lockout or suspension risk. You still book the slot yourself.

## Setup (about 10 minutes)

1. **Install ntfy on your phone** from the [App Store](https://apps.apple.com/app/ntfy/id1625396347) or [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy).
2. **Subscribe to a hard-to-guess topic.** Tap **+** and enter your own random string (e.g. `j2-` plus 10+ random letters and digits). Anyone who knows the name can read it, so keep it random.
3. **Make the alerts loud.**
   - **Android:** in the ntfy app, set "Urgent" notifications to override Do Not Disturb.
   - **iPhone:** allow ntfy in your Focus settings.
4. **Add the topic to GitHub.** In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**, name it `NTFY_TOPIC`, and set the value to your topic name.
5. **(Optional) Add variables** on the **Variables** tab of the same page:

   | Variable | Default | Example |
   |---|---|---|
   | `CUTOFF_DATE` | no limit | `2026-11-20` to only alert for dates on or before this |
   | `CITIES` | all five posts | `MUMBAI,CHENNAI,HYDERABAD` |
   | `VISA_TYPES` | `j-2-regular,j-1-regular` | Leave as is |
   | `MAX_AGE_HOURS` | `24` | Ignore older sightings |

6. **Test it.** Go to **Actions → Slot watcher → Run workflow**, tick "Send a test push", and run it. Your phone should ring.

After that it runs every 5 minutes on its own.

## Reading an alert

- **"Slot seen: J-2 Mumbai interview 20 Nov"** at urgent priority: open usvisascheduling.com right away and check your own calendar.
- **J-1 sightings** come at high priority. They're only a hint, because J-1 and J-2 may not share the same slots.
- **VAC** means the biometrics appointment, and you need both biometrics and an interview. A VAC date by itself still tells you that city's calendar is moving.
- **Sightings come from other applicants' reports.** A slot can be gone within minutes, and quiet periods don't mean nothing is open.

## If it goes quiet

If the source fails 6 runs in a row (about 30 minutes), you get one **"Slot watcher is NOT working"** push. Then check the page manually. The usual cause is the site blocking GitHub's servers. If that happens, run it on your laptop instead:

```bash
pip install -r requirements.txt
NTFY_TOPIC=your-topic python watch.py   # checks every 5 min; add CUTOFF_DATE=2026-11-20 to limit dates
```

## Notes

- **Timing:** GitHub sometimes delays scheduled runs by 5–15 minutes, so keep checking the portal yourself in the evening (ET) too.
- **Inactivity:** GitHub turns off scheduled workflows after 60 days with no repo activity. This repo commits its state file, which counts as activity.
- **Tests:** `python -m unittest discover tests`
