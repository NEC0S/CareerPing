FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies from the backend app.
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the FastAPI application and its static dashboard.
COPY app/ /app/

# Render provides PORT at runtime; default to 8000 locally.
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
