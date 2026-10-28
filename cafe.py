"""Cafe module for Real View CRM: billing (dine-in, takeaway, delivery), KOT, tables, menu with photos and offers,
a public digital menu with QR code and online ordering, customers with loyalty points, refer & earn and birthdays,
WhatsApp marketing, expenses and income/expense analysis.

The Super Admin decides per client organization whether it is a CRM, a Cafe, or both (organizations.app_type).
Everything here is scoped to the current organization (g.org) exactly like the CRM: the organization always
comes from the server-side session, never from a form. Amounts are stored as whole paise (INTEGER).

This file is loaded by app.py (cafe.init(app_module)); helpers such as query() and save_image() come from there.
"""
import csv
import io
import json
import re
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)

bp = Blueprint("cafe", __name__)
H = None  # the host app module (app.py), set by init()


def init(host):
    global H
    H = host
    host.app.register_blueprint(bp)
    host.app.jinja_env.filters["rs"] = rupees
    host.app.jinja_env.filters["rs0"] = lambda v: rupees(v, decimals=False)
    host.app.jinja_env.filters["pstr"] = paise_str
    host.app.jinja_env.filters["meas"] = fmt_measure
    host.app.jinja_env.globals["line_qty"] = line_amount_text
    host.app.jinja_env.globals["UNIT_PER"] = UNIT_PER
    host.app.jinja_env.globals["pay_text"] = pay_text


# ---------------------------------------------------------------- constants

APP_TYPES = {"crm": "CRM", "cafe": "Cafe", "both": "CRM + Cafe"}
ORDER_TYPES = {"dine_in": "Dine-in", "takeaway": "Takeaway", "delivery": "Delivery"}
PAYMENT_MODES = ["Cash", "UPI", "Card"]
PAY_COLS = (("Cash", "pay_cash"), ("UPI", "pay_upi"), ("Card", "pay_card"))
BILL_PAY_MODES = PAYMENT_MODES + ["Split", "Later"]   # what the billing screen can send
PAY_LABELS = {"Later": "Pay later", "Split": "Split"}
PAPER_SIZES = {"58": "58 mm thermal", "80": "80 mm thermal", "A4": "A4 / normal printer"}
GST_RATES = [0, 5, 12, 18, 28]
UNITS = {"pc": "Piece", "kg": "Kg", "l": "Litre"}
UNIT_PER = {"pc": "", "kg": "/ kg", "l": "/ litre"}
FOOD_TYPES = {"veg": "🟢 Veg", "nonveg": "🔴 Non-veg", "egg": "🟡 Egg"}
ITEM_TAGS = {"bestseller": "⭐ Bestseller", "new": "🆕 New", "spicy": "🌶 Spicy", "chef": "👨‍🍳 Chef's special"}
DEFAULT_EXPENSE_CATEGORIES = ["Raw materials", "Gas", "Salary", "Rent", "Electricity", "Water", "Maintenance",
                              "Packing", "Marketing", "Other"]
DEFAULT_INCOME_CATEGORIES = ["Catering", "Party order", "Other"]
ITEM_PHOTO_HINT = "Size: 600 × 600 px (square), under 50 KB"
BANNER_PHOTO_HINT = "Size: 1200 × 500 px (wide), under 50 KB"
PHOTO_MAX_KB = 50

# Permissions a staff member can be given (admins always have all of them).
PERMS = [
    ("bill_create", "Create bills / take orders"),
    ("bill_discount", "Give discount"),
    ("bill_cancel", "Cancel bills"),
    ("bills_view", "View old bills / reprint"),
    ("orders_online", "Handle online orders"),
    ("price_edit", "Change item prices on open orders"),
    ("payment_edit", "Change payment of old bills (other days)"),
    ("menu_edit", "Edit menu / prices / offers / tables"),
    ("expense_add", "Add expenses"),
    ("expense_view", "View / edit expenses"),
    ("reports_view", "View reports & analysis (graphs)"),
    ("customers_view", "View customers"),
    ("points_redeem", "Redeem points on bills"),
    ("points_adjust", "Add / remove points manually"),
    ("marketing", "Send WhatsApp marketing messages"),
    ("settings_edit", "Change cafe settings (GST, printer, design)"),
    ("staff_manage", "Add / edit staff"),
]
PERM_KEYS = [k for k, _ in PERMS]
ROLE_PRESETS = {
    "cashier": ["bill_create", "bills_view", "orders_online", "customers_view", "points_redeem"],
    "manager": [k for k in PERM_KEYS if k not in ("settings_edit", "staff_manage")],
}
ROLE_PRESET_LABELS = {"cashier": "Cashier", "manager": "Manager", "custom": "Custom"}

