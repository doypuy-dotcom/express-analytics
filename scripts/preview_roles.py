"""Build a static, offline copy of the dashboard for each role, and serve it.

Why this exists. The frontend has no build step and this machine has no npm,
so the only way to find out whether app.js actually runs is to load it in a
browser. But the deployed API pins CORS to the Vercel origin, and running the
API locally with real auth would mean handling the test passwords -- which is
exactly what the credentials were pulled out of the repo to avoid.

So: the payloads are produced HERE, in-process, through the same dependency
override the pytest suite uses. No password is needed because identity is
stubbed, not authenticated, and the numbers are the real ones straight out of
Postgres. They are written to disk as plain files at the paths the frontend
fetches, next to a copy of the frontend itself. One origin, no CORS, no auth,
real data.

What this proves and what it does not. It proves the page parses, renders
every panel, and draws every chart without a console error, against each
role's true payload -- which is the failure this project cannot otherwise
catch before deploying. It proves nothing about authentication or about the
server's enforcement of role scope: those are the pytest suite and
scripts/verify_roles_live.py, which run against real tokens.

    python scripts/preview_roles.py            # build + serve on 8097
    python scripts/preview_roles.py --build    # build only

Then open  http://127.0.0.1:8097/<role>/  for role in sales03, manager, ceo.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".tmp" / "preview"

for line in io.open(ROOT / "backend" / ".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(ROOT / "backend"))

ACCOUNTS = {
    "sales03": "sales03@express.local",
    "manager": "manager@express.local",
    "ceo": "doykong1369@gmail.com",
}

# The query string is dropped: http.server serves the path and ignores it, and
# `?limit=300` only ever shrinks the payload. Keeping the whole list means the
# preview shows MORE rows than production, not fewer, so a rendering bug in a
# long table still surfaces.
PATHS = [
    "/api/me", "/api/overview", "/api/sales", "/api/demand",
    "/api/forecast", "/api/stock", "/api/accuracy", "/api/customers",
]

# config.js in the preview: no Supabase, so AUTH_ON is false and the page skips
# the login screen entirely. API_URL is "." and not "": the page builds
# `${API_URL}${path}`, so "" would resolve /api/overview against the server
# root and serve the wrong role's payload to every role. "." anchors it to the
# role's own directory.
CONFIG_JS = 'window.CONFIG = { API_URL: ".", SUPABASE_URL: "", SUPABASE_ANON_KEY: "" };\n'


def build() -> None:
    from fastapi.testclient import TestClient
    from app.auth import User, current_user
    from app.main import app
    from app import db

    rows = db.fetch("select email, user_id::text as user_id from user_profiles "
                    "where email = any(%s)", (list(ACCOUNTS.values()),))
    ids = {r["email"]: r["user_id"] for r in rows}
    missing = [e for e in ACCOUNTS.values() if e not in ids]
    if missing:
        sys.exit(f"test accounts not provisioned: {missing}")

    if OUT.exists():
        shutil.rmtree(OUT)

    for role, email in ACCOUNTS.items():
        async def _fake_user(_id=ids[email], _em=email) -> User:
            return User(id=_id, email=_em, role="authenticated")

        app.dependency_overrides[current_user] = _fake_user
        client = TestClient(app, raise_server_exceptions=False)

        dest = OUT / role
        dest.mkdir(parents=True)
        for name in ("index.html", "app.js", "styles.css", "preferences.js", "password.js"):
            src = ROOT / "frontend" / name
            if src.exists():
                shutil.copy(src, dest / name)
        io.open(dest / "config.js", "w", encoding="utf-8").write(CONFIG_JS)

        (dest / "api").mkdir()
        got = []
        for path in PATHS:
            r = client.get(path)
            # A 403 is a real answer -- sales cannot open forecast -- and the
            # page must cope with it, so it is written out as the API sent it
            # rather than skipped.
            body = r.json() if r.content else {}
            io.open(dest / path.lstrip("/"), "w", encoding="utf-8").write(
                json.dumps(body, ensure_ascii=False))
            got.append(f"{r.status_code} {path}")
        print(f"{role:9s} {', '.join(got)}")

    app.dependency_overrides.clear()
    print(f"\nbuilt {OUT}")


def serve(port: int = 8097) -> None:
    import functools
    import http.server
    import socketserver

    class Handler(http.server.SimpleHTTPRequestHandler):
        """No caching.

        Every rebuild rewrites app.js and config.js in place under the same
        URLs. The browser served the previous config.js from cache after a
        rebuild and the whole site 404'd against the wrong API base -- a bug
        in the harness that looks exactly like a bug in the app.
        """

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            super().end_headers()

    handler = functools.partial(Handler, directory=str(OUT))
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        for role in ACCOUNTS:
            print(f"  http://127.0.0.1:{port}/{role}/")
        httpd.serve_forever()


if __name__ == "__main__":
    build()
    if "--build" not in sys.argv:
        serve()
