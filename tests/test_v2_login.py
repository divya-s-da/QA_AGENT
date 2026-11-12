"""Guided login: a model-driven login for a non-standard form (overlay + reveal-on-click), with the
credentials kept in code. The fake model only names element ids; it never receives the secrets."""

import json
import os
import tempfile
import unittest

from tools.site_login import guided_login

FIXTURE = """<!doctype html><html><head><meta charset=utf-8><title>Login</title></head><body>
<div id=cookie style="position:fixed;inset:0;background:#0008;z-index:9">
  <button onclick="document.getElementById('cookie').remove()">Accept cookies</button></div>
<button id=open onclick="document.getElementById('f').style.display='block';this.style.display='none'">Log in</button>
<form id=f style="display:none" onsubmit="event.preventDefault();
    if(document.getElementById('email').value && document.getElementById('pw').value){
      document.body.innerHTML='<h1>Welcome back</h1>';history.pushState({},'','/home');}">
  <input id=company name=company placeholder=Company>
  <input id=email name=email type=email placeholder=Email>
  <input id=pw name=pw type=password placeholder=Password>
  <button type=submit>Sign in</button>
</form></body></html>"""


class FakeLoginModel:
    """Looks at the element list in the prompt and returns the next sensible login actions. Never sees secrets."""
    def __init__(self):
        self.saw_secret = False

    def __call__(self, team, messages, tools=None, **kw):
        text = messages[-1]["content"]
        # the credentials must never reach the model
        if "sekret-pass-9" in text or "tester@acme.test" in text:
            self.saw_secret = True
        els = json.loads(text[text.index("["):text.rindex("]") + 1])
        pw = next((e for e in els if e.get("type") == "password"), None)

        def reply(actions):
            from types import SimpleNamespace
            return SimpleNamespace(content="", tool_calls=[SimpleNamespace(id="l1",
                function=SimpleNamespace(name="login_step", arguments=json.dumps({"actions": actions})))])
        if pw:
            email = next((e for e in els if e.get("type") == "email" or "email" in e.get("name", "")), None)
            comp = next((e for e in els if "company" in e.get("name", "")), None)
            acts = []
            if comp:
                acts.append({"act": "fill", "id": comp["id"], "value": "ACME"})
            if email:
                acts.append({"act": "user", "id": email["id"]})
            acts.append({"act": "pass", "id": pw["id"]})
            return reply(acts)
        opener = next((e for e in els if "log in" in (e.get("text", "") or "").lower()), None)
        return reply([{"act": "click", "id": opener["id"]}] if opener else [{"act": "done"}])


class GuidedLogin(unittest.TestCase):
    def test_overlay_and_reveal_login(self):
        from playwright.sync_api import sync_playwright
        from v2.crawler import _default_browsers_path
        _default_browsers_path()
        fd, path = tempfile.mkstemp(suffix=".html")
        with os.fdopen(fd, "w") as f:
            f.write(FIXTURE)
        model = FakeLoginModel()
        try:
            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True)
                page = b.new_page()
                page.goto("file://" + path, wait_until="load")
                ok = guided_login(page, "tester@acme.test", "sekret-pass-9", "Fill Company with ACME.", model)
                b.close()
        finally:
            os.remove(path)
        self.assertTrue(ok, "guided login should reach the welcome page")
        self.assertFalse(model.saw_secret, "the email and password must never reach the model")


if __name__ == "__main__":
    unittest.main()
