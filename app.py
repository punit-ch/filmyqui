"""FilmyQui: movie discovery, watchlist and reviews (Flask + TMDB + SQLite)."""

import hmac
import os
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import unquote, urlsplit

import requests
from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from flask.helpers import get_debug_flag
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional; plain environment variables still work
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))



def secret_key():
    """The session signing key. A missing key is only tolerated in debug mode.

    Sessions carry the CSRF token, the watchlist owner id and the list of reviews a
    browser may delete, so a guessable key would let anyone forge all three.
    """
    key = os.getenv("SECRET_KEY")
    if key:
        return key
    if get_debug_flag():
        return "dev-only-change-me"
    raise RuntimeError(
        "SECRET_KEY is not set. Add a long random value to your .env file or your host's "
        "environment variables. Generate one with: python -c \"import secrets; "
        "print(secrets.token_hex(32))\"")


app = Flask(__name__)
app.config.update(
    SECRET_KEY=secret_key(),
    DATABASE=os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "movies.db")),
    API_KEY=os.getenv("API_KEY") or os.getenv("TMDB_API_KEY"),
    READ_TOKEN=os.getenv("TMDB_READ_TOKEN"),  # optional: TMDB v4 read token, sent as a Bearer header
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not get_debug_flag(),  # plain-http local dev only works in debug mode
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),
)

if os.getenv("TRUST_PROXY") == "1":  # behind Render/Railway/nginx: use the real client IP and scheme
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

TMDB_URL = "https://api.themoviedb.org/3"
CACHE_SECONDS = 600
CACHE_MAX_ENTRIES = 500
MAX_PAGE = 500  # TMDB refuses anything above this
MAX_ATTEMPTS = 3  # TMDB calls are tried up to this many times on 429/5xx/connection errors
RETRY_STATUSES = {429, 502, 503, 504}
REVIEWS_SHOWN = 50

# key -> (tab label, page heading)
CATEGORIES = {
    "popular": ("Popular", "Popular movies"),
    "top_rated": ("Top rated", "Top rated movies"),
    "now_playing": ("In theatres", "Now in theatres"),
    "upcoming": ("Coming soon", "Coming soon"),
}


# --------------------------------------------------------------------------
# TMDB client (one place for timeouts, error handling and a small cache)
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
    """GET a TMDB endpoint. Returns parsed JSON, or None if TMDB says 404."""
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
    request_params = params if token else {**params, "api_key": api_key}

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
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT,
    movie_id INTEGER NOT NULL,
    title TEXT,
    poster_path TEXT,
    watched INTEGER NOT NULL DEFAULT 0,
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    review TEXT NOT NULL,
    rating INTEGER,
    created_at TEXT
);
"""


def init_db(path=None):
    """Create tables and upgrade older databases in place. Safe to run on every start."""
    db_path = path or app.config["DATABASE"]
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=10)
    con.executescript(SCHEMA)

    def columns(table):
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}

    for table, column, ddl in [
        ("watchlist", "watched", "INTEGER NOT NULL DEFAULT 0"),
        ("watchlist", "added_at", "TEXT"),
        ("watchlist", "owner", "TEXT"),
        ("reviews", "rating", "INTEGER"),
        ("reviews", "created_at", "TEXT"),
    ]:
        if column not in columns(table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # Old versions stored the string "None" when a movie had no poster.
    con.execute("UPDATE watchlist SET poster_path = NULL WHERE poster_path IN ('None', '')")
    # Enforce "one row per movie per browser" in the database, not just in Python.
    # (Older copies had one global list with a unique index on movie_id alone; drop it first.
    # Rows saved before watchlists became per-browser have no owner and are simply never shown.)
    con.execute("DROP INDEX IF EXISTS idx_watchlist_movie")
    con.execute("DELETE FROM watchlist WHERE id NOT IN "
                "(SELECT MIN(id) FROM watchlist GROUP BY owner, movie_id)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_owner_movie ON watchlist(owner, movie_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_reviews_movie ON reviews(movie_id)")
    con.commit()
    con.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"], timeout=10)
        g.db.row_factory = sqlite3.Row
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
# Who owns a watchlist? A random id kept in this browser's session (no accounts).
# --------------------------------------------------------------------------

def current_owner(create=False):
    owner = session.get("owner")
    if not owner and create:
        owner = session["owner"] = secrets.token_hex(16)
    return owner


def watchlist_rows():
    owner = current_owner()
    if not owner:
        return []
    return query("SELECT * FROM watchlist WHERE owner = ? ORDER BY id DESC", (owner,))


# --------------------------------------------------------------------------
# Rate limiting for anything that writes (in memory, per process, keyed by client IP)
# --------------------------------------------------------------------------

_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def rate_limit(bucket, max_calls, seconds):
    """Allow `max_calls` requests per `seconds` per client to this view, then answer 429."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            key = (bucket, request.remote_addr or "unknown")
            moment = time.monotonic()
            with _hits_lock:
                calls = _hits[key]
                while calls and moment - calls[0] > seconds:
                    calls.popleft()
                if len(calls) >= max_calls:
                    abort(429)
                calls.append(moment)
                if len(_hits) > 10_000:  # forget clients we haven't heard from for an hour
                    for stale in [k for k, q in _hits.items() if not q or moment - q[-1] > 3600]:
                        del _hits[stale]
            return view(*args, **kwargs)
        return wrapper
    return decorator