THEMES = {
    "classic": {"label": "Classic Cafe", "primary": "#6f4e37", "secondary": "#d4a373", "bg": "#fdf8f0", "text": "#2b1d14"},
    "fresh": {"label": "Fresh", "primary": "#15803d", "secondary": "#84cc16", "bg": "#f7fdf4", "text": "#14281b"},
    "bold": {"label": "Bold Red", "primary": "#dc2626", "secondary": "#111827", "bg": "#ffffff", "text": "#111827"},
    "royal": {"label": "Royal", "primary": "#1e3a8a", "secondary": "#d4af37", "bg": "#f8f7f2", "text": "#111a33"},
    "dark": {"label": "Dark Mode", "primary": "#f97316", "secondary": "#facc15", "bg": "#111111", "text": "#f5f5f5"},
    "pastel": {"label": "Pastel", "primary": "#db2777", "secondary": "#34d399", "bg": "#fff7fb", "text": "#3b1f2e"},
}
FONTS = {"modern": "Modern", "classic": "Classic", "rounded": "Rounded"}
LAYOUTS = {"grid": "Grid (big photos, 2 per row)", "list": "List (small photo, details beside)"}
SHAPES = {"round": "Rounded", "square": "Square"}

DEFAULT_SETTINGS = {
    "name": "", "tagline": "", "address": "", "phone": "", "whatsapp": "", "map_url": "", "about": "",
    "logo": "", "cover": "",
    "bill_footer": "Thank you! Visit again 🙏", "bill_prefix": "", "paper": "80", "kot_on": True,
    "kot_with_bill": True,
    "gst_on": False, "gstin": "", "gst_inclusive": True, "default_gst": 5,
    "menu_on": True, "online_on": False, "order_dine_in": True, "order_takeaway": True, "order_delivery": False,
    "delivery_note": "", "fssai": "",
    "theme": {"preset": "classic", **{k: v for k, v in THEMES["classic"].items() if k != "label"},
              "layout": "grid", "font": "modern", "shape": "round"},
    "loyalty": {"on": False, "mode": "percent", "percent": 5, "per_amount": 100, "per_points": 1,
                "point_value": 100, "min_bill": 0, "min_redeem": 0, "max_redeem_pct": 50, "expiry_days": 0},
    "referral": {"on": False, "referrer_points": 50, "new_points": 25},
    "birthday_on": False,
    "sort_pos": "popular", "sort_menu": "popular",   # most sold first, or the menu order
    "bt_print": False, "bt_auto": False,             # phone Bluetooth printing through the RawBT app
    "pos_tiles": "photo",                            # billing tiles with photos, or text only (slow network)
    "card_qr": False,                                # QR to the customer's My Card page on printed bills
}

def _now():
    return H.now()


def today():
    return H.today()


def today_date():
    return date.fromisoformat(H.today())


# ---------------------------------------------------------------- money

def rupees(paise, decimals=True):
    """₹1,234.50 (Indian grouping). Whole rupees drop the .00."""
    try:
        paise = int(paise or 0)
    except (TypeError, ValueError):
        paise = 0
    neg = paise < 0
    paise = abs(paise)
    r, p = divmod(paise, 100)
    s = str(r)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = head + "," + tail
    out = "₹" + s + (f".{p:02d}" if p and decimals else "")
    return ("-" if neg else "") + out


def to_paise(value, default=None):
    """'120', '120.5', '₹1,200.50' -> paise. Empty -> default. Invalid -> None."""
    v = re.sub(r"[₹,\s]", "", str(value if value is not None else ""))
    if v == "":
        return default
    if not re.match(r"^\d{1,7}(\.\d{1,2})?$", v):
        return None
    whole, _, frac = v.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def paise_str(paise):
    """Paise -> '120' / '120.50' for form inputs."""
    if paise is None:
        return ""
    r, p = divmod(int(paise), 100)
    return f"{r}.{p:02d}" if p else str(r)


def fmt_measure(measure, unit="kg"):
    """0.25 kg -> '250 g', 1.5 kg -> '1.5 kg', 0.5 l -> '500 ml'."""
    if measure is None or unit not in ("kg", "l"):
        return ""
    m = float(measure)
    small, big = ("g", "kg") if unit == "kg" else ("ml", "L")
    if m < 1:
        return f"{round(m * 1000):g} {small}"
    return f"{round(m, 3):g} {big}"


