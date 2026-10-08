"""FilmyQui: movie discovery, watchlist and reviews (Flask + TMDB + SQLite)."""

import hmac
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone

import requests
from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional; plain environment variables still work
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-only-change-me")
app.config["DATABASE"] = os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "movies.db"))
app.config["API_KEY"] = os.getenv("API_KEY") or os.getenv("TMDB_API_KEY")

TMDB_URL = "https://api.themoviedb.org/3"
CACHE_SECONDS = 600
MAX_PAGE = 500  # TMDB refuses anything above this

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


_cache = {}


def tmdb(path, **params):
    """GET a TMDB endpoint. Returns parsed JSON, or None if TMDB says 404."""
    api_key = app.config["API_KEY"]
    if not api_key:
        raise TMDBError("No TMDB API key found. Add API_KEY to your .env file and restart the app.")

    params = {k: v for k, v in params.items() if v not in (None, "")}
    cache_key = (path, tuple(sorted(params.items())))
    hit = _cache.get(cache_key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    try:
        response = requests.get(f"{TMDB_URL}{path}",
                                params={"api_key": api_key, **params}, timeout=8)
    except requests.RequestException:
        raise TMDBError("Couldn't reach TMDB. Check your internet connection and try again.") from None

    if response.status_code == 404:
        return None
    if response.status_code == 401:
        raise TMDBError("TMDB rejected the API key. Check API_KEY in your .env file.")
    if not response.ok:
        raise TMDBError(f"TMDB returned an error ({response.status_code}). Try again in a moment.")

    data = response.json()
    if len(_cache) > 500:
        _cache.clear()
    _cache[cache_key] = (time.time(), data)
    return data


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    con = sqlite3.connect(path or app.config["DATABASE"])
    con.executescript(SCHEMA)

    def columns(table):
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}

    for table, column, ddl in [
        ("watchlist", "watched", "INTEGER NOT NULL DEFAULT 0"),
        ("watchlist", "added_at", "TEXT"),
        ("reviews", "rating", "INTEGER"),
        ("reviews", "created_at", "TEXT"),
    ]:
        if column not in columns(table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # Old versions stored the string "None" when a movie had no poster.
    con.execute("UPDATE watchlist SET poster_path = NULL WHERE poster_path IN ('None', '')")
    # Enforce "one row per movie" in the database, not just in Python.
    con.execute("DELETE FROM watchlist WHERE id NOT IN (SELECT MIN(id) FROM watchlist GROUP BY movie_id)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_movie ON watchlist(movie_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_reviews_movie ON reviews(movie_id)")
    con.commit()
    con.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
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
# Helpers shared with templates
# --------------------------------------------------------------------------

@app.template_filter("img")
def img(path, size="w500"):
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None


@app.template_filter("runtime")
def runtime(minutes):
    if not minutes:
        return ""
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


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
    count = get_db().execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    return {"current_url": request.path + (f"?{qs}" if qs else ""), "watchlist_count": count}


def get_page():
    return max(1, min(request.args.get("page", 1, type=int), MAX_PAGE))


def total_pages(data):
    return max(1, min(data.get("total_pages", 1), MAX_PAGE))


def back(default):
    """Redirect to the page the form came from, but only to our own pages."""
    target = request.form.get("next", "")
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
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
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not hmac.compare_digest(sent.encode(), session["csrf"].encode()):
            abort(400)


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

    saved_ids = {r["movie_id"] for r in query("SELECT movie_id FROM watchlist")}
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

    reviews = query("SELECT * FROM reviews WHERE movie_id = ? ORDER BY id DESC", (movie_id,))
    ratings = [r["rating"] for r in reviews if r["rating"]]
    community = round(sum(ratings) / len(ratings), 1) if ratings else None
    in_watchlist = bool(query("SELECT 1 FROM watchlist WHERE movie_id = ?", (movie_id,)))

    return render_template("movie.html", movie=info, cast=cast, directors=directors,
                           recommendations=recommendations, trailer_key=trailer_key,
                           reviews=reviews, community=community, in_watchlist=in_watchlist,
                           my_reviews=set(session.get("my_reviews", [])))


@app.route("/watchlist")
def watchlist():
    rows = query("SELECT * FROM watchlist ORDER BY id DESC")
    return render_template("watchlist.html",
                           to_watch=[r for r in rows if not r["watched"]],
                           watched=[r for r in rows if r["watched"]])


@app.post("/watchlist/add")
def add_watchlist():
    movie_id = request.form.get("movie_id", type=int)
    if not movie_id:
        abort(400)
    info = tmdb(f"/movie/{movie_id}")  # take title/poster from TMDB, not from the form
    if info is None:
        abort(404)
    db = get_db()
    cur = db.execute(
        "INSERT OR IGNORE INTO watchlist (movie_id, title, poster_path, added_at) VALUES (?, ?, ?, ?)",
        (movie_id, info["title"], info.get("poster_path"), now()))
    db.commit()
    if cur.rowcount:
        flash(f"Added {info['title']} to your watchlist.", "success")
    else:
        flash(f"{info['title']} is already on your watchlist.", "info")
    return back(url_for("watchlist"))


@app.post("/watchlist/remove")
def remove_watchlist():
    movie_id = request.form.get("movie_id", type=int)
    if not movie_id:
        abort(400)
    db = get_db()
    db.execute("DELETE FROM watchlist WHERE movie_id = ?", (movie_id,))
    db.commit()
    flash("Removed from your watchlist.", "info")
    return back(url_for("watchlist"))


@app.post("/watchlist/watched")
def toggle_watched():
    movie_id = request.form.get("movie_id", type=int)
    if not movie_id:
        abort(400)
    db = get_db()
    db.execute("UPDATE watchlist SET watched = 1 - watched WHERE movie_id = ?", (movie_id,))
    db.commit()
    return back(url_for("watchlist"))


@app.post("/review")
def add_review():
    movie_id = request.form.get("movie_id", type=int)
    if not movie_id:
        abort(400)
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
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
