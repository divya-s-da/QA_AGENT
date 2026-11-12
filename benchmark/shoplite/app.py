"""ShopLite: a small shop web app with planted bugs, used to test the tester.

Run:
    python benchmark/shoplite/app.py --port 8765 --bugs all     # every bug on
    python benchmark/shoplite/app.py --port 8765 --bugs none    # clean build
    python benchmark/shoplite/app.py --bugs B01,B08             # only these

The bug IDs, tiers and descriptions live in bugs.yaml next to this file. Each
planted bug is guarded by `bug("Bxx")`; with --bugs none the app is meant to be
correct, so any FAIL the agent reports on the clean build is a false fail.

Standard library only, in-memory state. POST /__bench/reset restores the seed
data (the benchmark runner calls it before each run).

Test accounts:
    bob@shoplite.test / bob-pass-123     regular user (use this for the agent)
    alice@shoplite.test / alice-pass-123 admin
"""

import argparse
import html
import json
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ALL_BUGS = {f"B{i:02d}" for i in range(1, 23)}
ACTIVE_BUGS: set[str] = set()


def bug(bug_id: str) -> bool:
    return bug_id in ACTIVE_BUGS


# ─── data ────────────────────────────────────────────────────────────────

PRODUCTS = [
    {"id": 1, "name": "Canvas Tote", "price": 20.00, "img": "tote"},
    {"id": 2, "name": "Steel Bottle", "price": 100.00, "img": "bottle"},
    {"id": 3, "name": "Desk Lamp", "price": 45.00, "img": "lamp"},
    {"id": 4, "name": "Notebook Set", "price": 9.50, "img": "notebook"},
    {"id": 5, "name": "Wool Socks", "price": 12.00, "img": "socks"},
    {"id": 6, "name": "Coffee Mug", "price": 15.00, "img": "mug"},
]
PAGE_SIZE = 4

_lock = threading.Lock()
DB: dict = {}


def reset_db() -> None:
    with _lock:
        DB.clear()
        DB.update({
            "users": {
                "bob@shoplite.test": {"password": "bob-pass-123", "name": "Bob Tester", "role": "user"},
                "alice@shoplite.test": {"password": "alice-pass-123", "name": "Alice Admin", "role": "admin"},
                # more regular test users, so each tester of a run has its own account and data
                **{f"{n}@shoplite.test": {"password": f"{n}-pass-123", "name": f"{n.title()} Tester", "role": "user"}
                   for n in ("carol", "dave", "erin", "frank")},
            },
            "sessions": {},          # sid -> email
            "carts": {},             # email -> {product_id: qty}
            "coupons": {},           # email -> code
            "orders": [],            # {id, email, items, total}
            "next_order": 1001,
            "reviews": {},           # product_id -> [text]
            "messages": [],
        })
        # Bob starts with one past order so the orders page has content.
        DB["orders"].append({"id": 1000, "email": "bob@shoplite.test",
                             "items": {3: 1}, "total": 45.00})


def product(pid):
    return next((p for p in PRODUCTS if p["id"] == pid), None)


def cart_total(email: str) -> float:
    cart = DB["carts"].get(email, {})
    total = 0.0
    for pid, qty in cart.items():
        p = product(pid)
        if not p:
            continue
        total += p["price"] if bug("B02") else p["price"] * qty
    if DB["coupons"].get(email) == "SAVE10" and not bug("B14"):
        total *= 0.9
    return round(total, 2)


# ─── HTML ────────────────────────────────────────────────────────────────

