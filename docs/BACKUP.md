# Backup mode: your Garmin data on your own disk

Not on Intervals.icu – or simply want a copy of everything Garmin Connect has about you?
Backup mode keeps it in a folder on your computer or server, and keeps it current.
No Intervals account needed; nothing is written anywhere but that folder.

## Set it up

Container: the image with `BRIDGE_MODE=backup` and the command `run` – no `INTERVALS_API_KEY`.
Without a container: `garmin-intervals-bridge run --mode backup`.

## What it keeps, and where

Everything lands in the bridge's data folder (`~/GarminBridge/data`, `/data` in a
container):

| Path | What |
| --- | --- |
| `fits/<id>.fit` | every activity's **original recording**, byte for byte as your device made it |
| `raw/activities/<id>.json` | Garmin's summary of the activity (names, totals, Training Effect …) |
| `raw/activities/<id>.extras.json` | weather, gear, exercise sets, Garmin's own splits |
| `raw/<day>.json` | the day's wellness: the full answer of every Garmin endpoint – sleep, HRV, resting HR, stress, Body Battery, readiness, SpO₂, respiration, scores, race predictions, body composition, hydration, nutrition … |
| `wellness-files/<day>.zip` | the watch's **original wellness files** of the day (monitoring, sleep, HRV, Health Snapshots), as Garmin hands them out; `<day>.json` lists them |
| `raw/account/<date>.json` | weekly: profile, devices, settings, zones, personal records, badges, workouts, training plans, calendar |

FIT files open in [GoldenCheetah](https://www.goldencheetah.org/), most training
platforms and Garmin's own [FIT SDK](https://developer.garmin.com/fit/) tools; the JSON
files are plain text.

## When, kindly to Garmin

A run every 12 hours (`BRIDGE_BACKUP_HOURS`, e.g. `24` for once a day):

- **New activities**: one list request, then one download per new activity.
- **Wellness**: a day once it is final – at the first run after noon of the next day – and
  once more three days later for a watch that synced late; about 60 requests per day.
- **The account**: once a week.

## The past

Once, in a terminal – it runs next to the background backup and can be stopped and resumed:

```bash
garmin-intervals-bridge backup --from 2020-01-01
```

It takes everything from that day on: activities, wellness (about 30 requests per day:
`--endpoints standard` reads 15) and the account with its history. Days already backed up
are skipped (`--force` reads them again). Years of history take hours; the pause between
days (`--pause`, default 2 seconds) keeps it polite.

## Later on Intervals.icu?

The archive is exactly what the Intervals modes use: run `setup` again, answer **1**, and
`backfill --scope wellness --from-archive --from 2020-01-01 --apply` fills Intervals from
your backup without asking Garmin again.

Questions: <https://forum.intervals.icu/t/132863> or
<https://github.com/futureweb/garmin-intervals-bridge/issues>.
