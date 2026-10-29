"""Production module for Real View CRM: orders that move through production stages (Queue → In Production →
Ready for Dispatch → Dispatch → Delivered, or the client's own stages), makers with the amount to pay them, a
printable production sheet, GST bill number at dispatch, warranty from the bill date (also for parts made by other
companies), a private customer link to follow the order and see the warranty, unit codes with referral points,
expected dates worked out from the queue, Excel export of everything and bulk import of old deliveries.

The Super Admin switches the module on per client (organizations.mod_prod). Everything is scoped to the current
organization (g.org), which always comes from the server-side session. Amounts are stored as whole paise.
A lead marked as Converted (any status in the client's "won" stage) becomes an order in the Queue by itself.

Loaded by app.py (production.init(app_module)). Imports / exports, the sheet and the public page are in
production_io.py.
"""
import heapq
import json
import re
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

bp = Blueprint("prod", __name__)
H = None  # the host app module (app.py)


def init(host):
    global H
    H = host
    host.app.register_blueprint(bp)
    host.app.jinja_env.globals["pcan"] = lambda p: can(p)
    host.app.jinja_env.globals["warranty_info"] = warranty_info
    host.app.after_request(sync_after_lead_change)


# ---------------------------------------------------------------- constants

PERMS = [
    ("view", "View orders, warranty and customers"),
    ("edit", "Add / edit orders"),
    ("stage", "Move orders to the next stage"),
    ("maker", "Assign makers + see / change maker amounts"),
    ("bill", "Enter GST bill number (Dispatch)"),
    ("warranty", "Edit warranty and parts"),
    ("points", "Redeem / adjust points"),
    ("import", "Import old deliveries"),
    ("export", "Export data (Excel)"),
    ("settings", "Production settings (stages, products, points)"),
]
PERM_KEYS = [k for k, _ in PERMS]
ROLE_PRESETS = {
    "none": [],
    "worker": ["view", "stage"],
    "manager": [k for k in PERM_KEYS if k != "settings"],
}
ROLE_LABELS = {"none": "No Production access", "worker": "Production staff", "manager": "Production manager",
               "custom": "Custom"}

MARKS = {
    "production": "Production starts — asks the maker",
    "ready": "Production finished",
    "dispatch": "Dispatch — asks the GST bill number",
    "delivered": "Delivered — warranty page + points",
}
DEFAULT_STAGES = [("Queue", None), ("In Production", "production"), ("Ready for Dispatch", "ready"),
                  ("Dispatch", "dispatch"), ("Delivered", "delivered")]
MACHINE_TYPES = ["New", "Used", "Reconditioned", "Service"]
POWER_SOURCES = ["Electric", "Diesel", "Petrol", "Manual", "Hydraulic", "Solar"]
PAY_MODES = ["Cash", "UPI", "Bank transfer", "Cheque"]

DEFAULT_SETTINGS = {
    "customer_link": True,     # private tracking / warranty page for each order
    "warranty": True,
    "parts": True,             # warranties of parts made by other companies
    "referral": True,          # unit codes + referral points
    "referrer_points": 1,      # points to the code owner when the referred order is delivered
    "new_points_on": False,    # points for the new customer too
    "new_points": 1,
    "point_value": 50000,      # paise per point (₹500)
    "default_warranty": 12,    # months
    "default_days": 10,        # production days when the product has none
    "sheet_title": "PRODUCTION ORDER",
}


def _now():
    return H.now()


def today():
    return H.today()


def today_date():
    return date.fromisoformat(H.today())


def to_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def money_in(value):
    """Form text -> paise (None when empty, 'bad' when not a number)."""
    import cafe
    p = cafe.to_paise(value, default=None)
    if p is None and str(value or "").strip():
        return "bad"
    return p


def int_in(value, default=None, lo=0, hi=100000):
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def clean(v, n=200):
    return (v or "").strip()[:n]


def add_months(d, months):
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    import calendar
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


# ---------------------------------------------------------------- permissions

def user_perms(user=None):
    user = user if user is not None else g.get("user")
    if not user:
        return set()
    if user["role"] in (H.SUPER_ADMIN, H.ORG_ADMIN):
        return set(PERM_KEYS)
    try:
        perms = json.loads(user["prod_perms"] or "null")
    except (ValueError, TypeError, IndexError, KeyError):
        perms = None
    return {p for p in (perms or []) if p in PERM_KEYS}


def can(perm):
    if not g.get("user"):
        return False
    if g.get("viewing"):
        return True
    return perm in user_perms()


def has_prod(org=None):
    return "prod" in H.org_modules(org if org is not None else g.get("org"))


def prod_view(perm=None):
    """Decorator: logged in, Production is on for the organization and this person, and (optionally) the permission."""
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            def inner():
                if "prod" not in H.user_modules():
                    abort(404)
                if perm and not can(perm):
                    abort(403)
                if request.method == "GET" and session.get("app_mode") != "prod":
                    H.set_module("prod")  # e.g. "📦 Open order" from a lead switches to Production
                ensure_setup(H.org_id())
                return view(*args, **kwargs)
            return H.login_required(inner)()
        return wrapped
    return deco


def staff_form_perms():
    role = request.form.get("prod_role")
    if role not in ROLE_LABELS:
        role = "none"
    if role == "custom":
        perms = [p for p in request.form.getlist("prod_perm") if p in PERM_KEYS]
    else:
        perms = ROLE_PRESETS[role]
    return role, json.dumps(perms)


def staff_form_extra(member):
    role = (member["prod_role"] if member is not None and "prod_role" in member.keys() else None) or "none"
    have = user_perms(member) if member is not None and member["role"] == H.STAFF else set()
    if request.method == "POST":
        role = request.form.get("prod_role") or role
        have = set(request.form.getlist("prod_perm")) if role == "custom" else set(ROLE_PRESETS.get(role, []))
    allowed = set(PERM_KEYS) if H.is_admin() else user_perms()
    return {"all": PERMS, "have": have, "role": role, "presets": ROLE_PRESETS, "labels": ROLE_LABELS,
            "allowed": allowed}


# ---------------------------------------------------------------- settings

def get_settings(oid=None):
    oid = oid or H.org_id()
    cache = g.setdefault("prod_settings", {})
    if oid not in cache:
        row = H.query("SELECT data FROM prod_settings WHERE org_id = ?", (oid,), one=True)
        try:
            data = json.loads(row["data"]) if row else {}
        except (ValueError, TypeError):
            data = {}
        cache[oid] = {**DEFAULT_SETTINGS, **{k: v for k, v in data.items() if k in DEFAULT_SETTINGS}}
    return cache[oid]


def save_settings(data, oid=None):
    oid = oid or H.org_id()
    db = H.get_db()
    db.execute("""INSERT INTO prod_settings (org_id, data, updated_by, updated_at) VALUES (?,?,?,?)
                  ON CONFLICT(org_id) DO UPDATE SET data = excluded.data, updated_by = excluded.updated_by,
                  updated_at = excluded.updated_at""", (oid, json.dumps(data), g.user["id"], _now()))
    db.commit()
    g.setdefault("prod_settings", {}).pop(oid, None)


# ---------------------------------------------------------------- stage sets

def ensure_setup(oid):
    """First use: the standard stage set (Queue → In Production → Ready for Dispatch → Dispatch → Delivered)."""
    if H.scalar("SELECT 1 FROM prod_stage_sets WHERE org_id = ?", (oid,)):
        return
    db = H.get_db()
    ts = _now()
    sid = db.execute("INSERT INTO prod_stage_sets (org_id, name, is_default, created_at) VALUES (?, 'Standard', 1, ?)",
                     (oid, ts)).lastrowid
    db.executemany("INSERT INTO prod_stages (org_id, set_id, name, sort, mark, created_at) VALUES (?,?,?,?,?,?)",
                   [(oid, sid, n, i, m, ts) for i, (n, m) in enumerate(DEFAULT_STAGES)])
    db.commit()


def stage_sets(oid=None):
    return H.query("SELECT * FROM prod_stage_sets WHERE org_id = ? ORDER BY is_default DESC, name COLLATE NOCASE",
                   (oid or H.org_id(),))


def default_set_id(oid=None):
    oid = oid or H.org_id()
    ensure_setup(oid)
    return H.scalar("SELECT id FROM prod_stage_sets WHERE org_id = ? ORDER BY is_default DESC, id LIMIT 1", (oid,))


def set_stages(set_id):
    cache = g.setdefault("prod_stages", {})
    if set_id not in cache:
        cache[set_id] = H.query("SELECT * FROM prod_stages WHERE set_id = ? AND active = 1 ORDER BY sort, id",
                                (set_id,))
    return cache[set_id]


def marks_of(set_id):
    """{mark: stage row} for a set, plus 'first' (the queue) and 'last'."""
    st = set_stages(set_id)
    out = {s["mark"]: s for s in st if s["mark"]}
    if st:
        out["first"], out["last"] = st[0], st[-1]
        out.setdefault("delivered", st[-1])
        out.setdefault("dispatch", out["delivered"])
    return out


def product_set_id(product, oid=None):
    if product is not None and product["stage_set_id"] and H.scalar(
            "SELECT 1 FROM prod_stage_sets WHERE id = ? AND org_id = ?", (product["stage_set_id"], oid or H.org_id())):
        return product["stage_set_id"]
    return default_set_id(oid)


def phase(order, stage=None):
    """'queue' (waiting), 'working' (in production), 'ready' (finished, not dispatched), 'dispatched', 'delivered'."""
    mk = marks_of(order["set_id"])
    stage = stage or next((s for s in set_stages(order["set_id"]) if s["id"] == order["stage_id"]), None)
    if stage is None:
        return "queue"
    sort = stage["sort"]
    prod = mk.get("production")
    ready = mk.get("ready") or mk.get("dispatch")
    if sort >= mk["delivered"]["sort"]:
        return "delivered"
    if sort >= mk["dispatch"]["sort"]:
        return "dispatched"
    if ready is not None and sort >= ready["sort"]:
        return "ready"
    if prod is not None and sort >= prod["sort"]:
        return "working"
    return "queue"


