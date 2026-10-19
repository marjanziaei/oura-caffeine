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

## 7. Telegram bot: dates come from the question, and only my chat is accepted

**Date:** 2026-10-01

All dates and times below are made-up examples.

**Decisions:**
- **The evening date travels with the question.** Each button carries its evening in its hidden data, e.g. `caffeine:2030-01-10:yes`, set when the question is sent. An answer tapped at 00:30 on Jan 11, or days later on an old message, is saved to Jan 10. The time of the tap is never used to pick the date.
- **21:00 is Prague wall-clock time.** The scheduled time carries the `Europe/Prague` time zone, so it stays at 21:00 local when the clocks change. The underlying UTC time moves from 19:00 to 20:00 at the October change. A plain `21:00` with no time zone would have meant 21:00 UTC: 23:00 in summer, 22:00 in winter.
- **A catch-up check every 10 minutes** covers a laptop that's asleep or a bot that isn't running at 21:00. It sends the evening's question if it's past 21:00 Prague time and the question hasn't been sent yet. After midnight it stops, so a missed evening is skipped rather than sent late with a confusing date.
- **"Asked" is recorded as the `daily_log` row itself**, created as `'unanswered'` only after Telegram confirms the message was sent. A failed send is therefore retried, and an asked-but-unanswered evening is stored explicitly, never as "no".
- **Corrections:** the buttons stay on the message, with ✓ on the current answer. Tapping another one overwrites it. The "Anything unusual" follow-up is only sent the first time an evening's caffeine question is answered, so corrections don't send it again.
- **`tag` NULL vs `'none'`:** NULL means the follow-up wasn't answered, and `'none'` means "Nothing unusual". Storing "Nothing unusual" as NULL would have repeated the unanswered-means-no mistake. The table was recreated to allow `'none'`; it had no rows yet.
- **Only my chat:** a gatekeeper handler runs before all others and drops any message or button tap whose user or chat isn't my `TELEGRAM_CHAT_ID`. Anyone can find a bot by its username, so this is required, not optional.
- **Token hygiene:** `httpx`, the HTTP library used by the Telegram library, logs request URLs, and Telegram URLs contain the bot token. Its logging is turned down, and `find_chat_id.py` never prints URLs in errors.

**How I verified it:**
- The library's real 21:00 job fires at 21:00 Prague time on every day across both clock changes (Oct 2026 and Mar 2027).
- Simulated taps against a scratch database covered:
  - a tap at 00:30 going to the previous evening
  - corrections
  - tapping the already-selected button again
  - old messages
  - strangers, and my own account in a group chat, being ignored
  - a failed send being retried

## 8. Backfilled answers are flagged

**Date:** 2026-10-05

All dates below are made-up examples.

**What happened:** The bot missed a couple of evenings (e.g. Jan 11 and Jan 12), so I filled them in by hand once, from memory, days later. This was a one-off; the bot didn't get a backfill feature.

**Decision:**
- `daily_log` has a `backfilled` column: `1` for answers entered after the fact, `0` (the default) for answers given through the bot on the evening. It rejects NULL and anything other than 0 or 1.
- Only evenings I actually remembered were filled in. Anything I didn't remember stays unanswered, never a guessed "no".
- Backfilled rows were only inserted where no row existed, so no bot answer was overwritten.

**Why it matters:** An answer recalled days later is less reliable than one given that evening. And if I remember a night *because* I slept badly, the recalled answers aren't a random sample. Flagging them lets the analysis be run with and without them; if a result only holds when they're included, it's not a result I trust.

## 9. The project moves from my laptop to a rented server in Canada

**Date:** 2026-10-19

All names, paths and times below are generic examples, not my real setup.

**What happened:** The bot only works while the laptop is awake, and it had been off for days, so many evenings went unanswered. I moved everything to a small rented OVH server, in a data centre in Canada.

