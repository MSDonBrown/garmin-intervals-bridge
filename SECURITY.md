# Security and privacy

This project handles GPS tracks, detailed medical/fitness/health metrics (including logged nutrition and sleep data), Garmin session tokens and the Intervals API key.

**Must**

- Never commit `.env`, `data/`, FIT exports, JSON snapshots, OAuth tokens, logs, or raw API responses.
- Restrict data/tokens folder permissions (0700) and backup encrypted at rest. FIT files and JSON (0600) contain personal information.
- Run as an unprivileged user: the non-root container; keep the virtualenv outside any web document root and the data directory at mode 0700.
- Writing custom streams marks the activity's intervals as edited in Intervals (see README, known limitations); nothing is ever deleted.
- Use the Intervals API key only over HTTPS. Do not put it into CLI arguments or screenshots.
- Never paste Garmin passwords, session tokens, Intervals API keys or private FITs into public GitHub issues.
- Run `login` once in a local interactive terminal and let Garmin session tokens refresh. Do not store Garmin account password in cron or compose environment variables.
- `--verbose` makes the Garmin library log every request URL, and those URLs contain your Garmin display name. Use it for troubleshooting, not in a scheduled unit, and treat the journal as private.
- Keep remote writes behind explicit `--apply` and `--allow-activity-upload`; audit the difference before enabling them.
- The bridge deletes nothing – with one opt-in exception: swap mode with the `upload` method (`BRIDGE_SWAP=true` with `BRIDGE_SWAP_METHOD=upload`, or `swap <id> --method upload --apply`) deletes the filtered copy that Garmin's import created in Intervals – never anything on Garmin Connect, which the bridge only reads – after its original has been uploaded and the athlete's values carried over; never with comments or hand-edited intervals on it, never before the upload succeeded.
- Keep Garmin official wellness import if useful; only disable the overlapping *activity* import after proven testing.
- Do not auto-resubmit a pending FIT upload without checking Intervals for the activity. Upload requests may have succeeded even after a network timeout.
- `setup` reads the API key without echoing it and writes the settings file readable by its owner only (0600); keep `~/GarminBridge` (settings, log, data) private and out of cloud-synced folders you share.
- The web page (`ui`, or `BRIDGE_UI_PORT` next to `run`): on 127.0.0.1 a random key in the link; anywhere else only with `BRIDGE_UI_PASSWORD` (8+ characters, the templates' placeholder refused). It is plain http – keep it in your home network or behind an HTTPS reverse proxy, never expose the port to the internet. Forms carry per-session CSRF tokens, local pages check the Host header (DNS rebinding), strict CSP, no caching or framing. The Garmin password typed there goes to Garmin only; the API key is never shown.
- Alerts: the mailbox password (`BRIDGE_SMTP_PASSWORD`, ideally an app password that can only send) lives in the settings file like the API key; mail is sent with TLS. An ntfy topic name is a secret (anyone who knows it can read its messages): use a long random one, as `setup` does; push messages carry the headline only, never details.
- The bridge connects to Garmin Connect, Intervals.icu and, once a day for the update hint, pypi.org (`BRIDGE_UPDATE_CHECK=false` turns that off); when alerts are set up, your mail server and/or the ntfy server. No telemetry.

**Limitations**

- Garmin endpoints are unofficial/private, may change and may have access restrictions and anti-automation protection; honor their rules.
- FIT header validation is structural, not a cryptographic signature or complete FIT semantic/CRC validation. For deeper checks use `compare-fit` with `fitparse` and a verified Garmin web download.
- Existing wellness values are preserved, but concurrently changing server-side custom fields between GET and PUT can theoretically race; this is not a transactional multiwriter merge.
- Automatic retry is limited to GET requests; a failed write might still have taken effect, and the app avoids replaying activity uploads automatically.
- Synthetic tests cannot prove Garmin session tokens or Intervals API data shapes; see README acceptance checklist before release.

**Reporting vulnerabilities**

If publishing a public GitHub repo, set up private security advisories / vulnerability reporting first. Avoid posting tokens or customer data.
