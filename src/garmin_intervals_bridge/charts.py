"""Private Intervals fitness charts for the values the bridge adds.

Intervals stores charts as custom items of type FITNESS_CHART whose `content`
lists plots. A plot of a custom wellness field embeds the field's own item
(`filter: "customInput"`), so the field must exist before the chart is made.
Native fields are referenced by Intervals' chart ids (`hrv`, `resting_hr`,
`sleep`, `weight`, `calories`, `readiness`); only ids seen in real chart
definitions are used here.
"""
from __future__ import annotations

import secrets
from typing import Any

# Native wellness chart ids, read from Intervals' app bundle (`{id:..., fn:..., scale:...}`
# definitions), so none of them is a guess: hrv, resting_hr, avg_sleeping_hr, readiness,
# steps, respiration, spo2, calories, kcal_consumed, carbohydrates, protein, fatTotal,
# weight, body_fat, vo2max, sleep, sleep_score, hydration_volume, systolic, diastolic.
# Each chart: name, title, y-axis label, height, plots. A plot is either
#   ("custom", code, text, type, agg, days, fill, stroke, stack[, scale[, filter]])
# Intervals draws one y axis per distinct plot `scale` (a second axis from two scales, a third
# from three), so plots with different units get different scale names. `filter` formats the
# legend value: dec0/dec1/dec2, hours, percent, interval_time (seconds as a duration).
#   ("native", field, scale, filter, text, type, agg, days, fill, stroke)
# Every chart is named "Garmin Bridge: <topic>": the chart picker's search finds them all with
# "Garmin" or "Bridge", and they sit together in its alphabetical list. `aliases` are earlier
# names of the same chart; a bridge chart found under one is renamed in place, never duplicated.
CHARTS: list[dict[str, Any]] = [
    {
        "name": "Garmin Bridge: Readiness & recovery",
        "aliases": ("Garmin readiness & recovery",),
        "title": "Readiness, recovery time, acute load (Garmin)",
        "y": "Readiness",
        "y2": "Recovery (h)",
        "height": 180,
        "plots": [
            ("custom", "GarminTrainingReadiness", "Ready", "bars", "none",
             None, "#009E0040", "#009E00FF", "", "score"),
            ("custom", "GarminRecoveryTimeHours", "Recov.", "line", "none",
             None, "#D6272800", "#D62728FF", "", "hours", "dec1"),
            # acute load is a daily value: a day without one stays empty (fill_in would carry a stale
            # load across weeks without a watch sync)
            ("custom", "GarminAcuteLoad", "Load7d", "line", "none", None, "#1F77B400", "#1F77B4FF", "", "load"),
        ],
    },
    {
        "name": "Garmin Bridge: Sleep stages",
        "aliases": ("Garmin sleep stages",),
        "title": "Deep / light / REM / awake (minutes)",
        "y": "Minutes",
        "height": 180,
        "plots": [
            ("custom", "GarminSleepDeepMinutes", "Deep", "bars", "none", None, "#1F3A93CC", "#1F3A93FF", "sleep"),
            ("custom", "GarminSleepLightMinutes", "Light", "bars", "none", None, "#4C8BE2CC", "#4C8BE2FF", "sleep"),
            ("custom", "GarminSleepREMMinutes", "REM", "bars", "none", None, "#9B59B6CC", "#9B59B6FF", "sleep"),
            ("custom", "GarminSleepAwakeMinutes", "Awake", "bars", "none", None, "#E67E22CC", "#E67E22FF", "sleep"),
        ],
    },
    {
        "name": "Garmin Bridge: Stress & Body Battery",
        "aliases": ("Garmin stress & Body Battery",),
        "title": "Stress average, Body Battery max/min, charged/drained",
        "y": "0–100",
        "height": 180,
        "plots": [
            ("custom", "BodyBatteryMax", "BBmax", "line", "none", None, "#009E0030", "#009E00FF", ""),
            ("custom", "BodyBatteryMin", "BBmin", "line", "none", None, "#D6272830", "#D62728FF", ""),
            ("custom", "GarminStressAvg", "Stress", "bars", "none", None, "#FF7F0E40", "#FF7F0EFF", ""),
            ("custom", "GarminBodyBatteryCharged", "Charge", "dot", "none", None, "#2CA02C66", "#2CA02CFF", ""),
            ("custom", "GarminBodyBatteryDrained", "Drain", "dot", "none", None, "#8C564B66", "#8C564BFF", ""),
        ],
    },
    {
        "name": "Garmin Bridge: Nutrition intake vs. burn",
        "aliases": ("Nutrition: intake vs. burn (Garmin)",),
        "title": "kcal consumed (logged in Garmin) vs. Garmin's total daily burn (BMR + active); active part as bars",
        "y": "kcal",
        "height": 180,
        "plots": [
            # `kcal_consumed` follows Intervals' chart-id pattern (restingHR -> resting_hr,
            # spO2 -> spo2); it is the one id here not yet seen in a real chart definition.
            ("native", "kcal_consumed", "kcal", "dec0", "In",
             "line", "none", None, "#2CA02C00", "#2CA02CFF"),
            ("custom", "GarminTotalCalories", "Out", "line", "none", None, "#D6272800", "#D62728FF", "", "kcal"),
            ("custom", "GarminActiveCalories", "Active", "bars", "none", None, "#D6272833", "#D6272880", "", "kcal"),
        ],
    },
    {
        "name": "Garmin Bridge: Energy balance (weekly)",
        "title": "Weekly total of (kcal consumed - Garmin total burn); weight on the right axis",
        "y": "kcal / week",
        "y2": "kg",
        "height": 180,
        "plots": [
            ("custom", "GarminKcalBalance", "Balance", "bars", "week_tot", None, "#1F77B466", "#1F77B4FF", "", "kcal"),
            ("native", "weight", "kg", "dec1", "Wt 7d", "line", "moving_avg", 7, "#7F7F7F00", "#7F7F7FFF"),
        ],
    },
    {
        "name": "Garmin Bridge: Energy balance (monthly)",
        "title": "Monthly total of (kcal consumed - Garmin total burn); weight on the right axis",
        "y": "kcal / month",
        "y2": "kg",
        "height": 180,
        "plots": [
            ("custom", "GarminKcalBalance", "Balance", "bars", "month_tot", None, "#9467BD66", "#9467BDFF", "", "kcal"),
            ("native", "weight", "kg", "dec1", "Wt 7d", "line", "moving_avg", 7, "#7F7F7F00", "#7F7F7FFF"),
        ],
    },
    {
        "name": "Garmin Bridge: Nutrition macros",
        "aliases": ("Nutrition: macros (Garmin)",),
        "title": "Energy from carbohydrates / protein / fat (g x 4/4/9); the percentage is the share of intake energy",
        "y": "kcal",
        "height": 180,
        "plots": [
            ("custom", "GarminCarbsKcal", "Carbs", "bars", "none", None, "#70663180", "#706631FF", "food"),
            ("custom", "GarminProteinKcal", "Prot.", "bars", "none", None, "#9B033280", "#9B0332FF", "food"),
            ("custom", "GarminFatKcal", "Fat", "bars", "none", None, "#DA6C0B80", "#DA6C0BFF", "food"),
            # the logged total as a thin line over the stack (same axis: scale None like the bars)
            ("native", "kcal_consumed", None, "dec0", "Total", "line", "none", None, "#33333300", "#333333AA"),
        ],
    },
    {
        "name": "Garmin Bridge: Endurance & hill scores",
        "aliases": ("Garmin scores",),
        "title": "Endurance score, hill score (strength / endurance)",
        "y": "Endurance",
        "y2": "Hill",
        "height": 180,
        "plots": [
            ("custom", "GarminEnduranceScore", "Endur.", "line", "fill_in", None, "#1F77B400", "#1F77B4FF", "",
             "endurance"),
            ("custom", "GarminHillScore", "Hill", "line", "fill_in", None, "#FF7F0E00", "#FF7F0EFF", "", "hill"),
            ("custom", "GarminHillStrength", "HillStr", "dot", "none", None, "#FF7F0E66", "#FF7F0E88", "", "hill"),
            ("custom", "GarminHillEndurance", "HillEnd", "dot", "none", None, "#FFBB7866", "#FFBB7888", "", "hill"),
        ],
    },
    {
        "name": "Garmin Bridge: VO2max & fitness age",
        "aliases": ("Garmin VO2max & fitness age",),
        "title": "Daily VO2max estimate (Garmin wellness: run / bike), fitness age",
        "y": "ml/kg/min",
        "y2": "Years",
        "height": 180,
        "plots": [
            ("native", "vo2max", "vo2max", "dec1", "VO2run", "line", "fill_in", None, "#1F77B400", "#1F77B4FF"),
            ("custom", "GarminVO2MaxCycling", "VO2bike", "line", "fill_in", None, "#9467BD00", "#9467BDFF", "",
             "vo2max"),
            # fill_in carries the last value over days without one, so the legend never shows "?"
            # (a moving average would smooth the steps but leaves the legend empty on such days).
            ("custom", "GarminFitnessAge", "FitAge", "line", "fill_in", None, "#7F7F7F00", "#7F7F7FFF", "", "years"),
        ],
    },
    {
        # Each prediction gets its own axis: two series per chart, two scales per chart. Sharing
        # one axis flattens every line to the edge, since the four times differ by a factor of ten.
        "name": "Garmin Bridge: Race predictions 5K / 10K",
        "aliases": ("Garmin race predictions",),
        "title": "Predicted race times: 5K (left axis), 10K (right axis)",
        "y": "5K",
        "y2": "10K",
        "height": 150,
        "plots": [
            ("custom", "GarminPredicted5KSeconds", "5K", "line", "fill_in", None, "#2CA02C00", "#2CA02CFF", "",
             "5k", "interval_time"),
            ("custom", "GarminPredicted10KSeconds", "10K", "line", "fill_in", None, "#1F77B400", "#1F77B4FF", "",
             "10k", "interval_time"),
        ],
    },
    {
        "name": "Garmin Bridge: Race predictions half / marathon",
        "aliases": ("Garmin race predictions (half / marathon)",),
        "title": "Predicted race times: half marathon (left axis), marathon (right axis)",
        "y": "Half",
        "y2": "Marathon",
        "height": 150,
        "plots": [
            ("custom", "GarminPredictedHalfSeconds", "Half", "line", "fill_in", None, "#FF7F0E00", "#FF7F0EFF", "",
             "half", "interval_time"),
            ("custom", "GarminPredictedMarathonSeconds", "Mara.", "line", "fill_in",
             None, "#D6272800", "#D62728FF", "", "marathon", "interval_time"),
        ],
    },
    {
        "name": "Garmin Bridge: Intensity minutes & sweat loss",
        "aliases": ("Garmin activity & hydration",),
        "title": "Intensity minutes (left axis) and estimated sweat loss (right axis)",
        "y": "Minutes",
        "y2": "Litres",
        "height": 180,
        "plots": [
            ("custom", "GarminIntensityModerateMinutes", "Mod", "bars", "none", None, "#1F77B466", "#1F77B4FF", "im",
             "minutes"),
            ("custom", "GarminIntensityVigorousMinutes", "Vig", "bars", "none", None, "#D6272866", "#D62728FF", "im",
             "minutes"),
            ("custom", "GarminSweatLossLitres", "Sweat", "line", "none", None, "#FF7F0E00", "#FF7F0EFF", "", "L"),
        ],
    },
    {
        "name": "Garmin Bridge: Hydration & sweat loss",
        "aliases": ("Garmin hydration & sweat loss",),
        "title": "Estimated sweat loss per day; hydration if you log it (litres)",
        "y": "Litres",
        "height": 160,
        "plots": [
            ("custom", "GarminSweatLossLitres", "Sweat", "bars", "none", None, "#FF7F0E66", "#FF7F0EFF", "", "L"),
            ("native", "hydration_volume", "L", "dec1", "Hydr.", "line", "none", None, "#17BECF40", "#17BECFFF"),
        ],
    },
    {
        # Native-only charts (no custom field to wait for): the bridge fills these wellness fields.
        "name": "Garmin Bridge: SpO2 & respiration",
        "title": "Overnight SpO2 (left axis) and respiration rate (right axis) with 7-day averages",
        "y": "SpO2 %",
        "y2": "Breaths / min",
        "height": 180,
        "plots": [
            ("native", "spo2", "spo2", "dec1", "SpO2", "bars", "none", None, "#1F77B44D", "#1F77B4FF"),
            ("native", "spo2", "spo2", "dec1", "SpO2 7d", "line", "moving_avg", 7, "#1F77B400", "#1F77B4FF"),
            ("custom", "GarminSleepSpO2Lowest", "Low", "dot", "none", None, "#17BECF66", "#17BECFFF", "", "spo2"),
            ("native", "respiration", "/min", "dec1", "Resp.", "dot", "none", None, "#D6272866", "#D62728FF"),
            ("native", "respiration", "/min", "dec1", "Resp 7d", "line", "moving_avg", 7, "#D6272800", "#D62728FF"),
        ],
    },
    {
        "name": "Garmin Bridge: Sleep score, sleeping HR & stress",
        "title": "Sleep score and sleep stress (left axis), sleeping and resting HR (right axis)",
        "y": "Score",
        "y2": "bpm",
        "height": 180,
        "plots": [
            ("native", "sleep_score", "sleep_score", "dec0", "Score", "bars", "none", None, "#1F77B44D", "#1F77B4FF"),
            ("custom", "GarminSleepStressAvg", "Stress", "line", "none", None, "#FF7F0E00", "#FF7F0EFF", "",
             "sleep_score"),
            ("native", "avg_sleeping_hr", "bpm", "dec0", "SleepHR", "line", "none", None, "#D6272800", "#D62728FF"),
            ("native", "resting_hr", "bpm", "dec0", "RestHR", "line", "none", None, "#9467BD00", "#9467BDFF"),
        ],
    },
    {
        "name": "Garmin Bridge: Steps",
        "title": "Steps per day with a 7-day average",
        "y": "Steps",
        "height": 160,
        "plots": [
            ("native", "steps", "steps", "dec0", "Steps", "bars", "none", None, "#2CA02C4D", "#2CA02CFF"),
            ("native", "steps", "steps", "dec0", "7d avg", "line", "moving_avg", 7, "#2CA02C00", "#2CA02CFF"),
        ],
    },
    {
        "name": "Garmin Bridge: Body composition",
        "title": "Weight: daily weigh-ins and 7-day average (left axis); body fat 7-day average (right axis)",
        "y": "kg",
        "y2": "Body fat %",
        "height": 160,
        "plots": [
            # daily weigh-ins as faint dots, the 7-day average as the line (scale noise smoothed)
            ("native", "weight", "weight", "dec1", "Weight", "dot", "none", None, "#7F7F7F66", "#7F7F7F99"),
            ("native", "weight", "weight", "dec1", "Wt 7d", "line", "moving_avg", 7, "#7F7F7F00", "#7F7F7FFF"),
            ("native", "body_fat", "percent", "dec1", "Fat %", "line", "moving_avg", 7, "#8C564B00", "#8C564BFF"),
        ],
    },
    {
        "name": "Garmin Bridge: Skin temperature",
        "aliases": ("Garmin skin temperature",),
        "title": "Overnight skin temperature deviation (°C)",
        "y": "°C",
        "height": 140,
        "plots": [
            ("custom", "GarminSkinTempDeviationC", "SkinΔ", "bars", "none", None, "#E377C266", "#E377C2FF", ""),
            ("custom", "GarminSkinTempDeviationC", "Skin7d", "line", "moving_avg", 7, "#E377C200", "#E377C2FF", ""),
        ],
    },
    {
        "name": "Garmin Bridge: HRV detail",
        "aliases": ("HRV detail (Garmin)",),
        "title": "Overnight HRV, 5-min high, 7-day average",
        "y": "ms",
        "height": 180,
        "plots": [
            ("native", "hrv", "ms", "dec0", "HRV", "bars", "none", None, "#1F77B44D", "#1F77B4FF"),
            ("custom", "GarminHRV5MinHigh", "5min", "dot", "none", None, "#9467BD66", "#9467BDFF", "", "ms"),
            ("custom", "GarminHRV7DayAvg", "7d avg", "line", "fill_in", None, "#2CA02C00", "#2CA02CFF", "", "ms"),
            ("custom", "GarminHRVBaselineLow", "Base lo", "line", "fill_in", None, "#7F7F7F00", "#7F7F7F99", "", "ms"),
            ("custom", "GarminHRVBaselineHigh", "Base hi", "line", "fill_in", None, "#7F7F7F00", "#7F7F7F99", "", "ms"),
        ],
    },
    {
        "name": "Garmin Bridge: Health Snapshot",
        "title": "First Health Snapshot of the day: HRV as RMSSD and SDRR (left axis), heart rate and "
                 "respiration (right axes)",
        "y": "ms",
        "y2": "bpm",
        "height": 180,
        "plots": [
            ("custom", "GarminSnapshotRMSSD", "RMSSD", "dot", "none", None, "#1F77B4AA", "#1F77B4FF", "", "ms"),
            ("custom", "GarminSnapshotSDRR", "SDRR", "dot", "none", None, "#9467BDAA", "#9467BDFF", "", "ms"),
            ("custom", "GarminSnapshotHR", "HR", "dot", "none", None, "#D62728AA", "#D62728FF", "", "bpm"),
            ("custom", "GarminSnapshotRespiration", "Resp.", "dot", "none", None, "#2CA02CAA", "#2CA02CFF", "",
             "/min", "dec1"),
        ],
    },
]


