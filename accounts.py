"""User accounts for the Dash app: email/password signup and login, password
reset by email, and each user's saved watchlist.

Plain Flask routes (/login, /signup, /logout, /forgot-password,
/reset-password) with server-rendered HTML forms rather than Dash
callbacks -- a normal form POST can set the session cookie and redirect,
and keeps passwords out of Dash's callback plumbing. dash_app.py calls
init_accounts(server) once and then uses current_user / load_watchlist /
save_watchlist from its own callbacks.

Storage is one SQLite file at USER_DB_PATH. On Render that has to live on
the persistent disk (see render.yaml) -- the rest of the filesystem is
wiped on every deploy, which is fine for the other *.db caches but would
delete every account. gunicorn runs a single worker, so SQLite's
one-writer-at-a-time locking is never contended across processes.

Reset emails go out through Resend (RESEND_API_KEY). Without a key -- local
runs -- the reset link is printed to the console instead, so the whole
flow can be tested without sending anything.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import timedelta
from html import escape
from pathlib import Path

import flask
import requests
from flask_limiter import Limiter
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user
from flask_wtf.csrf import generate_csrf, validate_csrf
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import ValidationError

__all__ = ["init_accounts", "current_user", "generate_csrf", "load_watchlist", "save_watchlist",
           "MERGE_LOCAL_WATCHLIST_KEY"]

_DB_PATH = Path(os.environ.get("USER_DB_PATH") or Path(__file__).resolve().parent / "users.db")
_db_lock = threading.Lock()

# Deliberately loose (something@something.tld) -- signup doesn't verify the
# address, so this only catches typos like a missing @. A password reset
# only ever reaches whoever actually reads that inbox.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_EMAIL_LEN = 254
# A single free-form name field -- first name only, full name, a nickname;
# whatever people are comfortable with. Shown as "Welcome, <name>".
_MAX_NAME_LEN = 60
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 128
# Watchlist sanity caps -- items come from the browser, so bound what one
# account can store.
_MAX_WATCHLIST_ITEMS = 200
_MAX_FIELD_LEN = 200
_WATCHLIST_KINDS = {"company", "manager", "politician"}

_RESET_TOKEN_MAX_AGE = 3600  # seconds
_RESET_TOKEN_SALT = "password-reset"
# The token from a reset link, moved out of the URL into the session on
# first visit (see reset_password).
_RESET_TOKEN_SESSION_KEY = "password_reset_token"
_RESEND_API_URL = "https://api.resend.com/emails"
_EMAIL_FROM = os.environ.get("EMAIL_FROM") or "Stockpick <no-reply@stockpick.io>"

# Set in the session on login/signup; dash_app's page-load callback merges
# that browser's pre-login watchlist into the account once, then clears it.
# Only once, not on every page load -- otherwise an item removed on one
# device would come back from another device's stale local copy.
MERGE_LOCAL_WATCHLIST_KEY = "merge_local_watchlist"


def _connect():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db_lock, _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                name TEXT NOT NULL DEFAULT ''
            )""")
        # Accounts created before names were asked for: add the column in
        # place (existing rows get ''), keeping every account as-is.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "name" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlists (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                items_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )""")


