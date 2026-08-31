# Career Email Agent — multi-tenant dashboard

A hosted, per-visitor version of the career-email-agent: IMAP → LLM decision
→ SMTP reply / Pushover notify, wrapped in a FastAPI backend with a one-page
frontend. Anyone who opens the site connects their **own** inbox, in the
browser — nothing about which mailbox to watch is baked into the server.

## How accounts work

- On first visit you get a setup screen: email, app password, and
  (optionally) your own Gemini API key. These are tested against IMAP
  immediately, then remembered via a browser cookie — there's no
  username/password login, just "this browser = this inbox".
- If you don't paste your own Gemini key, the agent falls back to one shared
  key configured on the server (`GOOGLE_API_KEY` in `.env`) — set that if you
  want to offer a no-key-required experience; otherwise each visitor must
  bring their own.
- A second setup step lets you upload a resume (PDF) and a short summary
  about yourself — this is the only context the agent is allowed to use when
  writing replies. Both are optional and can be skipped.
- **Change account** (bottom of the left rail) disconnects the current
  browser's inbox and wipes that session's data on the server, so a fresh
  setup can start clean.
- Each session's credentials, resume/summary, handled-message IDs, and
  activity log live under `sessions/<session-id>/` next to `app.py`.

## Running locally

```bash
pip install -r requirements.txt
uvicorn app:app --port 8000
```

Open **http://localhost:8000** — you'll land on the setup screen.

Optional `.env` next to `app.py`:
```
GOOGLE_API_KEY=fallback_gemini_key_for_visitors_without_their_own
DAYS_TO_SCAN=2
POLL_INTERVAL_SECONDS=120
```

## Deploying

**Important:** this app runs a background polling thread per active session
that has to keep running between requests. That does **not** work on
serverless hosts (Vercel, Netlify Functions, AWS Lambda) — they spin the
process down between requests and kill background loops. Use a host that
keeps one process alive: **Render, Railway, Fly.io, or a plain VPS.**

A `Dockerfile` is included:
```bash
docker build -t career-email-agent .
docker run -p 8000:8000 -v $(pwd)/sessions:/app/sessions --env-file .env career-email-agent
```
Mount `/app/sessions` to a volume (as above) so accounts survive a redeploy —
without it, everyone has to reconnect their inbox after every deploy.

The frontend is plain HTML/JS (no build step) on purpose — that's what keeps
this a single deployable process. It would need to move off the FastAPI
static-file setup (and probably off the single-process background-thread
model too) to become a Next.js app; happy to do that if you specifically
need it, but it's not required for this to work.

## What the dashboard does

- **Check inbox now** — runs one pass over your inbox immediately.
- **Start/Stop watching** — starts/stops the background loop that checks
  automatically every `POLL_INTERVAL_SECONDS`.
- **Send replies for real** — off = dry-run (the agent still decides what it
  would do, but nothing is emailed or pushed). On = live behavior.
- **Activity list** — every email looked at, in order, with what it decided:
  replied automatically, flagged for you (with why), or skipped. Click "Show
  reply" on a replied item to see the exact text sent.
- The left panel shows whether your resume/summary were found, whether
  you're on your own Gemini key or the shared fallback, and when the inbox
  was last checked.

## Behavior (unchanged from the single-tenant version)

- Scans the last **2 days** by default (`DAYS_TO_SCAN`).
- Only auto-replies to emails that are still **unread** — if you've already
  opened a career-related email yourself, the agent leaves it for you
  ("Already read - leaving it for you to handle personally" in the log).
- `handled_message_ids.json` is saved after every single email (not once per
  batch), so one bad email mid-batch can't cause everything before it to be
  silently re-sent on the next poll.
- Emails with no `Message-ID` header get a stable fallback key derived from
  From/Subject/Date, so they're not rediscovered and re-replied-to forever.
- "Check inbox now" and the background poller share a lock per session, so
  they can never race and overwrite each other's saved progress.

## Notes / limitations

- App passwords, and optionally a personal Gemini key and Pushover
  credentials, are stored in plaintext JSON per session
  (`sessions/<id>/config.json`) on the server's disk. This is fine for
  personal or small-trusted-group use; for a public multi-user product you'd
  want to encrypt these at rest and put the app behind HTTPS (all major
  hosts above provide this automatically).
- There's no way to recover access if you lose the cookie/browser — you'd
  just reconnect via setup again with the same email; nothing is deleted
  except by explicit "Change account".