**Decisions:**
- **Location:** Canada, so the data now sits outside the EU. That is a change from "only on my laptop" and is reflected in `PRIVACY.md`. **Why Canada:** the EU locations were sold out for the plan I wanted. I judged Canada acceptable because the EU recognises Canada's privacy law as adequate, and the data is only my own.
- **SSH:** key login only. A drop-in config file named `00-hardening.conf` turns off passwords and root login. The `00-` prefix matters: sshd uses the first value it reads, and the provider's own drop-in files would otherwise win. The firewall (`ufw`) allows only SSH in. The bot polls Telegram from the inside, so it needs no inbound port. Automatic security updates are on.
- **Checked before closing the old session:** a fresh key login worked, and password login and root login were both refused.
- **Services:** the bot is a systemd service (`Restart=always`, enabled at boot). I killed it on purpose to check it comes back, and later rebooted the server: the bot and the sync timer both came back on their own, and SSH hardening and the firewall were still in force.
- **Sync schedule:** a systemd timer at 13:00 and 18:00, written with `Europe/Prague` inside the timer itself (`OnCalendar=*-*-* 13:00:00 Europe/Prague`), not left to the server's clock. The server runs on UTC. 13:00 suits a late riser after the ring has synced to the app, and 18:00 catches a late sync. The sync is an upsert, so running twice is harmless. `Persistent=true` runs a missed sync after downtime.
- **The bot's 21:00 was already tied to Prague time in the code (decision 7).** On the new server it showed 21:00 CEST = 19:00 UTC now, and 21:00 CET = 20:00 UTC after the clocks change.
- **Oura token:** refresh tokens are single use, so only one machine may hold a live one (see decision 2). Order of steps: stop everything on the laptop, copy the token file, compare checksums, rename the laptop's copy to `.oura_tokens.json.moved-to-server`, and only then start anything on the server. A laptop script now stops with "no saved tokens" instead of spending the token. The Telegram bot token is the same: two machines polling it would fight, so the laptop must not run the bot again.
- **Files copied by hand** (not in git): `.env`, the token file and the database, then set to owner-only (0600), in a folder only the owner can open (0700).

**Open:** the database has no backup yet. See decision 11.

## 10. First connection: the server's host key was accepted on first use

**Date:** 2026-10-19

**What happened:** The first time I connected, SSH showed the server's host key fingerprint and I accepted it without a second check.

**Decision:** I accepted the risk. I saw the fingerprint over one network only and did not compare it against a second source such as the provider's panel. For a brand-new server with nothing on it yet, I judged that acceptable.

**What the risk is:** if someone had intercepted that first connection, my laptop would trust their key. The server would then be an impostor and could capture what I sent to it. Later connections would not reveal it, because SSH only warns when the key *changes*.

**Why it matters / how to undo it:** the first thing sent after that was my public key (harmless), but later I sent real secrets to the server. If I ever want certainty, I can compare the fingerprint in the provider's panel with the one in `~/.ssh/known_hosts` (`ssh-keygen -F <host> -l`). If they differ, I would rotate the Telegram bot token and the Oura client secret and re-run `login.py`.

## 11. Backup: the laptop pulls a copy of the database each day

**Date:** 2026-10-19

All names and times below are generic examples.

**Options considered:** (A) pull to my laptop over SSH; (B) encrypted push to cloud storage; (C) the provider's snapshot add-on.

**Decision: A.** No third party ever holds the data, and it costs nothing. The weakness is that it only runs while the laptop is on. A missed run happens when the laptop wakes up, and the 30 kept copies cover a short gap.

**How it works:**
- **Dedicated key:** the laptop uses its own SSH key with no passphrase, so a scheduled job can run unattended. On the server that key is locked with `restrict,command="..."`, so it can do exactly one thing: print a copy of the database. It cannot open a shell, forward ports or read other files. My normal key keeps its passphrase.
- **Consistent copy:** the server makes it with SQLite's `.backup`, which is safe while the bot is writing. A plain file copy could capture a half-written page. The copy goes to stdout and the temporary file is deleted straight away.
- **Checked before it's kept:** the laptop runs an integrity check and confirms both tables exist and have rows. A bad download is discarded and logged as a failure, and never replaces a good copy.
- **Retention:** dated files, newest 30 kept, in a folder only I can open (0700, files 0600). Only files with the backup's own name pattern are ever pruned.
- **Schedule:** daily at 14:00, an hour after the first sync, via a macOS launchd job. launchd has no per-job time zone, so this follows the laptop's own zone (Europe/Prague). That is a known gap against my rule that schedules name their zone. If the laptop is asleep at 14:00, the job runs when it wakes.
- **Only the database is backed up.** The token file and `.env` are not, because a restored old token would be dead anyway (single use), and the secrets can be re-created.

