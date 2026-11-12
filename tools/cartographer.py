"""Cartographer (failure-inversion §2) — builds the system model before testing.

Produces a state graph: nodes = (route, auth_state, DOM-signature-hash), edges =
the concrete action causing the transition. DOM signature captures SPA state
changes that don't change the URL. Fully deterministic (no LLM).
"""

import hashlib
import json
import os

from .browser_server import CDP_URL
from .native_browser import _find_page


def _dom_signature(page) -> str:
    """Hash the set of interactive elements + visible text — changes when the
    SPA changes state without changing the URL."""
    js = """
    () => {
      const els = [...document.querySelectorAll('a, button, input, select, textarea, [role]')];
      const sig = els.map(el => (el.tagName + ':' + (el.getAttribute('role') || '')
        + ':' + (el.getAttribute('href') || '') + ':' + ((el.textContent || '').trim().slice(0, 40)))).join('|');
      const title = document.title;
      return title + '::' + sig;
    }
    """
    raw = page.evaluate(js)
    return hashlib.sha1(str(raw).encode("utf-8", errors="replace")).hexdigest()[:16]


def _collect_edges(page, route: str) -> list[dict]:
    """Collect candidate edges: clickable elements with href or text (bounded)."""
    js = """
    () => {
      const out = [];
      document.querySelectorAll('a[href]').forEach(a => {
        const href = a.getAttribute('href') || '';
        if (href.startsWith('#') || href.startsWith('mailto') || href.startsWith('tel')) return;
        out.push({kind: 'link', label: (a.textContent || '').trim().slice(0, 50), href});
      });
      return JSON.stringify(out.slice(0, 30));
    }
    """
    try:
        return json.loads(page.evaluate(js))
    except Exception:
        return []


async def build_state_graph(site_url: str, max_nodes: int = 12, max_depth: int = 3) -> dict:
    """Deterministic breadth-first crawl over the live site (async Playwright).

    Returns {"nodes": [...], "edges": [...], "entry_points": [...]}.
    """
    from playwright.async_api import async_playwright
    nodes = {}
    edges = []
    queue = [("/", 0)]
    nodes["/"] = {"route": "/", "auth_state": "unknown", "dom_sig": None}
    visited_routes = set()

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        page = await _find_page(browser)
        while queue and len(nodes) < max_nodes:
            route, depth = queue.pop(0)
            if depth > max_depth or route in visited_routes:
                continue
            visited_routes.add(route)
            url = site_url.rstrip("/") + route
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(800)
            except Exception as e:
                nodes[route]["dom_sig"] = f"error:{str(e)[:60]}"
                continue
            sig = _dom_signature(page)
            nodes[route]["dom_sig"] = sig
            for edge in _collect_edges(page, route):
                href = edge["href"]
                if not href.startswith("/"):
                    continue
                target = href.split("?")[0].split("#")[0]
                edges.append({"from": route, "to": target, "action": f"click[{edge['label']}]", "kind": "link"})
                if target not in nodes and len(nodes) < max_nodes:
                    nodes[target] = {"route": target, "auth_state": "unknown", "dom_sig": None}
                    queue.append((target, depth + 1))
        # Deduplicate edges
        seen = set()
        uniq = []
        for e in edges:
            k = (e["from"], e["to"], e["action"])
            if k not in seen:
                seen.add(k)
                uniq.append(e)

    return {
        "nodes": list(nodes.values()),
        "edges": uniq,
        "entry_points": ["/"],
        "site_url": site_url,
    }


def save_state_graph(graph: dict, path: str = None) -> str:
    if path is None:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "state_graph.json"
        )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
    return path


def load_state_graph(path: str = None) -> dict | None:
    if path is None:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "state_graph.json"
        )
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
