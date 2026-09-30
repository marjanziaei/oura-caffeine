"""One-time login: authorize this app with your Oura account and save the tokens.

Run:  python login.py

Flow (OAuth2 authorization code):
  1. Open the browser to Oura's login/consent page.
  2. Oura redirects back to http://localhost:8080/callback?code=...&state=...
  3. A temporary local server catches that request.
  4. Exchange the one-time code for an access token and a refresh token.
"""
import secrets
import socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from oura_client import (
    AUTHORIZE_URL,
    REDIRECT_URI,
    SCOPES,
    TOKEN_FILE,
    load_config,
    request_tokens,
    save_tokens,
    token_lock,
)

LOGIN_TIMEOUT_SECONDS = 300


class CallbackServer(HTTPServer):
    """HTTPServer bound to whatever address the hostname resolves to first, IPv4 or IPv6.

    Plain HTTPServer is IPv4-only, but "localhost" may resolve to ::1 (IPv6),
    which is where the browser will send the redirect.
    """

    def __init__(self, host, port, handler):
        family, _, _, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
        self.address_family = family
        super().__init__(address[:2], handler)


class CallbackHandler(BaseHTTPRequestHandler):
    """Handles the single redirect from Oura and stores its query parameters on the server."""

    def do_GET(self):
        url = urlparse(self.path)
        if url.path != urlparse(REDIRECT_URI).path:
            self.send_error(404)  # e.g. the browser asking for /favicon.ico
            return
        self.server.result = {key: values[0] for key, values in parse_qs(url.query).items()}

        ok = "code" in self.server.result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = "Logged in to Oura. You can close this tab." if ok else "Login failed. Check the terminal."
        self.wfile.write(f"<h2>{message}</h2>".encode())

    def log_message(self, *args):
        pass  # keep the terminal output clean


def main():
    client_id, _ = load_config()
    state = secrets.token_urlsafe(16)
    auth_url = AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
    })

    redirect = urlparse(REDIRECT_URI)
    try:
        server = CallbackServer(redirect.hostname, redirect.port, CallbackHandler)
    except OSError as e:
        raise SystemExit(f"Can't listen on port {redirect.port} ({e}). Is something else using it?")

    with server:
        server.result = None
        server.timeout = 1  # wake up every second so we can check the overall deadline
        print("Opening your browser to log in to Oura...")
        print(f"If it doesn't open, visit this URL:\n\n{auth_url}\n")
        webbrowser.open(auth_url)

        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        while server.result is None and time.monotonic() < deadline:
            server.handle_request()
        result = server.result

    if result is None:
        raise SystemExit("Timed out waiting for the Oura login.")
    if "error" in result:
        raise SystemExit(f"Oura returned an error: {result['error']}")
    if result.get("state") != state:
        raise SystemExit("State mismatch: this response didn't come from our login request. Ignoring it.")

    tokens = request_tokens({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
    })
    with token_lock():
        save_tokens(tokens)
    print(f"Saved tokens to {TOKEN_FILE.name} (scope: {tokens.get('scope')}).")


if __name__ == "__main__":
    main()