def line_amount_text(qty, measure, unit):
    """What a bill line shows for quantity: '2' for pieces, '250 g' or '2 × 250 g' for weighed items."""
    if unit in ("kg", "l") and measure:
        return (f"{qty} × " if qty and qty != 1 else "") + fmt_measure(measure, unit)
    return str(qty)


def to_int(value, default=0, lo=None, hi=None):
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def to_float(value, default=0.0, lo=None, hi=None):
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def phone10(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def token(n=12):
    return secrets.token_urlsafe(n)


# ---------------------------------------------------------------- organization type & mode

def app_type(org=None):
    org = org if org is not None else g.get("org")
    if not org:
        return "crm"
    try:
        t = org["app_type"]
    except (IndexError, KeyError):
        t = "crm"
    return t if t in APP_TYPES else "crm"


def has_cafe(org=None):
    return app_type(org) in ("cafe", "both")


def has_crm(org=None):
    return app_type(org) in ("crm", "both")


def current_mode():
    """'crm' or 'cafe' - what the current user is looking at."""
    t = app_type()
    if t == "cafe":
        return "cafe"
    if t == "both" and session.get("app_mode") == "cafe":
        return "cafe"
    return "crm"


# ---------------------------------------------------------------- permissions

def user_perms(user=None):
    user = user if user is not None else g.get("user")
    if not user:
        return set()
    if user["role"] in (H.SUPER_ADMIN, H.ORG_ADMIN):
        return set(PERM_KEYS)
    try:
        perms = json.loads(user["cafe_perms"] or "null")
    except (ValueError, TypeError):
        perms = None
    if perms is None:  # never set: use the role preset (cashier by default)
        perms = ROLE_PRESETS.get(user["cafe_role"] or "cashier", ROLE_PRESETS["cashier"])
    return {p for p in perms if p in PERM_KEYS}


def can(perm):
    if not g.get("user"):
        return False
    if g.get("viewing"):  # Super Admin looking around (read-only)
        return True
    return perm in user_perms()


def cafe_view(perm=None):
    """Decorator: logged in, the organization has the cafe, and (optionally) the user has the permission."""
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            def inner():
                if not has_cafe():
                    abort(404)
                if perm and not can(perm):
                    abort(403)
                return view(*args, **kwargs)
            return H.login_required(inner)()
        return wrapped
    return deco


def staff_form_perms():
    """Read the cafe role + permission ticks from the staff form. Returns (role, json or None)."""
    role = request.form.get("cafe_role")
    if role not in ROLE_PRESET_LABELS:
        role = "cashier"
    if role == "custom":
        perms = [p for p in request.form.getlist("cafe_perm") if p in PERM_KEYS]
    else:
        perms = ROLE_PRESETS[role]
    return role, json.dumps(perms)


# ---------------------------------------------------------------- settings

def _merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def get_settings(oid=None):
    oid = oid or H.org_id()
    cache = g.setdefault("cafe_settings", {})
    if oid not in cache:
        row = H.query("SELECT data FROM cafe_settings WHERE org_id = ?", (oid,), one=True)
        try:
            data = json.loads(row["data"]) if row else {}
        except (ValueError, TypeError):
            data = {}
        cache[oid] = _merge(DEFAULT_SETTINGS, data)
    return cache[oid]


def save_settings(data, oid=None):
    oid = oid or H.org_id()
    db = H.get_db()
    db.execute("""INSERT INTO cafe_settings (org_id, data, updated_by, updated_at) VALUES (?,?,?,?)
                  ON CONFLICT(org_id) DO UPDATE SET data = excluded.data, updated_by = excluded.updated_by,
                  updated_at = excluded.updated_at""", (oid, json.dumps(data), g.user["id"], _now()))
    db.commit()
    g.setdefault("cafe_settings", {}).pop(oid, None)


def cafe_name(st=None, org=None):
    st = st or get_settings()
    org = org or g.org
    return st["name"] or org["name"]


def menu_url(org=None, external=True, **kw):
    org = org or g.org
    return url_for("cafe.public_menu", code=org["code"].lower(), _external=external, **kw)


def card_url(tok, org=None, external=True):
    """The customer's private My Card page (points, bills, referral)."""
    org = org or g.org
    return url_for("cafe.my_card", code=org["code"].lower(), tok=tok, _external=external)


def qr_svg(url, scale=3):
    import segno
    return segno.make(url, error="m").svg_inline(scale=scale, border=1)


def ensure_categories(oid):
    """First use: give the organization the default expense/income categories."""
    if not H.scalar("SELECT 1 FROM cafe_ledger_categories WHERE org_id = ?", (oid,)):
        db = H.get_db()
        db.executemany("INSERT INTO cafe_ledger_categories (org_id, kind, name, sort) VALUES (?,?,?,?)",
                       [(oid, "expense", n, i) for i, n in enumerate(DEFAULT_EXPENSE_CATEGORIES)]
                       + [(oid, "income", n, i) for i, n in enumerate(DEFAULT_INCOME_CATEGORIES)])
        db.commit()


DEFAULT_CAFE_TEMPLATES = [
    ("Offer", "Hi {name} 👋\nThis week's special at {cafe_name}! 🎉\nSee the menu & order: {menu_link}"),
    ("We miss you", "Hi {name},\nIt's been a while! We miss you at {cafe_name} ☕\n"
                    "You have {points} points waiting. Come in soon!\n{menu_link}"),
    ("Birthday", "Happy Birthday {name}! 🎂🎉\nCelebrate with us at {cafe_name}. A special treat is waiting for you!"),
    ("Points expiring", "Hi {name},\nYour {points} points at {cafe_name} expire soon ⏳\nUse them on your next visit!"),
    ("Refer & Earn", "Hi {name},\nInvite your friends to {cafe_name} and earn points! 🎁\n"
                     "Share your link: {referral_link}"),
]


def ensure_templates(oid):
    if not H.scalar("SELECT 1 FROM cafe_templates WHERE org_id = ?", (oid,)):
        db = H.get_db()
        ts = _now()
        db.executemany("INSERT INTO cafe_templates (org_id, title, body, sort, created_at) VALUES (?,?,?,?,?)",
                       [(oid, t, b, i, ts) for i, (t, b) in enumerate(DEFAULT_CAFE_TEMPLATES)])
        db.commit()


# ---------------------------------------------------------------- menu helpers

def offer_active(item, day=None):
    if item["offer_price"] is None or item["offer_price"] >= item["price"]:
        return False
    day = day or today()
    if item["offer_from"] and day < item["offer_from"]:
        return False
    if item["offer_to"] and day > item["offer_to"]:
        return False
    return True


def effective_price(item, day=None):
    return item["offer_price"] if offer_active(item, day) else item["price"]


def item_dict(r, day=None):
    on = offer_active(r, day)
    tags = [t for t in (r["tags"] or "").split(",") if t in ITEM_TAGS]
    return {
        "id": r["id"], "name": r["name"], "desc": r["description"] or "", "cat": r["category_id"],
        "price": effective_price(r, day), "mrp": r["price"] if on else None,
        "off": round((r["price"] - r["offer_price"]) * 100 / r["price"]) if on and r["price"] else 0,
        "photo": url_for("media", name=r["photo"]) if r["photo"] else "",
        "type": r["food_type"] or "", "tags": tags, "available": bool(r["available"]), "gst": r["gst_rate"],
        "sno": r["serial"], "unit": r["unit"] or "pc", "barcode": r["barcode"] or "", "pin": bool(r["pinned"]),
    }


def popularity(oid, days=30):
    """{item_id: how much sold in the last `days` days} (pieces, or packs for weighed items)."""
    since = (today_date() - timedelta(days=days)).isoformat()
    rows = H.query("""SELECT bi.item_id, SUM(bi.qty) n FROM cafe_bill_items bi JOIN cafe_bills b ON b.id = bi.bill_id
                      WHERE b.org_id = ? AND b.status = 'paid' AND b.bill_date >= ? AND bi.item_id IS NOT NULL
                      GROUP BY bi.item_id""", (oid, since))
    return {r["item_id"]: r["n"] for r in rows}


def menu_data(oid, for_public=False, sort=None):
    """(categories, items). sort='popular': pinned items first, then the most sold in the last 30 days."""
    cats = H.query("SELECT * FROM menu_categories WHERE org_id = ? AND active = 1 ORDER BY sort, name COLLATE NOCASE",
                   (oid,))
    items = H.query("""SELECT i.* FROM menu_items i LEFT JOIN menu_categories c ON c.id = i.category_id
                       WHERE i.org_id = ? AND i.active = 1 ORDER BY COALESCE(c.sort, 9999), i.sort,
                       i.name COLLATE NOCASE""", (oid,))
    day = today()
    out = [item_dict(i, day) for i in items]
    if sort == "popular":
        pop = popularity(oid)
        order = {it["id"]: n for n, it in enumerate(out)}
        out.sort(key=lambda it: (not it["pin"], -pop.get(it["id"], 0), order[it["id"]]))
    elif sort == "menu":
        order = {it["id"]: n for n, it in enumerate(out)}
        out.sort(key=lambda it: (not it["pin"], order[it["id"]]))
    if for_public:  # serial numbers and barcodes are for staff only
        for i in out:
            i.pop("sno", None)
            i.pop("barcode", None)
    return ([{"id": c["id"], "name": c["name"]} for c in cats], out)


def next_serial(oid=None):
    return (H.scalar("SELECT MAX(serial) FROM menu_items WHERE org_id = ? AND active = 1", (oid or H.org_id(),)) or 0) + 1


def get_item(iid):
    return H.query("SELECT * FROM menu_items WHERE id = ? AND org_id = ? AND active = 1", (iid, H.org_id()),
                   one=True) or abort(404)


def get_category(cid):
    return H.query("SELECT * FROM menu_categories WHERE id = ? AND org_id = ?", (cid, H.org_id()), one=True) \
        or abort(404)


def save_photo(field, box, fit):
    f = request.files.get(field)
    if not f or not f.filename:
        return None
    name = H.save_image(f, box=box, max_kb=PHOTO_MAX_KB, fit=fit)
    if not name:
        flash("That file is not a photo, so it was skipped.", "warning")
    return name


def live_banners(oid, cats_items=None):
    day = today()
    rows = H.query("""SELECT b.*, i.name item_name FROM menu_banners b LEFT JOIN menu_items i ON i.id = b.item_id
                      AND i.active = 1 WHERE b.org_id = ? AND b.active = 1 ORDER BY b.sort, b.id""", (oid,))
    out = []
    for b in rows:
        if b["start_date"] and day < b["start_date"] or b["end_date"] and day > b["end_date"]:
            continue
        out.append({"title": b["title"] or b["item_name"] or "", "photo": url_for("media", name=b["photo"])
                    if b["photo"] else "", "item": b["item_id"] if b["item_name"] else None, "auto": False})
    if not out and cats_items is not None:  # no banner made: offer items with photos become banners
        for it in cats_items[1]:
            if it["mrp"] and it["available"]:
                out.append({"title": it["name"], "photo": it["photo"], "item": it["id"], "auto": True,
                            "price": it["price"], "mrp": it["mrp"], "off": it["off"]})
            if len(out) >= 6:
                break
    return out


def theme_css(theme):
    t = dict(DEFAULT_SETTINGS["theme"], **(theme or {}))
    safe = {k: (t.get(k) if re.match(r"^#[0-9a-fA-F]{6}$", str(t.get(k) or "")) else DEFAULT_SETTINGS["theme"][k])
            for k in ("primary", "secondary", "bg", "text")}
    return safe, (t.get("layout") if t.get("layout") in LAYOUTS else "grid"), \
        (t.get("font") if t.get("font") in FONTS else "modern"), (t.get("shape") if t.get("shape") in SHAPES else "round")


# ---------------------------------------------------------------- customers & points

def customer_by_phone(phone, oid=None):
    p = phone10(phone)
    if not p:
        return None
    return H.query("SELECT * FROM customers WHERE org_id = ? AND wa_norm = ?", (oid or H.org_id(), p), one=True)


def new_referral_code(db, oid):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(50):
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if not db.execute("SELECT 1 FROM customers WHERE org_id = ? AND referral_code = ?", (oid, code)).fetchone():
            return code
    return secrets.token_hex(5).upper()


def valid_birthday(value):
    """'MM-DD' from 'MM-DD', 'YYYY-MM-DD' or a day/month pair; '' when empty; None when invalid."""
    v = (value or "").strip()
    if not v:
        return ""
    m = re.match(r"^(?:\d{4}-)?(\d{2})-(\d{2})$", v)
    if not m:
        return None
    mm, dd = int(m.group(1)), int(m.group(2))
    try:
        date(2000, mm, dd)
    except ValueError:
        return None
    return f"{mm:02d}-{dd:02d}"


def upsert_customer(db, oid, phone, name, birthday=None, referral=None, user_id=None):
    """Find the customer by WhatsApp number, or create them. Returns the customer id (or None without a phone)."""
    p = phone10(phone)
    if not p:
        return None
    name = (name or "").strip()[:80]
    row = db.execute("SELECT * FROM customers WHERE org_id = ? AND wa_norm = ?", (oid, p)).fetchone()
    st = get_settings(oid)
    bday = valid_birthday(birthday) if st["birthday_on"] and birthday else None
    if row:
        sets, args = [], []
        if name and name != row["name"] and (not row["name"] or row["name"] == "Customer"):
            sets.append("name = ?"); args.append(name)
        if bday and not row["birthday"]:
            sets.append("birthday = ?"); args.append(bday)
        if sets:
            db.execute(f"UPDATE customers SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                       args + [_now(), row["id"]])
        return row["id"]
    referred_by = None
    if referral and st["referral"]["on"]:
        ref = db.execute("SELECT id FROM customers WHERE org_id = ? AND (referral_code = ? OR wa_norm = ?)",
                         (oid, referral.strip().upper(), phone10(referral) or "-")).fetchone()
        referred_by = ref["id"] if ref else None
    cur = db.execute("""INSERT INTO customers (org_id, name, whatsapp, wa_norm, birthday, referral_code, referred_by,
                        created_by, created_at, card_token) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (oid, name or "Customer", phone.strip()[:20], p, bday or None, new_referral_code(db, oid),
                      referred_by, user_id, _now(), secrets.token_urlsafe(12)))
    return cur.lastrowid


def _expiry(st, start=None):
    days = to_int(st["loyalty"].get("expiry_days"), 0, 0, 3650)
    if not days:
        return None
    return ((start or today_date()) + timedelta(days=days)).isoformat()


def add_points(db, oid, cid, pts, kind, note="", bill_id=None, user_id=None):
    """Credit points (they can expire); returns pts."""
    if pts <= 0:
        return 0
    st = get_settings(oid)
    db.execute("""INSERT INTO points_ledger (org_id, customer_id, bill_id, change, kind, note, expires_at, remaining,
                  created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
               (oid, cid, bill_id, pts, kind, note, _expiry(st), pts, user_id, _now()))
    db.execute("UPDATE customers SET points = points + ? WHERE id = ? AND org_id = ?", (pts, cid, oid))
    return pts


def take_points(db, oid, cid, pts, kind, note="", bill_id=None, user_id=None):
    """Debit points, oldest (soonest expiring) first. Returns pts."""
    if pts <= 0:
        return 0
    left = pts
    rows = db.execute("""SELECT id, remaining FROM points_ledger WHERE customer_id = ? AND org_id = ? AND remaining > 0
                         ORDER BY CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END, expires_at, id""",
                      (cid, oid)).fetchall()
    for r in rows:
        if left <= 0:
            break
        use = min(left, r["remaining"])
        db.execute("UPDATE points_ledger SET remaining = remaining - ? WHERE id = ?", (use, r["id"]))
        left -= use
    db.execute("""INSERT INTO points_ledger (org_id, customer_id, bill_id, change, kind, note, remaining, created_by,
                  created_at) VALUES (?,?,?,?,?,?,0,?,?)""", (oid, cid, bill_id, -pts, kind, note, user_id, _now()))
    db.execute("UPDATE customers SET points = points - ? WHERE id = ? AND org_id = ?", (pts, cid, oid))
    return pts


def expire_points(oid):
    """Expired points leave the balance (an 'expired' line in the customer's history). Cheap to call often."""
    day = today()
    db = H.get_db()
    rows = db.execute("""SELECT id, customer_id, remaining FROM points_ledger
                         WHERE org_id = ? AND remaining > 0 AND expires_at IS NOT NULL AND expires_at < ?""",
                      (oid, day)).fetchall()
    if not rows:
        return
    ts = _now()
    for r in rows:
        db.execute("UPDATE points_ledger SET remaining = 0 WHERE id = ?", (r["id"],))
        db.execute("""INSERT INTO points_ledger (org_id, customer_id, change, kind, note, remaining, created_at)
                      VALUES (?,?,?, 'expired', 'Points expired', 0, ?)""", (oid, r["customer_id"], -r["remaining"], ts))
        db.execute("UPDATE customers SET points = points - ? WHERE id = ?", (r["remaining"], r["customer_id"]))
    db.commit()


def points_for_amount(total_paise, st):
    lo = st["loyalty"]
    if not lo["on"] or total_paise <= 0:
        return 0
    rupees_ = total_paise // 100
    if rupees_ < to_int(lo.get("min_bill"), 0, 0):
        return 0
    if lo.get("mode") == "per_amount":
        per = to_int(lo.get("per_amount"), 100, 1)
        return (rupees_ // per) * to_int(lo.get("per_points"), 1, 0)
    return int(rupees_ * to_float(lo.get("percent"), 0, 0, 100) // 100)


def point_value(st):
    return to_int(st["loyalty"].get("point_value"), 100, 1)  # paise per point


def max_redeemable(balance, payable_paise, st):
    """How many points may be used on a bill of payable_paise (before points)."""
    lo = st["loyalty"]
    if not lo["on"] or balance <= 0 or payable_paise <= 0:
        return 0
    if balance < to_int(lo.get("min_redeem"), 0, 0):
        return 0
    cap_value = payable_paise * to_int(lo.get("max_redeem_pct"), 100, 0, 100) // 100
    return max(0, min(balance, cap_value // point_value(st)))


# ---------------------------------------------------------------- bill maths

def parse_payment(mode, split, total):
    """-> (payment_mode, cash, upi, card, due) in paise. split = {"cash": "200", "upi": "150", "card": ""} for Split."""
    if mode in PAYMENT_MODES:
        amounts = {m: (total if m == mode else 0) for m in PAYMENT_MODES}
        return mode, amounts["Cash"], amounts["UPI"], amounts["Card"], 0
    if mode == "Later":
        return "Later", 0, 0, 0, total
    if mode != "Split":
        raise ValueError("Choose the payment mode (Cash / UPI / Card).")
    split = split if isinstance(split, dict) else {}
    got = {}
    for m in PAYMENT_MODES:
        v = to_paise(split.get(m.lower()), 0)
        if v is None or v < 0:
            raise ValueError(f"The {m} amount is not valid.")
        got[m] = v
    if sum(got.values()) != total:
        raise ValueError(f"Cash + UPI + Card must be {rupees(total)} (now {rupees(sum(got.values()))}).")
    used = [m for m in PAYMENT_MODES if got[m]]
    if len(used) == 1:
        return parse_payment(used[0], None, total)
    return "Split", got["Cash"], got["UPI"], got["Card"], 0


def pay_text(b):
    """'Cash', 'Cash ₹200 + UPI ₹150', 'Not paid ₹350' for a bill row/dict."""
    keys = b.keys() if hasattr(b, "keys") else []
    if "pay_cash" not in keys:
        return b["payment_mode"] or ""
    if b["due"]:
        return f"Not paid {rupees(b['due'])}"
    parts = [(m, b[c]) for m, c in PAY_COLS if b[c]]
    if len(parts) <= 1:
        return parts[0][0] if parts else (b["payment_mode"] or "")
    return " + ".join(f"{m} {rupees(v)}" for m, v in parts)


def pay_breakdown(oid, d_from, d_to):
    """[{k: 'Cash'|'UPI'|'Card'|'Not paid', n: bills, total: paise}] for valid bills between the dates (split bills
    count in each of their parts)."""
    r = H.query("""SELECT COALESCE(SUM(pay_cash), 0) cash, COALESCE(SUM(pay_upi), 0) upi,
                     COALESCE(SUM(pay_card), 0) card, COALESCE(SUM(due), 0) due,
                     SUM(pay_cash > 0) n_cash, SUM(pay_upi > 0) n_upi, SUM(pay_card > 0) n_card, SUM(due > 0) n_due
                   FROM cafe_bills WHERE org_id = ? AND status = 'paid' AND bill_date BETWEEN ? AND ?""",
                (oid, d_from, d_to), one=True)
    out = [{"k": k, "n": r["n_" + c] or 0, "total": r[c]} for k, c in
           (("Cash", "cash"), ("UPI", "upi"), ("Card", "card"), ("Not paid", "due")) if r[c]]
    return sorted(out, key=lambda x: -x["total"])


def compute_totals(lines, gst_on, inclusive, discount):
    """lines: [{'price': paise, 'qty': n, 'gst_rate': %}]. Returns subtotal, discount, taxable, cgst, sgst, gross."""
    subtotal = sum(l["price"] * l["qty"] for l in lines)
    discount = max(0, min(int(discount or 0), subtotal))
    factor = (subtotal - discount) / subtotal if subtotal else 0
    taxable = tax = 0.0
    for l in lines:
        amt = l["price"] * l["qty"] * factor
        rate = float(l.get("gst_rate") or 0) if gst_on else 0
        if rate > 0:
            if inclusive:
                base = amt * 100 / (100 + rate)
                taxable += base
                tax += amt - base
            else:
                taxable += amt
                tax += amt * rate / 100
        else:
            taxable += amt
    tax = int(round(tax))
    cgst = tax // 2
    sgst = tax - cgst
    if gst_on and inclusive:
        gross = subtotal - discount
        taxable = gross - tax
    else:
        taxable = int(round(taxable))
        gross = taxable + tax
    return {"subtotal": subtotal, "discount": discount, "taxable": int(taxable), "cgst": cgst, "sgst": sgst,
            "gross": int(gross)}


def round_rupee(paise):
    return int((paise + 50) // 100 * 100) if paise >= 0 else 0


# ---------------------------------------------------------------- template context

@bp.app_context_processor
def cafe_context():
    def _can(p):
        return can(p)
    ctx = dict(cafe_mode=False, has_cafe=False, has_crm=True, can=_can, APP_TYPES=APP_TYPES)
    if g.get("org") is not None and g.get("user") is not None:
        ctx.update(cafe_mode=current_mode() == "cafe", has_cafe=has_cafe(), has_crm=has_crm(),
                   app_type=app_type())
    return ctx


# ---------------------------------------------------------------- mode switch & home

@bp.route("/switch/<mode>", methods=["POST"])
def switch_mode(mode):
    @H.login_required
    def inner():
        if app_type() != "both" or mode not in ("crm", "cafe"):
            abort(404)
        session["app_mode"] = mode
        return redirect(url_for("cafe.home") if mode == "cafe" else url_for("dashboard"))
    return inner()


@bp.route("/cafe")
@cafe_view()
def home():
    oid = H.org_id()
    expire_points(oid)
    day = today()
    sales = H.query("""SELECT COUNT(*) n, COALESCE(SUM(total), 0) total FROM cafe_bills
                       WHERE org_id = ? AND bill_date = ? AND status = 'paid'""", (oid, day), one=True)
    by_mode = [{"payment_mode": m["k"], "total": m["total"]} for m in pay_breakdown(oid, day, day)]
    expense = H.scalar("""SELECT COALESCE(SUM(amount), 0) FROM cafe_ledger WHERE org_id = ? AND kind = 'expense'
                          AND entry_date = ? AND deleted_at IS NULL""", (oid, day))
    pending = H.scalar("SELECT COUNT(*) FROM cafe_orders WHERE org_id = ? AND status = 'pending'", (oid,))
    open_orders = H.query("""SELECT o.*, t.name table_name,
                               (SELECT COALESCE(SUM(price * qty), 0) FROM cafe_order_items i WHERE i.order_id = o.id) amount
                             FROM cafe_orders o LEFT JOIN cafe_tables t ON t.id = o.table_id
                             WHERE o.org_id = ? AND o.status = 'open' ORDER BY o.id""", (oid,))
    mmdd = day[5:]
    week = [(today_date() + timedelta(days=i)).isoformat()[5:] for i in range(7)]
    st = get_settings()
    birthdays = H.query(f"""SELECT * FROM customers WHERE org_id = ? AND birthday IN ({','.join('?' * 7)})
                            ORDER BY CASE WHEN birthday = ? THEN 0 ELSE 1 END, birthday""",
                        [oid] + week + [mmdd]) if st["birthday_on"] and can("customers_view") else []
    top_items = H.query("""SELECT bi.name, SUM(bi.qty) q, MAX(bi.unit) unit, SUM(COALESCE(bi.measure, 0) * bi.qty) meas
                           FROM cafe_bill_items bi JOIN cafe_bills b ON b.id = bi.bill_id
                           WHERE b.org_id = ? AND b.bill_date = ? AND b.status = 'paid' GROUP BY bi.name
                           ORDER BY q DESC LIMIT 5""", (oid, day))
    setup_missing = not H.scalar("SELECT 1 FROM menu_items WHERE org_id = ? AND active = 1", (oid,))
    return render_template("cafe/home.html", sales=sales, by_mode=by_mode, expense=expense, pending=pending,
                           open_orders=open_orders, birthdays=birthdays, top_items=top_items, mmdd=mmdd,
                           setup_missing=setup_missing, st=st)


class _Host:
    """Late-bound access to app.py helpers for the other cafe_*.py files (H is set when the app starts)."""
    def __getattr__(self, name):
        return getattr(H, name)


host = _Host()

# The remaining routes live in these files (kept separate so each stays readable).
import cafe_admin  # noqa: E402,F401
import cafe_pos  # noqa: E402,F401
import cafe_people  # noqa: E402,F401
import cafe_import  # noqa: E402,F401
import cafe_push  # noqa: E402,F401
