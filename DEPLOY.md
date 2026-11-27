# Deploy FilmyQui for free on PythonAnywhere

**Why PythonAnywhere:** FilmyQui stores accounts, watchlists and reviews in a SQLite file, so the host must keep files between restarts. PythonAnywhere's free "Beginner" account does (512 MB of persistent disk), stays on without sleeping, and gives you HTTPS on `yourname.pythonanywhere.com`. Free hosts that wipe their disk (Render's free tier, Hugging Face Spaces, serverless hosts like Vercel) would delete every account on each restart.

**Limits of the free account** (from PythonAnywhere's help pages, check the current terms when you sign up): one web app, one worker, 512 MB disk, no custom domain, no SSH, and the web app must be renewed with one click about once a month (they email you first). Free accounts can only make outgoing requests to an allowlist of sites. The TMDB API (`api.themoviedb.org`) has been added to it, and step 5 checks that for you.

Total cost: $0.

> **Hosts that will not work with this app (as it is now):** Vercel, Netlify, AWS Lambda and other serverless hosts, Hugging Face Spaces, and Render's free tier without a disk. They start every instance, or every restart, with an empty copy of the files, so the SQLite database is lost. The symptoms are always the same: you sign up and are fine, then the next page logs you out, and logging in says "Wrong username or password" for the account you just made. The app now detects most of these hosts and shows a red warning on every page.

## 1. Put the new code on GitHub

On your computer, in your `filmyqui` folder: copy everything from the zip's `filmyqui-main/` folder over your files (overwrite), then:

```
git add -A
git status
```

Look at the list: `.env` and any `.db` file must **not** appear (`.gitignore` blocks them). Then:

```
git commit -m "Add accounts, security hardening, new UI and deploy guide"
git push
```

## 2. Create the account

Sign up at pythonanywhere.com and choose **Create a Beginner account**. Your username becomes your web address: `username.pythonanywhere.com` (or `username.eu.pythonanywhere.com` if you pick the EU server; the Web tab shows the exact address).

## 3. Get the code and install it

Open **Consoles → Bash** and run (replace nothing; `~` means your home folder):

```
git clone https://github.com/punit-ch/filmyqui.git
ls /usr/bin/python3*
```

The second command lists the Python versions. Use the newest one that is 3.10 or higher (3.11 is used below):

```
mkvirtualenv --python=/usr/bin/python3.11 filmyqui
cd ~/filmyqui
pip install -r requirements.txt
```

## 4. Add your secrets (never commit these)

```
mkdir -p ~/filmyqui-data
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"
nano .env
chmod 600 .env
```

Copy the long random line printed by the third command, then fill in `.env` (in nano: Ctrl+O, Enter to save, Ctrl+X to exit):

```
TMDB_READ_TOKEN=your TMDB "API Read Access Token"
SECRET_KEY=the long random line you just copied
TRUST_PROXY=1
ALLOWED_HOSTS=yourname.pythonanywhere.com
DATABASE_PATH=/home/yourname/filmyqui-data/movies.db
```

Use your real username in the last two lines. If you only have the shorter TMDB "API Key (v3 auth)", delete the `TMDB_READ_TOKEN` line and use `API_KEY=` instead (remove the `#` in front of it in the file).

## 5. Check it works before going live

Still in the console, with `(filmyqui)` showing at the start of the prompt:

```
python -c "import requests; print(requests.get('https://api.themoviedb.org/3/configuration', timeout=10).status_code)"
python -c "import app; print('app starts fine')"
```

- The first line should print **401**. That means PythonAnywhere can reach TMDB (the 401 only says no key was sent). If you see a "Tunnel connection failed: 403" error instead, TMDB is not on the free allowlist for you: ask them to add `api.themoviedb.org` (instructions: help.pythonanywhere.com/pages/RequestingAllowlistAdditions/), or use a paid plan.
- The second line should print `app starts fine`. If it prints a `RuntimeError`, read the message: it tells you what is missing in `.env`.

## 6. Create the web app

1. Open the **Web** tab, click **Add a new web app**, then **Next**.
2. Choose **Manual configuration** (not "Flask"), pick the same Python version as above, then **Next**.
3. Under **Code**, set **Source code** to `/home/yourname/filmyqui`.
4. Under **Virtualenv**, enter `/home/yourname/.virtualenvs/filmyqui`.
5. Click the **WSGI configuration file** link, delete everything in it, and paste this (with your username):

```python
import sys

path = "/home/yourname/filmyqui"
if path not in sys.path:
    sys.path.insert(0, path)

from app import app as application
```

6. Save, go back to the Web tab, and turn on **Force HTTPS**. (This matters: login cookies only work over https.)
7. Click the big green **Reload** button and open `https://yourname.pythonanywhere.com`.

## 7. Try everything

Create an account, open a movie, add it to your watchlist, write a review, log out and back in. Then try opening the site in a private window: you should see the movies but not your watchlist.

## Keeping it running

- **Renew monthly.** On the Web tab, click the button that says "Run until ... from today". They email you before it expires; an expired app can be turned back on from the same tab and your data is kept.
- **Update the site.** Push to GitHub, then in a Bash console: `cd ~/filmyqui && git pull && pip install -r requirements.txt`, and click **Reload** on the Web tab.
- **Back up the accounts.** In a Bash console: `sqlite3 ~/filmyqui-data/movies.db ".backup '/home/yourname/filmyqui-data/backup.db'"`, then download `backup.db` from the **Files** tab now and then. (Free accounts have no scheduled tasks, so this is manual.)
- **Read the logs.** The Web tab has links to the error log and server log. Failed logins and other security warnings appear in the error log.

## If something goes wrong

| What you see | Likely cause and fix |
|---|---|
| "Something went wrong" page from PythonAnywhere | Open the error log on the Web tab. Usually a typo in the WSGI file path or the virtualenv path. |
| `RuntimeError: SECRET_KEY must be set...` in the log | `.env` is missing or in the wrong folder. It must be at `/home/yourname/filmyqui/.env`. |
| "Bad request: this host name is not allowed." | `ALLOWED_HOSTS` doesn't exactly match the address in your browser. Fix `.env` and Reload. |
| "Movie data unavailable" page | TMDB can't be reached or the token is wrong. Re-run step 5 and check `TMDB_READ_TOKEN`. |
| Forms say "That didn't work" every time | You opened the site over `http://`. Turn on Force HTTPS and use the `https://` address. |
| "You're doing that too often" for everyone | `TRUST_PROXY=1` is missing from `.env`, so all visitors look like one address. Add it and Reload. |
| I sign up, then get logged out on the next page, and "Wrong username or password" for the account I just made | The host is not keeping the database file between visits (serverless host, or a disk that is wiped). Move to a host with a persistent disk, such as PythonAnywhere, using this guide. |
| `unable to open database file` | Run `mkdir -p ~/filmyqui-data` and check `DATABASE_PATH` has your real username. |

## Other hosts

Any host that gives you a persistent disk and runs `gunicorn app:app --workers 2` will work with the same `.env` values (set them in the host's dashboard instead of a file). Hosts with a disk that is wiped on restart need a different database and a code change first.