# ---------------------------------------------------------------- customers & codes

def new_unit_code(oid):
    for _ in range(200):
        code = "U" + "".join(secrets.choice("0123456789") for _ in range(5))
        if not H.scalar("SELECT 1 FROM prod_customers WHERE org_id = ? AND unit_code = ?", (oid, code)):
            return code
    return "U" + secrets.token_hex(4).upper()


def find_or_create_customer(db, oid, name, phone, whatsapp="", address="", ts=None):
    key = H.phone_key(phone or whatsapp)
    row = None
    if key:
        row = db.execute("SELECT * FROM prod_customers WHERE org_id = ? AND phone_norm = ? ORDER BY id LIMIT 1",
                         (oid, key)).fetchone()
    if row:
        updates = {}
        if name and not row["name"]:
            updates["name"] = name
        if address and not row["address"]:
            updates["address"] = address
        if whatsapp and not row["whatsapp"]:
            updates["whatsapp"] = whatsapp
        if updates:
            db.execute(f"UPDATE prod_customers SET {', '.join(k + ' = ?' for k in updates)}, updated_at = ? WHERE id = ?",
                       (*updates.values(), ts or _now(), row["id"]))
        return row["id"]
    return db.execute("""INSERT INTO prod_customers (org_id, name, phone, whatsapp, phone_norm, address, unit_code,
                         created_at) VALUES (?,?,?,?,?,?,?,?)""",
                      (oid, name or "Customer", phone or "", whatsapp or "", key, address or "", new_unit_code(oid),
                       ts or _now())).lastrowid


def customer_by_code(oid, code):
    code = (code or "").strip().upper()
    if not code:
        return None
    return H.query("SELECT * FROM prod_customers WHERE org_id = ? AND unit_code = ?", (oid, code), one=True)


def refresh_points(db, cid):
    db.execute("UPDATE prod_customers SET points = (SELECT COALESCE(SUM(change), 0) FROM prod_points WHERE"
               " customer_id = ?) WHERE id = ?", (cid, cid))


# ---------------------------------------------------------------- orders

def next_order_no(db, oid):
    return (db.execute("SELECT COALESCE(MAX(order_no), 0) FROM prod_orders WHERE org_id = ?", (oid,)).fetchone()[0]
            or 0) + 1


def log(db, order_id, stage_name, note="", from_name=None, ts=None, oid=None, user_id=None):
    db.execute("""INSERT INTO prod_stage_log (org_id, order_id, stage_name, from_name, note, user_id, created_at)
                  VALUES (?,?,?,?,?,?,?)""", (oid or H.org_id(), order_id, stage_name, from_name, note or "",
                                               user_id if user_id is not None else (g.user["id"] if g.get("user") else None),
                                               ts or _now()))


def create_order(db, oid, d, lead_id=None, imported=False, ts=None, note="Order created"):
    """d: dict of order fields + customer fields (name, phone, whatsapp, address). Returns the new order id."""
    ts = ts or _now()
    st = get_settings(oid)
    product = None
    if d.get("product_id"):
        product = db.execute("SELECT * FROM products WHERE id = ? AND org_id = ?", (d["product_id"], oid)).fetchone()
        if product is None:
            d["product_id"] = None
    set_id = product_set_id(product, oid)
    mk = marks_of(set_id)
    stage = mk["delivered"] if imported else mk["first"]
    cid = find_or_create_customer(db, oid, d.get("name"), d.get("phone"), d.get("whatsapp"), d.get("address"), ts)
    ref = customer_by_code(oid, d.get("referral_code"))
    referrer_id = ref["id"] if ref and ref["id"] != cid else None
    pname = d.get("product_name") or (product["name"] if product else "")
    days = d.get("prod_days")
    if days is None:
        days = (product["prod_days"] if product and product["prod_days"] else None)
    warranty = d.get("warranty_months")
    if warranty is None:
        warranty = product["warranty_months"] if product and product["warranty_months"] is not None else st["default_warranty"]
    taken_by = d.get("taken_by")
    taken_name = d.get("taken_by_name")
    if taken_by and not taken_name:
        taken_name = H.scalar("SELECT name FROM users WHERE id = ? AND org_id = ?", (taken_by, oid))
    order_id = db.execute(
        """INSERT INTO prod_orders (org_id, order_no, lead_id, customer_id, product_id, product_name, category,
           machine_type, power_source, weight, quantity, unit_price, details, prod_days, order_date, taken_by,
           taken_by_name, approved_by, note, referral_code, referrer_id, set_id, stage_id, stage_at, maker_id,
           maker_amount, mfg_start, exp_finish, bill_no, bill_date, warranty_months, delivered_date, imported, token,
           created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (oid, next_order_no(db, oid), lead_id, cid, d.get("product_id"), pname,
         d.get("category") or (product["category"] if product else ""), d.get("machine_type") or "",
         d.get("power_source") or "", d.get("weight") or "", d.get("quantity") or 1, d.get("unit_price"),
         d.get("details") or "", days, d.get("order_date") or today(), taken_by or None, taken_name or "",
         d.get("approved_by") or "", d.get("note") or "", (d.get("referral_code") or "").strip().upper() or None,
         referrer_id, set_id, stage["id"], ts, d.get("maker_id"), d.get("maker_amount"), d.get("mfg_start"),
         d.get("exp_finish"), d.get("bill_no") or None, d.get("bill_date") or None, warranty,
         d.get("delivered_date") or (today() if imported else None), 1 if imported else 0, secrets.token_urlsafe(12),
         g.user["id"] if g.get("user") else None, ts, ts)).lastrowid
    if product is not None:  # the product's standard parts list (their own makers' warranties)
        db.execute("""INSERT INTO prod_parts (org_id, order_id, name, company, months, serial, sort)
                      SELECT org_id, ?, name, company, months, '', sort FROM prod_parts WHERE product_id = ?
                      ORDER BY sort, id""", (order_id, product["id"]))
    if imported:  # an old delivery with a maker counts in the maker's month of delivery
        db.execute("UPDATE prod_orders SET finished_at = delivered_date WHERE id = ? AND maker_id IS NOT NULL",
                   (order_id,))
    log(db, order_id, stage["name"], note, ts=ts, oid=oid)
    return order_id


def get_order(oid_or_none, order_id):
    oid = oid_or_none or H.org_id()
    o = H.query("""SELECT o.*, c.name cust_name, c.phone cust_phone, c.whatsapp cust_wa, c.address cust_address,
                     c.unit_code, c.points cust_points, s.name stage_name, s.mark stage_mark, s.sort stage_sort,
                     m.name maker_name, m.phone maker_phone, r.name referrer_name, r.unit_code referrer_code,
                     p.name catalog_product, pu.name pinned_by_name,
                     (SELECT MIN(a.pay_date) FROM prod_advances a WHERE a.order_id = o.id) first_adv
                   FROM prod_orders o LEFT JOIN users pu ON pu.id = o.pinned_by JOIN prod_customers c ON c.id = o.customer_id
                   JOIN prod_stages s ON s.id = o.stage_id LEFT JOIN prod_makers m ON m.id = o.maker_id
                   LEFT JOIN prod_customers r ON r.id = o.referrer_id LEFT JOIN products p ON p.id = o.product_id
                   WHERE o.id = ? AND o.org_id = ?""", (order_id, oid), one=True)
    if o is None:
        abort(404)
    return o


def details_pending(o):
    return not (o["product_name"] or "").strip() or o["unit_price"] is None or not o["quantity"]


FIRST_ADV = "(SELECT MIN(a.pay_date) FROM prod_advances a WHERE a.order_id = o.id)"
# Queue priority: 📌 pinned by the Admin (in the order they were pinned), then whoever paid the first advance earliest
# (any amount), then orders without an advance by order date.
QUEUE_ORDER = f"(o.pinned_at IS NULL), o.pinned_at, ({FIRST_ADV} IS NULL), {FIRST_ADV}, o.order_date, o.id"


def order_days(o):
    st = get_settings()
    return o["prod_days"] if o["prod_days"] else st["default_days"]


# ---------------------------------------------------------------- expected dates

def schedule(oid=None):
    """{order_id: {'start': date, 'finish': date, 'late': bool, 'phase': ...}} for running orders.

    No fixed capacity: the orders really in production now are the 'places'. A queued order (first come first) takes
    the place that is expected to finish first; its expected finish = that start + its production days. More orders
    moved into production (more workers) -> the queue's dates come earlier by themselves. Nothing in production ->
    the queue goes one after another from today."""
    oid = oid or H.org_id()
    t = today_date()
    rows = H.query(f"""SELECT o.id, o.set_id, o.stage_id, o.prod_days, o.mfg_start, o.exp_finish, o.stage_at,
                       o.order_date, o.pinned_at, {FIRST_ADV} first_adv
                       FROM prod_orders o WHERE o.org_id = ? AND o.status = 'active' AND o.imported = 0
                       ORDER BY {QUEUE_ORDER}""", (oid,))
    out, queue, places = {}, [], []
    for o in rows:
        ph = phase(o)
        if ph == "queue":
            queue.append(o)
        elif ph == "working":
            start = to_date(o["mfg_start"]) or to_date(o["stage_at"]) or t
            finish = to_date(o["exp_finish"]) or start + timedelta(days=order_days(o))
            out[o["id"]] = {"start": start, "finish": finish, "late": finish < t, "phase": ph}
            places.append(max(finish, t))
        elif ph == "ready":
            finish = to_date(o["exp_finish"]) or t
            out[o["id"]] = {"start": to_date(o["mfg_start"]), "finish": finish, "late": False, "phase": ph}
    heapq.heapify(places)
    nxt = t
    for pos, o in enumerate(queue, start=1):
        days = order_days(o)
        if places:
            start = heapq.heappop(places)
            finish = start + timedelta(days=days)
            heapq.heappush(places, finish)
        else:
            start = nxt
            finish = start + timedelta(days=days)
            nxt = finish
        out[o["id"]] = {"start": start, "finish": finish, "late": False, "phase": "queue", "pos": pos,
                        "pinned": bool(o["pinned_at"]), "first_adv": o["first_adv"]}
    return out


# ---------------------------------------------------------------- warranty

def warranty_info(o, parts=None):
    """{'start', 'end', 'left', 'expired'} for the order (from the bill date), and the same for each part."""
    start = to_date(o["bill_date"])
    if not start or o["warranty_months"] is None:
        main = None
    else:
        end = add_months(start, int(o["warranty_months"])) - timedelta(days=1)
        left = (end - today_date()).days
        main = {"start": start, "end": end, "left": left, "expired": left < 0, "months": o["warranty_months"]}
    out_parts = []
    for p in parts or []:
        if start and p["months"] is not None:
            end = add_months(start, int(p["months"])) - timedelta(days=1)
            left = (end - today_date()).days
            out_parts.append({"p": p, "end": end, "left": left, "expired": left < 0})
        else:
            out_parts.append({"p": p, "end": None, "left": None, "expired": False})
    return {"main": main, "parts": out_parts}


# ---------------------------------------------------------------- points

def give_points(db, o, ts):
    """Called when an order reaches Delivered: the code owner (and, if switched on, the new customer) earn points."""
    st = get_settings(o["org_id"])
    if o["points_given"] or o["imported"] or not st["referral"] or not o["referrer_id"]:
        return
    rows = [(o["referrer_id"], int(st["referrer_points"] or 0), "referral",
             f"Referral: order #{o['order_no']} delivered")]
    if st["new_points_on"]:
        rows.append((o["customer_id"], int(st["new_points"] or 0), "welcome", f"Joined with a code (order #{o['order_no']})"))
    for cid, n, kind, note in rows:
        if n > 0:
            db.execute("""INSERT INTO prod_points (org_id, customer_id, order_id, change, kind, note, created_by, created_at)
                          VALUES (?,?,?,?,?,?,?,?)""", (o["org_id"], cid, o["id"], n, kind, note,
                                                        g.user["id"] if g.get("user") else None, ts))
            refresh_points(db, cid)
    db.execute("UPDATE prod_orders SET points_given = 1 WHERE id = ?", (o["id"],))


def take_back_points(db, o, ts, why):
    """The order went back before Delivered or was cancelled: the points it gave are taken back."""
    if not o["points_given"]:
        return
    for r in db.execute("""SELECT customer_id, SUM(change) n FROM prod_points WHERE order_id = ? AND kind IN
                           ('referral', 'welcome', 'reversed') GROUP BY customer_id""", (o["id"],)).fetchall():
        if r["n"] > 0:
            db.execute("""INSERT INTO prod_points (org_id, customer_id, order_id, change, kind, note, created_by, created_at)
                          VALUES (?,?,?,?, 'reversed', ?,?,?)""", (o["org_id"], r["customer_id"], o["id"], -r["n"],
                                                                   f"Taken back: order #{o['order_no']} {why}",
                                                                   g.user["id"], ts))
            refresh_points(db, r["customer_id"])
    db.execute("UPDATE prod_orders SET points_given = 0 WHERE id = ?", (o["id"],))


# ---------------------------------------------------------------- CRM: converted lead -> order in the Queue

LEAD_WRITE_ENDPOINTS = {"lead_new", "lead_edit", "lead_quick_status", "lead_inline", "leads_bulk", "leads_import",
                        "recycle_bin_action"}


def lead_order_data(db, lead):
    prod = db.execute("""SELECT pr.* FROM lead_products lp JOIN products pr ON pr.id = lp.product_id
                         WHERE lp.lead_id = ? ORDER BY CASE lp.stage WHEN 'Ordered' THEN 0 WHEN 'Purchased' THEN 1
                         WHEN 'Interested' THEN 2 ELSE 3 END, pr.name LIMIT 1""", (lead["id"],)).fetchone()
    place = ", ".join(x for x in (lead["place"], lead["district"], lead["state"]) if x)
    name = lead["customer_name"]
    if lead["company_name"]:
        name = f"{lead['company_name']}" if not name else f"{name} ({lead['company_name']})"
    return {"name": name, "phone": lead["phone"] or lead["whatsapp"], "whatsapp": lead["whatsapp"] or lead["phone"],
            "address": place, "product_id": prod["id"] if prod else None, "product_name": prod["name"] if prod else "",
            "taken_by": lead["assigned_to"] or lead["entered_by"], "details": lead["requirement"] or "",
            "referral_code": lead["referral_code"] if "referral_code" in lead.keys() else ""}


def convert_leads(oid, since=None, lead_ids=None):
    """Make Queue orders for this organization's converted leads that have none yet. Returns how many."""
    won = H.stage_sql("won", oid)
    sql = f"""SELECT l.* FROM leads l WHERE l.org_id = ? AND l.status IN ({won})
              AND NOT EXISTS (SELECT 1 FROM prod_orders o WHERE o.lead_id = l.id)"""
    args = [oid]
    if since:
        sql += " AND l.updated_at >= ?"
        args.append(since)
    if lead_ids is not None:
        if not lead_ids:
            return 0
        sql += f" AND l.id IN ({','.join('?' * len(lead_ids))})"
        args += list(lead_ids)
    leads = H.query(sql + " ORDER BY l.updated_at, l.id", args)
    if not leads:
        return 0
    ensure_setup(oid)
    db = H.get_db()
    ts = _now()
    for lead in leads:
        oid_new = create_order(db, oid, lead_order_data(db, lead), lead_id=lead["id"], ts=ts,
                               note="Arrived from the CRM (lead converted)")
        no = db.execute("SELECT order_no FROM prod_orders WHERE id = ?", (oid_new,)).fetchone()[0]
        db.execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at) VALUES (?,?,?,?)",
                   (lead["id"], g.user["id"], f"📦 Production order #{no} created (Queue)", ts))
    db.commit()
    return len(leads)


