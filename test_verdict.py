"""Tests for verdict.py and weekly_message.py. A fake API only: the real one is never called.

Run:  .venv/bin/python -m unittest test_verdict -v
Made-up data in scratch databases (see test_analysis.Scratch).
"""
import json
import unittest
from contextlib import closing
from datetime import date
from unittest import mock

import requests

import analysis
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
        elif kind == "finding":                  # a clear difference, plenty of nights
            for score, hr in zip(spread(70, 12), spread(62, 12)):
                s.add("yes", score, hr=hr, nap=25)
            for score, hr in zip(spread(85, 12), spread(52, 12)):
                s.add("no", score, hr=hr, nap=5)
        elif kind == "none":                     # no difference
            for score, hr in zip(spread(80, 12), spread(55, 12)):
                s.add("yes", score, hr=hr)
                s.add("no", score, hr=hr)
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


def good_text(a):
    """A valid verdict whose numbers are taken from the analysis itself."""
    c = verdict.primary(a)
    cn, nn, ci = c["caffeine"], c["no_caffeine"], c["ci95"]
    return (f"On nights with caffeine after 5pm the sleep score averaged {cn['mean']} points, against "
            f"{nn['mean']} points on the other nights. The 95% range for the difference runs from "
            f"{ci['low']} to {ci['high']} points. This is an association, and a general improvement in health "
            "over time could be mistaken for a caffeine effect.")


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
        self.assertIn("primary_comparison", sent)

    def test_a_date_that_gets_in_anyway_stops_the_send(self):
        a = make_analysis("finding")
        a["caveats"] = a["caveats"] + ["Night of 2026-10-03 was odd."]
        with self.assertRaises(ValueError):
            verdict.to_payload(a)
        api = FakeAPI(good_text(make_analysis("finding")))
        with self.assertRaises(ValueError):
            verdict.build_message(a, api)
        self.assertEqual(api.calls, [])                       # nothing was sent

    def test_the_model_receives_only_the_payload(self):
        a = make_analysis("finding")
        api = FakeAPI(good_text(a))
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

    def problems(self, text, label="finding"):
        return verdict.problems_with(text, self.payload, label)

    def test_good_text_passes(self):
        self.assertEqual(self.problems(good_text(self.a)), [])

    def test_invented_number_is_caught(self):
        text = good_text(self.a).replace("5pm", "5pm and a 37 point swing")
        self.assertTrue(any("37" in p for p in self.problems(text)))

    def test_spelled_out_numbers_and_dates_are_caught(self):
        self.assertTrue(self.problems("It dropped by about eight points."))
        self.assertTrue(self.problems("On 3 October it dropped."))
        self.assertTrue(self.problems("At 21:00 it dropped."))

    def test_certainty_words_depend_on_the_label(self):
        text = "This is a significant finding."
        self.assertEqual(self.problems(text, "finding"), [])
        self.assertTrue(self.problems(text, "hint"))
        self.assertTrue(self.problems(text, "early signal"))
        self.assertTrue(self.problems("This proves it.", "finding"))      # never allowed
        self.assertTrue(self.problems("This is an early signal.", "hint"))

    def test_advice_markdown_and_label_line_are_rejected(self):
        self.assertTrue(self.problems("You should ask your doctor."))
        self.assertTrue(self.problems("Try to avoid caffeine."))
        self.assertTrue(self.problems("**Result** is fine."))
        self.assertTrue(self.problems("- first point\n- second point"))
        self.assertTrue(self.problems("Confidence: FINDING. Fine."))
        self.assertTrue(self.problems("x" * (verdict.MAX_TEXT_CHARS + 1)))
        self.assertTrue(self.problems("   "))


