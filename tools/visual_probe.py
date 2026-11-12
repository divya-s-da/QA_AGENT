"""Measurable substitutes for eyesight.

The navigator cannot see. take_screenshot returns a file path, the Playwright
MCP runs with --image-responses omit, and no tool hands image bytes to the
model — so asking it to "look at the screenshot for layout problems" asks for
something it physically cannot do, and it answers from the DOM instead.

That blindness is not incidental. In one manual pass a human found sixteen
defects the agent missed, and five were purely visual: three misaligned
tables, an invisible chart, and a refresh button the same colour as the panel
behind it. None of them are visible in an accessibility tree.

Turning images back on is the obvious fix and the wrong one here: a full-page
screenshot is enormous, this agent already had to be rescued from
ContextWindowExceededError, and a model squinting at a JPEG *guesses* at a
contrast ratio. So instead of vision, this measures — geometry and computed
style, straight from the page:

    overlapping text            two text boxes sharing pixels
    clipped text                scrollWidth beyond clientWidth
    invisible controls          contrast against their own background
    empty charts                svg/canvas with no drawn content
    misaligned columns          cell edges that do not line up with headers
    offscreen content           elements outside the viewport box

Every finding names the element and the number that proves it, so it can be
cited in evidence rather than described.
"""

import json

from .native_browser import _with_page


