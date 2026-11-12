"""📥 Lead Capture: ways to get leads into the CRM faster. The Super Admin switches each way on per company.

Staff ways:     Quick Add, Pick from Contacts, Share to CRM, Paste & Fill, Duplicate Alert
Customer ways:  Lead Form Link & QR (public form per campaign, one link per source), Catalogue Enquiry Form
Connections:    Google Forms Connect (a small script in the Google Form posts each answer here)
Coming later:   Meta Lead Ads, IndiaMART Connect

Leads that customers send themselves are entered by the company's admin, assigned as the form says, and never
duplicated: the same phone number adds a "enquired again" line to the existing lead.
"""
import json
import re
import secrets
import time
from datetime import datetime

from flask import Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

H = None
FEATURES = [
    ("quick_add", "Quick Add", "A ＋ Lead button on every CRM page: name and phone, saved in seconds."),
    ("contacts", "Pick from Contacts", "Fill name and phone from the phone's contacts (Android Chrome)."),
    ("share", "Share to CRM", "Share a WhatsApp message to the installed app: a new lead opens, already filled."),
    ("paste", "Paste & Fill", "Paste a message or visiting-card text: name, phone, email and place fill in."),
    ("dup_alert", "Duplicate Alert", "Warns while the phone number is typed if the customer is already in the CRM."),
    ("form_link", "Lead Form Link & QR", "Public form links for each campaign and source, with QR codes and counts."),
    ("catalog_enquiry", "Catalogue Enquiry Form", "An enquiry form on each product page of the digital catalogue."),
    ("gforms", "Google Forms Connect", "Answers to your Google Form arrive here as leads."),
    ("meta_ads", "Meta Lead Ads", "Facebook / Instagram lead forms straight into the CRM."),
    ("indiamart", "IndiaMART Connect", "IndiaMART enquiries straight into the CRM."),
]
SOON = {"meta_ads", "indiamart"}
LABEL = {k: v for k, v, _ in FEATURES}
FORM_FIELDS = [("place", "Place"), ("email", "Email"), ("product", "Product interested in"),
               ("message", "Message")]
SOURCE_HINTS = {"facebook": "Facebook", "fb": "Facebook", "instagram": "Instagram", "ig": "Instagram",
                "google": "Google Ads", "youtube": "YouTube", "yt": "YouTube", "whatsapp": "WhatsApp",
                "wa": "WhatsApp", "website": "Website", "web": "Website", "qr": "Walk-in", "poster": "Walk-in"}
LINK_SOURCES = [("facebook", "Facebook ad"), ("instagram", "Instagram"), ("whatsapp", "WhatsApp status"),
                ("google", "Google ad"), ("youtube", "YouTube"), ("qr", "QR / poster")]
_hits = {}


def init(host):
    global H
    H = host
    app = host.app
    r = app.add_url_rule
    r("/admin/lead-capture", "admin_capture", host.super_required(admin_page), methods=["GET", "POST"])
    r("/lead-capture", "lead_capture", host.admin_required(page), methods=["GET", "POST"])
    r("/lead-capture/forms/new", "capture_form_new", host.admin_required(form_edit), methods=["GET", "POST"])
    r("/lead-capture/forms/<int:fid>", "capture_form_edit", host.admin_required(form_edit), methods=["GET", "POST"])
    r("/lead-capture/forms/<int:fid>/qr.svg", "capture_form_qr", host.admin_required(form_qr))
    r("/leads/quick", "lead_quick", host.login_required(quick_add), methods=["POST"])
    r("/leads/dup-check", "lead_dup_check", host.login_required(dup_check))
    r("/share-lead", "share_lead", host.login_required(share_lead))
    r("/lf/<code>/<slug>", "public_form", public_form, methods=["GET", "POST"])
    r("/c/<code>/enquire", "catalog_enquire", catalog_enquire, methods=["POST"])
    r("/hook/gform/<token>", "gform_hook", gform_hook, methods=["POST"])
    app.jinja_env.globals["capture_on"] = capture_on
    app.jinja_env.globals["CAPTURE_LABEL"] = LABEL
    app.jinja_env.globals["quick_add_data"] = quick_add_data


