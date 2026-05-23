FROM python:3.12-slim

WORKDIR /app

# System deps (sqlite3 is bundled; just curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
  && rm -rf /var/lib/apt/lists/*

# Install python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Persistent data goes on a volume mounted at /data
ENV CLIPSCASH_ENV=prod \
    CLIPSCASH_BEHIND_PROXY=1 \
    CLIPSCASH_HTTPS=1 \
    CLIPSCASH_PORT=5001

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5001/login || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
