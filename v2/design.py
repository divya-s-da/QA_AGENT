"""QA Agent v2: design rules measured on the rendered page (plain code, no model).

Every website has its own design, so these rules do not judge taste. They find what is wrong on any
design: things that overlap or cover a button, clipped text, sideways scroll, low text contrast, tiny
text and targets, stretched or broken images, near-miss alignment of a row, and placeholder or raw
error text left on the page. The rule IDs come from docs/V2_DESIGN_RULES.yaml.

The axe-core library (MPL-2.0, in v2/vendor/) adds the accessibility rules. This module keeps only the
critical and serious results, so that the report stays about things a person sees.

The vision model (the UI/UX team) is for the judgment calls that no rule can measure: the page
hierarchy, a confusing flow, an odd look. This module and the model do not overlap.

The checker runs this module one time on the main pages, in its own browser, at the same time as the
testers. It makes no model call, so it does not use a model slot.
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse

from .board import TaskBoard
from .sitemap import SiteMap, route_of

SEVERITY_RISK = {"T0": 3, "T1": 2, "T2": 1}
AXE_PATH = os.path.join(os.path.dirname(__file__), "vendor", "axe.min.js")

# One pass over a page. Each finding: {rule, sev, title, what, where}. The caps keep it fast on big pages.
DESIGN_JS = r"""
() => {
  const out = [], W = window.innerWidth, H = window.innerHeight;
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity > 0.05; };
  const label = el => { const t = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('alt') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 40);
    return el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (t ? ' "' + t + '"' : ''); };
  const seen = {};
  const add = (rule, sev, title, el, what) => {
    seen[rule] = (seen[rule] || 0) + 1;
    if (seen[rule] > 6) return;
    out.push({ rule: rule, sev: sev, title: title, what: what, where: el ? label(el) : '' }); };
  const nodes = [...document.querySelectorAll('body *')].slice(0, 4000);
  const shown = nodes.filter(vis);
  const ownText = el => [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\s+/g, ' ').trim();
  const lum = c => { const m = (c || '').match(/[\d.]+/g); if (!m || m.length < 3) return null;
    const a = m.length >= 4 ? +m[3] : 1; if (a < 0.1) return null;
    const f = x => { x = x / 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(+m[0]) + 0.7152 * f(+m[1]) + 0.0722 * f(+m[2]); };
  const bgOf = el => { let n = el; while (n) { const b = getComputedStyle(n).backgroundColor;
    const l = lum(b); if (l !== null) return l; n = n.parentElement; } return 1; };

  // DS-RESP-001 sideways scroll
  const sw = document.documentElement.scrollWidth;
  if (sw > W + 2) {
    const off = shown.filter(el => getComputedStyle(el).position !== 'fixed' && el.getBoundingClientRect().right > W + 2)
      .sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right)[0];
    add('DS-RESP-001', 'T1', 'The page scrolls sideways', off || null, 'the content is ' + sw + 'px wide on a ' + W + 'px screen');
  }

  // DS-OVER-002 something covers a button or a link (the centre point hits another element)
  const inter = shown.filter(el => el.matches('a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link]'));
  for (const el of inter.slice(0, 300)) {
    const r = el.getBoundingClientRect();
    if (r.top < 0 || r.bottom > H || r.width < 2 || r.height < 2) continue;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const top = document.elementFromPoint(cx, cy);
    if (top && top !== el && !el.contains(top) && !top.contains(el)) {
      const tr = top.getBoundingClientRect();
      if (tr.width * tr.height < W * H * 0.9)                       // not a full-page overlay (cookie banner etc.)
        add('DS-OVER-002', 'T0', 'Something covers a button or a link', el, 'the centre of this control is behind ' + label(top));
    }
  }

  // DS-OVER-001 text overlaps text (two text elements, neither inside the other, boxes overlap a lot)
  const tnodes = shown.filter(el => ownText(el).length >= 2 && el.getBoundingClientRect().top < H).slice(0, 220);
  for (let i = 0; i < tnodes.length; i++) {
    const a = tnodes[i].getBoundingClientRect();
    for (let j = i + 1; j < tnodes.length; j++) {
      const b = tnodes[j].getBoundingClientRect();
      if (tnodes[i].contains(tnodes[j]) || tnodes[j].contains(tnodes[i])) continue;
      const ox = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
      const oy = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
      const ov = ox * oy, area = Math.min(a.width * a.height, b.width * b.height);
      if (area > 0 && ov / area > 0.3) {
        add('DS-OVER-001', 'T0', 'Text overlaps other text', tnodes[i], 'overlaps ' + label(tnodes[j]));
        break;
      }
    }
  }

  // DS-TEXT-007 text clipped by its own box (overflow hidden, no ellipsis)
  for (const el of tnodes) {
    const s = getComputedStyle(el);
    if ((s.overflowX === 'hidden' || s.overflowY === 'hidden') && s.textOverflow !== 'ellipsis'
        && (el.scrollWidth > el.clientWidth + 2 || el.scrollHeight > el.clientHeight + 2))
      add('DS-TEXT-007', 'T1', 'Text is cut off by its box', el, 'the text is wider or taller than the box that hides it');
  }

  // DS-TEXT-001 very small body text
  for (const el of tnodes) {
    if (ownText(el).split(' ').length < 3) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize);
    if (fs && fs < 11) add('DS-TEXT-001', 'T1', 'Body text is very small', el, 'the text is ' + fs.toFixed(0) + 'px (a readable size is at least 12px)');
  }

  // DS-CONTRAST-004 dark text on a dark background, or light on light (unreadable)
  for (const el of tnodes) {
    if (ownText(el).split(' ').length < 2) continue;
    const fl = lum(getComputedStyle(el).color), bl = bgOf(el);
    if (fl === null) continue;
    const ratio = (Math.max(fl, bl) + 0.05) / (Math.min(fl, bl) + 0.05);
    if (ratio < 1.6 && ((fl < 0.25 && bl < 0.25) || (fl > 0.75 && bl > 0.75)))
      add('DS-CONTRAST-004', 'T0', 'Text is the same shade as its background', el, 'the text is almost invisible against the background');
  }

  // DS-IMG-003 broken image, DS-IMG-001 stretched image
  for (const img of [...document.querySelectorAll('img')].slice(0, 200)) {
    if (!vis(img)) continue;
    if (img.complete && img.naturalWidth === 0) { add('DS-IMG-003', 'T1', 'A broken image', img, 'the image did not load (src ' + (img.getAttribute('src') || '').slice(0, 60) + ')'); continue; }
    const r = img.getBoundingClientRect(), fit = getComputedStyle(img).objectFit;
    if (img.naturalWidth > 0 && (fit === 'fill' || fit === 'unset' || fit === '') && r.width > 4 && r.height > 4) {
      const nat = img.naturalWidth / img.naturalHeight, ren = r.width / r.height;
      if (Math.abs(nat - ren) / nat > 0.12) add('DS-IMG-001', 'T1', 'A stretched image', img, 'the image is stretched out of its real shape');
    }
  }

  // DS-CONTENT-001 placeholder or debug text, DS-CONTENT-002 a raw error on the page
  const bodyText = (document.body ? document.body.innerText : '').slice(0, 40000);
  const junk = ['lorem ipsum', 'dolor sit amet', 'undefined', 'null', 'NaN', '[object Object]', '{{', '}}', 'TODO', 'FIXME', 'placeholder text'];
  for (const w of junk) { const i = bodyText.toLowerCase().indexOf(w.toLowerCase());
    if (i >= 0) { add('DS-CONTENT-001', 'T1', 'Placeholder or debug text is visible', null, 'the page shows "' + bodyText.slice(i, i + 40).replace(/\s+/g, ' ') + '"'); break; } }
  const errs = ['Traceback (most recent call last)', 'ReferenceError', 'TypeError:', 'Cannot read properties of', 'SQLSTATE', 'Warning: ', 'Fatal error', 'Uncaught '];
  for (const w of errs) { const i = bodyText.indexOf(w);
    if (i >= 0) { add('DS-CONTENT-002', 'T0', 'A raw program error is on the page', null, 'the page shows "' + bodyText.slice(i, i + 60).replace(/\s+/g, ' ') + '"'); break; } }

  // DS-ALIGN-002 a row of items that clearly does not line up (a real break, not a near miss)
  const groups = {};
  for (const el of inter) { const p = el.parentElement; if (!p) continue;
    const r = el.getBoundingClientRect(); if (r.width < 8 || r.height < 8) continue;
    (groups[p.tagName + '@' + Math.round(r.top / 60)] = groups[p.tagName + '@' + Math.round(r.top / 60)] || []).push(r); }
  for (const k in groups) { const rs = groups[k]; if (rs.length < 3) continue;
    const tops = rs.map(r => r.top), span = Math.max(...tops) - Math.min(...tops);
    if (span >= 10 && span < 40) { add('DS-ALIGN-002', 'T1', 'A row of items does not line up', null, Math.round(span) + 'px out of line across ' + rs.length + ' items in a row'); break; } }

  return out;
}
"""

AXE_JS = r"""
async () => {
  if (!window.axe) return null;
  try {
    const r = await window.axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] }, resultTypes: ['violations'] });
    return r.violations.filter(v => v.impact === 'critical' || v.impact === 'serious')
      .map(v => ({ id: v.id, impact: v.impact, help: v.help, n: v.nodes.length,
                   where: (v.nodes[0] && v.nodes[0].target || []).join(' ').slice(0, 80) }));
  } catch (e) { return null; }
}
"""


class DesignChecker:
    """Open a browser, visit the main mapped pages, measure the design rules, and post code results."""

    def __init__(self, site_url: str, board: TaskBoard, sitemap: SiteMap, out_dir: str, *,
                 user_storage=None, headless: bool = True, log=print, budget_s: float = 240.0,
                 max_pages: int = 12, guard=None):
        self.site = site_url.rstrip("/") + "/"
        self.host = (urlparse(site_url).hostname or "").lower()
        self.board, self.map, self.out, self.log = board, sitemap, out_dir, log
        self.user_storage, self.headless, self.budget_s = user_storage, headless, budget_s
        self.max_pages, self.guard = max_pages, guard
        self.t0 = time.monotonic()
        self.done = threading.Event()
        self.counts = {"PASS": 0, "FAIL": 0}
        self.seen_rule: set = set()                 # a site-wide rule is reported one time
        self._shots = 0
        os.makedirs(os.path.join(out_dir, "design"), exist_ok=True)

    def _pages(self) -> list:
        """The pages worth a design pass: logged-in first, then one per path, highest risk first."""
        best = {}
        for p in self.map.pages.values():
            if not p.status or p.status >= 400:
                continue
            cur = best.get(p.path)
            if cur is None or (p.auth == "user" and cur.auth != "user"):
                best[p.path] = p
        order = sorted(best.values(), key=lambda p: (p.auth != "user", p.depth))
        return order[: self.max_pages]

    def _shot(self, page, route: str) -> str:
        self._shots += 1
        import re
        name = f"{self._shots:03d}_{re.sub(r'[^a-zA-Z0-9]+', '_', route).strip('_') or 'home'}.png"
        path = os.path.join(self.out, "design", name[:110])
        try:
            page.screenshot(path=path, timeout=8000)
            return path
        except Exception:
            return "none"

    def _post(self, route: str, path: str, auth: str, url: str, shot: str, findings: list):
        """One result per page: FAIL with the findings, or PASS when the page is clean. Site-wide rules once."""
        fresh = []
        for f in findings:
            rule = f.get("rule") or f.get("id")
            # an accessibility rule on the same element (usually the nav or footer) repeats on every page:
            # report it one time. The measured layout rules stay per page.
            key = (rule, f.get("where", "")) if str(rule).startswith("DS-A11Y-") else None
            if key is not None and key in self.seen_rule:
                continue
            if key is not None:
                self.seen_rule.add(key)
            fresh.append(f)
        proof = {"screenshot": shot or "none", "url": url, "server_responses": []}
        tail = " (logged in)" if auth == "user" else ""
        if fresh:
            worst = min((SEVERITY_RISK.get(f.get("sev", "T1"), 2) for f in fresh), default=2)
            risk = max(SEVERITY_RISK.get(f.get("sev", "T1"), 2) for f in fresh)
            lines = [f"{f.get('title', f.get('help', 'issue'))}: {f.get('what', f.get('where', ''))}".strip() for f in fresh[:6]]
            self.board.add_result("code", f"Design check on {route}{tail}", "FAIL",
                                  {**proof, "design": fresh}, reason=f"{route}: " + "; ".join(lines),
                                  expected="The page has no overlap, cut-off text, sideways scroll or unreadable text.",
                                  page=path, user_state=auth, kind="design:rules", risk=risk,
                                  steps=[f"open {url}", "measure the rendered page"], source="design")
            self.counts["FAIL"] += 1
            self.log(f"  design {route}{tail}: {len(fresh)} issue(s): " + "; ".join(lines[:2]))
        else:
            self.board.add_result("code", f"Design check on {route}{tail}", "PASS", proof,
                                  reason=f"{route}: no measured design problem.",
                                  expected="The page has no overlap, cut-off text, sideways scroll or unreadable text.",
                                  page=path, user_state=auth, kind="design:rules", risk=1, source="design")
            self.counts["PASS"] += 1

    def run(self):
        from playwright.sync_api import sync_playwright
        try:
            axe_src = open(AXE_PATH, encoding="utf-8").read() if os.path.exists(AXE_PATH) else None
        except Exception:
            axe_src = None
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self.headless)
                ctx = browser.new_context(viewport={"width": 1280, "height": 800}, ignore_https_errors=True,
                                          storage_state=self.user_storage)
                if self.guard:
                    ctx.route("**/*", self.guard)
                page = ctx.new_page()
                for p in self._pages():
                    if time.monotonic() - self.t0 > self.budget_s:
                        self.log("  design checks: time limit reached")
                        break
                    try:
                        page.goto(p.url, wait_until="domcontentloaded", timeout=15000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=2500)
                        except Exception:
                            pass
                        page.mouse.wheel(0, 2000)
                        page.wait_for_timeout(250)
                        findings = page.evaluate(DESIGN_JS) or []
                        if axe_src:
                            try:
                                page.evaluate(axe_src)
                                ax = page.evaluate(AXE_JS) or []
                                for a in ax[:6]:
                                    findings.append({"rule": "DS-A11Y-" + a["id"], "sev": "T1" if a["impact"] == "serious" else "T0",
                                                     "title": a["help"], "what": f"{a['n']} element(s); first: {a['where']}", "where": a["where"]})
                            except Exception:
                                pass
                        shot = self._shot(page, p.route)
                        self._post(p.route, p.path, p.auth, page.url, shot, findings)
                    except Exception as e:
                        self.log(f"  design {p.route}: could not check ({type(e).__name__})")
                browser.close()
        except Exception as e:
            self.log(f"  design checks stopped: {type(e).__name__}: {str(e)[:120]}")
        finally:
            self.done.set()
            self.log(f"  design checks done in {time.monotonic() - self.t0:.0f}s: "
                     f"{self.counts['FAIL']} page(s) with issues, {self.counts['PASS']} clean")
