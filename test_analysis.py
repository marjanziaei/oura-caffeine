"""Tests for analysis.py. Made-up data only, in scratch databases in a temp folder.

Run:  .venv/bin/python -m unittest test_analysis -v
Nothing here opens the real database (sleep_caffeine.db); one test checks it was left untouched.
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import analysis
import db

START = date(2030, 1, 1)          # made-up dates, so nothing here can be mistaken for real data
AS_OF = date(2030, 2, 20)


REAL_DB_BEFORE = db.DB_FILE.stat().st_mtime_ns if db.DB_FILE.exists() else None


def tearDownModule():
    """The real database must be exactly as it was before the tests ran."""
    after = db.DB_FILE.stat().st_mtime_ns if db.DB_FILE.exists() else None
    assert after == REAL_DB_BEFORE, "the real database was modified by the tests"


def evening(i):
    return (START + timedelta(days=i)).isoformat()


class Scratch:
    """A scratch database with helpers to add made-up nights and answers."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "scratch.db"
        assert self.path != db.DB_FILE
        self.conn = db.connect(self.path)
        self.next = 0

    def add(self, caffeine, score, hr=55, nap=0, tag="none", backfilled=0, evening_date=None, log=True):
        """One evening with its Oura night (labelled the next day). caffeine: 'yes' | 'no' | 'unanswered'."""
        ev = evening_date or evening(self.next)
        self.next += 1
        night = (date.fromisoformat(ev) + timedelta(days=1)).isoformat()
        self.conn.execute(
            "INSERT INTO oura_nights (day, sleep_score, lowest_heart_rate, fetched_at, pre_sleep_nap_minutes) "
            "VALUES (?,?,?,?,?)", (night, score, hr, "2030-01-01T00:00:00+00:00", nap))
        if log:
            self.conn.execute(
                "INSERT INTO daily_log (evening_date, caffeine_after_5pm, tag, backfilled) VALUES (?,?,?,?)",
                (ev, caffeine, tag, backfilled))
        self.conn.commit()

    def run(self, since=evening(0), as_of=AS_OF):
        with closing(analysis.open_read_only(self.path)) as ro:
            result = analysis.analyse(ro, since, as_of)
        json.dumps(result, allow_nan=False)          # must always be plain JSON
        return result

    def close(self):
        self.conn.close()
        self.dir.cleanup()


def comp(result, metric, view):
    return next(c for c in result["comparisons"] if c["id"] == f"{metric}:{view}")


def spread(centre, n, step=1):
    """n made-up values around a centre: centre-2*step, centre-step, centre, centre+step, ..."""
    return [centre + step * ((i % 5) - 2) for i in range(n)]


