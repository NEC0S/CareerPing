FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persists sessions/ (credentials, resumes, logs) across restarts if you
# mount a volume at this path on your host (Render/Railway/Fly all support
# this). Without a mounted volume, sessions are lost on redeploy.
VOLUME ["/app/sessions"]

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
