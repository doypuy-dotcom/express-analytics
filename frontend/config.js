// Deployment configuration.
//
// These are PUBLIC values by design: the Supabase anon key is safe to ship to
// the browser -- it grants nothing on its own, because every table has Row
// Level Security requiring an authenticated user (see backend/schema.sql).
// The service_role key must NEVER appear in this file; it lives only in the
// Railway environment.
//
// Set by scripts/deploy.py at deploy time. Editable by hand for local work.
window.CONFIG = {
  API_URL: "https://backend-production-f146.up.railway.app",
  SUPABASE_URL: "https://uaxoethbecvdzbniived.supabase.co",
  SUPABASE_ANON_KEY: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVheG9ldGhiZWN2ZHpibmlpdmVkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk5MTU4ODEsImV4cCI6MjEwNTQ5MTg4MX0.aLIUCOxwSAw4hd6ZI8mKbhLZoG3ycRP4cL8SZ7kfP0I",
};

// Local override, handy when pointing a deployed frontend at a local API:
//   localStorage.setItem('API_URL', 'http://127.0.0.1:8099')
const _override = localStorage.getItem("API_URL");
if (_override) window.CONFIG.API_URL = _override;
