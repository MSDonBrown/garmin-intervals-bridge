# Roadmap

Where the bridge stands and where it goes. Ideas and reports:
[forum thread](https://forum.intervals.icu/t/132863) or
[issues](https://github.com/futureweb/garmin-intervals-bridge/issues). What changed when:
[CHANGELOG.md](../CHANGELOG.md).

## There

- **Activities:** Garmin's filtered copy completed from the original FIT (*enrich*), the
  original put into it (*swap*, in place since Intervals' *replace file*), or the originals
  uploaded with the official import off (*upload*); one activity again after Intervals'
  *Reprocess file*.
- **Wellness:** the values the official integration misses, read once the watch has synced;
  the device's own wellness files and Health Snapshots; backfills of the past.
- **More:** gear from Garmin onto the activities, ImReady4's training advice and an
  experimental HRV-trend advice, 20 charts, a complete local archive – or *backup* mode
  without Intervals at all.
- **Running it:** `setup` and autostart on Windows, Mac and Linux; containers for Synology,
  Unraid and Docker; a Home Assistant app; systemd units; a web page with the Garmin sign-in;
  alerts by calendar note, e-mail or push message.

## Next

- **Home Assistant app:** feedback from real installs, then the polish it asks for.
- **Umbrel and CasaOS:** entries in their app stores, once the Home Assistant app has settled.
- **History in backup mode** from the web page, not only from a terminal.

## Waiting for Intervals.icu

Asked for in the [feature request](https://forum.intervals.icu/t/api-upload-a-file-onto-an-existing-activity-and-a-new-activity-webhook-for-api-key-users/132953):

- ~~Upload a file onto an existing activity~~ – there since October 2026
  (`POST /activity/{id}/file`), swap mode's default now. Asked: whether the activity's source
  should then stop saying Garmin's import, keeping the link to the Garmin activity.
- **Being told about a new activity** (a webhook per API key, or an event stream) instead of
  asking every few minutes.

## Deliberately not

- **A hosted service.** Garmin sign-ins collected in one place would be a target and a
  liability; the bridge runs where your data is.
- **A signed stand-alone program.** It would cost certificates every year; `py -m` keeps
  Windows' Smart App Control content for free.
- **Garmin data the device did not record.** Nothing is estimated or made up; what Garmin
  does not deliver stays empty.
- **Deleting anything on Garmin Connect.** The bridge only ever reads from Garmin.