def quick_add_data():
    """Campaigns and products for the Quick Add window."""
    return {"projects": H.visible_projects(active_only=True),
            "products": H.query("SELECT id, name FROM products WHERE org_id = ? AND active = 1 "
                                "ORDER BY name COLLATE NOCASE", (g.org["id"],))}


# ---------------------------------------------------------------- switches

def enabled(oid):
    return {r["feature"] for r in H.query("SELECT feature FROM org_capture WHERE org_id = ?", (oid,))}


def capture_on(feature=None, org=None):
    """Is this way switched on for the company (any way, when feature is None)?"""
    org = org if org is not None else g.get("org")
    if org is None:
        return False
    cache = g.setdefault("_capture", {})
    if org["id"] not in cache:
        try:
            cache[org["id"]] = enabled(org["id"])
        except Exception:
            cache[org["id"]] = set()
    on = cache[org["id"]]
    return bool(on - SOON) if feature is None else feature in on


def _need(feature):
    if not capture_on(feature):
        abort(404)


def admin_page():
    orgs = H.query("SELECT id, name, code, kind FROM organizations ORDER BY kind = 'agency' DESC, name COLLATE NOCASE")
    orgs = [o for o in orgs if "crm" in H.org_modules(o)]
    if request.method == "POST":
        db = H.get_db()
        ts = H.now()
        for o in orgs:
            before = enabled(o["id"])
            after = {k for k, _, _ in FEATURES if k not in SOON and request.form.get(f"f_{o['id']}_{k}")}
            for k in before - after:
                db.execute("DELETE FROM org_capture WHERE org_id = ? AND feature = ?", (o["id"], k))
            for k in after - before:
                db.execute("INSERT OR IGNORE INTO org_capture (org_id, feature, enabled_at) VALUES (?,?,?)",
                           (o["id"], k, ts))
            new = [LABEL[k] for k, _, _ in FEATURES if k in after - before]
            if new:
                db.execute("""INSERT INTO app_updates (org_id, module, audience, title, body, created_by, created_at)
                              VALUES (?, 'crm', 'all', ?, ?, ?, ?)""",
                           (o["id"], "📥 Lead Capture: " + ", ".join(new),
                            "Now on for your company: " + "; ".join(
                                f"{LABEL[k]} — {d}" for k, _, d in FEATURES if k in after - before)
                            + " Admins: see 📥 Lead Capture in the menu.", g.user["id"], ts))
                db.execute("UPDATE feature_requests SET status = 'done', handled_at = ? WHERE org_id = ? "
                           "AND feature = 'capture' AND status = 'open'", (ts, o["id"]))
            if after and not H.query("SELECT capture_token FROM organizations WHERE id = ?", (o["id"],),
                                     one=True)["capture_token"]:
                db.execute("UPDATE organizations SET capture_token = ? WHERE id = ?", (secrets.token_hex(16), o["id"]))
        db.commit()
        H.audit("Lead Capture switches saved", f"{len(orgs)} companies")
        flash("Saved. Companies are told about what was switched on.", "success")
        return redirect(url_for("admin_capture"))
    on = {o["id"]: enabled(o["id"]) for o in orgs}
    reqs = {r["org_id"] for r in H.query("SELECT org_id FROM feature_requests WHERE feature = 'capture' "
                                         "AND status = 'open'")}
    return render_template("admin_capture.html", orgs=orgs, on=on, FEATURES=FEATURES, SOON=SOON, reqs=reqs)


# ---------------------------------------------------------------- creating leads

def _admin_of(oid):
    row = H.query("SELECT id FROM users WHERE org_id = ? AND active = 1 AND role IN ('org_admin', 'super_admin') "
                  "ORDER BY role = 'super_admin' DESC, id LIMIT 1", (oid,), one=True)
    return row["id"] if row else None


