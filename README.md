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
- **Watchlist**: add or remove from any page, and mark movies as watched. Watched movies move to their own section. Each entry is styled as a ticket stub. Your list is tied to your browser, so other visitors never see or change it.
- **Reviews**: star rating, name and text. The movie page shows the average rating from FilmyQui reviews and the latest 50 reviews. You can delete reviews you wrote from the same browser (no accounts needed).

## How it works

- `app.py` holds the routes, the TMDB client, the database helpers, the rate limiter and error handlers.
- `templates/` has the Jinja pages (`layout.html`, `index.html`, `movie.html`, `search.html`, `watchlist.html`, `error.html`) and `_macros.html` for the reusable movie card, pagination, stars and watchlist button.
- `static/styles.css` is all of the styling and `static/app.js` loads the trailer on click.
- `movies.db` is the SQLite database with two tables, `watchlist` and `reviews`. `init_db()` creates the tables and upgrades older copies of the database on start (including older copies that had one shared watchlist).
- `tests/test_app.py` has pytest tests that use a fake TMDB, so they run without internet or an API key. A second group of tests checks the TMDB client itself (retries, errors, caching) by faking the network call. `.github/workflows/tests.yml` runs them on every push.

## Design decisions

- **One TMDB call per movie page.** TMDB's `append_to_response` returns details, videos, cast and recommendations together. The old version made three requests.
- **A small TMDB wrapper.** Every call has a timeout, friendly error messages (missing key, bad key, no connection), a reused connection, a retry with a short wait when TMDB says 429 or is briefly down, and a 10-minute in-memory cache that drops its oldest entries when full. With several gunicorn workers each one keeps its own cache, which is fine for public movie data.
- **Safer forms.** Anything that changes data is a POST with a CSRF token. The watchlist stores the title and poster from TMDB rather than trusting form fields, and reviews are validated on the server. Redirects only go to pages on this site.
- **No login, on purpose.** I left out authentication to focus on the movie experience. Each browser gets a random id in its session cookie, which is what keeps your watchlist private and lets you delete the reviews you wrote. The cookie lasts a year, is HTTP-only and SameSite=Lax, and is Secure outside debug mode. The trade-off is that clearing cookies or switching device starts you with an empty watchlist; accounts would fix that.
- **Spam and abuse.** Writing routes are rate limited per visitor (5 reviews per 10 minutes, 60 other changes per minute), the review form has a hidden honeypot field, and every response carries a Content-Security-Policy and other security headers. The limiter is in memory and per worker, so it slows down casual abuse but is not a substitute for a shared limiter such as Redis if the site gets real traffic.
- **No default secret.** The app refuses to start without `SECRET_KEY` unless you run with `--debug`, because a guessable key would let anyone forge sessions.
- **A cinema look.** Deep wine background, marquee gold accent, Bricolage Grotesque for headings and Figtree for text. The watchlist ticket stubs are the main visual idea. The layout works on phones, has visible keyboard focus and respects reduced-motion settings.

## Run it

1. `pip install -r requirements.txt` (use `requirements-dev.txt` if you also want to run the tests)
2. Get a free API key from themoviedb.org, then copy `.env.example` to `.env` and set `API_KEY`.
3. `flask --app app run --debug` (in debug mode a missing `SECRET_KEY` is allowed; otherwise set one in `.env`)
4. Run the tests with `pytest`.

## Deploy it

1. Start command: `gunicorn app:app --workers 2`
2. Set these environment variables on your host: `API_KEY`, `SECRET_KEY` (a long random value), `TRUST_PROXY=1` (so rate limiting sees the real visitor IP), and `DATABASE_PATH` pointing at a persistent disk, for example `/var/data/movies.db`. Free hosts often wipe the normal disk on every deploy, which would delete all reviews and watchlists.
3. Serve it over https. The session cookie is only sent over https outside debug mode.

## Ideas for later

User accounts (so a watchlist follows you across devices), personal recommendations from your reviews, combined genre, year and language filters, where-to-watch information, sorting the watchlist, self-hosting the fonts, and moving from SQLite to PostgreSQL.
