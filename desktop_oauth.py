"""Small, dependency-free OAuth helper for ScreenSolve desktop clients.

The browser-based Google flow uses PKCE and a loopback callback.  Google and
Supabase secrets stay in their dashboards; this module receives only the
short-lived Supabase authorization code.
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer


CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 51824
CALLBACK_PATH = "/auth/callback"


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        # The browser callback is expected and should not fill the customer's log.
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        values = urllib.parse.parse_qs(parsed.query)
        state = values.get("state", [""])[0]
        if not secrets.compare_digest(state, self.server.expected_state):
            self.server.result = (None, "The sign-in response could not be verified. Please try again.")
        elif values.get("error"):
            self.server.result = (None, values.get("error_description", ["Google sign-in was cancelled."])[0])
        elif values.get("code"):
            self.server.result = (values["code"][0], None)
        else:
            self.server.result = (None, "Google did not return a sign-in code. Please try again.")
        page = """<!doctype html><html><head><meta charset='utf-8'><title>ScreenSolve</title></head>
<body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#0b1020;color:#f8fafc;padding:52px'>
<h1>You're signed in.</h1><p>You can close this tab and return to ScreenSolve.</p></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))


def google_sign_in(supabase_url, public_key, timeout_seconds=180):
    """Open Google sign-in and return the Supabase session, or raise RuntimeError."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    try:
        server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
    except OSError as error:
        raise RuntimeError("Google sign-in is already open in another ScreenSolve window. Finish or close it, then try again.") from error
    server.timeout = 0.5
    server.expected_state = state
    server.result = None
    callback = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
    query = urllib.parse.urlencode({
        "provider": "google",
        "redirect_to": callback,
        "code_challenge": challenge,
        "code_challenge_method": "s256",
        "state": state,
    })
    authorize_url = f"{supabase_url.rstrip('/')}/auth/v1/authorize?{query}"
    try:
        if not webbrowser.open(authorize_url):
            raise RuntimeError("Your browser could not be opened. Please open Google sign-in from a browser-enabled desktop session.")
        deadline = time.monotonic() + timeout_seconds
        while server.result is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if server.result is None:
        raise RuntimeError("Google sign-in timed out. Please try again.")
    code, callback_error = server.result
    if callback_error:
        raise RuntimeError(callback_error)
    request = urllib.request.Request(
        f"{supabase_url.rstrip('/')}/auth/v1/token?grant_type=pkce",
        data=json.dumps({"auth_code": code, "code_verifier": verifier}).encode("utf-8"),
        method="POST",
        headers={"apikey": public_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("message") or parsed.get("error") or detail
        except json.JSONDecodeError:
            pass
        raise RuntimeError("Google sign-in could not be completed. Please try again.") from error
