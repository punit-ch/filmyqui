# FilmyQui security audit

Date: 26 November 2026. Scope: the code in this repository (`app.py`, templates, static files, tests, CI) and its public Git history.

This is a careful review plus automated checks, not a professional penetration test, and nobody can promise "no mistakes". It lists what was checked, what was found and fixed, and what is still your job.

## How it was checked

- Line-by-line review of `app.py`, every template, `app.js` and the CSS.
- 63 automated tests. For the main protections I also broke the code on purpose (20 deliberate bugs, such as a delete query that forgets the owner) and confirmed the tests fail. One was missed at first (redirect checking) and now has a test.
- A live run of the app: start-up guards, sign-up, headers, file exposure, database file permissions.
- A real Chromium browser run for layout and to confirm the Content-Security-Policy blocks nothing the site needs (no CSP violations).
- Your Git history, read through the GitHub API: all 9 commits and the files each one touched. The text files those commits added were never changed later, so I scanned the matching files in your zip for keys, tokens and private keys (none found). The two binary files could not be opened (see the Git history notes).

## Your checklist

| Item | Result |
|---|---|
| Hide API keys | Keys only come from environment variables. No key is in the code or in any commit. The key is never shown in a page, error message or header (tested with a fake key). New `TMDB_READ_TOKEN` option sends the credential in a header instead of a URL. |
| Check environment variables | App refuses to start without a `SECRET_KEY` of 32+ random characters (placeholders rejected). `.env` and `.env.*` are git-ignored; only `.env.example` is committed. `ALLOWED_HOSTS` rejects unexpected Host headers. |
| Protect admin routes | There are no admin routes. A test lists every route in the app and fails if one is added, so a forgotten admin or debug page cannot appear silently. |
| Proper authentication | Added accounts: scrypt password hashing, fresh session on login, generic login errors, constant-time check for unknown users, per-address and per-account rate limits, session-wide logout when the password changes, account deletion. |
| Users only access their own data | Every watchlist and review query is filtered by user id in SQL. Deleting another person's review returns "not found". Tests check this with two accounts. |
| Sanitize forms | Server-side cleaning (Unicode normalised, control and text-direction characters removed, lengths checked), strict username and password rules, id ranges checked, 32 KB request cap, honeypot. |
| Protect against XSS | Jinja escapes everything and no template uses `\|safe`. Inline styles were removed so the policy can forbid inline code. TMDB image paths and trailer ids must match a strict pattern. Tests inject script tags through reviews, search, usernames from older data and TMDB fields. |
| Rate limiting | Overall cap per visitor, plus separate limits for login, sign-up, reviews and every write. In memory, per worker (see "Still your job"). |
| Check CORS | CORS is not enabled and a test confirms no `Access-Control-*` header is ever sent. Nothing here needs cross-origin access. |
| Security headers | CSP (no inline scripts or styles, no `unsafe-eval`), `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, cross-origin opener and resource policies, HSTS over https, `no-store` on account pages. |
| Turn off debug mode | Debug is off unless you set `FLASK_DEBUG`. The app refuses to start in debug mode when `TRUST_PROXY=1`. The dev server binds to localhost only. |
| Update dependencies | Pinned to current versions with upper bounds (`requirements.txt`). Dependabot opens update pull requests weekly. CI runs `pip-audit` on every push and weekly. |
| Remove anything not needed | Removed the free-text name field on reviews, the old shared-watchlist index, and the inline-style backdrop. |
| Check for exposed files | Only `/static` is served. Tests request `.env`, `app.py`, `movies.db`, `.git/config`, `tests/` and path-traversal variants: all 404. |
| Secure the database | Every query is parameterised. Foreign keys are enforced, ratings and review length are checked by the database, the file is created owner-only (600), and it is git-ignored. |
| Hash passwords properly | scrypt through Werkzeug with a per-password salt. Plain passwords are never stored or logged (tested). |
| Check Git history for leaked secrets | No keys, tokens or `.env` in any commit. See the history notes below. |
| Full audit | This document. |

## Problems found in the code and fixed

| Severity | Problem | Fix |
|---|---|---|
| High | The watchlist was one shared list for every visitor. | Per-account lists. |
| High | A missing `SECRET_KEY` silently fell back to a known string, which would let anyone forge sessions in production. | Refuses to start. |
| Medium | Page links copied every query parameter into `url_for`, so `?_scheme=http` crashed the page and `_external` could change links. | Only `category`, `genre` and `query` are carried. |
| Medium | No accounts: reviews were anonymous with a free-text name, and "your" reviews were tracked in the cookie only. | Accounts and per-user ownership. |
| Medium | The movie backdrop was inserted into an inline CSS `url('...')` from TMDB data. A hostile path could break out of the string. | Backdrop is now an `<img>`, inline styles are gone, and image paths are pattern-checked. |
| Low | Posters, cast photos and the detail poster were stretched by the CSS (the HTML `height` attribute fought the layout). | `height: auto` plus fixed 2:3 frames. Verified in a browser. |
| Low | No security headers or request size limit. | Added. |
| Low | Database file was world-readable (644). | 600. |
| Low | Nothing was logged for failed logins or account changes. | Security events are now logged (never with passwords). |

## Git history notes

- `movies.db` (commit `66873f2`) and `__pycache__/app.cpython-314.pyc` (commit `dda8ca8`) were committed and removed in `df773c1`, but **they still exist in the history**. I could not open the binary database. By its schema it holds reviews and watchlist rows only, no keys. If you ever put real people's reviews in it, remove it from history:
  ```
  pip install git-filter-repo
  git clone --mirror https://github.com/punit-ch/filmyqui.git
  cd filmyqui.git
  git filter-repo --invert-paths --path movies.db --path __pycache__/
  git push --force
  ```
  This rewrites history, so anyone who already cloned the repository keeps the old copy.
- With accounts, `movies.db` will hold password hashes. Never commit it again (it is in `.gitignore`).
- Your commits show the email `punit.ch08@gmail.com` publicly. GitHub's settings let you switch to a `noreply` address.
- CI now runs `gitleaks` over the full history on every push.

## Still your job

- **HTTPS only**, with a proper certificate, on your host. Cookies and HSTS assume it.
- **Rotate your TMDB key** if it was ever pasted into a chat, screenshot or issue. It is free and takes a minute.
- **Back up** the database file and keep it on a persistent disk.
- **Rate limits are per worker and in memory.** With several workers a visitor gets several allowances, and a restart clears them. Use a shared store such as Redis if the site gets real traffic.
- **No email**, so there is no password reset or email verification and no two-factor login. A forgotten password means a lost account.
- **Fonts** still load from Google Fonts (allowed in the CSP). Self-hosting them removes a third-party request.
- **Keep dependencies current.** Merge Dependabot pull requests and read any `pip-audit` failure in CI.
- **CI security jobs** (`pip-audit`, `bandit`, `gitleaks`) were written but could not be run offline here. Check the first run in the GitHub Actions tab.
