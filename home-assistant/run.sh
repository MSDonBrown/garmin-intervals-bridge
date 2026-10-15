#!/bin/sh
# Home Assistant app: the bridge with the settings of the app's form. Writes to Intervals only
# once "Write to Intervals" is switched on; until then a dry run that shows what it would do.
set -e
DATA=${HA_DATA:-/data}
SHARE=${HA_SHARE:-/share}
# Moving in from a NAS or another computer: its data folder, copied to Home Assistant's
# share/garmin-bridge (Samba share), is taken over – as long as this app has no Garmin session
# and no archive of its own yet (an earlier start may have left an empty database), and once.
if [ ! -e "$DATA/.taken-over" ] && [ -e "$SHARE/garmin-bridge/bridge.db" ] \
        && ! ls "$DATA"/tokens/*.json >/dev/null 2>&1 && [ -z "$(ls -A "$DATA/fits" 2>/dev/null)" ]; then
    echo "Taking over the data folder from $SHARE/garmin-bridge (Garmin session, archive)"
    for f in bridge.db bridge.db-wal bridge.db-shm; do     # the empty one of an earlier start: kept aside
        if [ -e "$DATA/$f" ]; then mv -f "$DATA/$f" "$DATA/$f.before-takeover"; fi
    done
    if [ -e "$DATA/options.json" ]; then cp -p "$DATA/options.json" "$DATA/.options.json.app"; fi
    cp -a "$SHARE/garmin-bridge/." "$DATA/"
    # the app's own form stays what Home Assistant says, whatever the copied folder brought along
    if [ -e "$DATA/.options.json.app" ]; then mv -f "$DATA/.options.json.app" "$DATA/options.json"; fi
    touch "$DATA/.taken-over"
fi
export BRIDGE_DATA_DIR="$DATA" GARMIN_TOKEN_DIR="$DATA/tokens" BRIDGE_OPTIONS_FILE="$DATA/options.json"
export BRIDGE_UI_PORT=8099 BRIDGE_UI_HOST=0.0.0.0 BRIDGE_UI_INGRESS=1
export BRIDGE_LOG_FILE="$DATA/bridge.log"      # the web page's Log, with date and time (5 MB x 3)
APPLY=$(python3 -c 'import json, sys; print("--apply" if json.load(open(sys.argv[1])).get("write_to_intervals") else "")' "$DATA/options.json")
exec garmin-intervals-bridge run $APPLY
