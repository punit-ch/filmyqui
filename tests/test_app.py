import copy
import logging
import os
import sqlite3
import types

import pytest
import requests

import app as appmod

PASSWORD = "correct horse battery staple"
REAL_TMDB = appmod.tmdb  # the client fixture swaps in a fake; a few tests want the real one

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


def fake_movie(movie_id):
    if movie_id == 1:
        return MOVIE
    if movie_id == 2:
        return OTHER
    if 3 <= movie_id <= 12:
        extra = copy.deepcopy(MOVIE)
        extra.update(id=movie_id, title=f"Movie {movie_id}")
        return extra
    return None


def fake_tmdb(path, **params):
    if path == "/genre/movie/list":
        return {"genres": [{"id": 18, "name": "Drama"}]}
    if path.startswith("/movie/") and path.count("/") == 2 and path.split("/")[2].isdigit():
        return fake_movie(int(path.split("/")[2]))
    if path == "/search/movie":
        return {"results": [MOVIE] if params["query"] == "test" else [], "total_results": 1, "total_pages": 1}
    return {"results": [MOVIE, OTHER], "total_pages": 3}  # list endpoints


def make_client():
    return appmod.app.test_client()


def csrf(client):
    with client.session_transaction() as s:
        s.setdefault("csrf", "tok")
        return s["csrf"]


def post(client, url, **data):
    return client.post(url, data={"csrf_token": csrf(client), **data})


def signup(client, name="asha", password=PASSWORD):
    r = post(client, "/register", username=name, password=password, confirm=password)
    assert r.status_code == 302, r.data[:300]
    return r


def login(client, name="asha", password=PASSWORD, **extra):
    return post(client, "/login", username=name, password=password, **extra)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    appmod.app.config.update(TESTING=True, DATABASE=path, API_KEY="x",
                             SESSION_COOKIE_SECURE=False)  # the test client talks plain http
    appmod.init_db(path)
    return path


@pytest.fixture
def client(db_path, monkeypatch):
    """A visitor who is not logged in."""
    monkeypatch.setattr(appmod, "tmdb", fake_tmdb)
    return make_client()


@pytest.fixture
def asha(client):
    """A logged-in visitor called asha."""
    signup(client, "asha")
    return client


def count(table, where="1=1"):
    con = sqlite3.connect(appmod.app.config["DATABASE"])
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    finally:
        con.close()


def sql(statement, args=()):
    con = sqlite3.connect(appmod.app.config["DATABASE"])
    try:
        con.execute(statement, args)
        con.commit()
    finally:
        con.close()


def location(response):
    return response.headers["Location"]


# --------------------------------------------------------------------------
# Public pages, search, forms
# --------------------------------------------------------------------------

def test_public_pages_render(client):
    assert b"Test Movie" in client.get("/").data
    assert client.get("/?category=top_rated&page=2").status_code == 200
    assert client.get("/?genre=18").status_code == 200
    assert b"Dee Director" in client.get("/movie/1").data


def test_search(client):
    assert client.get("/search").status_code == 302
    assert b"Test Movie" in client.get("/search?query=test").data
    assert b"No movies match" in client.get("/search?query=zzz").data


def test_search_text_is_cleaned_and_escaped(client, monkeypatch):
    page = client.get('/search?query="><script>alert(1)</script>').data
    assert b"<script>alert(1)" not in page
    assert b"&lt;script&gt;" in page
    seen = []
    monkeypatch.setattr(appmod, "tmdb", lambda path, **params: seen.append(params.get("query")) or fake_tmdb(path, **params))
    client.get("/search?query=" + "a" * 500)
    client.get("/search?query=ab%00c%e2%80%aed")
    assert len(seen[0]) == 100
    assert seen[1] == "abcd"          # NUL and the right-to-left override are gone


def test_missing_and_huge_movie_ids_are_404(client):
    assert client.get("/movie/999").status_code == 404
    assert client.get("/movie/99999999999999999999").status_code == 404


def test_page_links_ignore_special_url_arguments(client):
    page = client.get("/?_scheme=http&_external=1&_anchor=x&category=top_rated&page=1").data.decode()
    assert 'rel="next"' in page
    next_link = page.split('rel="next" href="')[1].split('"')[0]
    assert next_link == "/?category=top_rated&amp;page=2"             # a relative link with only known filters


