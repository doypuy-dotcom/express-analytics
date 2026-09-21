"""Run the API locally against the real Supabase, for browser verification.

The deployed API pins CORS to the Vercel origin, so a page served from
localhost cannot call it. Rather than loosening CORS in production just to
test, run the same app locally -- CORS_ORIGINS defaults to "*" -- and point
the frontend at it with the override config.js already supports:

    localStorage.setItem('API_URL', 'http://127.0.0.1:8099')
"""
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for line in io.open(ROOT / "backend" / ".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT / "backend")

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=8099, log_level="info")
