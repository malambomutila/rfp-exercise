#!/usr/bin/env python3
"""Minimal login gate for the RFP opportunity report.

Why this exists
    The report is served as static files by the rfp_site nginx container. The
    IDinsight reviewers need a login page in front of it, and nginx on its own
    cannot validate a form POST, so this small service does three jobs:

      GET  /auth    answers nginx's auth_request subrequest, 204 or 401
      GET  /login   serves the login form
      POST /login   checks the credentials and sets a session cookie
      GET  /logout  clears the cookie

Design notes
    Sessions are stateless. The cookie carries an expiry timestamp and an
    HMAC-SHA256 signature over it, so there is no session store to keep and
    nothing to clean up. Restarting the container keeps existing sessions
    valid as long as RFP_SESSION_SECRET is set in the environment.

    Python standard library only, matching the rest of this project, so the
    container needs no pip install and no image build.

Scope, stated plainly
    This gates public tender data for a reviewer demo. It is a courtesy gate,
    not an access control for anything sensitive, and the credentials are
    shared rather than per-user.
"""

import base64
import hashlib
import hmac
import html
import os
import secrets
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Credentials come from the environment only, never from this file, because
# this file lives in a public repository. See deploy/server/.env.example.
USERNAME = os.environ.get("RFP_USER", "")
PASSWORD = os.environ.get("RFP_PASSWORD", "")

# A stable secret keeps sessions alive across restarts. If it is absent we
# generate an ephemeral one rather than falling back to a predictable default,
# which would let anyone forge a cookie.
SECRET = os.environ.get("RFP_SESSION_SECRET", "").encode() or secrets.token_bytes(32)
if not os.environ.get("RFP_SESSION_SECRET"):
    print(
        "warning: RFP_SESSION_SECRET is unset, sessions will not survive a "
        "restart of this container",
        file=sys.stderr,
    )

COOKIE_NAME = "rfp_session"
SESSION_HOURS = int(os.environ.get("RFP_SESSION_HOURS", "12"))
PORT = int(os.environ.get("RFP_AUTH_PORT", "8000"))


def _sign(value):
    """Return the HMAC-SHA256 tag for a cookie payload."""
    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()


def _issue_cookie():
    """Build a signed cookie value carrying its own expiry."""
    expires = int(time.time()) + SESSION_HOURS * 3600
    payload = base64.urlsafe_b64encode(str(expires).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload)}"


def _cookie_valid(raw):
    """True when the cookie signature verifies and the expiry is in the future."""
    if not raw or "." not in raw:
        return False
    payload, _, tag = raw.rpartition(".")
    # compare_digest avoids leaking signature bytes through timing.
    if not hmac.compare_digest(tag, _sign(payload)):
        return False
    try:
        padded = payload + "=" * (-len(payload) % 4)
        expires = int(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, TypeError):
        return False
    return expires > time.time()


def _credentials_ok(user, password):
    """Constant-time credential check.

    Both comparisons always run, so a wrong username and a wrong password take
    the same time. Empty configured credentials always fail, so a
    misconfigured container denies access rather than allowing everyone in.
    """
    if not USERNAME or not PASSWORD:
        return False
    user_ok = hmac.compare_digest(user, USERNAME)
    pass_ok = hmac.compare_digest(password, PASSWORD)
    return user_ok and pass_ok


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Sign in, Opportunity Finder</title>
<style>
  :root {
    --bg: #f4f6f7; --panel: #ffffff; --ink: #14202a; --muted: #5c6b76;
    --line: #d9e0e4; --accent: #1f4e5f; --accent-ink: #ffffff; --warn: #8a2b2b;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #10161b; --panel: #18212a; --ink: #e8eef2; --muted: #9aabb6;
      --line: #2a3742; --accent: #5fa8bf; --accent-ink: #0d1418; --warn: #e88f8f;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; background: var(--bg); color: var(--ink);
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
    display: flex; align-items: center; justify-content: center;
    padding: 24px 16px;
  }
  .card {
    width: 100%; max-width: 380px; background: var(--panel);
    border: 1px solid var(--line); border-radius: 10px; padding: 28px 24px;
  }
  h1 { font-size: 1.15rem; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 0.875rem; margin: 0 0 22px; }
  label {
    display: block; font-size: 0.8rem; font-weight: 600; letter-spacing: 0.02em;
    text-transform: uppercase; color: var(--muted); margin: 0 0 5px;
  }
  input {
    width: 100%; padding: 10px 12px; margin: 0 0 16px; font-size: 0.95rem;
    color: var(--ink); background: var(--bg); border: 1px solid var(--line);
    border-radius: 6px;
  }
  input:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  button {
    width: 100%; padding: 11px 14px; font-size: 0.95rem; font-weight: 600;
    color: var(--accent-ink); background: var(--accent); border: 0;
    border-radius: 6px; cursor: pointer;
  }
  button:hover { filter: brightness(1.08); }
  .error {
    color: var(--warn); font-size: 0.875rem; margin: 0 0 16px;
    padding: 9px 11px; border-left: 3px solid var(--warn); background: var(--bg);
  }
  .note {
    color: var(--muted); font-size: 0.8rem; margin: 20px 0 0;
    padding-top: 16px; border-top: 1px solid var(--line);
  }
