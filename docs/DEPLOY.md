# Deploying WhyMoved

One process serves the API and the browser app (`/app/`). State is one SQLite file plus the
uploaded files, both under one folder.

## Run it with Docker

```
docker build -t whymoved .
docker run -p 8000:8000 -v whymoved-data:/data whymoved
python scripts/smoke_test.py http://127.0.0.1:8000
```

Then open http://127.0.0.1:8000/. `-v whymoved-data:/data` keeps accounts and files between
restarts; without it every restart starts empty.

**Status of this file's instructions:** the Dockerfile has NOT been built or run. The build
sandbox had the Docker client but no running Docker daemon. The application code under it was
tested (see README), the smoke test was run against a test server, but the image itself is
untested. If `docker build` fails, the error will most likely be a package version in
`requirements-prod.txt`; send me the message.

## Run it without Docker

```
pip install -r requirements-prod.txt
uvicorn whymoved.api:create_app --factory --app-dir src --host 0.0.0.0 --port 8000
```

## Settings (environment variables, see `.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `WHYMOVED_DB` | `data/whymoved.db` (`/data/whymoved.db` in Docker) | SQLite file |
| `WHYMOVED_DATA_DIR` | `data/uploads` (`/data/uploads` in Docker) | uploaded files |
| `WHYMOVED_MAX_UPLOAD_MB` | 20 | largest upload |
| `WHYMOVED_CORS` | localhost:5173 | extra browser origins; leave empty when one server does everything |
| `WHYMOVED_WEB` | repo `web/` folder | where the browser app is read from |
| `PORT` | 8000 | listening port (Docker image only) |

## Where to host it: the storage problem

SQLite needs a disk that survives restarts. That decides the host.

- **Render, free web service** (checked on Render's own page, https://render.com/docs/free,
  on 2026-10-16): the filesystem is ephemeral and "Free web services cannot" attach a persistent
  disk. The service spins down after 15 minutes without traffic and takes about a minute to wake.
  So on this tier **every account, dataset and share link disappears at each spin-down or
  redeploy**. Fine for a portfolio demo where visitors press "Try sample data" and look; wrong
  for anything people rely on. Paid services can attach a disk.
- **Render's free Postgres** is not a way out: per the same page it expires after 30 days
  (14 more days to upgrade, then deleted) and has no backups.
- **Anything with a volume** (a small VPS, or a host whose plan includes a persistent volume)
  runs this image as is, with the volume mounted at `/data`. I have not checked any other host's
  current free tier, so check pricing yourself before choosing.

### Why SQLite was kept, not Postgres

Only `src/whymoved/service/store.py` talks to the database, so a Postgres version is a
contained job: one file, plus swapping `?` placeholders for `%s` and the type names. I did not
do it because there is no Postgres in the build sandbox, so I could not test it, and untested
database code is the wrong thing to ship. SQLite also fits a single-process app. The limit it
sets: one server process, no horizontal scaling.

## Before showing it to the public

- Put it behind HTTPS (any host that gives you a URL does). Tokens travel in a header.
- Say on the page that the data is a demo and may reset (on free hosting it will).
- There is no password reset, no email verification and no rate limit beyond login throttling
  (5 failures per 15 minutes per email). Do not invite strangers to upload confidential files.
- Back up the `/data` volume if you keep it.
