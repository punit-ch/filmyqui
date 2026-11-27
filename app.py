"""FilmyQui: movie discovery, accounts, watchlists and reviews (Flask + TMDB + SQLite)."""

import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import unquote, urlsplit

import requests
from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from flask.helpers import get_debug_flag
from werkzeug.exceptions import SecurityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))  # next to app.py, whatever the working directory is
except ImportError:  # python-dotenv is optional; plain environment variables still work
    pass

# --------------------------------------------------------------------------
# Configuration. Anything secret comes from the environment, never from the code.
# --------------------------------------------------------------------------

MIN_SECRET_LENGTH = 32
PLACEHOLDER_SECRETS = {"dev-only-change-me", "change-me", "changeme", "secret", "your-secret-key"}


def secret_key():
    """The key that signs session cookies (login state, CSRF token).

    Outside debug mode the app refuses to start unless the key is long and not a placeholder,
    because a guessable key would let anyone forge a login session.
    """
    key = os.getenv("SECRET_KEY", "")
    if get_debug_flag():
        return key or "dev-only-change-me"
    if len(key) < MIN_SECRET_LENGTH or key.lower() in PLACEHOLDER_SECRETS:
        raise RuntimeError(
            f"SECRET_KEY must be set to a random value of at least {MIN_SECRET_LENGTH} characters. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"")
    return key


def check_runtime_safety():
    """Refuse the one combination that turns a bug into remote code execution."""
    if get_debug_flag() and os.getenv("TRUST_PROXY") == "1":
        raise RuntimeError(
            "Debug mode is on while TRUST_PROXY=1, which means this looks like a production host. "
            "Debug mode exposes an interactive Python console. Unset FLASK_DEBUG and try again.")


check_runtime_safety()
SECURE_COOKIES = not get_debug_flag()  # plain-http local development only works in debug mode

DEFAULT_DB = "/tmp/movies.db" if os.getenv("VERCEL") else os.path.join(BASE_DIR, "movies.db")
app = Flask(__name__)
app.config.update(
    SECRET_KEY=secret_key(),
    DATABASE=os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "movies.db")),
    API_KEY=os.getenv("API_KEY") or os.getenv("TMDB_API_KEY"),
    READ_TOKEN=os.getenv("TMDB_READ_TOKEN"),  # optional: TMDB read token, sent as a header (never in a URL)
    SESSION_COOKIE_NAME="__Host-filmyqui" if SECURE_COOKIES else "filmyqui",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SECURE_COOKIES,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=32 * 1024,  # no form on this site needs more than a few KB
)

if os.getenv("ALLOWED_HOSTS"):  # reject requests that arrive with an unexpected Host header
    app.config["TRUSTED_HOSTS"] = [h.strip() for h in os.getenv("ALLOWED_HOSTS").split(",") if h.strip()]

if os.getenv("TRUST_PROXY") == "1":  # behind Render/Railway/nginx: use the real client IP and scheme
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

security_log = logging.getLogger("filmyqui.security")

TMDB_URL = "https://api.themoviedb.org/3"
CACHE_SECONDS = 600
CACHE_MAX_ENTRIES = 500
MAX_PAGE = 500  # TMDB refuses anything above this
MAX_MOVIE_ID = 2**31 - 1
MAX_ATTEMPTS = 3  # TMDB calls are tried up to this many times on 429/5xx/connection errors
RETRY_STATUSES = {429, 502, 503, 504}
REVIEWS_SHOWN = 50
GLOBAL_LIMIT = (240, 60)  # requests per minute per visitor, mostly to protect the TMDB quota

# key -> (tab label, page heading)
CATEGORIES = {
    "popular": ("Popular", "Popular movies"),
    "top_rated": ("Top rated", "Top rated movies"),
    "now_playing": ("In theatres", "Now in theatres"),
    "upcoming": ("Coming soon", "Coming soon"),
}


# --------------------------------------------------------------------------
# TMDB client (one place for timeouts, retries, error handling and a small cache)
# --------------------------------------------------------------------------

