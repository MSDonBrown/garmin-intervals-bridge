# Running the bridge on a Mac

No server, no Docker, no programming, no files to edit. About five minutes,
three steps; afterwards the bridge runs in the background whenever you are
logged in.

## 1. Install Python

Download Python 3.12 or newer from <https://www.python.org/downloads/macos/>
and run the installer. When it is done, Finder shows the new Python folder:
double-click **Install Certificates.command** there once.

## 2. Install the bridge

Open Terminal (`Cmd`+`Space`, type `Terminal`, Enter) and paste:

```bash
mkdir -p ~/GarminBridge && cd ~/GarminBridge
python3 -m venv venv
venv/bin/pip install garmin-intervals-bridge
```

This puts the bridge and everything it needs into `GarminBridge` in your
home folder; nothing else on the Mac is changed. If `python3 -m venv`
complains, or macOS offers to install developer tools, Python from
python.org is not installed yet (macOS's own `python3` is too old); install
it, then open a new Terminal window.

## 3. Set it up

```bash
venv/bin/garmin-intervals-bridge setup
```

It asks, one question at a time (not on Intervals.icu? answer **2** to the first question for a
[backup of your Garmin data](BACKUP.md) – no key needed):

1. **Your Intervals.icu API key** – in Intervals.icu under *Settings →
   Developer Settings → API key*. Copy it and paste it into Terminal
   (`Cmd`+`V`); it is not shown while you paste, then press Enter. The
   bridge checks it right away and greets you by name.
2. **Your time zone** – it shows the one the Mac uses; Enter if it is right.
3. **Garmin Connect** – your e-mail, password (also not shown while typing)
   and, if Garmin asks, the MFA code. The password is used for this one
   sign-in and not stored; the session renews itself for about a year.
4. **Extras** – your Garmin gear (bikes, shoes) on the Intervals activities,
   charts for the Garmin values, ImReady4's training advice if you use it.
5. **When the bridge needs you** – Garmin wants a new sign-in, or something
   has been failing for a day: besides a note in your Intervals calendar, an
   e-mail (sent from your own mailbox; Gmail and iCloud need an app password,
   GMX and web.de "POP3/IMAP access" switched on) and/or a push message on
   your phone (the free app *ntfy*, no account). Each gets a test message.
6. **A trial run** that changes nothing and tells you what the bridge would
   do. Answer **y** to start it for real: it writes to Intervals from now on
   and starts by itself whenever you log in to the Mac.

macOS then notifies you that a background item was added (from *Python*):
that is the bridge. It is listed under *System Settings → General → Login
Items*.

Everything lives in `~/GarminBridge`: the settings (`.env`), the log
(`bridge.log`) and your data (`data`: the Garmin session, the original FIT
files, the daily archive – keep it private). Leave the folder where it is:
macOS does not let background jobs into Desktop, Documents or Downloads.

## Day to day

In Terminal, `cd ~/GarminBridge` first:

| | |
| --- | --- |
| Status, Garmin sign-in, settings, gear – in the browser | `venv/bin/garmin-intervals-bridge ui` |
| Is it running? | `venv/bin/garmin-intervals-bridge autostart --status` |
| What did it do? | `tail -f bridge.log` (`Ctrl`+`C` ends watching, not the bridge) |
| Change a setting | `venv/bin/garmin-intervals-bridge setup` (your answers are the defaults) |
| Stop it for good | `venv/bin/garmin-intervals-bridge autostart --remove` |
| Start it again | `venv/bin/garmin-intervals-bridge autostart` |

It polls Intervals every 5 minutes, does a full run every 30 minutes and checks
itself once a day; if it ever stops on an error, it is started again after
five minutes. Once a day it also looks whether a newer version exists and
says so in the log.

The bridge runs while the Mac is awake. A MacBook with the lid closed
catches up as soon as it wakes: last night's values arrive a few minutes
after you open it, if the watch has synced. For a bridge that never sleeps,
a Mac mini, a NAS or a small Linux box with the container image is the
better home.

**Charts:** in Intervals.icu open the *Fitness* page, a tab, *custom charts*,
search **Garmin Bridge** and tick what you like.

## The past (optional, once)

The bridge works from today on. To fill in the past, stop the background
bridge, run the backfill, start it again:

```bash
cd ~/GarminBridge
venv/bin/garmin-intervals-bridge autostart --remove
venv/bin/garmin-intervals-bridge backfill --scope wellness --from 2026-01-01 --apply
venv/bin/garmin-intervals-bridge backfill --scope activities --from 2026-09-01 --apply
venv/bin/garmin-intervals-bridge autostart
```

A long backfill takes a while (it is polite to Garmin). Gear of older
activities: `venv/bin/garmin-intervals-bridge gear` shows the plan,
`... gear --create --apply` carries it out (see the README, *Gear*).

## Updating

```bash
cd ~/GarminBridge
venv/bin/pip install --upgrade garmin-intervals-bridge
venv/bin/garmin-intervals-bridge autostart
```

The last line restarts the background bridge with the new version.

## If something goes wrong

- **A note "Garmin bridge: Garmin login needed" in your Intervals
  calendar:** Garmin ended the saved session. Run
  `venv/bin/garmin-intervals-bridge login` (or `setup`) in `~/GarminBridge`;
  the bridge picks it up by itself within 15 minutes. Delete the note
  whenever you like.
- *"command not found"* or *"no such file"*: you are not in the bridge's
  folder; `cd ~/GarminBridge` first.
- **Nothing happens:** `autostart --status` and the end of `bridge.log` say
  why.
- *"Another bridge instance is running"*: the background bridge is running;
  stop it first (see *The past*).
- **Garmin's values gone from an activity after Intervals' "Reprocess file"**:
  `venv/bin/garmin-intervals-bridge enrich <the activity's id or link> --apply`, or
  the web page (`ui`) → *Actions → One activity again*.
- *"429"* or *"too many requests"* at the Garmin sign-in: Garmin limits
  sign-ins; wait an hour and run `setup` again.
- Nothing is deleted on either side, so a mistake is at most a missing value – unless you
  switch swap mode on with the `upload` method: then Garmin's filtered copy of each new
  activity in Intervals is deleted once your original is in as a new activity (never one with
  comments; nothing on Garmin Connect). The default `replace` deletes nothing.
- Questions and reports: <https://github.com/futureweb/garmin-intervals-bridge/issues>.

## Set up with an earlier version of this guide?

Run `venv/bin/garmin-intervals-bridge setup` in `~/GarminBridge`: it takes
over your `.env`, Garmin session and data, and replaces the launch file you
pasted by its own (same name; the bridge now writes `bridge.log` itself).

## By hand, without setup

Everything setup does can be done by hand: a `.env` file with
`INTERVALS_API_KEY=…` (all settings: README, *Configuration*),
`venv/bin/garmin-intervals-bridge login`, a dry run with `sync`, then
`run --apply` in a window that stays open (`Ctrl`+`C` stops it).

Written for macOS 13 Ventura or newer, on Apple silicon and Intel Macs.
Already using Docker Desktop? The container image from the
[README](../README.md#running-it) (*Docker / Podman*) runs on a Mac as well.
