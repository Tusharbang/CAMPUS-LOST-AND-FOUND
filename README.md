# CampusFind – RBU Lost & Found (Flask + SQLite)

A privacy-first Lost & Found system for Ramdeobaba University: no public catalogue, staff-verified claims,
and a separate dashboard for each building incharge.

## Run locally
    python -m venv venv                # optional
    venv\Scripts\activate              # Windows   (Mac/Linux: source venv/bin/activate)
    pip install -r requirements.txt
    python app.py                      # open http://127.0.0.1:5000

`database.db` is created on first run and filled from `data/*.csv` (119 records). Delete it to reset.
Port busy? `PORT=5001 python app.py` (Windows: `set PORT=5001` first).

## Configuration (environment variables)
| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_PATH` | SQLite file location | `./database.db` |
| `UPLOAD_FOLDER` | Folder for private photos | `./uploads` |
| `SECRET_KEY` | Signs login sessions. Set a long random value in production | auto-generated file next to the database |
| `HTTPS` | Set to `1` to mark cookies Secure when serving over HTTPS | off (always on when `RENDER` is set) |
| `PORT` | Port for `python app.py` | `5000` |

## Deploy on Render
1. Push this folder to a GitHub repository (`.gitignore` already keeps the database, secrets and uploads out of Git).
2. In Render choose **New → Blueprint** and select the repository. `render.yaml` creates the web service,
   a generated `SECRET_KEY`, and a 1 GB persistent disk mounted at `/var/data`.
3. Wait for the build, then open the service URL. The first start creates the database on the disk and loads the 119 records.
4. **Log in as `admin@rbunagpur.in` / `admin123` and change the password immediately** (Profile page).
   Do the same for the student and the seven `incharge.*@rbunagpur.in` accounts, or delete them.

Notes
- SQLite and photos need the **persistent disk**, which requires a paid Render plan. On the free plan data is lost on every restart.
- Gunicorn runs with **one worker** (see `Procfile` / `render.yaml`): SQLite is a single file and the login-attempt counter is in memory. Use threads, not more workers.
- Other hosts (Railway, Heroku-style): the `Procfile` works; set `SECRET_KEY`, and point `DATABASE_PATH` / `UPLOAD_FOLDER` at a persistent volume.

## Demo logins (local only – change before deploying)
| Role | Email | Password |
|---|---|---|
| Main admin | admin@rbunagpur.in | admin123 |
| Student | student@rbunagpur.in | student123 |
| Building incharge | incharge.dt / gblock / ecs / mba / mca / bca / eblock @rbunagpur.in | incharge123 |

## Structure
app.py (routes, SQLite schema, matching, access control) · templates/ · static/css · static/img · uploads/ (private photos, served only after a permission check) · data/ · Procfile · render.yaml
