"""Weekly analysis: nights with caffeine after 5pm against nights without. Prints JSON.

Calculations only (DECISIONS.md #12). Read-only: the database is opened so it cannot be changed.

    python analysis.py [--db PATH] [--since 2026-10-01] [--as-of 2026-10-19]
"""
import argparse
import json
import math
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from db import DB_FILE

PRAGUE = ZoneInfo("Europe/Prague")
DEFAULT_SINCE = "2026-10-01"

# The label rule (DECISIONS.md #12). Checked in this order, first match wins.
MIN_NIGHTS_PER_GROUP = 5      # fewer than this in either group -> "hint"
MIN_NIGHTS_FOR_FINDING = 10   # "finding" needs this many in both groups ...
D_FINDING = 0.8               # ... a large difference, and a 95% interval that excludes zero
D_EARLY_SIGNAL = 0.5          # a medium difference
NAP_DIFF_MINUTES = 10         # evening dozing "differs" at this many minutes between group averages
NAP_MIN_NIGHTS = 5            # per group, with a measured value

EXCLUDED_TAGS = ("alcohol", "illness", "travel", "other")
GROUPS = ("caffeine", "no_caffeine")

# Which way is better is stated here and copied into the output, so later steps never guess.
METRICS = {
    "sleep_score": {"unit": "points", "better_direction": "higher"},
    "lowest_heart_rate": {"unit": "bpm", "better_direction": "lower"},
}
VIEWS = ("all_answers", "without_backfilled")
PRIMARY = ("sleep_score", "without_backfilled")

EXCLUSION_WHY = {
    "unanswered": "No caffeine answer for that evening. Counting it as 'no' would dilute the 'no' group.",
    "tagged_alcohol": "Tagged alcohol: an unusual night that could hide or fake a caffeine effect.",
    "tagged_illness": "Tagged illness: an unusual night that could hide or fake a caffeine effect.",
    "tagged_travel": "Tagged travel: an unusual night that could hide or fake a caffeine effect.",
    "tagged_other": "Tagged other: an unusual night that could hide or fake a caffeine effect.",
    "tag_not_answered": "Caffeine was answered but the 'anything unusual' follow-up was not, so it is "
                        "unknown whether the night was unusual. Not treated as 'nothing unusual'.",
}

CAVEATS = [
    "Four comparisons are made (2 metrics x 2 views), so one of them can look interesting by chance.",
    "Only the primary comparison (sleep score, without backfilled answers) is the headline; the rest are secondary.",
    "Nights are not randomised. Anything that changes over time (for example a general health "
    "improvement) can look like a caffeine effect if caffeine days and non-caffeine days are unevenly "
    "spread over time. No time trend is removed here.",
    "The difference is an association between caffeine in the evening and that night's numbers, not proof of cause.",
]


# ---------------------------------------------------------------------------------------------
# Statistics, standard library only
# ---------------------------------------------------------------------------------------------

def _betacf(a, b, x):
    """Continued fraction for the incomplete beta function (modified Lentz method)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _betainc(a, b, x):
    """Regularised incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_cdf(t, df):
    """P(T <= t) for Student's t with df degrees of freedom (df may be fractional)."""
    tail = 0.5 * _betainc(df / 2.0, 0.5, df / (df + t * t))
    return 1.0 - tail if t > 0 else tail


def t_critical(df, confidence=0.95):
    """The t value with `confidence` of the distribution between -t and +t."""
    target = 1.0 - (1.0 - confidence) / 2.0
    lo, hi = 0.0, 1.0
    while t_cdf(hi, df) < target:
        hi *= 2.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def summarize(values):
    n = len(values)
    mean = sum(values) / n if n else None
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n >= 2 else None
    return {"n": n, "mean": mean, "sd": sd}


def compare(caffeine, no_caffeine):
    """Difference in averages (caffeine minus no caffeine), its size compared to normal variation
    (Cohen's d with the pooled spread), and a 95% Welch interval in the metric's own units."""
    a, b = summarize(caffeine), summarize(no_caffeine)
    out = {"difference": None, "pooled_sd": None, "effect_size_d": None, "ci95": None}
    if a["n"] == 0 or b["n"] == 0:
        return out, None
    diff = a["mean"] - b["mean"]
    out["difference"] = diff
    if a["n"] < 2 or b["n"] < 2:
        return out, None

    pooled = math.sqrt(((a["n"] - 1) * a["sd"] ** 2 + (b["n"] - 1) * b["sd"] ** 2) / (a["n"] + b["n"] - 2))
    out["pooled_sd"] = pooled
    if pooled > 0:
        d_for_label = diff / pooled
        out["effect_size_d"] = d_for_label
    else:
        # No spread at all in either group: any difference is infinitely large compared to it.
        d_for_label = 0.0 if diff == 0 else math.copysign(math.inf, diff)

    va, vb = a["sd"] ** 2 / a["n"], b["sd"] ** 2 / b["n"]
    if va + vb == 0:
        low = high = diff
        method = "Welch (no spread in either group, interval has zero width)"
    else:
        df = (va + vb) ** 2 / (va ** 2 / (a["n"] - 1) + vb ** 2 / (b["n"] - 1))
        half = t_critical(df) * math.sqrt(va + vb)
        low, high = diff - half, diff + half
        method = f"Welch t, {df:.1f} degrees of freedom"
    out["ci95"] = {"low": low, "high": high, "excludes_zero": low > 0 or high < 0, "method": method}
    return out, d_for_label