STYLE = """
body{font-family:system-ui,sans-serif;margin:0;color:#222}
header{background:#1d3557;color:#fff;padding:12px 24px;display:flex;gap:18px;align-items:center}
header a{color:#fff;text-decoration:none}
main{padding:24px;max-width:960px;margin:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}
.card{border:1px solid #ddd;border-radius:8px;padding:12px}
.card img{width:100%;height:120px;object-fit:cover;background:#eee}
button{background:#e63946;color:#fff;border:0;padding:8px 14px;border-radius:6px;cursor:pointer}
button.secondary{background:#888}
.toast{position:fixed;bottom:20px;right:20px;background:#2a9d8f;color:#fff;padding:12px 18px;border-radius:6px;display:none}
.toast.error{background:#e63946}
.error{color:#c1121f}
footer{padding:24px;border-top:1px solid #ddd;margin-top:40px;color:#666}
.tabs button{background:#ccc;color:#222}
.tabs button[aria-selected=true]{background:#1d3557;color:#fff}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.4);display:none;align-items:center;justify-content:center}
.modal .box{background:#fff;padding:24px;border-radius:8px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px;text-align:left}
"""

TOAST_JS = """
function toast(msg, isError){
  const t=document.getElementById('toast'); t.textContent=msg;
  t.className='toast'+(isError?' error':''); t.style.display='block';
  t.setAttribute('role', isError?'alert':'status');
  setTimeout(()=>{t.style.display='none'},4000);
}
async function api(method, url, body){
  const r = await fetch(url,{method,headers:{'Content-Type':'application/json'},
    body: body===undefined?undefined:JSON.stringify(body)});
  let data={}; try{data=await r.json()}catch(e){}
  return {ok:r.ok,status:r.status,data};
}
"""


