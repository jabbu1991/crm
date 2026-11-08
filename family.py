"""Family module for Real View CRM: a registration form for a family meetup that builds the family tree.

The tree starts from the root couple (e.g. the eldest grandparents). Their children are the "branches". Everyone
else joins through the public registration form (one registration = one person): they pick their branch, then
either "This is me" (a name already in the tree that is not registered yet) or how they are related
(Son / Daughter / Husband / Wife of someone in the tree). Every registration waits for Admin approval before it
shows in the tree. Generations and relation text ("Grandson of ...") are worked out by the software.

Also: fee by age (e.g. ₹800 above 5 years) with UPI QR, Paid / Not paid marked by the Admin, achievers list for an
honouring ceremony, cultural programs list, income & expense accounts, Excel export, and "Clear all" for testing.

Public pages (no login): /f/<code> (tree), /f/<code>/register (form), /f/<code>/r/<token> (own status).
The Super Admin switches the module on per client (organizations.mod_fam). Amounts are whole paise.
Loaded by app.py (family.init(app_module)).
"""
import io
import json
import re
import secrets
import threading
import time
from functools import wraps
from urllib.parse import quote

from flask import (Blueprint, abort, flash, g, make_response, redirect, render_template, request, send_file,
                   session, url_for)

bp = Blueprint("fam", __name__)
H = None  # the host app module (app.py)


def init(host):
    global H
    H = host
    host.app.register_blueprint(bp)
    host.app.jinja_env.globals["fcan"] = lambda p: can(p)
    host.app.jinja_env.globals["fam_pending"] = pending_count
    host.app.jinja_env.globals["fam_alerts_on"] = lambda: bool(g.get("org") and has_fam() and can("approve"))


# ---------------------------------------------------------------- constants

PERMS = [
    ("view", "See registrations, members, achievers and programs"),
    ("approve", "Approve / reject registrations"),
    ("edit", "Add / edit members, mark fee paid"),
    ("acc_view", "View accounts (income & expense)"),
    ("acc_add", "Add income / expense"),
    ("settings", "Event settings"),
]
PERM_KEYS = [k for k, _ in PERMS]

RELATIONS = {"son": "Son", "daughter": "Daughter", "husband": "Husband", "wife": "Wife"}
GENDER = {"son": "M", "husband": "M", "daughter": "F", "wife": "F"}
INCOME_CATS = ["Sponsorship", "Donation", "Other income"]
EXPENSE_CATS = ["Food", "Venue", "Sound & Light", "Decoration", "Gifts / Awards", "Printing", "Transport", "Other"]
PAY_MODES = ["Cash", "UPI", "Bank transfer"]
BRANCH_COLORS = ["#2563eb", "#db2777", "#16a34a", "#ea580c", "#7c3aed", "#0891b2", "#ca8a04", "#dc2626",
                 "#4f46e5", "#0d9488"]
PHOTO_BOX = (400, 400)
PHOTO_MAX_KB = 50

DEFAULT_SETTINGS = {
    "event_name": "Family Meetup",
    "family_name": "",          # e.g. "Kunjimuhammed Haji Family"
    "event_date": "",
    "venue": "",
    "about": "",
    "cover": None,              # cover / poster photo
    "fee": 50000,               # paise (₹500)
    "free_upto_age": 5,         # age 5 and below: no fee
    "upi_id": "",
    "upi_name": "",
    "upi_qr": None,             # own QR photo (optional; else made from the UPI ID)
    "reg_open": True,
    "last_date": "",
    "start": None,              # {"h": name, "h_late": 1, "w": name, "w_late": 1, "heads": [[name, son|daughter]]}
    # WhatsApp messages (Approve / Reject). A line whose {placeholders} are all empty is left out.
    "msg_paid_en": "", "msg_paid_ml": "", "msg_due_en": "", "msg_due_ml": "", "msg_reject_en": "", "msg_reject_ml": "",
}

DEFAULT_MESSAGES = {
    "msg_paid_en": ("Hi {name},\n"
                    "Your registration for *{event}* is approved ✅\n"
                    "Reg No: {reg_no}\n"
                    "We have received your registration fee of ₹{fee}. Thank you! 🙏\n"
                    "📅 {date}\n"
                    "📍 {venue}\n"
                    "🌳 See yourself in the family tree: {tree_link}"),
    "msg_paid_ml": ("പ്രിയ {name},\n"
                    "*{event}* രജിസ്ട്രേഷൻ അംഗീകരിച്ചു ✅\n"
                    "Reg No: {reg_no}\n"
                    "രജിസ്ട്രേഷൻ ഫീസ് ₹{fee} ലഭിച്ചു. നന്ദി! 🙏\n"
                    "📅 {date}\n"
                    "📍 {venue}\n"
                    "🌳 കുടുംബവൃക്ഷത്തിൽ നിങ്ങളെ കാണാം: {tree_link}"),
    "msg_due_en": ("Hi {name},\n"
                   "Your registration for *{event}* is approved ✅\n"
                   "Reg No: {reg_no}\n"
                   "Your registration fee of ₹{fee} is not received yet. Please pay it soon.\n"
                   "💳 Pay by UPI here: {status_link}\n"
                   "📅 {date}\n"
                   "📍 {venue}\n"
                   "🌳 See yourself in the family tree: {tree_link}"),
    "msg_due_ml": ("പ്രിയ {name},\n"
                   "*{event}* രജിസ്ട്രേഷൻ അംഗീകരിച്ചു ✅\n"
                   "Reg No: {reg_no}\n"
                   "രജിസ്ട്രേഷൻ ഫീസ് ₹{fee} ഇതുവരെ ലഭിച്ചിട്ടില്ല. ദയവായി ഉടൻ അടയ്ക്കുക.\n"
                   "💳 UPI വഴി അടയ്ക്കാൻ: {status_link}\n"
                   "📅 {date}\n"
                   "📍 {venue}\n"
                   "🌳 കുടുംബവൃക്ഷത്തിൽ നിങ്ങളെ കാണാം: {tree_link}"),
    "msg_reject_en": ("Hi {name},\n"
                      "Sorry, we could not accept your registration for *{event}*.\n"
                      "Reason: {reason}\n"
                      "Please register again: {form_link}"),
    "msg_reject_ml": ("പ്രിയ {name},\n"
                      "ക്ഷമിക്കണം, *{event}* രജിസ്ട്രേഷൻ സ്വീകരിക്കാൻ കഴിഞ്ഞില്ല.\n"
                      "കാരണം: {reason}\n"
                      "ദയവായി വീണ്ടും രജിസ്റ്റർ ചെയ്യുക: {form_link}"),
}
MSG_KEYS = list(DEFAULT_MESSAGES)
PLACEHOLDERS = ["name", "reg_no", "event", "date", "venue", "fee", "tree_link", "status_link", "form_link", "reason"]


def _now():
    return H.now()


def clean(v, n=200):
    return re.sub(r"\s+", " ", (v or "").strip())[:n]


def money_in(value):
    import cafe
    return cafe.to_paise(value, default=None)


def rs(paise):
    p = int(paise or 0)
    return "₹" + (f"{p // 100:,}" if p % 100 == 0 else f"{p / 100:,.2f}")


