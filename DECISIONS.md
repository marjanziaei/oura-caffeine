# Decisions

A log of design decisions and the reasoning behind them. Newest entries go at the bottom.

## 1. Caffeine cutoff is 5pm

**Date:** 2026-09-30

The threshold is 5pm, not 7pm, because I rarely drink caffeine after 7 anyway. The 5 to 6pm window is where my behaviour actually varies, so that's where the signal is.

## 2. How Oura tokens are stored

**Date:** 2026-09-30

Tokens live in `.oura_tokens.json` in the project folder: git-ignored, and created with owner-only permissions (0600). A plain local file is enough for a single-user bot on my own machine; a keychain or secrets manager would add setup without a real threat it protects against here.

Oura refresh tokens are single-use, so losing the new one after a refresh means logging in again. To prevent that:
- **Save first.** The new tokens are written to disk immediately after the refresh response arrives, before they're used for anything.
- **Atomic write.** Tokens go to a temp file, which is flushed to disk and then renamed over the old one. A crash leaves either the old file or the new one, never a half-written file.
- **Lock.** Refreshing holds a file lock and re-reads the token file inside it, so two processes (e.g. the bot and a manual script) can't both spend the same refresh token.

Remaining gap: if the connection drops after Oura issues new tokens but before the response reaches me, the old token is dead and the new one was never received. The fix is simply running `python login.py` again.

## 3. Verify what the coding agent says, not just what it builds

**Date:** 2026-09-30

**What happened:** While setting up the Oura login, Claude Code reported that my .env file still contained placeholder values. I had already entered the real credentials. When I asked it to check again, it confirmed the real values were there. It had also flagged earlier that its own placeholder check wasn't working as expected.

**Decision:** Treat the agent's reports about the state of my system as claims to verify, not facts. Before acting on a surprising report, check it myself or ask the agent to re-verify.

**Why it matters:** A wrong report can send me in the wrong direction, e.g. re-entering credentials or suspecting a leak that didn't happen.

## 4. Resting heart rate comes from `lowest_heart_rate`, not `resting_heart_rate`

**Date:** 2026-09-30

**What happened:** The Oura app shows "Resting Heart Rate" in bpm. The API has a field with that exact name, but it's inside the readiness contributors and is a 1–100 score, not bpm. The bpm value is in the sleep data as `lowest_heart_rate`.

**Decision:** Use `lowest_heart_rate` from the main sleep period of each night.

**How I verified it:** Checked Oura's API spec and documentation. To confirm: compare the script's output with the app for a few nights. [I checked the numbers for 17/9 to 30/9 and they matched exactly.]

**Why it matters:** The wrong field would have produced plausible-looking numbers with no error, so the whole analysis would have been wrong without anyone noticing.

## 5. Date alignment and the two-table design

**Date:** 2026-09-30

**What happened:** My caffeine answer is about the evening of a date, but Oura labels each night with the date I *wake up*. Checked against 30 days of my real data (not reproduced here, since this repository is public): every main sleep's `day` equals the date of `bedtime_end`, whether I fell asleep before or after midnight. Oura even labels an evening nap with the next day.

Illustration with made-up example values:
- Asleep Jan 10 at 23:30, awake Jan 11 at 07:30 → labelled **Jan 11**, so it pairs with the evening of Jan 10.
- Asleep Jan 13 at 01:15 (after midnight), awake Jan 13 at 08:45 → labelled **Jan 13**, so it pairs with the evening of Jan 12, even though I fell asleep on the 13th.
- A nap on Jan 14 from 19:00 to 19:30 → labelled **Jan 15**.

**Decision:**
- Evening **D** pairs with the Oura night labelled **D + 1**. I use Oura's own `day` label rather than computing one from bedtime timestamps, because Oura already applies the rule I want and handles cases I'd get wrong.
- Two tables, because the data has two different sources and owners:
  - `oura_nights`: Oura's data, keyed by Oura's `day`. The sync may overwrite it at any time, since re-fetching gives Oura's latest numbers.
  - `daily_log`: my answers, keyed by the evening. Only I write to it. The sync's database connection is blocked from changing it by a SQLite authorizer, not just by convention.
- The pairing rule lives in exactly one place: the `nights_with_caffeine` view.
- "Unanswered" is stored explicitly as `'unanswered'` and the column rejects NULL, so a missing answer can never be mistaken for "no". Nights with no log row also show as `'unanswered'` in the view.

**Why it matters:** An off-by-one date would pair each answer with the wrong night. The analysis would still produce clean-looking numbers, just about the wrong nights. The same goes for counting unanswered days as "no", which would dilute the "no" group with nights I know nothing about.

## 6. Broken nights are combined; naps are split into before and after the night

**Date:** 2026-10-01

All times below are made-up examples, not my data.

**What happened:** Oura can split one night into several `long_sleep` periods when I'm awake for a long stretch in the middle, e.g. asleep 23:00–02:30, awake, asleep again 04:30–08:50. The sync used to keep only the longest part, without saying so. Separately, Oura records short `sleep` and `rest` periods (dozing, naps) that the sync ignored entirely.

**Decision 1, broken nights:** combine all `long_sleep` parts of an Oura day into one night: earliest start (23:00), latest end (08:50), and the lowest heart rate across the parts. Store the number of parts in `long_sleep_count`, and log a warning whenever there's more than one.
- *Why not take the longest part:* it throws away part of the night. If caffeine fragments my sleep, that's exactly the effect I'd be hiding.
- *Trade-off:* the awake gap sits inside the start-to-end span, so bedtime-to-wake time overstates time asleep. `long_sleep_count > 1` marks those nights so analysis can treat them separately.

**Decision 2, naps:** minutes asleep in an Oura day's non-`long_sleep` periods are stored in three columns:
- `pre_sleep_nap_minutes`: periods that end before the night, e.g. dozing 19:00–19:30 the evening before a 23:00 bedtime. These can affect that night, because they take the edge off my tiredness.
- `post_wake_nap_minutes`: periods that start after waking, e.g. a nap at 15:00 the next afternoon. These can't affect that night.
- `adjacent_sleep_minutes`: periods ending or starting within 30 minutes of the night, e.g. dozing 22:40–22:50 before a 23:00 bedtime. These are falling asleep or waking up, not naps.

A day without a main sleep has no "before" or "after", so its three columns stay empty and the sync logs a warning.

To total all napping in the day before night D, add night D−1's `post_wake_nap_minutes` to night D's `pre_sleep_nap_minutes`.

**Revised the same day:** the first version stored a single `nap_minutes` total, taken straight from my original request rather than chosen deliberately. But Oura labels evening dozing with the *next* day, so one total per Oura day mixed naps that could affect the night (the evening before it) with naps that couldn't (the afternoon after it). Both sit under the same label, so the analysis couldn't tell them apart. Splitting them at sync time, while the period timestamps are at hand, is cheap. Reconstructing the split later would mean storing every period.

**How I verified it:** made-up test cases gave the expected results: a two-part night, dozing inside the gap, periods exactly 30 and 30.5 minutes from the night, a deleted period, and a day with no main sleep. On all 62 nights of real data:
- the three columns add up to Oura's total for the other periods
- each column matches an independent recount from the stored bedtimes.

**Also:** older rows kept empty new columns because the sync only refreshes the last 30 days. `python sync_oura.py --days N` fills in older rows.
