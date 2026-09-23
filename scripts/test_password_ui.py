"""Browser regression checks with a mocked auth provider; no real accounts/emails.

Run: python scripts/test_password_ui.py
Requires playwright and Microsoft Edge. Does not print passwords or tokens.
These checks validate UI/routing, not Supabase email delivery.
"""
from pathlib import Path
import json
import secrets
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://password-test.invalid"
STUB = """
export function createClient(url, key, options) {
 const isolated = !!options;
 return {auth: {
  getSession: async () => ({data:{session:window.signed ? {user:{email:'test@example.invalid'}} : null}}),
  getUser: async () => ({data:{user:{id:'test-user', email:'test@example.invalid'}}}),
  signInWithPassword: async () => window.correct
    ? {data:{user:{id:'test-user'},session:{}},error:null}
    : {data:{},error:{code:'invalid_credentials'}},
  signOut: async () => ({error:null}),
  resetPasswordForEmail: async (email, opts) => {
    window.resetRequests.push({email,redirectTo:opts.redirectTo});
    return {error:window.authError};
  },
  updateUser: async () => {window.updates++;return {error:window.authError};},
  onAuthStateChange: cb => {
    if (!isolated && location.search.includes('recovery') && window.signed)
      setTimeout(()=>cb('PASSWORD_RECOVERY'),0);
    return {data:{subscription:{unsubscribe(){}}}};
  }
 }};
}
"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        errors = []

        def page(signed=False, suffix=""):
            ctx = browser.new_context()
            ctx.add_init_script(f"window.signed={json.dumps(signed)};window.correct=false;window.authError=null;window.resetRequests=[];window.updates=0;")

            def route(req):
                url = req.request.url
                if "esm.sh/" in url:
                    req.fulfill(body=STUB, content_type="application/javascript")
                elif url.startswith(ORIGIN):
                    name = url.split(ORIGIN, 1)[1].split("?", 1)[0].strip("/") or "index.html"
                    if name == "config.js":
                        req.fulfill(body=f"window.CONFIG={{API_URL:'{ORIGIN}',SUPABASE_URL:'https://auth.invalid',SUPABASE_ANON_KEY:'test'}}", content_type="application/javascript")
                    elif name == "api/me":
                        req.fulfill(json={"role": "none", "pages": {}})
                    else:
                        file = ROOT / "frontend" / name
                        if not file.is_file():
                            req.fulfill(status=404)
                        else:
                            mime = {".js":"application/javascript", ".css":"text/css", ".html":"text/html"}[file.suffix]
                            req.fulfill(body=file.read_text(encoding="utf-8"), content_type=mime)
                else:
                    req.fulfill(body="", content_type="application/javascript")
            ctx.route("**/*", route)
            tab = ctx.new_page()
            tab.on("pageerror", lambda error: errors.append(str(error)))
            tab.goto(ORIGIN + "/" + suffix)
            return tab

        tab = page()
        tab.locator("#forgot-password").click()
        tab.locator("#reset-email").fill("test@example.invalid")
        tab.locator("[type=submit]").click()
        tab.wait_for_function("resetRequests.length === 1")
        assert tab.evaluate("resetRequests[0].redirectTo") == ORIGIN + "/?auth=recovery"
        assert tab.locator(".password-message.ok").count() == 1
        tab.locator("#ui-language").click()
        assert tab.get_by_role("heading", name="Forgot password").count() == 1
        assert tab.locator("#reset-email").input_value() == "test@example.invalid"
        tab.evaluate("window.authError={status:429}")
        tab.locator("[type=submit]").click()
        tab.wait_for_function("document.querySelector('.password-message').textContent.includes('Too many')")
        print("PASS: reset request, fixed redirect, neutral success, rate limit and language toggle")

        value = secrets.token_urlsafe(20)
        tab = page(True, "#password")
        tab.locator("#current-password").fill(secrets.token_urlsafe(16))
        tab.locator("#new-password").fill(value)
        tab.locator("#confirm-password").fill(value + "x")
        tab.locator("[type=submit]").click()
        assert tab.evaluate("updates") == 0
        tab.locator("#confirm-password").fill(value)
        tab.locator("[type=submit]").click()
        tab.wait_for_timeout(150)
        assert tab.evaluate("updates") == 0
        tab.evaluate("window.correct=true")
        tab.locator("[type=submit]").click()
        tab.wait_for_function("updates === 1")
        assert tab.locator("#new-password").input_value() == ""
        assert tab.locator("[type=submit]").is_hidden()
        print("PASS: mismatch rejected, current password checked, successful update clears fields")

        tab = page(True, "?auth=recovery")
        tab.wait_for_selector("#new-password")
        assert tab.locator("#current-password").count() == 0
        tab.locator("#new-password").fill(value)
        tab.locator("#confirm-password").fill(value)
        tab.locator("[type=submit]").click()
        tab.wait_for_function("updates === 1")
        tab = page(False, "?auth=recovery#error=access_denied&error_code=otp_expired")
        tab.wait_for_selector("#reset-email")
        assert tab.locator("#new-password").count() == 0
        print("PASS: recovery opens before role checks; expired link requests a fresh email")
        assert not errors, errors
        print("PASS: zero JavaScript errors; no real emails sent or account passwords changed")
        browser.close()


if __name__ == "__main__":
    main()