# WCAG relative luminance and contrast ratio, computed in the page so the
# values come from the real rendered styles, not from a model's guess.
_PROBE_JS = r"""
() => {
  const out = [];
  const MAXH = 40;                        // cap findings; a broken page has many

  const num = (v) => parseFloat(v) || 0;
  const parseColor = (c) => {
    const m = String(c).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
  };
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
    return 0.2126*f(c.r) + 0.7152*f(c.g) + 0.0722*f(c.b);
  };
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1,l2) + 0.05) / (Math.min(l1,l2) + 0.05);
  };
  // Walk up for the first non-transparent background — what the eye sees behind.
  const bgOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parseColor(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.1) return c;
      n = n.parentElement;
    }
    return {r:255, g:255, b:255, a:1};
  };
  const visible = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || num(s.opacity) < 0.05) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const label = (el) => {
    const id = el.id ? '#' + el.id : '';
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).slice(0,2).join('.') : '';
    const txt = (el.textContent || '').trim().replace(/\s+/g,' ').slice(0,40);
    return (el.tagName.toLowerCase() + id + cls + (txt ? ' "' + txt + '"' : '')).slice(0,90);
  };
  const push = (rule, detail, el) => {
    if (out.length < MAXH) out.push({rule, element: label(el), detail});
  };

  // 1. Controls invisible against their own background (WCAG AA needs 4.5:1
  //    for text, 3:1 for large text and UI boundaries).
  document.querySelectorAll('button, a, [role="button"], input, select, .btn').forEach(el => {
    if (!visible(el)) return;
    const s = getComputedStyle(el);
    const fg = parseColor(s.color); if (!fg) return;
    const bg = bgOf(el);
    const r = ratio(fg, bg);
    const size = num(s.fontSize);
    const large = size >= 24 || (size >= 18.66 && num(s.fontWeight) >= 700);
    const need = large ? 3 : 4.5;
    if ((el.textContent || '').trim() && r < need) {
      push('control_low_contrast',
           'contrast ' + r.toFixed(2) + ':1 against its background, needs ' + need + ':1', el);
    }
    // A control whose own surface matches the panel behind it reads as absent
    // even when its label is legible - the "refresh button vanishes" defect.
    const own = parseColor(s.backgroundColor);
    if (own && own.a > 0.1) {
      const parent = el.parentElement ? bgOf(el.parentElement) : null;
      if (parent && Math.abs(lum(own) - lum(parent)) < 0.008 &&
          num(s.borderWidth) === 0 && s.borderStyle === 'none') {
        push('control_indistinct_from_background',
             'control surface is the same tone as the panel behind it and it has no border', el);
      }
    }
  });

  // 2. Text clipped by its container.
  document.querySelectorAll('td, th, li, p, span, div, h1, h2, h3, label').forEach(el => {
    if (!visible(el) || el.children.length > 0) return;
    if (!(el.textContent || '').trim()) return;
    const s = getComputedStyle(el);
    if (s.overflow === 'visible' && s.textOverflow !== 'ellipsis') return;
    if (el.scrollWidth > el.clientWidth + 2 && s.textOverflow !== 'ellipsis') {
      push('text_clipped', 'scrollWidth ' + el.scrollWidth + ' > clientWidth ' + el.clientWidth, el);
    }
  });

  // 3. Charts that render a container but never draw anything.
  document.querySelectorAll('svg, canvas').forEach(el => {
    if (!visible(el)) return;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 40) return;         // icons are not charts
    if (el.tagName.toLowerCase() === 'svg') {
      const marks = el.querySelectorAll('path, rect, circle, line, polyline, polygon, text');
      let drawn = 0;
      marks.forEach(m => {
        const b = m.getBoundingClientRect();
        const ms = getComputedStyle(m);
        if (b.width > 1 && b.height > 1 && ms.visibility !== 'hidden' && num(ms.opacity) > 0.05) drawn++;
      });
      if (drawn === 0) {
        push('chart_area_empty',
             Math.round(r.width) + 'x' + Math.round(r.height) + ' svg with no visible marks', el);
      }
    } else {
      try {
        const ctx = el.getContext('2d');
        if (ctx) {
          const w = Math.min(el.width || r.width, 300), h = Math.min(el.height || r.height, 300);
          const d = ctx.getImageData(0, 0, Math.max(w,1), Math.max(h,1)).data;
          let ink = 0;
          for (let i = 3; i < d.length; i += 4 * 37) if (d[i] > 8) { ink++; if (ink > 3) break; }
          if (ink <= 3) {
            push('chart_area_empty',
                 Math.round(r.width) + 'x' + Math.round(r.height) + ' canvas with no drawn pixels', el);
          }
        }
      } catch (e) { /* tainted canvas - cannot judge, say nothing */ }
    }
  });

  // 4. Table columns whose cells do not line up with their header.
  document.querySelectorAll('table').forEach(tbl => {
    if (!visible(tbl)) return;
    const heads = Array.from(tbl.querySelectorAll('thead th, thead td'));
    const firstRow = tbl.querySelector('tbody tr');
    if (!heads.length || !firstRow) return;
    const cells = Array.from(firstRow.children);
    if (cells.length !== heads.length) {
      push('table_column_count_mismatch',
           heads.length + ' header cells but ' + cells.length + ' cells in the first body row', tbl);
      return;
    }
    heads.forEach((h, i) => {
      const cell = cells[i];
      const hs = getComputedStyle(h), cs = getComputedStyle(cell);
      const a = h.getBoundingClientRect(), b = cell.getBoundingClientRect();
      // Compare where the TEXT starts, not where the box starts: a cell padded
      // 60px in still has a box flush with its header, yet reads as misaligned.
      const contentLeft = (r, st) => r.left + num(st.borderLeftWidth) + num(st.paddingLeft);
      const contentRight = (r, st) => r.right - num(st.borderRightWidth) - num(st.paddingRight);
      const norm = (v) => (v === 'start' ? 'left' : v === 'end' ? 'right' : v);
      const ha = norm(hs.textAlign), ca = norm(cs.textAlign);
      // Right-aligned numbers under a left-aligned header is a deliberate,
      // extremely common pattern - only compare like with like.
      // th defaults to center and td to left in every browser; flagging that
      // pairing would fire on every unstyled table and drown the real findings.
      const uaDefault = (ha === 'center' && ca === 'left');
      if (ha !== ca && !uaDefault && !(ha === 'left' && ca === 'right')) {
        push('table_column_alignment_differs',
             'column ' + (i+1) + ' ("' + (h.textContent||'').trim().slice(0,24) +
             '") header is ' + ha + '-aligned but its cells are ' + ca + '-aligned', tbl);
        return;
      }
      if (ha !== ca) return;
      let dx, where;
      if (ha === 'right') { dx = Math.abs(contentRight(a, hs) - contentRight(b, cs)); where = 'end'; }
      else if (ha === 'center') { dx = Math.abs((a.left+a.right)/2 - (b.left+b.right)/2); where = 'centre'; }
      else { dx = Math.abs(contentLeft(a, hs) - contentLeft(b, cs)); where = 'start'; }
      if (dx > 8) {
        push('table_column_misaligned',
             'column ' + (i+1) + ' ("' + (h.textContent||'').trim().slice(0,24) +
             '") header text ' + where + 's ' + Math.round(dx) +
             'px away from where its cell text ' + where + 's', tbl);
      }
    });
  });

  // 5. Text boxes physically overlapping other text.
  const texts = Array.from(document.querySelectorAll('h1,h2,h3,h4,p,span,td,th,label,a,button'))
    .filter(el => visible(el) && el.children.length === 0 && (el.textContent||'').trim().length > 2)
    .slice(0, 260);
  for (let i = 0; i < texts.length; i++) {
    const a = texts[i].getBoundingClientRect();
    if (a.width < 4 || a.height < 4) continue;
    for (let j = i + 1; j < texts.length; j++) {
      if (texts[i].contains(texts[j]) || texts[j].contains(texts[i])) continue;
      const b = texts[j].getBoundingClientRect();
      const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (ox > 4 && oy > 4) {
        const area = ox * oy, small = Math.min(a.width*a.height, b.width*b.height);
        if (small > 0 && area / small > 0.25) {
          push('text_overlap',
               'overlaps ' + label(texts[j]) + ' by ' + Math.round(ox) + 'x' + Math.round(oy) + 'px',
               texts[i]);
        }
        break;
      }
    }
  }

  // 6. Visible content sitting outside the viewport box.
  const vw = document.documentElement.clientWidth;
  document.querySelectorAll('body *').forEach(el => {
    if (out.length >= MAXH || !visible(el) || el.children.length > 0) return;
    if (!(el.textContent || '').trim()) return;
    const r = el.getBoundingClientRect();
    if (r.left < -12 || r.right > vw + 12) {
      push('content_outside_viewport',
           'spans x=' + Math.round(r.left) + '..' + Math.round(r.right) + ' in a ' + vw + 'px viewport', el);
    }
  });

  return JSON.stringify(out);
}
"""


def visual_probe() -> str:
    """Measure layout and contrast defects on the current page.

    Use this as the mandatory visual check for every screen. You cannot see the
    screenshot you capture, so this is how you inspect a page's appearance: it
    reports, with numbers, the defects a human notices at a glance —

      control_low_contrast              text on a control fails WCAG contrast
      control_indistinct_from_background  a control that blends into its panel
      text_clipped                      text cut off by its container
      chart_area_empty                  a chart that drew nothing
      table_column_misaligned           cells that do not line up with headers
      table_column_count_mismatch       header and body row disagree
      text_overlap                      two pieces of text sharing pixels
      content_outside_viewport          content pushed off the page edge

    Returns compact JSON; an empty array means no defect was measurable. Every
    entry names the element and the measurement, so record failures with the
    number quoted (e.g. "contrast 1.14:1, needs 4.5:1"), not as "looks wrong".
    An empty result is evidence about layout only — it says nothing about
    whether the page's DATA is correct.
    """
    def _run(page):
        try:
            raw = page.evaluate(_PROBE_JS)
            findings = json.loads(raw)
        except Exception as e:
            return json.dumps([{"rule": "visual_probe_failed", "detail": str(e)[:200]}])
        return json.dumps(findings)

    return _with_page(_run)
