from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    session,
    g,
    has_request_context,
    abort,
    make_response,
    Response,
    send_from_directory,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
import collections
import db
import core
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import zoneinfo
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime
from logger import get_logger
import push
import availability
from web_ui_plugin import i18n, passwords

# Get logger for this module
logger = get_logger(__name__)


# ------------------------------------------------------------------ accounts
# Until the first account is created the interface behaves as it always did: one implicit
# owner (id 0), optionally protected by a single password. Creating an account (Configuration >
# Security) switches on multi-user mode: everyone signs in, and queries, items, notifications
# and personal settings belong to the signed-in account.
LEGACY_USER = {"id": 0, "username": "", "role": "admin", "disabled": 0, "legacy": True}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MAX_LOGIN_FAILURES = 5
LOGIN_LOCK_SECONDS = 60
MIN_PASSWORD_LENGTH = passwords.MIN_LENGTH
MAX_SIGNUPS_PER_HOUR = 5
_login_failures = {}  # ip -> (failures, locked_until)
_signups = collections.defaultdict(list)  # ip -> signup timestamps
# A hash to compare against when the username doesn't exist, so timing doesn't reveal it
_DUMMY_HASH = generate_password_hash(secrets.token_hex(8))
# Reachable without signing in: the login/sign-up pages, static files and what a PWA needs
PUBLIC_ENDPOINTS = (
    "static",
    "login",
    "register",
    "set_language_cookie",
    "manifest",
    "service_worker",
    "offline",
    "health",
)


def accounts_enabled():
    if "accounts" not in g:
        g.accounts = db.count_users() > 0
    return g.accounts


def _env_password():
    """VN_WEB_PASSWORD: the single password while accounts are off. It has no effect once they
    are on: a forgotten administrator password is reset with reset_admin_password.py."""
    return os.environ.get("VN_WEB_PASSWORD", "")


def _legacy_hash():
    return db.get_parameter("web_password_hash") or ""


def legacy_auth_enabled():
    return not accounts_enabled() and bool(_env_password() or _legacy_hash())


def _legacy_token():
    """Changes whenever the single password does, which logs out every other session."""
    secret = _env_password() or _legacy_hash()
    return hashlib.sha256(secret.encode()).hexdigest()[:24]


def _check_legacy_password(candidate):
    env = _env_password()
    if env:
        return hmac.compare_digest(env.encode(), candidate.encode())
    stored = _legacy_hash()
    return bool(stored) and check_password_hash(stored, candidate)


def _pw_version(user):
    """Changes with the account's password: older sessions stop being valid."""
    return hashlib.sha256(user["password_hash"].encode()).hexdigest()[:16]


def _verify_user(username, password):
    user = db.get_user_by_name(username.strip())
    hash_to_check = user["password_hash"] if user else _DUMMY_HASH
    ok = check_password_hash(hash_to_check, password) and user is not None
    if user and ok and not user["disabled"]:
        return user
    return None


def current_user():
    """The signed-in account, the implicit owner before accounts exist, or None."""
    if "user" in g:
        return g.user
    user = None
    if not accounts_enabled():
        user = LEGACY_USER
    else:
        user_id = session.get("uid")
        if user_id:
            row = db.get_user(user_id)
            if row and not row["disabled"] and session.get("pwv") == _pw_version(row):
                user = row
    g.user = user
    return user


def current_uid():
    user = current_user()
    return user["id"] if user else None


def is_admin():
    user = current_user()
    return bool(user and user["role"] == "admin")


def admin_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin():
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def admin_only_hidden(view):
    """Administrators only; everyone else gets a plain 404, as if the page did not exist."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin():
            abort(404)
        return view(*args, **kwargs)

    return wrapper


def _sign_in(user):
    session.clear()
    session["uid"] = user["id"]
    session["pwv"] = _pw_version(user)
    session.permanent = True


def _safe_next(target):
    """Only follow same-site relative redirects after login."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("index")


def _client_ip():
    return request.remote_addr or "?"


def _locked_message(ip):
    failures, locked_until = _login_failures.get(ip, (0, 0))
    if locked_until > time.time():
        return tr("Too many attempts. Try again in %(n)s seconds.", n=int(locked_until - time.time()) + 1)
    return None


def _register_failure(ip):
    failures, _ = _login_failures.get(ip, (0, 0))
    failures += 1
    locked = failures >= MAX_LOGIN_FAILURES
    _login_failures[ip] = (0 if locked else failures, time.time() + LOGIN_LOCK_SECONDS if locked else 0)


# ------------------------------------------------------------------ translations
def current_language():
    """Language for this request: the account's setting, else the cookie / browser, else English."""
    if not has_request_context():
        return i18n.DEFAULT_LANGUAGE
    if "lang" not in g:
        user_id = current_uid()
        chosen = db.get_user_setting(user_id, "language") if user_id is not None else None
        if not chosen:
            chosen = request.cookies.get("vn_lang")
        g.lang = i18n.resolve_language(chosen, request.accept_languages)
    return g.lang


def tr(msgid, **kwargs):
    return i18n.translate(current_language(), msgid, **kwargs)


def tr_n(singular, plural, n):
    return tr(singular if n == 1 else plural, n=n)


# Create Flask app
app = Flask(
    __name__,
    template_folder=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "templates"
    ),
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
)

# The secret key is stored so that logins survive restarts
_secret = db.get_parameter("web_secret_key")
if not _secret:
    _secret = secrets.token_hex(32)
    db.set_parameter("web_secret_key", _secret)
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_NAME="vn_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set VN_COOKIE_SECURE=1 when the interface is served over HTTPS
    SESSION_COOKIE_SECURE=os.environ.get("VN_COOKIE_SECURE", "") not in ("", "0", "false"),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    # Nothing in this interface uploads files: refuse oversized bodies before they are read
    MAX_CONTENT_LENGTH=256 * 1024,
)
TRUST_PROXY = os.environ.get("VN_TRUST_PROXY", "") not in ("", "0", "false")
if TRUST_PROXY:
    # Behind a reverse proxy: trust its X-Forwarded-* headers (client IP, scheme, host)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.jinja_env.globals.update(_=tr, _n=tr_n)


# ------------------------------------------------------------------ request limits
class RateLimiter:
    """Sliding-window limiter kept in memory: enough to blunt floods and password guessing
    against a single instance (it is not shared between processes or restarts)."""

    def __init__(self):
        self._hits = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()
        self._swept = time.time()

    def allow(self, key, limit, window):
        now = time.time()
        with self._lock:
            if now - self._swept > 300:  # forget idle clients so the table cannot grow forever
                for k in [k for k, q in self._hits.items() if not q or q[-1] < now - 3600]:
                    del self._hits[k]
                self._swept = now
            hits = self._hits[key]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


