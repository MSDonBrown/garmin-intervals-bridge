"""Small, explicit Intervals.icu REST client based on published API schema."""
from __future__ import annotations

import email.utils
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__
from .mapping import CUSTOM_FIELDS

log = logging.getLogger(__name__)

RETRY_AFTER_CAP_SECONDS = 30
WRITE_RETRIES = 3                  # a wellness PUT answered with 429 is sent again after the wait


def _seconds(value: Any) -> float | None:
    """A Retry-After header: seconds, or an HTTP date."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _retry_after(response: Any, attempt: int) -> float:
    value = _seconds(getattr(response, "headers", {}).get("Retry-After"))
    return max(1.0, min(value if value is not None else 5.0 * 2 ** attempt, RETRY_AFTER_CAP_SECONDS))


def rate_limited(exc: BaseException) -> float | None:
    """Seconds Intervals asks a client to wait when it answered 429 (0: it did not say);
    None for any other error. A GET whose retries ran out on 429s counts too."""
    if isinstance(exc, requests.exceptions.RetryError):
        return 0.0 if "429" in str(exc) else None
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) != 429:
        return None
    return _seconds(response.headers.get("Retry-After")) or 0.0


def refused(exc: BaseException) -> bool:
    """Intervals answered with a 4xx (a 429 included): it did not take the request – an upload
    that failed this way created nothing and may be sent again later."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and 400 <= status < 500


class _CappedRetry(Retry):
    """Honour Retry-After on 429, but never park a poll for longer than its
    own timer period (urllib3 would otherwise sleep for up to six hours)."""

    def get_retry_after(self, response):
        value = super().get_retry_after(response)
        return min(value, RETRY_AFTER_CAP_SECONDS) if value is not None else None


def _activity_id(activity_id: int | str) -> str:
    """Intervals activity ids look like `i12345678`; nothing else belongs in a URL path."""
    text = str(activity_id).strip()
    if not re.fullmatch(r"i?\d{1,20}", text):
        raise ValueError("Intervals activity id must be digits with an optional leading 'i'")
    return text