def test_missing_page_is_a_friendly_404(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404 and b"Page not found" in r.data


# --------------------------------------------------------------------------
# Accounts: sign up, log in, log out
# --------------------------------------------------------------------------

def test_register_logs_you_in_and_hashes_the_password(client, db_path):
    signup(client, "asha")
    page = client.get("/").data
    assert b"asha" in page and b"Log out" in page
    con = sqlite3.connect(db_path)
    stored = con.execute("SELECT password_hash FROM users").fetchone()[0]
    con.close()
    assert stored.startswith("scrypt:") and PASSWORD not in stored
    assert PASSWORD.encode() not in open(db_path, "rb").read()


def test_register_rejects_bad_input(client):
    cases = [
        ("ab", PASSWORD, PASSWORD),                       # too short
        ("has space", PASSWORD, PASSWORD),
        ("<b>x</b>", PASSWORD, PASSWORD),
        ("Admin", PASSWORD, PASSWORD),                    # reserved
        ("bob", "short", "short"),
        ("bob", "password123", "password123"),            # too common
        ("bob", "aaaaaaaaaaaa", "aaaaaaaaaaaa"),          # no variety
        ("bob", "bobbobbob12345", "bobbobbob12345"),      # contains the username
        ("bob", PASSWORD, PASSWORD + "x"),                # confirmation differs
        ("bob", "x" * 129, "x" * 129),                    # absurdly long
    ]
    for name, password, confirm in cases:
        r = post(client, "/register", username=name, password=password, confirm=confirm)
        assert r.status_code == 400, (name, password)
    assert count("users") == 0


def test_usernames_are_unique_ignoring_case(client):
    signup(client, "Asha")
    other = make_client()
    r = post(other, "/register", username="aSHA", password=PASSWORD, confirm=PASSWORD)
    assert r.status_code == 400 and b"taken" in r.data
    assert count("users") == 1


def test_login_and_logout(client):
    signup(client, "asha")
    post(client, "/logout")
    assert client.get("/watchlist").status_code == 302      # logged out again
    assert client.get("/logout").status_code == 405         # logging out is a POST, so it needs a CSRF token
    r = login(client)
    assert r.status_code == 302
    assert client.get("/watchlist").status_code == 200


def test_wrong_password_and_unknown_user_look_identical(client):
    signup(client, "asha")
    other = make_client()
    wrong = login(other, "asha", "not the password at all")
    unknown = login(other, "nobody", "not the password at all")
    assert wrong.status_code == unknown.status_code == 401
    assert b"Wrong username or password." in wrong.data
    assert b"Wrong username or password." in unknown.data


def test_unknown_user_still_costs_a_password_check(client, monkeypatch):
    calls = []
    real = appmod.check_password_hash
    monkeypatch.setattr(appmod, "check_password_hash", lambda h, p: calls.append(h) or real(h, p))
    login(client, "nobody", "whatever password")
    assert calls == [appmod._DUMMY_HASH]


def test_sql_injection_in_login_does_nothing(client):
    signup(client, "asha")
    other = make_client()
    for attempt in ("asha' --", "' OR '1'='1", "asha\"; DROP TABLE users; --"):
        r = login(other, attempt, "x' OR '1'='1")
        assert r.status_code == 401
    assert other.get("/watchlist").status_code == 302
    assert count("users") == 1


def test_login_is_rate_limited_per_address_and_per_account(client):
    signup(client, "asha")
    other = make_client()
    for _ in range(10):
        assert login(other, "asha", "wrong password here").status_code == 401
    assert login(other, "asha", "wrong password here").status_code == 429

    # Guessing from many addresses is still slowed down, because the account itself is limited.
    appmod.limiter.clear()
    for i in range(10):
        r = other.post("/login", environ_overrides={"REMOTE_ADDR": f"10.0.0.{i}"},
                       data={"csrf_token": csrf(other), "username": "asha", "password": "wrong password here"})
        assert r.status_code == 401
    r = other.post("/login", environ_overrides={"REMOTE_ADDR": "10.0.1.1"},
                   data={"csrf_token": csrf(other), "username": "asha", "password": PASSWORD})
    assert r.status_code == 429


def test_registration_is_rate_limited(client):
    for i in range(10):
        r = post(client, "/register", username="ab", password="x", confirm="x")   # invalid, still counts
        assert r.status_code == 400
    assert post(client, "/register", username="ab", password="x", confirm="x").status_code == 429


def test_login_starts_a_fresh_session(client):
    signup(client, "asha")
    other = make_client()
    before = csrf(other)
    login(other)
    assert csrf(other) != before                            # the pre-login token is gone
    stale = other.post("/watchlist/add", data={"csrf_token": before, "movie_id": 1})
    assert stale.status_code == 400


def test_next_only_goes_to_pages_on_this_site(client):
    signup(client, "asha")
    for target, expected in (("/watchlist", "/watchlist"), ("//evil.example", "/"),
                             ("https://evil.example", "/"), ("/\\evil.example", "/"),
                             ("javascript:alert(1)", "/")):
        other = make_client()
        r = login(other, next=target)
        assert location(r).endswith(expected) and "evil" not in location(r), target
    anon = make_client()
    assert location(anon.get("/watchlist")) == "/login?next=/watchlist"


def test_forms_only_redirect_back_to_pages_on_this_site(asha):
    for evil in ("//evil.example", "https://evil.example/x", "/\\evil.example", "javascript:alert(1)", "/ok\r\nX-Injected: 1",
                 "/%2F%2Fevil.example", "/%252F%252Fevil.example", "/%5Cevil.example"):
        r = post(asha, "/watchlist/add", movie_id=1, next=evil)
        assert location(r).endswith("/watchlist") and "evil" not in location(r), evil
    r = post(asha, "/watchlist/remove", movie_id=1, next="/movie/1?x=1#reviews")
    assert location(r) == "/movie/1?x=1#reviews"


def test_account_pages_are_never_cached(client):
    assert client.get("/login").headers["Cache-Control"] == "no-store"
    signup(client, "asha")
    assert client.get("/").headers["Cache-Control"] == "no-store"
    assert client.get("/account").headers["Cache-Control"] == "no-store"


def test_change_password_logs_out_other_devices(client):
    signup(client, "asha")
    phone = make_client()
    login(phone)
    assert phone.get("/account").status_code == 200

    new = "a completely different phrase"
    r = post(client, "/account/password", current="wrong", password=new, confirm=new)
    assert b"current password is wrong" in client.get(location(r)).data
    post(client, "/account/password", current=PASSWORD, password=new, confirm=new)

    assert client.get("/account").status_code == 200        # this device stays logged in
    assert phone.get("/account").status_code == 302        # the other one is logged out
    assert login(make_client(), password=PASSWORD).status_code == 401
    assert login(make_client(), password=new).status_code == 302


def test_delete_account_removes_only_that_users_data(client):
    signup(client, "asha")
    bob = make_client()
    signup(bob, "bob")
    for c in (client, bob):
        post(c, "/watchlist/add", movie_id=1)
        post(c, "/review", movie_id=1, rating=4, review="Nice one")

    post(client, "/account/delete", current="wrong password")
    assert count("users") == 2
    post(client, "/account/delete", current=PASSWORD)
    assert count("users") == 1
    assert count("watchlist") == 1 and count("reviews") == 1
    assert count("reviews", "username = 'bob'") == 1
    assert client.get("/watchlist").status_code == 302
    assert login(make_client()).status_code == 401


# --------------------------------------------------------------------------
# People only reach their own data
# --------------------------------------------------------------------------

def test_login_is_required_for_personal_pages_and_actions(client):
    for url in ("/watchlist", "/account"):
        assert client.get(url).status_code == 302 and "/login" in location(client.get(url))
    for url, data in (("/watchlist/add", {"movie_id": 1}), ("/watchlist/remove", {"movie_id": 1}),
                      ("/watchlist/watched", {"movie_id": 1}), ("/review", {"movie_id": 1, "rating": 5, "review": "x"}),
                      ("/review/1/delete", {}), ("/account/password", {}), ("/account/delete", {})):
        r = post(client, url, **data)
        assert r.status_code == 302 and "/login" in location(r), url
    assert count("watchlist") == 0 and count("reviews") == 0


def test_watchlists_are_private(client):
    signup(client, "asha")
    bob = make_client()
    signup(bob, "bob")
    post(client, "/watchlist/add", movie_id=1)
    post(bob, "/watchlist/add", movie_id=2)

    mine, theirs = client.get("/watchlist").data, bob.get("/watchlist").data
    assert b">Test Movie</a>" in mine and b">Other</a>" not in mine
    assert b">Other</a>" in theirs and b">Test Movie</a>" not in theirs

    post(bob, "/watchlist/add", movie_id=1)               # the same movie can be on both lists
    assert count("watchlist") == 3
    post(bob, "/watchlist/watched", movie_id=1)
    assert count("watchlist", "watched = 1") == 1
    post(bob, "/watchlist/remove", movie_id=1)
    assert count("watchlist") == 2
    assert b">Test Movie</a>" in client.get("/watchlist").data


def test_buttons_and_counts_follow_the_account(client):
    signup(client, "asha")
    bob = make_client()
    signup(bob, "bob")
    post(client, "/watchlist/add", movie_id=1)
    assert b'class="count"' in client.get("/").data
    assert b'class="count"' not in bob.get("/").data
    assert b"Remove from watchlist" in client.get("/movie/1").data
    assert b"Add to watchlist" in bob.get("/movie/1").data


def test_you_cannot_delete_someone_elses_review(client):
    signup(client, "asha")
    post(client, "/review", movie_id=1, rating=5, review="Mine")
    review_id = sqlite3.connect(appmod.app.config["DATABASE"]).execute("SELECT id FROM reviews").fetchone()[0]

    bob = make_client()
    signup(bob, "bob")
    assert b"Delete my review" not in bob.get("/movie/1").data
    assert post(bob, f"/review/{review_id}/delete").status_code == 404
    assert count("reviews") == 1

    anon = make_client()
    assert "/login" in location(post(anon, f"/review/{review_id}/delete"))
    assert count("reviews") == 1

    assert b"Delete my review" in client.get("/movie/1").data
    post(client, f"/review/{review_id}/delete")
    assert count("reviews") == 0


def test_reviews_use_the_account_name_and_one_per_movie(asha):
    post(asha, "/review", movie_id=1, rating=5, review="Great", username="someone else", user_id=99)
    assert count("reviews", "username = 'asha'") == 1
    r = post(asha, "/review", movie_id=1, rating=1, review="Changed my mind")
    assert b"already reviewed" in asha.get(location(r)).data
    assert count("reviews") == 1
    post(asha, "/review", movie_id=2, rating=3, review="Fine")
    assert count("reviews") == 2


def test_review_validation_and_delete(asha):
    post(asha, "/review", movie_id=1, review="No stars")
    post(asha, "/review", movie_id=1, rating=9, review="Nine stars")
    post(asha, "/review", movie_id=1, rating=5, review="   ")
    assert count("reviews") == 0
    post(asha, "/review", movie_id=1, rating=5, review="<b>Great</b>")
    page = asha.get("/movie/1").data
    assert b"&lt;b&gt;Great" in page and b"<b>Great</b>" not in page
    post(asha, "/review/1/delete")
    assert count("reviews") == 0


def test_bad_movie_ids_are_refused(asha):
    for bad in ("0", "-5", "abc", "99999999999999999999", ""):
        assert post(asha, "/watchlist/add", movie_id=bad).status_code == 400, bad
    assert post(asha, "/watchlist/add", movie_id=999).status_code == 404
    assert count("watchlist") == 0


# --------------------------------------------------------------------------
# Forms: cleaning, XSS, CSRF, size limits
# --------------------------------------------------------------------------

def test_clean_text():
    assert appmod.clean_text("  a\x00b\u202ec  d ") == "abc d"
    assert appmod.clean_text("one\ntwo\tthree") == "one two three"
    assert appmod.clean_text("one\r\n\r\n\r\n\r\ntwo  \n three", multiline=True) == "one\n\ntwo\nthree"
    assert appmod.clean_text("caf\u0065\u0301 🎬") == "café 🎬"      # accents combine, emoji survive
    assert appmod.clean_text(None) == ""


def test_review_text_is_cleaned_before_it_is_saved(asha):
    post(asha, "/review", movie_id=1, rating=4, review="Good\x00 film\u202e!\r\n\r\n\r\n\r\nReally.")
    con = sqlite3.connect(appmod.app.config["DATABASE"])
    saved = con.execute("SELECT review FROM reviews").fetchone()[0]
    con.close()
    assert saved == "Good film!\n\nReally."


def test_text_from_older_versions_and_from_tmdb_is_escaped(client, monkeypatch):
    sql("INSERT INTO reviews (movie_id, username, review, rating) VALUES (1, '<img src=x onerror=alert(1)>', '<script>alert(2)</script>', 3)")
    evil = copy.deepcopy(MOVIE)
    evil.update(title="<script>alert(3)</script>", overview="<img src=x onerror=alert(4)>", tagline="</p><b>",
                credits={"cast": [{"name": "<i>Actor</i>", "character": "\"><svg onload=alert(5)>", "profile_path": None}], "crew": []})
    monkeypatch.setattr(appmod, "tmdb", lambda path, **p: evil if path == "/movie/1" else fake_tmdb(path, **p))
    page = client.get("/movie/1").data
    for needle in (b"<script>alert", b"<img src=x", b"<svg onload", b"<i>Actor", b"</p><b>"):
        assert needle not in page, needle
    assert b"&lt;script&gt;alert(2)" in page and b"&lt;img src=x onerror" in page


def test_image_and_trailer_values_from_tmdb_must_look_right(client, monkeypatch):
    assert appmod.img("/abc_DEF-1.jpg") == "https://image.tmdb.org/t/p/w500/abc_DEF-1.jpg"
    for bad in ("/a.jpg')}</style><script>", "https://evil.example/a.jpg", "//evil/a.jpg", "/a b.jpg",
                "/a.svg", "/../x.jpg", "", None, 5):
        assert appmod.img(bad) is None, bad
    evil = copy.deepcopy(MOVIE)
    evil.update(poster_path="/x.jpg\"><script>alert(1)</script>", backdrop_path="/y.jpg' onerror='alert(1)",
                videos={"results": [{"site": "YouTube", "type": "Trailer", "official": True, "key": "abc\"><script>"}]})
    monkeypatch.setattr(appmod, "tmdb", lambda path, **p: evil if path == "/movie/1" else fake_tmdb(path, **p))
    page = client.get("/movie/1").data
    assert b"<script>" not in page and b"onerror" not in page
    assert b'id="trailer"' not in page


def test_csrf_is_required_everywhere(asha):
    for url in ("/watchlist/add", "/watchlist/remove", "/watchlist/watched", "/review", "/review/1/delete",
                "/account/password", "/account/delete", "/logout"):
        assert asha.post(url, data={"movie_id": 1}).status_code == 400, url
        assert asha.post(url, data={"movie_id": 1, "csrf_token": "wrong"}).status_code == 400, url
    anon = make_client()
    assert anon.post("/login", data={"username": "asha", "password": PASSWORD}).status_code == 400
    assert anon.post("/register", data={"username": "x1y", "password": PASSWORD}).status_code == 400


def test_oversized_forms_are_refused(asha):
    r = post(asha, "/review", movie_id=1, rating=5, review="x" * 40_000)
    assert r.status_code == 413
    assert count("reviews") == 0


def test_honeypot_field_blocks_bots(asha):
    r = post(asha, "/review", movie_id=1, rating=5, review="Buy now", website="spam.example")
    assert r.status_code == 302
    assert count("reviews") == 0


# --------------------------------------------------------------------------
# Rate limits
# --------------------------------------------------------------------------

def test_reviews_are_rate_limited_per_account(asha):
    for movie_id in range(1, 6):
        assert post(asha, "/review", movie_id=movie_id, rating=4, review="Fine").status_code == 302
    assert post(asha, "/review", movie_id=6, rating=4, review="One too many").status_code == 429
    assert count("reviews") == 5


def test_watchlist_writes_are_rate_limited(asha):
    for _ in range(60):
        assert post(asha, "/watchlist/add", movie_id=1).status_code == 302
    assert post(asha, "/watchlist/add", movie_id=1).status_code == 429


def test_every_visitor_has_an_overall_request_limit(client, monkeypatch):
    monkeypatch.setattr(appmod, "GLOBAL_LIMIT", (3, 60))
    for _ in range(3):
        assert client.get("/login").status_code == 200
    assert client.get("/login").status_code == 429
    assert client.get("/static/styles.css").status_code == 200          # files aren't counted
    other = client.get("/login", environ_overrides={"REMOTE_ADDR": "10.9.9.9"})
    assert other.status_code == 200                                      # other visitors are unaffected


# --------------------------------------------------------------------------
# Start-up safety, cookies, headers, CORS, exposed files, routes
# --------------------------------------------------------------------------

def test_secret_key_rules(monkeypatch):
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    for bad in (None, "", "short", "x" * 31, "dev-only-change-me", "change-me"):
        if bad is None:
            monkeypatch.delenv("SECRET_KEY", raising=False)
        else:
            monkeypatch.setenv("SECRET_KEY", bad)
        with pytest.raises(RuntimeError) as err:
            appmod.secret_key()
        assert "SECRET_KEY" in str(err.value), bad
    monkeypatch.setenv("SECRET_KEY", "k" * 32)
    assert appmod.secret_key() == "k" * 32
    monkeypatch.delenv("SECRET_KEY")
    monkeypatch.setenv("FLASK_DEBUG", "1")
    assert appmod.secret_key() == "dev-only-change-me"


def test_debug_mode_is_refused_on_a_proxied_host(monkeypatch):
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("TRUST_PROXY", "1")
    with pytest.raises(RuntimeError) as err:
        appmod.check_runtime_safety()
    assert "Debug mode" in str(err.value)
    monkeypatch.delenv("TRUST_PROXY")
    appmod.check_runtime_safety()                                       # plain local debugging is fine
    monkeypatch.setenv("FLASK_DEBUG", "0")
    monkeypatch.setenv("TRUST_PROXY", "1")
    appmod.check_runtime_safety()                                       # production is fine


def test_debug_is_off_by_default():
    assert appmod.app.debug is False
    assert appmod.SECURE_COOKIES is True


def test_session_cookie_flags(client, monkeypatch):
    cookie = client.get("/login").headers["Set-Cookie"]
    assert cookie.startswith("__Host-filmyqui=")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Expires=" in cookie
    assert "Domain" not in cookie and "Path=/" in cookie
    monkeypatch.setitem(appmod.app.config, "SESSION_COOKIE_SECURE", True)
    assert "Secure" in make_client().get("/login", base_url="https://localhost").headers["Set-Cookie"]


def test_security_headers(client):
    h = client.get("/").headers
    csp = h["Content-Security-Policy"]
    for needed in ("default-src 'self'", "script-src 'self'", "object-src 'none'", "frame-ancestors 'none'",
                   "form-action 'self'", "base-uri 'self'"):
        assert needed in csp, needed
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert "camera=()" in h["Permissions-Policy"]
    assert h["Cross-Origin-Opener-Policy"] == "same-origin"
    assert h["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "Strict-Transport-Security" not in h                         # only over https
    secure = client.get("/", base_url="https://localhost").headers
    assert "max-age=" in secure["Strict-Transport-Security"]
    assert "upgrade-insecure-requests" in secure["Content-Security-Policy"]


def test_pages_use_no_inline_scripts_or_styles(client):
    page = client.get("/movie/1").data.decode()
    assert " style=" not in page and "<style" not in page and "onclick=" not in page
    assert "<script>" not in page.replace('<script src=', '')


def test_cors_is_never_enabled(client):
    for response in (client.get("/", headers={"Origin": "https://evil.example"}),
                     client.options("/", headers={"Origin": "https://evil.example",
                                                  "Access-Control-Request-Method": "POST"}),
                     client.get("/static/app.js", headers={"Origin": "https://evil.example"})):
        assert not any(h.lower().startswith("access-control-") for h in response.headers.keys())


def test_unexpected_host_headers_are_refused(client, monkeypatch):
    monkeypatch.setitem(appmod.app.config, "TRUSTED_HOSTS", ["filmyqui.example"])
    bad = client.get("/login", headers={"Host": "evil.example"})
    assert bad.status_code == 400 and b"not allowed" in bad.data
    assert "X-Content-Type-Options" in bad.headers
    assert client.get("/login", headers={"Host": "filmyqui.example"}).status_code == 200


def test_project_files_are_not_served(client):
    for path in ("/.env", "/app.py", "/movies.db", "/requirements.txt", "/tests/test_app.py", "/.git/config",
                 "/static/../app.py", "/static/..%2fapp.py", "/static/%2e%2e/app.py", "/static/", "/templates/layout.html"):
        assert client.get(path).status_code in (400, 404), path
    assert client.get("/static/styles.css").status_code == 200


def test_the_site_has_only_the_routes_we_expect():
    """If someone adds an admin or debug route by accident, this fails and makes them look at it."""
    routes = {(r.rule, tuple(sorted(r.methods - {"HEAD", "OPTIONS"}))) for r in appmod.app.url_map.iter_rules()}
    assert routes == {
        ("/", ("GET",)), ("/search", ("GET",)), ("/movie/<int:movie_id>", ("GET",)),
        ("/register", ("GET", "POST")), ("/login", ("GET", "POST")), ("/logout", ("POST",)),
        ("/account", ("GET",)), ("/account/password", ("POST",)), ("/account/delete", ("POST",)),
        ("/watchlist", ("GET",)), ("/watchlist/add", ("POST",)), ("/watchlist/remove", ("POST",)),
        ("/watchlist/watched", ("POST",)), ("/review", ("POST",)), ("/review/<int:review_id>/delete", ("POST",)),
        ("/static/<path:filename>", ("GET",)),
    }


def test_security_events_are_logged_without_passwords(client):
    lines = []

    class Catch(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Catch()
    appmod.security_log.addHandler(handler)
    appmod.security_log.setLevel(logging.INFO)
    try:
        signup(client, "asha")
        login(make_client(), "asha\nFAKE LOG LINE", "very secret phrase")
        login(make_client(), "asha", "another wrong phrase")
    finally:
        appmod.security_log.removeHandler(handler)
    text = "\n".join(lines)
    assert "register ok" in text and "login failed" in text
    assert "very secret phrase" not in text and "another wrong phrase" not in text and PASSWORD not in text
    assert "\nFAKE LOG LINE" not in text                                 # no forged log lines


# --------------------------------------------------------------------------
# The database
# --------------------------------------------------------------------------

def test_database_file_is_private_and_enforces_relationships(db_path):
    if os.name == "posix":
        assert oct(os.stat(db_path).st_mode & 0o777) == "0o600"
    with appmod.app.app_context():
        assert appmod.get_db().execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            appmod.get_db().execute("INSERT INTO watchlist (user_id, movie_id, title) VALUES (999, 1, 'x')")


def test_the_database_folder_is_created_when_missing(tmp_path):
    path = str(tmp_path / "data" / "nested" / "movies.db")
    appmod.init_db(path)
    assert os.path.exists(path)
    if os.name == "posix":
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_the_env_file_is_read_from_next_to_app_py(tmp_path):
    """Hosts such as PythonAnywhere start the app from a different working directory."""
    import subprocess, sys
    app_dir = os.path.dirname(os.path.abspath(appmod.__file__))
    env_file = os.path.join(app_dir, ".env")
    assert not os.path.exists(env_file), "remove your local .env before running this test"
    try:
        with open(env_file, "w") as f:
            f.write("SECRET_KEY=" + "e" * 40 + "\nDATABASE_PATH=" + str(tmp_path / "from_env.db") + "\n")
        env = {k: v for k, v in os.environ.items() if k not in ("SECRET_KEY", "DATABASE_PATH", "FLASK_DEBUG")}
        out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import app; print(app.app.config['SECRET_KEY'][:4], app.app.config['DATABASE'])" % app_dir],
                             cwd=str(tmp_path), env=env, capture_output=True, text=True)
    finally:
        os.remove(env_file)
    assert out.returncode == 0, out.stderr[-300:]
    assert out.stdout.split()[0] == "eeee" and out.stdout.split()[1].endswith("from_env.db")


def test_database_checks_ratings_and_review_length(db_path):
    with pytest.raises(sqlite3.IntegrityError):
        sql("INSERT INTO reviews (movie_id, username, review, rating) VALUES (1, 'a', 'x', 9)")
    with pytest.raises(sqlite3.IntegrityError):
        sql("INSERT INTO reviews (movie_id, username, review, rating) VALUES (1, 'a', ?, 3)", ("x" * 2001,))


def test_old_databases_are_upgraded_without_losing_reviews(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE watchlist (id INTEGER PRIMARY KEY AUTOINCREMENT, movie_id INTEGER NOT NULL,
            title TEXT, poster_path TEXT, watched INTEGER NOT NULL DEFAULT 0, added_at TEXT, owner TEXT);
        CREATE UNIQUE INDEX idx_watchlist_movie ON watchlist(movie_id);
        CREATE UNIQUE INDEX idx_watchlist_owner_movie ON watchlist(owner, movie_id);
        CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, movie_id INTEGER NOT NULL,
            username TEXT NOT NULL, review TEXT NOT NULL, rating INTEGER, created_at TEXT);
        INSERT INTO watchlist (movie_id, title, owner) VALUES (1, 'Old', 'anon');
        INSERT INTO reviews (movie_id, username, review, rating) VALUES (1, 'Old Timer', 'From before accounts', 4);
    """)
    con.commit()
    con.close()
    appmod.app.config.update(DATABASE=path, API_KEY="x", SESSION_COOKIE_SECURE=False)
    appmod.init_db(path)
    appmod.init_db(path)                                                 # running it again is harmless

    monkeypatch.setattr(appmod, "tmdb", fake_tmdb)
    a, b = make_client(), make_client()
    signup(a, "asha")
    signup(b, "bob")
    post(a, "/watchlist/add", movie_id=1)
    post(b, "/watchlist/add", movie_id=1)                                # the old one-per-movie index is gone
    assert count("watchlist", "user_id IS NOT NULL") == 2
    assert b"Old" not in a.get("/watchlist").data                        # rows nobody owns are never shown
    page = a.get("/movie/1").data
    assert b"Old Timer" in page and b"From before accounts" in page
    assert b"Delete my review" not in page                               # nobody can delete them either


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


def test_tmdb_can_use_a_bearer_token_so_the_key_stays_out_of_urls(wire, monkeypatch):
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


def test_the_api_key_never_reaches_visitors(client, wire, monkeypatch):
    monkeypatch.setattr(appmod, "tmdb", REAL_TMDB)
    monkeypatch.setitem(appmod.app.config, "API_KEY", "testkey")
    wire.script = [requests.ConnectionError("HTTPSConnectionPool: /genre/movie/list?api_key=testkey")] * 3
    page = client.get("/")
    assert page.status_code == 502 and b"testkey" not in page.data
    for header, value in page.headers.items():
        assert "testkey" not in value


def test_tmdb_handles_invalid_json(wire):
    response = FakeResponse(200)

    def bad_json():
        raise ValueError("bad json")

    response.json = bad_json
    wire.script = [response]
    with pytest.raises(appmod.TMDBError) as err:
        appmod.tmdb("/x")
    assert "unreadable" in str(err.value)


def test_odd_values_from_tmdb_do_not_crash_the_helpers():
    assert appmod.runtime("abc") == "" and appmod.runtime(None) == "" and appmod.runtime(125) == "2h 5m"
    assert appmod.total_pages({"total_pages": None}) == 1
    assert appmod.total_pages({"total_pages": "x"}) == 1
    assert appmod.total_pages({"total_pages": 99999}) == appmod.MAX_PAGE


def test_the_old_api_key_variable_name_still_works(monkeypatch):
    import subprocess, sys
    env = {k: v for k, v in os.environ.items() if k not in ("API_KEY", "TMDB_API_KEY")}
    env["TMDB_API_KEY"] = "legacy-name-key"
    code = "import sys; sys.path.insert(0, %r); import app; print(app.app.config['API_KEY'])" % os.path.dirname(os.path.abspath(appmod.__file__))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert out.stdout.strip() == "legacy-name-key", out.stderr[-300:]


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