def _plot(index: int, spec: tuple, inputs: dict[str, dict]) -> dict | None:
    kind = spec[0]
    if kind == "custom":
        _, code, text, ptype, agg, days, fill, stroke, stack = spec[:9]
        scale = spec[9] if len(spec) > 9 else None       # one y axis per distinct scale
        item = inputs.get(code)
        if item is None:
            return None                       # field not created yet: plot would be empty
        decimals = any(k in code for k in ("Litres", "TempDeviation", "VO2Max", "FitnessAge"))
        filt = spec[10] if len(spec) > 10 else ("dec1" if decimals else "dec0")
        plot = {"id": index, "field": code, "filter": filt, "customInput": item,
                "text": text, "title": item.get("name") or code, "type": ptype, "agg": agg,
                "fill": fill, "stroke": stroke, "strokeWidth": 1, "radius": 3, "gauge": True,
                "scale": scale, "stack": stack, "band": 0, "min": None, "extras": [], "filters": [],
                "markerValue": "right-inline"}
    else:
        _, field, scale, filt, text, ptype, agg, days, fill, stroke = spec
        plot = {"id": index, "field": field, "filter": filt, "scale": scale, "text": text,
                "title": text, "type": ptype, "agg": agg, "fill": fill, "stroke": stroke,
                "strokeWidth": 1, "radius": 3, "gauge": True, "stack": "", "band": 0,
                "extras": [], "filters": [], "markerValue": "right-inline"}
    if agg == "moving_avg":
        plot["aggArgs"] = {"days": days}
    return plot