def label_for(n_caffeine, n_no_caffeine, d, ci):
    """Fixed rule, first match wins. Returns (label, reason)."""
    smallest = min(n_caffeine, n_no_caffeine)
    if smallest < MIN_NIGHTS_PER_GROUP:
        return "hint", f"a group has {smallest} night(s), fewer than {MIN_NIGHTS_PER_GROUP}"
    if d is None or ci is None:
        return "hint", "the spread could not be calculated"
    size = abs(d)
    if size >= D_FINDING and smallest >= MIN_NIGHTS_FOR_FINDING and ci["excludes_zero"]:
        return "finding", (f"|d| {_fmt(size)} >= {D_FINDING}, at least {MIN_NIGHTS_FOR_FINDING} nights "
                           "in both groups, and the 95% interval excludes zero")
    if size >= D_EARLY_SIGNAL:
        why = f"|d| {_fmt(size)} >= {D_EARLY_SIGNAL}"
        if size >= D_FINDING:
            missing = []
            if smallest < MIN_NIGHTS_FOR_FINDING:
                missing.append(f"a group has fewer than {MIN_NIGHTS_FOR_FINDING} nights")
            if not ci["excludes_zero"]:
                missing.append("the 95% interval includes zero")
            why += f", but not a finding because {' and '.join(missing)}"
        return "early signal", why
    return "no clear difference", f"|d| {_fmt(size)} < {D_EARLY_SIGNAL}"


def _fmt(x):
    return "infinite" if math.isinf(x) else f"{x:.2f}"


def _round(x, places=2):
    return None if x is None else round(x, places)


# ---------------------------------------------------------------------------------------------
# Selecting nights
# ---------------------------------------------------------------------------------------------

def classify(row):
    """('caffeine' | 'no_caffeine', None) if the night is used, else (None, reason). One reason each."""
    answer = row["caffeine_after_5pm"]
    if answer not in ("yes", "no"):
        return None, "unanswered"
    if row["tag"] in EXCLUDED_TAGS:
        return None, f"tagged_{row['tag']}"
    if row["tag"] != "none":
        return None, "tag_not_answered"
    return ("caffeine" if answer == "yes" else "no_caffeine"), None


def split_nights(rows):
    groups = {g: [] for g in GROUPS}
    excluded = dict.fromkeys(EXCLUSION_WHY, 0)
    for row in rows:
        group, reason = classify(row)
        if group:
            groups[group].append(row)
        else:
            excluded[reason] += 1
    return groups, excluded


def view_rows(groups, view):
    if view == "all_answers":
        return groups
    return {g: [r for r in rows if not r["backfilled"]] for g, rows in groups.items()}


def values(rows, column):
    return [r[column] for r in rows if r[column] is not None]


def group_summary(rows, column):
    s = summarize(values(rows, column))
    return {"nights": len(rows), "n": s["n"], "dropped_missing_value": len(rows) - s["n"],
            "mean": _round(s["mean"]), "sd": _round(s["sd"])}


# ---------------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------------

def build_comparison(groups, metric, view):
    info = METRICS[metric]
    chosen = view_rows(groups, view)
    stats, d_for_label = compare(values(chosen["caffeine"], metric), values(chosen["no_caffeine"], metric))
    n_c, n_n = (len(values(chosen[g], metric)) for g in GROUPS)
    label, reason = label_for(n_c, n_n, d_for_label, stats["ci95"])

    diff = stats["difference"]
    if diff is None:
        looks = None
    elif diff == 0:
        looks = "same"
    else:
        looks = "better" if (diff > 0) == (info["better_direction"] == "higher") else "worse"

    ci = stats["ci95"]
    return {
        "id": f"{metric}:{view}",
        "metric": metric,
        "view": view,
        "primary": (metric, view) == PRIMARY,
        "unit": info["unit"],
        "better_direction": info["better_direction"],
        "caffeine": group_summary(chosen["caffeine"], metric),
        "no_caffeine": group_summary(chosen["no_caffeine"], metric),
        "difference": _round(diff),
        "caffeine_nights_look": looks,
        "pooled_sd": _round(stats["pooled_sd"]),
        "effect_size_d": _round(stats["effect_size_d"]),
        "ci95": None if ci is None else {"low": _round(ci["low"]), "high": _round(ci["high"]),
                                         "excludes_zero": ci["excludes_zero"], "method": ci["method"]},
        "label": label,
        "label_reason": reason,
    }


