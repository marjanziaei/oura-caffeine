"""Tests for verdict.py and weekly_message.py. A fake API only: the real one is never called.

Run:  .venv/bin/python -m unittest test_verdict -v
Made-up data in scratch databases (see test_analysis.Scratch).
"""
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import timedelta
from unittest import mock

import requests

import verdict
import weekly_message
from test_analysis import AS_OF, Scratch, spread

REAL_API_ATTEMPTS = []


def setUpModule():
    """Any code path that tries to construct the real API client records it and fails."""
    def forbidden(*args, **kwargs):
        REAL_API_ATTEMPTS.append(1)
        raise AssertionError("tests must never use the real API")
    patcher = mock.patch.object(verdict.anthropic, "Anthropic", forbidden)
    patcher.start()
    setUpModule.patcher = patcher


def tearDownModule():
    setUpModule.patcher.stop()
    assert not REAL_API_ATTEMPTS, f"{len(REAL_API_ATTEMPTS)} test(s) tried to reach the real API"


class FakeAPI:
    """Stands in for the model. Each entry is the text to return, or an exception to raise."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, system, user):
        self.calls.append({"system": system, "user": user})
        item = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def make_analysis(kind):
    s = Scratch()
    try:
        if kind == "hint":                       # one caffeine night
            s.add("yes", 70, hr=62)
            for score in (82, 84, 80, 85, 83, 81):
                s.add("no", score, hr=52)
        elif kind == "finding":                  # caffeine nights clearly worse
            for score, hr in zip(spread(70, 12), spread(62, 12)):
                s.add("yes", score, hr=hr, nap=25)
            for score, hr in zip(spread(85, 12), spread(52, 12)):
                s.add("no", score, hr=hr, nap=5)
        elif kind == "better":                   # caffeine nights clearly better
            for score, hr in zip(spread(90, 12), spread(50, 12)):
                s.add("yes", score, hr=hr)
            for score, hr in zip(spread(75, 12), spread(58, 12)):
                s.add("no", score, hr=hr)
        elif kind == "none":                     # no difference
            for score, hr in zip(spread(80, 12), spread(55, 12)):
                s.add("yes", score, hr=hr)
                s.add("no", score, hr=hr)
        elif kind == "noisy":                    # a small gap inside the noise: the range includes zero
            for score in (70, 80, 90, 75, 85, 72, 88, 78):
                s.add("yes", score)
            for score in (72, 82, 78, 88, 70, 86, 80, 76):
                s.add("no", score)
        elif kind == "all_tagged":
            for tag in ("alcohol", "illness", "travel", "other", "alcohol", "illness", "travel"):
                s.add("yes", 60, tag=tag)
        elif kind == "flip":                     # late answers create the difference
            for score in spread(80, 10):
                s.add("yes", score)
                s.add("no", score)
            for score in spread(68, 8):
                s.add("yes", score, backfilled=1)
            for score in spread(90, 8):
                s.add("no", score, backfilled=1)
        elif kind == "only_backfilled_caffeine":
            for _ in range(3):
                s.add("no", 80)
            s.add("yes", 90, backfilled=1)
            s.add("no", 80, backfilled=1)
        elif kind == "empty":
            pass
        return s.run()
    finally:
        s.close()


LABEL_LINES = {
    "hint": "This is only a first look, and more nights would help.",
    "early signal": "This is worth watching.",
    "finding": "The gap is clear.",
    "no clear difference": "There is no clear difference.",
}


def explanation(a, extra=""):
    """A valid explanation for this analysis: no direction words, no numbers unless added with extra."""
    label = verdict.primary(a)["label"]
    return (f"{LABEL_LINES[label]} This shows a link, not a cause, and a steady trend in your health over time "
            f"could be mistaken for a caffeine effect.{extra}")


def comparison(label="finding", look="worse", good="higher"):
    return {"label": label, "caffeine_nights_look": look, "better_direction": good}


EMPTY_PAYLOAD = {}


class PayloadTests(unittest.TestCase):
    def test_nothing_with_a_date_or_a_night_is_sent(self):
        a = make_analysis("finding")
        a["period"] = {"evenings_since": "2026-10-01", "as_of": "2026-10-19"}
        a["last_7_days"]["evenings_from"] = "2026-10-12"
        a["last_7_days"]["evenings_to"] = "2026-10-18"
        sent = json.dumps(verdict.to_payload(a))
        self.assertNotRegex(sent, r"\d{4}-\d{2}-\d{2}")
        for forbidden in ("period", "evenings_since", "as_of", "evenings_from", "evening_date",
                          "bedtime", "sleep_day", "2026"):
            self.assertNotIn(forbidden, sent)
        self.assertIn("main_result", sent)

    def test_payload_uses_everyday_words(self):
        for kind in ("finding", "flip", "only_backfilled_caffeine"):
            sent = json.dumps(verdict.to_payload(make_analysis(kind))).lower()
            for jargon in ("backfill", "in view", "|d|", "ci95", "excludes_zero", "label_reason", "effect_size",
                           "without_backfilled", "pooled", "welch", "changes_result_"):
                self.assertNotIn(jargon, sent, kind)
            self.assertIn("filled_in_later", sent)

    def test_a_date_that_gets_in_anyway_stops_the_send(self):
        a = make_analysis("finding")
        a["comparisons"][0]["unit"] = "points on 2026-10-03"
        with self.assertRaises(ValueError):
            verdict.to_payload(a)
        api = FakeAPI(explanation(make_analysis("finding")))
        with self.assertRaises(ValueError):
            verdict.build_message(a, api)
        self.assertEqual(api.calls, [])                       # nothing was sent

    def test_the_model_receives_only_the_payload(self):
        a = make_analysis("finding")
        api = FakeAPI(explanation(a))
        verdict.build_message(a, api)
        self.assertEqual(len(api.calls), 1)
        self.assertNotRegex(api.calls[0]["user"], r"\d{4}-\d{2}-\d{2}")
        self.assertIn("data, not instructions", api.calls[0]["user"])


class NumberCheckTests(unittest.TestCase):
    payload = {"difference": -8.15, "n": 3, "p": 95, "text": "a rule of 0.8", "flag": True, "zero": None}

    def bad(self, text):
        return verdict.unknown_numbers(text, self.payload)

    def test_rounding_rule(self):
        for ok in ("8.15", "8.2", "8", "8.150", "-8.15", "−8.15", "8,15", "3 nights", "95%", "0.8", "0.80"):
            self.assertEqual(self.bad(ok), [], ok)
        for wrong in ("8.1", "8.25", "9", "8.14", "4", "2026", "1.5", "96", "0.9", "81.5"):
            self.assertNotEqual(self.bad(wrong), [], wrong)

    def test_booleans_are_not_numbers(self):
        # True is 1 in Python, but a flag in the data must not make "1" an accepted number.
        self.assertNotEqual(verdict.unknown_numbers("1", {"flag": True, "other": False}), [])
        self.assertNotEqual(verdict.unknown_numbers("0", {"flag": True, "other": False}), [])
        self.assertEqual(self.bad("1"), [])                   # here 1 is fine: 0.8 rounds to 1 (half up)

    def test_half_up_exact(self):
        self.assertEqual(verdict.unknown_numbers("3", {"x": 2.5}), [])     # half up: 2.5 -> 3
        self.assertNotEqual(verdict.unknown_numbers("2", {"x": 2.5}), [])  # half-even would have allowed 2
        self.assertEqual(verdict.unknown_numbers("0.3", {"x": 0.25}), [])

    def test_absurdly_long_numbers_do_not_crash(self):
        self.assertNotEqual(self.bad("9" * 80), [])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.a = make_analysis("finding")
        self.payload = verdict.to_payload(self.a)
        self.c = verdict.primary(self.a)

    def problems(self, text, c=None):
        return verdict.problems_with(text, self.payload, c or self.c)

    def test_good_text_passes(self):
        self.assertEqual(self.problems(explanation(self.a)), [])

    def test_invented_number_is_caught(self):
        text = explanation(self.a, " There was a 37 point swing.")
        self.assertTrue(any("37" in p for p in self.problems(text)))

    def test_a_number_from_the_data_is_allowed(self):
        n = self.c["caffeine"]["n"]
        self.assertEqual(self.problems(explanation(self.a, f" That is {n} nights with caffeine.")), [])

    def test_spelled_out_numbers_and_dates_are_caught(self):
        self.assertTrue(self.problems("About eight nights were used."))
        self.assertTrue(self.problems("On 3 October it was odd."))
        self.assertTrue(self.problems("At 21:00 it was odd."))
        self.assertEqual(self.problems("The range includes zero."), [])         # ordinary wording is fine

    def test_certainty_words_depend_on_the_label(self):
        text = "This is a significant finding."
        self.assertEqual(self.problems(text, comparison("finding", "same")), [])
        self.assertTrue(self.problems(text, comparison("hint", "same")))
        self.assertTrue(self.problems(text, comparison("early signal", "same")))
        self.assertTrue(self.problems("This proves it.", comparison("finding", "same")))     # never allowed
        self.assertTrue(self.problems("This is an early signal.", comparison("hint", "same")))

    def test_advice_markdown_and_label_line_are_rejected(self):
        self.assertTrue(self.problems("You should ask your doctor."))
        self.assertTrue(self.problems("Try to avoid caffeine."))
        self.assertTrue(self.problems("**Result** is fine."))
        self.assertTrue(self.problems("- first point\n- second point"))
        self.assertTrue(self.problems("Confidence: FINDING. Fine."))
        self.assertTrue(self.problems("   "))

    def test_length_limit_is_about_eighty_words(self):
        self.assertEqual(verdict.ASKED_WORDS, 80)
        self.assertEqual(self.problems(" ".join(["nights"] * verdict.MAX_WORDS)), [])
        self.assertTrue(any("too long" in p for p in self.problems(" ".join(["nights"] * (verdict.MAX_WORDS + 1)))))
        # Few words but very long ones: the character limit catches it.
        long_words = " ".join(["nightsnightsnights"] * 60)
        self.assertLessEqual(len(long_words.split()), verdict.MAX_WORDS)
        self.assertTrue(any("too long" in p for p in self.problems(long_words)))

    def test_jargon_is_rejected(self):
        for text in ("The backfilled answers matter.", "Non-backfilled answers differ.", "Only 5 nights in view.",
                     "The JSON says so.", "The effect size is big.", "The ci95 is wide."):
            self.assertTrue(any("terms the reader will not know" in p for p in self.problems(text)), text)
        self.assertEqual(self.problems("It changes when the answers I filled in later are counted."), [])

    def test_opposite_direction_words_when_caffeine_looks_worse(self):
        worse = comparison("finding", "worse", "higher")
        for text in ("Caffeine nights looked better.", "The score was higher.", "It improved a little.",
                     "That is a boost.", "Better sleep on caffeine nights.", "It rose."):
            self.assertTrue(any("direction words" in p for p in self.problems(text, worse)), text)
        for text in ("More nights would help.", "A steady trend in your health could be mistaken for it.",
                     "This is worth watching."):
            self.assertEqual(self.problems(text, worse), [], text)

    def test_opposite_direction_words_when_caffeine_looks_better(self):
        better = comparison("finding", "better", "higher")
        for text in ("Caffeine nights looked worse.", "The score dropped.", "It was lower.", "That is harmful.",
                     "A decline in sleep.", "It fell."):
            self.assertTrue(any("direction words" in p for p in self.problems(text, better)), text)

    def test_the_opposite_word_depends_on_which_way_is_better(self):
        # For heart rate lower is better. If caffeine nights look worse, "lower" is the opposite wording.
        worse_hr = comparison("finding", "worse", "lower")
        self.assertTrue(self.problems("The rate was lower.", worse_hr))
        self.assertEqual(self.problems("The rate was higher.", worse_hr), [])      # restating, not contradicting
        worse_score = comparison("finding", "worse", "higher")
        self.assertTrue(self.problems("The score was higher.", worse_score))
        self.assertEqual(self.problems("The score was lower.", worse_score), [])

    def test_with_no_direction_every_direction_word_is_rejected(self):
        for look in ("same", None):
            c = comparison("hint", look, "higher")
            for word in ("better", "worse", "higher", "lower", "improved", "dropped", "Better"):
                self.assertTrue(self.problems(f"It was {word}.", c), f"{look}: {word}")
            self.assertEqual(self.problems("This shows a link, not a cause."), [])


class CoreSentenceTests(unittest.TestCase):
    def test_caffeine_worse(self):
        a = make_analysis("finding")
        text = verdict.core_sentence(a)
        self.assertIn("Sleep score was 15 points lower on nights with caffeine after 5pm, which is worse.", text)
        self.assertRegex(text, r"The 95% range for the gap is [\d.]+ to [\d.]+ points lower\.")
        self.assertIn("Based on 12 nights with caffeine after 5pm and 12 nights without.", text)

    def test_caffeine_better(self):
        text = verdict.core_sentence(make_analysis("better"))
        self.assertIn("points higher on nights with caffeine after 5pm, which is better.", text)
        self.assertRegex(text, r"The 95% range for the gap is [\d.]+ to [\d.]+ points higher\.")

    def test_range_that_includes_zero_is_said_plainly(self):
        text = verdict.core_sentence(make_analysis("noisy"))
        self.assertRegex(text, r"runs from [\d.]+ points lower to [\d.]+ points higher")
        self.assertIn("no difference at all cannot be ruled out", text)

    def test_no_range_means_not_enough_data(self):
        text = verdict.core_sentence(make_analysis("hint"))
        self.assertIn("Not enough data yet for a 95% range.", text)
        self.assertIn("Based on 1 night with caffeine", text)             # singular

    def test_no_comparison_at_all(self):
        for kind in ("empty", "all_tagged"):
            text = verdict.core_sentence(make_analysis(kind))
            self.assertTrue(text.startswith("Not enough data yet to compare sleep score."), kind)
            for word in ("lower", "higher", "better", "worse"):
                self.assertNotIn(word, text)

    def test_no_difference(self):
        self.assertIn("Sleep score was the same on both kinds of night.", verdict.core_sentence(make_analysis("none")))

    def test_numbers_and_direction_come_straight_from_the_data(self):
        from decimal import ROUND_HALF_UP, Decimal
        for kind in ("hint", "finding", "better", "noisy", "none", "flip", "only_backfilled_caffeine", "empty"):
            a = make_analysis(kind)
            text = verdict.core_sentence(a)
            # "less than 1 point" quotes the size of the rounding step, not a value from the data.
            checked = text.replace("less than 1", "")
            self.assertEqual(verdict.unknown_numbers(checked, verdict.to_payload(a)), [], kind)
            diff = verdict.primary(a)["difference"]
            if diff:
                shown = Decimal(str(abs(diff))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                words = f"{shown} point{'' if shown == 1 else 's'}" if shown else "less than 1 point"
                self.assertIn(f"{words} {'lower' if diff < 0 else 'higher'} on nights", text, kind)
                self.assertIn(f"which is {verdict.primary(a)['caffeine_nights_look']}", text)

    def test_heart_rate_line(self):
        self.assertIn("Lowest heart rate (secondary): 10.0 bpm higher on nights with caffeine, which is worse (finding).",
                      verdict.heart_rate_line(make_analysis("finding")))
        self.assertIn("lower on nights with caffeine, which is better", verdict.heart_rate_line(make_analysis("better")))
        self.assertIn("the same on both kinds of night", verdict.heart_rate_line(make_analysis("none")))
        self.assertIn("not enough data yet", verdict.heart_rate_line(make_analysis("empty")))


class HabitLineTests(unittest.TestCase):
    def line(self, kept, slipped, unanswered):
        answered = kept + slipped
        return verdict.habit_line({"habit_week": {"evenings": 7, "kept": kept, "slipped": slipped,
                                                  "answered": answered, "unanswered": unanswered}})

    def test_wording(self):
        self.assertEqual(self.line(4, 2, 1), "Last 7 evenings: kept the 5pm rule on 4 of 6 answered evenings; 1 unanswered.")
        self.assertEqual(self.line(7, 0, 0), "Last 7 evenings: kept the 5pm rule on 7 of 7 answered evenings; 0 unanswered.")
        self.assertEqual(self.line(0, 3, 4), "Last 7 evenings: kept the 5pm rule on 0 of 3 answered evenings; 4 unanswered.")
        self.assertEqual(self.line(1, 0, 6), "Last 7 evenings: kept the 5pm rule on 1 of 1 answered evening; 6 unanswered.")
        self.assertEqual(self.line(0, 0, 7), "Last 7 evenings: no answers yet; 7 unanswered.")

    def test_it_is_the_first_line_on_both_paths_and_above_the_confidence_line(self):
        s = Scratch()
        self.addCleanup(s.close)
        for i, answer in enumerate(("no", "no", "yes", "no", "no", "unanswered")):
            s.add(answer, 80, evening_date=(AS_OF - timedelta(days=7 - i)).isoformat())
        a = s.run(since=(AS_OF - timedelta(days=30)).isoformat())
        expected = "Last 7 evenings: kept the 5pm rule on 4 of 5 answered evenings; 2 unanswered."
        for api in (FakeAPI(explanation(a)), FakeAPI(ConnectionError())):
            m = verdict.build_message(a, api)
            lines = m["text"].split("\n")
            self.assertEqual(lines[0], expected, m["source"])
            self.assertEqual(lines[1], "")
            self.assertTrue(lines[2].startswith("Confidence:"), m["source"])

    def test_the_habit_numbers_never_go_to_the_model_and_the_model_cannot_change_them(self):
        a = make_analysis("finding")
        payload = json.dumps(verdict.to_payload(a))
        self.assertNotIn("habit", payload)
        self.assertNotIn("kept", payload)
        # If the model quotes habit counts anyway, they are numbers it was never given: rejected, then retried.
        m = verdict.build_message(a, FakeAPI("You kept the 5pm rule on 3 of 7 evenings. " + explanation(a),
                                             explanation(a)))
        self.assertEqual((m["source"], m["attempts"]), ("ai", 2))
        self.assertIn("3", m["problems"][0][0])
        self.assertNotIn("You kept", m["text"])
        self.assertTrue(m["text"].startswith(verdict.habit_line(a)))      # the first line is always the code's


def tiny_analysis(diff, low, high, hr=None, look="worse", n=(5, 5)):
    """Just enough of an analysis for the code-written lines, to test rounding on exact values."""
    def comp(metric, unit, difference, ci, primary_):
        return {"metric": metric, "view": "without_backfilled", "primary": primary_, "unit": unit,
                "better_direction": "higher" if metric == "sleep_score" else "lower",
                "caffeine": {"n": n[0], "mean": 70.0}, "no_caffeine": {"n": n[1], "mean": 80.0},
                "difference": difference, "caffeine_nights_look": look, "label": "hint",
                "ci95": None if ci is None else {"low": ci[0], "high": ci[1], "excludes_zero": not ci[0] <= 0 <= ci[1]}}
    return {"comparisons": [comp("sleep_score", "points", diff, None if low is None else (low, high), True),
                            comp("lowest_heart_rate", "bpm", hr, None, False)]}


class RoundingTests(unittest.TestCase):
    """Sleep score differences and ranges in whole points; heart rate to one decimal; half-up."""

    def core(self, diff, low=None, high=None, **kw):
        return verdict.core_sentence(tiny_analysis(diff, low, high, **kw))

    def hr(self, value):
        return verdict.heart_rate_line(tiny_analysis(-1.0, None, None, hr=value))

    def test_sleep_score_is_shown_in_whole_points(self):
        self.assertIn("was 7 points lower", self.core(-6.67, -13.79, -0.2))
        self.assertIn("was 7 points higher", self.core(6.67, 0.2, 13.79, look="better"))
        for text in (self.core(-6.67, -13.79, 0.46), self.core(-15.0, -20.4, -9.6), self.core(12.34, 5.5, 19.5)):
            self.assertNotRegex(text, r"\d\.\d", text)                     # no decimals anywhere in the result

    def test_halves_round_up_not_to_even(self):
        # Python's round(6.5) is 6 and round(0.5) is 0; the message must say 7 and 1.
        self.assertIn("was 7 points lower", self.core(-6.5))
        self.assertIn("was 1 point lower", self.core(-0.5))
        self.assertIn("was 6 points lower", self.core(-6.49))
        self.assertIn("was 3 points higher", self.core(2.5, look="better"))

    def test_one_point_is_singular(self):
        self.assertIn("was 1 point lower", self.core(-1.2))
        self.assertIn("was 2 points lower", self.core(-1.5))

    def test_a_tiny_difference_is_never_shown_as_zero_points(self):
        text = self.core(-0.3, -0.45, -0.1)
        self.assertIn("was less than 1 point lower on nights with caffeine after 5pm, which is worse", text)
        self.assertNotIn(" 0 points", text)
        self.assertIn("The 95% range for the gap is less than 1 point lower.", text)         # said once, singular
        self.assertNotIn("less than 1 to", text)

    def test_ranges(self):
        self.assertIn("The 95% range for the gap is 4 to 14 points lower.", self.core(-9.0, -13.6, -4.2))
        self.assertIn("The 95% range for the gap is 3 to 9 points higher.", self.core(6.0, 2.6, 8.5, look="better"))
        # a range that crosses zero keeps both ends and still says so
        self.assertIn("runs from 14 points lower to less than 1 point higher, so no difference at all cannot be "
                      "ruled out.", self.core(-6.67, -13.79, 0.46))
        self.assertIn("runs from 3 points lower to 2 points higher", self.core(-0.5, -2.6, 2.4))

    def test_an_exact_zero_end_is_called_no_difference(self):
        self.assertIn("runs from 5 points lower to no difference", self.core(-2.0, -5.0, 0.0))

    def test_heart_rate_has_exactly_one_decimal(self):
        self.assertIn("3.7 bpm higher", self.hr(3.73))
        self.assertIn("3.8 bpm higher", self.hr(3.75))                 # half up
        self.assertIn("3.0 bpm higher", self.hr(3.0))                  # the decimal is kept
        self.assertIn("10.0 bpm lower", self.hr(-10.04))
        self.assertIn("0.1 bpm higher", self.hr(0.05))                 # 0.05 rounds up to 0.1
        self.assertIn("less than 0.1 bpm higher", self.hr(0.04))
        self.assertIn("the same on both kinds of night", self.hr(0))

    def test_rounding_is_for_display_only(self):
        a = make_analysis("finding")
        self.assertEqual(verdict.primary(a)["difference"], -15.0)       # the data layer is untouched
        payload = verdict.to_payload(make_analysis("noisy"))
        self.assertRegex(json.dumps(payload), r"\d+\.\d\d")           # the model still gets the two decimals

    def test_the_message_shows_no_two_decimal_numbers_for_score_or_heart_rate(self):
        for kind in ("hint", "finding", "better", "noisy", "flip"):
            a = make_analysis(kind)
            for text in (verdict.core_sentence(a), verdict.heart_rate_line(a)):
                self.assertNotRegex(text, r"\d\.\d\d", text)


class BuildMessageTests(unittest.TestCase):
    def test_layout_confidence_then_code_result_then_ai_explanation(self):
        for kind in ("hint", "finding", "better", "none", "noisy"):
            a = make_analysis(kind)
            api = FakeAPI(explanation(a))
            m = verdict.build_message(a, api)
            self.assertEqual((m["source"], m["attempts"]), ("ai", 1), kind)
            lines = m["text"].split("\n")
            self.assertEqual(lines[0], verdict.habit_line(a))
            self.assertTrue(lines[0].startswith("Last 7 evenings:"))
            self.assertEqual(lines[1], "")
            self.assertEqual(lines[2], verdict.confidence_line(a))
            self.assertTrue(lines[2].startswith(f"Confidence: {verdict.primary(a)['label'].upper()}"))
            self.assertEqual(lines[4], verdict.core_sentence(a))
            self.assertEqual(lines[5], verdict.heart_rate_line(a))
            self.assertEqual(lines[7], explanation(a))
            self.assertIn("Explanation written by AI", lines[-1])

    def test_ai_cannot_change_the_label_line(self):
        a = make_analysis("hint")
        api = FakeAPI("Confidence: FINDING. This is certain.", explanation(a))
        m = verdict.build_message(a, api)
        self.assertTrue(m["text"].split("\n")[2].startswith("Confidence: HINT"))
        self.assertNotIn("FINDING", m["text"])
        self.assertEqual(m["attempts"], 2)

    def test_invented_number_triggers_one_retry_which_can_succeed(self):
        a = make_analysis("finding")
        invented = explanation(a, " There was a 37 point swing.")
        api = FakeAPI(invented, explanation(a))
        m = verdict.build_message(a, api)
        self.assertEqual((m["source"], m["attempts"], len(api.calls)), ("ai", 2, 2))
        self.assertIn("37", m["problems"][0][0])
        self.assertIn("rejected", api.calls[1]["user"])        # the retry says what was wrong
        self.assertIn("37", api.calls[1]["user"])
        self.assertNotIn("37", m["text"])

    def test_invented_number_twice_falls_back_to_the_plain_template(self):
        a = make_analysis("finding")
        invented = explanation(a, " There was a 37 point swing.")
        api = FakeAPI(invented, invented, invented)
        m = verdict.build_message(a, api)
        self.assertEqual(len(api.calls), 2)                      # one try plus exactly one retry
        self.assertEqual(m["source"], "template")
        self.assertNotIn("37", m["text"])
        self.assertIn("without AI", m["text"])
        self.assertTrue(m["text"].split("\n")[2].startswith("Confidence: FINDING"))

    def test_flipped_direction_is_rejected_then_retried(self):
        a = make_analysis("finding")                              # caffeine nights are worse
        flipped = "Caffeine nights had a better sleep score. " + explanation(a)
        api = FakeAPI(flipped, explanation(a))
        m = verdict.build_message(a, api)
        self.assertEqual((m["source"], m["attempts"]), ("ai", 2))
        self.assertIn("better", m["problems"][0][0])
        self.assertIn("better", api.calls[1]["user"])            # the retry is told which word was wrong
        self.assertNotIn("better sleep score", m["text"])

    def test_flipped_direction_twice_falls_back_and_the_code_keeps_its_direction(self):
        for kind, wrong, right in (("finding", "improved", "worse"), ("better", "dropped", "better"),
                                   ("none", "lower", "same")):
            a = make_analysis(kind)
            bad = f"The score {wrong} on caffeine nights. " + explanation(a)
            api = FakeAPI(bad, bad)
            m = verdict.build_message(a, api)
            self.assertEqual((m["source"], len(api.calls)), ("template", 2), kind)
            self.assertNotIn(f"The score {wrong}", m["text"])
            self.assertIn(right, m["text"])
            self.assertTrue(m["text"].split("\n")[2].startswith("Confidence:"))

    def test_api_down_gives_the_plain_template(self):
        a = make_analysis("finding")
        for error in (ConnectionError("down"), TimeoutError("slow"), RuntimeError("model stopped early (refusal)")):
            api = FakeAPI(error)
            m = verdict.build_message(a, api)
            self.assertEqual((m["source"], len(api.calls)), ("template", 2), repr(error))
            self.assertTrue(m["text"].split("\n")[2].startswith("Confidence: FINDING"))
            self.assertIn("the call failed", m["problems"][0][0])
            self.assertIn("which is worse", m["text"])

    def test_down_then_up_recovers_on_the_retry(self):
        a = make_analysis("finding")
        m = verdict.build_message(a, FakeAPI(ConnectionError("blip"), explanation(a)))
        self.assertEqual((m["source"], m["attempts"]), ("ai", 2))

    def test_the_error_text_never_reaches_the_message(self):
        a = make_analysis("finding")
        m = verdict.build_message(a, FakeAPI(RuntimeError("secret-key-sk-ant-123")))
        self.assertNotIn("sk-ant", m["text"])
        self.assertNotIn("sk-ant", json.dumps(m["problems"]))

    def test_default_path_without_a_working_client_still_gives_a_message(self):
        a = make_analysis("finding")
        before = len(REAL_API_ATTEMPTS)
        m = verdict.build_message(a)                              # real call path, real client forbidden above
        self.assertEqual(m["source"], "template")
        self.assertEqual(len(REAL_API_ATTEMPTS), before + 2)      # it tried twice and failed safely
        del REAL_API_ATTEMPTS[before:]


class PromptTests(unittest.TestCase):
    def test_prompt_asks_for_short_everyday_text(self):
        p = verdict.SYSTEM_PROMPT
        self.assertIn("at most 80 words", p)
        self.assertIn("everyday words", p)
        self.assertIn("short sentences", p)
        self.assertIn("the answers I filled in later", p)
        self.assertIn("Do not say whether the result is better or worse", p)
        self.assertIn("Do not repeat them", p)
        self.assertIn("Use no numbers at all", p)
        self.assertIn('Never use the words "finding" or "signal"', p)

    def test_payload_does_not_hand_the_model_banned_words(self):
        sent = json.dumps(verdict.to_payload(make_analysis("finding"))).lower()
        self.assertNotIn("a_finding", sent)
        self.assertIn("for_a_firm_result", sent)

    def test_the_only_banned_terms_in_the_prompt_sit_in_the_never_use_list(self):
        for line in verdict.SYSTEM_PROMPT.splitlines():
            if "backfilled" in line.lower() or "in view" in line.lower():
                self.assertIn("Never use these terms", line)


class TemplateTests(unittest.TestCase):
    def test_template_only_contains_numbers_from_the_data(self):
        for kind in ("hint", "finding", "better", "noisy", "none", "all_tagged", "flip",
                     "only_backfilled_caffeine", "empty"):
            a = make_analysis(kind)
            text = verdict.fallback_text(a)
            self.assertEqual(verdict.unknown_numbers(text, verdict.to_payload(a)), [], kind)
            for jargon in ("backfill", "in view", "effect size", "json"):
                self.assertNotIn(jargon, text.lower(), kind)

    def test_template_content(self):
        text = verdict.fallback_text(make_analysis("finding"))
        self.assertIn("15 points lower", text)
        self.assertIn("which is worse", text)
        self.assertIn("95% range", text)
        self.assertIn("a link, not a cause", text)
        self.assertIn("trend", text)

    def test_template_when_there_is_nothing_to_compare(self):
        for kind in ("all_tagged", "empty"):
            text = verdict.fallback_text(make_analysis(kind))
            self.assertIn("Not enough data yet", text)
            self.assertNotIn("a link, not a cause", text)

    def test_template_mentions_flags_only_when_relevant(self):
        flip = verdict.fallback_text(make_analysis("flip"))
        self.assertIn("answers I filled in later", flip)
        self.assertNotIn("filled in later", verdict.fallback_text(make_analysis("none")))
        self.assertIn("dozing differed", verdict.fallback_text(make_analysis("finding")))
        self.assertNotIn("dozing", verdict.fallback_text(make_analysis("none")))

    def test_message_fits_in_a_telegram_message(self):
        for kind in ("hint", "finding", "flip", "noisy"):
            a = make_analysis(kind)
            self.assertLess(len(verdict.build_message(a, FakeAPI(ConnectionError()))["text"]), 4096)
            self.assertLess(len(verdict.build_message(a, FakeAPI(explanation(a)))["text"]), 4096)


class WeeklyMessageTests(unittest.TestCase):
    def test_telegram_error_does_not_leak_the_token(self):
        def failing_post(url, **kwargs):
            raise requests.ConnectionError(f"could not reach {url}")
        with self.assertRaises(RuntimeError) as caught:
            weekly_message.send_telegram("hello", "123456:SECRET-TOKEN", "42", post=failing_post)
        self.assertNotIn("SECRET-TOKEN", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_sends_plain_text_to_the_right_chat(self):
        calls = []

        def post(url, json=None, timeout=None):
            calls.append((url, json, timeout))
            return mock.Mock(raise_for_status=lambda: None)
        weekly_message.send_telegram("hello", "T0KEN", "42", post=post)
        url, body, timeout = calls[0]
        self.assertEqual(url, "https://api.telegram.org/botT0KEN/sendMessage")
        self.assertEqual(body, {"chat_id": "42", "text": "hello"})          # no parse_mode: plain text
        self.assertEqual(timeout, weekly_message.TELEGRAM_TIMEOUT_SECONDS)

    def test_without_send_nothing_goes_to_telegram(self):
        s = Scratch()
        self.addCleanup(s.close)
        s.add("yes", 70)
        s.add("no", 80)
        with mock.patch.object(weekly_message, "send_telegram") as send, \
                mock.patch.object(verdict, "call_claude", FakeAPI(ConnectionError())), \
                mock.patch("sys.argv", ["weekly_message.py", "--db", str(s.path), "--since", "2030-01-01",
                                        "--as-of", AS_OF.isoformat()]), \
                mock.patch("builtins.print"):
            weekly_message.main()
        send.assert_not_called()

    def test_with_send_the_message_is_sent_once(self):
        s = Scratch()
        self.addCleanup(s.close)
        s.add("yes", 70)
        s.add("no", 80)
        with mock.patch.object(weekly_message, "send_telegram") as send, \
                mock.patch.object(verdict, "build_message", return_value={
                    "text": "T", "source": "template", "attempts": 2, "problems": [[], []]}), \
                mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}), \
                mock.patch("weekly_message.load_dotenv"), \
                mock.patch("sys.argv", ["weekly_message.py", "--db", str(s.path), "--since", "2030-01-01",
                                        "--as-of", AS_OF.isoformat(), "--send"]), \
                mock.patch("builtins.print"):
            weekly_message.main()
        send.assert_called_once_with("T", "tok", "42")


class MessageLogTests(unittest.TestCase):
    """Which path wrote each weekly message is recorded in the weekly_messages table."""
    HINT_TEXT = ("This is only a first look, and more nights would help. This shows a link, not a cause, and a "
                 "steady trend in your health over time could be mistaken for a caffeine effect.")

    def setUp(self):
        self.s = Scratch()
        self.addCleanup(self.s.close)
        self.s.add("yes", 70)
        self.s.add("no", 80)

    def run_main(self, send=True, api=None, send_effect=None):
        argv = ["weekly_message.py", "--db", str(self.s.path), "--since", "2030-01-01", "--as-of", AS_OF.isoformat()]
        if send:
            argv.append("--send")
        with mock.patch.object(weekly_message, "send_telegram", side_effect=send_effect) as sender, \
                mock.patch.object(verdict, "call_claude", api or FakeAPI(self.HINT_TEXT)), \
                mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}), \
                mock.patch("weekly_message.load_dotenv"), \
                mock.patch("sys.argv", argv), \
                mock.patch("builtins.print"):
            weekly_message.main()
        return sender

    def rows(self):
        with closing(sqlite3.connect(self.s.path)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM weekly_messages ORDER BY id").fetchall()

    def test_an_ai_message_is_logged_as_ai(self):
        self.run_main()
        (row,) = self.rows()
        self.assertEqual((row["source"], row["attempts"], row["label"], row["sent"]), ("ai", 1, "hint", 1))
        self.assertEqual(json.loads(row["problems"]), [[]])
        self.assertRegex(row["sent_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")     # UTC

    def test_a_template_message_is_logged_as_template_with_the_reason(self):
        self.run_main(api=FakeAPI(ConnectionError("down")))
        (row,) = self.rows()
        self.assertEqual((row["source"], row["attempts"], row["sent"]), ("template", 2, 1))
        self.assertEqual(len(json.loads(row["problems"])), 2)
        self.assertIn("the call failed", row["problems"])

    def test_a_rejected_then_accepted_answer_is_logged_with_what_was_rejected(self):
        self.run_main(api=FakeAPI("There were 37 nights. " + self.HINT_TEXT, self.HINT_TEXT))
        (row,) = self.rows()
        self.assertEqual((row["source"], row["attempts"]), ("ai", 2))
        self.assertIn("37", json.loads(row["problems"])[0][0])

    def test_a_dry_run_logs_nothing(self):
        sender = self.run_main(send=False)
        sender.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_a_failed_send_is_logged_as_not_sent_and_still_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_main(send_effect=RuntimeError("Telegram send failed (ConnectionError)"))
        (row,) = self.rows()
        self.assertEqual((row["source"], row["sent"]), ("ai", 0))

    def test_a_logging_failure_never_stops_the_message(self):
        with mock.patch.object(weekly_message, "connect", side_effect=sqlite3.OperationalError("database is locked")):
            sender = self.run_main()
        sender.assert_called_once()
        self.assertEqual(self.rows(), [])

    def test_the_log_can_be_counted_and_holds_no_message_text(self):
        for api in (FakeAPI(self.HINT_TEXT), FakeAPI(self.HINT_TEXT), FakeAPI(ConnectionError())):
            self.run_main(api=api)
        with closing(sqlite3.connect(self.s.path)) as conn:
            counts = dict(conn.execute("SELECT source, count(*) FROM weekly_messages GROUP BY source"))
            columns = [r[1] for r in conn.execute("PRAGMA table_info(weekly_messages)")]
        self.assertEqual(counts, {"ai": 2, "template": 1})
        self.assertEqual(columns, ["id", "sent_at", "source", "attempts", "label", "problems", "sent"])
        dumped = json.dumps([dict(r) for r in self.rows()])
        self.assertNotIn("first look", dumped)
        self.assertNotIn("Confidence", dumped)

    def test_the_table_refuses_nonsense(self):
        with closing(sqlite3.connect(self.s.path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO weekly_messages (sent_at, source, attempts, label, problems, sent) "
                             "VALUES ('x', 'guess', 1, 'hint', '[]', 1)")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO weekly_messages (sent_at, source, attempts, label, problems, sent) "
                             "VALUES ('x', 'ai', 1, 'hint', '[]', 2)")


def readme_sample_analysis():
    """The made-up data behind the sample message in README.md: 3 nights with caffeine, 11 without,
    one evening unanswered, and a Sunday as the day the message is built."""
    from datetime import date
    as_of = date(2030, 2, 24)
    assert as_of.weekday() == 6
    day = lambda k: (as_of - timedelta(days=k)).isoformat()
    caffeine = {13: (71, 59, 30), 10: (78, 60, 0), 2: (74, 58, 15)}          # evening k days back: score, hr, dozing
    no_scores = [82, 79, 85, 77, 81, 84, 78, 83, 80, 76, 86]
    no_hr = [55, 56, 54, 57, 55, 56, 53, 57, 55, 54, 56]
    s = Scratch()
    try:
        rest = iter(zip(no_scores, no_hr))
        for k in range(15, 0, -1):
            if k in caffeine:
                score, hr, nap = caffeine[k]
                s.add("yes", score, hr=hr, nap=nap, evening_date=day(k))
            elif k == 4:
                s.add("unanswered", 80, hr=55, nap=0, evening_date=day(k))
            else:
                score, hr = next(rest)
                s.add("no", score, hr=hr, nap=0, evening_date=day(k))
        return s.run(since=day(15), as_of=as_of)
    finally:
        s.close()


class ReadmeSampleTests(unittest.TestCase):
    """The sample messages in README.md must be exactly what the code produces, so the README never shows
    something the code does not do. If this fails, regenerate the two code blocks under 'What the Sunday
    message looks like' from the real output."""
    STAND_IN = ("This is only a first look, and more nights would help. This shows a link, not a cause, and a "
                "steady trend in your health over time could be mistaken for a caffeine effect.")

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.readme = (Path(__file__).resolve().parent / "README.md").read_text()
        cls.analysis = readme_sample_analysis()

    def test_the_data_is_what_the_readme_says(self):
        c = verdict.primary(self.analysis)
        self.assertEqual((c["caffeine"]["n"], c["no_caffeine"]["n"], c["label"]), (3, 11, "hint"))
        self.assertIsNotNone(c["ci95"])                 # a range is shown even at the hint level

    def test_the_normal_message_in_the_readme_is_real(self):
        m = verdict.build_message(self.analysis, lambda system, user: self.STAND_IN)
        self.assertEqual(m["source"], "ai")
        self.assertIn(m["text"], self.readme)

    def test_the_fallback_message_in_the_readme_is_real(self):
        def down(system, user):
            raise ConnectionError("down")
        m = verdict.build_message(self.analysis, down)
        self.assertEqual(m["source"], "template")
        self.assertIn(m["text"], self.readme)


class SafetyNetTests(unittest.TestCase):
    def test_the_real_api_was_never_reached_with_a_working_client(self):
        # Every attempt at the real client in this module was blocked and cleaned up by the tests that make it.
        self.assertEqual(REAL_API_ATTEMPTS, [])

    def test_model_and_settings(self):
        self.assertEqual(verdict.MODEL, "claude-haiku-5-5")
        self.assertEqual(verdict.MAX_ATTEMPTS, 2)


if __name__ == "__main__":
    unittest.main()