limiter = RateLimiter()
LIMIT_PAGES = (240, 60)  # requests per minute and client for pages and APIs
LIMIT_STATIC = (900, 60)  # static files: a page loads a dozen of them
LIMIT_AUTH = (10, 60)  # sign-in and sign-up attempts per minute and client
LIMIT_SENSITIVE = (30, 60)  # push test, password changes, account management


@app.before_request
def throttle_and_check_origin():
    """Per-client rate limits and a same-origin check on everything that changes state."""
    if request.endpoint == "health":
        return None
    ip = _client_ip()
    if request.endpoint == "static":
        allowed = limiter.allow(("static", ip), *LIMIT_STATIC)
    else:
        allowed = limiter.allow(("page", ip), *LIMIT_PAGES)
        if allowed and request.method == "POST":
            if request.endpoint in ("login", "register"):
                allowed = limiter.allow(("auth", ip), *LIMIT_AUTH)
            elif request.endpoint in ("api_push_test", "change_own_password", "set_password", "create_user", "update_user", "enable_accounts"):
                allowed = limiter.allow(("sensitive", ip), *LIMIT_SENSITIVE)
    if not allowed:
        response = jsonify({"status": "error", "message": tr("Too many requests. Try again in a minute.")})
        response.status_code = 429
        response.headers["Retry-After"] = "60"
        return response

    # CSRF: browsers always send Origin (or at least Sec-Fetch-Site) on cross-site writes
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin:
            if origin != "null" and urlparse(origin).netloc != request.host:
                abort(403)
            if origin == "null":
                abort(403)
        elif request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403)
    return None


CSP = "; ".join(
    (
        "default-src 'self'",
        # Pages carry inline handlers and small scripts; Bootstrap comes from jsDelivr
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "font-src 'self' https://cdn.jsdelivr.net data:",
        "img-src 'self' data: https:",  # listing photos come from Vinted's CDN
        "connect-src 'self'",
        "worker-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)


@app.after_request
def security_headers(response):
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    # Pages stay same-origin; the public static files (icons, styles) must be fetchable by the OS when it
    # draws a notification, which does not happen from inside the page
    headers.setdefault("Cross-Origin-Resource-Policy", "cross-origin" if request.endpoint == "static" else "same-origin")
    if response.mimetype in ("text/html", "application/manifest+json"):
        headers.setdefault("Content-Security-Policy", CSP)
    if request.is_secure:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.endpoint != "static" and response.mimetype == "text/html":
        # Pages show per-account data: never let a shared cache keep them
        headers["Cache-Control"] = "private, no-store"
    return response


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if accounts_enabled():
        if current_user() is not None:
            return None
    elif not legacy_auth_enabled() or session.get("auth") == _legacy_token():
        return None
    if request.headers.get("X-Requested-With") == "fetch" or request.path.startswith("/control"):
        return jsonify({"status": "error", "message": tr("Login required")}), 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


# Endpoints an account can still reach while it has to choose a new password
FORCED_CHANGE_ALLOWED = ("static", "logout", "force_password", "set_language_cookie", "manifest", "service_worker", "offline", "health", "login")


def must_change_password(user):
    return bool(user and user["id"] and db.get_user_setting(user["id"], "must_change_password") == "1")


@app.before_request
def force_password_change():
    """A password set by someone else (an administrator, or the recovery script) is single-use."""
    if request.endpoint in FORCED_CHANGE_ALLOWED or not accounts_enabled():
        return None
    if must_change_password(current_user()):
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"status": "error", "message": tr("Choose a new password first")}), 403
        return redirect(url_for("force_password"))
    return None


@app.route("/account/new-password", methods=["GET", "POST"])
def force_password():
    user = current_user()
    if not accounts_enabled() or user is None or not must_change_password(user):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if check_password_hash(user["password_hash"], new):
            error = tr("Choose a different password from the temporary one.")
        else:
            error = _password_problem(new, request.form.get("confirm_password", ""), user["username"])
        if not error:
            db.update_user(user["id"], password_hash=generate_password_hash(new))
            db.set_user_setting(user["id"], "must_change_password", "0")
            _sign_in(db.get_user(user["id"]))
            flash(tr("Password saved. Other browsers have been logged out."), "success")
            return redirect(url_for("index"))
    return render_template("force_password.html", error=error, min_password_length=MIN_PASSWORD_LENGTH), (400 if error else 200)


@app.route("/login", methods=["GET", "POST"])
def login():
    multi = accounts_enabled()
    if multi and current_user() is not None:
        return redirect(url_for("index"))
    if not multi and not legacy_auth_enabled():
        return redirect(url_for("index"))
    ip = _client_ip()
    error = _locked_message(ip)
    if request.method == "POST" and not error:
        if multi:
            user = _verify_user(request.form.get("username", ""), request.form.get("password", ""))
            if user:
                _login_failures.pop(ip, None)
                _sign_in(user)
                return redirect(_safe_next(request.form.get("next")))
            _register_failure(ip)
            error = tr("Wrong username or password.")
        elif _check_legacy_password(request.form.get("password", "")):
            _login_failures.pop(ip, None)
            session.clear()
            session["auth"] = _legacy_token()
            session.permanent = True
            return redirect(_safe_next(request.form.get("next")))
        else:
            _register_failure(ip)
            error = tr("Wrong password.")
        error = _locked_message(ip) or error
    locked = _locked_message(ip) is not None
    return render_template(
        "login.html",
        error=error,
        next=request.values.get("next", ""),
        username=request.form.get("username", "") if multi else "",
        signup_open=multi and db.get_parameter("allow_signup") == "True",
    ), (429 if locked and request.method == "POST" else 200)


