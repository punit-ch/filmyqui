import pytest

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


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    appmod.app.config.update(TESTING=True, DATABASE=db, API_KEY="x")
    appmod.init_db(db)
    monkeypatch.setattr(appmod, "tmdb", fake_tmdb)
    with appmod.app.test_client() as c:
        with c.session_transaction() as s:
            s["csrf"] = "tok"
        yield c


def post(client, url, **data):
    return client.post(url, data={"csrf_token": "tok", **data})


def count(table):
    return len(appmod.sqlite3.connect(appmod.app.config["DATABASE"]).execute(f"SELECT * FROM {table}").fetchall())


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


def test_review_validation_and_delete(client):
    post(client, "/review", movie_id=1, username="A", review="Great", rating=9)
    assert count("reviews") == 0  # rating 9 is rejected
    r = post(client, "/review", movie_id=1, username="Asha", review="<b>Great</b>", rating=5)
    assert count("reviews") == 1
    page = client.get("/movie/1").data
    assert b"&lt;b&gt;Great" in page          # escaped, not rendered
    assert b"Delete my review" in page
    post(client, "/review/1/delete")
    assert count("reviews") == 0


def test_cannot_delete_someone_elses_review(client):
    con = appmod.sqlite3.connect(appmod.app.config["DATABASE"])
    con.execute("INSERT INTO reviews (movie_id, username, review) VALUES (1, 'x', 'y')")
    con.commit()
    assert post(client, "/review/1/delete").status_code == 403
