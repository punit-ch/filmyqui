import sqlite3
import types

import pytest
import requests

import app as appmod

MOVIE = {"id": 1, "title": "Test Movie", "overview": "A film.", "poster_path": "/p.jpg",
         "backdrop_path": "/b.jpg", "release_date": "2024-05-01", "vote_average": 7.8,
         "vote_count": 100, "runtime": 125, "tagline": "Watch it.",
         "genres": [{"id": 18, "name": "Drama"}],
         "videos": {"results": [{"site": "YouTube", "type": "Trailer", "official": True, "key": "abc123"}]},
         "credits": {"cast": [{"name": "Ann Actor", "character": "Lead", "profile_path": None}],
                     "crew": [{"name": "Dee Director", "job": "Director"}]},
         "recommendations": {"results": [{"id": 2, "title": "Other", "poster_path": None,
                                          "release_date": "", "vote_count": 0, "vote_average": 0}]}}
OTHER = {"id": 2, "title": "Other", "poster_path": None, "backdrop_path": None,
         "overview": "", "release_date": "", "vote_count": 0, "vote_average": 0}


def fake_tmdb(path, **params):
    if path == "/genre/movie/list":
        return {"genres": [{"id": 18, "name": "Drama"}]}
    if path == "/movie/1":
        return MOVIE
    if path == "/movie/2":
        return OTHER
    if path.startswith("/movie/") and path.count("/") == 2 and path.split("/")[2].isdigit():
        return None
    if path == "/search/movie":
        return {"results": [MOVIE] if params["query"] == "test" else [], "total_results": 1, "total_pages": 1}
    return {"results": [MOVIE, OTHER], "total_pages": 3}  # list endpoints


def make_client():
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = "tok"
    return c


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    appmod.app.config.update(TESTING=True, DATABASE=db, API_KEY="x",
                             SESSION_COOKIE_SECURE=False)  # the test client talks plain http
    appmod.init_db(db)
    monkeypatch.setattr(appmod, "tmdb", fake_tmdb)
    with appmod.app.test_client() as c:
        with c.session_transaction() as s:
            s["csrf"] = "tok"
        yield c


def post(client, url, **data):
    return client.post(url, data={"csrf_token": "tok", **data})


def count(table, where="1=1"):
    con = sqlite3.connect(appmod.app.config["DATABASE"])
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    finally:
        con.close()


def sql(statement, args=()):
    con = sqlite3.connect(appmod.app.config["DATABASE"])
    con.execute(statement, args)
    con.commit()
    con.close()


# --------------------------------------------------------------------------
# Pages, search, forms (the original tests)
# --------------------------------------------------------------------------

def test_pages_render(client):
    assert b"Test Movie" in client.get("/").data
    assert client.get("/?category=top_rated&page=2").status_code == 200
    assert client.get("/?genre=18").status_code == 200
    assert b"Dee Director" in client.get("/movie/1").data
    assert client.get("/watchlist").status_code == 200


def test_search(client):
    assert client.get("/search").status_code == 302
    assert b"Test Movie" in client.get("/search?query=test").data
    assert b"No movies match" in client.get("/search?query=zzz").data


def test_missing_movie_is_404(client):
    assert client.get("/movie/999").status_code == 404


def test_watchlist_add_is_unique_and_removable(client):
    post(client, "/watchlist/add", movie_id=1)
    post(client, "/watchlist/add", movie_id=1)
    assert count("watchlist") == 1
    post(client, "/watchlist/watched", movie_id=1)
    assert b"Watched" in client.get("/watchlist").data
    post(client, "/watchlist/remove", movie_id=1)
    assert count("watchlist") == 0


def test_remove_is_not_available_via_get(client):
    assert client.get("/watchlist/remove").status_code == 405


def test_csrf_required(client):
    assert client.post("/watchlist/add", data={"movie_id": 1}).status_code == 400


def test_open_redirect_blocked(client):
    r = post(client, "/watchlist/add", movie_id=1, next="//evil.com")
    assert r.headers["Location"].endswith("/watchlist")
    r = post(client, "/watchlist/add", movie_id=1, next="/%2F%2Fevil.com")
    assert r.headers["Location"].endswith("/watchlist")