def _src(oid, hint, default=""):
    opts = H.opt_all("source", oid)
    want = SOURCE_HINTS.get((hint or "").lower().strip())
    if want and want in opts:
        return want
    return default if default in opts else ""


def public_lead(org, *, project_id, name, phone, email="", place="", message="", product_id=None, source="",
                assign_to=None, how="the lead form"):
    """Save a lead that a customer sent. The same phone number again: note it on the existing lead instead.
    Returns (lead_id, created)."""
    db = H.get_db()
    oid, ts = org["id"], H.now()
    key = H.phone_key(phone)
    owner = _admin_of(oid)
    if owner is None:
        raise ValueError("no admin")
    old = db.execute("SELECT id FROM leads WHERE org_id = ? AND phone_norm = ? "
                     "ORDER BY id DESC LIMIT 1", (oid, key)).fetchone() if len(key) >= 6 else None
    extra = " · ".join(x for x in (message and f"Message: {message}", email and f"Email: {email}",
                                    place and f"Place: {place}") if x)
    if old:
        lid = old[0]
        db.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (ts, lid))
        db.execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at) VALUES (?,?,?,?)",
                   (lid, owner, f"Enquired again via {how}" + (f" — {extra}" if extra else ""), ts))
        created = False
    else:
        lid = db.execute(
            """INSERT INTO leads (org_id, project_id, customer_name, phone, whatsapp, phone_norm, email, place, notes,
                                  status, priority, source, country, entered_by, assigned_to, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'India', ?,?,?,?)""",
            (oid, project_id, name, phone, phone, key, email, place, message, H.default_status(oid),
             H.default_priority(oid), source, owner, assign_to or owner, ts, ts)).lastrowid
        db.execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at) VALUES (?,?,?,?)",
                   (lid, owner, f"Lead received via {how}", ts))
        created = True
    if product_id and db.execute("SELECT 1 FROM products WHERE id = ? AND org_id = ?", (product_id, oid)).fetchone() \
            and not db.execute("SELECT 1 FROM lead_products WHERE lead_id = ? AND product_id = ?",
                               (lid, product_id)).fetchone():
        db.execute("INSERT INTO lead_products (lead_id, product_id, org_id, stage, created_at, updated_at)"
                   " VALUES (?,?,?, 'Interested', ?, ?)", (lid, product_id, oid, ts, ts))
    db.commit()
    return lid, created


def _limited(kind, limit=8, window=3600):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    now = time.time()
    hits = [t for t in _hits.get((kind, ip), []) if now - t < window]
    if len(hits) >= limit:
        return True
    _hits[(kind, ip)] = hits + [now]
    if len(_hits) > 20000:
        _hits.clear()
    return False


def _phone_ok(phone):
    digits = re.sub(r"\D", "", phone or "")
    return 10 <= len(digits) <= 13


def _org_by_code(code):
    return H.query("SELECT * FROM organizations WHERE code = ? COLLATE NOCASE AND status = 'Active'",
                   (code,), one=True)


# ---------------------------------------------------------------- public lead form

def _form(oid, slug):
    return H.query("SELECT * FROM lead_forms WHERE org_id = ? AND slug = ?", (oid, slug), one=True)


def _stat(form_id, src, col):
    H.execute(f"""INSERT INTO lead_form_stats (form_id, src, {col}) VALUES (?, ?, 1)
                  ON CONFLICT(form_id, src) DO UPDATE SET {col} = {col} + 1""", (form_id, src))