BRIDGE_MARK = "Created by garmin-intervals-bridge for the values it syncs: https://forum.intervals.icu/t/132863"
# The same charts in Intervals' chart library, shared by the bridge's author: a chart the bridge
# makes is a copy of its library chart (`from_id`, as Intervals' own "Copy" does), so the library
# counts its users. A later change to the library chart does not reach the copies.
LIBRARY_IDS = {
    "Garmin Bridge: Readiness & recovery": 1268942,
    "Garmin Bridge: Sleep stages": 1268943,
    "Garmin Bridge: Stress & Body Battery": 1268944,
    "Garmin Bridge: Nutrition intake vs. burn": 1268945,
    "Garmin Bridge: Energy balance (weekly)": 1269015,
    "Garmin Bridge: Energy balance (monthly)": 1269016,
    "Garmin Bridge: Nutrition macros": 1268958,
    "Garmin Bridge: Endurance & hill scores": 1268997,
    "Garmin Bridge: VO2max & fitness age": 1268946,
    "Garmin Bridge: Race predictions 5K / 10K": 1268998,
    "Garmin Bridge: Race predictions half / marathon": 1269005,
    "Garmin Bridge: Intensity minutes & sweat loss": 1268947,
    "Garmin Bridge: Hydration & sweat loss": 1268972,
    "Garmin Bridge: SpO2 & respiration": 1269043,
    "Garmin Bridge: Sleep score, sleeping HR & stress": 1269044,
    "Garmin Bridge: Steps": 1269045,
    "Garmin Bridge: Body composition": 1269046,
    "Garmin Bridge: Skin temperature": 1268948,
    "Garmin Bridge: HRV detail": 1268949,
    "Garmin Bridge: Health Snapshot": 1270871,
}