def test_review_validation_and_delete(client):
    post(client, "/review", movie_id=1, username="A", review="Great", rating=9)
    assert count("reviews") == 0  # rating 9 is rejected
    post(client, "/review", movie_id=1, username="Asha", review="<b>Great</b>", rating=5)
    assert count("reviews") == 1
    page = client.get("/movie/1").data
    assert b"&lt;b&gt;Great" in page          # escaped, not rendered
    assert b"Delete my review" in page
    post(client, "/review/1/delete")
    assert count("reviews") == 0


def test_cannot_delete_someone_elses_review(client):
    sql("INSERT INTO reviews (movie_id, username, review) VALUES (1, 'x', 'y')")
    assert post(client, "/review/1/delete").status_code == 403


# --------------------------------------------------------------------------
# Watchlists belong to one browser
# --------------------------------------------------------------------------

def test_watchlists_are_private_to_each_browser(client):
    other = make_client()
    post(client, "/watchlist/add", movie_id=1)
    assert b"Test Movie" in client.get("/watchlist").data
    assert b"Test Movie" not in other.get("/watchlist").data
    assert b"Your watchlist is empty" in other.get("/watchlist").data

    # The same movie can be saved by both browsers without clashing.
    post(other, "/watchlist/add", movie_id=1)
    assert count("watchlist") == 2

    # One browser changing its list never touches the other's.
    post(other, "/watchlist/watched", movie_id=1)
    assert count("watchlist", "watched = 1") == 1
    post(other, "/watchlist/remove", movie_id=1)
    assert count("watchlist") == 1
    assert b"Test Movie" in client.get("/watchlist").data


def test_each_browser_only_sees_its_own_movies(client):
    other = make_client()
    post(client, "/watchlist/add", movie_id=1)    # "Test Movie"
    post(other, "/watchlist/add", movie_id=2)     # "Other"
    mine, theirs = client.get("/watchlist").data, other.get("/watchlist").data
    assert b">Test Movie</a>" in mine and b">Other</a>" not in mine
    assert b">Other</a>" in theirs and b">Test Movie</a>" not in theirs


def test_navbar_count_and_buttons_follow_the_browser(client):
    other = make_client()
    post(client, "/watchlist/add", movie_id=1)
    assert b'class="count"' in client.get("/").data
    assert b'class="count"' not in other.get("/").data
    assert b"Remove from watchlist" in client.get("/movie/1").data
    assert b"Add to watchlist" in other.get("/movie/1").data