class BuildMessageTests(unittest.TestCase):
    def test_good_answer_is_used_and_label_line_comes_first(self):
        for kind in ("hint", "finding", "none"):
            a = make_analysis(kind)
            label = verdict.primary(a)["label"]
            ci = verdict.primary(a)["ci95"]
            text = good_text(a) if ci else "There are not enough nights yet to give a range."
            api = FakeAPI(text)
            m = verdict.build_message(a, api)
            self.assertEqual(m["source"], "ai", kind)
            self.assertEqual(m["attempts"], 1)
            first_line = m["text"].splitlines()[0]
            self.assertTrue(first_line.startswith(f"Confidence: {label.upper()}"), first_line)
            self.assertEqual(first_line, verdict.confidence_line(a))

    def test_ai_cannot_change_the_label_line(self):
        a = make_analysis("hint")
        api = FakeAPI("Confidence: FINDING. This is certain.", "Not enough nights yet to say anything firm.")
        m = verdict.build_message(a, api)
        self.assertTrue(m["text"].startswith("Confidence: HINT"))
        self.assertNotIn("FINDING", m["text"])
        self.assertEqual(m["attempts"], 2)

    def test_invented_number_triggers_one_retry_which_can_succeed(self):
        a = make_analysis("finding")
        invented = good_text(a).replace("5pm", "5pm, a 37 point swing")
        api = FakeAPI(invented, good_text(a))
        m = verdict.build_message(a, api)
        self.assertEqual((m["source"], m["attempts"], len(api.calls)), ("ai", 2, 2))
        self.assertIn("37", m["problems"][0][0])
        self.assertIn("rejected", api.calls[1]["user"])        # the retry says what was wrong
        self.assertIn("37", api.calls[1]["user"])
        self.assertNotIn("37", m["text"])

    def test_invented_number_twice_falls_back_to_the_plain_template(self):
        a = make_analysis("finding")
        invented = good_text(a).replace("5pm", "5pm, a 37 point swing")
        api = FakeAPI(invented, invented, invented)
        m = verdict.build_message(a, api)
        self.assertEqual(len(api.calls), 2)                      # one try plus exactly one retry
        self.assertEqual(m["source"], "template")
        self.assertNotIn("37", m["text"])
        self.assertIn("without AI", m["text"])
        self.assertTrue(m["text"].startswith("Confidence: FINDING"))

    def test_api_down_gives_the_plain_template(self):
        a = make_analysis("finding")
        for error in (ConnectionError("down"), TimeoutError("slow"), RuntimeError("model stopped early (refusal)")):
            api = FakeAPI(error)
            m = verdict.build_message(a, api)
            self.assertEqual((m["source"], len(api.calls)), ("template", 2), repr(error))
            self.assertTrue(m["text"].startswith("Confidence: FINDING"))
            self.assertIn("the call failed", m["problems"][0][0])

    def test_down_then_up_recovers_on_the_retry(self):
        a = make_analysis("finding")
        m = verdict.build_message(a, FakeAPI(ConnectionError("blip"), good_text(a)))
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


class TemplateTests(unittest.TestCase):
    def test_template_only_contains_numbers_from_the_data(self):
        for kind in ("hint", "finding", "none", "all_tagged", "flip", "only_backfilled_caffeine", "empty"):
            a = make_analysis(kind)
            text = verdict.fallback_text(a)
            payload = verdict.to_payload(a)
            self.assertEqual(verdict.unknown_numbers(text, payload), [], kind)
            self.assertEqual(verdict.problems_with(text, payload, verdict.primary(a)["label"]), [], kind)

    def test_template_content(self):
        a = make_analysis("finding")
        text = verdict.fallback_text(a)
        self.assertIn("95% range", text)
        self.assertIn("worse", text)
        self.assertIn("association", text)
        self.assertIn("lower", text)

    def test_template_when_there_is_nothing_to_compare(self):
        for kind in ("all_tagged", "empty"):
            text = verdict.fallback_text(make_analysis(kind))
            self.assertIn("not enough data yet", text)

    def test_template_mentions_flags_only_when_relevant(self):
        flip = verdict.fallback_text(make_analysis("flip"))
        self.assertIn("after the fact changes", flip)
        self.assertNotIn("after the fact", verdict.fallback_text(make_analysis("none")))
        self.assertIn("dozing differs", verdict.fallback_text(make_analysis("finding")))
        self.assertNotIn("dozing", verdict.fallback_text(make_analysis("none")))

    def test_message_fits_in_a_telegram_message(self):
        for kind in ("hint", "finding", "flip"):
            a = make_analysis(kind)
            self.assertLess(len(verdict.build_message(a, FakeAPI(ConnectionError()))["text"]), 4096)


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


class SafetyNetTests(unittest.TestCase):
    def test_the_real_api_was_never_reached_with_a_working_client(self):
        # Every attempt at the real client in this module was blocked and cleaned up by the tests that make it.
        self.assertEqual(REAL_API_ATTEMPTS, [])

    def test_model_and_settings(self):
        self.assertEqual(verdict.MODEL, "claude-haiku-5-5")
        self.assertEqual(verdict.MAX_ATTEMPTS, 2)


if __name__ == "__main__":
    unittest.main()