def _password_fingerprint(password_hash):
    """A short value that changes whenever the password does. Built into
    both login sessions and reset tokens, so changing the password signs
    out every other device and spends any outstanding reset link."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


class User(UserMixin):
    def __init__(self, user_id, email, password_hash, name=""):
        self.id = user_id
        self.email = email
        self.name = name or ""  # '' for accounts from before names were asked for
        self.fingerprint = _password_fingerprint(password_hash)

    def get_id(self):
        # Flask-Login stores this in the session/remember-me cookie; see
        # _load_user in init_accounts for the check on the way back in.
        return f"{self.id}:{self.fingerprint}"


def _user_from_row(row):
    return User(row["id"], row["email"], row["password_hash"], row["name"]) if row else None


def _get_user(user_id):
    with _connect() as conn:
        row = conn.execute("SELECT id, email, password_hash, name FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_from_row(row)


def _get_user_by_email(email):
    with _connect() as conn:
        row = conn.execute("SELECT id, email, password_hash, name FROM users WHERE email = ?", (email,)).fetchone()
    return _user_from_row(row)


def _create_user(email, password, name):
    """The new User, or None if the email is already registered."""
    password_hash = generate_password_hash(password)
    try:
        with _db_lock, _connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, created_at, name) VALUES (?, ?, ?, ?)",
                (email, password_hash, time.time(), name))
            return User(cur.lastrowid, email, password_hash, name)
    except sqlite3.IntegrityError:
        return None


def _set_password(user_id, password):
    """Stores a new password and returns the updated User."""
    password_hash = generate_password_hash(password)
    with _db_lock, _connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
    return _get_user(user_id)


# Checked against when an email isn't registered, so a failed login takes
# about as long either way and response timing doesn't reveal which
# emails have accounts.
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def _authenticate(email, password):
    with _connect() as conn:
        row = conn.execute("SELECT id, email, password_hash, name FROM users WHERE email = ?",
                           (email,)).fetchone()
    if row is None:
        check_password_hash(_DUMMY_HASH, password)
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return _user_from_row(row)


def _clean_watchlist(items):
    """items with anything malformed dropped, de-duplicated, and capped."""
    cleaned, seen = [], set()
    for item in items or []:
        if not isinstance(item, dict) or item.get("kind") not in _WATCHLIST_KINDS:
            continue
        key, label = item.get("key"), item.get("label")
        if not isinstance(key, (str, int)) or isinstance(key, bool) or not isinstance(label, str):
            continue
        if len(str(key)) > _MAX_FIELD_LEN:
            continue
        ident = (item["kind"], str(key))
        if ident in seen:
            continue
        seen.add(ident)
        cleaned.append({"kind": item["kind"], "key": key, "label": label[:_MAX_FIELD_LEN]})
        if len(cleaned) >= _MAX_WATCHLIST_ITEMS:
            break
    return cleaned


def load_watchlist(user_id):
    with _connect() as conn:
        row = conn.execute("SELECT items_json FROM watchlists WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return []
    try:
        return _clean_watchlist(json.loads(row["items_json"]))
    except json.JSONDecodeError:
        return []


def save_watchlist(user_id, items):
    """Saves (a cleaned copy of) items and returns what was saved."""
    items = _clean_watchlist(items)
    with _db_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO watchlists (user_id, items_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET items_json = excluded.items_json, updated_at = excluded.updated_at",
            (user_id, json.dumps(items), time.time()))
    return items


def _client_ip():
    # Production sits behind Cloudflare and then Render's proxy, so
    # request.remote_addr is a proxy's address shared by every visitor --
    # rate limiting on it would lock everyone out together. Either header
    # can be forged by a client that skips Cloudflare, but that only lets
    # them dodge the per-IP limit; the per-email limits still hold.
    forwarded = flask.request.headers.get("CF-Connecting-IP") or \
        flask.request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or flask.request.remote_addr or "unknown"


def _form_name():
    # Collapses whitespace runs (tabs, newlines) to single spaces and drops
    # other non-printing characters, so only a plain line of text can make
    # it into the "Welcome, ..." greeting.
    raw = "".join(ch for ch in (flask.request.form.get("name") or "") if ch.isprintable() or ch.isspace())
    return " ".join(raw.split())


def _form_email():
    # Lowercased: emails are stored and matched case-insensitively, and
    # this is also the per-email rate-limit key.
    return (flask.request.form.get("email") or "").strip().lower()


def _password_error(password, confirm):
    if len(password) < _MIN_PASSWORD_LEN:
        return f"Password must be at least {_MIN_PASSWORD_LEN} characters."
    if len(password) > _MAX_PASSWORD_LEN:
        return f"Password must be at most {_MAX_PASSWORD_LEN} characters."
    if password != confirm:
        return "Passwords don't match."
    return None


# --- Password reset tokens and email ----------------------------------------

def _reset_serializer():
    return URLSafeTimedSerializer(flask.current_app.config["SECRET_KEY"], salt=_RESET_TOKEN_SALT)


def _make_reset_token(user):
    return _reset_serializer().dumps({"u": user.id, "p": user.fingerprint})


def _user_for_reset_token(token):
    """The User a reset token belongs to, or None if it's forged, expired,
    or already spent (the password changed since it was issued)."""
    try:
        data = _reset_serializer().loads(token, max_age=_RESET_TOKEN_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    user = _get_user(data.get("u"))
    if user is None or not hmac.compare_digest(user.fingerprint, str(data.get("p"))):
        return None
    return user


def _public_base_url():
    # Never built from the request's Host header in production: a forged
    # Host would otherwise make the emailed link point at someone else's
    # site (and hand them the token).
    configured = os.environ.get("PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    if os.environ.get("RENDER"):
        return "https://stockpick.io"
    return flask.request.host_url.rstrip("/")


def _reset_email(link):
    minutes = _RESET_TOKEN_MAX_AGE // 60
    intro = ("We've received a request to reset your Stockpick password. If you made this request, "
             "you can reset your password using the link below.")
    text = (f"{intro}\n\n{link}\n\n"
            f"The link works once and expires in {minutes} minutes. If you didn't ask for this, you can "
            f"ignore this email -- your password hasn't changed.\n")
    html = f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:480px;
color:#1b1b19;font-size:15px;line-height:1.5">
<p style="font-weight:700;font-size:18px;margin:0 0 16px">Stockpick</p>
<p>{intro}</p>
<p><a href="{escape(link)}" style="display:inline-block;background:#1f8a5a;color:#ffffff;text-decoration:none;
font-weight:600;padding:10px 18px;border-radius:8px">Reset password</a></p>
<p style="color:#5f5e58;font-size:13px">This link works once and expires in {minutes} minutes. If you didn't
ask for this, you can ignore this email &mdash; your password hasn't changed.</p>
</div>"""
    return "Reset your Stockpick password", text, html


