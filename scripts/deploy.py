"""Deploy the frontend to Vercel and the backend to Railway -- over REST.

Why HTTP and not the CLIs: this machine has no working npm (the only node is
v6.14.0 from 2018), so `npm i -g vercel` is not available. Both platforms
expose the same capabilities over their APIs, and stdlib urllib is enough.

Usage
-----
    set VERCEL_TOKEN=...          (required for the frontend)
    set RAILWAY_TOKEN=...         (required for the backend)
    set SUPABASE_URL=...          (baked into the frontend config)
    set SUPABASE_ANON_KEY=...
    set API_URL=https://...       (Railway URL; baked into frontend config)

    python scripts/deploy.py frontend
    python scripts/deploy.py status

Deploy the backend first so its URL is known, then the frontend.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

VERCEL_API = "https://api.vercel.com"
PROJECT_NAME = os.environ.get("VERCEL_PROJECT", "express-analytics")


def _req(url: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"{method} {url}\n  HTTP {e.code}: {detail[:800]}")


def write_config() -> str:
    """Bake the deployment settings into frontend/config.js.

    Vercel serves static files, so there is no server-side env var to read at
    runtime -- the values have to be in the file at deploy time.
    """
    api = os.environ.get("API_URL", "").rstrip("/")
    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_ANON_KEY", "")
    if not api:
        raise SystemExit(
            "API_URL is not set. Deploy the backend to Railway first, then set\n"
            "  API_URL=https://<your-service>.up.railway.app"
        )
    text = (FRONTEND / "config.js").read_text(encoding="utf-8")
    new = text
    for key, val in (("API_URL", api), ("SUPABASE_URL", sb_url), ("SUPABASE_ANON_KEY", sb_key)):
        import re
        new = re.sub(rf'({key}:\s*")[^"]*(")', lambda m: m.group(1) + val + m.group(2), new, count=1)
    (FRONTEND / "config.js").write_text(new, encoding="utf-8")
    if not sb_url or not sb_key:
        print("  ! SUPABASE_URL / SUPABASE_ANON_KEY empty -> the site will deploy")
        print("    in no-login mode. Set them and redeploy to switch auth on.")
    return api


def deploy_frontend() -> None:
    token = os.environ.get("VERCEL_TOKEN", "").strip()
    if not token:
        raise SystemExit("VERCEL_TOKEN is not set (vercel.com -> Account -> Tokens).")

    api = write_config()
    print(f"  config.js -> API_URL={api}")

    # vercel.json MUST be uploaded, not skipped. It is where the security
    # headers and the no-store on config.js live, and the deployments API
    # applies them only if the file is part of the deployment. Skipping it
    # shipped a site with no X-Frame-Options and a cacheable config.js --
    # the file that holds the API URL and the Supabase anon key.
    files = []
    for p in sorted(FRONTEND.iterdir()):
        if p.is_file():
            files.append({"file": p.name, "data": p.read_text(encoding="utf-8")})
    print(f"  uploading {len(files)} files: {', '.join(f['file'] for f in files)}")

    team = os.environ.get("VERCEL_TEAM_ID", "")
    qs = f"?teamId={team}" if team else ""
    body = {
        "name": PROJECT_NAME,
        "files": files,
        "target": "production",
        "projectSettings": {"framework": None, "buildCommand": None, "outputDirectory": "."},
    }
    res = _req(f"{VERCEL_API}/v13/deployments{qs}", token, "POST", body)
    url = res.get("url") or res.get("alias", [None])[0]
    print(f"\n  deployment created: https://{url}")
    print(f"  state: {res.get('readyState', 'QUEUED')}")
    print("\n  Vercel builds asynchronously; give it ~30s then open the URL.")


def status() -> None:
    api = os.environ.get("API_URL", "").rstrip("/")
    if not api:
        print("API_URL not set; nothing to check.")
        return
    try:
        with urllib.request.urlopen(f"{api}/health", timeout=30) as r:
            print(json.dumps(json.loads(r.read()), indent=2))
    except Exception as exc:
        print(f"  backend unreachable: {exc}")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "frontend":
        deploy_frontend()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
