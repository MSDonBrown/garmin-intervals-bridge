# Garmin Intervals Bridge

Garmin filters the FIT files it sends to Intervals.icu. This app puts the data back – Stamina,
Recovery Time, VO₂max, Training Effect … on every activity – syncs the wellness data the
official integration misses, puts your Garmin gear on the activities, and can write
ImReady4's training advice. Or, in backup mode, it simply keeps all your Garmin data here in
Home Assistant, no Intervals needed.

## First start

1. **Configuration** tab: your **Intervals.icu API key** (Intervals.icu → Settings → Developer
   Settings → API key) and the **mode** – `enrich` is the usual one (Garmin's official import
   stays on), `backup` needs no key at all. Leave **Write to Intervals** off for now. *Save*.
2. **Start** the app, then open **Garmin Bridge** in the sidebar (or *Open web UI*):
   **Garmin** → your Garmin Connect e-mail and password, and the code if Garmin sends one.
   The password goes to Garmin once and is not stored; the session renews itself for about a
   year.
3. The **Log** tab (or the web page's *Log*) now shows what the bridge *would* write. When
   that looks right: **Write to Intervals** on, *Save*, restart the app.

That's it. It checks Intervals every 5 minutes and enriches a new activity within minutes
of Garmin's import; last night's wellness arrives once your watch has synced.

## Moving in from a NAS or another computer

Your Garmin session and archive can come along: stop the bridge there, copy its data folder
into Home Assistant's **share** folder as `share/garmin-bridge` (e.g. through the *Samba
share* app), then start this app. On its first start it takes the folder over – never over
data it already has – and carries on without a new Garmin sign-in.

## The web page

In the sidebar: what the bridge did, *Check now* for new activities, the Garmin sign-in, gear and chart plans with a button
to carry them out, one activity again after Intervals' *Reprocess file*, today's training
advice, the log. Home Assistant signs you in; nothing else can reach the page.

## Options

| Option | |
| --- | --- |
| Mode | `enrich` (usual), `upload` (Garmin's official activity import switched off), `backup` (no Intervals) |
| Write to Intervals | off = dry run |
| Swap Garmin's copy for the original | puts your original file in place of the filtered copy – "Reprocess file" then keeps everything; nothing on Garmin Connect is touched |
| How to swap | `replace` (default): into the same activity, nothing deleted · `upload`: a new activity of your own, Garmin's copy deleted |
| ImReady4 / HRV trend advice | the morning training advice into your wellness fields |
| Poll hours / interval / quiet interval | look for new activities less often, only during the day, or slower at night |
| Adaptive polling (beta) | learns when your activities usually end and checks often only then – fewer requests for Intervals.icu |
| Alert e-mail / Push messages (ntfy) | when Garmin wants a new sign-in or something has been failing for a day |
| Time zone | empty = Home Assistant's |

Everything else, the backfill of the past and what each value means:
<https://github.com/futureweb/garmin-intervals-bridge>. Questions and reports:
<https://forum.intervals.icu/t/132863>.
