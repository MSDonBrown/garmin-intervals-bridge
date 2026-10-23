<p align="center"><img src="https://raw.githubusercontent.com/futureweb/garmin-intervals-bridge/main/docs/assets/icon-bridge-128.png" alt="Garmin Intervals Bridge icon" width="128" height="128"></p>

# Garmin → Intervals.icu Bridge

**Garmin filters the FIT files it sends to Intervals.icu. This puts the data back — and syncs a lot more.**

**Set up in five minutes:** [Windows](https://github.com/futureweb/garmin-intervals-bridge/blob/main/docs/WINDOWS.md) · [Mac](https://github.com/futureweb/garmin-intervals-bridge/blob/main/docs/MAC.md) · [Synology](https://github.com/futureweb/garmin-intervals-bridge/blob/main/docs/SYNOLOGY.md) · [Unraid](https://github.com/futureweb/garmin-intervals-bridge/blob/main/docs/UNRAID.md) · [Linux & Docker](https://github.com/futureweb/garmin-intervals-bridge#running-it)

- **Your Garmin metrics back** on every activity — Stamina, Recovery Time, VO₂max, Performance
  Condition, Training Effect, Sweat Loss … from the original file, nothing duplicated, training
  load untouched.
- **The wellness the official sync misses** — night SpO₂, respiration, HRV details, sleep
  stages, Body Battery, stress, readiness, logged nutrition, endurance & hill scores, race
  predictions … and the past, backfilled.
- **And more** — your Garmin gear (bikes, shoes, skis) on the activities, 20 ready-made charts, a note or
  push message when it needs you. It runs quietly in the background on your own computer, NAS or server.
- **Not on Intervals?** [Backup mode](https://github.com/futureweb/garmin-intervals-bridge/blob/main/docs/BACKUP.md) keeps all your Garmin data
  – original activity files, every day's wellness, the watch's own files – in a folder of yours.

> **Why:** since late September 2026 Garmin strips its own metrics from the FIT files it
> hands to partners. The bridge fetches the original from Garmin Connect, with your own
> login, and completes the activity the official sync created — or, in
> [swap mode](https://github.com/futureweb/garmin-intervals-bridge#modes), puts the original file into it.

<p align="center">
<img src="https://raw.githubusercontent.com/futureweb/garmin-intervals-bridge/main/docs/images/activity-fields.png" alt="Activity fields filled from the original FIT: training effect, VO2max, performance condition, recovery time, stamina, sweat loss" width="900"><br>
<sub>An officially imported run after the bridge: Training Effect, VO₂max, Performance Condition, Recovery Time, Stamina at start/end, Sweat Loss — all stripped by Garmin, all back.</sub>
</p>
<p align="center">
<img src="https://raw.githubusercontent.com/futureweb/garmin-intervals-bridge/main/docs/images/activity-stream-ga-speed.png" alt="Grade-adjusted speed stream added to the activity" width="900"><br>
<sub>A custom stream (grade-adjusted speed) written into the same activity, aligned by timestamp.</sub>
</p>
<p align="center">
<img src="https://raw.githubusercontent.com/futureweb/garmin-intervals-bridge/main/docs/images/fitness-charts.png" alt="Fitness page with Garmin Bridge charts: sleep stages, stress and Body Battery, kcal consumed vs. burned" width="900"><br>
<sub>Three of the twenty charts from the chart library: sleep stages, stress & Body Battery, intake vs. total burn.</sub>
</p>

Since the end of September 2026 the file Garmin hands to partners is not
the file your device recorded. (Garmin first switched the filter on in
March 2026, rolled it back after a week of protest, and switched it back
on half a year later.) The recording survives (power, heart rate, GPS, cadence,
developer fields); Garmin's own metrics do not. Open the same activity in
Intervals.icu and in Garmin Connect and you will miss Performance
Condition, Stamina, Recovery Time, VO₂max, Training Effect and Sweat Loss.
The original file you can download from Garmin Connect still has them.

This bridge is a small self-hosted service that:

- fetches the **original FIT** through Garmin Connect's private API, byte-identical to the browser export;
- finds the activity the **official sync already created** in Intervals.icu — the sync stays on, nothing is duplicated, deleted or recomputed;
- adds exactly **what Garmin stripped**, into the custom fields and streams *you* have configured (Stamina curve, Recovery Time, VO₂max, Performance Condition, Sweat Loss, Training Effect …);
- fills the **daily wellness values the official sync does not deliver**: night SpO₂, respiration, sleeping heart rate, Body Battery, HRV details, sleep stages, stress, readiness, floors, scale data — and your **logged nutrition** (kcal, carbs, protein, fat);
- can **backfill the past** (on the reference account SpO₂ had silently stopped arriving in mid-2025);
- reacts within minutes of the official import (it asks Intervals every 5 minutes – or, [adaptive](#how-often-it-looks), mostly around the hours you usually finish an activity), with Garmin contacted only when there is something new.

Everything is a dry run until you say `--apply`. Values that exist are never
overwritten unless they demonstrably came from a filtered file.

**Sister project:** the [Futureweb Intervals MCP](https://github.com/futureweb/intervals-mcp-server)
lets ChatGPT, Claude and any other MCP client read and analyse everything the bridge writes:
every custom activity field and stream, every native and `Garmin…` wellness field, with the
names and units of your own definitions. Install with `uvx futureweb-intervals-mcp@1.0.0b1`,
from Docker Hub or as a Claude Desktop bundle; [more below](#use-it-with-an-ai-coach-mcp).

## What gets synced

| | Activities (enrich mode) | Daily wellness |
| --- | --- | --- |
| **Source** | Original FIT from Garmin Connect | Garmin's daily endpoints (up to 30 of them) |
| **Target** | Your Intervals custom activity fields and custom streams, defined by you, read from your own definitions | Native Intervals wellness fields first, private `Garmin…` custom fields for the rest |
| **Examples** | Stamina / Potential Stamina streams, Recovery Time, VO₂max, Performance Condition, Sweat Loss, Aerobic/Anaerobic Effect, Stamina at start/end, EPOC, Training Load, grade-adjusted speed | SpO₂, respiration, sleeping HR, resting HR, HRV (+5-min high, 7-day avg), sleep seconds/score/stages, Body Battery max/min/charged/drained, training readiness, recovery time, acute load, stress, intensity minutes, floors, steps, hydration, sweat loss, weight, body fat, kcal consumed, total and active burn, carbohydrates, protein, fat (g and kcal), endurance & hill scores, fitness age, race predictions, VO₂max (run/bike) |
| **Rule** | Only what the partner copy lacks; aligned by timestamp; idempotent | Only empty values; locked days skipped; today's running totals wait until tomorrow |
| **Archive** | Original + partner copy of every activity, Garmin's summary, weather, gear, splits | Raw JSON of every endpoint and the watch's original wellness files (Health Snapshots included), every day |

Version 0.11.7. Verified end to end on one account (fenix 8, Edge 1040); what changed
when is in the [CHANGELOG](CHANGELOG.md), where it goes next in the
[roadmap](docs/ROADMAP.md).

## How it works

1. **Original FIT.** `garminconnect` downloads the activity's original
   export; the ZIP is unpacked in memory, the FIT is CRC-checked with
   Garmin's official FIT SDK and archived unchanged.
2. **Match.** The Intervals activity is found by `external_id` (the official
   sync stores the Garmin activity ID) or, failing that, by source, start
   time (±120 s) and duration (±5 %). Anything ambiguous is left alone.
3. **Gap.** The file Intervals received (`GET /activity/{id}/file`) is
   decoded next to the original. What only the original carries is the gap:
   whole message types, record fields (stream candidates), session fields
   (scalar candidates), developer fields, and Garmin-internal numeric IDs.
4. **Write back.** *Your own custom items* say what to write and from where.
   Intervals fills custom activity fields and custom streams from the FIT
   itself, and every definition names its source (`fit_session_field:
   "140.9"`, `fit_record_field: "stance_time"`, or a one-line script reading
   `m.f_138`), plus an optional first-import conversion. The bridge reuses
   those definitions verbatim: same fields, same source, same units, nothing
   Garmin-specific hard-coded. Scalars go through `PUT /activity/{id}`,
   streams through `PUT /activity/{id}/streams`, aligned to the activity's
   `time` stream by timestamp.
5. **Never overwrite real data.** An existing value is replaced only when
   its FIT source is present in the original and absent from the partner
   copy; then it cannot have come from data (Intervals stores `0` for a
   stripped source). A second run writes nothing.

Garmin does not filter every activity. The gap is measured per activity
and never assumed.

## Quick start

**On your own computer** (Windows, Mac, Linux desktop), Python 3.12 or newer:

```bash
pip install garmin-intervals-bridge          # Windows: py -m pip install garmin-intervals-bridge
```

Put `INTERVALS_API_KEY` and `BRIDGE_TIMEZONE` in a settings file or the environment, sign in to
Garmin with `garmin-intervals-bridge login`, then start it with `garmin-intervals-bridge run --apply`
(`run` without `--apply` is a dry run). All settings: [Configuration](#configuration).

**For development**, or to try single commands by hand:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q                                   # synthetic tests, no accounts needed

export INTERVALS_API_KEY=...                # Intervals → Settings → Developer
export INTERVALS_ATHLETE_ID=0               # 0 = the key's owner
export BRIDGE_DATA_DIR=./data               # FITs, raw JSON, SQLite, tokens

garmin-intervals-bridge login               # once, interactive, MFA supported; stores tokens only
garmin-intervals-bridge probe               # newest original FIT, archived; no Intervals access
garmin-intervals-bridge gap --activity-id <garmin id>      # what did Garmin strip? read-only
garmin-intervals-bridge enrich --activity-id <garmin id>   # dry run: the exact plan
garmin-intervals-bridge enrich --activity-id <garmin id> --apply
```

Then let it run on its own:

```bash
garmin-intervals-bridge watch --apply       # one cheap Intervals poll; Garmin only on new activities
garmin-intervals-bridge sync --apply        # full run over the last days, activities + wellness
```

Everything is a dry run until `--apply` is given. The plan printed by a dry
run is exactly what `--apply` would send.

## Commands

| Command | What it does |
| --- | --- |
| `backup [--from DATE [--to DATE]] [--scope …]` | Back up Garmin into the data folder, no Intervals needed: without `--from` what backup mode does every `BRIDGE_BACKUP_HOURS`, with `--from` the past. Runs next to a running bridge. |
| `swap <id or link> [--method replace\|upload] [--apply]` | The original into Garmin's filtered copy of one activity, also an older one (see [Modes](#modes), *swap*). |
| `notify-test` | Send a test e-mail / push message to the alert channels that are set up. |
| `login` | Interactive Garmin sign-in (email, password, MFA). Only session tokens are stored. |
| `probe [--activity-id]` | Download and archive one original FIT. |
| `gap --activity-id` | Original vs. the copy Intervals holds; lists everything only the original carries. |
| `compare-fit A B` | Same comparison for two local files (e.g. against a manual browser export). |
| `enrich <id or link> [--apply]` | One activity again, compared with what Intervals holds now – after Intervals' *Reprocess file*, or a new custom field or stream. Works next to a running bridge; `--activity-id <garmin id>` instead names the Garmin side. |
| `watch [--apply]` | Poll Intervals once (~450 bytes); enrich activities seen for the first time. |
| `sync-now [--apply] [--wait-new] [--activity ID] [--day D] [--scope …]` | Sync right now, on request: [On-demand sync](#on-demand-sync). One JSON result, exit code for the caller. |
| `sync [--scope all\|activities\|wellness] [--mode enrich\|upload] [--apply]` | Scheduled run over the last days. |
| `backfill --scope wellness\|activities --from DATE [--to DATE] [--apply]` | Paced, resumable run over the past; with `--swap` the originals go into Garmin's copies (in place). |
| `setup-fields [--apply]` | Create the private custom wellness fields the mapping uses. |
| `setup-charts [--apply]` | Create (and later complete) private fitness charts for the synced values. |
| `gear [--prefer intervals\|evidence\|garmin] [--map G=I] [--create] [--apply]` | Put Garmin's gear (bikes, shoes, skis …) on the Intervals activities, from the archive; see [Gear](#gear). |
| `hrvtrend [--date D \| --from D [--to D]] [--compare] [--apply] [--overwrite]` | Experimental trend advice into its own field `HRVTrendAdvice`; see [docs/HRV_TREND.md](docs/HRV_TREND.md). |
| `run [--apply] [--mode …] [--poll-seconds N] [--sync-minutes N]` | Keep running in one process: a poll every 5 minutes (see [How often it looks](#how-often-it-looks)), a full run every 30 minutes, the daily health check (what autostart and the containers start). See [Running it](#running-it). |
| `status [--check]` | Pending uploads and failed activities with their retry time. `--check`: exit 1 when the bridge has stopped working – no run for twice the expected time (the container image's healthcheck). |
| `reset-pending --activity-id <garmin id>` | Upload mode: clear a pending upload after you checked Intervals by hand. |
| `snapshot-account [--history-from DATE]` | Archive what is not a time series: profile, settings, devices, zones, gear, records, badges, workouts, plans, the calendar. |
| `health` | Daily digest: probes both services and reports errors that would otherwise stay silent (dead timers, endpoints failing for days, fields Garmin stopped delivering, activities failing repeatedly). Exit 2 only when a human is needed. |

## Modes

**enrich** (default). The official Garmin import stays on. Each imported
activity is completed with what its file lacks. This is the mode for
everyone who wants to keep Intervals' own import, training load and workout
pairing.

**upload** (`--mode upload`). The v0.1 behaviour for accounts that switched
the official activity import off: originals are posted as new activities,
guarded by a persistent *pending* state so a timed-out upload is never
replayed blindly, and blocked when an activity already exists nearby.
Intervals de-duplicates uploads by file hash, so with the official import
on this mode *would* create duplicates. Use it only with the import off.

**backup** (`BRIDGE_MODE=backup`, or `run --mode backup`). Garmin only, no Intervals: every
activity's original file with Garmin's summary and extras, each day's wellness (all
endpoints, plus the watch's original wellness files) and weekly the account, into the
data folder. Kind to Garmin – a day is read once it is final and once more three days
later, about 60 requests a day. `backup --from DATE` fills in the past. See
[docs/BACKUP.md](docs/BACKUP.md).

**swap** (`BRIDGE_SWAP=true` on top of enrich, off by default). The official import
stays on and is the trigger: with the next poll after Garmin's filtered copy arrives, the
bridge puts the device's original into Intervals instead of the filtered file. Intervals
then holds the complete file: every custom field and stream is computed by Intervals
itself, and *Reprocess file* keeps them. No polling of Garmin (one original download per
new activity), and with the bridge down the copy simply stays and is completed or
swapped later. Nothing on Garmin Connect is touched (the bridge only ever reads from
Garmin). Two methods, `BRIDGE_SWAP_METHOD`:

- `replace` (the default): the original goes into **the same activity** (Intervals'
  *replace file*, `POST /activity/{id}/file`). Same id and link; name, description,
  comments, edited intervals, RPE, gear and tags stay (intervals nobody edited are found anew
  in the complete file); **nothing is deleted**. Intervals
  still shows the activity as Garmin's import (`source: GARMIN_CONNECT`). Works for the
  past too: `backfill --scope activities --from DATE --swap --apply`.
- `upload`: the original as **a new activity of your own** (your upload, not
  Garmin-sourced data), carrying over what you may have set already (name, description,
  the paired workout, type, RPE, feel, tags, gear, commute/race, ignore settings), then
  **deletes the copy Garmin's import created in Intervals**. This is the only place the
  bridge deletes anything, so it is careful: only Garmin's import, only within
  `BRIDGE_SWAP_HOURS` (48) of its import, never with comments or hand-edited intervals
  on it, not when Garmin sent the unfiltered file anyway; upload first, delete last – a
  failure leaves a duplicate to see, never a loss; an unknown upload outcome stays
  *pending* and deletes nothing. The new activity has a new id, so links to the copy end.

`swap <id or link> [--method replace|upload] [--apply]` does it for one activity by hand,
also an older one.

## Wellness

Garmin's daily endpoints (sleep, HRV, stress, Body Battery, readiness,
respiration, SpO₂, hydration, scale, nutrition, intensity minutes, scores,
predictions, fitness age, lactate threshold, …) are fetched per day; every
raw response is archived as JSON. Values are written to Intervals only where
the day has nothing yet, locked days are skipped, and running totals of the
current day wait for tomorrow.

**When Garmin is asked: on evidence, not on a clock.** Today's values come
from nine endpoints (sleep, HRV, Training Readiness, scale and Garmin's
status metrics) and only exist once the watch has synced after you wake up.
Every ten minutes the bridge looks at Intervals' own record for today, which
costs no Garmin request; as soon as the official integration has put last
night's sleep there, it reads those endpoints once, typically within ten
minutes of the watch sync, and asks twice more for Training Readiness if that
lags behind. Recovery time, acute load, endurance and hill scores, cycling
VO₂max, fitness age and race predictions arrive in the morning as Garmin
sees them then; the day's final read replaces them with the evening's. A finished
day is read in full once the device has synced after it ended, so its
totals are complete; a night-time phone sync is enough. Without the official
wellness sync, Garmin itself is checked at a few fixed times in the morning,
and a day that never sees a sync is read anyway at 20:00 the day after. A
normal day costs about 80 Garmin requests, plus a few per new activity.

Native Intervals fields filled when empty: `restingHR`, `hrv`, `sleepSecs`,
`sleepScore`, `readiness`, `vo2max`, `steps`, `floorsClimbed`,
`hydrationVolume`, `spO2`, `respiration`, `avgSleepingHR`, `weight`,
`bodyFat`, and from logged food `kcalConsumed`, `carbohydrates`, `protein`,
`fatTotal`. Everything else goes to private custom fields named `Garmin…`,
listed in [docs/FIELD_MAPPING.md](docs/FIELD_MAPPING.md).
`BRIDGE_WELLNESS_PROFILE=recommended` (default) leaves out a handful of
duplicates and goals; `all` keeps every code.

Backfill the past when the official sync has gaps (on the reference account
SpO₂ stopped arriving in mid-2025):

```bash
garmin-intervals-bridge backfill --scope wellness --from 2025-01-01            # dry run
garmin-intervals-bridge backfill --scope wellness --from 2025-01-01 --pause 3 --apply
```

A backfill reads fifteen endpoints per day by default: the per-day measurements plus
endurance & hill score and race predictions (`--endpoints essential` for the twelve
measurements only, `--endpoints all` for every endpoint). It pauses between days and
skips days already fetched, so it can be interrupted and resumed. To add endpoints to
days backfilled before, name them and force the re-read, e.g.
`--endpoints endurance_score,hill_score,race_predictions --force-wellness`.

## Gear

The official sync does not pass gear to Intervals. The bridge keeps each
activity's gear (bike, shoes, skis …) in its archive anyway, so `gear` puts it
on the Intervals activities **without a single Garmin request**. Intervals
then counts distance, time and its gear reminders itself, and a bike's
components follow the bike. The bridge never writes to Garmin and never
removes gear from Intervals.

```bash
garmin-intervals-bridge gear                    # the plan: pairs, gaps, conflicts; writes nothing
garmin-intervals-bridge gear --create --apply   # create missing gear in Intervals, fill the gaps
```

In a container, next to the running bridge: `docker exec <container>
garmin-intervals-bridge gear …` (it does not wait for the running bridge).

- **Gaps** (Intervals has no gear on the activity, Garmin has) are filled
  whatever you choose.
- **Conflicts** (both have different gear) follow `--prefer`:
  `intervals` (default) keeps Intervals' — its auto-assign rules or your
  corrections; `evidence` takes Garmin's only when the record proves
  Intervals' wrong: that gear was not bought yet or already retired on that
  day, or the bike's own sensors (Di2, power meter … by serial number,
  learned from the rides both sides agree on) are not in the recording;
  `garmin` always takes Garmin's. Every conflict is listed in the plan,
  with the proof where there is one. The first `--prefer evidence` run
  reads the sensors of every archived ride once (a few minutes; cached in
  `gear-sensors.json`).
- **Pairs** are found by name (`Grail CF SLX 8 Di2` = `Canyon Grail CF SLX
  8 Di2`); different names are paired with `--map 'GARMIN NAME=INTERVALS
  NAME'`. Gear Intervals does not have is listed, or created with `--create`
  (retired gear only with `--include-retired`).
- **Totals:** Intervals adds each assignment to a gear's totals, but totals
  that drifted earlier (reassignments by hand) stay off, and it computes a
  recount without storing it. So after writing, the bridge stores Intervals'
  own recount for the gear it touched and their components; `--recalc` does
  that for all paired gear (dry run first: the plan shows each change).
  Intervals counts a gear only from its purchase date; the plan lists
  activities that carry gear from before it was bought (an older pair?).
- **Afterwards** the scheduled runs keep the last week in line with the same
  choices. Gear chosen on the watch comes with the activity; gear picked in
  the Garmin Connect app after the run is read once more half a day later.
  Once `--create` was used with `--apply`, a new pair you add in Garmin
  Connect is created in Intervals by the scheduled runs too (`--no-create`
  stops that). The choices live in the data folder: a restart or a new
  container with the same folder carries on.
- **History:** the bridge knows the gear of the activities in its archive —
  those since it started, plus what `backfill --scope activities --from DATE
  --archive-only` adds once (a few Garmin requests per activity; stop a
  running container for it). Activities from Strava cannot be changed through
  Intervals' API and are left alone.

## Training advice (experimental)

Experimental: `BRIDGE_HRVTREND_ENABLED=true` writes a second
advice into a field of its own, `HRVTrendAdvice`, from the 7-day HRV trend
against a 60-day normal range and the night's heart rate, as the HRV-guided
training studies did – calmer than the single-day score, meant for comparing
both side by side. It never touches `TrainingAdvice`; see
[docs/HRV_TREND.md](docs/HRV_TREND.md).

## Charts

The bridge's twenty fitness charts are published in Intervals' chart
library: Fitness page → a tab → *custom charts* → search for **Garmin
Bridge** and tick what you want: readiness & recovery, sleep stages, sleep
score & sleeping HR, SpO₂ & respiration, stress & Body Battery, HRV detail,
nutrition intake vs. burn, macro energy, energy balance per week and month
with weight, endurance & hill scores, VO₂max & fitness age, race
predictions, intensity minutes & sweat loss, hydration, steps, body
composition, skin temperature, Health Snapshot. Each is built from the fields the bridge
writes, with one axis per unit and values that hold until the next
measurement carried across the days in between.

Prefer your own copies? `setup-charts --apply` creates the same charts as
private items in your account, only with the fields that already exist,
and completes them later as fields appear (for example after a backfill
with every endpoint). Charts you made yourself are never touched, even with
the same name. Intervals' API cannot place a chart on a Fitness tab; that
last click is yours either way.

## Use it with an AI coach (MCP)

The sister project [Futureweb Intervals MCP](https://github.com/futureweb/intervals-mcp-server)
lets ChatGPT, Claude and any other MCP client coach from everything the bridge writes:
Stamina per interval, Training Effect, Recovery Time, VO₂max, running dynamics, sleep and
HRV details, fueling and sweat loss, read with the names and units of your own fields and
set against your personal baselines. It talks to the Intervals.icu API only, never to
Garmin, and is read-only by default. Until the bridge has written this morning's values,
the MCP reports them as "not yet available" rather than as missing or zero; each project
also works without the other.

```bash
uvx futureweb-intervals-mcp@1.0.0b1                                                  # from PyPI, starts on stdio
docker run --rm -i -e API_KEY=... -e ATHLETE_ID=i123456 futurewebat/futureweb-intervals-mcp:1.0.0b1
```

Claude Desktop: open `futureweb-intervals-mcp-1.0.0b1.mcpb` from the
[release page](https://github.com/futureweb/intervals-mcp-server/releases/tag/v1.0.0b1).
[GitHub](https://github.com/futureweb/intervals-mcp-server) ·
[PyPI](https://pypi.org/project/futureweb-intervals-mcp/) ·
[MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.futureweb/intervals-mcp-server) ·
[how it reads the bridge's data](https://github.com/futureweb/intervals-mcp-server/blob/main/docs/GARMIN_BRIDGE.md)

## Running it

**systemd (recommended, no container):** an unprivileged service user, a
`watch` timer (it starts every minute and looks when due, every 5 minutes by default) and a 30-minute full run, secrets in an
`EnvironmentFile`, hardened units. See [deploy/README.md](deploy/README.md).

**Windows, macOS or any PC without systemd:** `pip install garmin-intervals-bridge`, then `garmin-intervals-bridge run --apply` in a window that stays open.

### Upload mode: when the official import is off

Garmin's partner terms now bar Intervals from passing Garmin-sourced data on
through its own API, and Intervals has said it will probably have to block
that. Data you upload yourself is not affected. The bridge's **upload mode**
is built for exactly this: switch the official Garmin import off in
Intervals, and the bridge uploads the **original, unfiltered FIT** of every
new activity as your own file (`source: UPLOAD`, `external_id: garmin:<id>`).
Intervals then computes everything from the complete file itself: Stamina
streams, Recovery Time, VO₂max, Performance Condition, Training Effect,
Sweat Loss and your other custom items appear without any enrichment, no
`icu_intervals_edited` flag is set, and Intervals' *Reprocess file* keeps all of it.
Wellness and gear work the same in both modes. Turn off a Strava connection's activity
import as well: a Strava copy that arrives first blocks the original's upload. New
activities arrive within ten minutes of Garmin's sync (the bridge asks Garmin every 10
minutes, about 450 requests a day); activities imported before stay as they are.

```bash
garmin-intervals-bridge sync --scope activities --mode upload --activity-days 2          # dry run
garmin-intervals-bridge sync --scope activities --mode upload --allow-activity-upload --apply
garmin-intervals-bridge run --mode upload --apply       # keeps polling Garmin every 10 minutes
garmin-intervals-bridge backfill --scope activities --mode upload --from 2026-01-01 --apply   # the past, as your uploads
garmin-intervals-bridge backfill --scope activities --from 2026-01-01 --archive-only     # mirror first, upload later
```

Uploads are de-duplicated against what Intervals already has (external id,
file hash, start time) and never retried blindly: an upload with an unknown
outcome stays `pending` until you check. Systemd users swap the watch timer
for `deploy/systemd/garmin-intervals-bridge-upload.timer` (10 minutes) and
keep the 30-minute unit for wellness.

**Docker / Podman:** a ready-made image is on the GitHub Container Registry,
`ghcr.io/futureweb/garmin-intervals-bridge`, and on Docker Hub,
`futurewebat/garmin-intervals-bridge` (for NAS boxes whose container manager
only knows Docker Hub, e.g. Synology – step by step in [docs/SYNOLOGY.md](docs/SYNOLOGY.md), with a project
file to paste; for Unraid there is a container template, [docs/UNRAID.md](docs/UNRAID.md)). Tags `latest` and the
version, amd64 and arm64. It runs as a non-root user and takes the same environment
variables; without compose:

```bash
docker run --rm -it -v $PWD/data:/data -e INTERVALS_API_KEY=... -e BRIDGE_TIMEZONE=Europe/London ghcr.io/futureweb/garmin-intervals-bridge login
docker run --rm    -v $PWD/data:/data -e INTERVALS_API_KEY=... -e BRIDGE_TIMEZONE=Europe/London ghcr.io/futureweb/garmin-intervals-bridge sync --apply
```

`BRIDGE_TIMEZONE` is your own zone (`Europe/London` above is an example): a
container has no time zone of its own, and the zone decides which date last
night's sleep and a day's totals belong to.

```bash
cp .env.example .env            # fill INTERVALS_API_KEY and BRIDGE_TIMEZONE
mkdir -p data && chown 10001:10001 data
docker compose pull             # ghcr.io/futureweb/garmin-intervals-bridge (amd64 + arm64); or: docker compose build
docker compose run --rm bridge login
docker compose up -d            # `run`: a dry run, always on; docker compose logs -f shows what it would do
# then command: ["run", "--apply"] in compose.yaml, and: docker compose up -d
```

`login` is the one interactive step: `run --rm` gives it a terminal, it asks
for your Garmin e-mail, password and MFA code, uses the password once and
keeps only Garmin's session tokens in the mounted `data/` volume
(`data/tokens`), where they refresh themselves for about a year. Every
other command just uses those tokens. If Garmin answers the login with
429, wait an hour and try again.

The compose service runs `run` and restarts with the machine. It checks itself every 5 minutes
(`status --check`, no request to anyone; the compose file, the Unraid template and the Synology
guide set this healthcheck): `docker ps`, Portainer, Unraid or Synology show the container as
*unhealthy* when no run came for about an hour – not during a break Intervals asked for, nor
while it waits for a Garmin sign-in (the alerts tell you about that). With a plain `docker run`,
add `--health-cmd "garmin-intervals-bridge status --check" --health-interval 5m`.
Prefer cron? Leave
the service stopped and schedule `docker compose run --rm -T bridge watch --apply`
every 5 minutes and `... sync --apply` every 30 minutes instead.

### How often it looks

Each look for new activities is one small request to Intervals.icu (in upload mode three to
Garmin). By default the bridge looks every 5 minutes, day and night. Intervals.icu serves a
lot of athletes on its own servers; fewer requests are kinder to it:

- `BRIDGE_POLL_HOURS=7-23`: look only in these hours, not at all outside them …
- `BRIDGE_POLL_QUIET_SECONDS=1800`: … or every 30 minutes outside them instead. Then, for an
  hour after a new activity, it looks at the full pace again – a second session or the
  cool-down may follow.
- `BRIDGE_POLL_MODE=adaptive` (**beta**): the bridge learns from its own archive when your
  activities usually end – per weekday and hour, the last twelve weeks, recent weeks counting
  most – and looks at the full pace (`BRIDGE_POLL_SECONDS`) only then, every few minutes
  around those hours, and every 30 minutes (`BRIDGE_POLL_QUIET_SECONDS`) at times you hardly
  ever finish one. It never looks faster than `BRIDGE_POLL_SECONDS`, and never outside
  `BRIDGE_POLL_HOURS` unless a quiet pace is set. It needs 15 activities in the archive; until
  then it keeps the full pace. What it learned is in the log; feedback is
  welcome in the [forum thread](https://forum.intervals.icu/t/132863).

The full runs every 30 minutes go on regardless, and they read Garmin's activity list every
two hours: nothing is missed, a slower pace only means an activity may arrive later.

**After a pause** – a computer switched on once or twice a week, a server down for days, a
Garmin sign-in that waited – the first runs read back to where the bridge's last run left
off, up to 14 days: the activities and wellness days in between are completed as if it had
run all along (each missed wellness day once, about 15 Garmin requests). If a run is cut short
(Garmin limits it, the computer goes off), the next one carries on. Longer pauses: a
`backfill`.

### On-demand sync

The timers (a look every 5 minutes, a full run every 30) are the bridge's own rhythm. When *you*
– or an agent, a shortcut, a script – know something has happened (the ride is done, the watch
has synced), ask for a sync right now:

```
docker exec garmin-intervals-bridge garmin-intervals-bridge sync-now --apply --wait-new
docker exec garmin-intervals-bridge garmin-intervals-bridge sync-now --apply --activity i12345678
docker exec garmin-intervals-bridge garmin-intervals-bridge sync-now --apply --scope wellness --day 2026-10-22
```

It goes to Garmin at once – whatever `BRIDGE_POLL_HOURS`, the activity-scan skip and the wellness
refresh window say – and enriches what Intervals holds. Nothing listens on a port: the trigger is
the command, so there is nothing new to protect. It prints one JSON result and exits with a code:

| Exit | `status` | Meaning |
| --- | --- | --- |
| 0 | `done` | Finished (`new` counts what was enriched or written) |
| 2 | `pending` | `--wait-new`: nothing new yet after `BRIDGE_ONDEMAND_RETRIES` looks – try again later |
| 3 | `paused` | Garmin or Intervals.icu asked for a break; `retry_after_seconds` says how long |
| 4 | `login_needed` | Sign in to Garmin again (`login`) |
| 5 | `busy` | A running sync did not let go of its part within `BRIDGE_ONDEMAND_LOCK_WAIT_SECONDS` |
| 1 | `error` | Anything else; `message` says what |

It uses the same per-part locks as the other commands, so it waits for a running cycle (or a
backfill) instead of overlapping with it. Once it has written, the timed ones step back: the
next full run of `run` waits `BRIDGE_ONDEMAND_SYNC_HOLDOFF_MINUTES`, and the polls pause for
`BRIDGE_ONDEMAND_POLL_HOLDOFF_SECONDS`. A second call within `BRIDGE_ONDEMAND_MIN_GAP_SECONDS`
returns the first result (`"coalesced": true`) without asking Garmin again; `--force`, or naming
an activity or day, always asks. Without `--apply` it is a dry run and delays nothing.

### When the bridge needs you

Garmin's session can end (a password change, a revoked device); then nothing syncs until
someone signs in again – and nobody reads a log file. So the bridge tells you: a note in
your Intervals calendar (from a bridge running with `--apply`), and, when set up, an
**e-mail** and/or a **push message** – once per episode, and once a day for anything the
daily health check finds that needs a person.

- E-mail goes out through your own mailbox (SMTP) to yourself: `BRIDGE_ALERT_EMAIL` and
  `BRIDGE_SMTP_PASSWORD` – for Gmail, iCloud and Yahoo an app password; GMX and web.de
  need "POP3/IMAP access" switched on. The server is known for the common providers;
  any other one is `BRIDGE_SMTP_HOST` / `BRIDGE_SMTP_PORT` / `BRIDGE_SMTP_USER`.
- Push messages go through [ntfy](https://ntfy.sh) (free app, no account):
  `BRIDGE_NTFY_URL=https://ntfy.sh/<a long topic name only you know>`; they carry the
  headline only.

`notify-test` sends a test message to the channels you set up.

## Configuration

Variables can also come from a settings file: the one named by `--env-file` or
`BRIDGE_ENV_FILE`, else `.env` in the working directory, else `~/GarminBridge/.env`
(on Windows in your user folder). Variables already set always
win. Relative paths in a settings file count from its folder.

| Variable | Default | Meaning |
| --- | --- | --- |
| `INTERVALS_API_KEY` | | Intervals API key (HTTP Basic, user `API_KEY`) |
| `INTERVALS_ATHLETE_ID` | `0` | `0` addresses the key's owner |
| `BRIDGE_DATA_DIR` | `./data` | Originals, partner copies, raw wellness JSON, SQLite state, lock files |
| `GARMIN_TOKEN_DIR` | `$BRIDGE_DATA_DIR/tokens` | Garmin session tokens (mode 0700) |
| `BRIDGE_TIMEZONE` | this computer's time zone | Your local day boundary (which date last night's sleep belongs to), e.g. `Europe/London`. A container has none of its own: set it there (otherwise UTC) |
| `BRIDGE_ACTIVITY_LOOKBACK_DAYS` | `4` | `sync` window for activities (1–30); after a pause back to the last run, up to 14 days |
| `BRIDGE_WELLNESS_LOOKBACK_DAYS` | `3` | `sync` window for wellness (1–30); after a pause back to the last run, up to 14 days |
| `BRIDGE_WELLNESS_REFRESH_HOURS` | `4` | Backfills only: how soon a backfill re-reads today or yesterday. The scheduled runs read on evidence instead (see [Wellness](#wellness)) |
| `BRIDGE_WELLNESS_PROFILE` | `recommended` | `recommended` or `all` custom fields |
| `BRIDGE_GARMIN_REQUEST_DELAY` | `0.5` | Seconds between Garmin requests (minimum 0.25) |
| `BRIDGE_STALE_HOURS` | `24` | `health` alerts once a service has failed this long |
| `BRIDGE_HRVTREND_ENABLED` | `false` | `true`: write the experimental trend advice into `HRVTrendAdvice` each morning (see [docs/HRV_TREND.md](docs/HRV_TREND.md)) |
| `BRIDGE_LOG_FILE` | | Also log to this file (5 MB, three old ones kept) |
| `BRIDGE_ALLOW_ADMIN` | `false` | Windows: `true` lets the bridge run in a window opened *as administrator* (refused otherwise: folders created there are closed to the background bridge; see [docs/WINDOWS.md](docs/WINDOWS.md)) |
| `BRIDGE_UPDATE_CHECK` | `true` | Once a day, ask pypi.org for the newest version and log a hint with the update command; `false` turns it off |
| `BRIDGE_ALERT_EMAIL` | | Alerts by e-mail to this address, sent from its own mailbox (see [When the bridge needs you](#when-the-bridge-needs-you)) |
| `BRIDGE_SMTP_PASSWORD` | | Password to send from that mailbox (often an app password) |
| `BRIDGE_SMTP_HOST`, `_PORT`, `_USER`, `_FROM` | known for common providers; `587`; the address | Another mail server (465 = SSL, otherwise STARTTLS) |
| `BRIDGE_NTFY_URL`, `BRIDGE_NTFY_TOKEN` | | Alerts as push messages (ntfy topic URL; a token for a protected server) |
| `BRIDGE_BACKUP_HOURS` | `12` | Backup mode: hours between runs (1–168); a day's wellness is taken once it is final either way |
| `BRIDGE_MODE` | `enrich` | What `run` does without `--mode`: `enrich`, `upload` or `backup` (Garmin only, no Intervals) |
| `BRIDGE_POLL_SECONDS` | `300` (upload mode `600`) | Seconds between the looks for new activities, of `run` (`--poll-seconds` wins) and of the `watch` timer; the fastest pace, in the adaptive mode too ([How often it looks](#how-often-it-looks)) |
| `BRIDGE_POLL_HOURS` | always | Look only in these hours, e.g. `10-18` or `6-9,16-22` (`22-6` runs over midnight). Outside them not at all – or every `BRIDGE_POLL_QUIET_SECONDS`; the half-hourly full runs go on, so an activity still arrives (within about two hours) and so does last night's wellness |
| `BRIDGE_POLL_QUIET_SECONDS` | | Outside `BRIDGE_POLL_HOURS` look this often instead of not at all, and at unusual times in the adaptive mode (there `1800` by default); at the full pace again for an hour after a new activity |
| `BRIDGE_POLL_MODE` | `fixed` | `adaptive` (beta): learn when your activities usually end, look at the full pace only then |
| `BRIDGE_SWAP` | `false` | `true`: the original of each new activity instead of Garmin's filtered copy (see [Modes](#modes)) |
| `BRIDGE_SWAP_METHOD` | `replace` | `replace`: the original into the same activity, nothing deleted; `upload`: a new activity of your own, Garmin's copy deleted |
| `BRIDGE_SWAP_HOURS` | `48` | `upload` only: only copies imported this recently are swapped automatically |
| `BRIDGE_INTERVALS_NOTES` | `true` | When Garmin's session ends and a new `login` is needed, a bridge running with `--apply` puts one note in your Intervals calendar (once per episode, never deleted by the bridge); `false` turns it off |

### Timers and thresholds

Every timer and threshold is a setting. The default is what the bridge always did; an empty value
means the default; a value outside its range (or no number) stops the bridge at start and names the
variable. Changes apply at the next start.

**On-demand sync (`sync-now`)**

| Variable | Default | Range | Meaning |
| --- | --- | --- | --- |
| `BRIDGE_ONDEMAND_SYNC_HOLDOFF_MINUTES` | `30` | 0–1440 minutes | After an on-demand sync the next full run of `run` waits at least this long (0 = no hold-off) |
| `BRIDGE_ONDEMAND_POLL_HOLDOFF_SECONDS` | `900` | 0–86400 seconds | After an on-demand sync the regular polls for new activities pause this long ("Check now" still works) |
| `BRIDGE_ONDEMAND_MIN_GAP_SECONDS` | `60` | 0–3600 seconds | A `sync-now` without a named activity or day, this soon after the last one, returns its result |
| `BRIDGE_ONDEMAND_LOCK_WAIT_SECONDS` | `600` | 0–7200 seconds | How long `sync-now` waits for a running sync (a cycle of `run`, a timer, a backfill) to let go of its part |
| `BRIDGE_ONDEMAND_MAX_SECONDS` | `900` | 60–7200 seconds | Longest a whole `sync-now` may take, waiting and retrying included (then it reports and stops) |
| `BRIDGE_ONDEMAND_RETRIES` | `5` | 0–50 tries | `sync-now --wait-new`: how often to look again when nothing new is there yet (the watch may still be syncing) |
| `BRIDGE_ONDEMAND_RETRY_SECONDS` | `120` | 10–3600 seconds | `sync-now --wait-new`: pause between those looks |
| `BRIDGE_ONDEMAND_ACTIVITY_DAYS` | `2` | 1–30 days | `sync-now` looks this many days back for activities (today included) |
| `BRIDGE_ONDEMAND_WELLNESS_DAYS` | `2` | 1–30 days | `sync-now` reads wellness for this many days (today included), ignoring the refresh window |

**The run loop (`run`)**

| Variable | Default | Range | Meaning |
| --- | --- | --- | --- |
| `BRIDGE_SYNC_MINUTES` | `30` | 5–1440 minutes | Minutes between the full runs (`--sync-minutes` wins; backup mode uses `BRIDGE_BACKUP_HOURS`) |
| `BRIDGE_ACTIVITY_SCAN_MINUTES` | `120` | 0–1440 minutes | Enrich mode: the full run skips its Garmin activity scan while the last one is younger (0 = never skip) |
| `BRIDGE_TICK_SECONDS` | `60` | 5–3600 seconds | How often the loop asks whether a poll or a full run is due |
| `BRIDGE_PART_BUSY_RETRY_SECONDS` | `300` | 30–3600 seconds | A full run that left a part out because a backfill works on it comes back this soon |
| `BRIDGE_BUSY_WAIT_SECONDS` | `300` | 0–86400 seconds | A backfill waits this long for a running sync to finish with its part |
| `BRIDGE_RATE_LIMIT_PAUSE_SECONDS` | `900` | 60–86400 seconds | Least pause after Intervals.icu answers 429 (longer if it asks for longer) |
| `BRIDGE_BLOCK_PAUSE_SECONDS` | `900` | 60–86400 seconds | Pause after Garmin blocks, rate-limits or wants a sign-in |
| `BRIDGE_FAILURE_BACKOFF_SECONDS` | `300` | 30–3600 seconds | A failed full run is tried again after this, doubling with each failure up to the full-run interval |
| `BRIDGE_HEALTH_HOURS` | `24` | 1–168 hours | Hours between the health probes written to the log |
| `BRIDGE_HEALTH_FIRST_SECONDS` | `300` | 0–86400 seconds | The first health probe comes this long after the start |

**Polling (`watch`, adaptive mode)**

| Variable | Default | Range | Meaning |
| --- | --- | --- | --- |
| `BRIDGE_HOT_MINUTES` | `60` | 0–1440 minutes | After a new activity: poll at the fast pace this long, a second one may follow |
| `BRIDGE_ASK_GAP_SECONDS` | `30` | 0–3600 seconds | "Check now" right after a look waits this long (no button hammering) |
| `BRIDGE_POLL_SLACK_SECONDS` | `15` | 0–300 seconds | A look this much earlier than its time still counts as due |
| `BRIDGE_POLL_QUIET_DEFAULT_SECONDS` | `1800` | 60–86400 seconds | Adaptive mode, unusual hours, without `BRIDGE_POLL_QUIET_SECONDS`: the pace |
| `BRIDGE_ADAPTIVE_LEARN_DAYS` | `84` | 7–365 days | Adaptive mode learns your usual activity hours from this many days |
| `BRIDGE_ADAPTIVE_HALF_LIFE_DAYS` | `28` | 1–365 days | ... the weight of an activity halves with this many days of age |
| `BRIDGE_ADAPTIVE_MIN_ACTIVITIES` | `15` | 1–1000 activities | ... and it needs this many before it trusts what it learned (until then: the fast pace) |
| `BRIDGE_ADAPTIVE_FAST_SHARE` | `0.8` | 0.05–1.0 share | ... the hours that hold this share of your activity ends get the fast pace |
| `BRIDGE_ADAPTIVE_MIDDLE_SHARE` | `0.97` | 0.05–1.0 share | ... up to this share the middle pace (must be above the fast share); the rest the quiet pace |

**Activities and wellness**

| Variable | Default | Range | Meaning |
| --- | --- | --- | --- |
| `BRIDGE_ARCHIVE_SETTLE_DAYS` | `2` | 0–30 days | Garmin attaches weather and gear to a fresh activity a little later: asked again within this many days |
| `BRIDGE_INTAKE_SETTLE_HOURS` | `12` | 0–168 hours | Drinks and gear are logged after the ride: Garmin is asked for them again once, this long after the start |
| `BRIDGE_CATCH_UP_DAYS` | `14` | 1–90 days | After a pause the scheduled runs look back to the last run, at most this far |
| `BRIDGE_RETRIES_BY_ID` | `3` | 0–50 activities | Per full run: failed activities that left the scan window and are retried by id |
| `BRIDGE_FINAL_TRIES` | `3` | 1–10 tries | A day with passing endpoint failures counts as final after this many reads (wellness and backup) |
| `BRIDGE_WELLNESS_CHECK_MINUTES` | `10` | 1–1440 minutes | The wellness pre-check of Intervals (costs no Garmin request) at most this often |
| `BRIDGE_WELLNESS_SIGNAL_DAYS` | `2` | 0–30 days | Official sleep seen this recently: wait for it instead of asking Garmin |
| `BRIDGE_GARMIN_NIGHT_DAYS` | `7` | 1–60 days | No night from Garmin for this long: the advice does not wait for one |
| `BRIDGE_FINAL_ANYWAY_HOUR` | `20` | 0–23 hour | A past day is read at this hour the day after, synced or not |
| `BRIDGE_READINESS_RETRIES` | `2` | 0–10 tries | Training readiness: tries when Garmin has not computed it yet |
| `BRIDGE_READINESS_RETRY_MINUTES` | `30` | 1–1440 minutes | Training readiness: minutes between those tries |

**Health checks**

| Variable | Default | Range | Meaning |
| --- | --- | --- | --- |
| `BRIDGE_HEALTH_STALE_RUN_HOURS` | `3` | 1–168 hours | No look or full run for this long (at least) counts as "not running" |
| `BRIDGE_HEALTH_ENDPOINT_ERROR_DAYS` | `3` | 1–30 days | A Garmin endpoint with an error on each of the last this many days is reported |
| `BRIDGE_HEALTH_DROPOUT_BASELINE_DAYS` | `7` | 3–60 days | Dropout check: the days a field must have been there before |
| `BRIDGE_HEALTH_DROPOUT_MIN_BASELINE` | `5` | 1–60 days | Dropout check: in at least this many of those |
| `BRIDGE_HEALTH_DROPOUT_MISSING_DAYS` | `2` | 1–14 days | Dropout check: missing on each of the latest this many days is reported |

**Backup mode**

| Variable | Default | Range | Meaning |
| --- | --- | --- | --- |
| `BRIDGE_BACKUP_FINAL_HOUR` | `12` | 0–23 hour | A day counts as final at this hour of the next day (the watch has synced) |
| `BRIDGE_BACKUP_LATE_READ_DAYS` | `3` | 1–14 days | A day is read once more this many days after it, for a watch that synced late |
| `BRIDGE_BACKUP_RECENT_ACTIVITY_DAYS` | `3` | 1–30 days | Each backup run looks for new activities this many days back |
| `BRIDGE_BACKUP_RECENT_WELLNESS_DAYS` | `7` | 1–30 days | Each backup run considers the wellness of this many past days |
| `BRIDGE_BACKUP_ACCOUNT_DAYS` | `7` | 1–90 days | The account data (profile, devices, zones ...) is archived this often |

`BRIDGE_IN_CONTAINER` is set by the container image itself.

## Known limitations

- **Garmin's endpoints are private and undocumented.** They can change,
  rate-limit or block without notice. The mobile login path answers 429
  for some clients; the web path with MFA works. Keep request delays.
- **Writing streams marks the activity's intervals as edited**
  (`icu_intervals_edited = true`, not resettable through the API).
  Intervals then no longer regenerates that activity's intervals on
  re-analysis. Harmless for an imported activity whose laps exist, but
  permanent. Writing scalar fields does not trigger it.
- **Only definitions the bridge can read safely are used.** Field scripts
  of the forms `activity.X / 60`, `activity.X == 0 ? NaN : activity.X / 36`
  and stream scripts reading one `m.f_<n>` with constant factors are
  understood; anything else is listed as unsupported and left alone.
- **Intervals' "Reprocess file" undoes the enrichment** of that activity: Intervals
  re-reads its own, filtered copy. `enrich <id or link> --apply` (or the web
  page's *Actions → One activity again*) puts Garmin's values back. In upload mode
  Intervals holds the original itself, and reprocessing keeps everything.
- **Manual activities** (no device file) are skipped. **Strava-sourced**
  activities are never touched.
- One Garmin account and one Intervals athlete per data directory.
- Intraday series (stress, heart rate, Body Battery curves) have no place
  in Intervals' one-value-per-day wellness model; they stay in the raw
  archive.

## Troubleshooting

| Symptom | Meaning |
| --- | --- |
| `Garmin login needed` (also as a note in the Intervals calendar) | Run `login` interactively in the same environment the service uses. |
| `429` during `login` | The mobile login path was refused; the web path usually follows. Do not retry in a loop. |
| `Another bridge instance is running (wellness)` | A backfill holds that part; the rest keeps running, and the bridge goes on with it once the backfill is done. For a backfill: another one is still working on that part. `(run)`: a background bridge runs already. |
| Garmin values gone after *Reprocess file* in Intervals | `enrich <the activity's id or link> --apply`. |
| `outcome: unmatched` | The official import has not created the activity yet; the next run looks again. |
| `status` lists a *pending* upload | Upload mode: the request's outcome is unknown. Check Intervals before `reset-pending`. |
| `203/EXEC Permission denied` under systemd | SELinux: the virtualenv must not live under a web document root. |

## Security and privacy

Read [SECURITY.md](SECURITY.md) and [PRIVACY.md](PRIVACY.md). In short: no passwords stored,
tokens and the API key readable by the service user only, the data
directory is personal (routes, health data) and must never be committed or
shared, dry run by default, no deletions – except Garmin's filtered copy in Intervals when you
switch swap mode on with the `upload` method. The bridge talks to Garmin Connect, Intervals.icu, once a day pypi.org for
the update hint, and the mail or ntfy server you set up for alerts – nothing else.

## Legal note

- The bridge signs in to Garmin Connect with **your** credentials and reads **your** data
  through the same private endpoints the Garmin Connect web app uses. Garmin offers no
  official API for personal use, and Garmin's [Terms of Use](https://www.garmin.com/en-US/legal/terms-of-use/)
  prohibit automated access to the site. Using this tool is your decision and your risk; the
  consequence Garmin's terms provide for is suspension of your Garmin account. No such case is
  known for any of the long-standing open-source Garmin clients, but there is no guarantee.
- Keep the load modest. The defaults – one download per new activity, a few wellness reads a
  day, paced backfills – make the bridge behave like a person using the site. Don't shorten the
  pauses to hammer the API; it would hurt everyone using tools like this one.
- Everything the bridge fetches stays on your machine. Nothing leaves it except what you choose
  to write into your own Intervals.icu account.
- In the EU, the Data Act (Regulation (EU) 2023/2854) and the GDPR give you a right to the data
  your device generates; this tool is one way of exercising it for your own use.
- Independent hobby project: not affiliated with, endorsed by or supported by Garmin Ltd. or
  Intervals.icu. Garmin is a trademark of Garmin Ltd.; names are used only to describe
  compatibility. MIT licence: provided as is, without warranty of any kind.

## Acknowledgements

[cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect),
Garmin's [FIT SDK](https://developer.garmin.com/fit/), the
[Intervals.icu API](https://intervals.icu/api/v1/docs), and the
[forum thread](https://forum.intervals.icu/t/garmin-fit-fields-not-coming-in-possible-garmin-is-filtering-data-prior-to-sending-to-intervals-icu/124287)
that documented the problem.

MIT licensed.