def public_form(code, slug):
    org = _org_by_code(code)
    if not org or not capture_on("form_link", org):
        abort(404)
    form = _form(org["id"], slug)
    if not form or not form["active"]:
        abort(404)
    src = re.sub(r"[^a-z0-9_-]", "", (request.values.get("s") or "").lower())[:20]
    fields = set((form["fields"] or "").split(","))
    products = H.query("SELECT id, name FROM products WHERE org_id = ? AND active = 1 ORDER BY name COLLATE NOCASE",
                       (org["id"],)) if "product" in fields else []
    wa = H.wa_number(org["phone"]) if org["phone"] else ""
    if request.method == "POST":
        f = request.form
        if f.get("website"):                                   # honeypot
            return redirect(url_for("public_form", code=code, slug=slug, done=1))
        if _limited("form", 8):
            abort(429)
        name, phone = H.clean(f.get("name"))[:120], H.clean(f.get("phone"))[:20]
        if not name or not _phone_ok(phone):
            return render_template("public_form.html", org=org, form=form, fields=fields, products=products,
                                   src=src, wa=wa, error="Please enter your name and a valid phone number.",
                                   values=f)
        assign = None
        if form["assign_mode"] == "user" and form["assign_user"]:
            assign = form["assign_user"]
        elif form["assign_mode"] == "rotate":
            staff = [r["id"] for r in H.query("SELECT id FROM users WHERE org_id = ? AND active = 1 AND role = 'staff' "
                                                "ORDER BY id", (org["id"],))]
            if staff:
                assign = staff[form["rotate_next"] % len(staff)]
        pid = f.get("product_id", type=int) if "product" in fields else None
        try:
            _, created = public_lead(org, project_id=form["project_id"], name=name, phone=phone,
                        email=H.clean(f.get("email"))[:120] if "email" in fields else "",
                        place=H.clean(f.get("place"))[:120] if "place" in fields else "",
                        message=H.clean(f.get("message"))[:1000] if "message" in fields else "",
                        product_id=pid, source=_src(org["id"], src, form["source"] or ""), assign_to=assign,
                        how=f"the form “{form['title']}”" + (f" ({src})" if src else ""))
        except ValueError:
            abort(503)
        if created and form["assign_mode"] == "rotate":       # the next new customer goes to the next person
            H.execute("UPDATE lead_forms SET rotate_next = rotate_next + 1 WHERE id = ?", (form["id"],))
        _stat(form["id"], src, "submits")
        return redirect(url_for("public_form", code=code, slug=slug, done=1, s=src or None))
    if not request.args.get("done"):
        _stat(form["id"], src, "views")
    return render_template("public_form.html", org=org, form=form, fields=fields, products=products,
                           src=src, wa=wa, done=bool(request.args.get("done")), values={})


def catalog_enquire(code):
    org = _org_by_code(code)
    if not org or not capture_on("catalog_enquiry", org):
        abort(404)
    f = request.form
    pid = f.get("pid", type=int)
    product = H.query("SELECT * FROM products WHERE id = ? AND org_id = ? AND active = 1", (pid, org["id"]), one=True)
    if not product:
        abort(404)
    back = url_for("catalog_product", code=code.lower(), pid=pid, l=f.get("l") or None)
    if f.get("website"):
        return redirect(back + ("&" if "?" in back else "?") + "sent=1")
    if _limited("cat", 6):
        abort(429)
    name, phone = H.clean(f.get("name"))[:120], H.clean(f.get("phone"))[:20]
    if not name or not _phone_ok(phone):
        return redirect(back + ("&" if "?" in back else "?") + "err=1#enquire")
    project = _project_for(org["id"], "Catalogue Enquiries", "CAT")
    try:
        public_lead(org, project_id=project, name=name, phone=phone, place=H.clean(f.get("place"))[:120],
                    message=H.clean(f.get("message"))[:1000], product_id=pid,
                    source=_src(org["id"], "website", "Website"), how=f"the catalogue ({product['name']})")
    except ValueError:
        abort(503)
    return redirect(back + ("&" if "?" in back else "?") + "sent=1#enquire")