@app.route("/register", methods=["GET", "POST"])
def register():
    """Self-service sign-up, only when an administrator turned it on."""
    if not accounts_enabled() or db.get_parameter("allow_signup") != "True":
        abort(404)
    if current_user() is not None:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        ip = _client_ip()
        recent = [t for t in _signups[ip] if t > time.time() - 3600]
        _signups[ip] = recent
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(recent) >= MAX_SIGNUPS_PER_HOUR:
            error = tr("Too many sign-ups from this address. Try again later.")
        elif not USERNAME_RE.match(username):
            error = tr("Usernames are 3-32 characters: letters, numbers, dots, dashes and underscores.")
        elif password != request.form.get("confirm_password", ""):
            error = tr("The two passwords do not match.")
        elif _password_error(password, username):
            error = _password_error(password, username)
        else:
            user_id = db.create_user(username, generate_password_hash(password), "user")
            if user_id is None:
                error = tr("That username is already taken.")
            else:
                _signups[ip].append(time.time())
                _sign_in(db.get_user(user_id))
                flash(tr("Welcome! Your account is ready."), "success")
                return redirect(url_for("index"))
    return render_template("register.html", error=error, username=request.form.get("username", ""), min_password_length=MIN_PASSWORD_LENGTH), (400 if error else 200)


