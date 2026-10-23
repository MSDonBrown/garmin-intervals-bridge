# Experimental: training advice from the HRV trend

ImReady4 judges each morning's single values. The
studies that guided endurance training by HRV mostly judged the trend of a week.
This experiment calculates such a trend advice and writes it into a field of its
own, **`HRVTrendAdvice`**, next to ImReady4's `TrainingAdvice` – so both can be
watched side by side for a few weeks before anyone decides which one to follow.
It never touches `TrainingAdvice`.

It is **off by default** and marked experimental: its rules come from the
literature below, but no study has validated this exact combination, and a
watch's night values differ from the morning readings most studies used.

## What it does

For a day, with x = ln(rMSSD) from the wellness field `hrv` and h = the night's
average heart rate (`avgSleepingHR`):

- **Week:** the last 7 days, the day included; at least 5 nights measured.
- **Normal range:** the 60 days before that week (at least 30 nights), mean ±
  0.5 standard deviations – the "smallest worthwhile change" band of the
  studies.
- **Rules,** the first that fits:

| Advice | When |
| --- | --- |
| 1 Rest | today's HRV below −1 SD **and** today's night HR above +2 SD (acute: HRV down, HR well up) |
| 1 Rest | the week's HRV below its range **and** the week's night HR above its range |
| 2 LIT | the week's HRV below its range |
| 2 LIT | today's HRV below −2 SD, or today's and yesterday's below −1 SD (acute drop) |
| 3 Planned | everything else – within the normal range the studies trained as planned, hard sessions included |

- **HIT** (4) is not used: the studies have no "more than planned" state.
- A week with HRV above and night HR below the range stays **Planned** with a
  note. One study (Vesterinen 2016) treated any value outside the band as a
  reason to go easy, but this pattern is mostly good form, and the evidence for
  "parasympathetic saturation" is mixed.
- `HRVTrendCV` holds the week's coefficient of variation of ln(rMSSD) in % – a
  stability marker (Plews 2012); a sudden rise or a steady fall is worth a look.
- Without enough night heart rate (Garmin reports it since about August 2025),
  the rules that need it are left out and HRV alone decides.

## Turning it on

```bash
garmin-intervals-bridge hrvtrend                                 # today, dry run
garmin-intervals-bridge hrvtrend --from 2026-01-01 --compare     # next to ImReady4's advice
garmin-intervals-bridge hrvtrend --from 2026-01-01 --apply       # creates the two fields, fills the days
```

Then set `BRIDGE_HRVTREND_ENABLED=true` (`-e` for a container, or the settings file): each morning the
bridge writes the trend advice the same way – Intervals requests only, an empty
field only, never a locked day, read back, once a day. The bridge creates the two
wellness fields itself (a select with 1 Rest, 2 LIT, 3 Planned, 4 HIT, and a
numeric `HRVTrendCV` in %); add them to your `TrainingAdvice` chart in Intervals
to see both advices together.

`--compare` shows both distributions, how often each changes from day to day,
and every pairing (ImReady4/trend). On the reference account, 650 days since
January 2025: the trend changed its advice on 79 days, ImReady4 on 262; the
trend said Rest on 45 days, LIT on 79 and Planned on 526 (ImReady4: 27, 86, 480
and HIT on 52), and 68 of ImReady4's 113 Rest/LIT days stood alone between
Planned/HIT days.

## Background

- Plews DJ et al. (2012) *Eur J Appl Physiol* – 7-day rolling ln rMSSD against
  the smallest worthwhile change; its coefficient of variation as a warning sign.
- Plews DJ et al. (2013) *Sports Med* – why weekly averages beat single values.
- Kiviniemi AM et al. (2007) *Eur J Appl Physiol* – daily guidance; lighter after
  a drop below the reference or two days of decline.
- Vesterinen V et al. (2016) *Med Sci Sports Exerc*; Javaloyes A et al. (2019)
  *Int J Sports Physiol Perform* – hard sessions while the 7-day average stays
  within the individual band.
- Nuuttila OP et al. (2022) *Med Sci Sports Exerc* – nocturnal HRV with other
  markers for individualised training.
- Le Meur Y et al. (2013) *Med Sci Sports Exerc* and the later meta-analysis on
  parasympathetic hyperactivity – why a high-HRV week is only a note here.
- Düking P et al. (2021) *J Sci Med Sport*; Granero-Gallegos A et al. (2020)
  *IJERPH* – HRV-guided training overall: small to medium benefits, fewer
  non-responders.

## Limitations

This is a comparison with your own recent past, not a diagnosis, and an
experiment: which advice suits you can only be judged against how you feel and
perform over weeks, not from the numbers alone.