class IntervalsClient:
    def __init__(self, api_key: str, athlete_id: str = "0", session: requests.Session | None = None):
        if not api_key:
            raise ValueError("INTERVALS_API_KEY required for Intervals requests")
        self.root = "https://intervals.icu/api/v1"
        self.base = f"{self.root}/athlete/{athlete_id}"
        self.session = session or requests.Session()
        self.session.auth = ("API_KEY", api_key)
        self.session.headers["User-Agent"] = f"garmin-intervals-bridge/{__version__}"
        self._custom_items: list[dict] | None = None     # one GET per process; writes invalidate it
        if session is None:
            # Only GET retries; never automatically replay a non-idempotent upload.
            # no retry on 429: it goes up to the pause, with the Retry-After Intervals sent
            retries = _CappedRetry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504],
                                   allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
            self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def _request(self, method: str, path: str, **kwargs) -> Any:
        return self._api(method, f"/athlete/{self.base.rsplit('/', 1)[1]}{path}", **kwargs)

    def _api(self, method: str, path: str, **kwargs) -> Any:
        """Request relative to /api/v1 (activity endpoints are not athlete-scoped)."""
        response = self.session.request(method, f"{self.root}{path}", timeout=(10, 75), **kwargs)
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ---- single activities: /api/v1/activity/{id}... ----

    def activity(self, activity_id: str) -> dict:
        obj = self._api("GET", f"/activity/{_activity_id(activity_id)}")
        if not isinstance(obj, dict):
            raise ValueError("Intervals activity API did not return an object")
        return obj

    def activity_file(self, activity_id: str) -> bytes:
        """The file Intervals received for this activity (for Garmin: the partner copy)."""
        response = self.session.get(f"{self.root}/activity/{_activity_id(activity_id)}/file", timeout=(10, 75))
        response.raise_for_status()
        return response.content

    def streams(self, activity_id: str, types: list[str] | None = None) -> list[dict]:
        params = {"types": ",".join(types)} if types else {}
        obj = self._api("GET", f"/activity/{_activity_id(activity_id)}/streams.json", params=params)
        if not isinstance(obj, list):
            raise ValueError("Intervals streams API did not return a list")
        return obj

    def put_streams(self, activity_id: str, streams: list[dict]) -> Any:
        """PUT /activity/{id}/streams with ActivityStream[]; returns UpdateStreamsResult."""
        return self._api("PUT", f"/activity/{_activity_id(activity_id)}/streams", json=streams)

    def update_activity(self, activity_id: str, fields: dict) -> Any:
        return self._api("PUT", f"/activity/{_activity_id(activity_id)}", json=fields)

    def set_activity_gear(self, activity_id: str, gear_id: str) -> Any:
        return self.update_activity(activity_id, {"gear": {"id": gear_id}})

    # ---- gear: /api/v1/athlete/{id}/gear ----

    def gear(self) -> list[dict]:
        return self._request("GET", "/gear")

    def create_gear(self, gear: dict) -> dict:
        return self._request("POST", "/gear", json=gear)

    def recalc_gear(self, gear_id: str) -> dict:
        """Intervals' own count of one gear's activities (distance, times, activities); not stored."""
        return self._request("GET", f"/gear/{gear_id}/calc")

    def update_gear(self, gear_id: str, fields: dict) -> Any:
        return self._request("PUT", f"/gear/{gear_id}", json=fields)

    def activities(self, oldest: date, newest: date, fields: list[str] | None = None,
                   limit: int | None = None) -> list[dict]:
        params: dict = {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        if fields:
            params["fields"] = ",".join(fields)     # small answers: a poll is ~450 bytes
        if limit:
            params["limit"] = limit
        result = self._request("GET", "/activities", params=params)
        if not isinstance(result, list):
            raise ValueError("Intervals activities API did not return a list (fail closed)")
        return result

    def nearby_activities(self, activity_day: date) -> list[dict]:
        return self.activities(activity_day - timedelta(days=1), activity_day + timedelta(days=1))

    def wellness(self, day: date) -> dict:
        # A day-scoped GET is preferred to an all-fields range where some
        # custom fields may be omitted unless explicitly requested.
        response = self.session.get(f"{self.base}/wellness/{day.isoformat()}", timeout=(10, 60))
        if response.status_code == 404:
            return {"id": day.isoformat()}
        response.raise_for_status()
        obj = response.json()
        if not isinstance(obj, dict):
            raise ValueError("Intervals wellness API returned invalid response")
        return obj

    def wellness_range(self, oldest: date, newest: date, cols: list[str] | None = None) -> list[dict]:
        """Wellness records of a date range, oldest first; custom fields only when named in `cols`."""
        params = {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        if cols:
            params["cols"] = ",".join(cols)
        records = self._request("GET", "/wellness", params=params)
        if not isinstance(records, list):
            raise ValueError("Intervals wellness range response was not a list")
        return records

    def write_wellness(self, day: date, changes: dict) -> Any:
        """PUT the changed fields of one day. Intervals' rate limit (429) is waited out and the same
        body sent again - safe, it sets the same values; any other error fails as before."""
        if not changes:
            return None
        for attempt in range(WRITE_RETRIES + 1):
            try:
                return self._request("PUT", f"/wellness/{day.isoformat()}", json=changes)
            except requests.HTTPError as exc:
                response = getattr(exc, "response", None)
                if attempt == WRITE_RETRIES or getattr(response, "status_code", None) != 429:
                    raise
                wait = _retry_after(response, attempt)
                log.info("Intervals asks to slow down (429); writing %s again in %.0f s", day, wait)
                time.sleep(wait)
        return None

    def upload_fit(self, garmin_id: str, path: Path, **extra: Any) -> dict:
        """An Intervals upload returns 201 if an activity was created, 200 otherwise.

        The response is normally an array, not an Activity object. This method
        deliberately distinguishes 'created' from 'accepted but not created'.
        `extra`: name, description, paired_event_id for the new activity (the swap).
        """
        params = {"external_id": f"garmin:{garmin_id}"}
        params.update({k: v for k, v in extra.items() if k in ("name", "description", "paired_event_id")
                       and v not in (None, "")})
        with path.open("rb") as f:
            response = self.session.post(
                f"{self.base}/activities",
                params=params,
                files={"file": (path.name, f, "application/octet-stream")},
                timeout=(10, 75),
            )
        response.raise_for_status()
        if response.status_code not in (200, 201):
            raise ValueError(f"Unexpected Intervals upload HTTP status {response.status_code}")
        payload = response.json()
        # Current OpenAPI defines UploadResponse as {"id", "icu_athlete_id",
        # "activities": [{"id", ...}]}. Older forum examples document
        # a top-level array. Accept both without losing replay protection.
        if isinstance(payload, dict):
            items = payload.get("activities")
            if not isinstance(items, list):
                raise ValueError("Unexpected Intervals upload response: UploadResponse.activities missing")
        elif isinstance(payload, list):
            items = payload
        else:
            raise ValueError("Unexpected Intervals upload response type")
        if not all(isinstance(item, dict) and item.get("id") for item in items):
            raise ValueError("Unexpected Intervals upload response: invalid ActivityId entry")
        if response.status_code == 201 and not items:
            raise ValueError("Intervals returned 201 with no created activities")
        return {"created": response.status_code == 201, "items": items}

    def delete_activity(self, activity_id: str) -> None:
        """Only the swap uses this: Garmin's filtered copy, once its original is in."""
        self._api("DELETE", f"/activity/{_activity_id(activity_id)}")

    def activity_messages(self, activity_id: str) -> list[dict]:
        out = self._api("GET", f"/activity/{_activity_id(activity_id)}/messages")
        if not isinstance(out, list):            # unknown answer: no proof of "no comments" (fail closed)
            raise ValueError("Intervals messages API did not return a list")
        return out

    def athlete(self) -> dict:
        """The athlete the key belongs to (name, id); `setup` checks a key with it."""
        obj = self._request("GET", "")
        if not isinstance(obj, dict):
            raise ValueError("Intervals athlete response was not an object")
        return obj

    def create_note(self, day: date, name: str, description: str) -> Any:
        """A NOTE in the calendar on `day`, for the athlete to read; the bridge never deletes it."""
        return self._request("POST", "/events", json={"category": "NOTE", "start_date_local": f"{day}T00:00:00",
                                                      "name": name, "description": description})

    def forget_custom_items(self) -> None:
        """The athlete may have changed their fields since: ask again (a long-running `run`)."""
        self._custom_items = None

    def custom_items(self) -> list[dict]:
        if self._custom_items is None:
            items = self._request("GET", "/custom-item")
            if not isinstance(items, list):
                raise ValueError("Intervals custom items response was not a list")
            self._custom_items = items
        return list(self._custom_items)

    def create_custom_item(self, item: dict) -> Any:
        self._custom_items = None
        return self._request("POST", "/custom-item", json=item)

    def update_custom_item(self, item_id: int | str, item: dict) -> Any:
        self._custom_items = None
        return self._request("PUT", f"/custom-item/{int(item_id)}", json=item)

    def reorder_custom_items(self, items: list[dict]) -> Any:
        """PUT /custom-item-indexes: items with their `index` set. Needed after POST, which
        leaves every new item at index 0; the chart picker then shows none of them."""
        self._custom_items = None
        return self._request("PUT", "/custom-item-indexes", json=items)

    def field_provision_plan(self) -> list[tuple[str, str]]:
        codes = {x.get("content", {}).get("code") for x in self.custom_items()
                 if isinstance(x, dict) and x.get("type") == "INPUT_FIELD" and isinstance(x.get("content"), dict)}
        return [(code, spec.name) for code, spec in CUSTOM_FIELDS.items() if code not in codes]

    def provision_fields(self, apply: bool, needed: set[str] | None = None) -> list[str]:
        """Create only missing, private numeric wellness INPUT_FIELD definitions.

        Deliberately no updates/deletes of existing definitions.
        """
        existing = self.custom_items()
        by_code = {item.get("content", {}).get("code"): item
                   for item in existing if isinstance(item, dict) and item.get("type") == "INPUT_FIELD"
                   and isinstance(item.get("content"), dict)}
        plan: list[str] = []
        for code, spec in CUSTOM_FIELDS.items():
            if needed is not None and code not in needed:
                continue
            if code in by_code:
                content = by_code[code]["content"]
                if content.get("type") != "numeric":
                    raise ValueError(f"Custom field {code} exists but is not numeric; refusing to overwrite")
                continue
            plan.append(code)
            if apply:
                content = {"code": code, "name": spec.name, "type": "numeric"}
                if spec.units:
                    content["units"] = spec.units
                self._custom_items = None
                self._request("POST", "/custom-item", json={
                    "name": spec.name, "type": "INPUT_FIELD", "visibility": "PRIVATE",
                    "description": "Automatically imported from Garmin Connect by garmin-intervals-bridge.",
                    "content": content,
                })
        return plan