def page(title: str, body: str, user=None, script: str = "") -> str:
    if user:
        auth = (f'<span>Hi, {html.escape(user["name"])}</span>'
                '<button class="secondary" id="logout-btn" onclick="logout()">Log out</button>')
    else:
        auth = '<a href="/login">Log in</a> <a href="/register">Register</a>'
    footer_help = '<a href="">Help</a>' if bug("B21") else '<a href="/contact">Help</a>'
    logout_js = (
        # B05: the client forgets the user but never ends the server session.
        "function logout(){location.href='/login'}" if bug("B05") else
        "async function logout(){await api('POST','/api/logout');location.href='/login'}"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{STYLE}</style></head>
<body>
<header><strong><a href="/">ShopLite</a></strong>
<nav aria-label="Main"><a href="/">Shop</a> · <a href="/cart">Cart</a> · <a href="/orders">Orders</a> ·
<a href="/account">Account</a> · <a href="/admin">Admin</a> · <a href="/contact">Contact</a> · <a href="/about">About</a></nav>
<span style="margin-left:auto">{auth}</span></header>
<main>{body}</main>
<footer>© ShopLite · {footer_help} · <a href="/about">About us</a></footer>
<div id="toast" class="toast" aria-live="polite"></div>
<script>{TOAST_JS}{logout_js}{script}</script>
</body></html>"""


def img_src(p) -> str:
    # B15: one product image points at a file that does not exist.
    if bug("B15") and p["id"] == 3:
        return "/static/img/lamp-large.png"
    return f"/static/img/{p['img']}.svg"


def svg_placeholder(name: str) -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120">'
            f'<rect width="200" height="120" fill="#a8dadc"/>'
            f'<text x="100" y="65" font-size="16" text-anchor="middle">{html.escape(name)}</text></svg>').encode()


# ─── pages ───────────────────────────────────────────────────────────────

def home_page(user, query):
    q = (query.get("q") or [""])[0].strip()
    sort = (query.get("sort") or [""])[0]
    try:
        pg = max(1, int((query.get("page") or ["1"])[0]))
    except ValueError:
        pg = 1
    items = list(PRODUCTS)
    if q and not bug("B06"):
        items = [p for p in items if q.lower() in p["name"].lower()]
    if sort == "price":
        if bug("B10"):
            items.sort(key=lambda p: f"${p['price']:.2f}")   # sorts as text
        else:
            items.sort(key=lambda p: p["price"])
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = 0 if (bug("B09") and pg > 1) else (pg - 1) * PAGE_SIZE
    shown = items[start:start + PAGE_SIZE]
    cards = "".join(
        f'<div class="card"><img src="{img_src(p)}" alt="{html.escape(p["name"])}">'
        f'<h3><a href="/product/{p["id"]}">{html.escape(p["name"])}</a></h3>'
        f'<p class="price">${p["price"]:.2f}</p>'
        f'<button onclick="addToCart({p["id"]})">Add to cart</button></div>'
        for p in shown
    ) or "<p>No products match your search.</p>"
    pager = " ".join(
        f'<a href="/?page={n}&q={html.escape(q)}&sort={html.escape(sort)}"'
        f'{" aria-current=page" if n == pg else ""}>Page {n}</a>'
        for n in range(1, pages + 1)
    )
    newsletter_input = ('<input id="nl" type="email" placeholder="">' if bug("B16") else
                        '<label for="nl">Email for our newsletter</label> '
                        '<input id="nl" type="email" placeholder="you@example.com">')
    body = f"""<h1>Shop</h1>
<form method="get" action="/" role="search">
  <label for="q">Search products</label> <input id="q" name="q" value="{html.escape(q)}">
  <label for="sort">Sort</label>
  <select id="sort" name="sort"><option value="">Featured</option>
  <option value="price"{" selected" if sort == "price" else ""}>Price: low to high</option></select>
  <button type="submit">Search</button>
</form>
<p>{len(items)} product(s)</p>
<div class="grid" id="products">{cards}</div>
<nav aria-label="Pagination">{pager}</nav>
<section><h2>Newsletter</h2>{newsletter_input}
<button onclick="toast('Thanks for subscribing')">Subscribe</button></section>"""
    script = """
async function addToCart(id){
  const r = await api('POST','/api/cart/add',{product_id:id});
  if(r.status===401){location.href='/login';return}
  toast(r.ok?'Added to cart':'Could not add to cart', !r.ok);
}"""
    if bug("B20"):
        script += "\nwindow.addEventListener('load',()=>{ window.shopConfig.currency.toUpperCase(); });"
    return page("ShopLite - Shop", body, user, script)


def product_page(user, pid):
    p = product(pid)
    reviews = DB["reviews"].get(pid, [])
    review_list = "".join(f"<li>{html.escape(r)}</li>" for r in reviews) or "<li>No reviews yet.</li>"
    tab_js = ("function showTab(n){}" if bug("B12") else """
function showTab(n){
  for(const t of ['desc','reviews']){
    document.getElementById('tab-'+t).setAttribute('aria-selected', t===n);
    document.getElementById('panel-'+t).hidden = t!==n;
  }
}""")
    submit_js = """
let reviewBusy=false;
async function addReview(e){
  e.preventDefault();
  const text=document.getElementById('review').value.trim();
  if(!text){toast('Please write a review first',true);return}
""" + ("" if bug("B11") else "  if(reviewBusy)return; reviewBusy=true; document.getElementById('review-btn').disabled=true;\n") + f"""
  const r=await api('POST','/api/reviews',{{product_id:{pid},text}});
  if(r.ok){{location.reload()}}else{{toast('Could not save review',true); reviewBusy=false;}}
}}
async function addToCart(id){{
  const r = await api('POST','/api/cart/add',{{product_id:id}});
  if(r.status===401){{location.href='/login';return}}
  toast(r.ok?'Added to cart':'Could not add to cart', !r.ok);
}}"""
    body = f"""<h1>{html.escape(p["name"])}</h1>
<img src="{img_src(p)}" alt="{html.escape(p["name"])}" width="300">
<p class="price">${p["price"]:.2f}</p>
<button onclick="addToCart({p["id"]})">Add to cart</button>
<div class="tabs" role="tablist">
  <button role="tab" id="tab-desc" aria-selected="true" onclick="showTab('desc')">Description</button>
  <button role="tab" id="tab-reviews" aria-selected="false" onclick="showTab('reviews')">Reviews ({len(reviews)})</button>
</div>
<section id="panel-desc" role="tabpanel"><p>A well made {html.escape(p["name"].lower())} for everyday use.</p></section>
<section id="panel-reviews" role="tabpanel" hidden>
  <ul id="review-list">{review_list}</ul>
  <form onsubmit="addReview(event)"><label for="review">Your review</label>
  <textarea id="review"></textarea><button id="review-btn" type="submit">Post review</button></form>