def build_backfill_check(comparisons):
    by_id = {c["id"]: c for c in comparisons}
    checks = []
    for metric in METRICS:
        a, b = by_id[f"{metric}:all_answers"], by_id[f"{metric}:without_backfilled"]
        same_label = a["label"] == b["label"]
        same_direction = a["caffeine_nights_look"] == b["caffeine_nights_look"]
        checks.append({
            "metric": metric,
            "all_answers_label": a["label"],
            "without_backfilled_label": b["label"],
            "same_label": same_label,
            "same_direction": same_direction,
            "changes_result": not (same_label and same_direction),
        })
    return checks


def build_nap_check(groups):
    out = []
    for view in VIEWS:
        chosen = view_rows(groups, view)
        s = {g: summarize(values(chosen[g], "pre_sleep_nap_minutes")) for g in GROUPS}
        entry = {"view": view, "unit": "minutes",
                 "caffeine": {"n": s["caffeine"]["n"], "mean": _round(s["caffeine"]["mean"])},
                 "no_caffeine": {"n": s["no_caffeine"]["n"], "mean": _round(s["no_caffeine"]["mean"])},
                 "difference": None}
        if min(s["caffeine"]["n"], s["no_caffeine"]["n"]) < NAP_MIN_NIGHTS:
            entry["flag"] = "not enough data"
        else:
            diff = s["caffeine"]["mean"] - s["no_caffeine"]["mean"]
            entry["difference"] = _round(diff)
            entry["flag"] = "differs" if abs(diff) >= NAP_DIFF_MINUTES else "similar"
        out.append(entry)
    return out


def build_last_7_days(rows, as_of):
    start, end = as_of - timedelta(days=7), as_of - timedelta(days=1)   # 7 evenings with a finished night
    window = [r for r in rows if start.isoformat() <= r["evening_date"] <= end.isoformat()]
    groups, excluded = split_nights(window)
    used = sum(len(v) for v in groups.values())
    return {
        "evenings_from": start.isoformat(), "evenings_to": end.isoformat(),
        "nights_found": len(window),
        "nights_used": used,
        "backfilled_among_used": sum(1 for g in groups.values() for r in g if r["backfilled"]),
        "excluded": {"total": sum(excluded.values()), "by_reason": excluded},
        "groups": {m: {g: group_summary(groups[g], m) for g in GROUPS} for m in METRICS},
        "label": "hint" if min(len(groups["caffeine"]), len(groups["no_caffeine"])) < MIN_NIGHTS_PER_GROUP else None,
        "note": "A single week is summarised, not tested. Averages only.",
    }


def analyse(conn, since=DEFAULT_SINCE, as_of=None):
    as_of = as_of or datetime.now(PRAGUE).date()
    rows = conn.execute(
        "SELECT evening_date, caffeine_after_5pm, tag, backfilled, sleep_score, lowest_heart_rate, "
        "pre_sleep_nap_minutes FROM nights_with_caffeine WHERE evening_date >= ? ORDER BY evening_date",
        (since,)).fetchall()
    groups, excluded = split_nights(rows)
    comparisons = [build_comparison(groups, m, v) for (m, v) in
                   [PRIMARY] + [(m, v) for m in METRICS for v in VIEWS if (m, v) != PRIMARY]]
    used = sum(len(v) for v in groups.values())
    return {
        "schema_version": 1,
        "period": {"evenings_since": since, "as_of": as_of.isoformat()},
        "rule": {
            "effect_size": "d = (average with caffeine - average without) / pooled night-to-night spread",
            "order": ["hint", "finding", "early signal", "no clear difference"],
            "min_nights_per_group": MIN_NIGHTS_PER_GROUP,
            "min_nights_per_group_for_finding": MIN_NIGHTS_FOR_FINDING,
            "d_finding": D_FINDING, "d_early_signal": D_EARLY_SIGNAL,
            "finding_also_requires": "95% interval for the difference excludes zero",
            "evening_dozing_flag_minutes": NAP_DIFF_MINUTES,
        },
        "primary_comparison": f"{PRIMARY[0]}:{PRIMARY[1]}",
        "nights": {
            "in_view": len(rows),
            "used": used,
            "left_out": {"total": sum(excluded.values()), "by_reason": excluded, "why": EXCLUSION_WHY},
            "backfilled_among_used": sum(1 for g in groups.values() for r in g if r["backfilled"]),
        },
        "comparisons": comparisons,
        "backfill_check": build_backfill_check(comparisons),
        "evening_dozing": build_nap_check(groups),
        "last_7_days": build_last_7_days(rows, as_of),
        "caveats": CAVEATS,
    }


def open_read_only(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(nights_with_caffeine)")}
    if not {"backfilled", "pre_sleep_nap_minutes"} <= columns:
        sys.exit("The database view is out of date. Start the bot or the sync once (they upgrade it), then retry.")
    return conn


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DB_FILE))
    parser.add_argument("--since", default=DEFAULT_SINCE, help="first evening to include (YYYY-MM-DD)")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None, help="today's date; for testing")
    args = parser.parse_args()
    with closing(open_read_only(args.db)) as conn:
        print(json.dumps(analyse(conn, args.since, args.as_of), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