def _send_email(to, subject, text, html):
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        # Local runs: no email service configured, so show what would have
        # been sent (production warns at startup instead -- see init_accounts).
        print(f"\n--- Email to {to} (RESEND_API_KEY not set, not sent) ---\n{subject}\n\n{text}---\n", flush=True)
        return
    try:
        resp = requests.post(
            _RESEND_API_URL, timeout=15,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": _EMAIL_FROM, "to": [to], "subject": subject, "text": text, "html": html})
        if resp.status_code >= 300:
            print(f"ERROR: Resend rejected password reset email ({resp.status_code}): {resp.text[:300]}", flush=True)
    except requests.RequestException as e:
        print(f"ERROR: sending password reset email failed: {e}", flush=True)


def _send_reset_email_async(to, link):
    # In the background, so the forgot-password response takes the same time
    # whether or not the email has an account (and isn't held up by Resend).
    subject, text, html = _reset_email(link)
    threading.Thread(target=_send_email, args=(to, subject, text, html), daemon=True).start()


# --- Pages ---------------------------------------------------------------------

# Same look as the app (assets/custom.css's theme variables), and the
# same saved light/dark choice -- dcc.Store writes theme-store to
# localStorage as a JSON string. no-referrer: custom.css pulls in Google
# Fonts, and a reset page's URL must never travel in a Referer header.
_PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{title} · Stockpick</title>
<link rel="icon" href="/assets/favicon.ico">
<link rel="stylesheet" href="/assets/custom.css">
<script>
try {{ if (JSON.parse(localStorage.getItem("theme-store")) === "light")
    document.documentElement.setAttribute("data-theme", "light"); }} catch (e) {{}}
</script>
<style>
body {{ min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 16px;
       box-sizing: border-box; }}
.auth-card {{ width: 100%; max-width: 360px; background: var(--card-bg); border: 1px solid var(--border);
             border-radius: 14px; padding: 28px 24px; box-sizing: border-box; }}
.auth-logo {{ display: flex; align-items: center; gap: 10px; margin-bottom: 22px; text-decoration: none; }}
.auth-logo span:first-child {{ width: 22px; height: 22px; border-radius: 6px; background: var(--accent); }}
.auth-logo span:last-child {{ font-weight: 700; font-size: 17px; letter-spacing: -0.02em; color: var(--text); }}
h1 {{ font-size: 22px; margin: 0 0 18px; letter-spacing: -0.01em; }}
p.lead {{ font-size: 14px; color: var(--body-text); line-height: 1.5; margin: -6px 0 4px; }}
label {{ display: block; font-size: 13px; color: var(--body-text); margin: 14px 0 6px; }}
/* .field, not input[type=password]: a revealed password field is
   type=text, and must keep the same look. Scoped under .auth-card so it
   outranks custom.css's app-wide input[type="text"] styling. */
.auth-card .field {{ width: 100%; box-sizing: border-box; padding: 10px 12px;
    font-size: 14px; font-family: inherit; color: var(--text); background: var(--bg);
    border: 1px solid var(--border); border-radius: 8px; outline: none; box-shadow: none; }}
.auth-card .field:focus {{ border-color: var(--accent); box-shadow: none; }}
.hint {{ font-size: 12px; color: var(--body-text); margin-top: 6px; }}
.forgot {{ font-size: 12px; text-align: right; margin-top: 8px; }}
.pw-wrap {{ position: relative; }}
.auth-card .pw-wrap .field {{ padding-right: 40px; }}
.pw-toggle {{ position: absolute; top: 50%; right: 6px; transform: translateY(-50%); padding: 4px;
    display: flex; color: var(--body-text); background: none; border: none; border-radius: 6px;
    cursor: pointer; }}
.pw-toggle:hover, .pw-toggle:focus-visible {{ color: var(--text); }}
/* Open eye while masked (click to show), crossed-out eye while shown. */
.pw-toggle[aria-pressed="true"] .eye-open, .pw-toggle[aria-pressed="false"] .eye-closed {{ display: none; }}
/* Edge's own built-in reveal eye -- would sit next to ours. */
.auth-card .field::-ms-reveal {{ display: none; }}
button[type=submit] {{ width: 100%; margin-top: 22px; padding: 10px 12px; font-size: 14px; font-weight: 600;
         font-family: inherit; border: none; border-radius: 8px; background: var(--accent); color: #0f0f0e;
         cursor: pointer; }}
.error, .notice {{ font-size: 13px; background: var(--card-bg-2); border: 1px solid var(--border);
         border-radius: 8px; padding: 10px 12px; margin-bottom: 4px; line-height: 1.5; }}
.error {{ color: var(--down); }}
.notice {{ color: var(--text); }}
.alt {{ font-size: 13px; color: var(--body-text); margin-top: 18px; text-align: center; }}
</style>
</head>
<body>
<div class="auth-card">
<a class="auth-logo" href="/"><span></span><span>Stockpick</span></a>
<h1>{title}</h1>
{message}
{body}
<div class="alt">{alt}</div>
</div>
<script>
// Eye toggle on each password field. Fields are switched back to masked
// on submit, so the browser's "save password?" prompt still sees a
// password field.
document.querySelectorAll(".pw-toggle").forEach(function (btn) {{
    btn.addEventListener("click", function () {{
        var input = document.getElementById(btn.getAttribute("aria-controls"));
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.setAttribute("aria-pressed", show ? "true" : "false");
        btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
        input.focus();
    }});
}});
document.querySelectorAll("form").forEach(function (form) {{
    form.addEventListener("submit", function () {{
        form.querySelectorAll(".pw-wrap .field").forEach(function (i) {{ i.type = "password"; }});
    }});
}});
</script>
</body>
</html>"""

# Eye / eye-off outline icons, drawn in currentColor so they follow the
# toggle's hover color and the light/dark theme.
_EYE_SVG_ATTRS = ('width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"')
_EYE_OPEN_SVG = (f'<svg class="eye-open" {_EYE_SVG_ATTRS}>'
                 '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>'
                 '<circle cx="12" cy="12" r="3"/></svg>')
_EYE_CLOSED_SVG = (f'<svg class="eye-closed" {_EYE_SVG_ATTRS}>'
                   '<path d="M9.9 4.24A9.1 9.1 0 0 1 12 4c6.5 0 10 8 10 8a18.5 18.5 0 0 1-2.16 3.19"/>'
                   '<path d="M6.61 6.61A13.5 13.5 0 0 0 2 12s3.5 8 10 8a9.7 9.7 0 0 0 5.39-1.61"/>'
                   '<path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/>'
                   '<line x1="2" y1="2" x2="22" y2="22"/></svg>')


def _password_field(field_id, label, autocomplete, autofocus=False):
    """A label plus a masked password input with an eye toggle to reveal it
    (wired up by the script at the bottom of _PAGE_TEMPLATE)."""
    return (f'<label for="{field_id}">{label}</label>'
            f'<div class="pw-wrap"><input type="password" id="{field_id}" name="{field_id}" class="field" '
            f'autocomplete="{autocomplete}" required{" autofocus" if autofocus else ""}>'
            f'<button type="button" class="pw-toggle" aria-controls="{field_id}" aria-pressed="false" '
            f'aria-label="Show password" title="Show/hide password">{_EYE_OPEN_SVG}{_EYE_CLOSED_SVG}</button></div>')


def _name_field(name):
    return ('<label for="name">Name</label>'
            f'<input type="text" id="name" name="name" class="field" value="{escape(name)}" '
            f'autocomplete="name" maxlength="{_MAX_NAME_LEN}" required autofocus>'
            '<div class="hint">First name, full name, or a nickname.</div>')


def _email_field(email, autofocus=True):
    return ('<label for="email">Email</label>'
            f'<input type="email" id="email" name="email" class="field" value="{escape(email)}" '
            'autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="254" required'
            + (' autofocus>' if autofocus else '>'))


_NEW_PASSWORD_HINT = f'<div class="hint">At least {_MIN_PASSWORD_LEN} characters.</div>'


def _form(action, fields, submit_label):
    return (f'<form method="post" action="{action}">'
            f'<input type="hidden" name="csrf_token" value="{generate_csrf()}">'
            f'{fields}<button type="submit">{submit_label}</button></form>')


def _page(title, body, alt, error=None, notice=None, status=200):
    message = ""
    if error:
        message = f'<div class="error">{escape(error)}</div>'
    elif notice:
        message = f'<div class="notice">{escape(notice)}</div>'
    html = _PAGE_TEMPLATE.format(title=title, message=message, body=body, alt=alt)
    return flask.Response(html, status=status, mimetype="text/html")


_ALT_TO_LOGIN = 'Remembered it? <a href="/login">Log in</a>'


def _login_page(error=None, notice=None, email="", status=200):
    fields = (_email_field(email)
              + _password_field("password", "Password", "current-password")
              + '<div class="forgot"><a href="/forgot-password">Forgot password?</a></div>')
    return _page("Log in", _form("/login", fields, "Log in"),
                 'New here? <a href="/signup">Create an account</a>', error, notice, status)


def _signup_page(error=None, email="", name="", status=200):
    fields = (_name_field(name) + _email_field(email, autofocus=False)
              + _password_field("password", "Password", "new-password") + _NEW_PASSWORD_HINT
              + _password_field("confirm", "Confirm password", "new-password"))
    return _page("Create account", _form("/signup", fields, "Create account"),
                 'Already have an account? <a href="/login">Log in</a>', error, status=status)


def _forgot_page(error=None, notice=None, email="", status=200):
    if notice:
        # Sent -- no form to resubmit (a second request is only a click on
        # the link below away, and stays rate-limited).
        body = '<p class="lead" style="margin-top:12px">Check your inbox (and spam folder) for the link.</p>'
        alt = 'Didn\'t get it? <a href="/forgot-password">Try again</a> · <a href="/login">Log in</a>'
        return _page("Check your email", body, alt, notice=notice, status=status)
    body = ('<p class="lead">Enter your account\'s email and we\'ll send you a link to choose a new password.</p>'
            + _form("/forgot-password", _email_field(email), "Send reset link"))
    return _page("Reset password", body, _ALT_TO_LOGIN, error, status=status)


def _reset_page(user, error=None, status=200):
    fields = (_password_field("password", "New password", "new-password", autofocus=True) + _NEW_PASSWORD_HINT
              + _password_field("confirm", "Confirm new password", "new-password"))
    body = (f'<p class="lead">Choose a new password for <b>{escape(user.email)}</b>.</p>'
            + _form("/reset-password", fields, "Save new password"))
    return _page("Choose a new password", body, _ALT_TO_LOGIN, error, status=status)


def _invalid_reset_link_page():
    body = ('<p class="lead">This reset link has expired or was already used. Links work once, '
            f'for {_RESET_TOKEN_MAX_AGE // 60} minutes.</p>'
            '<p class="lead" style="margin-top:12px"><a href="/forgot-password">Send a new link</a></p>')
    return _page("Link expired", body, _ALT_TO_LOGIN, status=400)


def _start_session(user):
    # Rotates the session first so a pre-login session cookie can't carry
    # over (session fixation); the merge flag goes in after.
    flask.session.clear()
    login_user(user, remember=True)
    flask.session[MERGE_LOCAL_WATCHLIST_KEY] = True


_TOO_MANY = "Too many attempts. Please wait a few minutes and try again."


def _too_many_attempts(_limit):
    path = flask.request.path
    if path.startswith("/signup"):
        return _signup_page(_TOO_MANY, email=_form_email(), name=_form_name(), status=429)
    if path.startswith("/forgot-password"):
        return _forgot_page(_TOO_MANY, email=_form_email(), status=429)
    if path.startswith("/reset-password"):
        return _page("Choose a new password", "", _ALT_TO_LOGIN, _TOO_MANY, status=429)
    return _login_page(_TOO_MANY, email=_form_email(), status=429)


def _on_mounted_disk(path):
    """Whether path sits under a mount point other than the filesystem
    root -- i.e. on an attached disk, not the instance's own filesystem
    (which Render resets on every deploy)."""
    for parent in Path(path).resolve().parents:
        if parent == Path(parent.anchor):
            return False
        if os.path.ismount(parent):
            return True
    return False


def _check_production_config():
    """Refuses to start on Render without durable account storage and a
    fixed SECRET_KEY. Without USER_DB_PATH, _DB_PATH silently falls back to
    the project folder, and every account would be wiped by the next
    deploy -- far better to fail this deploy loudly instead."""
    problems = []
    if not os.environ.get("USER_DB_PATH"):
        problems.append("USER_DB_PATH is not set (should be a file on the persistent disk, "
                        "e.g. /var/data/users.db)")
    elif not _on_mounted_disk(_DB_PATH):
        problems.append(f"USER_DB_PATH={_DB_PATH} is not on a mounted persistent disk -- "
                        "check the disk's mount path in Render's service settings")
    if not os.environ.get("SECRET_KEY"):
        problems.append("SECRET_KEY is not set (without it every deploy logs everyone out)")
    if problems:
        raise RuntimeError("Account storage isn't safely configured, refusing to start:\n  - "
                           + "\n  - ".join(problems))
    if not os.environ.get("RESEND_API_KEY"):
        # Not fatal -- everything else works, but "Forgot password?" emails
        # silently go nowhere until it's set.
        print("WARNING: RESEND_API_KEY not set -- password reset emails will NOT be sent.", flush=True)


def init_accounts(server):
    """Wires sessions, login, rate limiting and the account routes into the
    Flask server behind Dash."""
    production = bool(os.environ.get("RENDER"))  # Render sets RENDER=true on every service
    if production:
        _check_production_config()
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        # Fine for local runs (production can't get here -- see
        # _check_production_config): logins just reset on each restart.
        print("WARNING: SECRET_KEY not set -- using a random key; logins won't survive a restart.", flush=True)
        secret = secrets.token_hex(32)
    server.config.update(
        SECRET_KEY=secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=production,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=production,
        REMEMBER_COOKIE_DURATION=timedelta(days=30),
        # CSRF tokens are checked by hand in the form views below, not
        # app-wide: CSRFProtect would also reject Dash's own JSON callback
        # POSTs, which carry no token (those are covered by SameSite=Lax
        # cookies plus Dash requiring a JSON content type).
        WTF_CSRF_TIME_LIMIT=None,
    )
    _init_db()

    login_manager = LoginManager()
    login_manager.init_app(server)

    @login_manager.user_loader
    def _load_user(session_id):
        # "id:fingerprint" (see User.get_id) -- a session from before the
        # password last changed no longer matches, and is logged out.
        user_id, _, fingerprint = str(session_id).partition(":")
        try:
            user = _get_user(int(user_id))
        except ValueError:
            return None
        if user is None or not hmac.compare_digest(user.fingerprint, fingerprint):
            return None
        return user

    limiter = Limiter(_client_ip, app=server, storage_uri="memory://", on_breach=_too_many_attempts)
    post_only = ["POST"]

    def _csrf_ok():
        try:
            validate_csrf(flask.request.form.get("csrf_token"))
        except ValidationError:
            return False
        return True

    _SESSION_EXPIRED = "Your session expired. Please try again."

    @server.route("/signup", methods=["GET", "POST"])
    @limiter.limit("5 per hour", methods=post_only)
    def signup():
        if current_user.is_authenticated:
            return flask.redirect("/")
        if flask.request.method == "GET":
            return _signup_page()
        email, name = _form_email(), _form_name()
        if not _csrf_ok():
            return _signup_page(_SESSION_EXPIRED, email=email, name=name, status=400)
        password = flask.request.form.get("password") or ""
        if not name:
            error = "Enter your name."
        elif len(name) > _MAX_NAME_LEN:
            error = f"Name must be at most {_MAX_NAME_LEN} characters."
        elif len(email) > _MAX_EMAIL_LEN or not _EMAIL_RE.match(email):
            error = "Enter a valid email address."
        else:
            error = _password_error(password, flask.request.form.get("confirm") or "")
        if error:
            return _signup_page(error, email=email, name=name, status=400)
        user = _create_user(email, password, name)
        if user is None:
            return _signup_page("An account with that email already exists. Log in instead?",
                                email=email, name=name, status=400)
        _start_session(user)
        return flask.redirect("/")

    @server.route("/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=post_only)
    @limiter.limit("20 per hour", methods=post_only, key_func=_form_email)
    def login():
        if current_user.is_authenticated:
            return flask.redirect("/")
        if flask.request.method == "GET":
            return _login_page()
        email = _form_email()
        if not _csrf_ok():
            return _login_page(_SESSION_EXPIRED, email=email, status=400)
        password = flask.request.form.get("password") or ""
        user = _authenticate(email, password[:_MAX_PASSWORD_LEN]) if email and password else None
        if user is None:
            return _login_page("Wrong email or password.", email=email, status=401)
        _start_session(user)
        return flask.redirect("/")

    @server.route("/forgot-password", methods=["GET", "POST"])
    @limiter.limit("5 per hour", methods=post_only)
    @limiter.limit("3 per hour", methods=post_only, key_func=_form_email)
    def forgot_password():
        if flask.request.method == "GET":
            return _forgot_page()
        email = _form_email()
        if not _csrf_ok():
            return _forgot_page(_SESSION_EXPIRED, email=email, status=400)
        if len(email) > _MAX_EMAIL_LEN or not _EMAIL_RE.match(email):
            return _forgot_page("Enter a valid email address.", email=email, status=400)
        user = _get_user_by_email(email)
        if user is not None:
            link = f"{_public_base_url()}/reset-password?token={_make_reset_token(user)}"
            _send_reset_email_async(user.email, link)
        # Same answer either way, so this form can't be used to find out
        # which emails have accounts.
        return _forgot_page(notice=f"If an account exists for {email}, we've sent it a link to reset "
                                   f"the password. It expires in {_RESET_TOKEN_MAX_AGE // 60} minutes.")

    @server.route("/reset-password", methods=["GET", "POST"])
    @limiter.limit("10 per hour", methods=post_only)
    def reset_password():
        if flask.request.method == "GET" and flask.request.args.get("token"):
            # Move the token out of the address bar right away, so it isn't
            # left in browser history or shown on screen; the form below
            # reads it back from the session.
            flask.session[_RESET_TOKEN_SESSION_KEY] = flask.request.args["token"]
            return flask.redirect("/reset-password")
        user = _user_for_reset_token(flask.session.get(_RESET_TOKEN_SESSION_KEY) or "")
        if user is None:
            flask.session.pop(_RESET_TOKEN_SESSION_KEY, None)
            return _invalid_reset_link_page()
        if flask.request.method == "GET":
            return _reset_page(user)
        if not _csrf_ok():
            return _reset_page(user, _SESSION_EXPIRED, status=400)
        password = flask.request.form.get("password") or ""
        error = _password_error(password, flask.request.form.get("confirm") or "")
        if error:
            return _reset_page(user, error, status=400)
        # Changing the password changes the fingerprint: this token is now
        # spent, and every other signed-in device is logged out.
        _start_session(_set_password(user.id, password))
        return flask.redirect("/")

    # POST so a third-party page can't log someone out with a plain link or
    # image; the sidebar's Log out is a small form (see dash_app's
    # _render_account_area) carrying a CSRF token.
    @server.route("/logout", methods=["POST"])
    def logout():
        if not _csrf_ok():
            return flask.redirect("/")
        # Clear first: logout_user() leaves a flag in the session telling
        # Flask-Login to delete the 30-day remember-me cookie -- clearing
        # after it would wipe that flag, and the surviving cookie would
        # log the user straight back in on the next request.
        flask.session.clear()
        logout_user()
        # The watchlist store in localStorage still holds this account's
        # list -- clear it so the next person on this browser doesn't see
        # it (dcc.Store keeps a "-timestamp" key alongside the data).
        return flask.Response(
            """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Logging out</title><script>
            try { localStorage.removeItem("watchlist-store");
                  localStorage.removeItem("watchlist-store-timestamp"); } catch (e) {}
            location.replace("/");
            </script></head><body></body></html>""",
            mimetype="text/html")
