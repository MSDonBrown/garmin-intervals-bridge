# Running the bridge as a systemd timer (no container)

Tested on AlmaLinux 10 with SELinux enforcing. Adjust paths and the user name
to taste; the defaults below are what the unit files expect.

```bash
# 1. Unprivileged service user and private state directory
useradd -r -s /sbin/nologin -d /var/lib/garmin-intervals-bridge -M gib
install -d -m 700 -o gib -g gib /var/lib/garmin-intervals-bridge

# 2. Virtualenv OUTSIDE any web document root (SELinux: systemd must be able to execute it)
python3 -m venv /opt/garmin-intervals-bridge/venv
/opt/garmin-intervals-bridge/venv/bin/pip install /path/to/garmin-intervals-bridge

# 3. Secrets, readable by the service user only
install -d -m 750 -o root -g gib /etc/garmin-intervals-bridge
umask 077; printf 'INTERVALS_API_KEY=%s\nINTERVALS_ATHLETE_ID=0\n' "$(read -rsp 'Intervals API key: ' k; echo "$k")" \
  > /etc/garmin-intervals-bridge/env
chown root:gib /etc/garmin-intervals-bridge/env; chmod 640 /etc/garmin-intervals-bridge/env

# 4. Garmin login once, interactively, as the service user (MFA prompt included; tokens only are stored)
sudo -u gib env HOME=/var/lib/garmin-intervals-bridge BRIDGE_DATA_DIR=/var/lib/garmin-intervals-bridge \
  GARMIN_TOKEN_DIR=/var/lib/garmin-intervals-bridge/tokens \
  /opt/garmin-intervals-bridge/venv/bin/garmin-intervals-bridge login

# 5. Units
cp deploy/systemd/garmin-intervals-bridge*.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now garmin-intervals-bridge.timer          # full run every 30 min (safety net)
systemctl enable --now garmin-intervals-bridge-watch.timer    # looks for new activities when due, Garmin only on new ones
systemctl start garmin-intervals-bridge.service     # first run now
journalctl -u garmin-intervals-bridge.service -o cat
```

The service runs in **dry-run mode** until you append `--apply` to `ExecStart`.
Read a few journals first: every run prints what it would write, per activity.

## Two timers, one purpose

- `garmin-intervals-bridge-watch` starts every minute and looks when a look is
  due – every 5 minutes by default (`BRIDGE_POLL_SECONDS`), or as quiet hours and
  the adaptive mode say (README: *How often it looks*); otherwise it ends at once,
  without a request. A look asks Intervals for the last three days (one
  ~450-byte request) and enriches only activities it sees for the first time. Every
  ten minutes it also looks at today's wellness record in Intervals and reads
  last night's values from Garmin once the official sync has delivered the
  sleep (see *Wellness* in the main README).
- `garmin-intervals-bridge` runs every 30 minutes over the last four days and
  catches anything the watcher missed (an import that lagged, a failed download
  that is due for its retry, new field definitions). With `--activity-interval 120`
  it reads Garmin's activity list every two hours, sooner when a retry is due;
  a run with nothing to do makes no Garmin request at all.

Both are dry runs until `--apply` is appended to their `ExecStart`.

## Backfilling the past

```bash
# as the service user, with the same environment as the units
garmin-intervals-bridge backfill --scope activities --from 2026-03-01            # dry run
garmin-intervals-bridge backfill --scope activities --from 2026-03-01 --apply
garmin-intervals-bridge backfill --scope wellness   --from 2026-01-01 --pause 3 --apply
garmin-intervals-bridge backfill --scope wellness   --from 2026-01-01 --from-archive --apply   # after new fields
garmin-intervals-bridge backfill --scope wellness   --from 2026-01-01 --endpoints heart_rates,floors --force-wellness --apply   # add endpoints to fetched days
garmin-intervals-bridge backfill --scope activities --from 2020-01-01 --archive-only --pause 3   # mirror every original FIT + summary, no Intervals
garmin-intervals-bridge backfill --scope wellness   --from 2020-01-01 --wellness-files --pause 3 # the device's original wellness files, no Intervals
garmin-intervals-bridge snapshot-account --history-from 2020-01-01                               # once: calendar, FTP and running-tolerance history
```

The archive is yours: `raw/<day>.json` holds every endpoint's full answer for
that day (30 of them with `--endpoints all`, intraday series included),
`raw/activities/<id>.json` Garmin's summary of an activity,
`raw/activities/<id>.extras.json` its weather, gear, exercise sets and Garmin's
own splits (run/walk/stand, climbs, intervals), and `fits/<id>.fit` the
recording itself. `wellness-files/<day>.zip` keeps the device's original
wellness files of each day exactly as Garmin delivers them (all-day heart
rate, respiration, stress, SpO₂ readings, overnight HRV, sleep, skin
temperature, Health Snapshots and a few dozen undocumented Garmin messages);
`wellness-files/<day>.json` lists what is inside, with every FIT file
CRC-checked and each Health Snapshot summarised (heart rate, RMSSD and SDRR,
respiration, SpO₂, stress). `snapshot-account` adds what is not a time
series (profile, settings, devices, zones, gear, personal records, badges,
goals, workouts, training plans with their details, the calendar, Garmin's
FTP and running tolerance of the recent weeks) as `raw/account/<date>.json`;
`garmin-intervals-bridge-account.timer` runs it monthly. Nothing is ever deleted; later fields or other
targets can be fed from the archive without asking Garmin again. ECG
recordings are not among the files Garmin offers this way.

