# Running the bridge on a Synology NAS (Container Manager)

A NAS is a good home for the bridge: it is always on, so last night's values
arrive minutes after your morning sync even when your computer sleeps. This
guide builds on **@DarKen's** write-up from the
[forum thread](https://forum.intervals.icu/t/garmin-intervals-bridge-your-complete-garmin-fit-files-and-wellness-data-in-intervals-icu-with-or-without-the-official-garmin-integration/132863/23) –
thanks! Menu names are those of Container Manager (DSM 7.2); older versions
call some of them slightly differently. The image is built for 64-bit models
(Intel/AMD and ARM64); older 32-bit ARM models cannot run it.

## 1. A folder for the bridge

In *File Station*, open the shared folder `docker` (Container Manager made
it) and create a folder **`garmin-bridge`**, and inside it a folder
**`data`**. Right-click `data` → *Properties* → *Permission* → *Create*:
user or group **Everyone**, **Read & Write**, and tick *Apply to this
folder, sub-folders and files*. (The bridge runs as its own user inside the
container and has to write there: the Garmin session, the original FIT
files, the daily archive. Keep the folder out of shares other people use.)

## 2. The project

*Container Manager → Project → Create*:

- **Project name:** `garmin-bridge`
- **Path:** the folder from step 1, `/docker/garmin-bridge`
- **Source:** *Create docker-compose.yml*, and paste:

```yaml
services:
  bridge:
    image: futurewebat/garmin-intervals-bridge:latest
    container_name: garmin-intervals-bridge
    pull_policy: always              # every Build fetches the newest version
    command: ["run"]                 # a dry run; step 5 changes it to ["run", "--apply"]
    environment:
      INTERVALS_API_KEY: "paste-your-key-here"   # Intervals.icu: Settings, Developer Settings, API key
      BRIDGE_TIMEZONE: "Europe/London"           # yours: which date last night's sleep belongs to
      BRIDGE_UI_PORT: "8765"                     # the bridge's web page
      BRIDGE_UI_PASSWORD: "change-me-please"     # your own password for that page, 8+ characters
      # BRIDGE_IMREADY4_ENABLED: "true"          # ImReady4's training advice, see docs/IMREADY4.md
      # BRIDGE_SWAP: "true"                      # the original into Garmin's filtered copy (README: Modes)
      # BRIDGE_POLL_HOURS: "6-22"                # look for new activities only then (README: How often it looks)
      # BRIDGE_POLL_MODE: "adaptive"             # beta: look often only when your activities usually end
      # BRIDGE_NTFY_URL: "https://ntfy.sh/a-long-name-only-you-know"   # push message when it needs you
      # BRIDGE_ALERT_EMAIL: "you@gmail.com"      # e-mail when it needs you, with
      # BRIDGE_SMTP_PASSWORD: "app password"     # the password to send from that mailbox
    ports:
      - "8765:8765"
    volumes:
      - ./data:/data
    restart: unless-stopped
    init: true
    healthcheck:                     # Container Manager shows whether the bridge still works
      test: ["CMD", "garmin-intervals-bridge", "status", "--check"]
      interval: 5m
      timeout: 30s
      start_period: 2m
      retries: 3
```

Change three values in quotes: your **API key**, your **time zone**
(Region/City, e.g. `Europe/Dublin`, `America/New_York`) and a **password**
of your own for the bridge's web page (8 characters or more; the bridge
refuses the placeholder). *Next*, skip the web portal settings, *Done*:
Container Manager downloads the image and starts the bridge.

Under *Container → garmin-intervals-bridge → Log* it begins with
`garmin-intervals-bridge <version> running …` and the day boundary; then it
reports that a Garmin login is needed and waits.

## 3. Sign in to Garmin once

Open the bridge's web page in your browser: **`http://<your NAS>:8765`**
(the address you use for DSM, with `:8765` instead of DSM's port), sign in
with the password from step 2, then **Garmin** → your Garmin Connect e-mail
and password → *Sign in*, and the code if Garmin sends one. The password is
used once and not stored; the session lands in your `data` folder and
renews itself for about a year, and the bridge starts working within a
minute. If Garmin answers "429", wait an hour – Garmin rate-limits
sign-ins.

The page is plain http: use it at home, or put it behind DSM's reverse proxy
(*Control Panel → Login Portal → Advanced → Reverse Proxy*) for https. It
also shows what the bridge is doing (*Status*), sets up gear and charts
(*Actions*) and shows the log.

Without the web page (no `BRIDGE_UI_…` lines and no `ports`): *Container →
garmin-intervals-bridge → Action → Open terminal* → *Create*, and type
`garmin-intervals-bridge login`.

**When the bridge needs you** – Garmin wants a new sign-in, or it has been
failing for a day – it puts a note in your Intervals calendar. For a push
message on your phone too, install the free app **ntfy**, subscribe to a
topic name only you know, and remove the `#` in front of `BRIDGE_NTFY_URL`
with that name at its end. For an e-mail, the two lines below it: your
address and the password to send from that mailbox (Gmail, iCloud, Yahoo: an
app password; GMX, web.de: allow POP3/IMAP access in the mailbox settings).
The web page's *Settings* has a button that sends a test message.

## 4. See what it would do

The container's *Log* (or the web page's *Log*) now lists what the dry run
would enrich and write.

