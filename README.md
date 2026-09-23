# Riftbound Hype Index

An unbounded growth index for Riftbound TCG community and market interest, built for
[nexusnights.sg](https://nexusnights.sg). It works like a price index: **base period = 100**.
The level can rise without limit (150, 300, 1000+) or fall below 100. The level moves when
real activity grows or shrinks.

It is **not** a 0–100 sentiment score.

## The formula

For each metric *m* on day *t*:

```
R_m(t) = mean of m's daily observations in the trailing window [t-W+1, t]
B_m    = mean of m's daily observations over the base period   (frozen at lock)
r_m(t) = R_m(t) / B_m

I(t) = L × Σ w_m · r_m(t)          combination: weighted_arithmetic (default)
I(t) = L × Π r_m(t)^w_m            combination: weighted_geometric
```

`L` (base level), `W` (window), weights, sources and the combination method are all set in
`config.yaml → formula`. A base period of `W` days ending on launch day gives exactly `100.00`
on launch day.

**Publication rule.** This is the only conditional in the formula. It is part of the formula
from day one and applies the same way every day. If any metric has fewer than its
`min_observations` inside the window, that day is **not published** (`status=insufficient_data`,
`index_level` empty). Weights are never redistributed. The formula has no capping, smoothing,
damping, outlier clipping or special cases. A spike in the data is a spike in the index.

### Default metrics (edit before locking)

| id | weight | source | notes |
|---|---|---|---|
| `reddit_subscribers` | 0.20 | r/RiftboundTCG `about.json` | stock metric (community size) |
| `reddit_daily_posts` | 0.20 | r/RiftboundTCG `/new` | posts created per SGT day |
| `wikipedia_pageviews` | 0.10 | Wikimedia pageviews API | UTC days, backfills 14 days |
| `youtube_daily_uploads` | 0.15 | YouTube Data API search | needs `YOUTUBE_API_KEY` |
| `official_events` | 0.15 | official Riftbound locator | worldwide events starting that day |
| `official_event_players` | 0.20 | official Riftbound locator | total players at those events |

The two official metrics come from one crawl of the event locator at
[locator.riftbound.uvsgames.com](https://locator.riftbound.uvsgames.com/events) (run on Carde.io):
`GET https://api.riftbound.uvsgames.com/api/v2/events/?game_slug=riftbound&start_date_after=…&start_date_before=…`.
The crawl counts every event worldwide of every status, and sums `starting_player_count`
(a missing count is treated as 0). It counts events from 2 days before the collection date
(`lag_days`), so stores have time to finish and report them. If the crawl returns fewer events
than the API's reported `count`, collection fails for that day instead of saving a low number.
This API is undocumented, so run `collect` once and check the numbers before locking.

**Verify each source before you lock:** check the subreddit name, the Wikipedia article title
and the YouTube query. Google Trends is left out on purpose. Trends rescales every query to its
own 0–100 range, so values pulled on different days can't be compared. Putting it in a
fixed-formula index would add drift from the data source itself.

## Guarantees against silent formula changes

1. **`formula/lock.json` is the source of truth.** Every formula version is stored there with
   its frozen base values and a SHA-256 `spec_hash`. The daily calculation reads the frozen spec,
   not `config.yaml`.
2. **Drift check.** `compute` refuses to run if `config.yaml → formula` differs from the latest
   locked version. It prints the exact diff and tells you to run `update-formula`.
3. **Tamper check.** If `lock.json` is edited by hand, its hashes stop matching and every command
   refuses to run.
4. **Every output row carries `formula_version` and `formula_hash`.** Rows that are already
   published are never rewritten. A new version can only take effect *after* the last published
   day.
5. **Sources are fingerprinted.** Each observation is stored with a `source_key`, which is a hash
   of the metric's source spec. If you swap a proxy (for example, a different subreddit), the new
   data can never mix into the old metric's series.
6. **`verify`** recomputes every published row from the raw observations using that row's
   version. It fails if anything changed: config drift, edited observations, or a hash mismatch.
   It runs in CI and in the daily job.

## Workflow

```bash
pip install -r requirements.txt

# 1. Pre-launch: collect data every day for at least `window_days` (28) days.
python -m hype_index collect                 # automatic metrics for yesterday (SGT)
python -m hype_index status                  # coverage per metric

# 2. Launch: freeze base values and lock formula v1 (one time only).
python -m hype_index lock-base --base-end 2026-10-31      # base = the 28 days ending 31 Oct

# 3. Daily (automated by .github/workflows/daily.yml at 00:30 SGT):
python -m hype_index collect
python -m hype_index compute                 # or --date D / --from A --to B
python -m hype_index verify
```

Output goes to `output/hype_index.csv` and `output/hype_index.json`. The JSON is for the site.
Its `meta.versions[].series_break` flags re-base boundaries so charts can show them.

## Changing the formula (`update-formula`)

Adding or removing a metric, changing a weight, the window, `min_observations`, the combination
method, or swapping a proxy source is a **version change**. To make one:

1. Edit `config.yaml → formula`. From this point `compute` refuses to run until step 2 is done.
2. Record the change and choose how it affects the series:

```bash
# Re-base: a new index=100 period going forward. Old rows keep their version and level.
python -m hype_index update-formula --mode rebase \
    --base-start 2027-03-04 --base-end 2027-03-31 \
    --reason "Added TikTok metric; official events reweighted" \
    --summary "add tiktok_views 0.10, official_event_players 0.20->0.15"

# Continuity: keep the level and the original base period. Valid only for neutral changes.
python -m hype_index update-formula --mode continuity --reason "..."
```

What `update-formula` does:

- It sets `formula_version` to N+1 and makes the new version effective after the last published
  day (or on `--effective-date`).
- It requires `--reason` and the rebase/continuity decision.
- **rebase:** computes new frozen base values over the new base period you give it.
- **continuity:** reuses frozen base values for unchanged metrics. Base values for new metrics
  come from the *original* base period, so that period needs data for them. It then runs a
  **neutrality check**: old vs new formula on the latest day both can compute, with a ±0.5%
  tolerance. If the check fails, the command refuses unless you pass `--accept-non-neutral`,
  and the changelog records that you did.
- It writes a changelog entry to `formula/lock.json` and regenerates `formula/CHANGELOG.md`. The
  entry records the date, the version change, the mode, the effective date, the automatic diff,
  your summary and reason, the base period, the old and new hashes, and the neutrality result.

Commit `config.yaml`, `formula/` and `output/` together.

## Tests

```bash
python -m unittest discover -s tests -v
```