def is_bridge_item(item: dict) -> bool:
    """Charts the bridge made, whatever wording the mark had when they were created."""
    return str(item.get("description") or "").startswith("Created by garmin-intervals-bridge")


_PLOT_KEYS = ("field", "text", "type", "agg", "filter", "stack", "fill", "stroke", "aggArgs", "scale")


def _normalise(plots: list[dict]) -> list[tuple]:
    """What makes a plot definition: the keys the bridge sets, minus the embedded item."""
    return [tuple((k, str(p.get(k))) for k in _PLOT_KEYS) for p in plots]


def plan_charts(custom_items: list[dict]) -> dict:
    """Charts to create or complete.

    A chart is created when no chart of that name exists and at least one of
    its fields does. A chart the bridge created earlier (recognised by its
    description) is updated when fields it lacked have appeared since, e.g.
    after a backfill with every endpoint. Charts the athlete made themselves
    are never touched, even if the name matches.
    """
    existing = {it.get("name"): it for it in custom_items if it.get("type") == "FITNESS_CHART"}
    inputs = {it["content"]["code"]: it for it in custom_items
              if it.get("type") == "INPUT_FIELD" and isinstance(it.get("content"), dict)
              and it["content"].get("code")}
    create, update, skipped = [], [], {}
    for chart in CHARTS:
        plots = [p for i, spec in enumerate(chart["plots"], 1) if (p := _plot(i, spec, inputs))]
        if chart.get("stack"):
            for plot in plots:
                plot["stack"] = chart["stack"]
        missing = [spec[1] for spec in chart["plots"] if spec[0] == "custom" and spec[1] not in inputs]
        present = existing.get(chart["name"])
        if present is None:
            # The same chart under an earlier name, if it is ours: rename it instead of adding one.
            present = next((existing[a] for a in chart.get("aliases", ())
                            if a in existing and is_bridge_item(existing[a])), None)
        if present is not None:
            if not is_bridge_item(present):
                skipped[chart["name"]] = "exists and was not created by the bridge"
                continue
            current_plots = (present.get("content") or {}).get("plots", [])
            have = {p.get("field") for p in current_plots}
            gained = [p["field"] for p in plots if p["field"] not in have]
            renamed = present.get("name") != chart["name"]
            if _normalise(current_plots) == _normalise(plots) and not gained and not renamed:
                skipped[chart["name"]] = "already up to date"
                continue
            content = dict(present.get("content") or {})
            content["plots"] = plots
            content["name"] = chart["name"]
            content["title"] = chart["title"]
            content["yAxisLabel"] = chart["y"]
            content["y2AxisLabel"] = chart.get("y2")
            update.append({"id": present["id"], "name": chart["name"], "type": "FITNESS_CHART",
                           "visibility": present.get("visibility", "PRIVATE"),
                           "description": BRIDGE_MARK, "content": content,
                           "_gained_fields": gained, "_missing_fields": missing,
                           "_renamed_from": present.get("name") if renamed else None})
            continue
        if not plots:
            skipped[chart["name"]] = f"none of its fields exist yet: {missing}"
            continue
        content = {"id": secrets.token_hex(4), "name": chart["name"], "title": chart["title"],
                   "plots": plots, "height": chart["height"], "yAxisLabel": chart["y"],
                   "yAxisMin": None, "yAxisMax": None, "y2AxisLabel": chart.get("y2"), "y2AxisMin": None,
                   "y2AxisMax": None, "y3AxisMin": None, "y3AxisMax": None, "stackTo100Percent": None}
        item = {"name": chart["name"], "type": "FITNESS_CHART", "visibility": "PRIVATE",
                "description": BRIDGE_MARK, "content": content, "_missing_fields": missing}
        if chart["name"] in LIBRARY_IDS:
            item["from_id"] = LIBRARY_IDS[chart["name"]]
        create.append(item)
    return {"create": create, "update": update, "skipped": skipped}