def test_old_global_watchlist_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE watchlist (id INTEGER PRIMARY KEY AUTOINCREMENT, movie_id INTEGER NOT NULL,
            title TEXT, poster_path TEXT, watched INTEGER NOT NULL DEFAULT 0, added_at TEXT);
        CREATE UNIQUE INDEX idx_watchlist_movie ON watchlist(movie_id);
        INSERT INTO watchlist (movie_id, title) VALUES (1, 'Old');
    """)
    con.commit()
    con.close()

    appmod.init_db(path)
    appmod.init_db(path)  # running it again on every start must be harmless

    con = sqlite3.connect(path)
    assert "owner" in {r[1] for r in con.execute("PRAGMA table_info(watchlist)")}
    indexes = {r[1] for r in con.execute("PRAGMA index_list(watchlist)")}
    assert "idx_watchlist_movie" not in indexes
    assert "idx_watchlist_owner_movie" in indexes
    assert con.execute("SELECT COUNT(*) FROM watchlist WHERE owner IS NULL").fetchone()[0] == 1
    con.execute("INSERT INTO watchlist (owner, movie_id) VALUES ('a', 1)")
    con.execute("INSERT INTO watchlist (owner, movie_id) VALUES ('b', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO watchlist (owner, movie_id) VALUES ('a', 1)")
    con.close()


# --------------------------------------------------------------------------
# Sessions, secret key, headers
# --------------------------------------------------------------------------

def test_secret_key_is_required_outside_debug(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    with pytest.raises(RuntimeError) as err:
        appmod.secret_key()
    assert "SECRET_KEY" in str(err.value)
    monkeypatch.setenv("FLASK_DEBUG", "1")
    assert appmod.secret_key() == "dev-only-change-me"
    monkeypatch.setenv("SECRET_KEY", "a-real-key")
    assert appmod.secret_key() == "a-real-key"


def test_session_cookie_is_long_lived_and_httponly(client):
    cookie = client.get("/").headers["Set-Cookie"]
    assert "Expires=" in cookie       # permanent session, survives closing the browser
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_security_headers(client):
    headers = client.get("/").headers
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "Referrer-Policy" in headers


def test_movie_page_has_open_graph_tags(client):
    page = client.get("/movie/1").data
    assert b'property="og:title" content="Test Movie (2024)"' in page
    assert b"https://image.tmdb.org/t/p/w780/b.jpg" in page


# --------------------------------------------------------------------------
# Spam protection
# --------------------------------------------------------------------------

def test_honeypot_field_blocks_bots(client):
    r = post(client, "/review", movie_id=1, username="Bot", review="Buy now", rating=5, website="spam.example")
    assert r.status_code == 302
    assert count("reviews") == 0


def test_reviews_are_rate_limited_per_client(client):
    for i in range(5):
        assert post(client, "/review", movie_id=1, username="Asha", review=f"Review {i}", rating=4).status_code == 302
    r = post(client, "/review", movie_id=1, username="Asha", review="One too many", rating=4)
    assert r.status_code == 429
    assert b"Slow down" in r.data
    assert count("reviews") == 5
    # A different visitor is not affected.
    other = client.post("/review", environ_overrides={"REMOTE_ADDR": "10.0.0.2"},
                        data={"csrf_token": "tok", "movie_id": 1, "username": "Ravi",
                              "review": "Fine", "rating": 3})
    assert other.status_code == 302


def test_watchlist_writes_are_rate_limited(client):
    for _ in range(60):
        assert post(client, "/watchlist/add", movie_id=1).status_code == 302
    assert post(client, "/watchlist/add", movie_id=1).status_code == 429


# --------------------------------------------------------------------------
# Reviews: average comes from SQL, long lists are capped
# --------------------------------------------------------------------------

def test_community_rating_is_the_average(client):
    sql("INSERT INTO reviews (movie_id, username, review, rating) VALUES (1, 'a', 'x', 4)")
    sql("INSERT INTO reviews (movie_id, username, review, rating) VALUES (1, 'b', 'y', 5)")
    page = client.get("/movie/1").data
    assert b"4.5" in page and b"from FilmyQui reviews" in page
    assert b"(2)" in page


def test_only_the_latest_reviews_are_shown(client):
    for i in range(55):
        sql("INSERT INTO reviews (movie_id, username, review, rating) VALUES (1, 'u', ?, 3)", (f"Review number {i}.",))
    page = client.get("/movie/1").data
    assert b"(55)" in page
    assert b"Showing the latest 50 of 55 reviews." in page
    assert b"Review number 54." in page
    assert b"Review number 4." not in page


# --------------------------------------------------------------------------
# The TMDB client itself (the tests above replace it with a fake)
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.headers = headers or {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


@pytest.fixture
def wire(monkeypatch):
    """Script what TMDB 'answers' (responses or exceptions) and record each call and sleep."""
    monkeypatch.setitem(appmod.app.config, "API_KEY", "k")
    monkeypatch.setitem(appmod.app.config, "READ_TOKEN", None)
    state = types.SimpleNamespace(calls=[], script=[], sleeps=[])

    def fake_get(url, params=None, headers=None, timeout=None):
        state.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        item = state.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(appmod._http, "get", fake_get)
    monkeypatch.setattr(appmod.time, "sleep", lambda seconds: state.sleeps.append(seconds))
    return state


def test_tmdb_sends_the_key_and_caches_answers(wire):
    wire.script = [FakeResponse(200, {"a": 1})]
    assert appmod.tmdb("/x", page=1) == {"a": 1}
    assert appmod.tmdb("/x", page=1) == {"a": 1}   # second call comes from the cache
    assert len(wire.calls) == 1
    assert wire.calls[0]["params"] == {"api_key": "k", "page": 1}
    assert wire.calls[0]["timeout"] == 8


def test_tmdb_can_use_a_bearer_token(wire, monkeypatch):
    monkeypatch.setitem(appmod.app.config, "READ_TOKEN", "tok")
    wire.script = [FakeResponse(200, {"a": 1})]
    appmod.tmdb("/x")
    assert wire.calls[0]["headers"] == {"Authorization": "Bearer tok"}
    assert "api_key" not in wire.calls[0]["params"]


def test_tmdb_404_is_none(wire):
    wire.script = [FakeResponse(404)]
    assert appmod.tmdb("/movie/1") is None


def test_tmdb_bad_and_missing_keys_give_friendly_errors(wire, monkeypatch):
    wire.script = [FakeResponse(401)]
    with pytest.raises(appmod.TMDBError) as err:
        appmod.tmdb("/x")
    assert "rejected" in str(err.value)

    monkeypatch.setitem(appmod.app.config, "API_KEY", None)
    with pytest.raises(appmod.TMDBError) as err:
        appmod.tmdb("/y")
    assert "No TMDB API key" in str(err.value)


def test_tmdb_retries_when_rate_limited(wire):
    wire.script = [FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200, {"ok": True})]
    assert appmod.tmdb("/x") == {"ok": True}
    assert len(wire.calls) == 2
    assert wire.sleeps == [1.0]


def test_tmdb_never_makes_a_visitor_wait_long(wire):
    wire.script = [FakeResponse(429, headers={"Retry-After": "120"}), FakeResponse(200, {"ok": True})]
    appmod.tmdb("/x")
    assert wire.sleeps == [2.0]


def test_tmdb_gives_up_after_repeated_errors(wire):
    wire.script = [FakeResponse(429)] * 3
    with pytest.raises(appmod.TMDBError) as err:
        appmod.tmdb("/x")
    assert "busy" in str(err.value)
    assert len(wire.calls) == 3

    wire.script = [FakeResponse(503)] * 3
    with pytest.raises(appmod.TMDBError) as err:
        appmod.tmdb("/z")
    assert "503" in str(err.value)


def test_tmdb_does_not_retry_other_errors(wire):
    wire.script = [FakeResponse(500)]
    with pytest.raises(appmod.TMDBError):
        appmod.tmdb("/x")
    assert len(wire.calls) == 1


def test_tmdb_handles_invalid_json(wire):
    response = FakeResponse(200)

    def bad_json():
        raise ValueError("bad json")

    response.json = bad_json
    wire.script = [response]
    with pytest.raises(appmod.TMDBError) as err:
        appmod.tmdb("/x")
    assert "unreadable" in str(err.value)


def test_tmdb_retries_connection_errors_but_not_timeouts(wire):
    wire.script = [requests.ConnectionError(), FakeResponse(200, {"ok": True})]
    assert appmod.tmdb("/a") == {"ok": True}

    wire.script = [requests.ConnectionError()] * 3
    with pytest.raises(appmod.TMDBError) as err:
        appmod.tmdb("/b")
    assert "Couldn't reach" in str(err.value)

    wire.calls.clear()
    wire.script = [requests.Timeout()]
    with pytest.raises(appmod.TMDBError) as err:
        appmod.tmdb("/c")
    assert "too long" in str(err.value)
    assert len(wire.calls) == 1


def test_cache_expires_and_evicts_oldest():
    cache = appmod.TTLCache(ttl=10, maxsize=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)                                   # "a" is the oldest, so it goes
    assert cache.get("a") is appmod.TTLCache._MISS
    assert cache.get("b") == 2 and cache.get("c") == 3
    old_time = cache._data["b"][0] - 100
    cache._data["b"] = (old_time, 2)                    # pretend it was stored long ago
    assert cache.get("b") is appmod.TTLCache._MISS