# --------------------------------------------------------------------------
# Helpers shared with templates
# --------------------------------------------------------------------------

@app.template_filter("img")
def img(path, size="w500"):
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None


@app.template_filter("runtime")
def runtime(minutes):
    if not minutes:
        return ""
    try:
        hours, mins = divmod(int(minutes), 60)
        return f"{hours}h {mins}m" if hours else f"{mins}m"
    except (TypeError, ValueError):
        return ""


@app.template_filter("pretty_date")
def pretty_date(value):
    try:
        d = datetime.fromisoformat(value[:10])
        return f"{d.day} {d:%b %Y}"
    except (TypeError, ValueError):
        return ""


def page_url(page):
    """URL for the current page with ?page=N swapped in (keeps other filters)."""
    args = request.args.to_dict()
    args["page"] = page
    return url_for(request.endpoint, **args)


app.jinja_env.globals["csrf_token"] = lambda: session.get("csrf", "")
app.jinja_env.globals["page_url"] = page_url


@app.context_processor
def inject_globals():
    qs = request.query_string.decode("utf-8", "ignore")
    owner = current_owner()
    count = get_db().execute("SELECT COUNT(*) FROM watchlist WHERE owner = ?", (owner,)).fetchone()[0] if owner else 0
    return {"current_url": request.path + (f"?{qs}" if qs else ""), "watchlist_count": count}


def get_page():
    return max(1, min(request.args.get("page", 1, type=int), MAX_PAGE))


def total_pages(data):
    try:
        return max(1, min(int(data.get("total_pages", 1) or 1), MAX_PAGE))
    except (TypeError, ValueError):
        return 1


def back(default):
    """Redirect to the page the form came from, but only to our own pages."""
    target = request.form.get("next", "")
    decoded = target
    for _ in range(3):
        decoded_once = unquote(decoded)
        if decoded_once == decoded:
            break
        decoded = decoded_once
    if decoded.startswith("/") and not decoded.startswith("//") and "\\" not in decoded:
        parsed = urlsplit(decoded)
        if not parsed.scheme and not parsed.netloc:
            return redirect(target)
    return redirect(default)


def pick_trailer(videos):
    youtube = [v for v in videos if v.get("site") == "YouTube"]
    for kind, official_only in (("Trailer", True), ("Trailer", False), ("Teaser", False)):
        for v in youtube:
            if v.get("type") == kind and (v.get("official") or not official_only):
                return v["key"]
    return None


# --------------------------------------------------------------------------
# Security: a small CSRF check for every POST form
# --------------------------------------------------------------------------

@app.before_request
def csrf_protect():
    if request.endpoint == "static":
        return
    session.permanent = True  # keep the session (and your watchlist) after the browser closes
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not hmac.compare_digest(sent.encode(), session["csrf"].encode()):
            abort(400)


CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' https://fonts.googleapis.com",
    "style-src-attr 'unsafe-inline'",  # the movie page sets its backdrop with an inline style
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: https://image.tmdb.org https://i.ytimg.com",
    "frame-src https://www.youtube-nocookie.com",
    "form-action 'self'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
])


@app.after_request
def security_headers(response):
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
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
        featured = next((m for m in movies if m.get("backdrop_path") and m.get("overview")), None)
        if featured:
            movies = [m for m in movies if m is not featured]

    saved_ids = {r["movie_id"] for r in watchlist_rows()}
    heading = f"{genre_name} movies" if genre_id else CATEGORIES[category][1]
    return render_template("index.html", movies=movies, featured=featured, genres=genres,
                           genre_id=genre_id, category=category, categories=CATEGORIES,
                           heading=heading, page=page, total_pages=total_pages(data),
                           saved_ids=saved_ids)


@app.route("/search")
def search():
    text = request.args.get("query", "").strip()
    if not text:
        return redirect(url_for("index"))
    page = get_page()
    data = tmdb("/search/movie", query=text, page=page, include_adult="false") or {}
    return render_template("search.html", query=text, movies=data.get("results", []),
                           total=data.get("total_results", 0), page=page,
                           total_pages=total_pages(data))