def _project_for(oid, name, code):
    row = H.query("SELECT id FROM projects WHERE org_id = ? AND code = ?", (oid, code), one=True)
    if row:
        return row["id"]
    ts = H.now()
    pid = H.execute("""INSERT INTO projects (org_id, name, code, campaign_type, description, status, created_by,
                       created_at, updated_at) VALUES (?, ?, ?, 'Lead Generation', ?, 'Active', ?, ?, ?)""",
                    (oid, name, code, f"Leads from {name.lower()}", _admin_of(oid), ts, ts))
    return pid


# ---------------------------------------------------------------- Google Forms Connect

GFORM_SCRIPT = """// Real View CRM — Google Forms Connect
// In the Google Form: ⋮ → Script editor (Apps Script) → paste this → Save →
// Triggers (clock icon) → Add trigger → onSubmit · From form · On form submit → Save → allow access.
function onSubmit(e) {
  var answers = {};
  e.response.getItemResponses().forEach(function (r) {
    answers[r.getItem().getTitle()] = String(r.getResponse());
  });
  UrlFetchApp.fetch("%s", {
    method: "post", contentType: "application/json", muteHttpExceptions: true,
    payload: JSON.stringify({form: e.source.getTitle(), answers: answers})
  });
}"""


def _pick(answers, *words):
    for q, a in answers.items():
        ql = q.lower()
        if any(w in ql for w in words) and a:
            return str(a).strip()
    return ""


