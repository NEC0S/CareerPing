# Career Email Agent — Public MVP

## What this version includes
- Simple customer sign-up/login with private per-user session data.
- Resume upload, About You text, and supporting document upload (PDF/DOCX/TXT/MD).
- Automatic supporting-document text extraction.
- Email app-password credentials encrypted at rest with Fernet.
- PostgreSQL support via `DATABASE_URL`; SQLite remains the local-development default.
- 8 MB upload limit for profile documents.
- Docker Compose with PostgreSQL and persistent volumes.
- `COOKIE_SECURE=1` for HTTPS production cookies.

## Local development
1. Create a virtual environment and install requirements.
2. Run `uvicorn app:app --reload --port 8000` from the `app` directory.
3. Open http://localhost:8000.
4. The app uses SQLite locally if `DATABASE_URL` is not set.

## Production with Docker Compose
1. Copy `app/.env.example` to `app/.env`.
2. Generate a Fernet key:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
3. Put that key into `ENCRYPTION_KEY` in `app/.env`.
4. Set a strong PostgreSQL password in both `docker-compose.yml` and `DATABASE_URL`.
5. Keep `COOKIE_SECURE=1` when serving through HTTPS.
6. Run `docker compose up -d --build`.
7. Put HTTPS in front of port 8000 using your hosting provider's domain/SSL or a reverse proxy.

## Important
- Do not commit `.env`, customer session data, or database files to GitHub.
- The background poller still runs inside the app process. Use a persistent VM/container host, not a serverless function platform.
- For a public beta, keep the number of customers modest until a proper job queue and OAuth-based mailbox connection are added.


## Deploy publicly on Render (recommended for this MVP)

This app is a long-running FastAPI process with per-user background polling, so deploy it as a Render **Web Service**, not a serverless function. Render gives the service a public `onrender.com` URL and manages HTTPS/TLS. citeturn747582search1turn747582search3

### 1. Push the project to GitHub
Create a private GitHub repository and push this project. Do **not** commit `app/.env`, `sessions/`, database files, or customer credentials.

### 2. Create Render Postgres
In Render, create a managed PostgreSQL database. Copy its internal connection URL into the web service as `DATABASE_URL`. Render recommends managed Postgres over using a disk as your primary relational datastore. citeturn747582search7

### 3. Create the Render Web Service
Create **New → Web Service**, connect the GitHub repo, and choose Docker. The included `app/Dockerfile` uses Render's `PORT` value, so the service can receive public traffic correctly. Render web services must listen on `0.0.0.0`. citeturn747582search1

Set these environment variables in Render:

```text
DATABASE_URL=<your Render Postgres internal URL>
ENCRYPTION_KEY=<Fernet key>
COOKIE_SECURE=1
GOOGLE_API_KEY=<shared Gemini API key>
DAYS_TO_SCAN=2
POLL_INTERVAL_SECONDS=120
```

Generate the encryption key locally with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep `GOOGLE_API_KEY` as the server-side fallback. A user can still enter their own Gemini key; leaving that field blank uses this `.env`/Render environment value.

### 4. Add persistent storage for resumes and credentials
The app stores encrypted mailbox credentials, resumes, supporting documents, and activity files under `/app/sessions`. Render's normal filesystem is ephemeral, so attach a **persistent disk** to the web service and mount it at exactly:

```text
/app/sessions
```

Only data under the mounted path survives restarts/deploys. Render currently requires a paid web service for persistent disks. citeturn747582search2turn747582search4

### 5. Add a health check
Set the Render health-check path to:

```text
/health
```

The app now returns `{"ok": true}` from that endpoint. Render can use health checks when bringing a new deploy online. citeturn747582search9

### 6. Deploy and test
Deploy the service and open the generated `https://<your-service>.onrender.com` URL. Render provides a public subdomain for every web service and automatically terminates HTTPS. citeturn747582search1turn747582search3

Test these flows before sharing the link:

1. Create an account.
2. Connect an inbox and upload the profile files.
3. Log out and log back in — the profile should still be there.
4. Use **Change email account**, enter the same inbox again, and confirm the saved resume/supporting file are restored without re-uploading.
5. Enter a different inbox, then switch back to the first one and confirm each inbox keeps its own profile.
6. Leave the Gemini field blank and confirm the dashboard shows **Shared fallback** when `GOOGLE_API_KEY` is set.
7. Keep **Send replies for real** off until you have tested the workflow.

### 7. Share the link with your manager
For a demo, you can simply share the Render `onrender.com` URL. Render also supports custom domains and automatically provisions/renews TLS certificates for them. citeturn747582search0turn747582search3

One important limitation: the current app has real account creation/login, but it does **not** have a read-only guest/demo mode. Your manager will need an account and a configured inbox/profile to see a live dashboard. For a polished manager demo, add a separate demo/read-only mode rather than sharing a real mailbox account.