@app.route("/movie/<int:movie_id>")
def movie(movie_id):
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
    owner = current_owner()
    in_watchlist = bool(owner and query("SELECT 1 FROM watchlist WHERE owner = ? AND movie_id = ?",
                                        (owner, movie_id)))

    return render_template("movie.html", movie=info, cast=cast, directors=directors,
                           recommendations=recommendations, trailer_key=trailer_key,
                           reviews=reviews, review_count=review_count, community=community,
                           in_watchlist=in_watchlist,
                           my_reviews=set(session.get("my_reviews", [])))


@app.route("/watchlist")
def watchlist():
    rows = watchlist_rows()
    return render_template("watchlist.html",
                           to_watch=[r for r in rows if not r["watched"]],
                           watched=[r for r in rows if r["watched"]])


@app.post("/watchlist/add")
@rate_limit("write", 60, 60)
def add_watchlist():
    movie_id = request.form.get("movie_id", type=int)
    if not movie_id:
        abort(400)
    info = tmdb(f"/movie/{movie_id}")  # take title/poster from TMDB, not from the form
    if info is None:
        abort(404)
    db = get_db()
    title = info.get("title") or "Untitled movie"
    cur = db.execute(
        "INSERT OR IGNORE INTO watchlist (owner, movie_id, title, poster_path, added_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (current_owner(create=True), movie_id, title, info.get("poster_path"), now()))
    db.commit()
    if cur.rowcount:
        flash(f"Added {title} to your watchlist.", "success")
    else:
        flash(f"{title} is already on your watchlist.", "info")
    return back(url_for("watchlist"))


@app.post("/watchlist/remove")
@rate_limit("write", 60, 60)
def remove_watchlist():
    movie_id = request.form.get("movie_id", type=int)
    if not movie_id:
        abort(400)
    db = get_db()
    db.execute("DELETE FROM watchlist WHERE owner = ? AND movie_id = ?",
               (current_owner(create=True), movie_id))
    db.commit()
    flash("Removed from your watchlist.", "info")
    return back(url_for("watchlist"))


@app.post("/watchlist/watched")
@rate_limit("write", 60, 60)
def toggle_watched():
    movie_id = request.form.get("movie_id", type=int)
    if not movie_id:
        abort(400)
    db = get_db()
    db.execute("UPDATE watchlist SET watched = 1 - watched WHERE owner = ? AND movie_id = ?",
               (current_owner(create=True), movie_id))
    db.commit()
    return back(url_for("watchlist"))


@app.post("/review")
@rate_limit("review", 5, 600)
def add_review():
    movie_id = request.form.get("movie_id", type=int)
    if not movie_id:
        abort(400)
    if request.form.get("website"):  # hidden honeypot field: only bots fill it in
        flash("Thanks, your review is posted.", "success")
        return redirect(url_for("movie", movie_id=movie_id) + "#reviews")
    username = request.form.get("username", "").strip()
    text = request.form.get("review", "").strip()
    rating = request.form.get("rating", type=int)

    error = None
    if not 1 <= len(username) <= 40:
        error = "Enter a name of up to 40 characters."
    elif rating not in (1, 2, 3, 4, 5):
        error = "Choose a star rating from 1 to 5."
    elif not 1 <= len(text) <= 2000:
        error = "Write a review of up to 2,000 characters."
    if error:
        flash(error, "error")
        return redirect(url_for("movie", movie_id=movie_id) + "#reviews")

    if tmdb(f"/movie/{movie_id}") is None:
        abort(404)
    db = get_db()
    cur = db.execute(
        "INSERT INTO reviews (movie_id, username, review, rating, created_at) VALUES (?, ?, ?, ?, ?)",
        (movie_id, username, text, rating, now()))
    db.commit()
    # Remember which reviews this browser wrote so it can delete them (no accounts needed).
    session["my_reviews"] = (session.get("my_reviews", []) + [cur.lastrowid])[-100:]
    flash("Thanks, your review is posted.", "success")
    return redirect(url_for("movie", movie_id=movie_id) + "#reviews")


@app.post("/review/<int:review_id>/delete")
@rate_limit("write", 60, 60)
def delete_review(review_id):
    if review_id not in session.get("my_reviews", []):
        abort(403)
    db = get_db()
    db.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
    db.commit()
    flash("Your review was deleted.", "info")
    return back(url_for("index"))


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

def error_page(code, title, message):
    return render_template("error.html", code=code, title=title, message=message), code


@app.errorhandler(400)
def bad_request(_e):
    return error_page(400, "That didn't work",
                      "Your session may have expired. Go back, refresh the page and try again.")


@app.errorhandler(403)
def forbidden(_e):
    return error_page(403, "Not allowed", "You can only delete reviews you wrote in this browser.")


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
    app.run(debug=get_debug_flag())
