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
  API_URL: "http://127.0.0.1:8099",
  SUPABASE_URL: "",
  SUPABASE_ANON_KEY: "",
};

// Local override, handy when pointing a deployed frontend at a local API:
//   localStorage.setItem('API_URL', 'http://127.0.0.1:8099')
const _override = localStorage.getItem("API_URL");
if (_override) window.CONFIG.API_URL = _override;