</style>
</head>
<body>
  <main class="card">
    <h1>IDinsight Opportunity Finder</h1>
    <p class="sub">Sign in to view the daily RFP report.</p>
    __ERROR__
    <form method="post" action="/login" autocomplete="on">
      <label for="u">Username</label>
      <input id="u" name="username" type="text" required autofocus
             autocapitalize="none" autocomplete="username">
      <label for="p">Password</label>
      <input id="p" name="password" type="password" required
             autocomplete="current-password">
      <button type="submit">Sign in</button>
    </form>
    <p class="note">Shared demo access for IDinsight reviewers. The report
    contains public tender notices only.</p>
  </main>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "rfp-auth"
    # Keep the default BaseHTTPRequestHandler banner out of responses.
    sys_version = ""

    def log_message(self, fmt, *args):
        """Log to stderr without the client address, which nginx already logs."""
        sys.stderr.write("auth: " + (fmt % args) + "\n")

    def _cookie(self):
        """Pull our session cookie out of the request, if present."""
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return ""

    def _secure_flag(self):
        """Only mark the cookie Secure when the request really came over TLS.

        nginx passes X-Forwarded-Proto. Without this check the cookie would
        never be stored when testing against the loopback port over plain HTTP.
        """
        proto = self.headers.get("X-Forwarded-Proto", "")
        return "; Secure" if proto == "https" else ""

    def _send(self, code, body=b"", content_type="text/html; charset=utf-8",
              extra_headers=()):
        self.send_response(code)
        if body:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        # The login page must never be cached, or a signed-out reviewer can be
        # shown a stale authenticated view from the browser cache.
        self.send_header("Cache-Control", "no-store")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _login_page(self, code=200, error=""):
        block = (
            f'<p class="error">{html.escape(error)}</p>' if error else ""
        )
        page = LOGIN_PAGE.replace("__ERROR__", block).encode()
        self._send(code, page)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/auth":
            # nginx auth_request: body is discarded, only the status matters.
            self._send(204 if _cookie_valid(self._cookie()) else 401)
        elif path == "/login":
            # Already signed in, so skip the form.
            if _cookie_valid(self._cookie()):
                self._send(302, extra_headers=(("Location", "/"),))
            else:
                self._login_page()
        elif path == "/logout":
            self._send(302, extra_headers=(
                ("Location", "/login"),
                ("Set-Cookie",
                 f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
                 + self._secure_flag()),
            ))
        elif path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
        else:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/login":
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return

        # Cap the body so a large POST cannot exhaust memory.
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = urllib.parse.parse_qs(raw)
        user = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]

        if _credentials_ok(user, password):
            self._send(302, extra_headers=(
                ("Location", "/"),
                ("Set-Cookie",
                 f"{COOKIE_NAME}={_issue_cookie()}; Path=/; HttpOnly; "
                 f"SameSite=Lax; Max-Age={SESSION_HOURS * 3600}"
                 + self._secure_flag()),
            ))
        else:
            # A deliberate small delay blunts scripted guessing without
            # needing any state. nginx also rate limits this endpoint.
            time.sleep(0.5)
            self._login_page(401, "Those credentials were not recognised.")


if __name__ == "__main__":
    if not USERNAME or not PASSWORD:
        print(
            "warning: RFP_USER or RFP_PASSWORD is unset, every login will be "
            "refused until both are provided",
            file=sys.stderr,
        )
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"auth: listening on port {PORT}", file=sys.stderr)
    server.serve_forever()