</section>"""
    return page(f"ShopLite - {p['name']}", body, user, tab_js + submit_js)


def cart_page(user):
    cart = DB["carts"].get(user["email"], {})
    rows = "".join(
        f'<tr><td>{html.escape(product(pid)["name"])}</td><td>${product(pid)["price"]:.2f}</td>'
        f'<td><label for="qty-{pid}" class="sr">Quantity</label>'
        f'<input id="qty-{pid}" type="number" min="1" value="{qty}" onchange="setQty({pid}, this.value)"></td>'
        f'<td><button class="secondary" onclick="removeItem({pid})">Remove</button></td></tr>'
        for pid, qty in cart.items() if product(pid)
    )
    coupon = DB["coupons"].get(user["email"], "")
    continue_href = "/shop" if bug("B13") else "/"
    if rows:
        content = f"""<table><thead><tr><th>Item</th><th>Price</th><th>Qty</th><th></th></tr></thead>
<tbody>{rows}</tbody></table>
<p>Total: <strong id="total">${cart_total(user["email"]):.2f}</strong>{" (coupon " + html.escape(coupon) + ")" if coupon else ""}</p>
<form onsubmit="applyCoupon(event)"><label for="coupon">Coupon code</label>
<input id="coupon" placeholder="SAVE10"><button type="submit">Apply</button></form>
<p><button id="place-order" onclick="placeOrder()">Place order</button></p>"""
    else:
        content = "<p>Your cart is empty.</p>"
    body = f'<h1>Your cart</h1>{content}<p><a href="{continue_href}">Continue shopping</a></p>'
    place_js = ("""
async function placeOrder(){
  api('POST','/api/orders');
  toast('Order placed! Thank you.');
}""" if bug("B01") else """
async function placeOrder(){
  document.getElementById('place-order').disabled=true;
  const r=await api('POST','/api/orders');
  if(r.ok){toast('Order #'+r.data.id+' placed! Thank you.'); setTimeout(()=>location.href='/orders',1500)}
  else{toast('Order failed: '+(r.data.error||r.status),true); document.getElementById('place-order').disabled=false}
}""")
    script = place_js + """
async function setQty(id,q){await api('POST','/api/cart/update',{product_id:id,qty:parseInt(q)});location.reload()}
async function removeItem(id){await api('POST','/api/cart/remove',{product_id:id});location.reload()}
async function applyCoupon(e){
  e.preventDefault();
  const code=document.getElementById('coupon').value.trim();
  const r=await api('POST','/api/cart/coupon',{code});
  if(r.ok){toast('Coupon applied'); setTimeout(()=>location.reload(),1200)} else toast(r.data.error||'Invalid coupon',true);
}"""
    return page("ShopLite - Cart", body, user, script)


def orders_page(user):
    mine = [o for o in DB["orders"] if o["email"] == user["email"]]
    rows = "".join(
        f'<tr><td>#{o["id"]}</td><td>{sum(o["items"].values())} item(s)</td><td>${o["total"]:.2f}</td>'
        f'<td><button class="secondary" onclick="askDelete({o["id"]})">Delete</button></td></tr>'
        for o in mine
    )
    wide = '<div style="width:1600px;height:4px;background:#eee"></div>' if bug("B17") else ""
    body = f"""<h1>Your orders</h1>{wide}
{"<table><thead><tr><th>Order</th><th>Items</th><th>Total</th><th></th></tr></thead><tbody>" + rows + "</tbody></table>" if rows else "<p>You have no orders yet.</p>"}
<div class="modal" id="confirm" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
 <div class="box"><h2 id="confirm-title">Delete this order?</h2><p>This cannot be undone.</p>
 <button id="confirm-yes" onclick="doDelete()">Delete</button>
 <button class="secondary" id="confirm-no" onclick="cancelDelete()">Cancel</button></div></div>"""
    cancel_js = ("async function cancelDelete(){await api('DELETE','/api/orders/'+pending);location.reload()}"
                 if bug("B04") else
                 "function cancelDelete(){pending=null;document.getElementById('confirm').style.display='none'}")
    script = """