**How I verified it:** ran it for real; the copy passed the integrity check. Asking the backup key for a shell returned database bytes and never ran my command. The first launchd run exited with an error even though the copy succeeded: macOS `head` rejects a negative line count in the prune step. Fixed and re-run clean. The prune logic was tested on 33 empty fake files.

**Not yet tested:** restoring from a copy onto a fresh server, and the job's behaviour after a laptop reboot.

## 12. Weekly analysis: how caffeine nights are compared, and how sure the label is allowed to sound

**Date:** 2026-10-19

All numbers below are made-up examples, not my data.

**What it is:** `analysis.py` compares nights with caffeine after 5pm against nights without, for sleep score and lowest heart rate, and prints JSON. It is calculations only: no AI model, no Telegram message. It opens the database read-only, so it cannot change anything. Not deployed to the server yet.

**Which nights are used:** all evenings since 2026-10-01, from the `nights_with_caffeine` view. A night is left out, for one reason only, and the output counts each reason:
- no caffeine answer (a missing row and an `unanswered` row count the same);
- tagged alcohol, illness, travel or other;
- **caffeine answered but the "anything unusual" follow-up wasn't.** This goes one step past what I asked for. NULL means "unknown", not "nothing unusual" (decision 7), so using the night would repeat the mistake of treating a missing answer as "no".

A night with no score or heart rate is dropped from that metric only and counted.

**View change:** `nights_with_caffeine` gained `backfilled` and `pre_sleep_nap_minutes`, so the analysis never repeats the evening-to-night pairing in its own query (decision 5 says it lives in one place). `connect()` replaces an older version of the view in one transaction. The view holds no data.

**Size of a difference:** `d = (average with caffeine − average without) / pooled spread`, where the pooled spread is the usual night-to-night variation inside the two groups combined. Example: a 6-point drop in score when the spread is 4 points gives d = −1.5. It is measured inside the groups so the caffeine effect itself doesn't inflate "normal variation". With a 95% interval for the difference in the metric's own units. The interval is **Welch's**, not the pooled-variance one: the standard error is √(s₁²/n₁ + s₂²/n₂) and the degrees of freedom come from the Welch–Satterthwaite formula. I chose it because my groups will be very unequal (few caffeine nights), and a pooled interval is too narrow when the smaller group is also the more spread out. Example: 4 caffeine nights spread widely against 20 other nights spread narrowly give about 3 degrees of freedom (t ≈ 3.2), where a pooled interval would use 22 (t ≈ 2.1) and look far more certain than the data allow. The t values are computed in plain Python (no new dependency) and checked against published tables.