def gform_hook(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        abort(404)
    org = H.query("SELECT * FROM organizations WHERE capture_token = ?", (token,), one=True)
    if not org or not capture_on("gforms", org):
        abort(404)
    if _limited("gform", 300):
        abort(429)
    data = request.get_json(silent=True) or {}
    answers = {str(k)[:200]: str(v)[:1000] for k, v in (data.get("answers") or {}).items()}
    name = _pick(answers, "name", "പേര്")[:120]
    phone = _pick(answers, "phone", "mobile", "whatsapp", "contact", "number", "ഫോൺ")[:20]
    if not name or not _phone_ok(phone):
        return jsonify(ok=False, error="name and phone needed"), 400
    used = {k for k in answers if any(w in k.lower() for w in ("name", "phone", "mobile", "whatsapp", "contact",
                                                               "number", "email", "place", "city", "district",
                                                               "location", "പേര്", "ഫോൺ"))}
    rest = " · ".join(f"{k}: {v}" for k, v in answers.items() if k not in used and v)
    title = H.clean(data.get("form"))[:80] or "Google Form"
    project = _project_for(org["id"], "Google Forms", "GFORM")
    lid, created = public_lead(org, project_id=project, name=name, phone=phone,
                               email=_pick(answers, "email", "e-mail")[:120],
                               place=_pick(answers, "place", "city", "district", "location")[:120], message=rest[:1000],
                               source=_src(org["id"], "", "Other"), how=f"Google Form “{title}”")
    return jsonify(ok=True, created=created)


# ---------------------------------------------------------------- the company's Lead Capture page

def page():
    if not capture_on():
        abort(404)
    org = g.org
    if request.method == "POST" and not g.get("viewing"):
        act = request.form.get("act")
        if act == "pixel":
            px = re.sub(r"\D", "", request.form.get("meta_pixel") or "")[:20]
            H.execute("UPDATE organizations SET meta_pixel = ? WHERE id = ?", (px or None, org["id"]))
            flash("Meta Pixel saved." if px else "Meta Pixel removed.", "success")
        elif act == "gform_token" and capture_on("gforms"):
            H.execute("UPDATE organizations SET capture_token = ? WHERE id = ?", (secrets.token_hex(16), org["id"]))
            H.audit("Google Forms Connect address changed", "")
            flash("New address made. Paste the new script in your Google Form; the old one stops working.", "warning")
        return redirect(url_for("lead_capture"))
    forms = H.query("""SELECT f.*, p.name project_name,
                         (SELECT COALESCE(SUM(views), 0) FROM lead_form_stats s WHERE s.form_id = f.id) views,
                         (SELECT COALESCE(SUM(submits), 0) FROM lead_form_stats s WHERE s.form_id = f.id) submits
                       FROM lead_forms f LEFT JOIN projects p ON p.id = f.project_id
                       WHERE f.org_id = ? ORDER BY f.active DESC, f.id DESC""", (org["id"],))
    stats = {}
    for r in H.query("SELECT s.* FROM lead_form_stats s JOIN lead_forms f ON f.id = s.form_id WHERE f.org_id = ?",
                     (org["id"],)):
        stats.setdefault(r["form_id"], []).append(r)
    org_row = H.query("SELECT meta_pixel, capture_token, catalog_on, catalog_allowed FROM organizations WHERE id = ?",
                      (org["id"],), one=True)
    hook = url_for("gform_hook", token=org_row["capture_token"], _external=True) if org_row["capture_token"] else ""
    return render_template("lead_capture.html", forms=forms, stats=stats, FEATURES=FEATURES, SOON=SOON,
                           org_row=org_row, gscript=GFORM_SCRIPT % hook if hook else "", LINK_SOURCES=LINK_SOURCES,
                           on=enabled(org["id"]))


def form_edit(fid=None):
    _need("form_link")
    oid = g.org["id"]
    form = H.query("SELECT * FROM lead_forms WHERE id = ? AND org_id = ?", (fid, oid), one=True) if fid else None
    if fid and not form:
        abort(404)
    projects = H.visible_projects(active_only=True, include_id=form["project_id"] if form else None)
    staff = H.query("SELECT id, name FROM users WHERE org_id = ? AND active = 1 ORDER BY name COLLATE NOCASE", (oid,))
    if request.method == "POST" and not g.get("viewing"):
        f = request.form
        title = H.clean(f.get("title"))[:80]
        slug = re.sub(r"[^a-z0-9-]+", "-", (f.get("slug") or title).lower()).strip("-")[:40] or "enquiry"
        pid = f.get("project_id", type=int)
        fields = ",".join(k for k, _ in FORM_FIELDS if f.get(f"fld_{k}"))
        mode = f.get("assign_mode") if f.get("assign_mode") in ("admin", "user", "rotate") else "admin"
        auser = f.get("assign_user", type=int)
        err = None
        if not title:
            err = "Give the form a title."
        elif pid not in {p["id"] for p in projects}:
            err = "Choose a campaign."
        elif H.query("SELECT 1 FROM lead_forms WHERE org_id = ? AND slug = ? AND id != ?", (oid, slug, fid or 0),
                     one=True):
            err = f"The link name “{slug}” is used by another form."
        elif mode == "user" and auser not in {s["id"] for s in staff}:
            err = "Choose the staff member who gets these leads."
        if err:
            flash(err, "error")
        else:
            vals = (slug, title, H.clean(f.get("intro"))[:300], pid, f.get("source") if f.get("source") in
                    H.opt_all("source", oid) else "", fields, mode, auser if mode == "user" else None,
                    H.clean(f.get("thank_text"))[:300], 1 if f.get("active") else 0)
            if form:
                H.execute("""UPDATE lead_forms SET slug=?, title=?, intro=?, project_id=?, source=?, fields=?, assign_mode=?,
                             assign_user=?, thank_text=?, active=? WHERE id = ? AND org_id = ?""", vals + (fid, oid))
            else:
                fid = H.execute("""INSERT INTO lead_forms (slug, title, intro, project_id, source, fields, assign_mode,
                                   assign_user, thank_text, active, org_id, created_by, created_at)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals + (oid, g.user["id"], H.now()))
            H.audit("Lead form saved", title)
            flash("Form saved. Copy its links below.", "success")
            return redirect(url_for("lead_capture") + f"#form{fid}")
    return render_template("lead_form_edit.html", form=form, projects=projects, staff=staff, FORM_FIELDS=FORM_FIELDS,
                           sources=H.opt_all("source", oid))


def form_qr(fid):
    _need("form_link")
    form = H.query("SELECT * FROM lead_forms WHERE id = ? AND org_id = ?", (fid, g.org["id"]), one=True) or abort(404)
    src = re.sub(r"[^a-z0-9_-]", "", (request.args.get("s") or "qr").lower())[:20]
    import segno
    url = url_for("public_form", code=g.org["code"].lower(), slug=form["slug"], s=src, _external=True)
    import io
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=10, border=2)
    return Response(buf.getvalue(), mimetype="image/svg+xml",
                    headers={"Content-Disposition": f'attachment; filename="{form["slug"]}-{src}-qr.svg"'})


# ---------------------------------------------------------------- staff tools

def quick_add():
    _need("quick_add")
    if g.get("viewing"):
        abort(403)
    f = request.form
    data = {k: "" for k in H.LEAD_TEXT_FIELDS}
    data.update(status=H.default_status(), priority=H.default_priority(), country=H.DEFAULT_COUNTRY, product_ids=[],
                assigned_to=g.user["id"], next_follow_up="", referral_code="")
    data["customer_name"] = H.clean(f.get("customer_name"))[:120]
    data["phone"] = data["whatsapp"] = H.clean(f.get("phone"))[:40]
    data["email"] = H.clean(f.get("email"))[:120]
    data["place"] = H.clean(f.get("place"))[:120]
    data["notes"] = H.clean(f.get("notes"))[:2000]
    data["project_id"] = f.get("project_id", type=int)
    pid = f.get("product_id", type=int)
    if pid and H.query("SELECT 1 FROM products WHERE id = ? AND org_id = ?", (pid, g.org["id"]), one=True):
        data["product_ids"] = [pid]
    errors = H.validate_lead(data)
    if errors:
        return jsonify(ok=False, error=" ".join(errors)), 400
    dups, hidden = H.find_duplicates(data)
    if (dups or hidden) and not f.get("confirm_duplicate"):
        return jsonify(ok=False, duplicate=True, matches=[_dup_row(d) for d in dups], hidden=hidden)
    db = H.get_db()
    ts = H.now()
    cols = H.LEAD_TEXT_FIELDS + ["project_id", "assigned_to"]
    lid = db.execute(f"INSERT INTO leads (org_id, entered_by, phone_norm, created_at, updated_at, {', '.join(cols)})"
                     f" VALUES (?,?,?,?,?, {', '.join('?' * len(cols))})",
                     (g.org["id"], g.user["id"], H.phone_key(data["phone"]), ts, ts, *[data[k] for k in cols])).lastrowid
    H.sync_products(db, lid, data["product_ids"], ts)
    H.log_activity(lid, "Lead created (Quick Add)", commit=False)
    db.commit()
    return jsonify(ok=True, id=lid, url=url_for("lead_view", lid=lid), name=data["customer_name"])


def _dup_row(d):
    return {"id": d["id"], "name": d["customer_name"], "url": url_for("lead_view", lid=d["id"]),
            "status": d["status"] or "", "assigned": d["assigned_name"] if "assigned_name" in d.keys() else "",
            "when": H.ist_time(d["created_at"], "%d %b %Y")}


def dup_check():
    _need("dup_alert")
    phone = H.clean(request.args.get("phone"))
    if len(H.phone_key(phone)) < 6:
        return jsonify(matches=[], hidden=0)
    data = {"phone": phone, "whatsapp": "", "email": ""}
    dups, hidden = H.find_duplicates(data, exclude_id=request.args.get("exclude", type=int))
    return jsonify(matches=[_dup_row(d) for d in dups][:5], hidden=hidden)


def share_lead():
    _need("share")
    text = "\n".join(x for x in (request.args.get("title"), request.args.get("text"), request.args.get("url")) if x)
    return render_template("share_lead.html", shared=text[:3000])