class StatisticsTests(unittest.TestCase):
    def test_t_critical_matches_published_table(self):
        # Two-sided 95% critical values from standard t tables.
        for df, expected in [(1, 12.706), (2, 4.303), (5, 2.571), (8, 2.306), (10, 2.228),
                             (30, 2.042), (120, 1.980)]:
            self.assertAlmostEqual(analysis.t_critical(df), expected, places=2, msg=f"df={df}")

    def test_welch_interval_worked_example(self):
        # [1..5] against [3..7]: difference -2, each variance 2.5, standard error 1, 8 degrees of freedom.
        stats, d = analysis.compare([1, 2, 3, 4, 5], [3, 4, 5, 6, 7])
        self.assertAlmostEqual(stats["difference"], -2.0)
        self.assertAlmostEqual(stats["pooled_sd"], 2.5 ** 0.5)
        self.assertAlmostEqual(d, -2.0 / 2.5 ** 0.5)
        self.assertAlmostEqual(stats["ci95"]["low"], -2 - 2.306, places=2)
        self.assertAlmostEqual(stats["ci95"]["high"], -2 + 2.306, places=2)
        self.assertFalse(stats["ci95"]["excludes_zero"])

    def test_interval_is_welch_not_pooled_for_very_unequal_groups(self):
        # 4 caffeine nights with a wide spread against 20 other nights with a very narrow one.
        # Here the two methods disagree a lot: pooled would use 22 degrees of freedom (t = 2.07);
        # Welch's degrees of freedom are about 3 (t = 3.18), so its interval must be clearly wider.
        caffeine = [60, 70, 80, 90]
        other = [79, 80, 81] * 6 + [79, 80]
        self.assertEqual((len(caffeine), len(other)), (4, 20))
        stats, _ = analysis.compare(caffeine, other)

        n1, n2 = len(caffeine), len(other)
        m1, m2 = sum(caffeine) / n1, sum(other) / n2
        v1 = sum((x - m1) ** 2 for x in caffeine) / (n1 - 1)
        v2 = sum((x - m2) ** 2 for x in other) / (n2 - 1)
        se = (v1 / n1 + v2 / n2) ** 0.5                                   # not pooled
        df = (v1 / n1 + v2 / n2) ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
        self.assertAlmostEqual(df, 3.0, delta=0.05)

        ci = stats["ci95"]
        half = (ci["high"] - ci["low"]) / 2
        self.assertAlmostEqual((ci["low"] + ci["high"]) / 2, m1 - m2)
        # t for 3 and 4 degrees of freedom are 3.182 and 2.776, so for df just over 3 it lies between.
        self.assertTrue(2.776 < half / se < 3.19, half / se)
        self.assertIn(f"{df:.1f} degrees of freedom", ci["method"])

        pooled_var = ((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2)
        pooled_half = analysis.t_critical(n1 + n2 - 2) * (pooled_var * (1 / n1 + 1 / n2)) ** 0.5
        self.assertGreater(half, 1.5 * pooled_half)                       # a pooled interval would be far narrower
        self.assertFalse(ci["excludes_zero"])                             # and Welch correctly cannot rule out zero

    def test_welch_interval_flips_with_which_group_is_small(self):
        # Swap which group is small and wide: the Welch interval stays the same width.
        wide, narrow = [60, 70, 80, 90], [79, 80, 81] * 6 + [79, 80]
        a, _ = analysis.compare(wide, narrow)
        b, _ = analysis.compare(narrow, wide)
        self.assertAlmostEqual(a["ci95"]["high"] - a["ci95"]["low"], b["ci95"]["high"] - b["ci95"]["low"])
        self.assertAlmostEqual(a["difference"], -b["difference"])

    def test_no_spread_does_not_crash(self):
        stats, d = analysis.compare([60] * 6, [50] * 6)
        self.assertEqual(d, float("inf"))                    # infinite, positive
        self.assertIsNone(stats["effect_size_d"])             # never an infinity in the output
        self.assertTrue(stats["ci95"]["excludes_zero"])
        self.assertEqual(stats["ci95"]["low"], stats["ci95"]["high"])


class LabelRuleTests(unittest.TestCase):
    ci_out = {"excludes_zero": True}
    ci_in = {"excludes_zero": False}

    def label(self, n1, n2, d, ci):
        return analysis.label_for(n1, n2, d, ci)[0]

    def test_hint_when_a_group_is_small_even_if_difference_is_huge(self):
        self.assertEqual(self.label(4, 30, 3.0, self.ci_out), "hint")
        self.assertEqual(self.label(30, 4, -3.0, self.ci_out), "hint")
        self.assertEqual(self.label(0, 0, None, None), "hint")

    def test_finding_needs_size_ten_nights_and_interval(self):
        self.assertEqual(self.label(10, 10, 0.8, self.ci_out), "finding")
        self.assertEqual(self.label(10, 10, -1.5, self.ci_out), "finding")
        self.assertEqual(self.label(9, 12, 1.5, self.ci_out), "early signal")      # a group under 10
        self.assertEqual(self.label(10, 10, 1.5, self.ci_in), "early signal")      # interval includes zero
        self.assertEqual(self.label(10, 10, 0.79, self.ci_out), "early signal")    # just under 0.8

    def test_early_signal_and_no_clear_difference(self):
        self.assertEqual(self.label(5, 5, 0.5, self.ci_in), "early signal")
        self.assertEqual(self.label(5, 5, 0.49, self.ci_out), "no clear difference")
        self.assertEqual(self.label(20, 20, 0.0, self.ci_in), "no clear difference")


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.s = Scratch()
        self.addCleanup(self.s.close)

    def test_week_with_a_single_caffeine_night(self):
        self.s.add("yes", 70, hr=62)
        for score in (82, 84, 80, 85, 83, 81):
            self.s.add("no", score, hr=52)
        r = self.s.run()
        c = comp(r, "sleep_score", "without_backfilled")
        self.assertEqual(c["caffeine"]["n"], 1)
        self.assertEqual(c["no_caffeine"]["n"], 6)
        self.assertEqual(c["label"], "hint")
        self.assertIsNone(c["ci95"])                       # one night has no spread, so no interval
        self.assertIsNone(c["effect_size_d"])
        self.assertLess(c["difference"], 0)                # the difference is still reported
        self.assertEqual(r["last_7_days"]["label"], "hint")

    def test_clear_difference(self):
        for score, hr in zip(spread(70, 12), spread(62, 12)):
            self.s.add("yes", score, hr=hr)
        for score, hr in zip(spread(85, 12), spread(52, 12)):
            self.s.add("no", score, hr=hr)
        r = self.s.run()
        for view in analysis.VIEWS:
            score = comp(r, "sleep_score", view)
            self.assertEqual(score["label"], "finding")
            self.assertTrue(score["ci95"]["excludes_zero"])
            self.assertAlmostEqual(score["difference"], -15.0)
            self.assertEqual(score["caffeine_nights_look"], "worse")
            heart = comp(r, "lowest_heart_rate", view)
            self.assertEqual(heart["label"], "finding")
            self.assertAlmostEqual(heart["difference"], 10.0)
            self.assertEqual(heart["caffeine_nights_look"], "worse")   # a higher heart rate is worse
        self.assertTrue(all(b["comparable"] for b in r["backfill_check"]))
        self.assertTrue(all(b["changes_result"] is False for b in r["backfill_check"]))

    def test_no_difference(self):
        for score, hr in zip(spread(80, 12), spread(55, 12)):
            self.s.add("yes", score, hr=hr)
            self.s.add("no", score, hr=hr)
        r = self.s.run()
        for c in r["comparisons"]:
            self.assertEqual(c["label"], "no clear difference", c["id"])
            self.assertEqual(c["difference"], 0)
            self.assertEqual(c["caffeine_nights_look"], "same")
            self.assertFalse(c["ci95"]["excludes_zero"])

    def test_every_night_tagged(self):
        for tag in ("alcohol", "illness", "travel", "other", "alcohol", "illness", "travel"):
            self.s.add("yes", 60, tag=tag)
        r = self.s.run()
        self.assertEqual(r["nights"]["in_view"], 7)
        self.assertEqual(r["nights"]["used"], 0)
        self.assertEqual(r["nights"]["left_out"]["total"], 7)
        self.assertEqual(r["nights"]["left_out"]["by_reason"]["tagged_alcohol"], 2)
        for c in r["comparisons"]:
            self.assertEqual(c["label"], "hint")
            self.assertIsNone(c["difference"])
            self.assertIsNone(c["ci95"])
        self.assertEqual(r["evening_dozing"][0]["flag"], "not enough data")
        self.assertTrue(all(b["changes_result"] is None for b in r["backfill_check"]))

    def test_backfilled_answers_flip_the_result(self):
        # Answers given on the evening show no difference ...
        for score in spread(80, 10):
            self.s.add("yes", score)
            self.s.add("no", score)
        # ... but the answers entered later pair low scores with caffeine and high scores with none.
        for score in spread(68, 8):
            self.s.add("yes", score, backfilled=1)
        for score in spread(90, 8):
            self.s.add("no", score, backfilled=1)
        r = self.s.run()
        without = comp(r, "sleep_score", "without_backfilled")
        with_all = comp(r, "sleep_score", "all_answers")
        self.assertEqual(without["label"], "no clear difference")
        self.assertEqual(with_all["label"], "finding")
        self.assertEqual(without["caffeine_nights_look"], "same")
        self.assertEqual(with_all["caffeine_nights_look"], "worse")
        check = next(b for b in r["backfill_check"] if b["metric"] == "sleep_score")
        self.assertTrue(check["comparable"])
        self.assertTrue(check["changes_result"])
        self.assertFalse(check["same_label"])
        self.assertEqual(r["nights"]["backfilled_among_used"], 16)
        self.assertEqual(without["caffeine"]["nights"], 10)    # backfilled nights are really left out

    def test_backfill_check_is_null_when_a_view_has_no_direction(self):
        # The only caffeine nights are backfilled, so "without backfilled" has an empty caffeine group.
        # That is "cannot compare", not "the result changed".
        for _ in range(3):
            self.s.add("no", 80)
        self.s.add("yes", 90, backfilled=1)
        self.s.add("no", 80, backfilled=1)
        r = self.s.run()
        self.assertIsNone(comp(r, "sleep_score", "without_backfilled")["caffeine_nights_look"])
        for check in r["backfill_check"]:
            self.assertFalse(check["comparable"])
            self.assertIsNone(check["changes_result"])
            self.assertIsNone(check["same_direction"])

    def test_backfill_check_with_no_data_at_all(self):
        r = self.s.run()
        self.assertTrue(all(b["comparable"] is False and b["changes_result"] is None for b in r["backfill_check"]))

    def test_leaving_nights_out_and_saying_why(self):
        self.s.add("yes", 70)                                         # used
        self.s.add("no", 80)                                          # used
        self.s.add("unanswered", 75)                                  # row says unanswered
        self.s.add("unanswered", 75, log=False)                       # no row at all
        self.s.add("unanswered", 75, tag="alcohol")                   # unanswered wins over tag: one reason only
        self.s.add("yes", 75, tag="travel")
        self.s.add("no", 75, tag="other")
        self.s.add("no", 75, tag="illness")
        self.s.add("yes", 75, tag=None)                               # follow-up never answered
        r = self.s.run()
        left = r["nights"]["left_out"]["by_reason"]
        self.assertEqual(r["nights"]["in_view"], 9)
        self.assertEqual(r["nights"]["used"], 2)
        self.assertEqual(left, {"unanswered": 3, "tagged_alcohol": 0, "tagged_illness": 1, "tagged_travel": 1,
                                "tagged_other": 1, "tag_not_answered": 1})
        self.assertEqual(sum(left.values()) + r["nights"]["used"], r["nights"]["in_view"])
        self.assertEqual(set(r["nights"]["left_out"]["why"]), set(left))      # every reason is explained

    def test_since_date_limits_the_nights(self):
        for i in range(10):
            self.s.add("yes" if i % 2 else "no", 80)
        r = self.s.run(since=evening(6))
        self.assertEqual(r["nights"]["in_view"], 4)

    def test_missing_values_are_dropped_per_metric_and_counted(self):
        for score in spread(80, 6):
            self.s.add("yes", score)
            self.s.add("no", score)
        self.s.add("yes", None, hr=55)                                 # night without a sleep score
        r = self.s.run()
        score = comp(r, "sleep_score", "all_answers")
        heart = comp(r, "lowest_heart_rate", "all_answers")
        self.assertEqual(score["caffeine"]["n"], 6)
        self.assertEqual(score["caffeine"]["dropped_missing_value"], 1)
        self.assertEqual(heart["caffeine"]["n"], 7)

    def test_primary_and_directions_are_marked(self):
        for score in spread(80, 6):
            self.s.add("yes", score)
            self.s.add("no", score)
        r = self.s.run()
        self.assertEqual([c["id"] for c in r["comparisons"] if c["primary"]], ["sleep_score:without_backfilled"])
        self.assertEqual(r["primary_comparison"], "sleep_score:without_backfilled")
        self.assertEqual(len(r["comparisons"]), 4)
        self.assertEqual(comp(r, "sleep_score", "all_answers")["better_direction"], "higher")
        self.assertEqual(comp(r, "lowest_heart_rate", "all_answers")["better_direction"], "lower")
        self.assertEqual(comp(r, "lowest_heart_rate", "all_answers")["unit"], "bpm")
        self.assertTrue(r["caveats"])
        self.assertTrue(any("time" in c for c in r["caveats"]))

    def test_evening_dozing_flag(self):
        for _ in range(6):
            self.s.add("yes", 80, nap=30)
            self.s.add("no", 80, nap=5)
        r = self.s.run()
        self.assertEqual(r["evening_dozing"][0]["flag"], "differs")
        self.assertEqual(r["evening_dozing"][0]["difference"], 25)

        other = Scratch()
        self.addCleanup(other.close)
        for _ in range(6):
            other.add("yes", 80, nap=12)
            other.add("no", 80, nap=5)                                 # 7 minutes apart: under the limit
        self.assertEqual(other.run()["evening_dozing"][0]["flag"], "similar")

        few = Scratch()
        self.addCleanup(few.close)
        for _ in range(4):
            few.add("yes", 80, nap=60)
            few.add("no", 80, nap=0)
        self.assertEqual(few.run()["evening_dozing"][0]["flag"], "not enough data")

    def test_unmeasured_dozing_is_not_counted_as_zero(self):
        for _ in range(6):
            self.s.add("yes", 80, nap=None)                            # not measured
            self.s.add("no", 80, nap=20)
        r = self.s.run()
        self.assertEqual(r["evening_dozing"][0]["caffeine"]["n"], 0)
        self.assertEqual(r["evening_dozing"][0]["flag"], "not enough data")

    def test_last_seven_days_window(self):
        for i in range(20):
            self.s.add("yes" if i % 2 else "no", 80, evening_date=(AS_OF - timedelta(days=20 - i)).isoformat())
        r = self.s.run(since=(AS_OF - timedelta(days=30)).isoformat())
        week = r["last_7_days"]
        self.assertEqual(week["evenings_from"], (AS_OF - timedelta(days=7)).isoformat())
        self.assertEqual(week["evenings_to"], (AS_OF - timedelta(days=1)).isoformat())
        self.assertEqual(week["nights_found"], 7)
        self.assertEqual(week["nights_used"], 7)
        self.assertEqual(week["label"], "hint")

    def test_empty_database(self):
        r = self.s.run()
        self.assertEqual(r["nights"]["in_view"], 0)
        self.assertTrue(all(c["label"] == "hint" for c in r["comparisons"]))


class DatabaseTests(unittest.TestCase):
    def test_old_view_is_upgraded_without_losing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE oura_nights (day TEXT PRIMARY KEY, sleep_score INTEGER, lowest_heart_rate INTEGER,
                    bedtime_start TEXT, bedtime_end TEXT, fetched_at TEXT NOT NULL, long_sleep_count INTEGER,
                    pre_sleep_nap_minutes INTEGER, post_wake_nap_minutes INTEGER, adjacent_sleep_minutes INTEGER);
                CREATE TABLE daily_log (evening_date TEXT PRIMARY KEY,
                    caffeine_after_5pm TEXT NOT NULL DEFAULT 'unanswered', tag TEXT,
                    backfilled INTEGER NOT NULL DEFAULT 0);
                CREATE VIEW nights_with_caffeine AS SELECT date(n.day,'-1 day') AS evening_date,
                    COALESCE(l.caffeine_after_5pm,'unanswered') AS caffeine_after_5pm, l.tag, n.day AS sleep_day,
                    n.sleep_score, n.lowest_heart_rate, n.bedtime_start, n.bedtime_end
                    FROM oura_nights n LEFT JOIN daily_log l ON l.evening_date = date(n.day,'-1 day');
                INSERT INTO oura_nights (day, sleep_score, lowest_heart_rate, fetched_at, pre_sleep_nap_minutes)
                    VALUES ('2030-01-02', 81, 54, 'x', 7);
                INSERT INTO daily_log VALUES ('2030-01-01', 'yes', 'none', 1);
            """)
            conn.commit()
            conn.close()
            with self.assertRaises(SystemExit):                       # the analysis refuses an old view
                analysis.open_read_only(path)

            upgraded = db.connect(path)
            row = upgraded.execute("SELECT * FROM nights_with_caffeine").fetchone()
            upgraded.close()
            self.assertEqual((row["evening_date"], row["caffeine_after_5pm"], row["backfilled"],
                              row["sleep_score"], row["pre_sleep_nap_minutes"]), ("2030-01-01", "yes", 1, 81, 7))
            self.assertEqual(db.connect(path).execute("SELECT count(*) FROM daily_log").fetchone()[0], 1)

    def test_analysis_cannot_write(self):
        s = Scratch()
        self.addCleanup(s.close)
        s.add("yes", 80)
        with closing(analysis.open_read_only(s.path)) as ro:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("DELETE FROM daily_log")



if __name__ == "__main__":
    unittest.main()
