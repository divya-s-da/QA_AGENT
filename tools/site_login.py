"""Deterministic login: the credentials never pass through the model.

Before this tool, the site password was pasted into every navigator prompt,
so it went to the LLM provider and into its logs. Now the model only calls
site_login(); this code reads SITE_EMAIL / SITE_PASSWORD from .env, fills the
form and reports the outcome without echoing either value.

Handles a single-page form and the two-step "email, Continue, password" form.
SSO redirects, CAPTCHA and OTP are not handled - the tool says so and the
check is recorded BLOCKED.
"""

import re
from urllib.parse import urlparse

from . import write_guard
from .native_browser import _with_page

_USER_FIELD = (
    "input[type=email]:visible, input[name*=user i]:visible, input[name*=email i]:visible, "
    "input[name*=login i]:visible, input[autocomplete=username]:visible, "
    "input[type=text]:visible, input[type=tel]:visible, input:not([type]):visible"
)
_PASSWORD_FIELD = "input[type=password]:visible"
_NEXT_BUTTON = re.compile(r"^\s*(next|continue|sign\s*in|log\s*in|submit)\s*$", re.I)
_WAIT_MS = 15000


def _redact(text: str, secrets) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _visible_error(page) -> str:
    """First alert-like text on the page, to explain a failed login."""
    try:
        loc = page.locator("[role=alert]:visible, .error:visible, .alert:visible, [class*=error i]:visible")
        if loc.count():
            return loc.first.inner_text(timeout=2000).strip()[:200]
    except Exception:
        pass
    return ""


def _attempt(page, email: str, password: str) -> bool:
    """Fill and submit the form once. True when the page left the login form."""
    if page.locator(_PASSWORD_FIELD).count() == 0:
        # Two-step form: identifier first, then the password screen.
        page.locator(_USER_FIELD).first.fill(email, timeout=5000)
        page.get_by_role("button", name=_NEXT_BUTTON).first.click(timeout=5000)
        page.locator(_PASSWORD_FIELD).first.wait_for(state="visible", timeout=_WAIT_MS)
    else:
        # Prefer the identifier field in the password's own form, so a search
        # box in the page header is never mistaken for it.
        form = page.locator(_PASSWORD_FIELD).first.locator("xpath=ancestor::form[1]")
        user = (form if form.count() else page).locator(_USER_FIELD)
        if user.count():
            user.first.fill(email, timeout=5000)
    pw = page.locator(_PASSWORD_FIELD).first
    pw.fill(password, timeout=5000)
    pw.press("Enter")
    try:
        page.locator(_PASSWORD_FIELD).first.wait_for(state="hidden", timeout=_WAIT_MS)
    except Exception:
        return False
    try:
        page.wait_for_load_state("networkidle", timeout=_WAIT_MS)
    except Exception:
        pass
    return page.locator(_PASSWORD_FIELD).count() == 0


REGISTER_URL = re.compile(r"regist|sign[-_]?up|/join|create[-_]?account|create[-_]?your|new[-_]?account", re.I)

# Only buttons inside a real overlay (a position:fixed or sticky banner) and with clear cookie/consent text are
# clicked. A plain "Continue" / "Submit" / "Create account" is never clicked here: it could submit a form.
_DISMISS_JS = r"""() => {
  const words=/^(accept|accept all|accept cookies|agree|i agree|got it|allow all|allow|i understand|ok|okay|reject all|reject cookies|only necessary|dismiss)$/i;
  const overlaid=e=>{for(let n=e;n;n=n.parentElement){const p=getComputedStyle(n).position;if(p==='fixed'||p==='sticky')return true;}return false;};
  let n=0;
  for(const b of document.querySelectorAll('button,a,[role=button]')){
    const t=(b.innerText||b.value||'').trim();
    if(t && words.test(t) && overlaid(b)){ try{ b.click(); n++; }catch(e){} if(n>=2) break; }
  }
  return n;
}"""

