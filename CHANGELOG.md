# Changelog

## 0.11.3 — 2026-10-15

Swap mode puts the original into Garmin's copy itself – the same activity, nothing deleted.

### Added

- **Swap in place** (`BRIDGE_SWAP_METHOD=replace`, the new default): swap mode puts the
  original into Garmin's copy itself, with Intervals' new *replace file*
  (`POST /activity/{id}/file`, thanks David). The activity keeps its id and link, its name,
  description, comments, edited intervals, RPE, gear and tags; nothing is deleted, and
  Intervals computes every field and stream from the complete file. Intervals still lists it
  as Garmin's import. Thanks Lukasz_Pludowski for the question about edited intervals.
- `backfill --scope activities --from DATE --swap` does it for the past (in place, so no
  time limit), `swap <activity> --method replace|upload` for one activity by hand.

### Changed

- **Swap mode replaces by default.** The previous way – upload the original as a new
  activity of your own and delete Garmin's copy – stays as `BRIDGE_SWAP_METHOD=upload` (a
  choice on the web page's settings and in the Home Assistant app).
- **Charts:** a chart the bridge puts on the Fitness page (*Actions → Charts*, `setup-charts`)
  is now a copy of the same chart in Intervals' chart library, as Intervals' own *Copy* makes
  it (`from_id`): the library shows how many use it. Each copy stays your own; a later change
  to the library chart does not reach it.
- The charts' description links the forum thread instead of GitHub – where Intervals users
  find what the bridge is, its guides and answers.
- `setup` and the web page say what "log in" means for the background bridge: it starts by
  itself whenever you log in to this computer (thanks Lukasz_Pludowski in the forum).

### Fixed

