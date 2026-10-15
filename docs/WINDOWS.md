# Running the bridge on Windows

No server, no Docker, no programming, no files to edit. About five minutes,
three steps; afterwards the bridge runs in the background whenever you are
logged in, without a window.

## 1. Install Python

Download Python 3.12 or newer from <https://www.python.org/downloads/windows/>
and run the installer. **Tick "Add python.exe to PATH"** on the first screen,
then *Install Now*.

## 2. Install the bridge

Open PowerShell (press the Windows key, type `PowerShell`, Enter) and paste:

```powershell
py -m pip install garmin-intervals-bridge
```

If `py` is unknown, close PowerShell and open it again (the installer's PATH
change needs a new window).

## 3. Set it up

```powershell
py -m garmin_intervals_bridge setup
```

It asks, one question at a time (not on Intervals.icu? answer **2** to the first question for a
[backup of your Garmin data](BACKUP.md) – no key needed):

1. **Your Intervals.icu API key** – in Intervals.icu under *Settings →
   Developer Settings → API key*. Copy it and paste it into PowerShell
   (right-click or `Ctrl`+`V`); it is not shown while you paste, then press
   Enter. The bridge checks it right away and greets you by name.
2. **Your time zone** – it shows the one Windows uses; Enter if it is right.
3. **Garmin Connect** – your e-mail, password and, if Garmin asks, the MFA
   code. The password is used for this one sign-in and not stored; the
   session renews itself for about a year.
4. **Extras** – your Garmin gear (bikes, shoes) on the Intervals activities,
   charts for the Garmin values, ImReady4's training advice if you use it.
5. **When the bridge needs you** – Garmin wants a new sign-in, or something
   has been failing for a day: besides a note in your Intervals calendar, an
   e-mail (sent from your own mailbox; Gmail and iCloud need an app password,
   GMX and web.de "POP3/IMAP access" switched on) and/or a push message on
   your phone (the free app *ntfy*, no account). Each gets a test message.
6. **A trial run** that changes nothing and tells you what the bridge would
   do. Answer **y** to start it for real: it writes to Intervals from now on
   and starts by itself whenever you log in to Windows.

That's it. Everything lives in the folder `GarminBridge` in your user folder
(`C:\Users\<you>\GarminBridge`): the settings (`.env`), the log
(`bridge.log`, open it with Notepad) and your data (`data`: the Garmin
session, the original FIT files, the daily archive – keep it private).

## Day to day

Every command starts with `py -m garmin_intervals_bridge` and works in any
PowerShell window:

| | |
| --- | --- |
| Status, Garmin sign-in, settings, gear – in the browser | `py -m garmin_intervals_bridge ui` |
| Is it running? | `py -m garmin_intervals_bridge autostart --status` |
| What did it do? | `notepad $HOME\GarminBridge\bridge.log` |
| Change a setting | `py -m garmin_intervals_bridge setup` (your answers are the defaults) |
| Stop it for good | `py -m garmin_intervals_bridge autostart --remove` |
| Start it again | `py -m garmin_intervals_bridge autostart` |

It polls Intervals every 5 minutes, does a full run every 30 minutes and checks
itself once a day. When the PC sleeps, it catches up after waking up; when it
was off for days, it reads back to its last run (up to two weeks) and fills in
the days in between. Once a day it also looks whether a newer version exists
and says so in the log.

**Charts:** in Intervals.icu open the *Fitness* page, a tab, *custom charts*,
search **Garmin Bridge** and tick what you like.

## The past (optional, once)

The bridge works from today on. To fill in the past, stop the background
bridge, run the backfill, start it again:

```powershell
py -m garmin_intervals_bridge autostart --remove
py -m garmin_intervals_bridge backfill --scope wellness --from 2026-01-01 --apply
py -m garmin_intervals_bridge backfill --scope activities --from 2026-09-01 --apply
py -m garmin_intervals_bridge autostart
```

A long backfill takes a while (it is polite to Garmin). Gear of older
activities: `py -m garmin_intervals_bridge gear` shows the plan,
`... gear --create --apply` carries it out (see the README, *Gear*).

## Updating

```powershell
py -m pip install --upgrade garmin-intervals-bridge
py -m garmin_intervals_bridge autostart
```

The second line restarts the background bridge with the new version.

## If something goes wrong

- **A note "Garmin bridge: Garmin login needed" in your Intervals
  calendar:** Garmin ended the saved session. Run
  `py -m garmin_intervals_bridge login` (or `setup`); the bridge picks it up
  by itself within 15 minutes. Delete the note whenever you like.
- **Nothing happens after logging in:** `autostart --status` and the end of
  `bridge.log` say why. *Task Scheduler* (Start menu) lists the task as
  *Garmin Intervals Bridge*, with its last run result.
- *"An application control policy has blocked this file"* (German: *"Eine
  Anwendungssteuerungsrichtlinie hat diese Datei blockiert"*): Windows' Smart
  App Control stopped `garmin-intervals-bridge.exe`. Use
  `py -m garmin_intervals_bridge …` as in this guide; the background bridge
  already starts that way.
- *"Another bridge instance is running"*: the background bridge is running;
  stop it first (see *The past*).
- **Garmin's values gone from an activity after Intervals' "Reprocess file"**:
  `py -m garmin_intervals_bridge enrich <the activity's id or link> --apply`, or
  the web page (`ui`) → *Actions → One activity again*.
- *"429"* or *"too many requests"* at the Garmin sign-in: Garmin limits
  sign-ins; wait an hour and run `setup` again.
- Nothing is deleted on either side, so a mistake is at most a missing value – unless you
  switch swap mode on with the `upload` method: then Garmin's filtered copy of each new
  activity in Intervals is deleted once your original is in as a new activity (never one with
  comments; nothing on Garmin Connect). The default `replace` deletes nothing.
- Questions and reports: <https://github.com/futureweb/garmin-intervals-bridge/issues>.

## Set up with an earlier version of this guide?

If you made `C:\GarminBridge` with a `.env` file and `run-bridge.cmd` by
hand, keep it: run setup **in that folder**,

```powershell
cd C:\GarminBridge
py -m garmin_intervals_bridge setup
```

It takes over your settings, Garmin session and data, and replaces the old
logon task (the one with the console window) by the windowless one;
`run-bridge.cmd` is not needed any more. In that case all commands above
need `cd C:\GarminBridge` first, or `--env-file C:\GarminBridge\.env` right
after `garmin_intervals_bridge`.

## By hand, without setup

Everything setup does can be done by hand: a `.env` file with
`INTERVALS_API_KEY=…` (all settings: README, *Configuration*),
`py -m garmin_intervals_bridge login`, a dry run with `sync`, then
`run --apply` in a window that stays open (`Ctrl`+`C` stops it).

Written for Windows 11 and PowerShell; the same steps work in `cmd.exe`
(there, `%USERPROFILE%` instead of `$HOME`).