def sync_after_lead_change(resp):
    """After a lead is saved (form, quick status, list, bulk, import): converted leads go to the Production Queue."""
    try:
        if (request.method != "POST" or request.endpoint not in LEAD_WRITE_ENDPOINTS or resp.status_code >= 400
                or not g.get("user") or not g.get("org") or g.get("viewing") or not has_prod()):
            return resp
        n = convert_leads(H.org_id(), since=g.get("req_ts"))
        if n:
            flash(f"📦 {n} converted lead{'s' if n > 1 else ''} sent to Production (Queue).", "success")
    except Exception:  # never break a lead save because of Production
        H.app.logger.exception("Production: converting leads failed")
    return resp


# ---------------------------------------------------------------- template helpers

@bp.app_context_processor
def prod_context():
    return dict(PROD_MARKS=MARKS, has_prod=bool(g.get("org")) and has_prod())


def stage_counts(oid):
    rows = H.query("""SELECT s.name, s.mark, MIN(s.sort) sort, COUNT(o.id) n FROM prod_orders o
                      JOIN prod_stages s ON s.id = o.stage_id WHERE o.org_id = ? AND o.status = 'active'
                      GROUP BY s.name ORDER BY MIN(s.sort), s.name""", (oid,))
    return rows


def order_rows(oid, where="", args=(), order_by="o.order_date, o.id", limit=None):
    sql = f"""SELECT o.*, c.name cust_name, c.phone cust_phone, c.whatsapp cust_wa, c.unit_code, s.name stage_name,
                     s.mark stage_mark, s.sort stage_sort, m.name maker_name, {FIRST_ADV} first_adv
              FROM prod_orders o JOIN prod_customers c ON c.id = o.customer_id JOIN prod_stages s ON s.id = o.stage_id
              LEFT JOIN prod_makers m ON m.id = o.maker_id WHERE o.org_id = ? {where} ORDER BY {order_by}"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return H.query(sql, (oid, *args))


def next_stage(o):
    st = set_stages(o["set_id"])
    for i, s in enumerate(st):
        if s["id"] == o["stage_id"]:
            return st[i + 1] if i + 1 < len(st) else None
    return st[0] if st else None


def needs_form(o, target):
    """Moving into this stage asks for something (the maker, or the GST bill number)."""
    if target is None:
        return False
    cur_sort = o["stage_sort"]
    mk = marks_of(o["set_id"])
    prod = mk.get("production")
    if prod is not None and cur_sort < prod["sort"] <= target["sort"]:
        return True
    if cur_sort < mk["dispatch"]["sort"] <= target["sort"] and not o["bill_no"]:
        return True
    return False


def track_url(o, external=True):
    return url_for("prod.public_track", code=g.org["code"].lower(), tok=o["token"], _external=external)


def wa_track_text(o):
    return (f"Hi {o['cust_name']},\nThank you for your order with {H.org_brand()['name'] if H.org_brand() else g.org['name']}"
            f" 🙏\nOrder #{o['order_no']}: {o['product_name'] or ''}\nFollow your order here: {track_url(o)}")


def wa_url(number, text):
    from urllib.parse import quote
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 10:
        digits = "91" + digits
    return f"https://wa.me/{digits}?text={quote(text)}" if digits else ""


# ---------------------------------------------------------------- dashboard

@bp.route("/production")
@prod_view()
def home():
    oid = H.org_id()
    counts = stage_counts(oid)
    sched = schedule(oid) if can("view") else {}
    running = order_rows(oid, "AND o.status = 'active' AND o.imported = 0") if can("view") else []
    late = [o for o in running if sched.get(o["id"], {}).get("late")]
    pending = [o for o in running if details_pending(o) and phase(o) in ("queue", "working")]
    soon = []
    if can("view") and get_settings()["warranty"]:
        for o in order_rows(oid, "AND o.status = 'active' AND o.bill_date IS NOT NULL"):
            w = warranty_info(o)["main"]
            if w and 0 <= w["left"] <= 30:
                soon.append((o, w))
    recent = H.query("""SELECT l.*, o.order_no, o.id oid, c.name cust_name, u.name user_name FROM prod_stage_log l
                        JOIN prod_orders o ON o.id = l.order_id JOIN prod_customers c ON c.id = o.customer_id
                        LEFT JOIN users u ON u.id = l.user_id WHERE l.org_id = ? ORDER BY l.id DESC LIMIT 12""",
                     (oid,)) if can("view") else []
    unconverted = 0
    if H.is_admin() and "crm" in H.org_modules():
        unconverted = H.scalar(f"""SELECT COUNT(*) FROM leads l WHERE l.org_id = ? AND l.status IN
                                   ({H.stage_sql('won', oid)}) AND NOT EXISTS
                                   (SELECT 1 FROM prod_orders o WHERE o.lead_id = l.id)""", (oid,))
    return render_template("production/home.html", counts=counts, late=late, pending=pending, soon=soon,
                           recent=recent, sched=sched, unconverted=unconverted, st=get_settings(),
                           running=len(running))


@bp.route("/production/convert-leads", methods=["POST"])
@prod_view("edit")
def convert_old_leads():
    if not H.is_admin():
        abort(403)
    n = convert_leads(H.org_id())
    flash(f"{n} converted lead{'s' if n != 1 else ''} added to the Queue." if n else "Nothing to add.", "success")
    return redirect(url_for("prod.home"))


# ---------------------------------------------------------------- orders list

@bp.route("/production/orders")
@prod_view("view")
def orders():
    oid = H.org_id()
    a = request.args
    where, args = [], []
    stage = a.get("stage") or ""
    show = a.get("show") or ""
    if show == "cancelled":
        where.append("o.status = 'cancelled'")
    else:
        where.append("o.status = 'active'")
    last_sort = "(SELECT MAX(s2.sort) FROM prod_stages s2 WHERE s2.set_id = o.set_id AND s2.active = 1)"
    if stage:
        where.append("s.name = ?")
        args.append(stage)
    elif show == "delivered":
        where.append(f"s.sort >= {last_sort}")
    elif show not in ("all", "cancelled"):
        where.append(f"s.sort < {last_sort}")  # running orders (not delivered yet)
    q = (a.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        num = re.sub(r"\D", "", q)
        where.append("(c.name LIKE ? OR c.phone LIKE ? OR o.product_name LIKE ? OR o.bill_no LIKE ? OR c.unit_code LIKE ?"
                     + (" OR o.order_no = ?" if num and len(num) < 7 else "") + ")")
        args += [like, f"%{num or q}%", like, like, like] + ([int(num)] if num and len(num) < 7 else [])
    if a.get("product"):
        where.append("o.product_name = ?")
        args.append(a.get("product"))
    if a.get("maker", type=int):
        where.append("o.maker_id = ?")
        args.append(a.get("maker", type=int))
    rows = order_rows(oid, "AND " + " AND ".join(where), args,
                      order_by="o.order_no DESC" if show in ("all", "cancelled", "delivered") or (
                          stage and marks_of_name(stage) == "delivered") else "s.sort, " + QUEUE_ORDER)
    sched = schedule(oid)
    if show == "late":
        rows = [o for o in rows if sched.get(o["id"], {}).get("late")]
    if show == "pending":
        rows = [o for o in rows if details_pending(o)]
    products = [r[0] for r in H.query("SELECT DISTINCT product_name FROM prod_orders WHERE org_id = ? AND"
                                      " product_name != '' ORDER BY product_name", (oid,))]
    makers = H.query("SELECT id, name FROM prod_makers WHERE org_id = ? ORDER BY name", (oid,)) if can("maker") else []
    return render_template("production/orders.html", rows=rows, sched=sched, counts=stage_counts(oid), stage=stage,
                           show=show, q=q, products=products, makers=makers, args=a,
                           nexts={o["id"]: next_stage(o) for o in rows}, needs_form=needs_form, phase=phase,
                           details_pending=details_pending)


def marks_of_name(stage_name):
    return H.scalar("SELECT mark FROM prod_stages WHERE org_id = ? AND name = ? AND mark IS NOT NULL LIMIT 1",
                    (H.org_id(), stage_name))


# ---------------------------------------------------------------- order form (new / edit / old delivery)

ORDER_TEXT = ["name", "phone", "whatsapp", "address", "product_name", "category", "machine_type", "power_source",
              "weight", "details", "approved_by", "note", "referral_code", "taken_by_name"]


def order_form_data():
    f = request.form
    d = {k: clean(f.get(k), 4000 if k in ("details", "note") else 200) for k in ORDER_TEXT}
    d["product_id"] = f.get("product_id", type=int)
    d["quantity"] = int_in(f.get("quantity"), 1, 1, 100000)
    d["unit_price"] = money_in(f.get("unit_price"))
    d["prod_days"] = int_in(f.get("prod_days"), None, 0, 3650) if (f.get("prod_days") or "").strip() else None
    d["order_date"] = H.valid_date(clean(f.get("order_date"))) if f.get("order_date") else today()
    d["taken_by"] = f.get("taken_by", type=int)
    d["warranty_months"] = int_in(f.get("warranty_months"), None, 0, 600) if (f.get("warranty_months") or "").strip() else None
    d["bill_no"] = clean(f.get("bill_no"), 60)
    d["bill_date"] = H.valid_date(clean(f.get("bill_date"))) if f.get("bill_date") else ""
    d["delivered_date"] = H.valid_date(clean(f.get("delivered_date"))) if f.get("delivered_date") else ""
    d["maker_amount"] = money_in(f.get("maker_amount"))
    d["advances"] = []
    dates, amts, notes = f.getlist("adv_date"), f.getlist("adv_amount"), f.getlist("adv_note")
    for i, amt in enumerate(amts):
        dt = dates[i] if i < len(dates) else ""
        note = notes[i] if i < len(notes) else ""
        if not (amt or "").strip():
            continue
        d["advances"].append({"pay_date": H.valid_date((dt or "").strip()) or today(), "amount": money_in(amt),
                              "note": clean(note, 200)})
    d["parts"] = parts_from_form()
    return d


def parts_from_form():
    f = request.form
    out = []
    for i, (n, c, m, s) in enumerate(zip(f.getlist("part_name"), f.getlist("part_company"), f.getlist("part_months"),
                                         f.getlist("part_serial"))):
        if not (n or "").strip():
            continue
        out.append({"name": clean(n, 120), "company": clean(c, 120),
                    "months": int_in(m, None, 0, 600) if (m or "").strip() else None, "serial": clean(s, 80), "sort": i})
    return out


def validate_order(d, old=None, old_delivery=False):
    errors = []
    if not d["name"]:
        errors.append("Customer name is required.")
    if not (d["phone"] or d["whatsapp"]):
        errors.append("Enter the customer's phone or WhatsApp number.")
    if d["unit_price"] == "bad":
        errors.append("Unit price is not a number.")
    if d["maker_amount"] == "bad":
        errors.append("Maker amount is not a number.")
    if any(a["amount"] in ("bad", None) for a in d["advances"]):
        errors.append("An advance amount is not a number.")
    for k, label in (("order_date", "Order date"), ("bill_date", "Bill date"), ("delivered_date", "Delivered date")):
        if d[k] is None:
            errors.append(f"{label} is invalid.")
            d[k] = ""
    if d["product_id"] and not H.scalar("SELECT 1 FROM products WHERE id = ? AND org_id = ?",
                                        (d["product_id"], H.org_id())):
        d["product_id"] = None
    if d["product_id"] and not d["product_name"]:
        d["product_name"] = H.scalar("SELECT name FROM products WHERE id = ?", (d["product_id"],))
    if d["taken_by"] and not H.scalar("SELECT 1 FROM users WHERE id = ? AND org_id = ?", (d["taken_by"], H.org_id())):
        d["taken_by"] = None
    if d["referral_code"]:
        d["referral_code"] = d["referral_code"].upper()
        if not customer_by_code(H.org_id(), d["referral_code"]):
            errors.append(f"Referral code {d['referral_code']} was not found.")
    if old_delivery:
        if not d["bill_date"]:
            errors.append("Bill date is required for an old delivery (warranty starts from it).")
    if d["bill_no"]:
        dup = H.scalar("SELECT order_no FROM prod_orders WHERE org_id = ? AND bill_no = ? AND id != ?",
                       (H.org_id(), d["bill_no"], old["id"] if old else 0))
        if dup:
            errors.append(f"GST bill number {d['bill_no']} is already on order #{dup}.")
    return errors


def save_children(db, order_id, d, ts, parts=True, advances=True):
    oid = H.org_id()
    if advances:
        db.execute("DELETE FROM prod_advances WHERE order_id = ?", (order_id,))
        db.executemany("""INSERT INTO prod_advances (org_id, order_id, pay_date, amount, note, created_by, created_at)
                          VALUES (?,?,?,?,?,?,?)""", [(oid, order_id, a["pay_date"], a["amount"], a["note"],
                                                       g.user["id"], ts) for a in d["advances"]])
    if parts:
        db.execute("DELETE FROM prod_parts WHERE order_id = ?", (order_id,))
        db.executemany("""INSERT INTO prod_parts (org_id, order_id, name, company, months, serial, sort)
                          VALUES (?,?,?,?,?,?,?)""", [(oid, order_id, p["name"], p["company"], p["months"],
                                                       p["serial"], p["sort"]) for p in d["parts"]])


def form_context(d, order=None, old_delivery=False, parts=None, advances=None):
    oid = H.org_id()
    prods = H.query("""SELECT id, name, category, prod_days, warranty_months FROM products WHERE org_id = ? AND
                       (active = 1 OR id = ?) ORDER BY name COLLATE NOCASE""", (oid, d.get("product_id") or 0))
    users = H.query("SELECT id, name FROM users WHERE org_id = ? AND active = 1 ORDER BY name COLLATE NOCASE", (oid,))
    return dict(d=d, order=order, old_delivery=old_delivery, products=prods, users=users, st=get_settings(),
                parts=parts if parts is not None else d.get("parts", []),
                advances=advances if advances is not None else d.get("advances", []),
                MACHINE_TYPES=MACHINE_TYPES, POWER_SOURCES=POWER_SOURCES,
                paise=lambda v: "" if v in (None, "bad") else __import__("cafe").paise_str(v))


@bp.route("/production/orders/new", methods=["GET", "POST"])
@prod_view("edit")
def order_new():
    old_delivery = bool(request.values.get("old"))
    if old_delivery and not (can("import") or H.is_admin()):
        abort(403)
    d = {k: "" for k in ORDER_TEXT}
    d.update(quantity=1, order_date=today(), taken_by=g.user["id"], advances=[], parts=[], product_id=None,
             unit_price=None, maker_amount=None, prod_days=None, warranty_months=None, bill_no="", bill_date="",
             delivered_date="")
    if request.method == "POST":
        d = order_form_data()
        errors = validate_order(d, old_delivery=old_delivery)
        for e in errors:
            flash(e, "error")
        if not errors:
            db = H.get_db()
            ts = _now()
            if not old_delivery:
                d.update(bill_no="", bill_date="", delivered_date="")
            parts_given = bool(d["parts"])
            new_id = create_order(db, H.org_id(), dict(d, maker_amount=None), imported=old_delivery, ts=ts,
                                  note="Old delivery added by hand" if old_delivery else "Order created")
            save_children(db, new_id, d, ts, parts=parts_given)
            db.commit()
            flash("Old delivery saved." if old_delivery else "Order saved in the Queue.", "success")
            return redirect(url_for("prod.order_view", order_id=new_id))
    return render_template("production/order_form.html", **form_context(d, old_delivery=old_delivery))


@bp.route("/production/orders/<int:order_id>/edit", methods=["GET", "POST"])
@prod_view("edit")
def order_edit(order_id):
    o = get_order(None, order_id)
    parts = H.query("SELECT * FROM prod_parts WHERE order_id = ? ORDER BY sort, id", (order_id,))
    advances = H.query("SELECT * FROM prod_advances WHERE order_id = ? ORDER BY pay_date, id", (order_id,))
    d = dict(o)
    d.update(name=o["cust_name"], phone=o["cust_phone"], whatsapp=o["cust_wa"], address=o["cust_address"])
    if request.method == "POST":
        d = order_form_data()
        errors = validate_order(d, old=o)
        for e in errors:
            flash(e, "error")
        if not errors:
            db = H.get_db()
            ts = _now()
            key = H.phone_key(d["phone"] or d["whatsapp"])
            db.execute("""UPDATE prod_customers SET name = ?, phone = ?, whatsapp = ?, phone_norm = ?, address = ?,
                          updated_at = ? WHERE id = ? AND org_id = ?""",
                       (d["name"], d["phone"], d["whatsapp"], key, d["address"], ts, o["customer_id"], H.org_id()))
            ref = customer_by_code(H.org_id(), d["referral_code"])
            referrer_id = ref["id"] if ref and ref["id"] != o["customer_id"] else None
            cols = {"product_id": d["product_id"], "product_name": d["product_name"], "category": d["category"],
                    "machine_type": d["machine_type"], "power_source": d["power_source"], "weight": d["weight"],
                    "quantity": d["quantity"], "unit_price": d["unit_price"], "details": d["details"],
                    "prod_days": d["prod_days"], "order_date": d["order_date"] or o["order_date"],
                    "taken_by": d["taken_by"], "taken_by_name": d["taken_by_name"] or (
                        H.scalar("SELECT name FROM users WHERE id = ?", (d["taken_by"],)) if d["taken_by"] else ""),
                    "approved_by": d["approved_by"], "note": d["note"], "referral_code": d["referral_code"] or None,
                    "referrer_id": referrer_id if not o["points_given"] else o["referrer_id"]}
            if can("bill"):
                cols.update(bill_no=d["bill_no"] or None, bill_date=d["bill_date"] or None)
            if o["imported"]:
                cols["delivered_date"] = d["delivered_date"] or o["delivered_date"]
            if can("warranty"):
                w = d["warranty_months"]
                if w is None:  # empty = the product's warranty, else the default
                    pw = H.scalar("SELECT warranty_months FROM products WHERE id = ?", (d["product_id"],)) \
                        if d["product_id"] else None
                    w = pw if pw is not None else get_settings()["default_warranty"]
                cols["warranty_months"] = w
            if can("maker"):
                cols["maker_amount"] = d["maker_amount"]
            db.execute(f"UPDATE prod_orders SET {', '.join(k + ' = ?' for k in cols)}, updated_by = ?, updated_at = ?"
                       " WHERE id = ? AND org_id = ?", (*cols.values(), g.user["id"], ts, order_id, H.org_id()))
            st = get_settings()
            save_children(db, order_id, d, ts, parts=can("warranty") and st["warranty"] and st["parts"])
            log(db, order_id, o["stage_name"], "Details updated", ts=ts)
            db.commit()
            flash("Order updated.", "success")
            return redirect(url_for("prod.order_view", order_id=order_id))
        parts, advances = d["parts"], d["advances"]
    return render_template("production/order_form.html", **form_context(d, order=o, old_delivery=bool(o["imported"]),
                                                                        parts=parts, advances=advances))


# ---------------------------------------------------------------- order page

@bp.route("/production/orders/<int:order_id>")
@prod_view("view")
def order_view(order_id):
    o = get_order(None, order_id)
    stages = set_stages(o["set_id"])
    parts = H.query("SELECT * FROM prod_parts WHERE order_id = ? ORDER BY sort, id", (order_id,))
    advances = H.query("SELECT * FROM prod_advances WHERE order_id = ? ORDER BY pay_date, id", (order_id,))
    history = H.query("""SELECT l.*, u.name user_name FROM prod_stage_log l LEFT JOIN users u ON u.id = l.user_id
                         WHERE l.order_id = ? ORDER BY l.id DESC""", (order_id,))
    reached = {r["stage_name"]: r["created_at"] for r in reversed(history)}
    lead = None
    if o["lead_id"] and "crm" in H.user_modules():
        lead = H.query("SELECT id, customer_name FROM leads WHERE id = ? AND org_id = ?", (o["lead_id"], H.org_id()),
                       one=True)
    makers = H.query("SELECT * FROM prod_makers WHERE org_id = ? AND active = 1 ORDER BY name", (H.org_id(),))
    sched = schedule().get(order_id)
    nxt = next_stage(o)
    link = track_url(o)
    return render_template("production/order_view.html", o=o, stages=stages, parts=parts, advances=advances,
                           history=history, reached=reached, lead=lead, sched=sched, nxt=nxt,
                           nxt_form=needs_form(o, nxt), link=link, wa=wa_url(o["cust_wa"] or o["cust_phone"],
                                                                              wa_track_text(o)),
                           w=warranty_info(o, parts), st=get_settings(), makers=makers, ph=phase(o),
                           pending=details_pending(o), adv_total=sum(a["amount"] for a in advances),
                           mk=marks_of(o["set_id"]))


def maker_from_form(db, ts):
    """The chosen maker, or a new one typed in the same box."""
    f = request.form
    mid = f.get("maker_id", type=int)
    if mid and H.scalar("SELECT 1 FROM prod_makers WHERE id = ? AND org_id = ?", (mid, H.org_id())):
        return mid
    name = clean(f.get("new_maker"), 120)
    if name:
        row = db.execute("SELECT id FROM prod_makers WHERE org_id = ? AND name = ? COLLATE NOCASE", (H.org_id(), name)).fetchone()
        if row:
            return row[0]
        return db.execute("INSERT INTO prod_makers (org_id, name, phone, work, created_at) VALUES (?,?,?,?,?)",
                          (H.org_id(), name, clean(f.get("new_maker_phone"), 40), clean(f.get("new_maker_work"), 120),
                           ts)).lastrowid
    return None


@bp.route("/production/orders/<int:order_id>/move", methods=["GET", "POST"])
@prod_view("stage")
def order_move(order_id):
    """Move an order to another stage. Into the production stage: asks the maker (+ optional amount). Into
    Dispatch: asks the GST bill number. Into Delivered: referral points are given. Back out: points taken back."""
    o = get_order(None, order_id)
    if o["status"] != "active":
        flash("This order is cancelled. Restore it first.", "error")
        return redirect(url_for("prod.order_view", order_id=order_id))
    stages = set_stages(o["set_id"])
    to = request.values.get("to", type=int)
    target = next((s for s in stages if s["id"] == to), None) if to else next_stage(o)
    if target is None or target["id"] == o["stage_id"]:
        return redirect(url_for("prod.order_view", order_id=order_id))
    mk = marks_of(o["set_id"])
    prod_stage = mk.get("production")
    enter_prod = prod_stage is not None and o["stage_sort"] < prod_stage["sort"] <= target["sort"]
    is_dispatch = mk["dispatch"]["sort"] <= target["sort"] and o["stage_sort"] < mk["dispatch"]["sort"]
    days = order_days(o)
    form = {"mfg_start": o["mfg_start"] or today(), "exp_finish": o["exp_finish"] or "", "maker_id": o["maker_id"],
            "maker_amount": o["maker_amount"], "bill_no": o["bill_no"] or "", "bill_date": o["bill_date"] or today(),
            "note": "", "days": days}
    if is_dispatch and not can("bill"):
        flash("Moving to Dispatch needs the GST bill number — ask someone with the 'Enter GST bill number' permission.",
              "error")
        return redirect(url_for("prod.order_view", order_id=order_id))
    needs = (enter_prod or (is_dispatch and not o["bill_no"]))
    if request.method == "POST" and (request.form.get("confirm") or not needs):
        f = request.form
        db = H.get_db()
        ts = _now()
        errors, sets = [], {"stage_id": target["id"], "stage_at": ts}
        if enter_prod:
            mid = maker_from_form(db, ts)
            if not mid:
                errors.append("Choose who makes it (Manufacturing in-charge), or type a new name.")
            start = H.valid_date(clean(f.get("mfg_start"))) or today()
            finish = H.valid_date(clean(f.get("exp_finish"))) if f.get("exp_finish") else ""
            if finish is None:
                errors.append("Expected finish date is invalid.")
            amt = money_in(f.get("maker_amount")) if can("maker") else o["maker_amount"]
            if amt == "bad":
                errors.append("Maker amount is not a number.")
            sets.update(maker_id=mid, mfg_start=start,
                        exp_finish=finish or (to_date(start) + timedelta(days=days)).isoformat(),
                        maker_amount=amt if can("maker") else o["maker_amount"],
                        maker_note=clean(f.get("maker_note"), 300) or o["maker_note"])
        if is_dispatch:
            bill_no = clean(f.get("bill_no"), 60) or (o["bill_no"] or "")
            bill_date = H.valid_date(clean(f.get("bill_date"))) or o["bill_date"] or today()
            if not bill_no:
                errors.append("Enter the GST bill number.")
            elif H.scalar("SELECT order_no FROM prod_orders WHERE org_id = ? AND bill_no = ? AND id != ?",
                          (H.org_id(), bill_no, order_id)):
                errors.append(f"GST bill number {bill_no} is already on another order.")
            sets.update(bill_no=bill_no, bill_date=bill_date)
        done_stage = mk.get("ready") or mk["dispatch"]
        if target["sort"] >= done_stage["sort"] and (o["stage_sort"] < done_stage["sort"] or not o["finished_at"]):
            sets["finished_at"] = ts   # production finished: counts in this month for the maker
        elif target["sort"] < done_stage["sort"]:
            sets["finished_at"] = None
        if prod_stage is not None and target["sort"] >= prod_stage["sort"]:
            sets.update(pinned_at=None, pinned_by=None)   # out of the queue: the 📌 is no longer needed
        if target["sort"] >= mk["delivered"]["sort"] and not o["delivered_date"]:
            sets["delivered_date"] = today()
        if target["sort"] < mk["delivered"]["sort"]:
            sets["delivered_date"] = None
        if errors:
            for e in errors:
                flash(e, "error")
            db.rollback()
            form.update({k: f.get(k, form.get(k)) for k in ("mfg_start", "exp_finish", "bill_no", "bill_date", "note")})
            form["maker_id"] = f.get("maker_id", type=int)
            form["maker_amount"] = money_in(f.get("maker_amount"))
        else:
            db.execute(f"UPDATE prod_orders SET {', '.join(k + ' = ?' for k in sets)}, updated_by = ?, updated_at = ?"
                       " WHERE id = ? AND org_id = ?", (*sets.values(), g.user["id"], ts, order_id, H.org_id()))
            note = clean(f.get("note"), 500)
            extra = []
            if enter_prod:
                mname = H.scalar("SELECT name FROM prod_makers WHERE id = ?", (sets["maker_id"],))
                extra.append(f"Maker: {mname}")
            if is_dispatch and sets.get("bill_no") != o["bill_no"]:
                extra.append(f"GST bill {sets['bill_no']} ({sets['bill_date']})")
            log(db, order_id, target["name"], " · ".join(extra + ([note] if note else [])), from_name=o["stage_name"],
                ts=ts)
            fresh = db.execute("SELECT * FROM prod_orders WHERE id = ?", (order_id,)).fetchone()
            if target["sort"] >= mk["delivered"]["sort"]:
                give_points(db, fresh, ts)
            elif o["stage_sort"] >= mk["delivered"]["sort"]:
                take_back_points(db, fresh, ts, "moved back from Delivered")
            db.commit()
            flash(f"Order #{o['order_no']} → {target['name']}.", "success")
            back = request.form.get("next") or ""
            if back.startswith("/") and not back.startswith("//"):
                return redirect(back)
            return redirect(url_for("prod.order_view", order_id=order_id))
    makers = H.query("SELECT * FROM prod_makers WHERE org_id = ? AND active = 1 ORDER BY name", (H.org_id(),))
    return render_template("production/move.html", o=o, target=target, enter_prod=enter_prod,
                           is_dispatch=is_dispatch and not o["bill_no"], form=form, makers=makers,
                           paise=lambda v: "" if v in (None, "bad") else __import__("cafe").paise_str(v),
                           back=request.values.get("next") or "")


@bp.route("/production/orders/<int:order_id>/maker-amount", methods=["POST"])
@prod_view("maker")
def order_maker_amount(order_id):
    o = get_order(None, order_id)
    amt = money_in(request.form.get("maker_amount"))
    if amt == "bad":
        flash("Maker amount is not a number.", "error")
    else:
        db = H.get_db()
        mid = maker_from_form(db, _now()) or o["maker_id"]
        db.execute("UPDATE prod_orders SET maker_amount = ?, maker_id = ?, updated_by = ?, updated_at = ? WHERE id = ?"
                   " AND org_id = ?", (amt, mid, g.user["id"], _now(), order_id, H.org_id()))
        db.commit()
        flash("Maker details saved.", "success")
    return redirect(url_for("prod.order_view", order_id=order_id))


@bp.route("/production/orders/<int:order_id>/cancel", methods=["POST"])
@prod_view("edit")
def order_cancel(order_id):
    o = get_order(None, order_id)
    db = H.get_db()
    ts = _now()
    if o["status"] == "active":
        reason = clean(request.form.get("reason"), 300)
        if not reason:
            flash("Write why the order is cancelled.", "error")
            return redirect(url_for("prod.order_view", order_id=order_id))
        db.execute("UPDATE prod_orders SET status = 'cancelled', cancel_reason = ?, updated_by = ?, updated_at = ?"
                   " WHERE id = ?", (reason, g.user["id"], ts, order_id))
        take_back_points(db, db.execute("SELECT * FROM prod_orders WHERE id = ?", (order_id,)).fetchone(), ts, "cancelled")
        log(db, order_id, "Cancelled", reason, from_name=o["stage_name"], ts=ts)
        flash(f"Order #{o['order_no']} cancelled.", "success")
    else:
        db.execute("UPDATE prod_orders SET status = 'active', cancel_reason = NULL, updated_by = ?, updated_at = ?"
                   " WHERE id = ?", (g.user["id"], ts, order_id))
        log(db, order_id, o["stage_name"], "Order restored", from_name="Cancelled", ts=ts)
        if o["stage_sort"] >= marks_of(o["set_id"])["delivered"]["sort"]:
            give_points(db, db.execute("SELECT * FROM prod_orders WHERE id = ?", (order_id,)).fetchone(), ts)
        flash(f"Order #{o['order_no']} restored.", "success")
    db.commit()
    return redirect(url_for("prod.order_view", order_id=order_id))


@bp.route("/production/orders/<int:order_id>/new-link", methods=["POST"])
@prod_view("edit")
def order_new_link(order_id):
    get_order(None, order_id)
    H.execute("UPDATE prod_orders SET token = ? WHERE id = ? AND org_id = ?", (secrets.token_urlsafe(12), order_id,
                                                                              H.org_id()))
    flash("New customer link made. The old link no longer works.", "success")
    return redirect(url_for("prod.order_view", order_id=order_id))


@bp.route("/production/orders/<int:order_id>/pin", methods=["POST"])
@prod_view("view")
def order_pin(order_id):
    """📌 Move to top of the Queue (Admin only). A reason is required; who / when goes to the history."""
    if not H.is_admin():
        abort(403)
    o = get_order(None, order_id)
    db = H.get_db()
    ts = _now()
    if request.form.get("action") == "unpin":
        db.execute("UPDATE prod_orders SET pinned_at = NULL, pinned_by = NULL, updated_at = ? WHERE id = ?", (ts, order_id))
        log(db, order_id, o["stage_name"], "📌 Removed from the top of the queue", ts=ts)
        flash(f"Order #{o['order_no']} back in its normal place.", "success")
    else:
        reason = clean(request.form.get("reason"), 300)
        if not reason:
            flash("Write why this order goes to the top.", "error")
            return redirect(url_for("prod.order_view", order_id=order_id))
        if phase(o) != "queue":
            flash("Only orders still in the queue can be moved to the top.", "error")
            return redirect(url_for("prod.order_view", order_id=order_id))
        db.execute("UPDATE prod_orders SET pinned_at = ?, pinned_by = ?, pin_reason = ?, updated_at = ? WHERE id = ?",
                   (ts, g.user["id"], reason, ts, order_id))
        log(db, order_id, o["stage_name"], f"📌 Moved to the top of the queue: {reason}", ts=ts)
        flash(f"Order #{o['order_no']} moved to the top of the queue.", "success")
    db.commit()
    back = request.form.get("next") or ""
    if back.startswith("/") and not back.startswith("//"):
        return redirect(back)
    return redirect(url_for("prod.order_view", order_id=order_id))


# ---------------------------------------------------------------- makers

@bp.route("/production/makers", methods=["GET", "POST"])
@prod_view("maker")
def makers():
    oid = H.org_id()
    if request.method == "POST":
        f = request.form
        name = clean(f.get("name"), 120)
        mid = f.get("id", type=int)
        if f.get("action") == "toggle" and mid:
            H.execute("UPDATE prod_makers SET active = 1 - active WHERE id = ? AND org_id = ?", (mid, oid))
        elif not name:
            flash("Name is required.", "error")
        elif mid:
            H.execute("UPDATE prod_makers SET name = ?, phone = ?, work = ?, note = ? WHERE id = ? AND org_id = ?",
                      (name, clean(f.get("phone"), 40), clean(f.get("work"), 120), clean(f.get("note"), 300), mid, oid))
            flash("Saved.", "success")
        else:
            H.execute("INSERT INTO prod_makers (org_id, name, phone, work, note, created_at) VALUES (?,?,?,?,?,?)",
                      (oid, name, clean(f.get("phone"), 40), clean(f.get("work"), 120), clean(f.get("note"), 300),
                       _now()))
            flash(f"{name} added.", "success")
        return redirect(url_for("prod.makers"))
    rows = H.query("""SELECT m.*,
                        (SELECT COUNT(*) FROM prod_orders o WHERE o.maker_id = m.id AND o.status = 'active') orders,
                        (SELECT COALESCE(SUM(o.maker_amount), 0) FROM prod_orders o WHERE o.maker_id = m.id
                           AND o.status = 'active') amount
                      FROM prod_makers m WHERE m.org_id = ? ORDER BY m.active DESC, m.name COLLATE NOCASE""", (oid,))
    working = {r["maker_id"]: r["n"] for r in H.query(
        "SELECT maker_id, COUNT(*) n FROM prod_orders WHERE org_id = ? AND status = 'active' AND maker_id IS NOT NULL"
        " GROUP BY maker_id", (oid,))}
    now_working = {}
    for o in order_rows(oid, "AND o.status = 'active' AND o.maker_id IS NOT NULL"):
        if phase(o) == "working":
            now_working[o["maker_id"]] = now_working.get(o["maker_id"], 0) + 1
    return render_template("production/makers.html", rows=rows, working=now_working, total=working)


@bp.route("/production/makers/<int:mid>")
@prod_view("maker")
def maker_view(mid):
    """One maker: totals, a month-wise table (an order counts in the month its production finished = reached
    Ready for Dispatch), and the orders (tap a month to see only that month)."""
    oid = H.org_id()
    m = H.query("SELECT * FROM prod_makers WHERE id = ? AND org_id = ?", (mid, oid), one=True) or abort(404)
    rows = order_rows(oid, "AND o.maker_id = ? AND o.status = 'active'", (mid,),
                      order_by="COALESCE(o.finished_at, '9999') DESC, o.mfg_start DESC, o.id DESC")
    t = today_date()
    months = []
    y, mo = t.year, t.month
    for _ in range(12):
        months.append(f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    table = {k: {"month": k, "finished": 0, "amount": 0, "started": 0} for k in months}
    making = finished = 0
    total_amount = 0
    items = []
    for o in rows:
        ph = phase(o)
        fin = to_date(o["finished_at"])
        if ph == "working":
            making += 1
        if fin:
            finished += 1
            total_amount += o["maker_amount"] or 0
            k = fin.isoformat()[:7]
            if k in table:
                table[k]["finished"] += 1
                table[k]["amount"] += o["maker_amount"] or 0
        st_ = to_date(o["mfg_start"])
        if st_ and st_.isoformat()[:7] in table:
            table[st_.isoformat()[:7]]["started"] += 1
        exp = to_date(o["exp_finish"])
        late_by = None
        if fin and exp:
            late_by = (fin - exp).days
        elif not fin and exp and ph == "working":
            late_by = (t - exp).days if t > exp else None
        items.append({"o": o, "fin": fin, "late_by": late_by, "ph": ph})
    month = request.args.get("month") or ""
    if re.fullmatch(r"\d{4}-\d{2}", month):
        items = [i for i in items if i["fin"] and i["fin"].isoformat()[:7] == month]
    else:
        month = ""
    return render_template("production/maker.html", m=m, table=[table[k] for k in months], items=items,
                           making=making, finished=finished, total=len(rows), total_amount=total_amount, month=month)


# ---------------------------------------------------------------- products (production defaults)

@bp.route("/production/products", methods=["GET", "POST"])
@prod_view("settings")
def products():
    oid = H.org_id()
    if request.method == "POST":
        name = clean(request.form.get("name"), 120)
        if not name:
            flash("Product name is required.", "error")
        elif H.scalar("SELECT 1 FROM products WHERE org_id = ? AND name = ? COLLATE NOCASE", (oid, name)):
            flash("A product with this name already exists.", "error")
        else:
            pid = H.execute("""INSERT INTO products (org_id, name, category, active, created_by, created_at, updated_at,
                               prod_days, warranty_months) VALUES (?,?,?,1,?,?,?,?,?)""",
                            (oid, name, clean(request.form.get("category"), 80), g.user["id"], _now(), _now(),
                             int_in(request.form.get("prod_days"), None, 0, 3650),
                             int_in(request.form.get("warranty_months"), None, 0, 600)))
            flash(f"{name} added.", "success")
            return redirect(url_for("prod.product_edit", pid=pid))
        return redirect(url_for("prod.products"))
    rows = H.query("""SELECT p.*, s.name set_name, (SELECT COUNT(*) FROM prod_parts x WHERE x.product_id = p.id) parts,
                        (SELECT COUNT(*) FROM prod_orders o WHERE o.product_id = p.id) orders
                      FROM products p LEFT JOIN prod_stage_sets s ON s.id = p.stage_set_id
                      WHERE p.org_id = ? ORDER BY p.active DESC, p.name COLLATE NOCASE""", (oid,))
    return render_template("production/products.html", rows=rows, st=get_settings(),
                           default_set=H.scalar("SELECT name FROM prod_stage_sets WHERE id = ?", (default_set_id(),)))


@bp.route("/production/products/<int:pid>", methods=["GET", "POST"])
@prod_view("settings")
def product_edit(pid):
    oid = H.org_id()
    p = H.query("SELECT * FROM products WHERE id = ? AND org_id = ?", (pid, oid), one=True) or abort(404)
    if request.method == "POST":
        f = request.form
        sid = f.get("stage_set_id", type=int)
        if sid and not H.scalar("SELECT 1 FROM prod_stage_sets WHERE id = ? AND org_id = ?", (sid, oid)):
            sid = None
        db = H.get_db()
        db.execute("""UPDATE products SET prod_days = ?, warranty_months = ?, stage_set_id = ?, updated_at = ?
                      WHERE id = ? AND org_id = ?""",
                   (int_in(f.get("prod_days"), None, 0, 3650) if (f.get("prod_days") or "").strip() else None,
                    int_in(f.get("warranty_months"), None, 0, 600) if (f.get("warranty_months") or "").strip() else None,
                    sid, _now(), pid, oid))
        db.execute("DELETE FROM prod_parts WHERE product_id = ?", (pid,))
        db.executemany("INSERT INTO prod_parts (org_id, product_id, name, company, months, serial, sort) VALUES (?,?,?,?,?,?,?)",
                       [(oid, pid, x["name"], x["company"], x["months"], "", x["sort"]) for x in parts_from_form()])
        db.commit()
        flash("Saved. New orders of this product get these defaults.", "success")
        return redirect(url_for("prod.products"))
    parts = H.query("SELECT * FROM prod_parts WHERE product_id = ? ORDER BY sort, id", (pid,))
    return render_template("production/product_form.html", p=p, parts=parts, sets=stage_sets(), st=get_settings())


# ---------------------------------------------------------------- warranty

@bp.route("/production/warranty")
@prod_view("view")
def warranty():
    oid = H.org_id()
    show = request.args.get("show") or "soon"
    rows = order_rows(oid, "AND o.status = 'active' AND o.bill_date IS NOT NULL", order_by="o.bill_date DESC")
    all_parts = {}
    for p in H.query("""SELECT x.* FROM prod_parts x JOIN prod_orders o ON o.id = x.order_id WHERE o.org_id = ?
                        ORDER BY x.sort, x.id""", (oid,)):
        all_parts.setdefault(p["order_id"], []).append(p)
    items = []
    for o in rows:
        w = warranty_info(o, all_parts.get(o["id"], []))
        m = w["main"]
        if not m:
            continue
        part_soon = [x for x in w["parts"] if x["left"] is not None and 0 <= x["left"] <= 30]
        if show == "soon" and not (0 <= m["left"] <= 30 or part_soon):
            continue
        if show == "active" and m["expired"]:
            continue
        if show == "expired" and not m["expired"]:
            continue
        items.append((o, w, part_soon))
    if show == "soon":
        items.sort(key=lambda t: min([t[1]["main"]["left"] if t[1]["main"]["left"] >= 0 else 9999]
                                     + [x["left"] for x in t[2]]))
    return render_template("production/warranty.html", items=items, show=show, st=get_settings())


# ---------------------------------------------------------------- customers & points

@bp.route("/production/customers")
@prod_view("view")
def customers():
    oid = H.org_id()
    q = (request.args.get("q") or "").strip()
    where, args = "", []
    if q:
        where = "AND (c.name LIKE ? OR c.phone LIKE ? OR c.unit_code LIKE ?)"
        args = [f"%{q}%"] * 3
    rows = H.query(f"""SELECT c.*, (SELECT COUNT(*) FROM prod_orders o WHERE o.customer_id = c.id) orders,
                         (SELECT COUNT(*) FROM prod_orders o WHERE o.referrer_id = c.id AND o.status = 'active') referred
                       FROM prod_customers c WHERE c.org_id = ? {where}
                       ORDER BY c.points DESC, c.name COLLATE NOCASE LIMIT 500""", (oid, *args))
    return render_template("production/customers.html", rows=rows, q=q, st=get_settings())


@bp.route("/production/customers/<int:cid>", methods=["GET", "POST"])
@prod_view("view")
def customer_view(cid):
    oid = H.org_id()
    c = H.query("SELECT * FROM prod_customers WHERE id = ? AND org_id = ?", (cid, oid), one=True) or abort(404)
    st = get_settings()
    if request.method == "POST":
        if not can("points"):
            abort(403)
        f = request.form
        db = H.get_db()
        ts = _now()
        n = int_in(f.get("points"), 0, 0, 100000)
        action = f.get("action")
        if action == "redeem":
            amt = money_in(f.get("amount"))
            if n <= 0 or n > c["points"]:
                flash(f"Points to redeem must be between 1 and {c['points']}.", "error")
            elif amt == "bad":
                flash("Amount is not a number.", "error")
            else:
                if amt is None:
                    amt = n * int(st["point_value"] or 0)
                db.execute("""INSERT INTO prod_points (org_id, customer_id, change, kind, amount, pay_mode, pay_date, note,
                              created_by, created_at) VALUES (?,?,?, 'redeem', ?,?,?,?,?,?)""",
                           (oid, cid, -n, amt, f.get("pay_mode") if f.get("pay_mode") in PAY_MODES else "",
                            H.valid_date(clean(f.get("pay_date"))) or today(), clean(f.get("note"), 300), g.user["id"], ts))
                refresh_points(db, cid)
                db.commit()
                import cafe
                flash(f"Redeemed {n} points ({cafe.rupees(amt)} paid).", "success")
        elif action == "adjust":
            sign = -1 if f.get("sign") == "-" else 1
            reason = clean(f.get("note"), 300)
            if n <= 0 or not reason:
                flash("Enter the points and a reason.", "error")
            elif sign < 0 and n > c["points"]:
                flash("Cannot remove more points than the customer has.", "error")
            else:
                db.execute("""INSERT INTO prod_points (org_id, customer_id, change, kind, note, created_by, created_at)
                              VALUES (?,?,?, 'adjust', ?,?,?)""", (oid, cid, sign * n, reason, g.user["id"], ts))
                refresh_points(db, cid)
                db.commit()
                flash("Points updated.", "success")
        return redirect(url_for("prod.customer_view", cid=cid))
    orders_ = order_rows(oid, "AND o.customer_id = ?", (cid,), order_by="o.order_no DESC")
    referred = order_rows(oid, "AND o.referrer_id = ?", (cid,), order_by="o.order_no DESC")
    ledger = H.query("""SELECT p.*, u.name user_name, o.order_no FROM prod_points p LEFT JOIN users u ON u.id = p.created_by
                        LEFT JOIN prod_orders o ON o.id = p.order_id WHERE p.customer_id = ? ORDER BY p.id DESC""", (cid,))
    latest = orders_[0] if orders_ else None
    wa = ""
    if latest:
        text = (f"Hi {c['name']},\nYour unit code at {g.org['name']} is {c['unit_code']}. Share it with friends — when "
                f"they order with your code, you earn points 🎁\nYour page: {track_url(latest)}")
        wa = wa_url(c["whatsapp"] or c["phone"], text)
    return render_template("production/customer_view.html", c=c, orders=orders_, referred=referred, ledger=ledger,
                           st=st, latest=latest, wa=wa, PAY_MODES=PAY_MODES, link=track_url(latest) if latest else "")


# ---------------------------------------------------------------- settings & stage sets

SWITCHES = [("customer_link", "Customer link (order tracking + warranty page)"), ("warranty", "Warranty"),
            ("parts", "Part warranties (parts from other companies)"), ("referral", "Unit code + referral points"),
            ("new_points_on", "Points for the new customer too (who ordered with a code)")]


@bp.route("/production/settings", methods=["GET", "POST"])
@prod_view("settings")
def settings():
    st = dict(get_settings())
    if request.method == "POST":
        f = request.form
        for k, _ in SWITCHES:
            st[k] = bool(f.get(k))
        st["referrer_points"] = int_in(f.get("referrer_points"), 1, 0, 100000)
        st["new_points"] = int_in(f.get("new_points"), 0, 0, 100000)
        pv = money_in(f.get("point_value"))
        if pv not in (None, "bad"):
            st["point_value"] = pv
        st["default_warranty"] = int_in(f.get("default_warranty"), 12, 0, 600)
        st["default_days"] = int_in(f.get("default_days"), 10, 1, 3650)
        st["sheet_title"] = clean(f.get("sheet_title"), 60) or DEFAULT_SETTINGS["sheet_title"]
        save_settings(st)
        flash("Production settings saved.", "success")
        return redirect(url_for("prod.settings"))
    sets = H.query("""SELECT s.*, (SELECT COUNT(*) FROM prod_orders o WHERE o.set_id = s.id) orders,
                        (SELECT GROUP_CONCAT(name, ' → ') FROM (SELECT name FROM prod_stages x WHERE x.set_id = s.id
                           AND x.active = 1 ORDER BY sort, id)) path,
                        (SELECT COUNT(*) FROM products p WHERE p.stage_set_id = s.id) products
                      FROM prod_stage_sets s WHERE s.org_id = ? ORDER BY s.is_default DESC, s.name""", (H.org_id(),))
    import cafe
    return render_template("production/settings.html", st=st, switches=SWITCHES, sets=sets,
                           point_value=cafe.paise_str(st["point_value"]))


@bp.route("/production/stage-sets/new", methods=["POST"])
@prod_view("settings")
def stage_set_new():
    oid = H.org_id()
    name = clean(request.form.get("name"), 60) or "New set"
    db = H.get_db()
    ts = _now()
    sid = db.execute("INSERT INTO prod_stage_sets (org_id, name, is_default, created_at) VALUES (?,?,0,?)",
                     (oid, name, ts)).lastrowid
    db.executemany("INSERT INTO prod_stages (org_id, set_id, name, sort, mark, created_at) VALUES (?,?,?,?,?,?)",
                   [(oid, sid, n, i, m, ts) for i, (n, m) in enumerate(DEFAULT_STAGES)])
    db.commit()
    flash(f"Stage set '{name}' made with the standard stages — rename / add / remove them below.", "success")
    return redirect(url_for("prod.stage_set", sid=sid))


@bp.route("/production/stage-sets/<int:sid>", methods=["GET", "POST"])
@prod_view("settings")
def stage_set(sid):
    """Edit one set: its name, how many stages, their names, order and marks. Only this set changes. A stage that
    has running orders cannot be removed until those orders are moved."""
    oid = H.org_id()
    s = H.query("SELECT * FROM prod_stage_sets WHERE id = ? AND org_id = ?", (sid, oid), one=True) or abort(404)
    current = H.query("""SELECT x.*, (SELECT COUNT(*) FROM prod_orders o WHERE o.stage_id = x.id) orders
                         FROM prod_stages x WHERE x.set_id = ? AND x.active = 1 ORDER BY sort, id""", (sid,))
    if request.method == "POST":
        f = request.form
        ids, names, marks = f.getlist("stage_id"), f.getlist("stage_name"), f.getlist("stage_mark")
        rows = []
        for sid_txt, name, mark in zip(ids, names, marks):
            name = clean(name, 60)
            if not name:
                continue
            rows.append({"id": int(sid_txt) if sid_txt.isdigit() else None, "name": name,
                         "mark": mark if mark in MARKS else None})
        errors = []
        if len(rows) < 2:
            errors.append("A set needs at least 2 stages.")
        seen = {}
        for i, r in enumerate(rows):
            if r["mark"]:
                if r["mark"] in seen:
                    errors.append(f"'{MARKS[r['mark']]}' can be on one stage only.")
                seen[r["mark"]] = i
        if len({r["name"].lower() for r in rows}) != len(rows):
            errors.append("Two stages have the same name.")
        if "production" not in seen:
            errors.append("Mark the stage where production starts (it asks for the maker).")
        if "dispatch" not in seen:
            errors.append("Mark the Dispatch stage (it asks for the GST bill number).")
        if rows and "delivered" in seen and seen["delivered"] != len(rows) - 1:
            errors.append("The Delivered stage must be the last one.")
        if "production" in seen and seen["production"] == 0:
            errors.append("The first stage is the queue — production must start at the 2nd stage or later.")
        order = [seen.get(k) for k in ("production", "ready", "dispatch", "delivered") if seen.get(k) is not None]
        if order != sorted(order):
            errors.append("Order of the marks must be: production starts → finished → dispatch → delivered.")
        kept = {r["id"] for r in rows if r["id"]}
        for c in current:
            if c["id"] not in kept and c["orders"]:
                errors.append(f"'{c['name']}' has {c['orders']} order(s). Move them to another stage first.")
        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("prod.stage_set", sid=sid))
        db = H.get_db()
        ts = _now()
        db.execute("UPDATE prod_stage_sets SET name = ? WHERE id = ?", (clean(f.get("set_name"), 60) or s["name"], sid))
        valid = {c["id"] for c in current}
        for i, r in enumerate(rows):
            if r["id"] in valid:
                db.execute("UPDATE prod_stages SET name = ?, mark = ?, sort = ? WHERE id = ? AND set_id = ?",
                           (r["name"], r["mark"], i, r["id"], sid))
            else:
                db.execute("INSERT INTO prod_stages (org_id, set_id, name, sort, mark, created_at) VALUES (?,?,?,?,?,?)",
                           (oid, sid, r["name"], i, r["mark"], ts))
        for c in current:
            if c["id"] not in kept:
                db.execute("UPDATE prod_stages SET active = 0 WHERE id = ?", (c["id"],))
        if f.get("make_default"):
            db.execute("UPDATE prod_stage_sets SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END WHERE org_id = ?",
                       (sid, oid))
        db.commit()
        flash("Stages saved. Only this set changed.", "success")
        return redirect(url_for("prod.stage_set", sid=sid))
    return render_template("production/stage_set.html", s=s, stages=current, MARKS=MARKS)


class _Host:
    def __getattr__(self, name):
        return getattr(H, name)


host = _Host()

import production_io  # noqa: E402,F401