_SCAN_JS = r"""() => {
  const vis = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none'; };
  const els=[...document.querySelectorAll('input:not([type=hidden]),select,textarea,button,a,[role=button]')].filter(vis).slice(0,60);
  return els.map((e,i)=>{ e.setAttribute('data-ql', i);
    return {id:i, tag:e.tagName.toLowerCase(), type:(e.getAttribute('type')||'').toLowerCase(),
      name:e.getAttribute('name')||'', ph:e.getAttribute('placeholder')||'',
      label:(e.getAttribute('aria-label')||'')||((e.labels&&e.labels[0])?e.labels[0].innerText:''),
      text:(e.innerText||e.value||'').trim().slice(0,40)};}); }"""

GUIDED_SYSTEM = """You help a QA tool log in to a website. The email and password are typed by CODE, never by you:
you do not see them and must not ask for them. From the element list, give the NEXT one or two actions to log in.
Actions (use ids from the list only):
- {"act":"user","id":N}  fill the username / email box
- {"act":"pass","id":N}  fill the password box
- {"act":"fill","id":N,"value":"..."}  type a NON-SECRET value (a company, a code from the owner notes)
- {"act":"click","id":N}  click a button or link (Next, Sign in, a Log in link that opens the form)
- {"act":"done"}  the login is finished: no password box and the page has changed
Follow the owner notes for extra steps (two-step forms, an extra field, a cookie banner).
NEVER register or create an account: only log in with the existing account. Never click Sign up, Register or
Create account. If you only see a sign-up form and no log-in, return {"act":"done"} and do nothing. Return the tool once."""

GUIDED_TOOL = [{"type": "function", "function": {"name": "login_step", "description": "the next login actions",
    "parameters": {"type": "object", "properties": {"actions": {"type": "array", "items": {"type": "object",
        "properties": {"act": {"type": "string", "enum": ["user", "pass", "fill", "click", "done"]},
                       "id": {"type": "integer"}, "value": {"type": "string"}}, "required": ["act"]}}},
        "required": ["actions"]}}}]


def dismiss_overlays(page) -> None:
    """Best effort: close a cookie or consent banner that covers the login. Only clicks a clear cookie button
    inside a fixed or sticky overlay, so it never submits a form or creates data."""
    try:
        if page.evaluate(_DISMISS_JS):
            page.wait_for_timeout(300)
    except Exception:
        pass


def _looks_like_password(el: dict) -> bool:
    return el.get("type") == "password" or bool(re.search(r"pass", f"{el.get('name','')} {el.get('ph','')} {el.get('label','')}", re.I))


def _el_locator(page, el: dict):
    """A stable locator for an element from the scan: by name, placeholder, type, or button/link text. The scan's
    temporary id does not survive a client re-render, so we re-find the element by a real property at use time."""
    tag = el.get("tag", "")
    name, ph, typ = el.get("name", ""), el.get("ph", ""), el.get("type", "")
    text = (el.get("text") or el.get("label") or "").strip()
    try:
        if tag in ("input", "textarea", "select"):
            if name:
                return page.locator(f'{tag}[name="{name}"]').first
            if ph:
                return page.get_by_placeholder(ph).first
            if typ:
                return page.locator(f'{tag}[type="{typ}"]').first
            return page.locator(tag).first
        if text:
            return page.get_by_role("button", name=text).or_(page.get_by_role("link", name=text)).first
    except Exception:
        return None
    return None


