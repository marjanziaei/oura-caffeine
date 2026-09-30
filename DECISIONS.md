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