class TMDBError(Exception):
    """Raised when TMDB can't give us what we need. The message is user-friendly."""


class TTLCache:
    """A small thread-safe cache: entries expire after `ttl` seconds, oldest go first when full.

    It lives in one process's memory, so with several gunicorn workers each keeps its own copy.
    That is fine for this app (the data is public); use Redis if you ever need one shared cache.
    """

    _MISS = object()

    def __init__(self, ttl, maxsize):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return self._MISS
            stored_at, value = hit
            if time.time() - stored_at >= self.ttl:
                del self._data[key]
                return self._MISS
            self._data.move_to_end(key)
            return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = (time.time(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self):
        with self._lock:
            self._data.clear()


_cache = TTLCache(CACHE_SECONDS, CACHE_MAX_ENTRIES)
_http = requests.Session()  # reuses connections between TMDB calls


def _backoff(attempt, response=None):
    """Seconds to wait before retrying. Honours Retry-After, but never makes a visitor wait long."""
    delay = 0.5 * 2 ** attempt
    if response is not None:
        try:
            delay = float(response.headers.get("Retry-After", delay))
        except (TypeError, ValueError):
            pass
    return max(0.0, min(delay, 2.0))


def tmdb(path, **params):
    """GET a TMDB endpoint. Returns parsed JSON, or None if TMDB says 404.

    Error messages shown to visitors never include the request URL, because with an API key
    in the query string the URL would contain the key.
    """
    token = app.config.get("READ_TOKEN")
    api_key = app.config.get("API_KEY")
    if not (token or api_key):
        raise TMDBError("No TMDB API key found. Add API_KEY to your .env file and restart the app.")

    params = {k: v for k, v in params.items() if v not in (None, "")}
    cache_key = (path, tuple(sorted(params.items())))
    hit = _cache.get(cache_key)
    if hit is not TTLCache._MISS:
        return hit

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request_params = params if token else {"api_key": api_key, **params}

    response = None
    for attempt in range(MAX_ATTEMPTS):
        last = attempt == MAX_ATTEMPTS - 1
        try:
            response = _http.get(f"{TMDB_URL}{path}", params=request_params,
                                 headers=headers, timeout=8)
        except requests.Timeout:
            raise TMDBError("TMDB took too long to answer. Try again in a moment.") from None
        except requests.RequestException:
            if last:
                raise TMDBError("Couldn't reach TMDB. Check your internet connection and try again.") from None
            time.sleep(_backoff(attempt))
            continue
        if response.status_code in RETRY_STATUSES and not last:
            time.sleep(_backoff(attempt, response))
            continue
        break

    if response.status_code == 404:
        return None
    if response.status_code == 401:
        raise TMDBError("TMDB rejected the API key. Check API_KEY in your .env file.")
    if response.status_code == 429:
        raise TMDBError("TMDB is busy right now. Try again in a few seconds.")
    if not response.ok:
        raise TMDBError(f"TMDB returned an error ({response.status_code}). Try again in a moment.")

    try:
        data = response.json()
    except ValueError:
        raise TMDBError("TMDB returned an unreadable response. Try again in a moment.") from None
    _cache.set(cache_key, data)
    return data


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    poster_path TEXT,
    watched INTEGER NOT NULL DEFAULT 0,
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    movie_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    review TEXT NOT NULL CHECK (length(review) BETWEEN 1 AND 2000),
    rating INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_movie ON reviews(movie_id);
"""


def init_db(path=None):
    path = path or app.config["DATABASE"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)  # e.g. ~/filmyqui-data
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)

    # Upgrade databases made by older versions of the app.
    def columns(table):
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}  # table names are constants below

    for table, column, ddl in [
        ("reviews", "rating", "INTEGER"),
        ("reviews", "created_at", "TEXT"),
        ("watchlist", "watched", "INTEGER NOT NULL DEFAULT 0"),
        ("watchlist", "added_at", "TEXT"),
        ("watchlist", "user_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE"),
        ("reviews", "user_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE"),
    ]:
        if column not in columns(table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    con.execute("UPDATE watchlist SET poster_path = NULL WHERE poster_path IN ('None', '')")
    # Older versions kept one shared watchlist with a unique index on movie_id alone.
    # Rows saved back then belong to nobody (user_id is NULL) and are never shown.
    con.execute("DROP INDEX IF EXISTS idx_watchlist_movie")
    con.execute("DROP INDEX IF EXISTS idx_watchlist_owner_movie")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_user_movie ON watchlist(user_id, movie_id)")
    # One review per person per movie (reviews from before accounts have no user_id and are exempt).
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_user_movie ON reviews(user_id, movie_id)")
    con.commit()
    con.close()
    try:
        os.chmod(path, 0o600)  # the database holds password hashes: owner-only access
    except OSError:
        pass  # e.g. Windows, or a filesystem without permissions


def get_db():
    if "db" not in g:
        db_path = app.config["DATABASE"]
        if not os.path.exists(db_path):
            init_db(db_path)
        g.db = sqlite3.connect(db_path, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=()):
    return get_db().execute(sql, args).fetchall()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------
# Cleaning and checking what visitors type
# --------------------------------------------------------------------------

USERNAME_RE = re.compile(r"[A-Za-z0-9_]{3,30}")
RESERVED_USERNAMES = {"admin", "administrator", "root", "system", "support", "moderator", "mod",
                      "staff", "filmyqui", "tmdb", "null", "undefined", "anonymous"}
PASSWORD_MIN, PASSWORD_MAX = 10, 128
COMMON_PASSWORDS = {
    "password", "password1", "password123", "1234567890", "12345678910", "0123456789", "qwertyuiop",
    "qwerty12345", "qwerty123456", "iloveyou123", "letmein1234", "welcome1234", "admin12345",
    "abc1234567", "1q2w3e4r5t", "1qaz2wsx3edc", "passw0rd123", "changeme123", "football123",
    "monkey12345", "dragon12345", "baseball123", "superman123", "trustno1234", "filmyqui123",
}
# Text-direction override characters can make text display as something it is not.
_BIDI_CONTROLS = dict.fromkeys(map(ord, "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c"))


def clean_text(value, multiline=False):
    """Normalise what a visitor typed: Unicode NFC, no control or direction-override characters,
    no runs of blank space. Escaping for HTML is done separately by Jinja when it is shown."""
    value = unicodedata.normalize("NFC", value or "").replace("\r\n", "\n").replace("\r", "\n")
    value = value.translate(_BIDI_CONTROLS)
    kept = []
    for ch in value:
        if ch == "\n" and multiline:
            kept.append(ch)
        elif ch in "\n\t":
            kept.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cs", "Co"):
            continue
        else:
            kept.append(ch)
    text = re.sub(r" {2,}", " ", "".join(kept))
    if multiline:
        text = re.sub(r" ?\n ?", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_password(password):
    return unicodedata.normalize("NFKC", password or "")


def username_error(username):
    if not USERNAME_RE.fullmatch(username):
        return "Usernames are 3 to 30 characters: letters, numbers and underscores only."
    if username.lower() in RESERVED_USERNAMES:
        return "That username is reserved. Please choose another."
    return None


def password_error(password, username=""):
    if len(password) < PASSWORD_MIN:
        return f"Use at least {PASSWORD_MIN} characters for your password."
    if len(password) > PASSWORD_MAX:
        return f"Use at most {PASSWORD_MAX} characters for your password."
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(lowered)) < 4:
        return "That password is too easy to guess. Try a longer phrase."
    if username and username.lower() in lowered:
        return "Your password shouldn't contain your username."
    return None


def safe_next(target):
    """Return `target` if it is a path on this site, otherwise None (stops open redirects).

    The address is checked as written and again with any percent-escapes undone, so
    "/%2F%2Fevil.com" (which means "//evil.com") is refused too.
    """
    if not target or len(target) > 2000:
        return None
    decoded = target
    for _ in range(3):
        again = unquote(decoded)
        if again == decoded:
            break
        decoded = again
    for text in (target, decoded):
        if any(ord(c) < 32 or c == "\\" for c in text):
            return None
        parts = urlsplit(text)
        if parts.scheme or parts.netloc or not text.startswith("/") or text.startswith("//"):
            return None
    return target


def back(default):
    """Redirect to the page the form came from, but only to our own pages."""
    return redirect(safe_next(request.form.get("next", "")) or default)


def movie_id_from_form():
    movie_id = request.form.get("movie_id", type=int)
    if movie_id is None or not 1 <= movie_id <= MAX_MOVIE_ID:
        abort(400)
    return movie_id


# --------------------------------------------------------------------------
# Rate limiting (in memory, per process, so it slows abuse down rather than stopping a botnet)
# --------------------------------------------------------------------------

class RateLimiter:
    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key, max_calls, seconds):
        moment = time.monotonic()
        with self._lock:
            calls = self._hits[key]
            while calls and moment - calls[0] > seconds:
                calls.popleft()
            if len(calls) >= max_calls:
                return False
            calls.append(moment)
            if len(self._hits) > 10_000:  # forget clients we haven't heard from for an hour
                for stale in [k for k, q in self._hits.items() if not q or moment - q[-1] > 3600]:
                    del self._hits[stale]
            return True

    def clear(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip():
    return request.remote_addr or "unknown"


def throttle(bucket, key, max_calls, seconds):
    """Answer 429 if `key` has used up its `max_calls` per `seconds` in this bucket."""
    if not limiter.allow((bucket, key), max_calls, seconds):
        abort(429)


def rate_limit(bucket, max_calls, seconds):
    """Decorator: allow `max_calls` requests per `seconds` per client IP to this view."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            throttle(bucket, client_ip(), max_calls, seconds)
            return view(*args, **kwargs)
        return wrapper
    return decorator


# --------------------------------------------------------------------------
# Helpers shared with templates
# --------------------------------------------------------------------------

_IMAGE_PATH_RE = re.compile(r"/[A-Za-z0-9_-]{1,64}\.(?:jpg|jpeg|png|webp)")
_YOUTUBE_KEY_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")


@app.template_filter("img")
def img(path, size="w500"):
    """A TMDB image URL, but only for paths that look like real image paths."""
    if isinstance(path, str) and _IMAGE_PATH_RE.fullmatch(path):
        return f"https://image.tmdb.org/t/p/{size}{path}"
    return None


@app.template_filter("runtime")
def runtime(minutes):
    if not minutes:
        return ""
    try:
        hours, mins = divmod(int(minutes), 60)
    except (TypeError, ValueError):
        return ""
    return f"{hours}h {mins}m" if hours else f"{mins}m"


@app.template_filter("pretty_date")
def pretty_date(value):
    try:
        d = datetime.fromisoformat(value[:10])
        return f"{d.day} {d:%b %Y}"
    except (TypeError, ValueError):
        return ""


PAGE_URL_ARGS = ("category", "genre", "query")  # the only query parameters we carry between pages


def page_url(page):
    """URL for the current page with ?page=N swapped in (keeps the filters we know about).

    Passing arbitrary query keys to url_for would let a visitor use its special arguments
    (_external, _scheme, ...), so only a fixed list is kept.
    """
    args = {k: v for k, v in request.args.items() if k in PAGE_URL_ARGS}
    args["page"] = page
    return url_for(request.endpoint, **args)


app.jinja_env.globals["csrf_token"] = lambda: session.get("csrf", "")
app.jinja_env.globals["page_url"] = page_url


@app.context_processor
def inject_globals():
    qs = request.query_string.decode("utf-8", "ignore")
    user = getattr(g, "user", None)
    count = 0
    if user:
        count = get_db().execute("SELECT COUNT(*) FROM watchlist WHERE user_id = ?", (user["id"],)).fetchone()[0]
    return {"current_url": request.path + (f"?{qs}" if qs else ""), "current_user": user,
            "watchlist_count": count}


def get_page():
    return max(1, min(request.args.get("page", 1, type=int), MAX_PAGE))


def total_pages(data):
    try:
        return max(1, min(int(data.get("total_pages", 1) or 1), MAX_PAGE))
    except (TypeError, ValueError):
        return 1


def pick_trailer(videos):
    youtube = [v for v in videos if v.get("site") == "YouTube"]
    for kind, official_only in (("Trailer", True), ("Trailer", False), ("Teaser", False)):
        for v in youtube:
            key = v.get("key")
            if (v.get("type") == kind and (v.get("official") or not official_only)
                    and isinstance(key, str) and _YOUTUBE_KEY_RE.fullmatch(key)):
                return key
    return None


# --------------------------------------------------------------------------
# Accounts and sessions
# --------------------------------------------------------------------------

# Checking a password for a username that doesn't exist still costs the same time as a real
# check, so response time doesn't reveal which usernames are registered.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def load_user():
    g.user = None
    uid = session.get("uid")
    if uid is None:
        return
    rows = query("SELECT id, username, session_version FROM users WHERE id = ?", (uid,))
    if not rows or rows[0]["session_version"] != session.get("sv"):
        session.pop("uid", None)  # account deleted, or password changed on another device
        session.pop("sv", None)
        return
    g.user = rows[0]


def start_session(user):
    """Log `user` in with a brand-new session (so a pre-login session can't be reused)."""
    session.clear()
    session["csrf"] = secrets.token_hex(16)
    session["uid"] = user["id"]
    session["sv"] = user["session_version"]
    session.permanent = True


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            flash("Log in to continue.", "info")
            if request.method == "GET":
                target = safe_next(request.full_path.rstrip("?"))
            else:
                target = safe_next(request.form.get("next", ""))
            return redirect(url_for("login", next=target) if target else url_for("login"))
        return view(*args, **kwargs)
    return wrapper


@app.before_request
def guard():
    """Runs before every page: rate limit, session, who is logged in, and the CSRF check."""
    if request.endpoint == "static":
        return
    throttle("global", client_ip(), *GLOBAL_LIMIT)
    session.permanent = True
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    load_user()
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not hmac.compare_digest(sent.encode(), session["csrf"].encode()):
            abort(400)


CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: https://image.tmdb.org https://i.ytimg.com",
    "frame-src https://www.youtube-nocookie.com",
    "connect-src 'self'",
    "object-src 'none'",
    "form-action 'self'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
])
PRIVATE_ENDPOINTS = {"login", "register", "account", "watchlist"}