def setup_charts(intervals: Any, *, apply: bool) -> dict:
    plan = plan_charts(intervals.custom_items())
    out = {"apply": apply, "skipped": plan["skipped"],
           "charts": [{"name": c["name"], "plots": [p["field"] for p in c["content"]["plots"]],
                       "fields_missing": c["_missing_fields"]} for c in plan["create"]],
           "updates": [{"name": c["name"], "id": c["id"], "adds": c["_gained_fields"],
                        "fields_missing": c["_missing_fields"], "renamed_from": c["_renamed_from"]}
                       for c in plan["update"]],
           "created": [], "updated": []}
    if apply:
        for chart in plan["create"]:
            body = {k: v for k, v in chart.items() if not k.startswith("_")}
            result = intervals.create_custom_item(body)
            out["created"].append({"name": chart["name"], "id": (result or {}).get("id")})
        for chart in plan["update"]:
            body = {k: v for k, v in chart.items() if not k.startswith("_")}
            intervals.update_custom_item(chart["id"], body)
            out["updated"].append({"name": chart["name"], "id": chart["id"], "adds": chart["_gained_fields"]})
        out["reindexed"] = assign_indexes(intervals)
    return out


def assign_indexes(intervals: Any) -> list[int]:
    """Give the bridge's charts unique indexes. POST leaves new items at index 0 and
    Intervals' chart picker does not list colliding items; the UI reindexes on its own
    creations, the API client has to do it itself."""
    items = intervals.custom_items()
    taken = [it.get("index") or 0 for it in items]
    top = max(taken) if taken else 0
    seen: set[int] = set()
    fix = []
    for it in sorted((i for i in items if is_bridge_item(i)), key=lambda i: i.get("id") or 0):
        idx = it.get("index") or 0
        if idx == 0 or idx in seen:
            top += 1
            it = dict(it)
            it["index"] = top
            fix.append(it)
        seen.add(it["index"])
    if fix:
        intervals.reorder_custom_items(fix)
    return [it["id"] for it in fix]
