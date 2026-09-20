# Deployed from the REPOSITORY ROOT, not from backend/.
#
# The upload endpoint shells out to src/parse_express.py, src/customer_rfm.py,
# src/forecast_baseline.py, src/forecast_plan.py and src/export_app.py, and
# reads data/reference/product_groups.csv. Shipping backend/ alone would leave
# ingest.py resolving ROOT to "/" and SRC to "/src" -- every read-only page
# would work and the first upload would fail.
#
# An explicit Dockerfile rather than builder auto-detection: Railway's Railpack
# builder could not prepare this layout (a root requirements.txt that only
# includes backend/requirements.txt is not a shape it recognises), and
# railway.json is config-as-code, which Railway now ignores for new services.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so that a source-only change does not reinstall pandas.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY . .

# data/raw and data/clean are gitignored and therefore absent from the image.
# The pipeline writes into them on the first upload, so they must exist and be
# writable. Railway's filesystem is ephemeral -- Postgres is the durable store.
RUN mkdir -p data/raw data/clean data/app data/forecast

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000}"]