let pending=null;
function askDelete(id){pending=id;document.getElementById('confirm').style.display='flex'}
async function doDelete(){await api('DELETE','/api/orders/'+pending);location.reload()}
""" + cancel_js
    return page("ShopLite - Orders", body, user, script)


def account_page(user):
    body = f"""<h1>Your account</h1>
<p>Email: {html.escape(user["email"])}</p>
<form onsubmit="saveProfile(event)">
<label for="name">Display name</label> <input id="name" value="{html.escape(user["name"])}">
<button type="submit">Save</button></form>"""
    script = """
async function saveProfile(e){
  e.preventDefault();
  const r=await api('POST','/api/profile',{name:document.getElementById('name').value});
  toast(r.ok?'Saved':'Could not save', !r.ok);
}"""
    return page("ShopLite - Account", body, user, script)


def admin_page(user):
    rows = "".join(
        f"<tr><td>{html.escape(u['name'])}</td><td>{html.escape(e)}</td><td>{u['role']}</td></tr>"
        for e, u in DB["users"].items()
    )
    body = f"""<h1>Admin: users</h1><table><thead><tr><th>Name</th><th>Email</th><th>Role</th></tr></thead>
<tbody>{rows}</tbody></table>"""
    return page("ShopLite - Admin", body, user)


def access_denied(user):
    return page("ShopLite - Access denied",
                "<h1>Access denied</h1><p>You need an admin account to see this page.</p>", user)


def login_page(error=""):
    err = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""<h1>Log in</h1>{err}
<form method="post" action="/login">
<p><label for="email">Email</label><br><input id="email" name="email" type="email" autocomplete="username" required></p>
<p><label for="password">Password</label><br><input id="password" name="password" type="password" autocomplete="current-password" required></p>
<button type="submit">Log in</button></form>
<p>No account? <a href="/register">Register</a></p>"""
    return page("ShopLite - Log in", body)


def register_page(error="", done=False):
    if done:
        return page("ShopLite - Registered", '<h1>Welcome!</h1><p role="status">Your account was created. '
                    '<a href="/login">Log in</a></p>')
    err = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    email_type = "text" if bug("B07") else "email"
    body = f"""<h1>Create an account</h1>{err}
<form method="post" action="/register"{" novalidate" if bug("B07") else ""}>
<p><label for="r-name">Name</label><br><input id="r-name" name="name" required></p>
<p><label for="r-email">Email</label><br><input id="r-email" name="email" type="{email_type}" required></p>
<p><label for="r-pass">Password (8+ characters)</label><br><input id="r-pass" name="password" type="password" required minlength="8"></p>
<button type="submit">Create account</button></form>"""
    return page("ShopLite - Register", body)


def contact_page(user, sent=False):
    title = "" if bug("B19") else "ShopLite - Contact"
    if sent:
        return page(title, '<h1>Contact us</h1><p role="status">Thanks, we will reply within 2 days.</p>', user)
    body = """<h1>Contact us</h1>
<form method="post" action="/contact">
<p><label for="c-email">Your email</label><br><input id="c-email" name="email" type="email" required></p>
<p><label for="c-msg">Message</label><br><textarea id="c-msg" name="message" required></textarea></p>
<button type="submit">Send</button></form>"""
    return page(title, body, user)


def about_page(user):
    sub = "h4" if bug("B18") else "h2"
    body = f"""<h1>About ShopLite</h1><p>We sell useful everyday things.</p>
<{sub}>Our story</{sub}><p>Started in 2026 by two friends.</p>
<{sub}>Shipping</{sub}><p>Free shipping on orders over $50.</p>"""
    return page("ShopLite - About", body, user)


def not_found(user):
    return page("ShopLite - Not found", '<h1>Page not found</h1><p><a href="/">Back to the shop</a></p>', user)


