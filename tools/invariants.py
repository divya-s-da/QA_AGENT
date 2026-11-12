"""Invariant checks — property-based rules verified automatically on every
page state (failure-inversion architecture, section 12).

These are global rules, not per-test-case: run check_page_invariants() after
each major navigation/state change and record the result as evidence.
"""

import json

from .browser_server import CDP_URL
from .native_browser import _with_page


_CHECK_JS = """
() => {
  const out = [];
  // 1. Broken images
  let brokenImgs = 0;
  document.querySelectorAll('img').forEach(img => {
    if (img.complete && img.naturalWidth === 0) brokenImgs++;
  });
  if (brokenImgs > 0) out.push({rule: 'no_broken_images', pass: false, detail: brokenImgs + ' broken image(s)'});

  // 2. Unlabeled form fields
  const unlabeled = [];
  document.querySelectorAll('input, select, textarea').forEach(el => {
    if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button' || el.type === 'checkbox' || el.type === 'radio') return;
    const id = el.id;
    const labeled = el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || (id && document.querySelector('label[for="' + id + '"]'))
      || (el.closest('label'));
    if (!labeled) unlabeled.push(el.tagName + (el.name ? '#' + el.name : ''));
  });
  if (unlabeled.length > 0) out.push({rule: 'form_fields_labeled', pass: false, detail: 'unlabeled: ' + unlabeled.slice(0, 5).join(', ')});

  // 3. Horizontal overflow (content wider than viewport)
  const doc = document.documentElement;
  if (doc.scrollWidth > doc.clientWidth + 2) {
    out.push({rule: 'no_horizontal_overflow', pass: false, detail: 'scrollWidth=' + doc.scrollWidth + ' clientWidth=' + doc.clientWidth});
  }

  // 4. Page has a non-empty title
  if (!document.title || !document.title.trim()) {
    out.push({rule: 'page_has_title', pass: false, detail: 'empty title'});
  }

  // 5. Links with empty href
  document.querySelectorAll('a[href=""]').forEach(() => {});
  const emptyHrefs = document.querySelectorAll('a[href=""]').length;
  if (emptyHrefs > 0) out.push({rule: 'no_empty_hrefs', pass: false, detail: emptyHrefs + ' link(s) with empty href'});

  return JSON.stringify(out);
}
"""


def check_page_invariants() -> str:
    """Check global invariants on the current page (broken images, unlabeled
    fields, horizontal overflow, missing title, empty hrefs).

    Returns compact JSON of violated rules; an empty array means all
    invariants hold. Call after every major navigation and record the result.
    """
    def _run(page):
        try:
            raw = page.evaluate(_CHECK_JS)
            violations = json.loads(raw)
        except Exception as e:
            return json.dumps([{"rule": "invariant_check_failed", "pass": False, "detail": str(e)[:200]}])
        if not violations:
            return json.dumps([])
        return json.dumps(violations)
    return _with_page(_run)
