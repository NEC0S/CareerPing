FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/

EXPOSE 10000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