## 5. Let it write

*Project → garmin-bridge → YAML configurations → Edit* (in some versions:
*Action → Edit*): change

```yaml
    command: ["run"]
```

to

```yaml
    command: ["run", "--apply"]
```

*Save*, and let Container Manager rebuild the project (or *Action → Stop*,
then *Build*). From now on it polls Intervals every 5 minutes, does a full run
every 30 minutes and writes a health line once a day.

## 6. Your gear (optional)

On the web page: *Actions → Show the gear plan*, then *Apply this plan*. Or
in the container's terminal:

```bash
garmin-intervals-bridge gear                                        # the plan
garmin-intervals-bridge gear --create --include-retired --apply     # create your Garmin gear in Intervals, fill it in
```

`--include-retired` adds your retired shoes too (retired, with their dates). No
restart needed: the running bridge keeps new activities in line, and a new
pair you add in Garmin Connect later is created in Intervals by itself (because
`--create` was used once). All of that lives in your data folder, so a rebuilt
project just carries on. Details: [README – Gear](../README.md#gear).

## 7. The past (optional, once)

The bridge knows the gear and the original files of the activities it has
archived – those since you started it. To mirror your history once, the
running bridge has to stop (both would use the same data):

1. *Project → garmin-bridge → Action → Stop*.
2. *Container → Create* a second container from the image
   `futurewebat/garmin-intervals-bridge` with the **same data folder**
   (`/docker/garmin-bridge/data` → `/data`) and the **same two variables**,
   execution command
   `backfill --scope activities --from 2025-01-01 --archive-only --pause 2`,
   auto-restart **off**. Start it and let it finish (19 months took about
   90 minutes on DarKen's NAS); it stops by itself.
3. Delete that second container, *Project → garmin-bridge → Action → Start*.
4. In the bridge's terminal: `garmin-intervals-bridge gear --create --include-retired --apply`.

Wellness history works the same way with
`backfill --scope wellness --from 2025-01-01 --pause 3 --apply`.

## Updating

*Project → garmin-bridge → Action → Stop*, then *Build*: `pull_policy:
always` fetches the newest image, your settings and data stay. Once a day
the bridge says in its log when a newer version exists; the first log line
shows which version runs.

## Older DSM: the Docker package

Before DSM 7.2 the app is called *Docker* and has no projects. Make the
container by hand instead: *Registry* → download
`futurewebat/garmin-intervals-bridge`; *Image* → *Launch* → *Advanced
settings*: auto-restart on; *Volume* → your `data` folder from step 1 mounted
at `/data`; *Port settings* (only for the web page) → local port `8765` to
container port `8765`; *Environment* → the variables of the project file
above (`INTERVALS_API_KEY`, `BRIDGE_TIMEZONE`, and if you want the web page
`BRIDGE_UI_PORT` and `BRIDGE_UI_PASSWORD`); execution command `run`, later
`run --apply`. Updating: download the new tag, stop the container, *Action →
Clear* (or delete and recreate it with the same settings – the data folder
keeps everything).

## If something goes wrong

- *"The data folder /data is not writable"*: the permission of step 1 is
  missing (or was not applied to sub-folders).
- *"Garmin login needed"* in the log, or a note "Garmin bridge: Garmin login
  needed" in your Intervals calendar: step 3 again.
- *"Another bridge instance is running"*: two containers use the same data
  folder; stop one.
- Garmin's values gone from an activity after Intervals' *Reprocess file*:
  web page → *Actions → One activity again*, or in the terminal
  `garmin-intervals-bridge enrich <the activity's id or link> --apply`.
- Times or dates a few hours off: `BRIDGE_TIMEZONE` is missing or wrong (the
  log's second line says which zone it uses).
- Questions and reports: the forum thread above, or
  <https://github.com/futureweb/garmin-intervals-bridge/issues>.

## Set up with an earlier version of this guide?

A container made by hand (*Container → Create*) keeps working. To move to
the project: stop and delete the old container (the data folder stays),
then create the project of step 2 with the **same data folder** – if yours
is not `/docker/garmin-bridge/data`, write its path instead of `./data`
(e.g. `/volume1/docker/garmin-bridge:/data`) – and the same values. The
Garmin session and the archive carry over; no new login needed.
