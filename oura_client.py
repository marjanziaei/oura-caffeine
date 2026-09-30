"""Shared Oura helpers: config, token storage, token refresh, and API requests.

Endpoints and scopes come from Oura's API v2 OpenAPI spec (v1.41):
https://cloud.ouraring.com/v2/docs
"""
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
TOKEN_FILE = PROJECT_DIR / ".oura_tokens.json"
LOCK_FILE = PROJECT_DIR / ".oura_tokens.lock"

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/usercollection"
REDIRECT_URI = "http://localhost:8080/callback"

# "daily" covers daily sleep, activity and readiness summaries plus sleep periods.
SCOPES = ["daily"]

# Refresh slightly early so a token never expires between checking it and using it.
EXPIRY_MARGIN_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 30


def load_config():
    """Return (client_id, client_secret) from .env, or exit with a helpful message."""
    load_dotenv(PROJECT_DIR / ".env")
    client_id = os.getenv("OURA_CLIENT_ID", "")
    client_secret = os.getenv("OURA_CLIENT_SECRET", "")
    for value in (client_id, client_secret):
        if not value or value.startswith("your_"):
            raise SystemExit("Put your real OURA_CLIENT_ID and OURA_CLIENT_SECRET in .env first.")
    return client_id, client_secret


@contextmanager
def token_lock():
    """Hold an exclusive lock so two processes never spend the same refresh token."""
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save_tokens(token_response):
    """Write tokens to TOKEN_FILE atomically, readable only by you.

    Writes to a temp file, flushes it to disk, then renames it over the old file.
    A rename is atomic, so a crash leaves either the old file or the new one,
    never a half-written file.
    """
    tokens = {
        "access_token": token_response["access_token"],
        "refresh_token": token_response["refresh_token"],
        "expires_at": int(time.time()) + int(token_response["expires_in"]),
        "scope": token_response.get("scope"),
    }
    # mkstemp creates the file with 0600 permissions (owner read/write only).
    fd, tmp_path = tempfile.mkstemp(dir=PROJECT_DIR, prefix=".oura_tokens.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(tokens, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, TOKEN_FILE)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return tokens


def load_tokens():
    if not TOKEN_FILE.exists():
        raise SystemExit("No saved tokens. Run `python login.py` first.")
    return json.loads(TOKEN_FILE.read_text())


def request_tokens(form):
    """POST to Oura's token endpoint with our client credentials; return the JSON response."""
    client_id, client_secret = load_config()
    resp = requests.post(
        TOKEN_URL,
        data={**form, "client_id": client_id, "client_secret": client_secret},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise SystemExit(f"Oura token request failed ({resp.status_code}): {resp.text}")
    return resp.json()


def get_access_token(force_refresh=False):
    """Return a valid access token, refreshing it first if it is about to expire."""
    with token_lock():
        # Read inside the lock: another process may have refreshed while we waited.
        tokens = load_tokens()
        if not force_refresh and tokens["expires_at"] - time.time() > EXPIRY_MARGIN_SECONDS:
            return tokens["access_token"]

        response = request_tokens({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        # The old refresh token is now dead. Persist the new one before doing anything else.
        response.setdefault("refresh_token", tokens["refresh_token"])
        return save_tokens(response)["access_token"]


def api_get(endpoint, params):
    """GET every page of a /v2/usercollection endpoint and return the combined `data` list."""
    params = dict(params)
    token = get_access_token()
    retried = False
    results = []
    while True:
        resp = requests.get(
            f"{API_BASE}/{endpoint}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        # A 401 means the token was rejected early (e.g. revoked); refresh once and retry.
        if resp.status_code == 401 and not retried:
            token = get_access_token(force_refresh=True)
            retried = True
            continue
        if resp.status_code != 200:
            raise SystemExit(f"Oura API error on {endpoint} ({resp.status_code}): {resp.text}")

        body = resp.json()
        results.extend(body["data"])
        if not body.get("next_token"):
            return results
        params["next_token"] = body["next_token"]
