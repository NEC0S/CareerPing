# CareerPing

CareerPing is a FastAPI-based career email agent with a browser dashboard, per-user mailbox configuration, optional per-user Gemini keys, a shared Gemini fallback, and per-email resume/profile data.

## Free deployment architecture

This project is prepared for:

- Render Free Web Service: runs the FastAPI/Docker app and provides the public `*.onrender.com` URL.
- Supabase Free: provides PostgreSQL plus private Storage for durable user files/session state.

Render Free has an ephemeral filesystem, so this project does **not** rely on `sessions/` being durable in production. Instead, the app mirrors each user's session workspace into a private Supabase Storage object. Supabase Free currently includes 500 MB database size and 1 GB file storage; free projects may pause after inactivity. Render Free web services also spin down after 15 minutes without inbound traffic, so background email polling is not continuous while the service is asleep. Use **Check inbox now** after the service wakes, or upgrade hosting later for always-on polling.

## Local development

From `app/`:

```powershell
pip install -r requirements.txt
uvicorn app:app --reload --port 8002
```

For local development, leave `DATABASE_URL` unset or use:

```env
DATABASE_URL=sqlite:///./career_agent.db
COOKIE_SECURE=0
```

Generate an encryption key once:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Production environment variables

Set these in Render **Environment**. Do not commit them to GitHub.

```env
DATABASE_URL=postgresql://postgres.<PROJECT_REF>:<PASSWORD>@aws-<REGION>.pooler.supabase.com:5432/postgres
ENCRYPTION_KEY=<generated Fernet key>
COOKIE_SECURE=1
GOOGLE_API_KEY=<shared Gemini fallback key>
DAYS_TO_SCAN=2
POLL_INTERVAL_SECONDS=120
SUPABASE_URL=https://<PROJECT_REF>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<Supabase service-role key>
SUPABASE_STORAGE_BUCKET=career-files
```

For a free Supabase project on Render, use the **Session pooler** connection string from Supabase's Connect panel. Supabase documents Session mode (port 5432) as the IPv4-compatible option for persistent backends on IPv4-only networks.

## Supabase setup

1. Create one Supabase project on the Free plan.
2. Open **Connect** and copy the **Session pooler** PostgreSQL connection string. Put it in Render as `DATABASE_URL`.
3. Open **Settings / API** and copy the server-side **service_role** key. Put it in Render as `SUPABASE_SERVICE_ROLE_KEY`. Never expose this value to the browser.
4. Leave `SUPABASE_STORAGE_BUCKET=career-files`. The app creates this bucket automatically on first startup if it does not already exist. It is private.
5. Deploy. SQLAlchemy creates the application's tables automatically on startup.

### What is stored where

- Supabase Postgres: application accounts and authentication sessions.
- Supabase Storage: per-user mailbox credentials (encrypted with `ENCRYPTION_KEY`), resumes, summaries, supporting documents, handled-message IDs, logs, and saved per-inbox profiles.
- Render local filesystem: temporary working cache only.

## Render setup

Create **New → Web Service** and connect the GitHub repository.

Use:

```text
Environment: Docker
Root Directory: (blank)
Dockerfile Path: Dockerfile
```

Choose the Free plan.

Set health check path to:

```text
/health
```

Add the environment variables above and deploy.

The public URL is the Web Service's `https://<service-name>.onrender.com` URL. The Supabase database URL is not a browser URL.

## Important free-tier behavior

Render Free is suitable for a demo/hobby deployment, but the service sleeps after 15 minutes without inbound traffic and loses its local filesystem on sleep/redeploy. Supabase Storage is therefore the durable file layer in production.

Because the email watcher is a background thread inside the FastAPI process, it can only run while the Render service is awake. A free deployment cannot guarantee 24/7 background polling. The manual **Check inbox now** action remains available after the service wakes.
