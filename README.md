# FilmyQui

#### Video Demo: https://youtu.be/61XkvAn5n8w?si=ax6pfGaNNtgYaGNO

#### Description:

FilmyQui is a movie discovery web app built with Flask, Python, SQLite, Jinja, HTML, CSS and a little JavaScript, using data from The Movie Database (TMDB) API. You can browse popular, top rated, in-theatre and upcoming movies, filter by genre, search by title, watch trailers, see the cast, keep a watchlist, and write star-rated reviews. It is my final project for CS50x.

I picked cinema because it is one of my biggest interests, and I wanted a dynamic app that talks to a real API and stores its own data instead of a static page.

## Features

- **Home page**: a featured movie banner, tabs for Popular / Top rated / In theatres / Coming soon, genre chips, and pagination.
- **Search**: results by title with a result count, pagination, and a helpful empty state.
- **Movie page**: poster, tagline, year, runtime, TMDB rating, genres (each links to that genre), overview, director, cast, an official trailer, and recommendations.
- **Trailer**: shows a thumbnail first and only loads the YouTube player when you click, so the page opens fast.
- **Watchlist**: add or remove from any page, and mark movies as watched. Watched movies move to their own section. Each entry is styled as a ticket stub.
- **Reviews**: star rating, name and text. The movie page shows the average rating from FilmyQui reviews. You can delete reviews you wrote from the same browser (no accounts needed).

## How it works

- `app.py` holds the routes, the TMDB client, the database helpers and error handlers.
- `templates/` has the Jinja pages (`layout.html`, `index.html`, `movie.html`, `search.html`, `watchlist.html`, `error.html`) and `_macros.html` for the reusable movie card, pagination, stars and watchlist button.
- `static/styles.css` is all of the styling and `static/app.js` loads the trailer on click.
- `movies.db` is the SQLite database with two tables, `watchlist` and `reviews`. `init_db()` creates the tables and upgrades older copies of the database on start.
- `tests/test_app.py` has pytest tests that use a fake TMDB, so they run without internet or an API key.

## Design decisions

- **One TMDB call per movie page.** TMDB's `append_to_response` returns details, videos, cast and recommendations together. The old version made three requests.
- **A small TMDB wrapper.** Every call has a timeout, friendly error messages (missing key, bad key, no connection), and a 10-minute in-memory cache.
- **Safer forms.** Anything that changes data is a POST with a CSRF token. The watchlist stores the title and poster from TMDB rather than trusting form fields, and reviews are validated on the server. Redirects only go to pages on this site.
- **No login, on purpose.** I left out authentication to focus on the movie experience. Reviews you wrote are remembered in your browser session so you can delete them.
- **A cinema look.** Deep wine background, marquee gold accent, Bricolage Grotesque for headings and Figtree for text. The watchlist ticket stubs are the main visual idea. The layout works on phones, has visible keyboard focus and respects reduced-motion settings.

## Run it

1. `pip install -r requirements.txt`
2. Get a free API key from themoviedb.org, then copy `.env.example` to `.env` and set `API_KEY`.
3. `flask --app app run --debug`
4. Run the tests with `pytest`.

## Ideas for later

User accounts, personal recommendations from your reviews, sorting and filtering the watchlist, and deployment (`gunicorn app:app`).