- A Gmail app password works as Google shows it, in groups with spaces ("abcd efgh ijkl
  mnop"), and without them. Before, it was refused with the spaces.
- The Gmail hint links Google's app-password page directly: the Security menu shows it only
  once you have one (thanks Lukasz_Pludowski).

## 0.11.2 — 2026-10-14

The Home Assistant app starts again.

### Fixed

- **Home Assistant app stuck in "Starting":** 0.11.1 put the container healthcheck into the
  image, and the app turned it off with `HEALTHCHECK NONE`. Home Assistant's Supervisor holds
  an app whose image has any healthcheck entry in "Starting" until a health result comes – with
  `NONE`, never. The bridge ran fine, but the app could not be stopped or restarted from Home
  Assistant. The image carries no healthcheck now; `compose.yaml`, the Unraid template and the
  Synology guide set it (`status --check` every 5 minutes, as before). With a plain `docker run`:
  `--health-cmd "garmin-intervals-bridge status --check" --health-interval 5m`. Thanks DarKen.

## 0.11.1 — 2026-10-14

The web page works in Firefox, a short outage is no alarm – and a container that says
whether the bridge works.

### Added

- **The container checks itself** every 5 minutes (`status --check`, no request to
  anyone; from 0.11.2 set by the compose file and the templates, not the image): Docker, Portainer, Unraid and Synology show it *unhealthy* when no run came for
  twice the expected time (about an hour; backup mode: twice `BRIDGE_BACKUP_HOURS`). A break
  Intervals asked for, and a Garmin sign-in it waits for, count as healthy – a restart would
  not help there, and the alerts tell you. The Home Assistant app leaves it out (Home
  Assistant watches its apps itself). Thanks @FrancescoCastaldi for the idea.
- The whole test suite runs on Windows and macOS too (thanks @FrancescoCastaldi).
- The package says its type hints count (PEP 561 `py.typed`; thanks @FrancescoCastaldi).

### Fixed

- **The web page in Firefox:** every button and form answered "foreign form". The page
  asked browsers to send no referrer, and with that Firefox (as the web's Fetch standard
  says) sends a form's origin as `null`, which the check took for a foreign site. The page
  now keeps the referrer on its own pages, and a `null` origin counts as "no information";
  the session's token and cookie still have to fit. Thanks Samantha_Lane and No_Cheetah in the forum.
- **Windows:** the web page's "Starts at login" check read the scheduler's answer as UTF-8.
  On a German Windows it comes in the console's code page ("ä" is byte 0x84), which ended in
  a `UnicodeDecodeError` in the console. It is read in that code page now, and an odd byte
  never stops anything. Thanks No_Cheetah.
- **`setup` on Windows:** in the classic PowerShell window Ctrl+V does not paste into the
  hidden API-key prompt but types the control character ^V, so Intervals refused the "key".
  Control characters, line breaks and quotes a paste brings along are dropped now; the prompt
  says to paste with a right-click, and when too little arrived, it says so. Thanks Okocha in
  the forum.
- **A server briefly away:**
  - A failed look for new activities counts for the pace. During an outage the bridge
    tries again at the planned pace (at night every 30 minutes), not every minute.
  - Under systemd, `watch` treats a connection failure, a timeout or a 5xx as a warning,
    without a failed unit or an alert mail; a watch that keeps failing is still reported
    (health: "watch not running" after 3 hours).
  - The log says which server and why, e.g. `intervals.icu: too many 503 error responses`,
    instead of only `RetryError`.

Thanks @FrancescoCastaldi for each of these:

- A setting left empty (`BRIDGE_ACTIVITY_LOOKBACK_DAYS=`) means its default. Before, it
  stopped the bridge.
- A push message's title is one line: a line break in it made the push fail. The ntfy
  address and token may carry stray spaces.
- A FIT file may not declare more than 32 MB.

## 0.11.0 — 2026-10-13

Fewer requests for Intervals.icu, a button to look now – and a second review of the whole
code, every finding fixed.

Upgrading needs no change. systemd users: the `watch` command now decides itself when a look
is due, so the shipped watch timer starts it every minute again (copy it to take that over).
To keep looking every minute: `BRIDGE_POLL_SECONDS=60`.

### Added

- **A slower pace instead of none in quiet hours:** `BRIDGE_POLL_QUIET_SECONDS` – outside
  `BRIDGE_POLL_HOURS` the bridge looks this often instead of not at all (unset: not at all,
  as before). For an hour after a new activity it looks at the full pace again: a second
  session or the cool-down may follow.
- **Adaptive polling (beta):** `BRIDGE_POLL_MODE=adaptive` learns from the archive when your
  activities usually end – per weekday and hour, the last twelve weeks, recent weeks counting
  most – and looks at the full pace only then, every few minutes around those hours, every
  30 minutes at unusual times. It never looks faster than `BRIDGE_POLL_SECONDS` and never
  outside `BRIDGE_POLL_HOURS`; with fewer than 15 activities in the archive it keeps the full
  pace. Its aim: as few requests for Intervals.icu as possible without making you wait.
  Feedback welcome.
- **Check now** on the web page: looks at once, and at the full pace for the hour after.
- The web page shows when the bridge looks and why; its settings page sets the pace, the
  hours, the quiet pace and the adaptive mode. Home Assistant app and Unraid template: the
  new options.

### Changed

- `watch` (the systemd timer's command) keeps to `BRIDGE_POLL_SECONDS` like `run` does,
  5 minutes by default; a `watch` that is not due ends at once, without a request.
- Each FIT file's CRC is checked once instead of three times: about 40 % less CPU per new
  activity, nothing skipped (noticeable on a Raspberry Pi; thanks @Michael.0000).

### Fixed

A second review of the whole code (an independent AI review, each finding checked against
the code and reproduced) found ways a rare failure could cost data. None is known to have
happened; all are closed:

- **Swap mode:**
  - Garmin's copy is deleted only once the athlete's values (RPE, notes, typed fields …)
    read back from the original. Before, a failed transfer still deleted the copy.
  - The comments are asked again right before every delete, and a delete tried again later
    happens only while Intervals still lists both the copy and the original. Before, a retry
    could delete a copy that had got a comment, or one whose original had been deleted by hand.
  - An upload whose answer got lost is never sent again; an original uploaded before is
    taken up instead of uploaded twice.
  - Automatic swaps leave copies alone whose intervals count as edited (`swap <id> --apply`
    still does it, with a note).
  - A typed 0 goes over. Fields Intervals computes from the file keep the original's value
    instead of the copy's filtered one.
  - One swap at a time: the watch, the full runs and `swap` by hand share a lock.
  - A workout not paired again is tried again.
- **Wellness:**
  - A read in which one Garmin endpoint failed keeps what the archive had for it. Before,
    it replaced the archive.
  - A day with such a passing failure is final only after three reads.
  - A write to Intervals that failed is tried again, the evening's corrections included.
    Before, a day written in the morning was never written again.
  - A Health Snapshot check that failed is done again.
- **Backup mode:**
  - The day's original files are asked again after an error, and once more when Garmin said
    "none" before a late watch sync.
  - A read with failed endpoints counts only once they came.
- An empty FIT download is refused (before, it counted as the original and the activity as
  done for good), and the size limit holds for plain FIT downloads too.
- **Kinder when something is wrong:**
  - A full run that keeps failing (Intervals down, say) waits 5, 10, 20 … minutes before
    the next try. Before, it started again every minute, and no poll came in between.
  - When Intervals answers 429, the whole run pauses for as long as it asks, a date in
    `Retry-After` included. Before, the 429 was sometimes booked against a single activity
    or day, and the Intervals client retried after at most 30 seconds.
  - There is no health probe during such a pause.
  - An upload Intervals refused (4xx) is no longer left "pending" for good.
- **Looking for new activities:**
  - Upload mode counts the full run's look at Garmin and makes the hour after an upload
    fast. Since this release's new polling, it had looked half as often again as before.
  - `run` wakes when a look is due, so a 90-second pace means 90 seconds (it meant 120).
  - *Check now* right after a look waits half a minute, so pressing it again and again
    does not hammer Intervals. The status page shows the pace `run` actually uses, and
    says when *Check now* looks only once (outside `BRIDGE_POLL_HOURS`).
  - The pace is measured from the start of each look, not its end.
  - Health no longer reports "watch not running" while `BRIDGE_POLL_HOURS` are closed. A
    slow quiet pace stretches the limit. The status page counts the 30-minute full runs as
    alive.
- An activity whose retry falls after it left the 4-day scan window is retried by its id.
  Before, it stayed failed for good and made the scan run every 30 minutes instead of every
  2 hours.
- Garmin's short activity list falls back to the full one when its 100th activity is on the
  first day asked for, so that day's earlier ones are not missed.
- `health` in backup mode, and `backfill --wellness-files` / `--archive-only`, no longer
  ask for an Intervals key.
- Tests can no longer reach the internet. A few of them had been sending dummy requests
  to intervals.icu unnoticed.
- **The web page:**
  - A request gets 20 seconds. At most 16 are served at once, and further connections are
    closed. A negative or broken length is refused before anything is read.
  - Password guesses take turns, so guessing in parallel is no faster than one at a time.
  - Behind an HTTPS reverse proxy (`X-Forwarded-Proto: https`) the session cookie is
    `Secure`.
- **Settings:**
  - A value with a line break is refused, so a form field can no longer add a setting of
    its own. Values that start and end with a quote, or with a space, are saved quoted and
    read back unchanged.
  - Two saves at once (a double click) take turns and use a fresh temporary file each, so
    no setting is lost.
  - A setting the bridge could not start with (an SMTP port of 65536, say) is not kept, and
    the page says why. Before, it locked out the web page and stopped the bridge from
    starting.
  - `BRIDGE_POLL_HOURS` is read strictly: `6:99` or `24-0` are errors, `22-24` works.
- Log files stay readable by their owner only after a rotation too, in a private folder.
- **Enriching:**
  - A stream script counts only when it does nothing but copy one record field (scaled by
    constants or not). Before, a script with a condition, a value changed before it is
    written, or a second field was read as the plain form, and the stream got wrong values.
  - Where the filtered copy lacks a field's source, only Intervals' `0` placeholder, or the
    value the bridge itself wrote, is replaced. A value typed in by hand stays. Before, any
    value was replaced, again whenever the field definitions changed.
  - A long-running `run` reads the athlete's field definitions again for every full run, so
    a newly added field or stream no longer waits for a restart.
  - The activity whose external id names the Garmin activity wins, wherever it is in the
    list. A look-alike counts only when start and duration agree and it is the only one;
    two recordings of the same session, watch and Edge, are no match.
  - `gap` keeps Intervals' copy for later enrichment only when it is exactly that
    recording's.
  - Streams are not written when nothing can check their alignment and the original starts
    at another second than Intervals' copy.
- The web page asks pypi.org for a new version once a day, as documented (it was every six
  hours while the page was open). The morning wellness check's ten-minute spacing is
  measured on the bridge's own clock.
- Home Assistant app: taking over a copied data folder keeps the app's own form settings,
  whatever the folder brought along. The empty database of an earlier start is set aside
  (`bridge.db.before-takeover`) instead of deleted.

### Documentation

- PRIVACY.md says what an alert carries: push messages carry a headline, e-mails the details
  too. It says what the bridge deletes (swap mode only), and no longer describes webhooks,
  which are not built. README and the Windows/Mac guides no longer promise "no deletions"
  without naming swap mode.
- The field mapping guide explains which existing values are replaced, which stream scripts
  count, and the limit of the Health Snapshot correction: a second HRV or resting-HR source
  whose value equals a snapshot's is put back to the night's too.

## 0.10.3 — 2026-10-13

The Home Assistant app's first real install, fixed.

### Fixed

- Home Assistant app: the web page in the sidebar showed "refused to connect" – it forbade
  every frame, Home Assistant's own included. Behind ingress Home Assistant may frame it now
  (same origin only); everywhere else framing stays forbidden. Thanks @DarKen.
- Home Assistant app: a data folder copied to `share/garmin-bridge` after the app's first
  start was never taken over (that start had left an empty database). It is taken over now
  as long as the app has no Garmin session and no archive of its own – once, never over them.

## 0.10.2 — 2026-10-13

Kinder to Intervals.icu: a poll every 5 minutes, and a break when it asks for one.

Upgrading needs no change. To keep polling every minute: `BRIDGE_POLL_SECONDS=60` (please
only if you really need it). systemd users: the shipped watch timer now runs every 5 minutes;
copy it again to take that over.

### Changed

- **A poll every 5 minutes by default** instead of every minute (`run`, autostart,
  containers, the Home Assistant app, the shipped systemd timer) – Intervals.icu asked
  self-hosted tools not to poll every minute. New activities arrive within minutes.
- **A break when Intervals asks for one:** an HTTP 429 pauses the polls and full runs for its
  `Retry-After`, at least 15 minutes; the systemd timers skip their runs meanwhile.
- The container image's default command is `run` (the bridge keeps running, a dry run until
  the command says `--apply`) instead of a one-time dry `sync`. Every template set `run`
  already.

### Documentation

- A current [roadmap](docs/ROADMAP.md); the rules contributions keep are in CONTRIBUTING.md
  (the planning notes of the first day, `docs/PLAN.md`, are retired to the git history).
- The README lists every command, `run`, `reset-pending` and `snapshot-account` included.

## 0.10.1 — 2026-10-13

The bridge as a Home Assistant app.

### Added

- **Home Assistant app** (thanks @DarKen): add this repository to Home Assistant's app store;
  settings in a form (mode, swap, advice, alerts, poll hours …), the web page with the Garmin
  sign-in in the sidebar through ingress – only Home Assistant's proxy is let in, its users
  are signed in already, every link lives under the ingress path. A data folder copied to
  `share/garmin-bridge` is taken over on the first start. `docs/HOME_ASSISTANT.md`.
- The settings of an app's form (`/data/options.json`) cover mode, swap, poll hours and
  interval and the backup interval too.

## 0.10.0 — 2026-10-13

Backup mode – your Garmin data in your own folder, no Intervals needed – a fuller wellness
backfill, and a request counter that no longer scares anyone.

### Added

- **Backup mode** (`BRIDGE_MODE=backup`, `run --mode backup`, a choice in `setup`): your
  Garmin data in your own folder, no Intervals needed – every activity's original file
  with Garmin's summary and extras, each day's wellness (all endpoints and the watch's own
  wellness files) once it is final and again three days later, the account weekly; a run
  every 12 hours (`BRIDGE_BACKUP_HOURS`). `backup --from DATE` fills in the past, next to
  a running bridge. `docs/BACKUP.md`.

### Changed

- A wellness backfill also reads endurance & hill score and race predictions by default
  (15 instead of 12 requests per day; `--endpoints essential` for the old set). Thanks
  @Jetty. For days backfilled before: `--endpoints endurance_score,hill_score,race_predictions
  --force-wellness`.
- Swap mode's wording says plainly that the deleted copy is the one in Intervals – nothing
  on Garmin Connect is ever touched (thanks @DarKen).

### Fixed

- The daily summary ("last 24 h … Garmin requests", web page and health log) counted far
  too many Garmin requests for `run`: every minute's poll recorded the whole request count of
  the last full run again (it shares that Garmin session) – 50 real requests showed up as
  2000. Only the count was wrong, not the requests. Thanks @Jeroen_Doesburg.

## 0.9.1 — 2026-10-12

Poll less, or only in certain hours.

### Added

- `BRIDGE_POLL_SECONDS` and `BRIDGE_POLL_HOURS` (thanks @Michael.0000): poll less often,
  or only in certain hours (`10-18`, `6-9,16-22`, `22-6`). Outside them `run` skips its
  polls and `watch` does nothing; the half-hourly full runs go on, so activities and last
  night's wellness still arrive.

## 0.9.0 — 2026-10-12

Swap mode: Garmin's filtered copy out, the original in – so Intervals holds the complete
file and "Reprocess file" keeps everything. Off until you switch it on.

### Added

- **Swap mode** (`BRIDGE_SWAP=true`, off by default): the official import stays on as
  the trigger; within a minute the bridge uploads the device's original, carries name,
  description, paired workout, type, RPE, feel, tags, gear and flags over, and deletes
  Garmin's filtered copy. Intervals then holds the complete file – *Reprocess file*
  keeps everything, and it is your own upload, not Garmin-sourced data. Guarded: only
  Garmin's import, within `BRIDGE_SWAP_HOURS` (48), never with comments or hand-edited
  intervals, upload first and delete last, a failed delete is tried again.
  `swap <id or link> [--apply]` for one activity by hand. Values of custom fields
  without a script (the logged intake, what you typed) go over too. `setup` asks, the
  web page has a switch.

### Changed

- `enrich <id> --apply` (and the web page's *One activity again*) also puts the logged
  intake back (`GarminFluidIntake`, `GarminCaloriesConsumed`, from the archived summary):
  Intervals' *Reprocess file* empties every field the file does not fill – in both modes.

## 0.8.1 — 2026-10-12

One activity again, the easy way – thanks @DarKen.

### Changed

- `enrich` needs only the Intervals activity: `enrich <id or link> --apply` (id
  `i12345678` or the activity's link). It compares with what Intervals holds now (also
  when the bridge had finished with it), rewrites its own streams, and works next to a
  running bridge
  (`run`, a container) instead of stopping at "Another bridge instance is running".
  The use: Intervals' *Reprocess file* re-reads its own filtered copy and drops what
  the bridge added; a new custom field or stream for an older activity.
- The web page's *Actions* do the same ("One activity again"), showing first what the
  activity gets.
- Upload mode puts the Garmin gear on the uploaded originals too (it did only in enrich
  mode).

### Documentation

- *Reprocess file* and how to undo its effect (README, guides); Synology on older DSM
  with the Docker package instead of Container Manager; switching an installation from
  enrich to upload mode, step by step (`deploy/README.md`).

## 0.8.0 — 2026-10-12

Set up by answering questions, run in the background without a window, sign in to
Garmin in your browser – also on a NAS – and get an e-mail or a push message when the
bridge needs you.

Upgrading needs no change: systemd units and containers keep working as before.
Windows and Mac users who followed the earlier guides can run `setup` once in their
bridge folder; it takes over settings, Garmin session and data and replaces the
hand-made logon task / launch file. Container users: the image now serves an optional
web page (`BRIDGE_UI_PORT`, `BRIDGE_UI_PASSWORD`).

### Added

- `setup`: interactive setup – Intervals API key (checked, never shown), time zone,
  Garmin sign-in with MFA, gear, ImReady4 (only when its field exists), the trend
  advice, charts, a trial run that writes nothing, then the start in the background.
  Saves after every step; run it again to change anything.
- `autostart [--status | --remove | --dry-run]`: runs `run --apply` at every login –
  a Task Scheduler task for your own account on Windows (`pythonw.exe`, no window, no
  admin rights, no 72-hour limit, also on battery), a LaunchAgent on the Mac, a
  systemd user service on Linux.
- Settings from `~/GarminBridge/.env` (what `setup` writes) when there is no `.env`
  in the working folder: commands work from any terminal. Global `--env-file PATH`.
  Relative paths in a settings file count from its folder.
- `BRIDGE_LOG_FILE`: a log file, 5 MB × 3; also catches a broken setting.
- Update hint: once a day the health run asks pypi.org for the newest version and
  logs how to update (`BRIDGE_UPDATE_CHECK=false` turns it off).
- When Garmin's session ends, a bridge running with `--apply` puts one note in the
  Intervals calendar ("Garmin bridge: Garmin login needed"), once per episode, never
  deleted by the bridge (`BRIDGE_INTERVALS_NOTES=false` turns it off).
- Alerts by e-mail (through your own mailbox, servers of the common providers built in)
  and/or push message (ntfy): when Garmin wants a new sign-in (once per episode) and
  when the daily health check finds something that needs a person (at most once a day).
  `setup` and the web page set them up with a test message; `notify-test`.
- `ui` and `BRIDGE_UI_PORT`: the bridge's web page – status in plain words, Garmin
  sign-in with MFA (a waiting bridge goes on within a minute), settings, gear and chart
  plans with a confirm button, today's training advice, the log. Local with a random
  key in the link; on the network only with `BRIDGE_UI_PASSWORD`. Standard library only.
- Settings from a Home Assistant add-on's `/data/options.json` (`BRIDGE_OPTIONS_FILE`)
  when there is no settings file.
- Unraid container template (`deploy/unraid/`, `docs/UNRAID.md`).
- A data folder the container cannot write to is reported in words, with the fix.

### Changed

- `compose.yaml` keeps the bridge running (`run`, a dry run until you make it
  `run --apply`; restarts with the machine). Cron users schedule `compose run` as before.
- Synology: a project file to paste instead of clicking a container together, Garmin
  sign-in on the web page instead of the container terminal (`docs/SYNOLOGY.md`);
  Windows and Mac guides are three steps now.
- The log file is readable by its owner only.

## 0.7.1 — 2026-10-12

New shoes in Garmin Connect reach Intervals by themselves.

If you set up gear with `gear --create --apply` before this version, run it once more
(`gear --create --include-retired --apply`): that is what lets the scheduled runs create
new gear from now on.

### Changed

- `gear --create --apply` is remembered: from then on the scheduled runs also
  create gear that appears in Garmin Connect later (a new pair of shoes) and
  put it on its activities; `gear --apply --no-create` stops that. A later
  `gear --apply` without `--create` keeps it.

### Documentation

- ImReady4's advice without a Garmin watch: `imready4` needs Intervals only,
  scheduled once a morning (`docs/IMREADY4.md`).

## 0.7.0 — 2026-10-12

Strava-first activities enriched at once, a calmer second training advice to compare
with ImReady4's, and guides for Synology and a Windows that blocks unknown programs.

Upgrading needs no change to the configuration; the trend advice stays off until
`BRIDGE_HRVTREND_ENABLED=true`. Windows users: start the bridge with
`py -m garmin_intervals_bridge …` (also in `run-bridge.cmd`).

### Added

- **Experimental HRV trend advice** (off by default, `BRIDGE_HRVTREND_ENABLED`):
  a second advice in a field of its own, `HRVTrendAdvice` (Rest / LIT /
  Planned), from the 7-day ln(rMSSD) mean against a 60-day normal range (±0.5
  SD), the night's average heart rate and a two-bad-nights rule – the way the
  HRV-guided training studies judged the trend (Plews, Kiviniemi, Vesterinen,
  Javaloyes, Nuuttila). `HRVTrendCV` holds the week's coefficient of
  variation. The bridge creates both fields; `TrainingAdvice` is never
  touched. `hrvtrend` previews, compares with ImReady4 (`--compare`) and fills
  the past (`--apply`). See `docs/HRV_TREND.md`.

### Changed

- ImReady4 and the trend advice share their write and once-a-morning rules
  (`advice.py`); ImReady4's behaviour is unchanged.
- An activity the watch first saw as Strava's copy and that Intervals then
  replaced with Garmin's is enriched within the minute, no longer only by the
  next full scan (up to two hours later).
- The `gear` plan names retired Garmin gear that `--create` leaves out, and
  how to add it (`--include-retired`).

### Documentation

- A step-by-step guide for Synology NAS (Container Manager), after @DarKen's
  write-up: the container, the Garmin login in its terminal while it runs, gear,
  the one-off history backfill, updating (`docs/SYNOLOGY.md`).
- The Windows guide starts the bridge with `py -m garmin_intervals_bridge`
  everywhere, `run-bridge.cmd` included: Windows' Smart App Control blocks the
  unsigned `garmin-intervals-bridge.exe` launcher pip creates (issue #5);
  Python itself is signed.

### Fixed

- Intervals' rate limit (HTTP 429) no longer ends a wellness write: the same
  values are sent again after the wait Intervals asks for, and filling a range
  of days (`imready4` / `hrvtrend --apply`) pauses briefly between writes.

## 0.6.0 — 2026-10-12

ImReady4's training advice, written for you each morning – no more MATLAB Mobile step.

Upgrading changes nothing until you switch it on: `BRIDGE_IMREADY4_ENABLED=true` (systemd env file,
`-e` for a container, `.env` on Windows and the Mac), with the `TrainingAdvice` wellness field ImReady4
uses. `garmin-intervals-bridge imready4` shows today's advice first; `imready4 --from DATE --compare`
compares it with the values the field already holds.

### Added

- **ImReady4 training advice, without the morning MATLAB step** (optional, off
  by default): with `BRIDGE_IMREADY4_ENABLED=true` the bridge calculates
  ImReady4's advice (Rest / LIT / Planned / HIT) once last night's HRV and
  resting HR are in Intervals and writes it into the existing
  `TrainingAdvice` field – an independent implementation of the script's
  scoring (4.43): 20·ln(rMSSD) and resting HR as z-scores against the last 30
  days with the population SD, ImReady4's zones. Intervals requests only;
  only an empty field is written, never a locked day, nothing without the
  night's values or 14 measured days; read back and done once a day.
  `imready4` previews a day, compares a range with the values already there
  and fills days without advice (`--apply`, `--overwrite`). Agrees with the
  script's reference results on 30 of 30 days and with 1,196 of 1,225 days on
  the reference account. See `docs/IMREADY4.md`.

## 0.5.2 — 2026-10-12

Gear picked in the app after the run, and the version in the log.

### Changed

- `run` names its version in the first log line (`garmin-intervals-bridge
  <version> running …`), so a container's log shows which image is running.
- Gear picked in the Garmin Connect app after the run reaches Intervals: the
  activity's gear is read once more half a day later, together with the
  logged fluid and food (one Garmin request per activity), and the gear sync
  takes it from there. Gear chosen on the watch arrived already.

## 0.5.1 — 2026-10-12

Windows works again, and the day boundary is your own.

### Fixed

- Windows: every command stopped at once with `ZoneInfoNotFoundError: No time
  zone found with key Europe/Vienna` – Windows has no time zone database Python
  can read. The bridge now brings one (`tzdata`).

### Changed

- Without `BRIDGE_TIMEZONE` the day boundary is this computer's own time zone
  (via `tzlocal`), no longer Europe/Vienna for everyone. **Containers** have no
  time zone of their own: set `BRIDGE_TIMEZONE` there (e.g. `Europe/London`),
  otherwise the bridge uses UTC – it says so at the start of `run` and in the
  daily health digest.

## 0.5.0 — 2026-10-11

Your Garmin gear on the Intervals activities.

Upgrading needs no change to the configuration. `gear` is a separate command:
run it once without `--apply` to see the plan, then with `--apply` (and
`--create` if your gear exists only in Garmin Connect); from then on the
scheduled runs keep the last week in line.

### Added

- **Gear:** `gear` puts Garmin's gear (bikes, shoes, skis …) on the Intervals
  activities, from the local archive – no Garmin request, nothing removed,
  nothing written to Garmin. Gaps are filled; where both sides differ,
  `--prefer intervals` (default) keeps Intervals' assignment (its auto-assign
  rules or your corrections), `--prefer evidence` takes Garmin's only with
  proof (the gear not bought yet or retired on that day, or the bike's own
  sensors – Di2, power meter by serial number – missing from the recording),
  `--prefer garmin` always takes Garmin's. Gear is paired by name or `--map`;
  missing gear is created with `--create`. Runs next to a running bridge
  (`docker exec`).
- **Gear totals:** Intervals computes a gear's recount but does not store it,
  so totals that drifted (reassignments by hand) stayed off. After writing,
  the bridge stores the recount for the touched gear and their components;
  `--recalc` for all paired gear. The plan lists activities that carry gear
  from before its purchase date (Intervals does not count those).

## 0.4.0 — 2026-10-11

More of the morning, what you drank and ate per activity, quicker recovery
when Garmin isn't ready right after a sync, and a guide for the Mac.

Upgrading needs no change to the configuration; the new activity fields are
created by the bridge. To fill fluid and food for past activities from the
local archive (no Garmin requests): `backfill --scope activities --from
2026-01-01 --intake --apply`.

### Changed

- **More of the morning.** Recovery time, acute load, endurance and hill
  scores, cycling VO₂max, fitness age and race predictions now arrive with the
  morning read, as Garmin reports them at wake-up, instead of the day after;
  the day's final read replaces them with the evening's values. Five more
  endpoints in the morning read: about 80 Garmin requests a day instead of 75.
- `GarminSleepSpO2Avg` and `GarminSleepRespirationAvg` are written in the
  default profile again (an AI coach reading Garmin's names finds them).
- A failed activity is retried after 10 minutes the first time, then after
  an hour, 6 hours, a day and a week (it was an hour first). Right after the
  watch syncs, Garmin sometimes answers with a server error for an activity
  it serves fine minutes later.
- Warnings show the HTTP status next to the error type, e.g.
  `failed with GarminConnectConnectionError (HTTP 503)`.

### Added

- **Fluid and food per activity:** what you log in Garmin (`waterConsumed`
  in ml, `caloriesConsumed` in kcal) goes into the activity fields
  `GarminFluidIntake` and `GarminCaloriesConsumed`, created by the bridge.
  The summary is read once more half a day after the activity (one request
  per activity); `backfill --scope activities --from DATE --intake` fills
  the past from the archive without asking Garmin.

### Fixed

- `health` no longer alerts "weight, bodyFat stopped arriving" after two days
  without stepping on the scale. The dropout check now looks only at finished
  days (the morning's file for today has no weigh-in yet) and only at what the
  device measures by itself, not at what you log or start (weight, food,
  drink, Health Snapshots).

### Documentation

- A step-by-step guide for the Mac (`docs/MAC.md`), like the Windows one:
  Python from python.org, the bridge in its own folder, and a LaunchAgent
  that keeps it running in the background after login.
- README: "Use it with an AI coach (MCP)" with the sister project's install
  channels (PyPI via `uvx`, Docker Hub, Claude Desktop bundle, MCP Registry).
- A project icon in the style of the sister project's: the Futureweb Intervals
  MCP's bars and endpoints, here as a bridge deck between two endpoints carried
  by the bars as piers (`docs/assets/icon-bridge*`, shown above the README).

## 0.3.1 — 2026-10-10

Easier diagnosis next to a running container.

### Added

- `--version`.

### Changed

- `gap` takes no lock: it only reads (and caches the two files it compares),
  so it can diagnose an activity next to a running `run` container.

## 0.3.0 — 2026-10-10

The night's values within minutes of the morning watch sync, a quarter of the
Garmin requests, and a mirror that now includes what the watch itself
recorded: the original wellness files of every day, Health Snapshots among
them, which Intervals now shows too.

Upgrading: systemd users add `--activity-interval 120` to the 30-minute
unit's `ExecStart` (the copy in `deploy/systemd/` has it). The first run
takes over the new schedule on its own. To mirror the past once:
`backfill --scope wellness --from 2020-01-01 --wellness-files --pause 3`,
`backfill --scope activities --from 2020-01-01 --archive-only --pause 3`
(adds Garmin's splits) and `snapshot-account --history-from 2020-01-01`.
If Intervals' Garmin settings import SpO2, untick it: the official
integration only ever delivers it from Health Snapshots.

### Changed

- **Wellness is read on evidence, not on a clock.** Last night's values
  (sleep, SpO₂, respiration, sleeping HR, HRV detail, sleep stages, Training
  Readiness, skin temperature) reach Intervals within about ten minutes of
  the morning watch sync instead of up to four hours later. The watch checks
  Intervals' record for today every ten minutes, which costs no Garmin
  request, and reads four Garmin endpoints once the official integration has
  delivered the sleep; Training Readiness gets two more tries if it lags. A
  finished day is read in full once the device has synced after it ended
  (proven by Garmin's last-sync time), so its totals are complete; a day
  without any sync is read anyway at 20:00 the day after. Without the
  official wellness sync, Garmin is checked at a few fixed morning times.
- The scheduled runs ask Garmin far less: no more four-hourly re-reads of
  today and yesterday, no re-read of the day before yesterday, no login on
  runs with nothing to do, and in enrich mode the Garmin activity list is
  scanned every two hours (`sync --activity-interval`, also in `run`); the
  watch enriches new activities in between. About 75 requests on a normal
  day instead of about 310.
- Manual container builds move `latest` only when asked; a release always
  does. The tag of a manual build is validated before it reaches the shell.

### Added

- **The device's original wellness files.** Every finished day's ZIP of the
  watch's own wellness FIT files is mirrored to `wellness-files/<day>.zip`
  with its final read (one request a day): all-day heart rate, respiration
  per minute, stress, SpO₂ readings, overnight HRV, sleep, skin temperature,
  and Health Snapshots, which exist nowhere else. An index next to it lists
  the files (each CRC-checked) and summarises every Health Snapshot (heart
  rate, RMSSD and SDRR, respiration, SpO₂, stress). `backfill --scope
  wellness --wellness-files` fetches the past.
- Garmin's own splits of every activity (typed splits and split summaries)
  in the activity extras; `backfill --archive-only` adds them to older
  archive entries without fetching anything else again.
- **Health Snapshots in Intervals**: the day's first snapshot (heart rate,
  RMSSD, SDRR, respiration, SpO₂, stress) in `GarminSnapshot…` fields with
  a new chart. Where the official integration writes a snapshot into
  resting HR, HRV or SpO₂, the night's value is put back, on the same day
  (one read of today's files when Intervals' HRV moves away from the
  night's); that is the only case besides a filtered file in which the
  bridge replaces a value.
- More of the night: lowest SpO₂, lowest and highest respiration, Body
  Battery change during sleep and Garmin's HRV baseline band, all from the
  archive (the HRV and SpO₂ charts show them).
- The container image is also on Docker Hub,
  `futurewebat/garmin-intervals-bridge`, for NAS container managers that
  only know Docker Hub.
- The account snapshot also keeps goals, gear defaults, training plan
  details, the calendar of scheduled workouts, Garmin's FTP (latest and
  daily history, cycling and running), running tolerance and the activity
  type table; `snapshot-account --history-from DATE` reaches back once.

### Fixed

- The regular enrich and upload runs now archive Garmin's summary, weather,
  gear and exercise sets of every activity they handle. Before, only
  `backfill --archive-only` did, so a mirror built once drifted as new
  activities came in (the recording was kept, the rest was not). Extras that
  Garmin has not attached yet to a fresh activity are asked for again on the
  next run instead of being archived as empty.
- Exercise sets are also fetched when the activity comes from the detail
  endpoint (`activityTypeDTO`), not only from the list.

### Documentation

- README links the sister project, the
  [Futureweb Intervals MCP](https://github.com/futureweb/intervals-mcp-server):
  it lets ChatGPT, Claude and other MCP clients read and analyse every field
  and stream the bridge writes. A table maps the bridge's data to the MCP
  tools that read it.

## 0.2.1 — 2026-10-09

The day Garmin's new partner terms surfaced: Intervals expects to have to
block Garmin-sourced data in its API; files an athlete uploads are expected
to stay accessible. This release makes the bridge's upload mode the
first-class answer and completes the local mirror.

### Added

- **Upload mode, first-class and live-verified.** `run --mode upload` polls
  Garmin every ten minutes and uploads the original of every new activity
  as the athlete's own file; `backfill --scope activities --mode upload`
  does the same for the past; a 10-minute upload timer in `deploy/`.
  Intervals computes every custom field and stream from the complete file
  itself, nothing is flagged as edited.
- A complete local mirror: eight more daily endpoints in the archive
  (all-day heart rate, intraday steps and floors, Body Battery and all-day
  events, four-week load balance, resting HR course, daily training status),
  `--endpoints` takes a list of keys to add to days fetched earlier,
  `backfill --scope activities --archive-only` downloads every original FIT
  plus Garmin's summary, weather, gear and exercise sets without touching
  Intervals, and `snapshot-account` archives profile, settings, devices,
  zones, gear, personal records, badges, workouts and training plans.
- Container image on ghcr.io (amd64 + arm64), built by GitHub Actions on
  every release; compose pulls it.
- Monthly `snapshot-account` timer in `deploy/`.
- README: legal note (your account, your data, Garmin's terms, trademarks),
  how login works in the container, screenshots.

### Changed

- Readiness: the score is the morning's, recovery time and acute load are
  the day's last reading (a hard session shows its recovery on its own day);
  `GarminRecoveryTimeHours` backs the chart; `backfill --rewrite` replaces
  the bridge's own `Garmin…` values from the archive after such a correction.
- VO₂max is written once the day is over, weigh-ins the same day, an
  unlogged hydration day stays empty, floors are whole numbers.
- Weight in the charts as a 7-day average; daily weigh-ins as dots.

### Fixed

- Chained FIT files (two complete files back to back in one Garmin
  original) are accepted and decoded together.
- A 4xx answer from a Garmin endpoint that did not exist for an old date no
  longer counts as an outage; a port number in an error message is no longer
  read as an HTTP status.
- The scheduled sync does the free scope while a backfill holds the other,
  instead of failing (and alerting).

## 0.2.0 — 2026-10-08

First version used in anger. Everything below was verified against a live
Garmin and Intervals.icu account; see `docs/PLAN.md` (in the git history) for the evidence.

### Changed

- **Enrich instead of upload.** The default mode keeps the official Garmin
  import and adds to the imported activity what its filtered file lacks,
  via `PUT /activity/{id}` and `PUT /activity/{id}/streams`. Intervals
  de-duplicates uploads by file hash, so the v0.1 upload created a
  duplicate whenever the official import was on. Upload remains available
  as `--mode upload` for accounts with the import switched off.
- **Field definitions come from the athlete's own custom items**
  (`fit_session_field`, `fit_record_field`, single-field stream scripts,
  first-import conversions). Nothing Garmin-specific is hard-coded.
- **`fitparse` replaced by Garmin's official FIT SDK.** CRC is verified;
  unknown messages and fields keep their numeric IDs with sample values,
  which is where the stripped data lives.
- Custom wellness fields are written as **top-level keys**; v0.1.1 used a
  nested `customFields` object that the API does not have, so every custom
  write would have been ignored.
- Activity matching recognises the official sync's bare Garmin ID, manual
  uploads (`<id>_ACTIVITY.fit`) and the bridge's own `garmin:<id>`.

### Added

- `gap`: original vs. the copy Intervals holds, per activity.
- `enrich`, `watch` (one-minute poll, Garmin only on new activities),
  `backfill` (paced, resumable), `--refresh-own-streams`.
- Per-activity failure isolation with exponential backoff; a Garmin-wide
  block aborts the run without blaming an activity; uploads with unknown
  outcome stay pending.
- Native wellness fields `spO2`, `respiration`, `avgSleepingHR`,
  `floorsClimbed`, `weight`, `bodyFat`, and `kcalConsumed`,
  `carbohydrates`, `protein`, `fatTotal` from Garmin's nutrition log.
  `BRIDGE_WELLNESS_PROFILE` (`recommended` / `all`).
- Per-scope instance locks, so a long wellness backfill does not stall the
  activity watcher.
- Hardened systemd units (`deploy/`), container image smoke-tested with
  Podman, CI with ruff, pip-audit and pytest.

- `setup-charts`: nineteen private fitness charts for the synced values, using
  Intervals' real chart field ids (read from its app bundle), unique item
  indexes (otherwise the chart picker hides them), and completion of the
  bridge's own charts when fields appear later.
- Past days are re-read once after local midnight so finished totals do not
  wait for the throttle; default throttle 4 h.
- The watcher logs in to Garmin lazily: a poll that finds nothing new makes
  no Garmin request at all.

- `health`: daily probe of both services with a configurable stale window
  and alert mail; failure alerts for the timers (`deploy/`).
- `run`: the three timers in one long-running process, for Windows and
  anything without systemd; `.env` file support; file locks on Windows;
  `python -m garmin_intervals_bridge`. Step-by-step Windows guide.

### Fixed after the code review

- A Garmin outage or rate limit at login time was reported as "login
  needed" and, in the watcher, booked against the activity being processed.
  It now ends the run as a Garmin block, and `health` tells the two apart.
- The library's own retries (4 attempts per endpoint) are off; a day whose
  endpoints fail three times in a row aborts the run instead of trying the
  remaining twenty.
- `watch` exits 0 when the sync or a backfill holds the activities lock; it
  used to fail, which fired the alert unit and used up its throttle.
- Dry runs now count as Garmin reads for the throttle, and the first
  `--apply` maps the archived snapshot instead of fetching again.
- Yesterday is re-read once after midnight and then every second refresh
  period, older days once after midnight; refreshes within a day read only
  the measurement endpoints. About 160 instead of 450 wellness requests a day.
- `backfill --scope wellness --from-archive` completes the past from the
  local archive after the mapping gained fields, without Garmin requests.
- An activity that failed once and then succeeded no longer stays "failed"
  in `status` and `health`.
- Intervals `Retry-After` is capped at 30 s; custom items are read once per
  process; Intervals activity ids are validated before they enter a URL.
- Matching: a bare numeric `external_id` counts only for `GARMIN_CONNECT`
  activities; Garmin's timer duration is compared with moving and elapsed
  time; numeric-string select options are understood.
- The alert unit template was committed empty; `deploy/` now ships it with
  the install step for `gib-alert`.
- Streams are only written when a stream both sides have (heart rate, else
  cadence) lines up point by point after the same alignment; a shifted
  origin is caught instead of written.
- Activities enriched earlier are revisited when the athlete's field
  definitions change; the partner copy is re-read when the Intervals
  activity was replaced.
- Today's resting HR is left to tomorrow (Garmin revises it during the
  day); a short activity window costs one Garmin request instead of two;
  the enrich candidate list is fetched with a field list; FIT downloads are
  capped at 32 MB.

### Known limitations

- Writing streams sets `icu_intervals_edited` on the activity; the API
  does not reset it.
- Garmin's endpoints are private; the mobile login path may answer 429.

## 0.1.1 — developer preview

Original-FIT download and archive, upload mode with pending state,
wellness mapping, Docker, synthetic tests. Never live-tested.