def int_in(value, lo=0, hi=150):
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def phone_in(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if 8 <= len(digits) <= 13 else None


# ---------------------------------------------------------------- permissions

def user_perms(user=None):
    user = user if user is not None else g.get("user")
    if not user:
        return set()
    if user["role"] in (H.SUPER_ADMIN, H.ORG_ADMIN):
        return set(PERM_KEYS)
    try:
        perms = json.loads(user["fam_perms"] or "null")
    except (ValueError, TypeError, IndexError, KeyError):
        perms = None
    return {p for p in (perms or []) if p in PERM_KEYS}


def can(perm):
    if not g.get("user"):
        return False
    if g.get("viewing"):
        return perm in PERM_KEYS
    return perm in user_perms()


def has_fam(org=None):
    return "fam" in H.org_modules(org if org is not None else g.get("org"))


def fam_view(perm=None, admin=False):
    """Decorator: logged in, Family is on for the organization and this person, and (optionally) the permission."""
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            def inner():
                if "fam" not in H.user_modules() or not (g.get("viewing") or user_perms()):
                    abort(404)
                if admin and not H.is_admin():
                    abort(403)
                if perm and not can(perm):
                    abort(403)
                if request.method == "GET" and session.get("app_mode") != "fam":
                    H.set_module("fam")
                return view(*args, **kwargs)
            return H.login_required(inner)()
        return wrapped
    return deco


def staff_form_perms():
    return [p for p in request.form.getlist("fam_perm") if p in PERM_KEYS]


def staff_form_extra(member):
    have = user_perms(member) if member is not None and member["role"] == H.STAFF else set()
    if request.method == "POST":
        have = set(request.form.getlist("fam_perm"))
    allowed = set(PERM_KEYS) if H.is_admin() else user_perms()
    return {"all": PERMS, "have": have, "allowed": allowed}


def pending_count():
    if not g.get("org") or not has_fam():
        return 0
    return H.scalar("SELECT COUNT(*) FROM fam_members WHERE org_id = ? AND status = 'pending'", (H.org_id(),)) or 0


# ---------------------------------------------------------------- settings

def get_settings(oid=None):
    oid = oid or H.org_id()
    cache = g.setdefault("fam_settings", {})
    if oid not in cache:
        row = H.query("SELECT data FROM fam_settings WHERE org_id = ?", (oid,), one=True)
        try:
            data = json.loads(row["data"]) if row else {}
        except (ValueError, TypeError):
            data = {}
        cache[oid] = {**DEFAULT_SETTINGS, **{k: v for k, v in data.items() if k in DEFAULT_SETTINGS}}
    return cache[oid]


def save_settings(data, oid=None):
    oid = oid or H.org_id()
    H.get_db().execute("""INSERT INTO fam_settings (org_id, data, updated_by, updated_at) VALUES (?,?,?,?)
                          ON CONFLICT(org_id) DO UPDATE SET data = excluded.data, updated_by = excluded.updated_by,
                          updated_at = excluded.updated_at""",
                       (oid, json.dumps(data), g.user["id"] if g.get("user") else None, _now()))
    H.get_db().commit()
    g.setdefault("fam_settings", {}).pop(oid, None)


def fee_for(age, late, st, attending=None):
    """Fee by age; nothing for the Late and for those not coming to the meetup."""
    if late or age is None or attending == 0:
        return 0
    return int(st["fee"] or 0) if age > int(st["free_upto_age"] or 0) else 0


def reg_open(st):
    if not st["reg_open"]:
        return False
    return not (st["last_date"] and H.today() > st["last_date"])


def upi_link(st, amount_paise):
    if not st["upi_id"]:
        return None
    amt = f"{amount_paise / 100:.2f}"
    return (f"upi://pay?pa={quote(st['upi_id'], safe='@.')}&pn={quote(st['upi_name'] or st['event_name'])}"
            f"&am={amt}&cu=INR&tn={quote(st['event_name'][:30])}")


def qr_svg(text, scale=4):
    import segno
    return segno.make(text, error="m").svg_inline(scale=scale, border=2)


def form_url(org=None, external=True, **kw):
    org = org or g.org
    return url_for("fam.public_form", code=org["code"].lower(), _external=external, **kw)


def tree_url(org=None, external=True, **kw):
    org = org or g.org
    return url_for("fam.public_tree", code=org["code"].lower(), _external=external, **kw)


# ---------------------------------------------------------------- WhatsApp messages (no API: opens WhatsApp)

def message_template(st, key):
    return (st.get(key) or "").strip() or DEFAULT_MESSAGES[key]


def fill_message(body, values):
    """Put the values in. A line whose placeholders are all empty is left out (e.g. no venue set)."""
    out = []
    for line in body.split("\n"):
        keys = re.findall(r"\{(\w+)\}", line)
        known = [k for k in keys if k in values]
        if known and not any(str(values[k] or "").strip() for k in known):
            continue
        out.append(re.sub(r"\{(\w+)\}", lambda mt: str(values.get(mt.group(1), mt.group(0)) or ""), line))
    return "\n".join(out).strip()


def message_values(m, st, org=None, reason=""):
    org = org or g.org
    code = org["code"].lower()
    tok = m["token"]
    return {"name": m["name"], "reg_no": m["reg_no"] or "", "event": st["event_name"], "date": st["event_date"],
            "venue": st["venue"], "fee": (m["fee"] or 0) // 100 or "",
            "tree_link": url_for("fam.public_tree", code=code, me=m["claim_of"] or m["id"], _external=True),
            "status_link": url_for("fam.public_status", code=code, tok=tok, _external=True) if tok else "",
            "form_link": url_for("fam.public_form", code=code, _external=True), "reason": reason}


def compose(kind, m, st, lang="en", paid=None, reason="", org=None):
    """kind: 'approve' (paid / due chosen by `paid`) or 'reject'."""
    lang = lang if lang in ("en", "ml") else "en"
    if kind == "reject":
        key = "msg_reject_" + lang
    else:
        paid = m["paid"] if paid is None else paid
        key = ("msg_paid_" if paid or not m["fee"] else "msg_due_") + lang
    return fill_message(message_template(st, key), message_values(m, st, org, reason))


def wa_target(m):
    return H.wa_number(m["whatsapp"] or m["phone"] or "")


# ---------------------------------------------------------------- the tree

def ordinal_rel(depth, gender):
    """depth 1 = child, 2 = grandchild, 3 = great-grandchild ..."""
    base = {"M": "son", "F": "daughter"}.get(gender, "child")
    if depth <= 0:
        return ""
    if depth == 1:
        word = base
    elif depth == 2:
        word = "grand" + base
    else:
        word = ("great-" * (depth - 2)) + "grand" + base
    return word[:1].upper() + word[1:]


def build(oid, include_pending=False):
    """All approved members (optionally also pending ones) worked into a tree.
    Returns dict: nodes (id -> info), roots (blood root ids), heads (branch head ids), order (display order)."""
    sts = ("approved", "pending") if include_pending else ("approved",)
    rows = H.query(f"SELECT * FROM fam_members WHERE org_id = ? AND status IN ({','.join('?' * len(sts))})"
                   " ORDER BY id", (oid, *sts))
    nodes = {r["id"]: dict(r) for r in rows}
    for n in nodes.values():
        n.update(children=[], spouses=[], gen=None, branch=None, depth=None)
    for n in nodes.values():
        if n["spouse_of"] and n["spouse_of"] in nodes:
            nodes[n["spouse_of"]]["spouses"].append(n["id"])
        elif n["parent_id"] and n["parent_id"] in nodes:
            nodes[n["parent_id"]]["children"].append(n["id"])
    root = next((n for n in nodes.values() if n["is_root"] and not n["spouse_of"]), None)
    for n in nodes.values():   # eldest first (the root's children keep the order they were entered in)
        if not n["is_root"]:
            n["children"].sort(key=lambda c: (-(nodes[c]["age"] if nodes[c]["age"] is not None else -1), c))
    roots = [root["id"]] if root else []
    heads = list(root["children"]) if root else []

    def walk(nid, gen, depth, branch):
        n = nodes[nid]
        if n["gen"] is not None:
            return
        n.update(gen=gen, depth=depth, branch=branch)
        for s in n["spouses"]:
            nodes[s].update(gen=gen, depth=depth, branch=branch)
        for c in n["children"]:
            walk(c, gen + 1, depth + 1, branch if branch is not None else c)
    for r in roots:
        walk(r, 1, 0, None)
    for n in nodes.values():
        b = n["branch"]
        n["bidx"] = heads.index(b) if b in heads else -1
        n["color"] = BRANCH_COLORS[heads.index(b) % len(BRANCH_COLORS)] if b in heads else "#475569"
        n["branch_name"] = nodes[b]["name"] if b in nodes else ""
    root_names = ""
    if root:
        root_names = root["name"] + "".join(" & " + nodes[s]["name"] for s in root["spouses"])
    for n in nodes.values():
        n["rel_text"], n["rel_sub"] = relation_text(n, nodes, root)
        n["root_names"] = root_names
    return {"nodes": nodes, "roots": roots, "heads": heads, "root": root}


def parents_text(n, nodes):
    p = nodes.get(n["parent_id"])
    if not p:
        return ""
    other = nodes.get(n["other_parent_id"]) if n["other_parent_id"] else None
    if not other and len(p["spouses"]) == 1:
        other = nodes.get(p["spouses"][0])
    return p["name"] + (" & " + other["name"] if other else "")


def relation_text(n, nodes, root):
    """('Son of A & B', 'Grandson of Root · A branch · Gen 3')"""
    if n["is_root"]:
        partner = nodes.get(n["spouse_of"]) if n["spouse_of"] else None
        return (f"{RELATIONS.get(n['relation'], 'Wife')} of {partner['name']}" if partner else "Family head"), "Gen 1"
    gen = f"Gen {n['gen']}" if n["gen"] else ""
    branch = f"{n['branch_name']} branch" if n.get("branch_name") and n["branch"] != n["id"] else ""
    if n["spouse_of"]:
        partner = nodes.get(n["spouse_of"])
        main = f"{RELATIONS.get(n['relation'], 'Spouse')} of {partner['name']}" if partner else "Spouse"
        return main, " · ".join(x for x in (branch, gen) if x)
    if n["parent_id"]:
        main = f"{ordinal_rel(1, n['gender'])} of {parents_text(n, nodes)}"
        sub = []
        if n["depth"] and n["depth"] >= 2 and root:
            sub.append(f"{ordinal_rel(n['depth'], n['gender'])} of {root['name']}")
        return main, " · ".join(x for x in (*sub, branch, gen) if x)
    return "", gen


def nested(t, nid):
    """Nested dicts for the tree template: person + spouses + children (recursively)."""
    n = t["nodes"][nid]
    return {"p": n, "spouses": [t["nodes"][s] for s in n["spouses"]],
            "children": [nested(t, c) for c in n["children"]]}


def member_choices(t, branch=None):
    """People a registration can attach to: [(id, label, kind, registered)] sorted by generation."""
    out = []
    for n in sorted(t["nodes"].values(), key=lambda n: (n["gen"] or 99, n["id"])):
        if n["gen"] is None or n["status"] != "approved":
            continue
        if branch and n["branch"] != branch:
            continue
        out.append(n)
    return out


def ensure_seed(oid=None):
    """Create the root couple and the branch heads from Settings → Family start (only when the tree is empty)."""
    oid = oid or H.org_id()
    st = get_settings(oid)
    start = st["start"]
    if not start or not start.get("h"):
        return False
    if H.scalar("SELECT COUNT(*) FROM fam_members WHERE org_id = ?", (oid,)):
        return False
    db, ts = H.get_db(), _now()
    hid = db.execute("""INSERT INTO fam_members (org_id, name, gender, relation, is_root, is_late, registered, status,
                        created_at) VALUES (?,?, 'M', 'root', 1, ?, 1, 'approved', ?)""",
                     (oid, start["h"], 1 if start.get("h_late") else 0, ts)).lastrowid
    wid = None
    if start.get("w"):
        wid = db.execute("""INSERT INTO fam_members (org_id, name, gender, relation, spouse_of, is_root, is_late,
                            registered, status, created_at) VALUES (?,?, 'F', 'wife', ?, 1, ?, 1, 'approved', ?)""",
                         (oid, start["w"], hid, 1 if start.get("w_late") else 0, ts)).lastrowid
    for name, rel in start.get("heads") or []:
        rel = rel if rel in ("son", "daughter") else "son"
        db.execute("""INSERT INTO fam_members (org_id, name, gender, relation, parent_id, other_parent_id, registered,
                      status, created_at) VALUES (?,?,?,?,?,?, 0, 'approved', ?)""",
                   (oid, name, GENDER[rel], rel, hid, wid, ts))
    db.commit()
    return True


def get_member(mid, oid=None):
    return H.query("SELECT * FROM fam_members WHERE id = ? AND org_id = ?", (mid, oid or H.org_id()),
                   one=True) or abort(404)


def next_reg_no(db, oid):
    return (db.execute("SELECT MAX(reg_no) FROM fam_members WHERE org_id = ?", (oid,)).fetchone()[0] or 0) + 1


def resolve_link(t, relation, of_id):
    """(parent_id, other_parent_id, spouse_of) for 'relation of person', or an error text."""
    target = t["nodes"].get(of_id)
    if relation not in RELATIONS:
        return None, "Choose how you are related."
    if not target or target["status"] != "approved" or target["gen"] is None:
        return None, "Choose the person you are related to."
    if relation in ("husband", "wife"):
        if target["spouse_of"] or target["is_root"]:
            return None, f"{target['name']} married into the family — choose the family member instead."
        return (None, None, of_id), None
    if target["spouse_of"]:   # child of a person who married in: the blood parent is the partner
        partner = t["nodes"].get(target["spouse_of"])
        if not partner:
            return None, "Choose the parent from the family."
        return (partner["id"], target["id"], None), None
    other = target["spouses"][0] if len(target["spouses"]) == 1 else None
    return (of_id, other, None), None


def possible_duplicates(t, n):
    """Members with the same name under the same parent / partner (shown on the approval card)."""
    name = (n["name"] or "").strip().lower()
    out = []
    for m in t["nodes"].values():
        if m["id"] == n["id"] or m["status"] != "approved" or (m["name"] or "").strip().lower() != name:
            continue
        if (n["parent_id"] and m["parent_id"] == n["parent_id"]) or (n["spouse_of"] and m["spouse_of"] == n["spouse_of"]) \
                or n["claim_of"] == m["id"]:
            out.append(m)
    return out


# ---------------------------------------------------------------- form data (public + admin)

ERR_ML = {
    "Enter the name.": "പേര് നൽകുക.",
    "Enter the age (in years).": "വയസ്സ് നൽകുക.",
    "Phone number looks wrong.": "ഫോൺ നമ്പർ ശരിയല്ലെന്ന് തോന്നുന്നു.",
    "Enter the phone number.": "ഫോൺ നമ്പർ നൽകുക.",
    "WhatsApp number looks wrong.": "WhatsApp നമ്പർ ശരിയല്ലെന്ന് തോന്നുന്നു.",
    "Tell us: coming to the meetup? Yes / No": "മീറ്റപ്പിന് വരുന്നുണ്ടോ? Yes / No തിരഞ്ഞെടുക്കുക.",
    "Choose your name from the list (or choose 'I am new').": "ലിസ്റ്റിൽ നിന്ന് നിങ്ങളുടെ പേര് തിരഞ്ഞെടുക്കുക (അല്ലെങ്കിൽ 'ഞാൻ പുതിയ ആളാണ്').",
    "Choose how you are related.": "ബന്ധം തിരഞ്ഞെടുക്കുക.",
    "Choose the person you are related to.": "ബന്ധമുള്ള ആളെ തിരഞ്ഞെടുക്കുക.",
    "Choose the parent from the family.": "കുടുംബത്തിൽ നിന്നുള്ള ഉപ്പ / ഉമ്മയെ തിരഞ്ഞെടുക്കുക.",
}

def person_fields(st, existing=None, admin=False):
    """Name, age, late, phone, WhatsApp, achievement, program, attending, UPI ref from the form. -> (data, errors)"""
    f = request.form
    data = {"name": clean(f.get("name"), 120), "is_late": 1 if f.get("is_late") else 0,
            "achievement": clean(f.get("achievement"), 500), "program": clean(f.get("program"), 200),
            "upi_ref": clean(f.get("upi_ref"), 60)}
    errors = []
    if not data["name"]:
        errors.append("Enter the name.")
    data["age"] = int_in(f.get("age"))
    if data["age"] is None and not data["is_late"] and not admin:
        errors.append("Enter the age (in years).")
    phone = f.get("phone")
    data["phone"] = phone_in(phone)
    if phone and phone.strip() and not data["phone"]:
        errors.append("Phone number looks wrong.")
    if not data["phone"] and not data["is_late"] and not admin:
        errors.append("Enter the phone number.")
    if f.get("wa_same") or not (f.get("whatsapp") or "").strip():
        data["whatsapp"] = data["phone"]
    else:
        data["whatsapp"] = phone_in(f.get("whatsapp"))
        if not data["whatsapp"]:
            errors.append("WhatsApp number looks wrong.")
    att = f.get("attending")
    if data["is_late"]:
        data["attending"] = 0
    elif att in ("1", "0"):
        data["attending"] = int(att)
    else:
        data["attending"] = None
        if not admin:
            errors.append("Tell us: coming to the meetup? Yes / No")
    data["fee"] = fee_for(data["age"], data["is_late"], st, data["attending"])
    data["sponsor"] = (f.get("sponsor") or "").strip()[:500]
    return data, errors


def save_member_photo(oid):
    f = request.files.get("photo")
    if not f or not f.filename:
        return None
    name = H.save_image(f, box=PHOTO_BOX, max_kb=PHOTO_MAX_KB, fit=True)
    if not name:
        flash("That file is not a photo, so it was skipped.", "warning")
    return name


# ---------------------------------------------------------------- public pages

_hits = {}
_hits_lock = threading.Lock()


def too_many(ip, limit=30):
    now_ts = time.time()
    with _hits_lock:
        hits = [t for t in _hits.get(ip, []) if now_ts - t < 3600]
        if len(hits) >= limit:
            _hits[ip] = hits
            return True
        hits.append(now_ts)
        _hits[ip] = hits
    return False


def public_org(code):
    org = H.query("SELECT * FROM organizations WHERE code = ? COLLATE NOCASE", (code,), one=True)
    if not org or not has_fam(org) or org["status"] != "Active":
        abort(404)
    return org


def node_json(n):
    return {"id": n["id"], "name": n["name"], "photo": url_for("media", name=n["photo"]) if n["photo"] else None,
            "late": bool(n["is_late"]), "reg": bool(n["registered"]), "rel": n["rel_text"], "sub": n["rel_sub"],
            "gen": n["gen"], "branch": n["branch_name"], "color": n["color"],
            "ach": n["achievement"] or "", "prog": n["program"] or "", "g": n["gender"],
            "bh": n["branch"], "msp": bool(n["spouse_of"]), "root": bool(n["is_root"])}


@bp.route("/f/<code>")
def public_tree(code):
    org = public_org(code)
    st = get_settings(org["id"])
    t = build(org["id"])
    people = {n["id"]: dict(node_json(n), kids=[t["nodes"][c]["name"] for c in n["children"]],
                            sp=[t["nodes"][s]["name"] for s in n["spouses"]])
              for n in t["nodes"].values() if n["gen"] is not None}
    tree = [nested(t, r) for r in t["roots"]]
    blood = [n for n in t["nodes"].values() if n["gen"] is not None]
    stats = {"members": len(blood), "registered": sum(1 for n in blood if n["registered"]),
             "gens": max((n["gen"] for n in blood), default=0)}
    heads = [t["nodes"][h] for h in t["heads"]]

    def ids(nid):   # the tree shape for the "Download image" drawing
        n = t["nodes"][nid]
        return {"id": nid, "sp": list(n["spouses"]), "ch": [ids(c) for c in n["children"]]}
    shape = [ids(r) for r in t["roots"]]
    resp = make_response(render_template("family/public_tree.html", org=org, st=st, tree=tree, people=people,
                                         stats=stats, heads=heads, shape=shape, focus=request.args.get("me", type=int),
                                         open_=reg_open(st)))
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@bp.route("/f/<code>/register", methods=["GET", "POST"])
def public_form(code):
    org = public_org(code)
    oid = org["id"]
    st = get_settings(oid)
    t = build(oid)
    if not t["root"]:
        return render_template("family/public_closed.html", org=org, st=st,
                               message="The family tree is not set up yet. Please try again later.")
    if not reg_open(st):
        return render_template("family/public_closed.html", org=org, st=st, message="Registration is closed.")
    errors, form = [], (request.form if request.method == "POST" else request.args)
    if request.method == "POST":
        if form.get("website"):   # honeypot
            return redirect(public_tree_url(org))
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        if too_many(ip):
            abort(429)
        data, errors = person_fields(st)
        claim, link = None, None
        mode = form.get("mode")
        if mode == "me":
            claim = t["nodes"].get(form.get("claim_id", type=int))
            if not claim or claim["registered"] or claim["is_root"] or claim["status"] != "approved":
                errors.append("Choose your name from the list (or choose 'I am new').")
        else:
            link, err = resolve_link(t, form.get("relation"), form.get("of_id", type=int))
            if err:
                errors.append(err)
        if not errors:
            db = H.get_db()
            tok = secrets.token_urlsafe(9)
            photo = save_member_photo(oid)
            rel = claim["relation"] if claim else form.get("relation")
            parent_id, other_id, spouse_of = (None, None, None) if claim else link
            db.execute("""INSERT INTO fam_members (org_id, name, gender, relation, parent_id, other_parent_id,
                                spouse_of, claim_of, is_late, registered, status, age, phone, whatsapp, achievement,
                                program, photo, attending, fee, upi_ref, reg_no, token, created_at, lang, sponsor)
                                VALUES (?,?,?,?,?,?,?,?,?, 1, 'pending', ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (oid, data["name"], claim["gender"] if claim else GENDER[rel], rel, parent_id, other_id,
                              spouse_of, claim["id"] if claim else None, data["is_late"], data["age"], data["phone"],
                              data["whatsapp"], data["achievement"] or None, data["program"] or None, photo,
                              data["attending"], data["fee"], data["upi_ref"] or None, next_reg_no(db, oid), tok,
                              _now(), "en" if form.get("lang") == "en" else "ml", data["sponsor"] or None))
            new_id = db.execute("SELECT id FROM fam_members WHERE token = ?", (tok,)).fetchone()[0]
            db.commit()
            notify_new_registration(org, new_id, data["name"])
            return redirect(url_for("fam.public_status", code=code, tok=tok, new=1,
                                    b=form.get("branch", type=int) or ""))
    branches = []
    for h in t["heads"]:
        hn = t["nodes"][h]
        people = [n for n in member_choices(t, h)]
        branches.append({"id": h, "name": hn["name"], "color": hn["color"],
                         "people": [{"id": n["id"], "name": n["name"], "rel": n["rel_text"], "reg": bool(n["registered"]),
                                     "spouse": bool(n["spouse_of"]), "gen": n["gen"],
                                     "photo": url_for("media", name=n["photo"]) if n["photo"] else None}
                                    for n in people]})
    return render_template("family/public_form.html", org=org, st=st, branches=branches, errors=errors, ERR_ML=ERR_ML,
                           form=form, RELATIONS=RELATIONS, fee_rs=(st["fee"] or 0) // 100,
                           pre_branch=request.values.get("b", type=int) or request.values.get("branch", type=int),
                           upi=upi_link(st, st["fee"] or 0),
                           upi_qr=None if st["upi_qr"] or not st["upi_id"] else qr_svg(upi_link(st, st["fee"] or 0)))


def public_tree_url(org):
    return url_for("fam.public_tree", code=org["code"].lower())


@bp.route("/f/<code>/r/<tok>")
def public_status(code, tok):
    org = public_org(code)
    st = get_settings(org["id"])
    m = H.query("SELECT * FROM fam_members WHERE token = ? AND org_id = ?", (tok, org["id"]), one=True) or abort(404)
    t = build(org["id"], include_pending=True)
    n = t["nodes"].get(m["id"])
    rel = ""
    if m["claim_of"] and m["claim_of"] in t["nodes"]:
        rel = t["nodes"][m["claim_of"]]["rel_text"]
    elif n:
        rel = n["rel_text"] or ""
    upi = upi_link(st, m["fee"]) if m["fee"] and not m["paid"] else None
    resp = make_response(render_template("family/public_status.html", org=org, st=st, m=m, rel=rel, upi=upi,
                                         upi_qr=qr_svg(upi) if upi and not st["upi_qr"] else None,
                                         new=request.args.get("new"), b=request.args.get("b", type=int)))
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


# ---------------------------------------------------------------- admin: dashboard

def branch_stats(t):
    rows = []
    for h in t["heads"]:
        ppl = [n for n in t["nodes"].values() if n["branch"] == h]
        rows.append({"name": t["nodes"][h]["name"], "color": t["nodes"][h]["color"], "total": len(ppl),
                     "registered": sum(1 for n in ppl if n["registered"]),
                     "coming": sum(1 for n in ppl if n["attending"] == 1)})
    return rows


def fee_sums(oid):
    r = H.query("""SELECT COALESCE(SUM(fee), 0) total, COALESCE(SUM(CASE WHEN paid = 1 THEN fee END), 0) got,
                   COUNT(CASE WHEN fee > 0 AND paid = 1 THEN 1 END) paid_n,
                   COUNT(CASE WHEN fee > 0 AND paid = 0 THEN 1 END) unpaid_n
                   FROM fam_members WHERE org_id = ? AND status IN ('approved', 'pending') AND registered = 1""",
                (oid,), one=True)
    return {"total": r["total"], "got": r["got"], "due": r["total"] - r["got"], "paid_n": r["paid_n"],
            "unpaid_n": r["unpaid_n"]}


@bp.route("/family")
@fam_view()
def home():
    oid = H.org_id()
    st = get_settings(oid)
    ensure_seed(oid)
    t = build(oid)
    blood = [n for n in t["nodes"].values() if n["gen"] is not None]
    stats = {"members": len(blood), "registered": sum(1 for n in blood if n["registered"] and not n["is_root"]),
             "not_registered": sum(1 for n in blood if not n["registered"]),
             "coming": sum(1 for n in blood if n["attending"] == 1), "pending": pending_count(),
             "gens": max((n["gen"] for n in blood), default=0),
             "achievers": sum(1 for n in blood if n["achievement"]),
             "programs": sum(1 for n in blood if n["program"])}
    recent = H.query("""SELECT * FROM fam_members WHERE org_id = ? AND registered = 1 AND is_root = 0
                        ORDER BY created_at DESC, id DESC LIMIT 8""", (oid,))
    link = form_url()
    tlink = tree_url()
    share = "https://wa.me/?text=" + quote(
        f"🌳 {st['event_name']}\nOpen our family tree, tap your name (or your father / mother) and register:\n{tlink}\n\n"
        f"🌳 {st['event_name']}\nകുടുംബവൃക്ഷം തുറന്ന് നിങ്ങളുടെ പേരിലോ ഉപ്പ / ഉമ്മയുടെ പേരിലോ tap ചെയ്ത് രജിസ്റ്റർ ചെയ്യുക:\n{tlink}")
    return render_template("family/home.html", st=st, stats=stats, fees=fee_sums(oid), branches=branch_stats(t),
                           recent=recent, form_link=link, tree_link=tlink, share=share,
                           qr=qr_svg(tlink, 5), setup_needed=not t["root"], rs=rs, nodes=t["nodes"])


@bp.route("/family/qr")
@fam_view()
def qr_poster():
    st = get_settings()
    return render_template("family/qr_poster.html", st=st, link=tree_url(), qr=qr_svg(tree_url(), 10))


# ---------------------------------------------------------------- admin: approvals

@bp.route("/family/approvals")
@fam_view("approve")
def approvals():
    oid = H.org_id()
    st = get_settings(oid)
    t = build(oid, include_pending=True)
    rows = [n for n in t["nodes"].values() if n["status"] == "pending"]
    rows.sort(key=lambda n: n["id"])
    for n in rows:
        n["dups"] = possible_duplicates(t, n)
        n["claim"] = t["nodes"].get(n["claim_of"]) if n["claim_of"] else None
        if n["gen"] is None and not n["claim"]:   # parent not approved / removed
            n["rel_text"] = n["rel_text"] or "Relation missing — edit before approving"
        n["problem"] = approve_problem(n)
        n["wa_to"] = wa_target(n)
        n["msgs"] = {f"{k}_{lang}": compose("approve", n, st, lang, paid=(k == "paid"))
                     for k in ("paid", "due") for lang in ("en", "ml")}
        n["msgs"].update({"reject_" + lang: compose("reject", n, st, lang, reason="{reason}") for lang in ("en", "ml")})
    rejected = H.query("""SELECT * FROM fam_members WHERE org_id = ? AND status = 'rejected'
                          ORDER BY approved_at DESC LIMIT 30""", (oid,))
    return render_template("family/approvals.html", rows=rows, rejected=rejected, rs=rs)


def approve_problem(m):
    """Why this registration cannot be approved now (None = it can)."""
    if m["claim_of"]:
        target = H.query("SELECT * FROM fam_members WHERE id = ? AND org_id = ?", (m["claim_of"], m["org_id"]), one=True)
        if not target or target["status"] != "approved":
            return "The person this registration claims is no longer in the tree. Edit it first."
        if target["registered"]:
            return f"{target['name']} is already registered. Reject this one (or edit it)."
        return None
    for col in ("parent_id", "spouse_of"):
        if m[col]:
            p = H.query("SELECT status FROM fam_members WHERE id = ?", (m[col],), one=True)
            if not p or p["status"] != "approved":
                return "The parent / partner of this person is not approved yet. Approve them first."
    if not m["parent_id"] and not m["spouse_of"]:
        return "This registration is not connected to anyone. Edit it first."
    return None


def approve_member(m, paid=None):
    """Approve a pending registration. A 'This is me' claim fills the existing (not registered) person.
    paid: True / False sets the fee Paid / Not paid (None = keep). Returns (error, id of the person in the tree)."""
    err = approve_problem(m)
    if err:
        return err, None
    db, ts, uid = H.get_db(), _now(), g.user["id"]
    if paid is not None and m["fee"]:
        if paid and not m["paid"]:
            m = dict(m, paid=1, paid_at=ts, paid_by=uid)
        elif not paid and m["paid"]:
            m = dict(m, paid=0, paid_at=None, paid_by=None)
    if m["claim_of"]:
        target_id = m["claim_of"]
        db.execute("""UPDATE fam_members SET name = ?, is_late = ?, registered = 1, age = ?, phone = ?, whatsapp = ?,
                      achievement = ?, program = ?, photo = COALESCE(?, photo), attending = ?, fee = ?, paid = ?,
                      paid_at = ?, paid_by = ?, upi_ref = ?, reg_no = ?, created_at = ?, approved_by = ?,
                      approved_at = ?, updated_at = ?, lang = ?, sponsor = COALESCE(?, sponsor) WHERE id = ?""",
                   (m["name"], m["is_late"], m["age"], m["phone"], m["whatsapp"], m["achievement"], m["program"],
                    m["photo"], m["attending"], m["fee"], m["paid"], m["paid_at"], m["paid_by"], m["upi_ref"],
                    m["reg_no"], m["created_at"], uid, ts, ts, m["lang"], m["sponsor"], target_id))
        db.execute("UPDATE fam_members SET token = NULL WHERE id = ?", (m["id"],))
        db.execute("DELETE FROM fam_members WHERE id = ?", (m["id"],))
        db.execute("UPDATE fam_members SET token = ? WHERE id = ?", (m["token"], target_id))
    else:
        target_id = m["id"]
        db.execute("UPDATE fam_members SET status = 'approved', paid = ?, paid_at = ?, paid_by = ?, approved_by = ?, "
                   "approved_at = ?, updated_at = ? WHERE id = ?",
                   (m["paid"], m["paid_at"], m["paid_by"], uid, ts, ts, m["id"]))
    db.commit()
    H.audit("family_approve", f"#{m['reg_no']} {m['name']}" + (f" · fee {'paid' if m['paid'] else 'not paid'}"
                                                               if m["fee"] else ""))
    return None, target_id


def mark_wa_sent(mid, kind):
    H.execute("UPDATE fam_members SET wa_sent_at = ?, wa_sent_kind = ? WHERE id = ?", (_now(), kind, mid))


def wants_json():
    return request.form.get("ajax") == "1"


def message_from_form(default):
    msg = (request.form.get("message") or "").replace("\r\n", "\n").strip()[:2000]
    return msg or default


@bp.route("/family/members/<int:mid>/approve", methods=["POST"])
@fam_view("approve")
def approve(mid):
    m = get_member(mid)
    if m["status"] != "pending":
        if wants_json():
            return {"ok": False, "error": "Already handled."}
        flash("Already handled.", "info")
        return redirect(request.form.get("next") or url_for("fam.approvals"))
    paid = None
    if "paid" in request.form or request.form.get("paid_box") == "1":
        paid = request.form.get("paid") == "1"
    st = get_settings()
    lang = request.form.get("lang") if request.form.get("lang") in ("en", "ml") else (m["lang"] or "en")
    msg = message_from_form(compose("approve", m, st, lang, paid))
    err, tid = approve_member(m, paid)
    phone = wa_target(m)
    if not err and request.form.get("send_wa") == "1" and phone:
        mark_wa_sent(tid, "approve")
    if wants_json():
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "phone": phone, "text": msg, "name": m["name"]}
    flash(err or f"✅ {m['name']} approved — now in the family tree.", "error" if err else "success")
    return redirect(request.form.get("next") or url_for("fam.approvals"))


@bp.route("/family/members/<int:mid>/reject", methods=["POST"])
@fam_view("approve")
def reject(mid):
    m = get_member(mid)
    reason = clean(request.form.get("reason"), 200)

    def fail(text):
        if wants_json():
            return {"ok": False, "error": text}
        flash(text, "error")
        return redirect(url_for("fam.approvals"))
    if not reason:
        return fail("Write the reason for rejecting.")
    if m["status"] != "pending":
        return fail("Already handled.")
    if H.scalar("SELECT COUNT(*) FROM fam_members WHERE (parent_id = ? OR spouse_of = ? OR other_parent_id = ?)"
                " AND status != 'rejected'", (mid, mid, mid)):
        return fail("Others are connected to this person. Move them first.")
    st = get_settings()
    lang = request.form.get("lang") if request.form.get("lang") in ("en", "ml") else (m["lang"] or "en")
    msg = message_from_form(compose("reject", m, st, lang, reason=reason))
    H.execute("UPDATE fam_members SET status = 'rejected', reject_reason = ?, approved_by = ?, approved_at = ? "
              "WHERE id = ?", (reason, g.user["id"], _now(), mid))
    phone = wa_target(m)
    if request.form.get("send_wa") == "1" and phone:
        mark_wa_sent(mid, "reject")
    H.audit("family_reject", f"#{m['reg_no']} {m['name']}: {reason}")
    if wants_json():
        return {"ok": True, "phone": phone, "text": msg, "name": m["name"]}
    flash(f"{m['name']} rejected.", "success")
    return redirect(url_for("fam.approvals"))


# ---------------------------------------------------------------- admin: members

SHOWS = [("all", "All"), ("registered", "Registered"), ("not_registered", "Not registered"), ("coming", "Coming"),
         ("not_coming", "Not coming"), ("not_paid", "Fee not paid"), ("paid", "Fee paid"), ("late", "Late")]


def filtered_members(t, branch=None, show="all", q=""):
    rows = [n for n in t["nodes"].values() if n["gen"] is not None]
    if branch:
        rows = [n for n in rows if n["branch"] == branch]
    q = (q or "").strip().lower()
    if q:
        rows = [n for n in rows if q in (n["name"] or "").lower() or q in (n["phone"] or "")
                or str(n["reg_no"] or "") == q]
    tests = {"registered": lambda n: n["registered"] and not n["is_root"],
             "not_registered": lambda n: not n["registered"], "coming": lambda n: n["attending"] == 1,
             "not_coming": lambda n: n["attending"] == 0, "not_paid": lambda n: n["fee"] and not n["paid"],
             "paid": lambda n: n["fee"] and n["paid"], "late": lambda n: n["is_late"]}
    if show in tests:
        rows = [n for n in rows if tests[show](n)]
    rows.sort(key=lambda n: (n["gen"], n["bidx"], n["id"], n["id"]))
    return rows


@bp.route("/family/members")
@fam_view("view")
def members():
    oid = H.org_id()
    t = build(oid)
    branch = request.args.get("branch", type=int)
    show = request.args.get("show") or "all"
    q = request.args.get("q") or ""
    rows = filtered_members(t, branch, show, q)
    st = get_settings(oid)
    for n in rows:
        n["wa_to"] = wa_target(n) if n["registered"] else ""
        n["wa_text"] = compose("approve", n, st, n["lang"] or "en") if n["wa_to"] else ""
    heads = [t["nodes"][h] for h in t["heads"]]
    return render_template("family/members.html", rows=rows, heads=heads, branch=branch, show=show, q=q,
                           SHOWS=SHOWS, rs=rs, wa=H.wa_number)


@bp.route("/family/members/<int:mid>/paid", methods=["POST"])
@fam_view("edit")
def mark_paid(mid):
    m = get_member(mid)
    paid = 1 if request.form.get("paid") == "1" else 0
    H.execute("UPDATE fam_members SET paid = ?, paid_at = ?, paid_by = ?, updated_at = ? WHERE id = ?",
              (paid, _now() if paid else None, g.user["id"] if paid else None, _now(), mid))
    H.audit("family_fee", f"{m['name']}: {'paid' if paid else 'not paid'}")
    flash(f"{m['name']}: fee marked {'Paid ✅' if paid else 'Not paid'}.", "success")
    return redirect(request.form.get("next") or url_for("fam.members"))


def link_options(t, exclude=None):
    """People a member can be connected to in the edit form."""
    out = []
    for n in member_choices(t):
        if exclude and n["id"] == exclude:
            continue
        out.append(n)
    return out


def descendants(t, mid):
    out, stack = set(), [mid]
    while stack:
        x = stack.pop()
        n = t["nodes"].get(x)
        if not n:
            continue
        for c in n["children"] + n["spouses"]:
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def member_form(m=None):
    oid = H.org_id()
    st = get_settings(oid)
    t = build(oid, include_pending=True)
    if request.method == "POST":
        data, errors = person_fields(st, existing=m, admin=True)
        link = None
        if m is not None and m["is_root"]:
            link = (m["parent_id"], m["other_parent_id"], m["spouse_of"])
            rel = m["relation"]
        elif m is not None and m["claim_of"] and m["status"] == "pending" and request.form.get("keep_claim"):
            link, rel = (None, None, None), m["relation"]
        else:
            rel = request.form.get("relation")
            of_id = request.form.get("of_id", type=int)
            if m is not None and (of_id == m["id"] or of_id in descendants(t, m["id"])):
                errors.append("A person cannot be connected under themselves.")
            else:
                link, err = resolve_link(t, rel, of_id)
                if err:
                    errors.append(err)
        if request.form.get("fee_manual"):
            data["fee"] = money_in(request.form.get("fee")) or 0
        if not errors:
            db, ts = H.get_db(), _now()
            parent_id, other_id, spouse_of = link
            if request.form.get("other_parent_id") and parent_id:
                op = request.form.get("other_parent_id", type=int)
                if op in t["nodes"][parent_id]["spouses"]:
                    other_id = op
            photo = save_member_photo(oid)
            paid = 1 if request.form.get("paid") else 0
            registered = 1 if request.form.get("registered") or (m is None and data["phone"]) else 0
            if m is not None and m["is_root"]:
                registered = 1
            if m is None:
                db.execute("""INSERT INTO fam_members (org_id, name, gender, relation, parent_id, other_parent_id,
                                    spouse_of, is_late, registered, status, age, phone, whatsapp, achievement, program,
                                    photo, attending, fee, paid, paid_at, paid_by, upi_ref, reg_no, token, created_by,
                                    created_at, approved_by, approved_at, sponsor)
                                    VALUES (?,?,?,?,?,?,?,?,?, 'approved', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (oid, data["name"], GENDER[rel], rel, parent_id, other_id, spouse_of, data["is_late"],
                                  registered, data["age"], data["phone"], data["whatsapp"], data["achievement"] or None,
                                  data["program"] or None, photo, data["attending"], data["fee"], paid,
                                  ts if paid else None, g.user["id"] if paid else None, data["upi_ref"] or None,
                                  next_reg_no(db, oid) if registered else None, secrets.token_urlsafe(9),
                                  g.user["id"], ts, g.user["id"], ts, data["sponsor"] or None))
                db.commit()
                H.audit("family_add", data["name"])
                flash(f"{data['name']} added to the tree.", "success")
                return redirect(url_for("fam.members", q=data["name"]))
            if photo and m["photo"]:
                H.remove_media(m["photo"])
            if request.form.get("remove_photo") and not photo and m["photo"]:
                H.remove_media(m["photo"])
                db.execute("UPDATE fam_members SET photo = NULL WHERE id = ?", (m["id"],))
            reg_no = m["reg_no"] or (next_reg_no(db, oid) if registered else None)
            keep_claim = m["claim_of"] if (m["status"] == "pending" and request.form.get("keep_claim")) else None
            db.execute("""UPDATE fam_members SET name = ?, gender = ?, relation = ?, parent_id = ?, other_parent_id = ?,
                          spouse_of = ?, claim_of = ?, is_late = ?, registered = ?, age = ?, phone = ?, whatsapp = ?,
                          achievement = ?, program = ?, photo = COALESCE(?, photo), attending = ?, fee = ?, paid = ?,
                          paid_at = CASE WHEN ? = 1 THEN COALESCE(paid_at, ?) END,
                          paid_by = CASE WHEN ? = 1 THEN COALESCE(paid_by, ?) END, upi_ref = ?, reg_no = ?,
                          sponsor = ?, updated_at = ? WHERE id = ?""",
                       (data["name"], m["gender"] if m["is_root"] else GENDER.get(rel, m["gender"]),
                        rel, parent_id, other_id, spouse_of, keep_claim, data["is_late"], registered, data["age"],
                        data["phone"], data["whatsapp"], data["achievement"] or None, data["program"] or None, photo,
                        data["attending"], data["fee"], paid, paid, ts, paid, g.user["id"], data["upi_ref"] or None,
                        reg_no, data["sponsor"] or None, ts, m["id"]))
            db.commit()
            H.audit("family_edit", data["name"])
            flash(f"{data['name']} saved.", "success")
            return redirect(request.form.get("next") or url_for("fam.members", q=data["name"]))
        for e in errors:
            flash(e, "error")
    cur_of, cur_rel = None, None
    if m is not None:
        cur_rel = m["relation"]
        cur_of = m["spouse_of"] or m["parent_id"]
    skip = descendants(t, m["id"]) | {m["id"]} if m is not None else set()
    opts = [n for n in link_options(t) if n["id"] not in skip]
    node = t["nodes"].get(m["id"]) if m is not None else None
    others = []
    if m is not None and m["parent_id"] and m["parent_id"] in t["nodes"]:
        others = [t["nodes"][s] for s in t["nodes"][m["parent_id"]]["spouses"]]
    merge_targets = [n for n in opts if not n["is_root"]] if m is not None else []
    return render_template("family/member_form.html", m=m, node=node, st=st, opts=opts, cur_of=cur_of,
                           cur_rel=cur_rel, RELATIONS=RELATIONS, others=others, merge_targets=merge_targets,
                           claim=t["nodes"].get(m["claim_of"]) if m is not None and m["claim_of"] else None, rs=rs,
                           pre_of=request.args.get("of", type=int), pre_rel=request.args.get("rel"))


@bp.route("/family/members/new", methods=["GET", "POST"])
@fam_view("edit")
def member_new():
    if not H.scalar("SELECT COUNT(*) FROM fam_members WHERE org_id = ? AND is_root = 1", (H.org_id(),)):
        flash("First set up the family start (root couple) in Settings.", "warning")
        return redirect(url_for("fam.settings"))
    return member_form()


@bp.route("/family/members/<int:mid>/edit", methods=["GET", "POST"])
@fam_view("edit")
def member_edit(mid):
    return member_form(get_member(mid))


@bp.route("/family/members/<int:mid>/delete", methods=["POST"])
@fam_view(admin=True)
def member_delete(mid):
    m = get_member(mid)
    if m["is_root"]:
        flash("The root couple cannot be deleted (use Clear all to start again).", "error")
        return redirect(url_for("fam.member_edit", mid=mid))
    linked = H.scalar("SELECT COUNT(*) FROM fam_members WHERE (parent_id = ? OR spouse_of = ? OR other_parent_id = ?"
                      " OR claim_of = ?) AND status != 'rejected'", (mid, mid, mid, mid))
    if linked:
        flash("Children / partner are connected to this person. Move them (or merge) first.", "error")
        return redirect(url_for("fam.member_edit", mid=mid))
    db = H.get_db()
    db.execute("UPDATE fam_members SET parent_id = NULL, spouse_of = NULL, other_parent_id = NULL, claim_of = NULL"
               " WHERE (parent_id = ? OR spouse_of = ? OR other_parent_id = ? OR claim_of = ?) AND org_id = ?",
               (mid, mid, mid, mid, H.org_id()))
    db.execute("DELETE FROM fam_members WHERE id = ? AND org_id = ?", (mid, H.org_id()))
    db.commit()
    H.remove_media(m["photo"])
    H.audit("family_delete", m["name"])
    flash(f"{m['name']} deleted.", "success")
    return redirect(url_for("fam.members"))


@bp.route("/family/members/<int:mid>/merge", methods=["POST"])
@fam_view(admin=True)
def member_merge(mid):
    """The same person was added twice: move everything to the one kept, fill its empty details, delete this one."""
    m = get_member(mid)
    target = get_member(request.form.get("into", type=int) or 0)
    t = build(H.org_id(), include_pending=True)
    if target["id"] == m["id"] or m["is_root"] or target["id"] in descendants(t, m["id"]):
        flash("Choose another person to merge into.", "error")
        return redirect(url_for("fam.member_edit", mid=mid))
    db = H.get_db()
    for col in ("parent_id", "spouse_of", "other_parent_id", "claim_of"):
        db.execute(f"UPDATE fam_members SET {col} = ? WHERE {col} = ? AND org_id = ?", (target["id"], mid, H.org_id()))
    fill = {}
    for col in ("age", "phone", "whatsapp", "achievement", "program", "photo", "attending", "upi_ref", "reg_no", "token"):
        if target[col] in (None, "") and m[col] not in (None, ""):
            fill[col] = m[col]
    if m["registered"] and not target["registered"]:
        fill.update(registered=1, fee=m["fee"], paid=m["paid"], paid_at=m["paid_at"], paid_by=m["paid_by"])
    elif m["paid"] and not target["paid"]:
        fill.update(paid=1, paid_at=m["paid_at"], paid_by=m["paid_by"])
    db.execute("UPDATE fam_members SET token = NULL WHERE id = ?", (mid,))
    db.execute("DELETE FROM fam_members WHERE id = ?", (mid,))
    if fill:
        db.execute(f"UPDATE fam_members SET {', '.join(k + ' = ?' for k in fill)}, updated_at = ? WHERE id = ?",
                   (*fill.values(), _now(), target["id"]))
    db.commit()
    if m["photo"] and fill.get("photo") != m["photo"]:
        H.remove_media(m["photo"])
    H.audit("family_merge", f"{m['name']} → {target['name']}")
    flash(f"Merged into {target['name']}.", "success")
    return redirect(url_for("fam.member_edit", mid=target["id"]))


# ---------------------------------------------------------------- achievers + programs

@bp.route("/family/achievers")
@fam_view("view")
def achievers():
    t = build(H.org_id())
    rows = [n for n in t["nodes"].values() if n["gen"] is not None and n["achievement"]]
    rows.sort(key=lambda n: (n["gen"], n["branch_name"], n["name"]))
    return render_template("family/achievers.html", rows=rows, st=get_settings())


@bp.route("/family/programs")
@fam_view("view")
def programs():
    t = build(H.org_id())
    groups = {}
    for n in t["nodes"].values():
        if n["gen"] is not None and n["program"]:
            key = n["program"].strip().lower()
            groups.setdefault(key, {"name": n["program"].strip(), "people": []})["people"].append(n)
    rows = sorted(groups.values(), key=lambda gr: gr["name"].lower())
    return render_template("family/programs.html", groups=rows, st=get_settings(),
                           total=sum(len(gr["people"]) for gr in rows))


# ---------------------------------------------------------------- accounts

@bp.route("/family/accounts")
@fam_view()
def accounts():
    if not (can("acc_view") or can("acc_add")):
        abort(403)
    oid = H.org_id()
    view_all = can("acc_view")
    sql = """SELECT a.*, u.name by_name FROM fam_accounts a LEFT JOIN users u ON u.id = a.created_by
             WHERE a.org_id = ?"""
    args = [oid]
    if not view_all:   # can only add: sees their own entries
        sql += " AND a.created_by = ?"
        args.append(g.user["id"])
    kind = request.args.get("kind") or "all"
    if kind in ("income", "expense"):
        sql += " AND a.kind = ?"
        args.append(kind)
    rows = H.query(sql + " ORDER BY a.entry_date DESC, a.id DESC", args)
    fees = fee_sums(oid)
    sums = H.query("""SELECT COALESCE(SUM(CASE WHEN kind = 'income' THEN amount END), 0) income,
                      COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount END), 0) expense
                      FROM fam_accounts WHERE org_id = ?""", (oid,), one=True)
    by_cat = H.query("""SELECT category, SUM(amount) amt FROM fam_accounts WHERE org_id = ? AND kind = 'expense'
                        GROUP BY category ORDER BY amt DESC""", (oid,))
    edit = None
    if request.args.get("edit", type=int) and H.is_admin():
        edit = H.query("SELECT * FROM fam_accounts WHERE id = ? AND org_id = ?",
                       (request.args.get("edit", type=int), oid), one=True)
    used = [r[0] for r in H.query("SELECT DISTINCT category FROM fam_accounts WHERE org_id = ?", (oid,))]
    custom = [c for c in used if c not in INCOME_CATS + EXPENSE_CATS]
    income_total = fees["got"] + sums["income"]
    return render_template("family/accounts.html", rows=rows, fees=fees, sums=sums, by_cat=by_cat, edit=edit,
                           kind=kind, INCOME_CATS=INCOME_CATS, EXPENSE_CATS=EXPENSE_CATS, custom=custom,
                           PAY_MODES=PAY_MODES, rs=rs, view_all=view_all, today_=H.today(),
                           income_total=income_total, balance=income_total - sums["expense"])


@bp.route("/family/accounts/save", methods=["POST"])
@fam_view("acc_add")
def account_save():
    oid = H.org_id()
    f = request.form
    eid = f.get("id", type=int)
    old = None
    if eid:
        if not H.is_admin():
            abort(403)
        old = H.query("SELECT * FROM fam_accounts WHERE id = ? AND org_id = ?", (eid, oid), one=True) or abort(404)
    kind = f.get("kind") if f.get("kind") in ("income", "expense") else "expense"
    amount = money_in(f.get("amount"))
    category = clean(f.get("new_category") or f.get("category"), 60)
    day = H.valid_date(f.get("entry_date")) or H.today()
    if not amount or amount <= 0 or not category:
        flash("Enter the amount and the category.", "error")
        return redirect(url_for("fam.accounts"))
    photo = None
    up = request.files.get("photo")
    if up and up.filename:
        photo = H.save_image(up, box=(1200, 1600), max_kb=200)
    mode = f.get("payment_mode") if f.get("payment_mode") in PAY_MODES else "Cash"
    vals = (kind, day, amount, category, clean(f.get("party"), 120) or None, mode, clean(f.get("note"), 300) or None)
    if old:
        H.execute("""UPDATE fam_accounts SET kind = ?, entry_date = ?, amount = ?, category = ?, party = ?,
                     payment_mode = ?, note = ?, photo = COALESCE(?, photo), updated_at = ? WHERE id = ?""",
                  (*vals, photo, _now(), eid))
        if photo and old["photo"]:
            H.remove_media(old["photo"])
        flash("Entry updated.", "success")
    else:
        H.execute("""INSERT INTO fam_accounts (org_id, kind, entry_date, amount, category, party, payment_mode, note,
                     photo, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (oid, *vals, photo, g.user["id"], _now()))
        flash(("Income" if kind == "income" else "Expense") + f" {rs(amount)} saved.", "success")
    return redirect(url_for("fam.accounts"))


@bp.route("/family/accounts/<int:eid>/delete", methods=["POST"])
@fam_view(admin=True)
def account_delete(eid):
    e = H.query("SELECT * FROM fam_accounts WHERE id = ? AND org_id = ?", (eid, H.org_id()), one=True) or abort(404)
    H.execute("UPDATE fam_members SET sponsor_done = 0, sponsor_acc_id = NULL WHERE sponsor_acc_id = ? AND org_id = ?",
              (eid, H.org_id()))
    H.execute("DELETE FROM fam_accounts WHERE id = ?", (eid,))
    H.remove_media(e["photo"])
    H.audit("family_account_delete", f"{e['kind']} {rs(e['amount'])} {e['category']}")
    flash("Entry deleted.", "success")
    return redirect(url_for("fam.accounts"))


# ---------------------------------------------------------------- sponsorship offers

@bp.route("/family/sponsors")
@fam_view("view")
def sponsors():
    oid = H.org_id()
    t = build(oid, include_pending=True)
    rows = [n for n in t["nodes"].values() if (n["sponsor"] or "").strip()]
    rows.sort(key=lambda n: (n["sponsor_done"], -(n["id"])))
    acc = {r["id"]: r for r in H.query("SELECT * FROM fam_accounts WHERE org_id = ? AND id IN (SELECT sponsor_acc_id "
                                        "FROM fam_members WHERE org_id = ? AND sponsor_acc_id IS NOT NULL)", (oid, oid))}
    got = sum(a["amount"] for a in acc.values())
    return render_template("family/sponsors.html", rows=rows, acc=acc, got=got, rs=rs, PAY_MODES=PAY_MODES,
                           today=H.today())


@bp.route("/family/sponsors/<int:mid>/received", methods=["POST"])
@fam_view("edit")
def sponsor_received(mid):
    m = get_member(mid)
    if m["sponsor_done"]:
        flash("Already marked received.", "info")
        return redirect(url_for("fam.sponsors"))
    amount = money_in(request.form.get("amount"))
    acc_id = None
    if amount and amount > 0:
        if not (can("acc_add") or H.is_admin()):
            flash("You can mark it received, but adding money to Accounts needs the 'Add income / expense' tick.",
                  "error")
            return redirect(url_for("fam.sponsors"))
        mode = request.form.get("payment_mode") if request.form.get("payment_mode") in PAY_MODES else "Cash"
        cur = H.get_db().execute("""INSERT INTO fam_accounts (org_id, kind, entry_date, amount, category, party,
                                    payment_mode, note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                 (H.org_id(), "income", H.valid_date(request.form.get("entry_date")) or H.today(),
                                  amount, "Sponsorship", m["name"], mode, (m["sponsor"] or "")[:300], g.user["id"],
                                  _now()))
        acc_id = cur.lastrowid
    H.get_db().execute("UPDATE fam_members SET sponsor_done = 1, sponsor_acc_id = ?, updated_at = ? WHERE id = ?",
                       (acc_id, _now(), mid))
    H.get_db().commit()
    H.audit("family_sponsor", f"{m['name']}: received" + (f" {rs(amount)}" if acc_id else ""))
    flash(f"🤝 {m['name']}: sponsorship received" + (f" — {rs(amount)} added to Accounts." if acc_id else "."),
          "success")
    return redirect(url_for("fam.sponsors"))


@bp.route("/family/sponsors/<int:mid>/undo", methods=["POST"])
@fam_view(admin=True)
def sponsor_undo(mid):
    m = get_member(mid)
    db = H.get_db()
    if m["sponsor_acc_id"]:
        db.execute("DELETE FROM fam_accounts WHERE id = ? AND org_id = ?", (m["sponsor_acc_id"], H.org_id()))
    db.execute("UPDATE fam_members SET sponsor_done = 0, sponsor_acc_id = NULL WHERE id = ?", (mid,))
    db.commit()
    H.audit("family_sponsor_undo", m["name"])
    flash(f"{m['name']}: marked not received (the Accounts entry was removed).", "success")
    return redirect(url_for("fam.sponsors"))


# ---------------------------------------------------------------- new registration: sound + phone notifications

def _push_subs(org_id):
    rows = H.query("""SELECT s.*, u.role, u.fam_perms FROM push_subscriptions s JOIN users u ON u.id = s.user_id
                      WHERE s.org_id = ? AND u.active = 1""", (org_id,))
    return [dict(r) for r in rows if "approve" in user_perms(r)]


def notify_new_registration(org, mid, name):
    """Phones of the Admin / approvers get "New registration" (even with the screen off). Never stops the form."""
    try:
        import cafe_push
        subs = _push_subs(org["id"])
        if not subs:
            return 0
        st = get_settings(org["id"])
        n = H.scalar("SELECT COUNT(*) FROM fam_members WHERE org_id = ? AND status = 'pending'", (org["id"],)) or 1
        data = json.dumps({"title": f"🌳 New registration: {name}",
                           "body": f"{st['event_name']} · {n} waiting for approval — tap to open",
                           "url": "/family/approvals", "tag": f"fam-{org['id']}-{mid}"}, ensure_ascii=False)
        cafe_push._dispatch(subs, data)
        return len(subs)
    except Exception:  # noqa: BLE001
        return 0


@bp.route("/family/pending.json")
@fam_view()
def pending_json():
    if not can("approve"):
        return {"count": 0, "latest": 0}
    r = H.query("SELECT COUNT(*) n, COALESCE(MAX(id), 0) latest FROM fam_members "
                "WHERE org_id = ? AND status = 'pending'", (H.org_id(),), one=True)
    last = H.query("SELECT name FROM fam_members WHERE org_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                   (H.org_id(),), one=True)
    return {"count": r["n"], "latest": r["latest"], "name": last["name"] if last else ""}


@bp.route("/family/push/key")
@fam_view("approve")
def push_key():
    import cafe_push
    return {"ok": True, "key": cafe_push.vapid()[1]}


@bp.route("/family/push/subscribe", methods=["POST"])
@fam_view("approve")
def push_subscribe():
    try:
        sub = json.loads(request.form.get("payload") or "{}")
        endpoint = str(sub["endpoint"])[:1000]
        keys = sub.get("keys") or {}
        p256dh, auth = str(keys["p256dh"])[:200], str(keys["auth"])[:100]
        assert endpoint.startswith("https://")
    except (ValueError, KeyError, TypeError, AssertionError):
        return {"ok": False, "error": "This browser cannot get notifications."}, 400
    db = H.get_db()
    db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    db.execute("""INSERT INTO push_subscriptions (org_id, user_id, endpoint, p256dh, auth, created_at)
                  VALUES (?,?,?,?,?,?)""", (g.org["id"], g.user["id"], endpoint, p256dh, auth, _now()))
    db.commit()
    return {"ok": True}


@bp.route("/family/push/unsubscribe", methods=["POST"])
@fam_view()
def push_unsubscribe():
    H.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
              ((request.form.get("endpoint") or "")[:1000], g.user["id"]))
    return {"ok": True}


@bp.route("/family/push/test", methods=["POST"])
@fam_view("approve")
def push_test():
    import cafe_push
    rows = [dict(r) for r in H.query("SELECT * FROM push_subscriptions WHERE user_id = ?", (g.user["id"],))]
    if not rows:
        return {"ok": False, "error": "Notifications are not on for this phone yet."}
    cafe_push._dispatch(rows, json.dumps({"title": "🌳 Test notification", "body": "New registrations will show like this.",
                                          "url": "/family/approvals", "tag": "fam-test"}, ensure_ascii=False))
    return {"ok": True}


# ---------------------------------------------------------------- Excel

@bp.route("/family/export.xlsx")
@fam_view("view")
def export():
    from openpyxl import Workbook
    from openpyxl.styles import Font
    oid = H.org_id()
    t = build(oid, include_pending=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Members"
    head = ["Reg No", "Name", "Relation", "More", "Branch", "Gen", "Age", "Phone", "WhatsApp", "Coming", "Fee (₹)",
            "Paid", "UPI ref", "Achievement", "Cultural program", "Sponsorship", "Sponsorship received", "Late",
            "Registered", "Status"]
    ws.append(head)
    rows = sorted(t["nodes"].values(), key=lambda n: (n["gen"] or 99, n["branch_name"], n["id"]))
    for n in rows:
        ws.append([n["reg_no"], n["name"], n["rel_text"], n["rel_sub"], n["branch_name"], n["gen"], n["age"],
                   n["phone"], n["whatsapp"], {1: "Yes", 0: "No"}.get(n["attending"], ""),
                   (n["fee"] or 0) / 100 if n["fee"] else "", "Yes" if n["paid"] else ("No" if n["fee"] else ""),
                   n["upi_ref"], n["achievement"], n["program"], n["sponsor"],
                   ("Yes" if n["sponsor_done"] else "No") if n["sponsor"] else "", "Yes" if n["is_late"] else "",
                   "Yes" if n["registered"] else "No", n["status"].title()])
    if can("acc_view"):
        wa = wb.create_sheet("Accounts")
        wa.append(["Date", "Type", "Category", "From / to", "Payment", "Amount (₹)", "Note"])
        f = fee_sums(oid)
        wa.append(["", "Income", "Registration fees (paid)", "", "", f["got"] / 100, f"{f['paid_n']} paid"])
        for e in H.query("SELECT * FROM fam_accounts WHERE org_id = ? ORDER BY entry_date, id", (oid,)):
            wa.append([e["entry_date"], e["kind"].title(), e["category"], e["party"], e["payment_mode"],
                       e["amount"] / 100 * (1 if e["kind"] == "income" else -1), e["note"]])
    for sheet in wb.worksheets:
        for c in sheet[1]:
            c.font = Font(bold=True)
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = min(40, max(10, max(len(str(c.value or "")) for c in col) + 2))
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name="family-registrations.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- settings + clear all

@bp.route("/family/settings", methods=["GET", "POST"])
@fam_view("settings")
def settings():
    oid = H.org_id()
    st = dict(get_settings(oid))
    has_tree = bool(H.scalar("SELECT COUNT(*) FROM fam_members WHERE org_id = ?", (oid,)))
    if request.method == "POST":
        f = request.form
        what = f.get("what")
        if what == "start":
            if has_tree:
                flash("The tree already exists. Edit the names in Members (or Clear all to start again).", "error")
                return redirect(url_for("fam.settings"))
            heads = []
            for name, rel in zip(f.getlist("head_name"), f.getlist("head_rel")):
                name = clean(name, 120)
                if name:
                    heads.append([name, rel if rel in ("son", "daughter") else "son"])
            start = {"h": clean(f.get("root_h"), 120), "h_late": 1 if f.get("root_h_late") else 0,
                     "w": clean(f.get("root_w"), 120), "w_late": 1 if f.get("root_w_late") else 0, "heads": heads}
            if not start["h"]:
                flash("Enter the name of the eldest member (root).", "error")
                return redirect(url_for("fam.settings"))
            st["start"] = start
            if not st["family_name"]:
                st["family_name"] = start["h"].title() + " Family"
            save_settings(st, oid)
            ensure_seed(oid)
            H.audit("family_start", start["h"])
            flash(f"Family tree started: {start['h']}" + (f" & {start['w']}" if start["w"] else "")
                  + f" with {len(heads)} children.", "success")
            return redirect(url_for("fam.home"))
        st.update(event_name=clean(f.get("event_name"), 120) or "Family Meetup",
                  family_name=clean(f.get("family_name"), 120), event_date=clean(f.get("event_date"), 60),
                  venue=clean(f.get("venue"), 200), about=(f.get("about") or "").strip()[:1000],
                  upi_id=clean(f.get("upi_id"), 80).replace(" ", ""), upi_name=clean(f.get("upi_name"), 80),
                  reg_open=bool(f.get("reg_open")), last_date=H.valid_date(f.get("last_date")) or "")
        for key in MSG_KEYS:
            text = (f.get(key) or "").replace("\r\n", "\n").strip()[:2000]
            st[key] = "" if text == DEFAULT_MESSAGES[key] else text
        fee = money_in(f.get("fee"))
        st["fee"] = fee if fee is not None and fee >= 0 else st["fee"]
        age = int_in(f.get("free_upto_age"), 0, 100)
        st["free_upto_age"] = age if age is not None else st["free_upto_age"]
        for key, box, fit, kb in (("cover", (1200, 630), False, 150), ("upi_qr", (700, 700), False, 120)):
            up = request.files.get(key)
            if up and up.filename:
                name = H.save_image(up, box=box, max_kb=kb, fit=fit)
                if name:
                    H.remove_media(st[key])
                    st[key] = name
                else:
                    flash("That file is not a photo, so it was skipped.", "warning")
            elif f.get("remove_" + key) and st[key]:
                H.remove_media(st[key])
                st[key] = None
        save_settings(st, oid)
        flash("Settings saved.", "success")
        return redirect(url_for("fam.settings"))
    start = st["start"] or {}
    heads = list(start.get("heads") or [])
    heads += [["", "son"]] * max(3, 12 - len(heads))
    msgs = {k: message_template(st, k) for k in MSG_KEYS}
    return render_template("family/settings.html", st=st, has_tree=has_tree, start=start, heads=heads, msgs=msgs,
                           PLACEHOLDERS=PLACEHOLDERS,
                           fee_rs=(st["fee"] or 0) / 100, form_link=form_url(), tree_link=tree_url())


@bp.route("/family/clear", methods=["POST"])
@fam_view(admin=True)
def clear_all():
    """Testing: delete every registration, member and account entry, then start again from the root couple."""
    if (request.form.get("confirm") or "").strip().upper() != "CLEAR":
        flash('Type CLEAR in the box to confirm.', "error")
        return redirect(url_for("fam.settings"))
    oid = H.org_id()
    db = H.get_db()
    photos = [r[0] for r in db.execute("SELECT photo FROM fam_members WHERE org_id = ? AND photo IS NOT NULL", (oid,))]
    photos += [r[0] for r in db.execute("SELECT photo FROM fam_accounts WHERE org_id = ? AND photo IS NOT NULL", (oid,))]
    n = db.execute("SELECT COUNT(*) FROM fam_members WHERE org_id = ? AND is_root = 0", (oid,)).fetchone()[0]
    db.execute("DELETE FROM fam_accounts WHERE org_id = ?", (oid,))
    db.execute("UPDATE fam_members SET parent_id = NULL, other_parent_id = NULL, spouse_of = NULL, claim_of = NULL"
               " WHERE org_id = ?", (oid,))
    db.execute("DELETE FROM fam_members WHERE org_id = ?", (oid,))
    db.commit()
    for p in photos:
        H.remove_media(p)
    ensure_seed(oid)
    H.audit("family_clear_all", f"{n} members removed")
    flash(f"🧹 Cleared: {n} members/registrations and all income & expense entries. "
          "The tree starts again from the root couple and the children in Settings.", "success")
    return redirect(url_for("fam.home"))