The regular runs keep the mirror complete on their own: the watch fetches the
recording of a new activity, the next half-hourly sync adds its summary and
extras (in upload mode the upload run does both), so `--archive-only` is only
ever needed for the past; run against an older archive, it adds just what an
entry lacks (the splits, for example). Weather and gear that Garmin has not
attached yet are asked for again on the next run during the activity's first
two days. A finished day's wellness files are fetched with its final read,
one request per day.

A backfilled wellness day costs 15 Garmin requests (12 with `--endpoints essential`, about 30 with
`--endpoints all`), each activity one original
download; the pause keeps a long backfill polite. Runs are resumable: days and
activities already handled are skipped.

## Upload mode

When the official Garmin import is off (or Intervals can no longer expose
Garmin-imported data through its API), install
`garmin-intervals-bridge-upload.{service,timer}` instead of the watch timer:
it asks Garmin every 10 minutes for new activities and uploads their originals
(dry run until `--apply`), and puts their Garmin gear on them once `gear --apply`
has set it up. Change the 30-minute unit to `--scope wellness`. Three Garmin
requests per poll, about 450 a day.

Switching an enrich installation over:

1. In Intervals, turn the Garmin connection's **activity** import off (wellness
   and workout upload stay on). A Strava connection that imports activities
   must go off too: a copy already there blocks the original's upload.
2. `cp deploy/systemd/garmin-intervals-bridge-upload.{service,timer} /etc/systemd/system/`,
   add `--apply` to its `ExecStart`.
3. In `garmin-intervals-bridge.service`: `sync --scope wellness --apply`.
4. `systemctl daemon-reload && systemctl disable --now garmin-intervals-bridge-watch.timer
   && systemctl enable --now garmin-intervals-bridge-upload.timer`.

Activities imported before stay as they are (the bridge never deletes); new ones
arrive as your own uploads within ten minutes of Garmin's sync.

## Failure alerts

All three service units carry `OnFailure=garmin-intervals-bridge-alert@%n.service`.
The alert unit runs as root (it reads the journal and calls `sendmail`) and
needs the script installed next to the units:

```bash
install -m 755 deploy/systemd/gib-alert /usr/local/sbin/gib-alert
cp deploy/systemd/garmin-intervals-bridge-alert@.service /etc/systemd/system/
systemctl daemon-reload
```

That unit mails the failed unit's status and last 40 journal lines through
the local `sendmail`, at most once every six hours (a Garmin outage would
otherwise produce a mail per minute from the watcher). Configure the
addresses once:

```bash
printf 'ALERT_TO=you@example.org\nALERT_FROM=bridge@example.org\n' > /etc/garmin-intervals-bridge/alert.env
chmod 640 /etc/garmin-intervals-bridge/alert.env
```

Typical reasons for a failure: Garmin tokens expired (run `login` again as
the service user), Intervals API key revoked, no network.

## Daily health probe

`garmin-intervals-bridge health` logs in to Garmin with the stored tokens, makes
one small Intervals request, and then looks for errors the scheduled runs
handle quietly: timers that stopped running, a Garmin endpoint erroring on
each of the last three days, wellness fields Garmin delivered all week but
not in the last days (a changed response, a stopped sensor), activities
failing repeatedly, uploads pending reconciliation. It exits 2 only for those,
so a single bad night stays a journal warning while a real problem becomes
one mail a day through the same `OnFailure=` hook. The timer runs it once a day:

```bash
systemctl enable --now garmin-intervals-bridge-health.timer
```

## ImReady4 training advice (optional)

To have the morning runs write ImReady4's training advice into the existing
`TrainingAdvice` wellness field, add `BRIDGE_IMREADY4_ENABLED=true` to
`/etc/garmin-intervals-bridge/env`; the next run of the watch timer picks it
up, no new timer is needed. Before that, see what it would write today (as
the service user, with the same environment as the units, like the backfills
above):

```bash
garmin-intervals-bridge imready4                              # today, dry run
garmin-intervals-bridge imready4 --from 2026-01-01 --compare  # against the advice already in the field
```

Afterwards the journal shows `ImReady4 advice for …` once each morning.
Details: [docs/IMREADY4.md](../docs/IMREADY4.md).

## Notes

- A `watch` without `--apply` still records the activities it saw; after
  you add `--apply`, those are enriched by the next scan of Garmin's activity
  list (within two hours), not by the watcher.
- `status` takes no lock and works during a backfill; `health` has its own.
- The units carry two hardening layers: read-only system, private /tmp, no
  new privileges, and a system-call filter (`@system-service`), no
  capabilities, no device access and a 512 MB memory ceiling. If a future
  dependency needs more, the journal shows the refused call; loosen one
  directive rather than removing the block.
- The daily `health` digest counts the Garmin requests of the last 24 h
  (recorded per run); a normal day is about 75 plus a few per new activity,
  a backfill day more.
- Disk: one JSON snapshot per wellness day (about 100 kB) and the original
  plus partner FIT per activity (a few MB each); a year is well under 1 GB.
