"""Print the last 14 nights: date, sleep score, and resting heart rate.

Run:  python print_sleep.py   (after `python login.py`)
"""
from datetime import date, timedelta

from oura_client import api_get

NIGHTS = 14


def main():
    today = date.today()
    # Ask for a slightly wider window than needed and trim afterwards, so the
    # result doesn't depend on whether Oura treats end_date as inclusive.
    params = {
        "start_date": (today - timedelta(days=NIGHTS)).isoformat(),
        "end_date": (today + timedelta(days=1)).isoformat(),
    }

    scores = {doc["day"]: doc["score"] for doc in api_get("daily_sleep", params)}

    # Resting HR in bpm is the lowest heart rate during the main sleep ("long_sleep").
    # (daily_readiness has a resting_heart_rate field, but it's a 1-100 score, not bpm.)
    resting_hr = {}
    for period in api_get("sleep", params):
        hr = period.get("lowest_heart_rate")
        if period.get("type") == "long_sleep" and hr is not None:
            day = period["day"]
            resting_hr[day] = min(hr, resting_hr.get(day, hr))

    days = sorted(set(scores) | set(resting_hr))[-NIGHTS:]
    if not days:
        print("No sleep data found for the last 14 nights.")
        return

    print(f"{'Date':<12}{'Sleep score':>12}{'Resting HR':>14}")
    for day in days:
        score = scores.get(day)
        hr = resting_hr.get(day)
        score_text = str(score) if score is not None else "-"
        hr_text = f"{hr} bpm" if hr is not None else "-"
        print(f"{day:<12}{score_text:>12}{hr_text:>14}")


if __name__ == "__main__":
    main()