Note: the size `d` above still uses the pooled spread (the usual Cohen's d), so with very unequal group sizes it is dominated by the larger group. The interval is the more careful of the two; a `finding` needs both.

**The label, first match wins:**

| Condition | Label |
|---|---|
| either group has fewer than 5 nights | `hint` |
| \|d\| ≥ 0.8, both groups ≥ 10 nights, and the interval excludes zero | `finding` |
| \|d\| ≥ 0.5 | `early signal` |
| otherwise | `no clear difference` |

Why the extra conditions on `finding`: with 5 against 5 nights, a large \|d\| turns up by luck fairly often. An interval that straddles zero says the same thing from the other side. A large difference that fails them is reported as `early signal`, with the reason written out. The label is about size only; whether caffeine nights look better or worse is a separate field, and the JSON states for each metric which direction is better (higher score, lower heart rate).

**Primary and secondary:** sleep score without backfilled answers is the primary comparison. The other three (heart rate, and both metrics with all answers) are secondary. Four comparisons means one can look interesting by chance, which is why only one is the headline. For each metric the output also says whether adding the backfilled answers changes the label or the direction. That is only answerable when both views have a direction, meaning both groups have nights in both views. Otherwise `comparable` is false and `changes_result` is null (not true), so a view with an empty group is never reported as a changed result. I found this by running the analysis on a backup of my real data: with only backfilled caffeine nights, the strict view had none, and the first version wrongly said "changed".

**Evening dozing:** average `pre_sleep_nap_minutes` per group. Flag `differs` at 10 or more minutes apart, with at least 5 measured nights per group; otherwise `similar` or `not enough data`. An unmeasured night is not counted as 0 minutes. The flag doesn't adjust the other results.

**Last 7 days:** the 7 evenings before today, summarised (counts, left-out reasons, averages), not tested. Nearly always a `hint`.

**Known limitation:** I'm trying to improve my health overall, so my numbers may drift upwards over time. If caffeine nights happen to cluster early or late in the period, that trend could be mistaken for a caffeine effect, or hide one. Nothing here removes a time trend. The JSON carries this caveat, and any text built from it should say so.

**How I verified it:** 25 tests on made-up data in scratch databases: a week with one caffeine night; a clear difference; no difference; a week where every night is tagged; backfilled answers flipping the result (no difference on the evening, a clear one with late answers added); the label boundaries (just under and over each threshold); each way of being left out; unmeasured dozing, and the backfill check when a view has no caffeine nights (it must say "cannot compare"). One test uses very unequal groups and spreads (4 wide against 20 narrow) and checks the interval against Welch's formulas by hand; the earlier worked example had equal groups, where Welch and pooled give the same answer, so on its own it could not have caught a pooled interval. Swapping in a pooled interval makes only the new test fail. I also deliberately broke the code five ways (the 10-night gate, the interval condition, the better/worse direction, the backfilled filter, the sign of the difference) and each was caught. My real database file was byte-identical before and after.

**Not yet done:** running it on the server, and turning the JSON into text.

## 13. The weekly message: the code states the result, the AI only explains, and every number is checked

**Date:** 2026-10-19 (revised the same day)

All numbers and wording below are made-up examples, not my data.

**What it is:** `verdict.py` turns the analysis JSON (decision 12) into a short weekly message, and `weekly_message.py` builds it and sends it to my Telegram chat. By default it only prints; sending needs `--send`.

**The message has two layers.**
1. **Written by the code, straight from the JSON:**
   - the confidence line: `Confidence: HINT (sleep score, answers given on the evening only): a first look only; ...`, one fixed sentence per label;
   - the main result: e.g. `Sleep score was 8 points lower on nights with caffeine after 5pm, which is worse. The 95% range for the gap is 5 to 11 points lower. Based on 12 nights with caffeine after 5pm and 14 without.` When the range includes zero it says so ("runs from 3 points lower to 2 points higher, so no difference at all cannot be ruled out"); with no range, "Not enough data yet for a 95% range"; with nothing to compare, "Not enough data yet to compare sleep score.";
   - one line for the secondary result (lowest heart rate), also from the JSON. I added this line myself so that heart rate, which sits in the data, still appears without the AI having to state a direction.
2. **Written by Claude (Claude Haiku 5.5, the current small model, low effort):** at most about 80 words, 2 to 4 short sentences, everyday words. It says what the result can and cannot tell me for its label, mentions evening dozing only if flagged and the answers I filled in later only if they change the result, and always adds that this is a link, not a cause, and that a steady trend in my health over time could be mistaken for a caffeine effect. It must not repeat the numbers or the direction, and is told to use no numbers at all.

**Why the split:** an AI sentence about direction or certainty is the part that can go quietly wrong. The code now owns the confidence, the direction and the range. The AI never gets to make a result sound firmer, or flip better and worse.

**The direction check:** the model's text is rejected if it contains a word for the *opposite* of what the code said about the main comparison. Which words count depends on the result and on which way is better:
- caffeine nights look worse (higher score is better): better, improved, boost, higher, rose ... are rejected;
- caffeine nights look better: worse, harm, decline, lower, dropped ... are rejected;
- no difference, or no data: every direction word is rejected, since any direction would be invented.

Words that restate the code's own direction are not rejected, and the prompt tells the model not to use them. "More nights would help" and "a steady trend" are fine; the trend sentence is worded without better or worse on purpose. This is a word list: a rare synonym, or a wrong claim about something other than direction, would pass.

**What is sent to the API:** only aggregate statistics, from a fixed list of fields, in everyday words (`answers_filled_in_later_among_used`, not the internal `backfilled` names): group sizes and averages, the difference, the range, the labels, the flags, and how many nights are needed. No dates, bedtimes, evening dates or individual nights, and the call is refused if anything date-shaped or time-shaped is in the payload. The same limit is in `PRIVACY.md`.

**The number check:**
- Every number in the model's text must come from the JSON. **Rounding rule:** a number is accepted if it equals some number in the JSON after rounding that number half-up to as many decimals as the text shows, ignoring the minus sign. Example: a data value of 8.15 may be written 8.15, 8.2 or 8, but not 8.1, 8.25 or 9.
- Spelled-out numbers (two, three, half, twice ...) are rejected. "One" and "zero" are allowed: "one" is too common in plain English, and "the range includes zero" is ordinary wording.
- Also rejected: dates and times; more than about 100 words or 900 characters; overclaiming words (proves, definitely ...); medical or advice wording; markdown and lists; the reader-unfriendly terms (backfilled, in view, JSON, effect size ...); and "finding" or "significant" unless the label really is `finding`.
- **Retry once, then fall back.** If the check fails, the model gets one more try and is told exactly what was wrong. If that fails too, or the API is down, or the model stops early, the message is built by code from the JSON with no AI. The template is tested against the same number check, so it never prints a number that is not in the data. I always get a correct message.

**Lesson from the first real run:** the very first version of the tightened prompt was rejected twice by my own checker (the model wrote a number as a word, then used the word "finding" because a data field was named after it). The fallback message was correct and complete, which is what it is for. I then told the model to use no numbers at all, never to say "finding" or "signal", and renamed the field. The next run passed on the first try. The checker caught a real problem before it reached my phone.

**Limits of the guard, stated plainly:** it checks that numbers exist, not that they are attached to the right claim, and it checks direction only for the main comparison. A real number swapped between the groups, or a wrong claim about heart rate, would pass. A made-up number can also pass if it happens to round to some other number in the data. The word lists are lists, not understanding.

**Schedule:** Sundays 13:30, with the zone inside the timer (`OnCalendar=Sun *-*-* 13:30:00 Europe/Prague`), 30 minutes after the 13:00 sync. Checked on the server with `systemd-analyze calendar`: the next run is 12:30 UTC on the Sunday the clocks change, which is 13:30 Prague time, as intended. The unit files are in `systemd/`.

**Sending:** plain text, no markdown parsing. If the send fails, the error says what kind of failure it was but never includes the URL, because the Telegram URL contains the bot token (decision 7).

**How I tested it:** 52 tests for the message (77 with the analysis tests), with a fake API only; any attempt to create the real client during the tests fails. They cover: a normal answer and the exact layout; an invented number caught, retried, then either fixed or replaced by the template (exactly two calls, never three); a fake answer that flips the direction, for caffeine worse, better and no difference, rejected, retried and then replaced by a template that keeps the code's direction; the API down, timing out, or refusing; the opposite words depending on whether higher or lower is better; every wording of the code's result sentence; no dates or nights in the payload, and a date forced into it stopping the send; the rounding rule at its edges; the label line unchangeable by the model; the template on nine situations; and Telegram errors not leaking the token. I also broke the code 15 more ways on purpose (direction check off, wrong word family, lower and higher swapped, no-direction case lenient, better and worse wrong in the result sentence, range branch wrong, "not enough data" text removed, word limit raised, character limit removed, jargon check off, prompt limit changed, AI body missing the code sentence, jargon in the payload) and each was caught. One of them (the character limit) first survived, so I added a test for it.