def guided_login(page, email, password, notes, chat, log=print, max_steps: int = 7) -> bool:
    """Model-guided login for a non-standard form (two-step, an extra field, an overlay). The credentials stay
    in code: the model only names element ids; the password is typed only into a real password field."""
    import json as _json
    log("  login: standard form not usable; trying the guided login (model-driven)")
    dismiss_overlays(page)
    try:                                   # let a client-rendered (SPA) login form finish rendering before the scan
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        page.wait_for_timeout(1500)
    filled_pass = False
    for _ in range(max_steps):
        try:
            els = page.evaluate(_SCAN_JS)
        except Exception:
            return False
        by_id = {e["id"]: e for e in els}
        user = ("ELEMENTS (id, tag, type, name, placeholder, label, text):\n" + _json.dumps(els)[:4500]
                + ("\n\nOWNER NOTES:\n" + notes[:1500] if notes else "")
                + "\n\nGive the next actions to log in. The email and password are filled by code.")
        try:
            msg = chat("planner", [{"role": "system", "content": GUIDED_SYSTEM},
                                   {"role": "user", "content": user}], GUIDED_TOOL, max_tokens=500, timeout=45)
        except Exception:
            return False
        acts = []
        for c in getattr(msg, "tool_calls", None) or []:
            if c.function.name == "login_step":
                try:
                    acts = _json.loads(c.function.arguments or "{}").get("actions", [])
                except ValueError:
                    acts = []
        if not acts:
            return False
        acted = done = clicked = False
        for a in acts:
            role, i = a.get("act"), a.get("id")
            if role == "done":
                done = True
                break
            loc = _el_locator(page, by_id.get(i, {})) if i is not None else None
            if loc is None:
                continue
            try:
                if role == "user":
                    loc.fill(email, timeout=5000); acted = True
                elif role == "pass" and _looks_like_password(by_id.get(i, {})):
                    loc.fill(password, timeout=5000); acted = filled_pass = True
                elif role == "fill":
                    loc.fill(str(a.get("value", ""))[:200], timeout=5000); acted = True
                elif role == "click":
                    loc.click(timeout=5000); acted = clicked = True
            except Exception:
                pass
        if filled_pass and not clicked:        # submit the form if the model did not click a button
            try:
                page.locator(_PASSWORD_FIELD).first.press("Enter")
            except Exception:
                pass
        if done or not acted:
            return False
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        page.wait_for_timeout(400)
        try:
            pw_now = page.evaluate("() => document.querySelector('input[type=password]') ? 1 : 0")
        except Exception:
            return False
        # success only after the password was typed and the password field then left the page (the form submitted)
        if filled_pass and not pw_now:
            return True
    return False


def site_login() -> str:
    """Log in to the target site with the test account from the server config.

    Navigate to the site's login page first, then call this tool. NEVER type
    an email or password yourself and never ask the user for one: you are not
    given the credentials, on purpose. The result says whether the page left
    the login form. If it reports LOGIN FAILED or LOGIN UNSUPPORTED, record
    the login check as BLOCKED with that reason.
    """
    from ..config import SITE_EMAIL, SITE_PASSWORD, SITE_PASSWORD_FALLBACK
    from . import credential_vault
    from .screenshot_tool import _WEB_SESSION

    # The session's own account wins over the server file. A client types the
    # account into the product; they cannot be asked to edit a file on a
    # server. The file stays as the fallback for an unattended CLI run.
    held = credential_vault.get(_WEB_SESSION or "")
    if held:
        email, password, fallback = held
        source = "the account you entered for this session"
    else:
        email, password, fallback = SITE_EMAIL, SITE_PASSWORD, SITE_PASSWORD_FALLBACK
        source = "the server configuration"

    if not email or not password:
        return ("LOGIN UNSUPPORTED: no test account is available. Tell the user "
                "to enter the site's email and password in the Site login panel, "
                "then call site_login() again. Never ask for the password in chat.")
    error = write_guard.ensure_active()
    if error:
        return error
    secrets = (email, password, fallback)

    def _run(page):
        if page.locator(_PASSWORD_FIELD).count() == 0 and page.locator(_USER_FIELD).count() == 0:
            return (f"LOGIN UNSUPPORTED: no login form on {page.url}. Navigate to the login "
                    "page first. SSO, CAPTCHA and OTP logins are not supported.")
        host = (urlparse(page.url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        with write_guard.login_window(host):
            for attempt_password in (password, fallback):
                if not password:
                    continue
                try:
                    if _attempt(page, email, attempt_password):
                        return (f"LOGGED IN with {source}: the page left the login "
                                f"form; now at {page.url}")
                except Exception as e:
                    reason = _redact(f"{type(e).__name__}: {e}", secrets)[:300]
                    return f"LOGIN FAILED: could not operate the form ({reason})"
        shown = _visible_error(page)
        return _redact(
            "LOGIN FAILED: the login form is still shown"
            + (f"; the page says: {shown!r}" if shown else ""),
            secrets,
        )

    return _redact(str(_with_page(_run)), secrets)