def stack_trace_page(pid):
    return (f"<pre>Traceback (most recent call last):\n  File \"/srv/shoplite/app.py\", line 212, in product_page\n"
            f"    p = PRODUCTS_BY_ID[{pid}]\nKeyError: {pid}\n\nDB_URL=postgres://shop:shop@10.0.0.5/shop</pre>")


# ─── server ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "ShopLite/1.0"

    def log_message(self, fmt, *args):
        pass

    # helpers
    def _user(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie["sid"].value if "sid" in cookie else ""
        email = DB["sessions"].get(sid)
        if not email:
            return None
        u = dict(DB["users"][email])
        u["email"] = email
        return u

    def _send(self, status, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, obj, headers=None):
        self._send(status, json.dumps(obj), "application/json", headers)

    def _redirect(self, to, headers=None):
        self.send_response(303)
        self.send_header("Location", to)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if "application/json" in (self.headers.get("Content-Type") or ""):
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return {}
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

    def _need_login(self):
        self._redirect("/login")

    # GET
    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        user = self._user()
        if path.startswith("/static/img/"):
            name = path.rsplit("/", 1)[-1]
            if name.endswith(".svg") and any(p["img"] + ".svg" == name for p in PRODUCTS):
                return self._send(200, svg_placeholder(name[:-4]), "image/svg+xml")
            return self._send(404, "not found", "text/plain")
        if path == "/":
            return self._send(200, home_page(user, query))
        if path.startswith("/product/"):
            try:
                pid = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._send(404, not_found(user))
            if not product(pid):
                if bug("B22"):
                    return self._send(500, stack_trace_page(pid))
                return self._send(404, not_found(user))
            return self._send(200, product_page(user, pid))
        if path == "/login":
            return self._send(200, login_page())
        if path == "/register":
            return self._send(200, register_page())
        if path == "/contact":
            return self._send(200, contact_page(user))
        if path == "/about":
            return self._send(200, about_page(user))
        if path in ("/cart", "/orders", "/account", "/admin"):
            if not user:
                return self._need_login()
            if path == "/cart":
                return self._send(200, cart_page(user))
            if path == "/orders":
                return self._send(200, orders_page(user))
            if path == "/account":
                return self._send(200, account_page(user))
            if user["role"] != "admin" and not bug("B03"):
                return self._send(403, access_denied(user))
            return self._send(200, admin_page(user))
        if path == "/api/me":
            return self._json(200 if user else 401, {"user": user and {"email": user["email"], "name": user["name"]}})
        if path == "/api/orders":
            if not user:
                return self._json(401, {"error": "login required"})
            return self._json(200, [o | {"items": {str(k): v for k, v in o["items"].items()}}
                                    for o in DB["orders"] if o["email"] == user["email"]])
        return self._send(404, not_found(user))

    # POST
    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()
        user = self._user()
        if path == "/__bench/reset":
            reset_db()
            return self._json(200, {"ok": True, "bugs": sorted(ACTIVE_BUGS)})
        if path == "/login":
            email = str(data.get("email", "")).strip().lower()
            u = DB["users"].get(email)
            if not u or u["password"] != data.get("password"):
                return self._send(200, login_page("Wrong email or password."))
            sid = secrets.token_hex(16)
            DB["sessions"][sid] = email
            return self._redirect("/", {"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Lax"})
        if path == "/register":
            email = str(data.get("email", "")).strip().lower()
            name = str(data.get("name", "")).strip()
            pw = str(data.get("password", ""))
            if not bug("B07") and ("@" not in email or "." not in email.split("@")[-1]):
                return self._send(200, register_page("Enter a valid email address."))
            if not name or len(pw) < 8:
                return self._send(200, register_page("Name and an 8+ character password are required."))
            if email in DB["users"]:
                return self._send(200, register_page("That email is already registered."))
            DB["users"][email] = {"password": pw, "name": name, "role": "user"}
            return self._send(200, register_page(done=True))
        if path == "/contact":
            DB["messages"].append(data)
            return self._send(200, contact_page(user, sent=True))
        if not user:
            return self._json(401, {"error": "login required"})
        email = user["email"]
        with _lock:
            if path == "/api/logout":
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                DB["sessions"].pop(cookie["sid"].value if "sid" in cookie else "", None)
                return self._json(200, {"ok": True}, {"Set-Cookie": "sid=; Path=/; Max-Age=0"})
            if path == "/api/cart/add":
                pid = int(data.get("product_id", 0))
                if not product(pid):
                    return self._json(404, {"error": "no such product"})
                cart = DB["carts"].setdefault(email, {})
                cart[pid] = cart.get(pid, 0) + 1
                return self._json(200, {"ok": True, "cart": cart})
            if path == "/api/cart/update":
                pid, qty = int(data.get("product_id", 0)), int(data.get("qty") or 1)
                cart = DB["carts"].setdefault(email, {})
                if pid in cart:
                    cart[pid] = max(1, qty)
                return self._json(200, {"ok": True})
            if path == "/api/cart/remove":
                DB["carts"].setdefault(email, {}).pop(int(data.get("product_id", 0)), None)
                return self._json(200, {"ok": True})
            if path == "/api/cart/coupon":
                code = str(data.get("code", "")).strip().upper()
                if code != "SAVE10":
                    return self._json(400, {"error": "Invalid coupon"})
                DB["coupons"][email] = code
                return self._json(200, {"ok": True})
            if path == "/api/orders":
                cart = DB["carts"].get(email, {})
                if not cart:
                    return self._json(400, {"error": "cart is empty"})
                if bug("B01"):
                    return self._json(500, {"error": "payment service unavailable"})
                order = {"id": DB["next_order"], "email": email, "items": dict(cart),
                         "total": cart_total(email)}
                DB["next_order"] += 1
                DB["orders"].append(order)
                DB["carts"][email] = {}
                DB["coupons"].pop(email, None)
                return self._json(200, {"ok": True, "id": order["id"], "total": order["total"]})
            if path == "/api/profile":
                name = str(data.get("name", "")).strip()
                if not name:
                    return self._json(400, {"error": "name required"})
                if not bug("B08"):
                    DB["users"][email]["name"] = name
                return self._json(200, {"ok": True})
            if path == "/api/reviews":
                pid = int(data.get("product_id", 0))
                text = str(data.get("text", "")).strip()
                if not product(pid) or not text:
                    return self._json(400, {"error": "bad review"})
                DB["reviews"].setdefault(pid, []).append(text)
                return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        user = self._user()
        if not user:
            return self._json(401, {"error": "login required"})
        if path.startswith("/api/orders/"):
            try:
                oid = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._json(400, {"error": "bad id"})
            with _lock:
                before = len(DB["orders"])
                DB["orders"] = [o for o in DB["orders"] if not (o["id"] == oid and o["email"] == user["email"])]
                return self._json(200 if len(DB["orders"]) < before else 404, {"ok": True})
        return self._json(404, {"error": "not found"})


def parse_bugs(spec: str) -> set[str]:
    spec = (spec or "all").strip()
    if spec == "all":
        return set(ALL_BUGS)
    if spec == "none":
        return set()
    chosen = {s.strip().upper() for s in spec.split(",") if s.strip()}
    unknown = chosen - ALL_BUGS
    if unknown:
        raise SystemExit(f"unknown bug id(s): {', '.join(sorted(unknown))}")
    return chosen


def serve(port: int, bugs: set[str]):
    ACTIVE_BUGS.clear()
    ACTIVE_BUGS.update(bugs)
    reset_db()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bugs", default="all", help="all | none | comma-separated IDs (B01,B05)")
    args = ap.parse_args()
    bugs = parse_bugs(args.bugs)
    ACTIVE_BUGS.update(bugs)
    reset_db()
    print(f"ShopLite on http://127.0.0.1:{args.port} with bugs: {','.join(sorted(bugs)) or 'none'}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