@app.route("/lang/<code>")
def set_language_cookie(code):
    """Language choice for visitors who are not signed in (login and sign-up pages)."""
    response = make_response(redirect(_safe_next(request.args.get("next"))))
    if code in dict(i18n.available_languages()):
        response.set_cookie("vn_lang", code, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return response


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def _password_error(password, username=""):
    """A translated reason why the password is not acceptable, or None."""
    code = passwords.check(password, username)
    if code is None:
        return None
    messages = {
        passwords.TOO_SHORT: lambda: tr("Use at least %(n)s characters. A few random words make a strong password.", n=MIN_PASSWORD_LENGTH),
        passwords.TOO_LONG: lambda: tr("Use at most %(n)s characters.", n=passwords.MAX_LENGTH),
        passwords.COMMON: lambda: tr("That password is too common. Pick something only you would think of."),
        passwords.CONTAINS_USERNAME: lambda: tr("The password must not contain the username."),
        passwords.CONTAINS_APP_NAME: lambda: tr("The password must not contain the name of this application."),
        passwords.PATTERN: lambda: tr("That password is a simple pattern (a keyboard row, a run or a repetition)."),
        passwords.BREACHED: lambda: tr("That password appears in known data breaches. Choose a different one."),
    }
    return messages[code]()


def _password_problem(new, confirm, username=""):
    if new != confirm:
        return tr("The two passwords do not match.")
    return _password_error(new, username)


# ---- single password (before accounts exist)
@app.route("/security/password", methods=["POST"])
def set_password():
    if accounts_enabled():
        abort(404)
    if _env_password():
        flash(tr("The password is managed by the VN_WEB_PASSWORD environment variable."), "warning")
        return redirect(url_for("config", _anchor="security"))
    if _legacy_hash() and not _check_legacy_password(request.form.get("current_password", "")):
        flash(tr("Current password is wrong."), "danger")
        return redirect(url_for("config", _anchor="security"))
    problem = _password_problem(request.form.get("new_password", ""), request.form.get("confirm_password", ""))
    if problem:
        flash(problem, "danger")
    else:
        db.set_parameter("web_password_hash", generate_password_hash(request.form["new_password"]))
        session["auth"] = _legacy_token()  # keep this browser logged in
        session.permanent = True
        flash(tr("Password saved. Other browsers have been logged out."), "success")
    return redirect(url_for("config", _anchor="security"))


@app.route("/security/password/remove", methods=["POST"])
def remove_password():
    if accounts_enabled():
        abort(404)
    if _env_password():
        flash(tr("The password is managed by the VN_WEB_PASSWORD environment variable."), "warning")
    elif not _check_legacy_password(request.form.get("current_password", "")):
        flash(tr("Current password is wrong."), "danger")
    else:
        db.set_parameter("web_password_hash", "")
        flash(tr("Password removed. The web interface is open to anyone who can reach it."), "warning")
    return redirect(url_for("config", _anchor="security"))


@app.route("/accounts/enable", methods=["POST"])
def enable_accounts():
    """Turn on multi-user mode by creating the first administrator."""
    if accounts_enabled():
        abort(404)
    if legacy_auth_enabled() and not _check_legacy_password(request.form.get("current_password", "")):
        flash(tr("Current password is wrong."), "danger")
        return redirect(url_for("config", _anchor="security"))
    username = request.form.get("username", "").strip()
    password = request.form.get("new_password", "")
    problem = None
    if not USERNAME_RE.match(username):
        problem = tr("Usernames are 3-32 characters: letters, numbers, dots, dashes and underscores.")
    else:
        problem = _password_problem(password, request.form.get("confirm_password", ""), username)
    if problem:
        flash(problem, "danger")
        return redirect(url_for("config", _anchor="security"))
    user_id = db.create_user(username, generate_password_hash(password), "admin")
    if user_id is None:
        flash(tr("That username is already taken."), "danger")
        return redirect(url_for("config", _anchor="security"))
    # Everything created so far becomes this administrator's
    db.adopt_unowned_data(user_id)
    db.set_parameter("web_password_hash", "")
    g.pop("accounts", None)
    _sign_in(db.get_user(user_id))
    flash(tr("Accounts are on. You are signed in as %(name)s.", name=username), "success")
    return redirect(url_for("config", _anchor="security"))


# ---- account password (accounts mode)
@app.route("/account/password", methods=["POST"])
def change_own_password():
    user = current_user()
    if not accounts_enabled() or user is None:
        abort(404)
    if not check_password_hash(user["password_hash"], request.form.get("current_password", "")):
        flash(tr("Current password is wrong."), "danger")
        return redirect(url_for("config", _anchor="security"))
    problem = _password_problem(request.form.get("new_password", ""), request.form.get("confirm_password", ""), user["username"])
    if problem:
        flash(problem, "danger")
    else:
        db.update_user(user["id"], password_hash=generate_password_hash(request.form["new_password"]))
        db.set_user_setting(user["id"], "must_change_password", "0")
        _sign_in(db.get_user(user["id"]))
        flash(tr("Password saved. Other browsers have been logged out."), "success")
    return redirect(url_for("config", _anchor="security"))


@app.route("/account/feed-token", methods=["POST"])
def regenerate_feed_token():
    db.set_user_setting(current_uid(), "feed_token", secrets.token_urlsafe(24))
    flash(tr("A new feed address was created. The old one no longer works."), "success")
    return redirect(url_for("config", _anchor="notifications"))


# ---- user management (administrators)
def _last_admin(user_id):
    """Would removing/demoting/disabling this account leave no active administrator?"""
    target = db.get_user(user_id)
    return bool(target and target["role"] == "admin" and not target["disabled"] and db.count_admins(exclude_id=user_id) == 0)


@app.route("/users")
@admin_required
def users():
    if not accounts_enabled():
        return redirect(url_for("config", _anchor="security"))
    rows = db.list_users()
    for row in rows:
        row["created"] = format_ts(row["created_at"])
        row["hue"] = sum(map(ord, row["username"])) * 7 % 360  # stable avatar colour
        row["queries"] = [
            {"name": _query_display(q[1], q[2]), "item_count": q[7], "last_ts": q[3], "push": bool(q[9])}
            for q in db.get_queries_overview(row["id"])
        ]
        row["recent_items"] = [
            {**item, "when": format_ts(item["timestamp"]) if item["timestamp"] else ""}
            for item in db.get_recent_items(row["id"], 5)
        ]
    return render_template("users.html", users=rows, allow_signup=db.get_parameter("allow_signup") == "True",
                           min_password_length=MIN_PASSWORD_LENGTH)


@app.route("/users/create", methods=["POST"])
@admin_required
def create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = "admin" if request.form.get("role") == "admin" else "user"
    if not USERNAME_RE.match(username):
        flash(tr("Usernames are 3-32 characters: letters, numbers, dots, dashes and underscores."), "danger")
    elif _password_error(password, username):
        flash(_password_error(password, username), "danger")
    elif db.create_user(username, generate_password_hash(password), role) is None:
        flash(tr("That username is already taken."), "danger")
    else:
        flash(tr("Account %(name)s created.", name=username), "success")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/update", methods=["POST"])
@admin_required
def update_user(user_id):
    target = db.get_user(user_id)
    if target is None:
        abort(404)
    action = request.form.get("action")
    if action == "role":
        new_role = "admin" if request.form.get("role") == "admin" else "user"
        if new_role != "admin" and _last_admin(user_id):
            flash(tr("There must be at least one active administrator."), "danger")
        else:
            db.update_user(user_id, role=new_role)
            flash(tr("Account updated."), "success")
    elif action == "disable":
        if user_id == current_uid() or _last_admin(user_id):
            flash(tr("You cannot disable this account."), "danger")
        else:
            db.update_user(user_id, disabled=0 if target["disabled"] else 1)
            flash(tr("Account updated."), "success")
    elif action == "password":
        password = request.form.get("password", "")
        problem = _password_error(password, target["username"])
        if problem:
            flash(problem, "danger")
        else:
            db.update_user(user_id, password_hash=generate_password_hash(password))
            if user_id == current_uid():
                _sign_in(db.get_user(user_id))
                flash(tr("Password reset. That account was signed out everywhere."), "success")
            else:
                # Known to the administrator, so the account must choose its own at the next sign-in
                db.set_user_setting(user_id, "must_change_password", "1")
                flash(tr("Password reset. That account was signed out everywhere and must choose a new password at its next sign-in."), "success")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    if user_id == current_uid() or _last_admin(user_id):
        flash(tr("You cannot delete this account."), "danger")
    elif db.get_user(user_id) is None:
        abort(404)
    else:
        db.delete_user(user_id)
        flash(tr("Account deleted together with its queries and items."), "success")
    return redirect(url_for("users"))


_version_cache = {"at": 0, "value": None}


def _cached_check_version(ttl=3600):
    """check_version() hits GitHub, so don't do it on every page/fragment request."""
    if _version_cache["value"] is None or time.time() - _version_cache["at"] > ttl:
        _version_cache["value"] = core.check_version()
        _version_cache["at"] = time.time()
    return _version_cache["value"]


@app.context_processor
def inject_version_info():
    is_up_to_date, current_ver, latest_version, github_url = _cached_check_version()
    return {
        "github_url": github_url,
        # Documentation, issues and the GitHub row point at this project's repository (override with VN_REPO_URL)
        "repo_url": (os.environ.get("VN_REPO_URL") or "https://github.com/angeloruggieridj/Vinted-Notifications").rstrip("/"),
        "current_version": current_ver,
        "latest_version": latest_version,
        "is_up_to_date": is_up_to_date,
    }


@app.context_processor
def inject_current_year():
    user = current_user()
    return {
        "current_year": datetime.now().year,
        "display_tz": (db.get_user_setting(user["id"], "timezone") or "") if user else "",
        "auth_enabled": accounts_enabled() or legacy_auth_enabled(),
        "accounts_enabled": accounts_enabled(),
        "current_user": user,
        "is_admin": is_admin(),
        "lang": current_language(),
        "languages": i18n.available_languages(),
        "js_i18n": i18n.js_catalog(current_language()),
    }


def _display_tz():
    """The signed-in account's timezone, or None to use the server's local time."""
    user_id = current_uid()
    name = db.get_user_setting(user_id, "timezone") if user_id is not None else None
    if not name:
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        return None


def format_ts(epoch):
    """Format an epoch timestamp in the account's timezone."""
    return datetime.fromtimestamp(epoch, _display_tz()).strftime("%Y-%m-%d %H:%M:%S")


def _clean_int(field, default, minimum=1):
    """Read a positive integer setting from the form, falling back to `default`."""
    try:
        value = int(request.form.get(field, ""))
        if value >= minimum:
            return str(value)
    except ValueError:
        pass
    flash(tr("Invalid value for %(field)s, using %(default)s", field=field, default=default), "warning")
    return str(default)


def _setting_int(name, default):
    try:
        return int(db.get_parameter(name) or default)
    except ValueError:
        return default


def parse_max_price(value):
    """Parse the optional maximum-price form value; None if empty/invalid/<=0."""
    try:
        return max(0.0, float(str(value).replace(",", "."))) or None
    except (TypeError, ValueError):
        return None


def parse_min_reviews(value):
    """Parse the optional minimum-reviews form value; None if empty/invalid/<=0."""
    try:
        return max(0, int(value)) or None
    except (TypeError, ValueError):
        return None


def parse_min_stars(value):
    """Parse the optional minimum-stars form value; clamp to 0-5, None if empty/invalid."""
    try:
        stars = float(value)
    except (TypeError, ValueError):
        return None
    return min(5.0, max(0.0, stars)) or None


def _query_display(url, name):
    return name or parse_qs(urlparse(url).query).get("search_text", [None])[0] or url


def _format_item(item):
    """Template-ready dict from a db.search_items row."""
    search_text = parse_qs(urlparse(item[5]).query).get("search_text", [None])[0]
    avg, history = item[11], item[12]
    deal_pct = None
    try:
        if avg and history >= db.DEAL_MIN_HISTORY and float(item[2]) <= avg * db.DEAL_RATIO:
            deal_pct = round((1 - float(item[2]) / avg) * 100)
    except (TypeError, ValueError):
        pass
    return {
        "id": item[0],
        "title": item[1],
        "price": item[2],
        "currency": item[3],
        # Epoch is formatted in the browser; the string is the no-JavaScript fallback.
        "ts": item[4],
        "timestamp": format_ts(item[4]),
        "query": item[7] or search_text or item[5],
        "url": f"{urlparse(item[5]).scheme}://{urlparse(item[5]).netloc}/items/{item[0]}",
        "photo_url": item[6],
        "seller_login": item[8],
        "seller_stars": item[9],
        "seller_reviews": item[10],
        "avg_price": round(avg, 2) if avg else None,
        "deal_pct": deal_pct,
    }


def _queries_overview():
    """The account's queries with filters, collected-item count, average price and last-found time."""
    overview = []
    for i, q in enumerate(db.get_queries_overview(current_uid()), start=1):
        overview.append(
            {
                "id": i,
                "query_id": q[0],
                "query": q[1],
                "display": _query_display(q[1], q[2]),
                "name": q[2] or "",
                "last_ts": q[3],
                "last_found_item": format_ts(q[3]) if q[3] else "Never",
                "min_stars": q[4] or "",
                "min_reviews": q[5] or "",
                "max_price": ("%g" % q[6]) if q[6] else "",
                "item_count": q[7],
                "avg_price": round(q[8], 2) if q[8] else None,
                "push": bool(q[9]),
                "host": (urlparse(q[1]).netloc or "").removeprefix("www."),
                "terms": (parse_qs(urlparse(q[1]).query).get("search_text") or [""])[0],
            }
        )
    return overview


def _notification_state():
    """Is the account's Telegram bot / RSS feed switched on (and the bot fully configured)?"""
    user_id = current_uid()
    telegram = (
        db.get_user_setting(user_id, "telegram_enabled") == "True"
        and bool(db.get_user_setting(user_id, "telegram_token"))
        and bool(db.get_user_setting(user_id, "telegram_chat_id"))
    )
    rss = db.get_user_setting(user_id, "rss_enabled") == "True"
    return telegram, rss


def _feed_url():
    """The account's private RSS address."""
    base = (db.get_parameter("rss_public_url") or "").strip().rstrip("/")
    if not base:
        host = urlparse("//" + request.host).hostname or "localhost"
        base = f"{request.scheme}://{host}:{_setting_int('rss_port', 8080)}"
    return f"{base}/feed/{db.get_feed_token(current_uid())}"


@app.route("/")
def index():
    uid = current_uid()
    latest, _ = db.search_items(uid, limit=8)
    deals_today = db.search_items(
        uid, limit=1, since=int(time.time()) - 86400, deals_only=True
    )[1]
    stats = {
        "total_items": db.get_total_items_count(uid),
        "total_queries": db.get_total_queries_count(uid),
        "items_per_day": db.get_items_per_day(uid),
        "deals_today": deals_today,
    }
    telegram_running, rss_running = _notification_state()
    return render_template(
        "index.html",
        queries=_queries_overview(),
        items=[_format_item(row) for row in latest],
        telegram_running=telegram_running,
        rss_running=rss_running,
        feed_url=_feed_url(),
        stats=stats,
    )


@app.route("/queries")
def queries():
    return render_template("queries.html", queries=_queries_overview())


@app.route("/add_query", methods=["POST"])
def add_query():
    query = request.form.get("query")
    query_name = request.form.get("query_name", "").strip()
    if query:
        message, is_new_query = core.process_query(
            query,
            name=query_name if query_name != "" else None,
            min_stars=parse_min_stars(request.form.get("min_stars")),
            min_reviews=parse_min_reviews(request.form.get("min_reviews")),
            max_price=parse_max_price(request.form.get("max_price")),
            user_id=current_uid(),
            push="push" in request.form,
        )
        if is_new_query:
            flash(tr("Query added: %(query)s. Its first scan is starting now.", query=query), "success")
        else:
            flash(tr(message), "warning")
    else:
        flash(tr("No query provided"), "error")

    return redirect(url_for("queries"))


@app.route("/remove_query/<int:query_id>", methods=["POST"])
def remove_query(query_id):
    message, success = core.process_remove_query(str(query_id), current_uid())
    if success:
        flash(tr("Query removed"), "success")
    else:
        flash(tr(message), "error")

    return redirect(url_for("queries"))


@app.route("/remove_query/all", methods=["POST"])
def remove_all_queries():
    message, success = core.process_remove_query("all", current_uid())
    if success:
        flash(tr("All queries removed"), "success")
    else:
        flash(tr(message), "error")

    return redirect(url_for("queries"))


@app.route("/update_query/<int:query_id>", methods=["POST"])
def update_query(query_id):
    query = request.form.get("query")
    query_name = request.form.get("query_name", "").strip()

    if query:
        message, success = core.process_update_query(
            query_id,
            query,
            name=query_name if query_name != "" else None,
            min_stars=parse_min_stars(request.form.get("min_stars")),
            min_reviews=parse_min_reviews(request.form.get("min_reviews")),
            max_price=parse_max_price(request.form.get("max_price")),
            user_id=current_uid(),
            push="push" in request.form,
        )
        if success:
            flash(tr("Query updated"), "success")
        else:
            flash(tr(message), "error")
    else:
        flash(tr("No query provided"), "error")

    return redirect(url_for("queries"))


def _float_arg(name):
    try:
        return float(request.args.get(name, ""))
    except ValueError:
        return None


def _int_arg(name, default=None):
    try:
        return int(request.args.get(name, ""))
    except ValueError:
        return default


PERIODS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
PAGE_SIZES = (12, 24, 48, 96, 192)
DEFAULT_PAGE_SIZE = 24


def _page_window(page, pages):
    """Page numbers to show in the pager, with None marking a gap."""
    wanted = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    window = []
    for number in wanted:
        if window and number - window[-1] > 1:
            window.append(None)
        window.append(number)
    return window


def _items_context():
    """Read the Items filters from the query string and build the template context."""
    uid = current_uid()
    per_page = _int_arg("per_page", _int_arg("limit", DEFAULT_PAGE_SIZE))
    if per_page not in PAGE_SIZES:
        per_page = DEFAULT_PAGE_SIZE
    filters = {
        "query": request.args.get("query", ""),
        "per_page": per_page,
        "q": request.args.get("q", "").strip(),
        "seller": request.args.get("seller", "").strip(),
        "min_price": request.args.get("min_price", ""),
        "max_price": request.args.get("max_price", ""),
        "min_stars": request.args.get("min_stars", ""),
        "min_reviews": request.args.get("min_reviews", ""),
        "period": request.args.get("period", ""),
        "sort": request.args.get("sort", "newest"),
        "deals": "1" if request.args.get("deals") else "",
    }
    since = (
        int(time.time()) - PERIODS[filters["period"]]
        if filters["period"] in PERIODS
        else None
    )
    search = dict(
        query_id=_int_arg("query"),
        text=filters["q"] or None,
        seller=filters["seller"] or None,
        min_price=_float_arg("min_price"),
        max_price=_float_arg("max_price"),
        min_stars=_float_arg("min_stars"),
        min_reviews=_int_arg("min_reviews"),
        since=since,
        sort=filters["sort"],
        deals_only=bool(filters["deals"]),
    )
    page = max(1, _int_arg("page", 1))
    items_data, total = db.search_items(
        uid, limit=per_page, offset=(page - 1) * per_page, **search
    )
    pages = max(1, -(-total // per_page))
    if page > pages:  # e.g. the last page emptied after a clear or a stricter filter
        page = pages
        items_data, total = db.search_items(
            uid, limit=per_page, offset=(page - 1) * per_page, **search
        )

    formatted_items = [_format_item(row) for row in items_data]

    # Queries for the filter dropdown
    formatted_queries = []
    selected_query_display = None
    for q in db.get_queries(uid):
        display_name = _query_display(q[1], q[3])
        if filters["query"] == str(q[0]):
            selected_query_display = display_name
        formatted_queries.append({"query_id": q[0], "display": display_name})

    active_filters = sum(
        1
        for key in ("query", "q", "seller", "min_price", "max_price", "min_stars", "min_reviews", "period", "deals")
        if filters[key]
    )
    return dict(
        items=formatted_items,
        total=total,
        page=page,
        pages=pages,
        page_window=_page_window(page, pages),
        page_sizes=PAGE_SIZES,
        first_shown=(page - 1) * per_page + 1 if formatted_items else 0,
        last_shown=(page - 1) * per_page + len(formatted_items),
        queries=formatted_queries,
        f=filters,
        active_filters=active_filters,
        selected_query=filters["query"],
        selected_query_display=selected_query_display,
        autorefresh_enabled=db.get_user_setting(uid, "items_autorefresh") == "True",
        autorefresh_interval=max(5, _setting_int("query_refresh_delay", 60)),
        deal_percent=round((1 - db.DEAL_RATIO) * 100),
        new_items_notice=db.get_user_setting(uid, "new_items_notice") != "False",
    )


@app.route("/items")
def items():
    return render_template("items.html", **_items_context())


@app.route("/items/fragment")
def items_fragment():
    """Results block only, used by the Items page to update without a full reload."""
    return render_template("_items_results.html", **_items_context())


@app.route("/items/clear", methods=["POST"])
def clear_items():
    query_id = request.form.get("query_id", type=int)
    deleted = db.clear_items(current_uid(), query_id)
    flash(
        tr_n("Deleted %(n)s item. ", "Deleted %(n)s items. ", deleted)
        + tr("Existing listings will be recorded silently on the next check."),
        "success",
    )
    return redirect(url_for("items"))


# ------------------------------------------------------------------ progressive web app
@app.route("/manifest.webmanifest")
def manifest():
    icon = lambda name, size, purpose="any": {
        "src": url_for("static", filename=f"icons/{name}"),
        "sizes": size,
        "type": "image/png",
        "purpose": purpose,
    }
    data = {
        "name": "Vinted Notifications",
        "short_name": "Vinted",
        "description": tr("New Vinted listings for your searches, as they appear."),
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "lang": current_language(),
        "background_color": "#F2F2F7",
        "theme_color": "#F2F2F7",
        "icons": [
            icon("icon-192.png", "192x192"),
            icon("icon-512.png", "512x512"),
            icon("icon-maskable-512.png", "512x512", "maskable"),
        ],
        "shortcuts": [
            {"name": tr("Items"), "url": "/items"},
            {"name": tr("Queries"), "url": "/queries"},
        ],
    }
    return Response(json.dumps(data), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    """Served from the root so the worker controls the whole site."""
    response = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


VINTED_LISTING = re.compile(r"^https://(?:[a-z0-9-]+\.)*vinted\.[a-z]{2,}(?:\.[a-z]{2})?/[^\s]*$", re.IGNORECASE)


@app.route("/open")
def open_listing():
    """Hand a listing over to the Vinted app. A notification lands here (inside the installed web app);
    the page then launches the Vinted app, with a button as fallback for when the phone needs a tap."""
    target = request.args.get("u", "")
    if len(target) > 600 or not VINTED_LISTING.match(target):
        return redirect(url_for("items"))
    item = None
    match = re.search(r"/items/(\d+)", urlparse(target).path)
    if match:
        item = db.get_item_for_user(int(match.group(1)), current_uid())
    return render_template(
        "open_listing.html", target=target, android_intent=_android_intent(target), item=item,
        reliability=_seller_reliability(item), modal=False,
    )


def _android_intent(target):
    """Link that launches the Vinted app on Android (the website is the fallback)."""
    parsed = urlparse(target)
    return "intent://" + parsed.netloc + parsed.path + (("?" + parsed.query) if parsed.query else "") + (
        "#Intent;scheme=https;package=fr.vinted;S.browser_fallback_url=" + quote(target, safe="") + ";end"
    )


@app.route("/items/<int:item_id>/sheet")
def item_sheet(item_id):
    """The listing card, as a fragment for the pop-up on the Items page."""
    item = db.get_item_for_user(item_id, current_uid())
    if not item or not item["url"]:
        return ("", 404)
    return render_template(
        "_listing_sheet.html", item=item, target=item["url"], android_intent=_android_intent(item["url"]),
        reliability=_seller_reliability(item), modal=True,
    )


@app.route("/api/items/<int:item_id>/delete", methods=["POST"])
def api_item_delete(item_id):
    uid = current_uid()
    if not db.get_item_for_user(item_id, uid):
        return jsonify({"status": "error", "message": tr("This listing is no longer in your items.")}), 404
    db.delete_item(item_id, uid)
    return jsonify({"status": "success", "message": tr("Removed from your items")})


@app.route("/api/items/<int:item_id>/check", methods=["POST"])
def api_item_check(item_id):
    """Is this listing still for sale? Looked up once when its card is opened (see availability.py)."""
    status, removed = availability.check_one(current_uid(), item_id)
    return jsonify({"status": status, "removed": removed})



def _seller_reliability(item):
    """(level, percent) for the seller summary: from Vinted's 0-5 stars, tempered by how few reviews back them."""
    if not item or item.get("seller_stars") is None:
        return ("none", 0)
    stars, reviews = float(item["seller_stars"]), int(item.get("seller_reviews") or 0)
    percent = int(round(max(0.0, min(stars, 5.0)) / 5 * 100))
    if reviews < 5:
        return ("new", percent)
    if stars >= 4.5 and reviews >= 20:
        return ("high", percent)
    if stars >= 4.0:
        return ("good", percent)
    return ("low", percent)


@app.route("/offline")
def offline():
    return render_template("offline.html")


# ------------------------------------------------------------------ settings
@app.route("/config")
def config():
    uid = current_uid()
    telegram_running, rss_running = _notification_state()
    return render_template(
        "config.html",
        params={
            k: v
            for k, v in db.get_all_parameters().items()
            if k not in SECRET_PARAMETERS
        }
        if is_admin()
        else {},
        us=db.get_user_settings(uid),
        timezones=sorted(zoneinfo.available_timezones()),
        languages=i18n.available_languages(),
        feed_url=_feed_url(),
        telegram_running=telegram_running,
        rss_running=rss_running,
        password_set=bool(_legacy_hash()),
        password_env=bool(_env_password()),
        min_password_length=MIN_PASSWORD_LENGTH,
        push_devices=len(db.get_push_subscriptions(uid)),
    )


# Never handed to a template
SECRET_PARAMETERS = {"web_secret_key", "web_password_hash", "vapid_private_pem", "vapid_subject", "last_scrape_at"}


def _valid_json(text, kind):
    try:
        return isinstance(json.loads(text), kind)
    except ValueError:
        return False


@app.route("/update_config", methods=["POST"])
def update_config():
    uid = current_uid()
    form = request.form

    # ---- personal settings (every account)
    db.set_user_setting(uid, "telegram_enabled", str("telegram_enabled" in form))
    db.set_user_setting(uid, "telegram_token", form.get("telegram_token", "").strip())
    db.set_user_setting(uid, "telegram_chat_id", form.get("telegram_chat_id", "").strip())
    db.set_user_setting(uid, "rss_enabled", str("rss_enabled" in form))
    db.set_user_setting(uid, "message_template", form.get("message_template", ""))
    db.set_user_setting(uid, "banwords", form.get("banwords", ""))
    db.set_user_setting(uid, "items_autorefresh", str("items_autorefresh" in form))
    db.set_user_setting(uid, "new_items_notice", str("new_items_notice" in form))
    timezone = form.get("timezone", "").strip()
    try:
        if timezone:
            zoneinfo.ZoneInfo(timezone)
        db.set_user_setting(uid, "timezone", timezone)
    except Exception:
        flash(tr('Unknown timezone "%(tz)s", keeping the previous one', tz=timezone), "warning")
    if "language" in form:
        language = form.get("language", "")
        if language == "" or language in dict(i18n.available_languages()):
            db.set_user_setting(uid, "language", language)

    # ---- server-wide settings (administrators)
    if is_admin():
        db.set_parameter("rss_port", _clean_int("rss_port", 8080))
        db.set_parameter("rss_max_items", _clean_int("rss_max_items", 100))
        db.set_parameter("rss_public_url", form.get("rss_public_url", "").strip())
        db.set_parameter("items_per_query", _clean_int("items_per_query", 20))
        db.set_parameter("query_refresh_delay", _clean_int("query_refresh_delay", 60))
        db.set_parameter("check_proxies", str("check_proxies" in form))
        db.set_parameter("proxy_list", form.get("proxy_list", ""))
        db.set_parameter("proxy_list_link", form.get("proxy_list_link", ""))
        if accounts_enabled():
            db.set_parameter("allow_signup", str("allow_signup" in form))
        for field, kind, default in (("user_agents", list, "[]"), ("default_headers", dict, "{}")):
            value = form.get(field, default)
            if _valid_json(value, kind):
                db.set_parameter(field, value)
            else:
                flash(tr("%(field)s is not valid JSON, keeping the previous value", field=field), "warning")
        # Reset proxy cache to force refresh on next use
        db.set_parameter("last_proxy_check_time", "1")

    flash(tr("Configuration updated"), "success")
    return redirect(url_for("config"))


@app.route("/control/<process_name>/<action>", methods=["POST"])
def control_process(process_name, action):
    """Switch the account's Telegram bot or RSS feed on or off."""
    if process_name not in ["telegram", "rss"]:
        return jsonify({"status": "error", "message": tr("Invalid process name")})
    if action not in ("start", "stop"):
        return jsonify({"status": "error", "message": tr("Invalid action")})

    uid = current_uid()
    key = "telegram_enabled" if process_name == "telegram" else "rss_enabled"
    running = db.get_user_setting(uid, key) == "True"
    label = "Telegram bot" if process_name == "telegram" else "RSS feed"

    if action == "start":
        if running:
            return jsonify({"status": "warning", "message": tr(f"{label} already running")})
        if process_name == "telegram" and not (
            db.get_user_setting(uid, "telegram_token") and db.get_user_setting(uid, "telegram_chat_id")
        ):
            return jsonify(
                {
                    "status": "error",
                    "message": tr("Please set Telegram token and chat ID in the configuration panel before starting the Telegram process"),
                }
            )
        db.set_user_setting(uid, key, "True")
        logger.info(f"{label} start requested by account {uid}")
        return jsonify({"status": "success", "message": tr(f"{label} start requested")})

    if not running:
        return jsonify({"status": "warning", "message": tr(f"{label} not running")})
    db.set_user_setting(uid, key, "False")
    logger.info(f"{label} stop requested by account {uid}")
    return jsonify({"status": "success", "message": tr(f"{label} stop requested")})


@app.route("/control/status", methods=["GET"])
def process_status():
    telegram_running, rss_running = _notification_state()
    return jsonify({"telegram": telegram_running, "rss": rss_running})


# ------------------------------------------------------------------ allowlist
@app.route("/allowlist")
def allowlist():
    countries = db.get_allowlist(current_uid())
    if countries == 0:
        countries = []

    return render_template("allowlist.html", countries=countries)


@app.route("/add_country", methods=["POST"])
def add_country():
    country = request.form.get("country", "").strip()
    if country:
        message, country_list = core.process_add_country(country, current_uid())
        if message.startswith('Country "') and message.endswith("already in allowlist."):
            shown = tr('Country "%(c)s" already in allowlist.', c=country.replace(" ", "").upper())
        else:
            shown = tr(message)
        flash(shown, "success" if "added" in message else "warning")
    else:
        flash(tr("No country provided"), "error")

    return redirect(url_for("allowlist"))


@app.route("/remove_country/<country>", methods=["POST"])
def remove_country(country):
    message, country_list = core.process_remove_country(country, current_uid())
    flash(tr(message), "success")

    return redirect(url_for("allowlist"))


@app.route("/clear_allowlist", methods=["POST"])
def clear_allowlist():
    db.clear_allowlist(current_uid())
    flash(tr("Allowlist cleared"), "success")

    return redirect(url_for("allowlist"))


# ------------------------------------------------------------------ logs (administrators)
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - (.+?) - ([A-Z]+) - (.*)$")
# ------------------------------------------------------------------ status, accounts overview, push
@app.route("/health")
def health():
    """Liveness probe for Docker and monitors: no data, no login."""
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    """What the status dot in the footer shows: is the scraper alive, is an update waiting?"""
    last = _setting_int("last_scrape_at", 0)
    delay = _setting_int("query_refresh_delay", 60)
    if not last:
        scraper = "starting"
    elif time.time() - last > 3 * delay + 120:
        scraper = "stale"
    else:
        scraper = "ok"
    return jsonify({"ok": True, "scraper": scraper, "update": not _cached_check_version()[0]})


@app.route("/api/users/stats")
@admin_required
def api_users_stats():
    """Fresh query/item counts, so the accounts page can follow the scraper without a reload."""
    stats = {}
    for row in db.list_users():
        stats[str(row["id"])] = tr_n("%(n)s query", "%(n)s queries", row["query_count"]) + " \u00b7 " + tr_n("%(n)s item", "%(n)s items", row["item_count"])
    return jsonify(stats)


@app.route("/api/push/key")
def api_push_key():
    return jsonify({"key": push.public_key()})


MAX_PUSH_DEVICES = 10


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    data = request.get_json(silent=True) or {}
    if not push.valid_subscription(data):
        return jsonify({"status": "error", "message": tr("Invalid subscription")}), 400
    uid = current_uid()
    known = {s["endpoint"] for s in db.get_push_subscriptions(uid)}
    if data["endpoint"] not in known and len(known) >= MAX_PUSH_DEVICES:
        return jsonify({"status": "error", "message": tr("Too many devices. Remove one first.")}), 400
    db.add_push_subscription(uid, data["endpoint"], data["keys"]["p256dh"], data["keys"]["auth"], request.headers.get("User-Agent", ""))
    if request.is_secure and not db.get_parameter("vapid_subject"):
        db.set_parameter("vapid_subject", request.url_root.rstrip("/"))
    return jsonify({"status": "success", "devices": len(db.get_push_subscriptions(uid))})


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    if isinstance(endpoint, str):
        db.remove_push_subscription(endpoint, current_uid())
    return jsonify({"status": "success", "devices": len(db.get_push_subscriptions(current_uid()))})


@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    uid = current_uid()
    payload = push.item_payload(tr("Notifications are on"), tr("You will get a message like this for every new item."), url_for("items"))
    try:
        delay = min(max(int((request.get_json(silent=True) or {}).get("delay", 0)), 0), 20)
    except (TypeError, ValueError):
        delay = 0
    if not db.get_push_subscriptions(uid):
        return jsonify({"status": "error", "message": tr("No device accepted the notification. Turn notifications on from this device first.")}), 502
    if delay:
        # Answer at once and send later: the page never waits on the push service
        push.schedule_send(uid, payload, delay)
        return jsonify({"status": "success", "delay": delay, "message": tr("Notification on its way in %(n)s seconds: leave the app or lock the screen to see it.", n=delay)})
    if push.send_to_user(uid, payload):
        return jsonify({"status": "success", "message": tr("Test notification sent")})
    return jsonify({"status": "error", "message": tr("No device accepted the notification. Turn notifications on from this device first.")}), 502


LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_SCAN_LINES = 20000  # only the tail of the file is read


def _read_log_entries():
    """Newest-first entries from the end of the log file; tracebacks stay with their entry."""
    path = os.path.join("logs", "vinted.log")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        tail = collections.deque(handle, maxlen=LOG_SCAN_LINES)
    entries = []
    for line in tail:
        line = line.rstrip("\n")
        match = LOG_LINE.match(line)
        if match:
            timestamp, module, level, message = match.groups()
            entries.append({"timestamp": timestamp, "module": module, "level": level, "message": message})
        elif entries and line.strip():
            entries[-1]["message"] += "\n" + line  # continuation (traceback, multi-line message)
    entries.reverse()
    return entries


@app.route("/logs")
@admin_only_hidden
def logs():
    return render_template("logs.html", levels=LOG_LEVELS)


@app.route("/api/logs")
@admin_only_hidden
def api_logs():
    offset = max(0, _int_arg("offset", 0))
    limit = min(max(1, _int_arg("limit", 50)), 500)
    level = request.args.get("level", "all").upper()
    needle = request.args.get("q", "").strip().lower()

    try:
        entries = _read_log_entries()
    except Exception as e:
        logger.error(f"Error reading log file: {e}")
        return jsonify({"logs": [], "total": 0, "error": str(e)})

    counts = collections.Counter(entry["level"] for entry in entries)
    if level in LOG_LEVELS:
        entries = [e for e in entries if e["level"] == level]
    if needle:
        entries = [e for e in entries if needle in e["message"].lower() or needle in e["module"].lower()]
    page = entries[offset: offset + limit]
    return jsonify(
        {
            "logs": page,
            "total": len(entries),
            "has_more": offset + limit < len(entries),
            "counts": counts,
        }
    )


def web_ui_process():
    logger.info("Web UI process started")
    try:
        # waitress is a production server: it bounds threads, queued connections and idle
        # time, which the development server does not
        from waitress import serve

        options = dict(
            host="0.0.0.0",
            port=8000,
            threads=8,
            connection_limit=200,
            channel_timeout=30,
            max_request_body_size=256 * 1024,
            ident="vinted-notifications",
        )
        if TRUST_PROXY:
            # waitress drops X-Forwarded-* unless told which proxy to believe. The operator
            # declared one (VN_TRUST_PROXY) and the app is only reachable through it.
            options.update(
                trusted_proxy="*",
                trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port"},
            )
        serve(app, **options)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Web UI process stopped")
    except Exception as e:
        logger.error(f"Error in web UI process: {e}", exc_info=True)