@app.after_request
def security_headers(response):
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP + ("; upgrade-insecure-requests" if request.is_secure else ""))
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if request.is_secure:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    # Pages that show or change someone's account must not be kept by browsers or proxies.
    if request.endpoint != "static" and (getattr(g, "user", None) or request.endpoint in PRIVATE_ENDPOINTS):
        h["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.route("/")
def index():
    category = request.args.get("category", "popular")
    if category not in CATEGORIES:
        category = "popular"
    genre_id = request.args.get("genre", type=int)
    page = get_page()

    genres = (tmdb("/genre/movie/list") or {}).get("genres", [])
    genre_name = next((g_["name"] for g_ in genres if g_["id"] == genre_id), None)
    if genre_id and not genre_name:
        genre_id = None

    if genre_id:
        data = tmdb("/discover/movie", with_genres=genre_id, sort_by="popularity.desc",
                    include_adult="false", page=page) or {}
    else:
        data = tmdb(f"/movie/{category}", page=page) or {}
    movies = data.get("results", [])

    # The first result with artwork becomes the big banner on page 1.
    featured = None
    if page == 1 and not genre_id:
        featured = next((m for m in movies if img(m.get("backdrop_path")) and m.get("overview")), None)
        if featured:
            movies = [m for m in movies if m is not featured]

    saved_ids = set()
    if g.user:
        saved_ids = {r["movie_id"] for r in query("SELECT movie_id FROM watchlist WHERE user_id = ?", (g.user["id"],))}
    heading = f"{genre_name} movies" if genre_id else CATEGORIES[category][1]
    return render_template("index.html", movies=movies, featured=featured, genres=genres,
                           genre_id=genre_id, category=category, categories=CATEGORIES,
                           heading=heading, page=page, total_pages=total_pages(data),
                           saved_ids=saved_ids)


@app.route("/search")
def search():
    text = clean_text(request.args.get("query", ""))[:100]
    if not text:
        return redirect(url_for("index"))
    page = get_page()
    data = tmdb("/search/movie", query=text, page=page, include_adult="false") or {}
    return render_template("search.html", query=text, movies=data.get("results", []),
                           total=data.get("total_results", 0), page=page,
                           total_pages=total_pages(data))


@app.route("/movie/<int:movie_id>")
def movie(movie_id):
    if not 1 <= movie_id <= MAX_MOVIE_ID:
        abort(404)
    info = tmdb(f"/movie/{movie_id}", append_to_response="videos,recommendations,credits")
    if info is None:
        abort(404)

    credits = info.get("credits") or {}
    cast = (credits.get("cast") or [])[:12]
    directors = [c["name"] for c in credits.get("crew") or [] if c.get("job") == "Director"]
    recommendations = ((info.get("recommendations") or {}).get("results") or [])[:12]
    trailer_key = pick_trailer((info.get("videos") or {}).get("results") or [])

    reviews = query("SELECT * FROM reviews WHERE movie_id = ? ORDER BY id DESC LIMIT ?",
                    (movie_id, REVIEWS_SHOWN))
    stats = query("SELECT COUNT(*) AS n, ROUND(AVG(rating), 1) AS average FROM reviews WHERE movie_id = ?",
                  (movie_id,))[0]
    review_count, community = stats["n"], stats["average"]
    in_watchlist = False
    already_reviewed = False
    if g.user:
        in_watchlist = bool(query("SELECT 1 FROM watchlist WHERE user_id = ? AND movie_id = ?",
                                  (g.user["id"], movie_id)))
        already_reviewed = bool(query("SELECT 1 FROM reviews WHERE user_id = ? AND movie_id = ?",
                                      (g.user["id"], movie_id)))

    return render_template("movie.html", movie=info, cast=cast, directors=directors,
                           recommendations=recommendations, trailer_key=trailer_key,
                           reviews=reviews, review_count=review_count, community=community,
                           in_watchlist=in_watchlist, already_reviewed=already_reviewed)


# --------------------------------------------------------------------------
# Register, log in, log out, account
# --------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))
    nxt = safe_next(request.values.get("next", ""))
    if request.method == "POST":
        throttle("register", client_ip(), 10, 3600)
        username = clean_text(request.form.get("username", ""))
        password = normalize_password(request.form.get("password", ""))
        confirm = normalize_password(request.form.get("confirm", ""))
        error = (username_error(username) or password_error(password, username)
                 or (None if password == confirm else "The two passwords don't match."))
        if not error:
            try:
                db = get_db()
                cur = db.execute(
                    "INSERT INTO users (username, username_key, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (username, username.lower(), generate_password_hash(password), now()))
                db.commit()
            except sqlite3.IntegrityError:
                error = "That username is taken. Please choose another."
        if error:
            flash(error, "error")
            return render_template("register.html", username=username, next_url=nxt), 400
        start_session({"id": cur.lastrowid, "session_version": 1})
        security_log.info("register ok user_id=%s ip=%s", cur.lastrowid, client_ip())
        flash(f"Welcome to FilmyQui, {username}!", "success")
        return redirect(nxt or url_for("index"))
    return render_template("register.html", username="", next_url=nxt)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    nxt = safe_next(request.values.get("next", ""))
    if request.method == "POST":
        throttle("login-ip", client_ip(), 10, 600)
        username = clean_text(request.form.get("username", ""))
        password = normalize_password(request.form.get("password", ""))
        throttle("login-user", username.lower()[:64], 10, 900)  # slows guessing against one account
        rows = query("SELECT id, username, password_hash, session_version FROM users WHERE username_key = ?",
                     (username.lower(),))
        stored = rows[0]["password_hash"] if rows else _DUMMY_HASH
        valid = check_password_hash(stored, password[:PASSWORD_MAX]) and bool(rows)
        if valid:
            start_session(rows[0])
            security_log.info("login ok user_id=%s ip=%s", rows[0]["id"], client_ip())
            return redirect(nxt or url_for("index"))
        security_log.warning("login failed username=%r ip=%s", username[:40], client_ip())
        flash("Wrong username or password.", "error")
        return render_template("login.html", username=username, next_url=nxt), 401
    return render_template("login.html", username="", next_url=nxt)


@app.post("/logout")
def logout():
    session.clear()
    flash("You're logged out.", "info")
    return redirect(url_for("index"))


@app.get("/account")
@login_required
def account():
    stats = query("SELECT (SELECT COUNT(*) FROM watchlist WHERE user_id = ?) AS saved, "
                  "(SELECT COUNT(*) FROM reviews WHERE user_id = ?) AS reviews",
                  (g.user["id"], g.user["id"]))[0]
    return render_template("account.html", stats=stats)


def _check_current_password(password):
    row = query("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],))[0]
    return check_password_hash(row["password_hash"], normalize_password(password)[:PASSWORD_MAX])


@app.post("/account/password")
@login_required
def change_password():
    throttle("account-user", g.user["id"], 5, 900)
    current = request.form.get("current", "")
    new = normalize_password(request.form.get("password", ""))
    confirm = normalize_password(request.form.get("confirm", ""))
    error = None
    if not _check_current_password(current):
        error = "Your current password is wrong."
    else:
        error = password_error(new, g.user["username"]) or (None if new == confirm else "The two new passwords don't match.")
    if error:
        flash(error, "error")
        return redirect(url_for("account"))
    db = get_db()
    # Bumping session_version logs this account out everywhere else.
    db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
               (generate_password_hash(new), g.user["id"]))
    db.commit()
    fresh = query("SELECT id, session_version FROM users WHERE id = ?", (g.user["id"],))[0]
    start_session(fresh)
    security_log.info("password changed user_id=%s ip=%s", fresh["id"], client_ip())
    flash("Password changed. Other devices have been logged out.", "success")
    return redirect(url_for("account"))


@app.post("/account/delete")
@login_required
def delete_account():
    throttle("account-user", g.user["id"], 5, 900)
    if not _check_current_password(request.form.get("current", "")):
        flash("Your password is wrong, so the account was not deleted.", "error")
        return redirect(url_for("account"))
    uid = g.user["id"]
    db = get_db()
    db.execute("DELETE FROM reviews WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM watchlist WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    db.commit()
    session.clear()
    security_log.info("account deleted user_id=%s ip=%s", uid, client_ip())
    flash("Your account and everything attached to it were deleted.", "info")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Watchlist and reviews: every query is limited to the logged-in user's own rows
# --------------------------------------------------------------------------

@app.route("/watchlist")
@login_required
def watchlist():
    rows = query("SELECT * FROM watchlist WHERE user_id = ? ORDER BY id DESC", (g.user["id"],))
    return render_template("watchlist.html",
                           to_watch=[r for r in rows if not r["watched"]],
                           watched=[r for r in rows if r["watched"]])


@app.post("/watchlist/add")
@login_required
@rate_limit("write", 60, 60)
def add_watchlist():
    movie_id = movie_id_from_form()
    info = tmdb(f"/movie/{movie_id}")  # take title/poster from TMDB, not from the form
    if info is None:
        abort(404)
    title = clean_text(str(info.get("title") or ""))[:300] or "Untitled"
    poster = info.get("poster_path") if img(info.get("poster_path")) else None
    db = get_db()
    cur = db.execute(
        "INSERT OR IGNORE INTO watchlist (user_id, movie_id, title, poster_path, added_at) VALUES (?, ?, ?, ?, ?)",
        (g.user["id"], movie_id, title, poster, now()))
    db.commit()
    if cur.rowcount:
        flash(f"Added {title} to your watchlist.", "success")
    else:
        flash(f"{title} is already on your watchlist.", "info")
    return back(url_for("watchlist"))


@app.post("/watchlist/remove")
@login_required
@rate_limit("write", 60, 60)
def remove_watchlist():
    movie_id = movie_id_from_form()
    db = get_db()
    db.execute("DELETE FROM watchlist WHERE user_id = ? AND movie_id = ?", (g.user["id"], movie_id))
    db.commit()
    flash("Removed from your watchlist.", "info")
    return back(url_for("watchlist"))


@app.post("/watchlist/watched")
@login_required
@rate_limit("write", 60, 60)
def toggle_watched():
    movie_id = movie_id_from_form()
    db = get_db()
    db.execute("UPDATE watchlist SET watched = 1 - watched WHERE user_id = ? AND movie_id = ?",
               (g.user["id"], movie_id))
    db.commit()
    return back(url_for("watchlist"))


@app.post("/review")
@login_required
@rate_limit("review-ip", 20, 600)
def add_review():
    movie_id = movie_id_from_form()
    to_reviews = url_for("movie", movie_id=movie_id) + "#reviews"
    if request.form.get("website"):  # hidden honeypot field: only bots fill it in
        flash("Thanks, your review is posted.", "success")
        return redirect(to_reviews)
    throttle("review-user", g.user["id"], 5, 600)
    text = clean_text(request.form.get("review", ""), multiline=True)
    rating = request.form.get("rating", type=int)

    error = None
    if rating not in (1, 2, 3, 4, 5):
        error = "Choose a star rating from 1 to 5."
    elif not 1 <= len(text) <= 2000:
        error = "Write a review of up to 2,000 characters."
    if error:
        flash(error, "error")
        return redirect(to_reviews)

    if tmdb(f"/movie/{movie_id}") is None:
        abort(404)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO reviews (user_id, movie_id, username, review, rating, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (g.user["id"], movie_id, g.user["username"], text, rating, now()))
        db.commit()
    except sqlite3.IntegrityError:
        flash("You've already reviewed this movie. Delete your review first if you want to write a new one.", "error")
        return redirect(to_reviews)
    flash("Thanks, your review is posted.", "success")
    return redirect(to_reviews)


@app.post("/review/<int:review_id>/delete")
@login_required
@rate_limit("write", 60, 60)
def delete_review(review_id):
    db = get_db()
    cur = db.execute("DELETE FROM reviews WHERE id = ? AND user_id = ?", (review_id, g.user["id"]))
    db.commit()
    if not cur.rowcount:
        abort(404)  # not yours, or doesn't exist: the answer is the same
    flash("Your review was deleted.", "info")
    return back(url_for("index"))


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

def error_page(code, title, message):
    return render_template("error.html", code=code, title=title, message=message), code


@app.errorhandler(SecurityError)
def untrusted_host(_e):
    # Raised when the Host header isn't one of ALLOWED_HOSTS. The normal error page can't be
    # drawn for such a request (building its links needs a trusted host), so answer in plain text.
    return "Bad request: this host name is not allowed.", 400, {"Content-Type": "text/plain; charset=utf-8"}


@app.errorhandler(400)
def bad_request(_e):
    return error_page(400, "That didn't work",
                      "Your session may have expired. Go back, refresh the page and try again.")


@app.errorhandler(413)
def too_large(_e):
    return error_page(413, "That's too much", "What you sent was too large. Try something shorter.")


@app.errorhandler(429)
def too_many(_e):
    return error_page(429, "Slow down a little", "You're doing that too often. Wait a few minutes and try again.")


@app.errorhandler(404)
def not_found(_e):
    return error_page(404, "Page not found", "That page or movie doesn't exist. Try searching instead.")


@app.errorhandler(TMDBError)
def tmdb_down(e):
    return error_page(502, "Movie data unavailable", str(e))


@app.errorhandler(500)
def server_error(_e):
    return error_page(500, "Something broke on our side", "Try again in a moment.")


init_db()

if __name__ == "__main__":
    app.run(debug=get_debug_flag())  # binds to 127.0.0.1 only; production runs under gunicorn
