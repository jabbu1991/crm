"""Real View CRM (by Real View Creative Studio) - multi-tenant lead management for a marketing agency and its client companies.

Run:  python app.py   (then open http://localhost:5000)

Security model
--------------
* Every tenant-owned row (projects, leads, products, follow-ups, users) carries org_id.
* The current organization and user always come from the server-side session - never from a
  form field, URL or query parameter.
* All lead access goes through lead_scope(): admins see their organization's leads, staff see
  only leads they created or that are assigned to them. Records outside the scope return 404.
* SQLite triggers (crm_db.TENANT_TRIGGERS) reject any write that would link rows across
  organizations, as a second line of defence.
"""
import csv
import hashlib
import io
import os
import secrets
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import crm_db
from crm_constants import (BUYING_POSSIBILITIES, CAMPAIGN_TYPES, CLOSED_LOST_STATUSES, COUNTRIES,
                           DEFAULT_COUNTRY, INDIAN_STATES, LEAD_QUALITIES, LEAD_SOURCES, LEAD_STATUSES,
                           ORG_ADMIN, PRIORITIES, PRODUCT_STAGES, PROJECT_STATUSES, ROLE_LABELS, STAFF,
                           SUPER_ADMIN, normalize_location, phone_key)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.environ.get("CRM_DB_PATH", os.path.join(INSTANCE_DIR, "crm.db"))
PER_PAGE = 50
MIN_PASSWORD = 8
INVITE_DAYS, RESET_HOURS = 7, 24

now = crm_db.now


def clean(value):
    return (value or "").strip()


def load_secret_key():
    if os.environ.get("CRM_SECRET_KEY"):
        return os.environ["CRM_SECRET_KEY"]
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("CRM_SECURE_COOKIES") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
if os.environ.get("CRM_BEHIND_PROXY") == "1":
    # Hosted behind an HTTPS proxy (e.g. Render): trust its X-Forwarded-* headers so
    # generated links (invites, password resets) use https and the public hostname.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
SETUP_KEY = os.environ.get("CRM_SETUP_KEY", "")
# Product branding (can be overridden per deployment).
BRAND = {
    "product": os.environ.get("CRM_PRODUCT_NAME", "Real View CRM"),
    "company": os.environ.get("CRM_COMPANY_NAME", "Real View Creative Studio"),
    "website": os.environ.get("CRM_COMPANY_URL", "https://realviewcreativestudio.in"),
}
# Website origins allowed to post enquiries to /enquiry (comma-separated). Empty = disabled.
ENQUIRY_ORIGINS = {o.strip().rstrip("/") for o in os.environ.get("CRM_ENQUIRY_ORIGINS", "").split(",") if o.strip()}


# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        g.db = crm_db.connect(DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    rows = get_db().execute(sql, tuple(args)).fetchall()
    return (rows[0] if rows else None) if one else rows


def scalar(sql, args=()):
    row = get_db().execute(sql, tuple(args)).fetchone()
    return row[0] if row else None


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, tuple(args))
    db.commit()
    return cur.lastrowid


def log_activity(lead_id, action, commit=True):
    get_db().execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at) VALUES (?,?,?,?)",
                     (lead_id, g.user["id"], action, now()))
    if commit:
        get_db().commit()


def today():
    return date.today().isoformat()


def valid_date(value):
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def int_list(values):
    out = []
    for v in values:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            pass
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- roles & session

def is_super():
    return bool(g.get("user")) and g.user["role"] == SUPER_ADMIN


def is_admin():
    return bool(g.get("user")) and g.user["role"] in (SUPER_ADMIN, ORG_ADMIN)


def org_id():
    return g.org["id"]


@app.before_request
def load_user():
    g.user = g.org = None
    uid = session.get("user_id")
    if uid:
        user = query("SELECT * FROM users WHERE id = ?", (uid,), one=True)
        org = query("SELECT * FROM organizations WHERE id = ?", (user["org_id"],), one=True) if user else None
        if (user is None or not user["active"] or org is None
                or session.get("sv") != user["session_version"]
                or (org["status"] != "Active" and user["role"] != SUPER_ADMIN)):
            session.clear()
        else:
            g.user, g.org = user, org
            # Super Admin viewing a client's workspace (read-only).
            view_id = session.get("view_org")
            if view_id and user["role"] == SUPER_ADMIN and view_id != org["id"]:
                viewed = query("SELECT * FROM organizations WHERE id = ?", (view_id,), one=True)
                if viewed:
                    g.home_org, g.org, g.viewing = org, viewed, True
                else:
                    session.pop("view_org", None)

    if g.get("viewing") and request.endpoint not in VIEW_ALLOWED_ENDPOINTS and (
            request.method == "POST" or request.endpoint in WRITE_FORM_ENDPOINTS):
        abort(403, "Read-only: you are viewing a client's workspace. Exit to make changes.")

    if request.method == "POST" and request.endpoint != "website_enquiry":
        token = session.get("csrf_token")
        if not token or not secrets.compare_digest(token, request.form.get("csrf_token", "")):
            abort(400, "Invalid form token. Please reload the page and try again.")


# While a Super Admin views a client workspace, these are the only POSTs allowed,
# and these form pages are hidden (their submit would be refused anyway).
VIEW_ALLOWED_ENDPOINTS = {"logout", "admin_view_exit", "admin_view_enter", "static"}
WRITE_FORM_ENDPOINTS = {"lead_new", "lead_edit", "leads_import", "project_new", "project_edit", "product_new",
                        "product_edit", "staff_new", "staff_edit", "account"}


def audit(action, detail="", org=None):
    execute("INSERT INTO audit_log (org_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
            (org or org_id(), g.user["id"], action, detail, now()))


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if g.get("user"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def start_session(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["sv"] = user["session_version"]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if scalar("SELECT COUNT(*) FROM users") == 0:
                return redirect(url_for("setup"))
            return redirect(url_for("login", next=request.full_path))
        if g.user["must_change_password"] and request.endpoint not in ("account", "logout"):
            flash("Please set a new password before continuing.", "warning")
            return redirect(url_for("account"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def super_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_super():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    return dict(
        csrf_token=csrf_token, brand=BRAND, is_admin=is_admin, viewing=bool(g.get('viewing')), is_super=is_super, ROLE_LABELS=ROLE_LABELS,
        LEAD_STATUSES=LEAD_STATUSES, BUYING_POSSIBILITIES=BUYING_POSSIBILITIES, LEAD_QUALITIES=LEAD_QUALITIES,
        LEAD_SOURCES=LEAD_SOURCES, PRIORITIES=PRIORITIES, PRODUCT_STAGES=PRODUCT_STAGES,
        PROJECT_STATUSES=PROJECT_STATUSES, CAMPAIGN_TYPES=CAMPAIGN_TYPES, INDIAN_STATES=INDIAN_STATES,
        COUNTRIES=COUNTRIES, GROUPABLE=GROUPABLE, today=today(),
    )


@app.template_filter("badge")
def badge_class(value):
    return "b-" + (value or "none").lower().replace(" ", "-")


# ---------------------------------------------------------------- password tokens

def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_password_link(user_id, purpose):
    """Create a single-use invite/reset link. Only the hash is stored; older unused links are revoked."""
    raw = secrets.token_urlsafe(32)
    expires = datetime.now() + (timedelta(days=INVITE_DAYS) if purpose == "invite" else timedelta(hours=RESET_HOURS))
    db = get_db()
    db.execute("UPDATE password_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now(), user_id))
    db.execute("INSERT INTO password_tokens (user_id, token_hash, purpose, expires_at, created_by, created_at)"
               " VALUES (?,?,?,?,?,?)", (user_id, _hash_token(raw), purpose,
                                         expires.strftime("%Y-%m-%d %H:%M:%S"), g.user["id"], now()))
    db.commit()
    return url_for("reset_password", token=raw, _external=True), expires.strftime("%Y-%m-%d %H:%M")


def set_password(user_id, password, must_change):
    """Set a password hash and end every existing session for that user."""
    execute("UPDATE users SET password_hash = ?, must_change_password = ?, session_version = session_version + 1"
            " WHERE id = ?", (generate_password_hash(password), 1 if must_change else 0, user_id))


def password_problem(password):
    if len(password or "") < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    return None


# ---------------------------------------------------------------- setup / login

@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First run: create the agency organization and the Super Admin account."""
    if scalar("SELECT COUNT(*) FROM users") > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        agency, name, username = (clean(request.form.get(k)) for k in ("agency", "name", "username"))
        password = request.form.get("password") or ""
        problem = password_problem(password)
        if SETUP_KEY and not secrets.compare_digest(SETUP_KEY, request.form.get("setup_key") or ""):
            flash("Setup key is incorrect. Find it in your hosting dashboard (CRM_SETUP_KEY).", "error")
        elif not agency or not name or not username:
            flash("Agency name, your name and a username are required.", "error")
        elif problem:
            flash(problem, "error")
        else:
            db = get_db()
            ts = now()
            oid = db.execute("INSERT INTO organizations (name, code, kind, status, created_at)"
                             " VALUES (?, 'AGENCY', 'agency', 'Active', ?)", (agency, ts)).lastrowid
            uid = db.execute(
                "INSERT INTO users (org_id, name, username, phone, email, designation, password_hash, role, created_at)"
                " VALUES (?,?,?,?,?, 'Owner', ?, ?, ?)",
                (oid, name, username, clean(request.form.get("phone")), clean(request.form.get("email")),
                 generate_password_hash(password), SUPER_ADMIN, ts)).lastrowid
            db.execute("UPDATE organizations SET created_by = ? WHERE id = ?", (uid, oid))
            db.commit()
            start_session(query("SELECT * FROM users WHERE id = ?", (uid,), one=True))
            flash("Super Admin account created. Add your agency's projects, or create client organizations.", "success")
            return redirect(url_for("dashboard"))
    return render_template("setup.html", need_key=bool(SETUP_KEY))


_failed_logins = {}


def _throttled(key):
    rec = _failed_logins.get(key)
    if rec and time.time() - rec[1] > 900:
        _failed_logins.pop(key, None)
        return False
    return bool(rec and rec[0] >= 5)


@app.route("/login", methods=["GET", "POST"])
def login():
    if scalar("SELECT COUNT(*) FROM users") == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        ident = clean(request.form.get("username"))
        key = (ident.lower(), request.remote_addr)
        if _throttled(key):
            flash("Too many failed attempts. Please wait 15 minutes and try again.", "error")
            return render_template("login.html"), 429
        user = query("SELECT * FROM users WHERE username = ?", (ident,), one=True)
        if user is None and "@" in ident:
            matches = query("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (ident,))
            user = matches[0] if len(matches) == 1 else None
        org = query("SELECT * FROM organizations WHERE id = ?", (user["org_id"],), one=True) if user else None
        ok = (user is not None and check_password_hash(user["password_hash"], request.form.get("password") or ""))
        if ok and user["active"] and org and (org["status"] == "Active" or user["role"] == SUPER_ADMIN):
            _failed_logins.pop(key, None)
            execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), user["id"]))
            start_session(user)
            nxt = request.args.get("next") or ""
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("dashboard")
            return redirect(nxt)
        rec = _failed_logins.setdefault(key, [0, time.time()])
        rec[0] += 1
        if ok and not user["active"]:
            flash("This account has been deactivated. Contact your administrator.", "error")
        elif ok:
            flash("Your organization's account is inactive. Contact the agency.", "error")
        else:
            flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    rec = query("""SELECT t.*, u.name, u.username, u.active, o.status org_status, u.role
                   FROM password_tokens t JOIN users u ON u.id = t.user_id
                   JOIN organizations o ON o.id = u.org_id WHERE t.token_hash = ?""",
                (_hash_token(token),), one=True)
    valid = (rec is not None and rec["used_at"] is None and rec["expires_at"] > now() and rec["active"]
             and (rec["org_status"] == "Active" or rec["role"] == SUPER_ADMIN))
    if not valid:
        return render_template("error.html", code=410,
                               message="This link is invalid, already used or expired. Ask your administrator for a new one."), 410
    if request.method == "POST":
        password = request.form.get("password") or ""
        problem = password_problem(password)
        if problem:
            flash(problem, "error")
        elif password != request.form.get("confirm"):
            flash("Passwords do not match.", "error")
        else:
            set_password(rec["user_id"], password, must_change=False)
            execute("UPDATE password_tokens SET used_at = ? WHERE id = ?", (now(), rec["id"]))
            session.clear()
            flash("Password set. You can now log in.", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html", rec=rec)


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current, new = request.form.get("current_password") or "", request.form.get("new_password") or ""
        problem = password_problem(new)
        if not check_password_hash(g.user["password_hash"], current):
            flash("Current password is incorrect.", "error")
        elif problem:
            flash(problem, "error")
        elif new != request.form.get("confirm"):
            flash("New passwords do not match.", "error")
        else:
            set_password(g.user["id"], new, must_change=False)
            start_session(query("SELECT * FROM users WHERE id = ?", (g.user["id"],), one=True))
            flash("Password changed.", "success")
            return redirect(url_for("dashboard"))
    return render_template("account.html")


# ---------------------------------------------------------------- tenant scoping

def lead_scope(a="l"):
    """SQL condition limiting leads to what the current user may see."""
    if is_admin():
        return f"{a}.org_id = ?", [org_id()]
    return f"{a}.org_id = ? AND ({a}.entered_by = ? OR {a}.assigned_to = ?)", [org_id(), g.user["id"], g.user["id"]]


def project_scope(a="p"):
    """Projects visible to the user. Staff see projects they are assigned to, or that have no assignment list."""
    if is_admin():
        return f"{a}.org_id = ?", [org_id()]
    return (f"{a}.org_id = ? AND (NOT EXISTS (SELECT 1 FROM project_staff ps WHERE ps.project_id = {a}.id)"
            f" OR EXISTS (SELECT 1 FROM project_staff ps WHERE ps.project_id = {a}.id AND ps.user_id = ?))",
            [org_id(), g.user["id"]])


LEAD_COLUMNS = """l.*, p.name project_name, p.code project_code, u.name staff_name, ua.name assigned_name,
    uu.name updated_by_name,
    (SELECT GROUP_CONCAT(pr.name, ', ') FROM lead_products lp JOIN products pr ON pr.id = lp.product_id
       WHERE lp.lead_id = l.id) product_names"""
LEAD_FROM = """FROM leads l JOIN projects p ON p.id = l.project_id JOIN users u ON u.id = l.entered_by
    LEFT JOIN users ua ON ua.id = l.assigned_to LEFT JOIN users uu ON uu.id = l.updated_by"""


def get_lead(lid):
    where, args = lead_scope()
    return query(f"SELECT {LEAD_COLUMNS} {LEAD_FROM} WHERE l.id = ? AND {where}", [lid] + args, one=True) or abort(404)


def get_project(pid):
    where, args = project_scope()
    return query(f"SELECT p.* FROM projects p WHERE p.id = ? AND {where}", [pid] + args, one=True) or abort(404)


def get_product(pid):
    return query("SELECT * FROM products WHERE id = ? AND org_id = ?", (pid, org_id()), one=True) or abort(404)


def org_users(active_only=True):
    return query("SELECT id, name, role, active FROM users WHERE org_id = ?" + (" AND active = 1" if active_only else "")
                 + " ORDER BY name COLLATE NOCASE", (org_id(),))


def org_products(active_only=True):
    return query("SELECT * FROM products WHERE org_id = ?" + (" AND active = 1" if active_only else "")
                 + " ORDER BY name COLLATE NOCASE", (org_id(),))


def visible_projects(active_only=False, include_id=None):
    where, args = project_scope()
    extra = ""
    if active_only:
        extra = " AND (p.status IN ('Active', 'Planned') OR p.id = ?)"
        args = args + [include_id or 0]
    return query(f"SELECT p.* FROM projects p WHERE {where}{extra} ORDER BY p.name COLLATE NOCASE", args)


# ---------------------------------------------------------------- lead filtering

SORTABLE = {
    "created": "l.created_at",
    "name": "l.customer_name COLLATE NOCASE",
    "phone": "l.phone",
    "email": "l.email COLLATE NOCASE",
    "place": "l.place COLLATE NOCASE",
    "district": "l.district COLLATE NOCASE",
    "state": "l.state COLLATE NOCASE",
    "country": "l.country COLLATE NOCASE",
    "requirement": "product_names COLLATE NOCASE",
    "status": "l.status",
    "possibility": "CASE l.buying_possibility WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 WHEN 'Low' THEN 3 ELSE 4 END",
    "quality": "CASE l.lead_quality WHEN 'Hot' THEN 1 WHEN 'Warm' THEN 2 WHEN 'Cold' THEN 3 ELSE 4 END",
    "priority": "CASE l.priority WHEN 'Urgent' THEN 1 WHEN 'High' THEN 2 WHEN 'Normal' THEN 3 WHEN 'Low' THEN 4 ELSE 5 END",
    "followup": "COALESCE(l.next_follow_up, '9999')",
    "staff": "u.name COLLATE NOCASE",
    "assigned": "ua.name COLLATE NOCASE",
    "project": "p.name COLLATE NOCASE",
}

TEXT_FILTERS = {
    "status": "l.status", "possibility": "l.buying_possibility", "quality": "l.lead_quality",
    "priority": "l.priority", "state": "l.state", "district": "l.district", "place": "l.place",
    "country": "l.country", "source": "l.source",
}


def lead_filters(params):
    """Return (clauses, args) for the lead scope plus filters given in params."""
    scope_sql, args = lead_scope()
    clauses = [scope_sql]
    for key, column in TEXT_FILTERS.items():
        value = params.get(key, "")
        if value == "":
            continue
        if value == "(not set)":
            clauses.append(f"({column} IS NULL OR TRIM({column}) = '')")
        else:
            clauses.append(f"{column} = ? COLLATE NOCASE")
            args.append(value)
    id_filters = {"project": "l.project_id"}
    if is_admin():  # filtering by staff only makes sense (and is only allowed) for admins
        id_filters.update(staff="l.entered_by", assigned="l.assigned_to")
    for key, column in id_filters.items():
        if str(params.get(key, "")).isdigit():
            clauses.append(f"{column} = ?")
            args.append(int(params[key]))
    if str(params.get("product", "")).isdigit():
        if params.get("stage") in PRODUCT_STAGES:
            clauses.append("EXISTS (SELECT 1 FROM lead_products lp WHERE lp.lead_id = l.id"
                           " AND lp.product_id = ? AND lp.stage = ?)")
            args += [int(params["product"]), params["stage"]]
        else:
            clauses.append("EXISTS (SELECT 1 FROM lead_products lp WHERE lp.lead_id = l.id AND lp.product_id = ?)")
            args.append(int(params["product"]))
    q = clean(params.get("q"))
    if q:
        like = f"%{q}%"
        cols = ["l.customer_name", "l.phone", "l.whatsapp", "l.email", "l.place", "l.district", "l.state",
                "l.requirement", "l.notes"]
        clauses.append("(" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")")
        args += [like] * len(cols)
    for key, op in (("date_from", ">="), ("date_to", "<=")):
        d = valid_date(params.get(key))
        if d:
            clauses.append(f"date(l.created_at) {op} date(?)")
            args.append(d)
    fu = params.get("followup")
    if fu == "due":
        clauses.append("l.next_follow_up IS NOT NULL AND l.next_follow_up != '' AND l.next_follow_up <= ?")
        args.append(today())
    elif fu == "scheduled":
        clauses.append("l.next_follow_up IS NOT NULL AND l.next_follow_up != ''")
    return clauses, args


def lead_list(params, extra_columns="", extra_args=()):
    """Filtered, sorted, paginated lead list. Returns a dict for the template."""
    clauses, args = lead_filters(params)
    where = " AND ".join(clauses)
    sort = params.get("sort") if params.get("sort") in SORTABLE else "created"
    direction = "ASC" if params.get("dir") == "asc" else "DESC"
    total = scalar(f"SELECT COUNT(*) {LEAD_FROM} WHERE {where}", args)
    pages = max(1, -(-total // PER_PAGE))
    try:
        page = min(max(1, int(params.get("page") or 1)), pages)
    except ValueError:
        page = 1
    rows = query(f"SELECT {LEAD_COLUMNS}{extra_columns} {LEAD_FROM} WHERE {where} "
                 f"ORDER BY {SORTABLE[sort]} {direction}, l.id DESC LIMIT ? OFFSET ?",
                 list(extra_args) + args + [PER_PAGE, (page - 1) * PER_PAGE])
    return dict(leads=rows, total=total, page=page, pages=pages, sort=sort, direction=direction.lower())


def distinct_values(column):
    where, args = lead_scope()
    return [r["v"] for r in query(
        f"SELECT DISTINCT TRIM({column}) v FROM leads l WHERE {where} AND TRIM(COALESCE({column},'')) != ''"
        f" ORDER BY v COLLATE NOCASE", args)]


def filter_options():
    return dict(projects_all=visible_projects(), products_all=org_products(active_only=False),
                staff=org_users(active_only=False) if is_admin() else [],
                states=distinct_values("l.state"), places=distinct_values("l.place"),
                districts=distinct_values("l.district"), countries=distinct_values("l.country"))


def clean_params(exclude=()):
    return {k: v for k, v in request.args.items() if v != "" and k not in exclude}


# ---------------------------------------------------------------- dashboard

def count_by(column, where, args, limit=None):
    rows = query(f"SELECT COALESCE(NULLIF(TRIM({column}),''),'(not set)') k, COUNT(*) c "
                 f"{LEAD_FROM} WHERE {where} GROUP BY k ORDER BY c DESC" + (f" LIMIT {int(limit)}" if limit else ""),
                 args)
    return rows


@app.route("/")
@login_required
def dashboard():
    where, args = lead_scope()

    def count(extra="", extra_args=()):
        return scalar(f"SELECT COUNT(*) FROM leads l WHERE {where}{extra}", args + list(extra_args))

    stats = dict(
        total=count(), today_added=count(" AND date(l.created_at) = date('now','localtime')"),
        new=count(" AND l.status = 'New'"), hot=count(" AND l.lead_quality = 'Hot'"),
        high=count(" AND l.buying_possibility = 'High'"), converted=count(" AND l.status = 'Converted'"),
        lost=count(" AND l.status IN ('Lost', 'Not Interested')"),
        followups_due=count(" AND l.next_follow_up IS NOT NULL AND l.next_follow_up != '' AND l.next_follow_up <= ?",
                            [today()]),
    )
    by_product = query(f"""SELECT pr.id, pr.name, COUNT(DISTINCT l.id) c FROM lead_products lp
                          JOIN products pr ON pr.id = lp.product_id JOIN leads l ON l.id = lp.lead_id
                          WHERE {where} GROUP BY pr.id ORDER BY c DESC LIMIT 10""", args)
    by_state = count_by("l.state", where, args, limit=10)
    by_status = count_by("l.status", where, args)
    by_project = query(f"""SELECT p.id, p.name, COUNT(l.id) c FROM leads l JOIN projects p ON p.id = l.project_id
                          WHERE {where} GROUP BY p.id ORDER BY c DESC LIMIT 10""", args)
    by_staff = query(f"""SELECT u.id, u.name, COUNT(l.id) c,
                           SUM(l.lead_quality = 'Hot') hot, SUM(l.status = 'Converted') converted
                         FROM leads l JOIN users u ON u.id = l.entered_by WHERE {where}
                         GROUP BY u.id ORDER BY c DESC""", args) if is_admin() else []
    due_followups = query(f"""SELECT f.*, l.customer_name, l.phone FROM follow_ups f JOIN leads l ON l.id = f.lead_id
                             WHERE {where} AND f.status = 'Pending' AND f.due_date <= ?
                             ORDER BY f.due_date LIMIT 8""", args + [today()])
    recent = query(f"SELECT {LEAD_COLUMNS} {LEAD_FROM} WHERE {where} ORDER BY l.created_at DESC, l.id DESC LIMIT 8",
                   args)
    orgs = []
    if is_super():
        orgs = query("""SELECT o.*, (SELECT COUNT(*) FROM users u WHERE u.org_id = o.id) users,
                          (SELECT COUNT(*) FROM users u WHERE u.org_id = o.id AND u.active = 1) active_users,
                          (SELECT COUNT(*) FROM leads l WHERE l.org_id = o.id) leads,
                          (SELECT COUNT(*) FROM projects p WHERE p.org_id = o.id) projects
                        FROM organizations o ORDER BY o.kind, o.name COLLATE NOCASE""")
    return render_template("dashboard.html", stats=stats, by_product=by_product, by_state=by_state,
                           by_status=by_status, by_project=by_project, by_staff=by_staff,
                           due_followups=due_followups, recent=recent, orgs=orgs)


# ---------------------------------------------------------------- projects

@app.route("/projects")
@login_required
def projects():
    where, args = project_scope()
    lwhere, largs = lead_scope()
    rows = query(f"""SELECT p.*, u.name creator,
                       (SELECT COUNT(*) FROM leads l WHERE l.project_id = p.id AND {lwhere}) lead_count,
                       (SELECT GROUP_CONCAT(su.name, ', ') FROM project_staff ps JOIN users su ON su.id = ps.user_id
                          WHERE ps.project_id = p.id) staff_names,
                       (SELECT GROUP_CONCAT(pr.name, ', ') FROM project_products pp JOIN products pr ON pr.id = pp.product_id
                          WHERE pp.project_id = p.id) product_names
                     FROM projects p LEFT JOIN users u ON u.id = p.created_by
                     WHERE {where} ORDER BY CASE p.status WHEN 'Active' THEN 0 WHEN 'Planned' THEN 1 ELSE 2 END,
                       p.name COLLATE NOCASE""", largs + args)
    return render_template("projects.html", projects=rows)


def project_form_data():
    data = {k: clean(request.form.get(k)) for k in
            ("name", "code", "client_name", "client_phone", "client_email", "description", "campaign_type",
             "start_date", "end_date", "status")}
    data["code"] = data["code"].upper()
    data["staff_ids"] = int_list(request.form.getlist("staff_ids"))
    data["product_ids"] = int_list(request.form.getlist("product_ids"))
    return data


def validate_project(data, pid=None):
    errors = []
    if not data["name"]:
        errors.append("Project name is required.")
    if data["status"] not in PROJECT_STATUSES:
        data["status"] = "Active"
    for k in ("start_date", "end_date"):
        d = valid_date(data[k])
        if d is None:
            errors.append("Dates must be valid.")
            data[k] = ""
        else:
            data[k] = d
    if data["start_date"] and data["end_date"] and data["end_date"] < data["start_date"]:
        errors.append("End date cannot be before start date.")
    if data["code"] and query("SELECT 1 FROM projects WHERE org_id = ? AND code = ? COLLATE NOCASE AND id != ?",
                              (org_id(), data["code"], pid or 0), one=True):
        errors.append(f"Project code {data['code']} is already used in your organization.")
    # Only keep ids that really belong to this organization.
    valid_staff = {u["id"] for u in org_users(active_only=False)}
    valid_products = {p["id"] for p in org_products(active_only=False)}
    data["staff_ids"] = [i for i in data["staff_ids"] if i in valid_staff]
    data["product_ids"] = [i for i in data["product_ids"] if i in valid_products]
    return errors


def save_project_links(db, pid, data):
    db.execute("DELETE FROM project_staff WHERE project_id = ?", (pid,))
    db.execute("DELETE FROM project_products WHERE project_id = ?", (pid,))
    db.executemany("INSERT INTO project_staff (project_id, user_id) VALUES (?,?)", [(pid, u) for u in data["staff_ids"]])
    db.executemany("INSERT INTO project_products (project_id, product_id) VALUES (?,?)",
                   [(pid, p) for p in data["product_ids"]])


PROJECT_COLS = ("name", "code", "client_name", "client_phone", "client_email", "description", "campaign_type",
                "start_date", "end_date", "status")


@app.route("/projects/new", methods=["GET", "POST"])
@admin_required
def project_new():
    data = {"status": "Active", "client_name": g.org["name"], "staff_ids": [], "product_ids": []}
    if request.method == "POST":
        data = project_form_data()
        errors = validate_project(data)
        for e in errors:
            flash(e, "error")
        if not errors:
            db = get_db()
            ts = now()
            pid = db.execute(f"INSERT INTO projects (org_id, {', '.join(PROJECT_COLS)}, created_by, created_at, updated_at)"
                             f" VALUES (?, {', '.join('?' * len(PROJECT_COLS))}, ?, ?, ?)",
                             (org_id(), *[data[k] for k in PROJECT_COLS], g.user["id"], ts, ts)).lastrowid
            save_project_links(db, pid, data)
            db.commit()
            flash(f"Project '{data['name']}' created.", "success")
            return redirect(url_for("projects"))
    return render_template("project_form.html", data=data, project=None, users=org_users(), products=org_products())


@app.route("/projects/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def project_edit(pid):
    project = get_project(pid)
    data = dict(project)
    data["staff_ids"] = [r[0] for r in query("SELECT user_id FROM project_staff WHERE project_id = ?", (pid,))]
    data["product_ids"] = [r[0] for r in query("SELECT product_id FROM project_products WHERE project_id = ?", (pid,))]
    if request.method == "POST":
        data = project_form_data()
        errors = validate_project(data, pid)
        for e in errors:
            flash(e, "error")
        if not errors:
            db = get_db()
            db.execute(f"UPDATE projects SET {', '.join(k + ' = ?' for k in PROJECT_COLS)}, updated_at = ?"
                       " WHERE id = ? AND org_id = ?", (*[data[k] for k in PROJECT_COLS], now(), pid, org_id()))
            save_project_links(db, pid, data)
            db.commit()
            flash("Project updated.", "success")
            return redirect(url_for("projects"))
    return render_template("project_form.html", data=data, project=project,
                           users=org_users(active_only=False), products=org_products(active_only=False))


@app.route("/projects/<int:pid>/delete", methods=["POST"])
@admin_required
def project_delete(pid):
    project = get_project(pid)
    execute("DELETE FROM projects WHERE id = ? AND org_id = ?", (pid, org_id()))
    flash(f"Project '{project['name']}' and all its leads were deleted.", "success")
    return redirect(url_for("projects"))


# ---------------------------------------------------------------- products & services

@app.route("/products")
@login_required
def products():
    params = clean_params()
    lwhere, largs = lead_scope()
    clauses, args = ["pr.org_id = ?"], [org_id()]
    if not is_admin():
        clauses.append("pr.active = 1")
    elif params.get("status") in ("active", "inactive"):
        clauses.append("pr.active = ?")
        args.append(1 if params["status"] == "active" else 0)
    if params.get("q"):
        like = f"%{params['q']}%"
        clauses.append("(pr.name LIKE ? OR pr.code LIKE ? OR pr.category LIKE ? OR pr.description LIKE ?)")
        args += [like] * 4
    if params.get("category"):
        clauses.append("pr.category = ?")
        args.append(params["category"])
    rows = query(f"""SELECT pr.*, u.name creator,
                       (SELECT COUNT(DISTINCT lp.lead_id) FROM lead_products lp JOIN leads l ON l.id = lp.lead_id
                          WHERE lp.product_id = pr.id AND {lwhere}) lead_count
                     FROM products pr LEFT JOIN users u ON u.id = pr.created_by
                     WHERE {' AND '.join(clauses)} ORDER BY pr.active DESC, pr.name COLLATE NOCASE""", largs + args)
    categories = [r[0] for r in query("SELECT DISTINCT category FROM products WHERE org_id = ? AND category != ''"
                                      " ORDER BY category", (org_id(),))]
    return render_template("products.html", products=rows, params=params, categories=categories)


def product_form_data():
    data = {k: clean(request.form.get(k)) for k in ("name", "code", "category", "description")}
    data["code"] = data["code"].upper()
    data["active"] = 1 if request.form.get("active") else 0
    return data


def validate_product(data, pid=None):
    errors = []
    if not data["name"]:
        errors.append("Product / service name is required.")
    elif query("SELECT 1 FROM products WHERE org_id = ? AND name = ? COLLATE NOCASE AND id != ?",
               (org_id(), data["name"], pid or 0), one=True):
        errors.append("A product with this name already exists.")
    if data["code"] and query("SELECT 1 FROM products WHERE org_id = ? AND code = ? COLLATE NOCASE AND id != ?",
                              (org_id(), data["code"], pid or 0), one=True):
        errors.append(f"Product code {data['code']} is already used.")
    return errors


@app.route("/products/new", methods=["GET", "POST"])
@admin_required
def product_new():
    data = {"active": 1}
    if request.method == "POST":
        data = product_form_data()
        errors = validate_product(data)
        for e in errors:
            flash(e, "error")
        if not errors:
            ts = now()
            execute("INSERT INTO products (org_id, name, code, category, description, active, created_by, created_at,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (org_id(), data["name"], data["code"], data["category"], data["description"], data["active"],
                     g.user["id"], ts, ts))
            flash(f"'{data['name']}' added.", "success")
            if request.form.get("save_and_new"):
                return redirect(url_for("product_new"))
            return redirect(url_for("products"))
    return render_template("product_form.html", data=data, product=None)


@app.route("/products/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def product_edit(pid):
    product = get_product(pid)
    data = dict(product)
    if request.method == "POST":
        data = product_form_data()
        errors = validate_product(data, pid)
        for e in errors:
            flash(e, "error")
        if not errors:
            execute("UPDATE products SET name=?, code=?, category=?, description=?, active=?, updated_at=?"
                    " WHERE id = ? AND org_id = ?", (data["name"], data["code"], data["category"],
                                                    data["description"], data["active"], now(), pid, org_id()))
            flash("Product updated.", "success")
            return redirect(url_for("products"))
    return render_template("product_form.html", data=data, product=product)


@app.route("/products/<int:pid>/toggle", methods=["POST"])
@admin_required
def product_toggle(pid):
    product = get_product(pid)
    execute("UPDATE products SET active = ?, updated_at = ? WHERE id = ? AND org_id = ?",
            (0 if product["active"] else 1, now(), pid, org_id()))
    flash(f"'{product['name']}' {'deactivated' if product['active'] else 'activated'}.", "success")
    return redirect(request.referrer if (request.referrer or "").startswith(request.host_url) else url_for("products"))


# ---------------------------------------------------------------- leads

LEAD_TEXT_FIELDS = ["customer_name", "phone", "whatsapp", "email", "place", "district", "state", "country",
                    "requirement", "status", "buying_possibility", "lead_quality", "priority", "source", "notes"]


def lead_form_data():
    data = {k: clean(request.form.get(k)) for k in LEAD_TEXT_FIELDS}
    if data["state"] == "__other__":
        data["state"] = clean(request.form.get("state_other"))
    data["state"], data["country"] = normalize_location(data["state"], data["country"] or DEFAULT_COUNTRY)
    data["project_id"] = request.form.get("project_id", type=int)
    data["assigned_to"] = request.form.get("assigned_to", type=int)
    data["product_ids"] = int_list(request.form.getlist("product_ids"))
    data["next_follow_up"] = clean(request.form.get("next_follow_up"))
    return data


def validate_lead(data, lead=None):
    """Validate and sanitise lead data in place. Ownership/tenant fields are derived server-side."""
    errors = []
    allowed_projects = {p["id"] for p in visible_projects()}
    if not data.get("project_id") or data["project_id"] not in allowed_projects:
        errors.append("Please choose a project.")
    if not data["customer_name"]:
        errors.append("Customer name is required.")
    if not data["phone"] and not data["whatsapp"] and not data["email"]:
        errors.append("Enter at least one contact: phone, WhatsApp or email.")
    if data["email"] and "@" not in data["email"]:
        errors.append("Email address looks invalid.")
    if data["status"] not in LEAD_STATUSES:
        data["status"] = "New"
    for key, options in (("buying_possibility", BUYING_POSSIBILITIES), ("lead_quality", LEAD_QUALITIES),
                         ("priority", PRIORITIES), ("source", LEAD_SOURCES)):
        if data[key] not in options:
            data[key] = ""
    fu = valid_date(data.get("next_follow_up"))
    if fu is None:
        errors.append("Next follow-up date is invalid.")
        data["next_follow_up"] = ""
    else:
        data["next_follow_up"] = fu
    # Products: active products of this org (plus ones already linked to this lead).
    allowed = {p["id"] for p in org_products()}
    if lead:
        allowed |= {r[0] for r in query("SELECT product_id FROM lead_products WHERE lead_id = ?", (lead["id"],))}
    data["product_ids"] = [p for p in data.get("product_ids", []) if p in allowed]
    # Assignment: only admins choose; staff keep the existing assignee (or themselves for new leads).
    if is_admin():
        valid_users = {u["id"] for u in org_users()}
        if lead and lead["assigned_to"]:
            valid_users.add(lead["assigned_to"])
        if data.get("assigned_to") not in valid_users:
            data["assigned_to"] = lead["assigned_to"] if lead else g.user["id"]
    else:
        data["assigned_to"] = lead["assigned_to"] if lead else g.user["id"]
    return errors


def find_duplicates(data, exclude_id=None):
    """Same phone/WhatsApp/email anywhere in this organization. Returns (visible_rows, hidden_count)."""
    conds, args = [], [org_id()]
    for field in ("phone", "whatsapp"):
        key = phone_key(data[field])
        if len(key) >= 6:
            conds.append("l.phone_norm = ? OR REPLACE(REPLACE(REPLACE(l.whatsapp,' ',''),'-',''),'+','') LIKE ?")
            args += [key, f"%{key}"]
    if data["email"]:
        conds.append("l.email = ? COLLATE NOCASE")
        args.append(data["email"])
    if not conds:
        return [], 0
    sql = f"SELECT l.id FROM leads l WHERE l.org_id = ? AND ({' OR '.join(conds)}) AND l.id != ?"
    ids = [r[0] for r in query(sql, args + [exclude_id or 0])]
    if not ids:
        return [], 0
    where, wargs = lead_scope()
    visible = query(f"SELECT {LEAD_COLUMNS} {LEAD_FROM} WHERE l.id IN ({','.join('?' * len(ids))}) AND {where}",
                    ids + wargs)
    return visible, len(ids) - len(visible)


def sync_products(db, lead_id, product_ids, ts):
    current = {r[0] for r in db.execute("SELECT product_id FROM lead_products WHERE lead_id = ?", (lead_id,))}
    wanted = set(product_ids)
    for pid in wanted - current:
        db.execute("INSERT INTO lead_products (lead_id, product_id, org_id, stage, created_at, updated_at, updated_by)"
                   " VALUES (?,?,?, 'Interested', ?, ?, ?)", (lead_id, pid, org_id(), ts, ts, g.user["id"]))
    for pid in current - wanted:
        db.execute("DELETE FROM lead_products WHERE lead_id = ? AND product_id = ?", (lead_id, pid))
    names = lambda ids: ", ".join(r[0] for r in db.execute(
        f"SELECT name FROM products WHERE id IN ({','.join('?' * len(ids))}) ORDER BY name", list(ids)))
    changes = []
    if wanted - current:
        changes.append("products added: " + names(wanted - current))
    if current - wanted:
        changes.append("products removed: " + names(current - wanted))
    return changes


def sync_next_follow_up(db, lead_id):
    db.execute("""UPDATE leads SET next_follow_up = (SELECT MIN(due_date) FROM follow_ups
                  WHERE lead_id = ? AND status = 'Pending') WHERE id = ?""", (lead_id, lead_id))


def schedule_follow_up(db, lead_id, due, current_due, ts):
    """Apply the lead form's 'next follow-up' date to the follow_ups table."""
    if due == (current_due or ""):
        return None
    pending = db.execute("SELECT id FROM follow_ups WHERE lead_id = ? AND status = 'Pending' ORDER BY due_date LIMIT 1",
                         (lead_id,)).fetchone()
    if due and pending:
        db.execute("UPDATE follow_ups SET due_date = ? WHERE id = ?", (due, pending[0]))
        msg = f"Follow-up rescheduled to {due}"
    elif due:
        db.execute("INSERT INTO follow_ups (org_id, lead_id, due_date, note, created_by, created_at)"
                   " VALUES (?,?,?,?,?,?)", (org_id(), lead_id, due, "", g.user["id"], ts))
        msg = f"Follow-up scheduled for {due}"
    else:
        db.execute("UPDATE follow_ups SET status = 'Cancelled', completed_by = ?, completed_at = ?"
                   " WHERE lead_id = ? AND status = 'Pending'", (g.user["id"], ts, lead_id))
        msg = "Pending follow-ups cancelled"
    sync_next_follow_up(db, lead_id)
    return msg


def lead_form_context(data, lead=None, duplicates=None, hidden_duplicates=0):
    return dict(data=data, lead=lead, duplicates=duplicates or [], hidden_duplicates=hidden_duplicates,
                projects_all=visible_projects(active_only=True, include_id=data.get("project_id")),
                products_all=[p for p in org_products(active_only=False)
                              if p["active"] or p["id"] in data.get("product_ids", [])],
                users=org_users(), places=distinct_values("l.place"), districts=distinct_values("l.district"))


@app.route("/leads")
@login_required
def leads():
    params = clean_params()
    ctx = lead_list(params)
    project = get_project(int(params["project"])) if str(params.get("project", "")).isdigit() else None
    return render_template("leads.html", params=params, project=project, **ctx, **filter_options())


@app.route("/leads/export")
@login_required
def leads_export():
    clauses, args = lead_filters(clean_params())
    sort = request.args.get("sort") if request.args.get("sort") in SORTABLE else "created"
    rows = query(f"""SELECT {LEAD_COLUMNS},
                       (SELECT GROUP_CONCAT(pr.name || ' [' || lp.stage || ']', '; ') FROM lead_products lp
                          JOIN products pr ON pr.id = lp.product_id WHERE lp.lead_id = l.id) product_stages
                     {LEAD_FROM} WHERE {' AND '.join(clauses)}
                     ORDER BY {SORTABLE[sort]} {'ASC' if request.args.get('dir') == 'asc' else 'DESC'}""", args)
    out = io.StringIO()
    writer = SafeCsvWriter(out)
    writer.writerow(["ID", "Project", "Project Code", "Customer Name", "Phone", "WhatsApp", "Email", "Place",
                     "District", "State", "Country", "Products", "Requirement", "Status", "Buying Possibility",
                     "Lead Quality", "Priority", "Source", "Next Follow-up", "Assigned To", "Created By",
                     "Created At", "Updated By", "Updated At", "Notes"])
    for r in rows:
        writer.writerow([r["id"], r["project_name"], r["project_code"], r["customer_name"], r["phone"], r["whatsapp"],
                         r["email"], r["place"], r["district"], r["state"], r["country"], r["product_stages"],
                         r["requirement"], r["status"], r["buying_possibility"], r["lead_quality"], r["priority"],
                         r["source"], r["next_follow_up"], r["assigned_name"], r["staff_name"], r["created_at"],
                         r["updated_by_name"], r["updated_at"], r["notes"]])
    if g.get("viewing"):
        audit("Super Admin exported leads (CSV)", f"{len(rows)} leads")
    filename = f"leads-{datetime.now():%Y%m%d-%H%M}.csv"
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/leads/new", methods=["GET", "POST"])
@login_required
def lead_new():
    data = {k: "" for k in LEAD_TEXT_FIELDS}
    data.update(status="New", priority="Normal", country=DEFAULT_COUNTRY,
                project_id=request.args.get("project", type=int), product_ids=[], assigned_to=g.user["id"])
    if request.method == "POST":
        data = lead_form_data()
        errors = validate_lead(data)
        for e in errors:
            flash(e, "error")
        if not errors:
            dups, hidden = find_duplicates(data)
            if (dups or hidden) and not request.form.get("confirm_duplicate"):
                flash("This customer may already exist in your organization. Review below, then tick "
                      "'Save anyway' if this is a separate enquiry.", "warning")
                return render_template("lead_form.html", **lead_form_context(data, duplicates=dups,
                                                                             hidden_duplicates=hidden))
            db = get_db()
            ts = now()
            cols = LEAD_TEXT_FIELDS + ["project_id", "assigned_to"]
            lead_id = db.execute(
                f"INSERT INTO leads (org_id, entered_by, phone_norm, created_at, updated_at, {', '.join(cols)})"
                f" VALUES (?,?,?,?,?, {', '.join('?' * len(cols))})",
                (org_id(), g.user["id"], phone_key(data["phone"] or data["whatsapp"]), ts, ts,
                 *[data[k] for k in cols])).lastrowid
            sync_products(db, lead_id, data["product_ids"], ts)
            schedule_follow_up(db, lead_id, data["next_follow_up"], "", ts)
            log_activity(lead_id, "Lead created", commit=False)
            db.commit()
            flash(f"Lead '{data['customer_name']}' saved. Created by {g.user['name']}.", "success")
            if request.form.get("save_and_new"):
                return redirect(url_for("lead_new", project=data["project_id"]))
            return redirect(url_for("lead_view", lid=lead_id))
    return render_template("lead_form.html", **lead_form_context(data))


@app.route("/leads/<int:lid>")
@login_required
def lead_view(lid):
    lead = get_lead(lid)
    products_linked = query("""SELECT lp.*, pr.name, pr.code, pr.active, u.name updated_by_name FROM lead_products lp
                              JOIN products pr ON pr.id = lp.product_id LEFT JOIN users u ON u.id = lp.updated_by
                              WHERE lp.lead_id = ? ORDER BY pr.name""", (lid,))
    followups = query("""SELECT f.*, u.name created_by_name, c.name completed_by_name FROM follow_ups f
                        JOIN users u ON u.id = f.created_by LEFT JOIN users c ON c.id = f.completed_by
                        WHERE f.lead_id = ? ORDER BY f.status = 'Pending' DESC, f.due_date""", (lid,))
    activity = query("""SELECT a.*, u.name user_name FROM lead_activity a JOIN users u ON u.id = a.user_id
                        WHERE a.lead_id = ? ORDER BY a.id DESC""", (lid,))
    return render_template("lead_view.html", lead=lead, products_linked=products_linked, followups=followups,
                           activity=activity)


@app.route("/leads/<int:lid>/edit", methods=["GET", "POST"])
@login_required
def lead_edit(lid):
    lead = get_lead(lid)
    data = dict(lead)
    data["product_ids"] = [r[0] for r in query("SELECT product_id FROM lead_products WHERE lead_id = ?", (lid,))]
    if request.method == "POST":
        data = lead_form_data()
        errors = validate_lead(data, lead)
        for e in errors:
            flash(e, "error")
        if not errors:
            db = get_db()
            ts = now()
            changes = [f"{k.replace('_', ' ')}: '{lead[k] or ''}' → '{data[k]}'"
                       for k in LEAD_TEXT_FIELDS if k != "notes" and (lead[k] or "") != data[k]]
            if (lead["notes"] or "") != data["notes"]:
                changes.append("notes updated")
            if lead["project_id"] != data["project_id"]:
                changes.append("moved to another project")
            if lead["assigned_to"] != data["assigned_to"]:
                changes.append("reassigned")
            cols = LEAD_TEXT_FIELDS + ["project_id", "assigned_to"]
            where, args = lead_scope("leads")
            db.execute(f"UPDATE leads SET {', '.join(k + ' = ?' for k in cols)}, phone_norm = ?, updated_by = ?,"
                       f" updated_at = ? WHERE id = ? AND {where}",
                       (*[data[k] for k in cols], phone_key(data["phone"] or data["whatsapp"]), g.user["id"], ts,
                        lid, *args))
            changes += sync_products(db, lid, data["product_ids"], ts)
            msg = schedule_follow_up(db, lid, data["next_follow_up"], lead["next_follow_up"], ts)
            if msg:
                changes.append(msg)
            if changes:
                log_activity(lid, "Updated " + "; ".join(changes), commit=False)
            db.commit()
            flash("Lead updated.", "success")
            return redirect(url_for("lead_view", lid=lid))
    return render_template("lead_form.html", **lead_form_context(data, lead=lead))


def touch_lead(db, lid, ts):
    db.execute("UPDATE leads SET updated_by = ?, updated_at = ? WHERE id = ?", (g.user["id"], ts, lid))


QUICK_FIELDS = {"status": ("Status", LEAD_STATUSES), "lead_quality": ("Quality", LEAD_QUALITIES),
                "buying_possibility": ("Buying possibility", BUYING_POSSIBILITIES), "priority": ("Priority", PRIORITIES)}


@app.route("/leads/<int:lid>/status", methods=["POST"])
@login_required
def lead_quick_status(lid):
    """Quick update from the lead page or straight from a lead list: status, quality, possibility,
    priority (only the fields sent) and an optional note. Every change goes to the lead's history."""
    lead = get_lead(lid)
    db = get_db()
    ts = now()
    changes = []
    for field, (label, options) in QUICK_FIELDS.items():
        if field not in request.form:
            continue
        value = request.form.get(field) or ""
        if value and value not in options:
            continue
        if field == "status" and not value:
            continue
        if value != (lead[field] or ""):
            db.execute(f"UPDATE leads SET {field} = ? WHERE id = ?", (value, lid))
            changes.append(f"{label}: '{lead[field] or ''}' → '{value}'")
    if changes:
        touch_lead(db, lid, ts)
        log_activity(lid, "; ".join(changes), commit=False)
    note = clean(request.form.get("note"))
    if note:
        log_activity(lid, f"Note: {note}", commit=False)
    db.commit()
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        if changes:
            flash(f"{lead['customer_name']}: " + "; ".join(changes), "success")
        return redirect(nxt)
    return redirect(url_for("lead_view", lid=lid))


@app.route("/leads/<int:lid>/products", methods=["POST"])
@login_required
def lead_product_stages(lid):
    get_lead(lid)
    db = get_db()
    ts = now()
    changes = []
    for row in query("SELECT lp.product_id, lp.stage, pr.name FROM lead_products lp JOIN products pr"
                     " ON pr.id = lp.product_id WHERE lp.lead_id = ?", (lid,)):
        new = request.form.get(f"stage_{row['product_id']}")
        if new in PRODUCT_STAGES and new != row["stage"]:
            db.execute("UPDATE lead_products SET stage = ?, updated_at = ?, updated_by = ? WHERE lead_id = ?"
                       " AND product_id = ?", (new, ts, g.user["id"], lid, row["product_id"]))
            changes.append(f"{row['name']}: {row['stage']} → {new}")
    if changes:
        touch_lead(db, lid, ts)
        log_activity(lid, "Product stage " + "; ".join(changes), commit=False)
        db.commit()
        flash("Product stages updated.", "success")
    return redirect(url_for("lead_view", lid=lid))


@app.route("/leads/<int:lid>/followups", methods=["POST"])
@login_required
def lead_followup_add(lid):
    get_lead(lid)
    due = valid_date(clean(request.form.get("due_date")))
    if not due:
        flash("Choose a valid follow-up date.", "error")
        return redirect(url_for("lead_view", lid=lid))
    db = get_db()
    ts = now()
    note = clean(request.form.get("note"))
    db.execute("INSERT INTO follow_ups (org_id, lead_id, due_date, note, created_by, created_at) VALUES (?,?,?,?,?,?)",
               (org_id(), lid, due, note, g.user["id"], ts))
    sync_next_follow_up(db, lid)
    log_activity(lid, f"Follow-up scheduled for {due}" + (f": {note}" if note else ""), commit=False)
    db.commit()
    flash("Follow-up scheduled.", "success")
    return redirect(url_for("lead_view", lid=lid))


@app.route("/followups/<int:fid>/complete", methods=["POST"])
@login_required
def followup_complete(fid):
    where, args = lead_scope()
    f = query(f"SELECT f.* FROM follow_ups f JOIN leads l ON l.id = f.lead_id WHERE f.id = ? AND {where}",
              [fid] + args, one=True) or abort(404)
    action = request.form.get("action", "done")
    if f["status"] == "Pending":
        db = get_db()
        ts = now()
        outcome = clean(request.form.get("outcome"))
        status = "Cancelled" if action == "cancel" else "Done"
        db.execute("UPDATE follow_ups SET status = ?, outcome = ?, completed_by = ?, completed_at = ? WHERE id = ?",
                   (status, outcome, g.user["id"], ts, fid))
        sync_next_follow_up(db, f["lead_id"])
        log_activity(f["lead_id"], f"Follow-up ({f['due_date']}) {status.lower()}" + (f": {outcome}" if outcome else ""),
                     commit=False)
        db.commit()
    nxt = request.form.get("next") or ""
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("lead_view", lid=f["lead_id"]))


@app.route("/leads/<int:lid>/delete", methods=["POST"])
@admin_required
def lead_delete(lid):
    lead = get_lead(lid)
    execute("DELETE FROM leads WHERE id = ? AND org_id = ?", (lid, org_id()))
    flash(f"Lead '{lead['customer_name']}' deleted.", "success")
    return redirect(url_for("leads", project=lead["project_id"]))


CSV_ALIASES = {
    "customer_name": ["customer_name", "name", "customer", "customer name", "full name", "full_name"],
    "phone": ["phone", "phone number", "mobile", "phone_number", "contact"],
    "whatsapp": ["whatsapp", "whatsapp number", "whatsapp_number"],
    "email": ["email", "email address", "e-mail"],
    "place": ["place", "city", "location", "town"],
    "district": ["district"],
    "state": ["state", "region"],
    "country": ["country"],
    "requirement": ["requirement", "requirement description", "requirements", "product details", "interest"],
    "products": ["products", "product", "product / service", "service"],
    "status": ["status"],
    "buying_possibility": ["buying_possibility", "buying possibility", "possibility"],
    "lead_quality": ["lead_quality", "lead quality", "quality"],
    "priority": ["priority"],
    "source": ["source", "lead source"],
    "notes": ["notes", "remarks", "comment", "comments"],
}


@app.route("/leads/import", methods=["GET", "POST"])
@login_required
def leads_import():
    projects_all = visible_projects(active_only=True)
    if request.method == "POST":
        pid = request.form.get("project_id", type=int)
        upload = request.files.get("file")
        if not pid or pid not in {p["id"] for p in projects_all}:
            flash("Please choose a project.", "error")
        elif not upload or not upload.filename:
            flash("Please choose a CSV file.", "error")
        else:
            text = upload.read().decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            headers = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
            mapping = {}
            for field, aliases in CSV_ALIASES.items():
                for alias in aliases:
                    if alias in headers:
                        mapping[field] = headers[alias]
                        break
            if "customer_name" not in mapping:
                flash("CSV must have a 'Name' (or 'Customer Name') column.", "error")
            else:
                products_by_name = {p["name"].lower(): p["id"] for p in org_products()}
                imported, skipped, ts = 0, 0, now()
                db = get_db()
                cols = LEAD_TEXT_FIELDS + ["project_id", "assigned_to"]
                for row in reader:
                    data = {f: clean(row.get(mapping[f])) if f in mapping else "" for f in LEAD_TEXT_FIELDS}
                    data["state"], data["country"] = normalize_location(data["state"], data["country"] or DEFAULT_COUNTRY)
                    data.update(project_id=pid, assigned_to=None, next_follow_up="", product_ids=[])
                    names = [n.split("[")[0].strip() for n in
                             clean(row.get(mapping["products"])).replace(";", ",").split(",")] if "products" in mapping else []
                    data["product_ids"] = [products_by_name[n.lower()] for n in names if n.lower() in products_by_name]
                    unknown = [n for n in names if n and n.lower() not in products_by_name]
                    if unknown and not data["requirement"]:
                        data["requirement"] = ", ".join(unknown)
                    if validate_lead(data):
                        skipped += 1
                        continue
                    data["assigned_to"] = g.user["id"]
                    lead_id = db.execute(
                        f"INSERT INTO leads (org_id, entered_by, phone_norm, created_at, updated_at, {', '.join(cols)})"
                        f" VALUES (?,?,?,?,?, {', '.join('?' * len(cols))})",
                        (org_id(), g.user["id"], phone_key(data["phone"] or data["whatsapp"]), ts, ts,
                         *[data[k] for k in cols])).lastrowid
                    sync_products(db, lead_id, data["product_ids"], ts)
                    log_activity(lead_id, "Imported from CSV", commit=False)
                    imported += 1
                db.commit()
                flash(f"Imported {imported} lead(s)." + (f" Skipped {skipped} row(s) missing a name or contact."
                                                         if skipped else ""), "success")
                return redirect(url_for("leads", project=pid))
    return render_template("import.html", projects_all=projects_all)


# ---------------------------------------------------------------- state-wise leads

@app.route("/states")
@login_required
def states():
    where, args = lead_scope()
    params = clean_params()
    if str(params.get("project", "")).isdigit():
        where += " AND l.project_id = ?"
        args = args + [int(params["project"])]
    rows = query(f"""SELECT COALESCE(NULLIF(TRIM(l.state),''),'(not set)') state,
                       COALESCE(NULLIF(TRIM(l.country),''),'') country, COUNT(*) total,
                       SUM(l.lead_quality = 'Hot') hot, SUM(l.status = 'Converted') converted
                     FROM leads l WHERE {where} GROUP BY 1, 2 ORDER BY total DESC, state""", args)
    india = [r for r in rows if r["country"] in ("India", "") and r["state"] != "(not set)"]
    other = [r for r in rows if r["country"] not in ("India", "") and r["state"] != "(not set)"]
    unset = [r for r in rows if r["state"] == "(not set)"]
    return render_template("states.html", india=india, other=other, unset=unset, params=params,
                           grand_total=sum(r["total"] for r in rows), projects_all=visible_projects())


@app.route("/states/view")
@login_required
def state_view():
    params = clean_params()
    state = params.get("state")
    if not state:
        return redirect(url_for("states"))
    ctx = lead_list(params)
    return render_template("state_view.html", state=state, params=params, **ctx, **filter_options())


# ---------------------------------------------------------------- product-wise leads

def product_stats(params, product_id=None):
    clauses, args = lead_filters({k: v for k, v in params.items() if k not in ("product", "stage")})
    join_cond = " AND ".join(clauses)
    extra = " AND pr.id = ?" if product_id else ""
    lost = "','".join(CLOSED_LOST_STATUSES)
    return query(f"""
        SELECT pr.id, pr.name, pr.code, pr.category, pr.active,
          COUNT(DISTINCT l.id) total_leads,
          COUNT(DISTINCT CASE WHEN l.id IS NOT NULL THEN
                COALESCE(NULLIF(l.phone_norm,''), LOWER(NULLIF(l.email,'')), 'lead-' || l.id) END) customers,
          COUNT(DISTINCT CASE WHEN l.lead_quality = 'Hot' THEN l.id END) hot,
          COUNT(DISTINCT CASE WHEN lp.stage = 'Ordered' THEN l.id END) ordered,
          COUNT(DISTINCT CASE WHEN lp.stage = 'Purchased' THEN l.id END) purchased,
          COUNT(DISTINCT CASE WHEN lp.stage = 'Interested' AND l.status NOT IN ('{lost}') THEN l.id END) open_interest,
          COUNT(DISTINCT CASE WHEN lp.stage = 'Dropped'
                OR (lp.stage = 'Interested' AND l.status IN ('{lost}')) THEN l.id END) lost
        FROM products pr
        LEFT JOIN lead_products lp ON lp.product_id = pr.id
        LEFT JOIN leads l ON l.id = lp.lead_id AND {join_cond}
        WHERE pr.org_id = ?{extra}
        GROUP BY pr.id
        HAVING pr.active = 1 OR COUNT(DISTINCT l.id) > 0
        ORDER BY total_leads DESC, pr.name COLLATE NOCASE""", args + [org_id()] + ([product_id] if product_id else []))


@app.route("/product-leads")
@login_required
def product_leads():
    params = clean_params()
    rows = product_stats(params)
    return render_template("product_leads.html", rows=rows, params=params, **filter_options())


@app.route("/product-leads/<int:pid>")
@login_required
def product_lead_list(pid):
    product = get_product(pid)
    params = clean_params()
    params["product"] = str(pid)
    stats = product_stats(params, product_id=pid)
    ctx = lead_list(params, extra_columns=", (SELECT stage FROM lead_products WHERE lead_id = l.id AND product_id = ?)"
                                          " product_stage", extra_args=[pid])
    return render_template("product_lead_list.html", product=product, stats=stats[0] if stats else None,
                           params=params, **ctx, **filter_options())


# ---------------------------------------------------------------- follow-ups

@app.route("/followups")
@login_required
def followups():
    view = request.args.get("view", "due")
    where, args = lead_scope()
    clauses = [where]
    t = today()
    if view == "overdue":
        clauses.append("f.status = 'Pending' AND f.due_date < ?")
        args.append(t)
    elif view == "today":
        clauses.append("f.status = 'Pending' AND f.due_date = ?")
        args.append(t)
    elif view == "upcoming":
        clauses.append("f.status = 'Pending' AND f.due_date > ?")
        args.append(t)
    elif view == "done":
        clauses.append("f.status != 'Pending'")
    else:
        view = "due"
        clauses.append("f.status = 'Pending' AND f.due_date <= ?")
        args.append(t)
    staff_id = request.args.get("staff", type=int)
    if is_admin() and staff_id:
        clauses.append("l.assigned_to = ?")
        args.append(staff_id)
    order = "f.completed_at DESC" if view == "done" else "f.due_date, f.id"
    rows = query(f"""SELECT f.*, l.customer_name, l.phone, l.whatsapp, l.status lead_status, l.lead_quality,
                       ua.name assigned_name, cu.name created_by_name, co.name completed_by_name
                     FROM follow_ups f JOIN leads l ON l.id = f.lead_id
                     LEFT JOIN users ua ON ua.id = l.assigned_to JOIN users cu ON cu.id = f.created_by
                     LEFT JOIN users co ON co.id = f.completed_by
                     WHERE {' AND '.join(clauses)} ORDER BY {order} LIMIT 500""", args)
    swhere, sargs = lead_scope()
    counts = query(f"""SELECT SUM(f.due_date < ?) overdue, SUM(f.due_date = ?) today, SUM(f.due_date > ?) upcoming
                      FROM follow_ups f JOIN leads l ON l.id = f.lead_id WHERE {swhere} AND f.status = 'Pending'""",
                   [t, t, t] + sargs, one=True)
    return render_template("followups.html", rows=rows, view=view, counts=counts, staff_id=staff_id,
                           staff=org_users(active_only=False) if is_admin() else [])


# ---------------------------------------------------------------- data export (every role, own scope)

class SafeCsvWriter:
    """csv.writer that neutralises spreadsheet formulas (=, @, +/- not followed by a digit)."""

    def __init__(self, out):
        self.w = csv.writer(out)

    @staticmethod
    def _cell(v):
        if v is None:
            return ""
        v = str(v) if not isinstance(v, str) else v
        if v and (v[0] in "=@\t\r" or (v[0] in "+-" and not v[1:2].isdigit())):
            return "'" + v
        return v

    def writerow(self, row):
        self.w.writerow([self._cell(v) for v in row])


def export_is_global():
    """Super Admin in the agency workspace exports every organization; everyone else their own scope."""
    return is_super() and not g.get("viewing")


def export_lead_where(a="l"):
    return ("1=1", []) if export_is_global() else lead_scope(a)


def export_org_where(a):
    return ("1=1", []) if export_is_global() else (f"{a}.org_id = ?", [org_id()])


def _export_leads():
    where, args = export_lead_where()
    rows = query(f"""SELECT {LEAD_COLUMNS}, o.name org_name,
                       (SELECT GROUP_CONCAT(pr.name || ' [' || lp.stage || ']', '; ') FROM lead_products lp
                          JOIN products pr ON pr.id = lp.product_id WHERE lp.lead_id = l.id) product_stages
                     {LEAD_FROM} JOIN organizations o ON o.id = l.org_id
                     WHERE {where} ORDER BY o.name, l.created_at DESC""", args)
    head = ["Organization", "Lead ID", "Project", "Project Code", "Customer Name", "Phone", "WhatsApp", "Email",
            "Place", "District", "State", "Country", "Products [stage]", "Requirement", "Status",
            "Buying Possibility", "Lead Quality", "Priority", "Source", "Next Follow-up", "Assigned To",
            "Created By", "Created At", "Updated By", "Updated At", "Notes"]
    return head, [[r["org_name"], r["id"], r["project_name"], r["project_code"], r["customer_name"], r["phone"],
                   r["whatsapp"], r["email"], r["place"], r["district"], r["state"], r["country"],
                   r["product_stages"], r["requirement"], r["status"], r["buying_possibility"], r["lead_quality"],
                   r["priority"], r["source"], r["next_follow_up"], r["assigned_name"], r["staff_name"],
                   r["created_at"], r["updated_by_name"], r["updated_at"], r["notes"]] for r in rows]


def _export_lead_products():
    where, args = export_lead_where()
    rows = query(f"""SELECT o.name org_name, l.id lead_id, l.customer_name, l.phone, pr.name, pr.code, lp.stage,
                       l.status, lp.created_at, lp.updated_at, u.name updated_by
                     FROM lead_products lp JOIN leads l ON l.id = lp.lead_id JOIN products pr ON pr.id = lp.product_id
                     JOIN organizations o ON o.id = l.org_id LEFT JOIN users u ON u.id = lp.updated_by
                     WHERE {where} ORDER BY o.name, pr.name, l.id""", args)
    head = ["Organization", "Lead ID", "Customer Name", "Phone", "Product", "Product Code", "Product Stage",
            "Lead Status", "Linked At", "Stage Updated At", "Stage Updated By"]
    return head, [list(r) for r in rows]


def _export_followups():
    where, args = export_lead_where()
    rows = query(f"""SELECT o.name, l.id, l.customer_name, l.phone, f.due_date, f.note, f.status, cu.name,
                       f.created_at, co.name, f.completed_at, f.outcome
                     FROM follow_ups f JOIN leads l ON l.id = f.lead_id JOIN organizations o ON o.id = l.org_id
                     JOIN users cu ON cu.id = f.created_by LEFT JOIN users co ON co.id = f.completed_by
                     WHERE {where} ORDER BY o.name, f.due_date""", args)
    head = ["Organization", "Lead ID", "Customer Name", "Phone", "Due Date", "Task", "Status", "Created By",
            "Created At", "Completed By", "Completed At", "Outcome"]
    return head, [list(r) for r in rows]


def _export_activity():
    where, args = export_lead_where()
    rows = query(f"""SELECT o.name, l.id, l.customer_name, u.name, a.action, a.created_at
                     FROM lead_activity a JOIN leads l ON l.id = a.lead_id JOIN organizations o ON o.id = l.org_id
                     JOIN users u ON u.id = a.user_id WHERE {where} ORDER BY o.name, a.id""", args)
    return ["Organization", "Lead ID", "Customer Name", "By", "Action", "At"], [list(r) for r in rows]


def _export_projects():
    if export_is_global():
        where, args = "1=1", []
    else:
        where, args = project_scope()
    rows = query(f"""SELECT o.name, p.name, p.code, p.client_name, p.campaign_type, p.start_date, p.end_date, p.status,
                       (SELECT GROUP_CONCAT(su.name, ', ') FROM project_staff ps JOIN users su ON su.id = ps.user_id
                          WHERE ps.project_id = p.id),
                       (SELECT GROUP_CONCAT(pr.name, ', ') FROM project_products pp JOIN products pr
                          ON pr.id = pp.product_id WHERE pp.project_id = p.id),
                       p.description, p.created_at
                     FROM projects p JOIN organizations o ON o.id = p.org_id WHERE {where} ORDER BY o.name, p.name""", args)
    head = ["Organization", "Project", "Code", "Client", "Campaign Type", "Start", "End", "Status", "Assigned Staff",
            "Products", "Description", "Created At"]
    return head, [list(r) for r in rows]


def _export_products():
    where, args = export_org_where("pr")
    if not is_admin():
        where += " AND pr.active = 1"
    rows = query(f"""SELECT o.name, pr.name, pr.code, pr.category, pr.description,
                       CASE pr.active WHEN 1 THEN 'Active' ELSE 'Inactive' END, u.name, pr.created_at
                     FROM products pr JOIN organizations o ON o.id = pr.org_id LEFT JOIN users u ON u.id = pr.created_by
                     WHERE {where} ORDER BY o.name, pr.name""", args)
    head = ["Organization", "Product / Service", "Code", "Category", "Description", "Status", "Created By",
            "Created At"]
    return head, [list(r) for r in rows]


def _export_users():
    if export_is_global():
        where, args = "1=1", []
    elif is_admin():
        where, args = "u.org_id = ?", [org_id()]
    else:
        where, args = "u.id = ?", [g.user["id"]]
    rows = query(f"""SELECT o.name, u.name, u.username, u.email, u.phone, u.designation, u.role,
                       CASE u.active WHEN 1 THEN 'Active' ELSE 'Deactivated' END, u.last_login_at, u.created_at,
                       (SELECT COUNT(*) FROM leads l WHERE l.entered_by = u.id)
                     FROM users u JOIN organizations o ON o.id = u.org_id WHERE {where} ORDER BY o.name, u.name""", args)
    head = ["Organization", "Name", "Username", "Email", "Phone", "Designation", "Role", "Status", "Last Login",
            "Created At", "Leads Created"]
    return head, [[*r[:6], ROLE_LABELS.get(r[6], r[6]), *r[7:]] for r in rows]


def _export_organizations():
    rows = query("""SELECT o.name, o.code, CASE o.kind WHEN 'agency' THEN 'Agency' ELSE 'Client' END, o.status,
                      o.contact_name, o.phone, o.email, o.address, o.created_at,
                      (SELECT COUNT(*) FROM users u WHERE u.org_id = o.id),
                      (SELECT COUNT(*) FROM leads l WHERE l.org_id = o.id)
                    FROM organizations o ORDER BY o.kind, o.name""")
    head = ["Organization", "Code", "Type", "Status", "Contact", "Phone", "Email", "Address", "Created At", "Users",
            "Leads"]
    return head, [list(r) for r in rows]


EXPORTS = {
    "leads": ("Leads", "Every lead with contact details, products, status and who created it", _export_leads),
    "lead-products": ("Product interest", "Each lead × product with its stage (Interested / Ordered / Purchased)",
                      _export_lead_products),
    "followups": ("Follow-ups", "Scheduled and completed follow-ups", _export_followups),
    "activity": ("Activity history", "Every change and note on each lead", _export_activity),
    "projects": ("Projects & campaigns", "Projects with dates, staff and products", _export_projects),
    "products": ("Products & services", "Product catalogue", _export_products),
    "users": ("Staff & users", "Accounts (no passwords) with lead counts", _export_users),
    "organizations": ("Organizations", "All organizations with user and lead counts (Super Admin only)",
                      _export_organizations),
}


def allowed_exports():
    return [k for k in EXPORTS if k != "organizations" or export_is_global()]


def _csv_bytes(head, rows):
    out = io.StringIO()
    w = SafeCsvWriter(out)
    w.writerow(head)
    for r in rows:
        w.writerow(r)
    return ("﻿" + out.getvalue()).encode("utf-8")


def _export_audit(what, count):
    """Record exports by a Super Admin in the affected organizations' access logs."""
    if not is_super():
        return
    if g.get("viewing"):
        audit(f"Super Admin exported {what}", f"{count} rows")
    elif export_is_global():
        for (oid,) in query("SELECT id FROM organizations WHERE kind = 'client'"):
            audit(f"Super Admin exported {what} for all organizations", org=oid)


def _export_scope_label():
    if export_is_global():
        return "all-organizations"
    base = "".join(ch if ch.isalnum() else "-" for ch in g.org["code"].lower())
    return base if is_admin() else f"{base}-{g.user['username'].lower()}"


@app.route("/export")
@login_required
def export_page():
    counts = {k: len(EXPORTS[k][2]()[1]) for k in allowed_exports()}
    return render_template("export.html", exports=[(k, *EXPORTS[k][:2], counts[k]) for k in allowed_exports()],
                           global_export=export_is_global())


@app.route("/export/<name>.csv")
@login_required
def export_csv(name):
    if name not in allowed_exports():
        abort(404)
    head, rows = EXPORTS[name][2]()
    _export_audit(EXPORTS[name][0].lower(), len(rows))
    filename = f"{_export_scope_label()}-{name}-{datetime.now():%Y%m%d-%H%M}.csv"
    return Response(_csv_bytes(head, rows), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/export/all.zip")
@login_required
def export_zip():
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in allowed_exports():
            head, rows = EXPORTS[name][2]()
            total += len(rows)
            zf.writestr(f"{name}.csv", _csv_bytes(head, rows))
    _export_audit("all data (ZIP)", total)
    filename = f"{_export_scope_label()}-export-{datetime.now():%Y%m%d-%H%M}.zip"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ---------------------------------------------------------------- reports (admins)

GROUPABLE = {
    "state": ("State", "l.state"),
    "district": ("District", "l.district"),
    "place": ("Place", "l.place"),
    "country": ("Country", "l.country"),
    "status": ("Status", "l.status"),
    "possibility": ("Buying Possibility", "l.buying_possibility"),
    "quality": ("Lead Quality", "l.lead_quality"),
    "priority": ("Priority", "l.priority"),
    "source": ("Lead Source", "l.source"),
    "staff": ("Created By (Staff)", "u.name"),
    "assigned": ("Assigned To", "ua.name"),
    "project": ("Project / Campaign", "p.name"),
}


@app.route("/reports")
@admin_required
def reports():
    by = request.args.get("by", "state")
    if by not in GROUPABLE:
        by = "state"
    label, column = GROUPABLE[by]
    params = clean_params(exclude=("by",))
    clauses, args = lead_filters(params)
    id_col = {"staff": "MIN(l.entered_by)", "assigned": "MIN(l.assigned_to)", "project": "MIN(l.project_id)"}.get(by, "NULL")
    group_key = {"staff": "l.entered_by", "assigned": "l.assigned_to", "project": "l.project_id"}.get(by, "grp")
    groups = query(f"""
        SELECT COALESCE(NULLIF(TRIM({column}),''),'(not set)') grp, COUNT(*) total,
          SUM(l.lead_quality = 'Hot') hot, SUM(l.lead_quality = 'Warm') warm, SUM(l.lead_quality = 'Cold') cold,
          SUM(l.buying_possibility = 'High') high_possibility, SUM(l.status = 'Converted') converted,
          SUM(l.status IN ('Lost', 'Not Interested')) lost, {id_col} group_id
        {LEAD_FROM} WHERE {' AND '.join(clauses)} GROUP BY {group_key} ORDER BY total DESC, grp COLLATE NOCASE""", args)
    return render_template("reports.html", by=by, label=label, groups=groups, params=params,
                           projects_all=visible_projects(), grand_total=sum(r["total"] for r in groups))


# ---------------------------------------------------------------- staff & users (org admins)

def manageable_user(uid):
    """A user in the current organization that the current admin may manage."""
    user = query("SELECT * FROM users WHERE id = ? AND org_id = ?", (uid, org_id()), one=True) or abort(404)
    if user["role"] == SUPER_ADMIN and user["id"] != g.user["id"]:
        abort(403)
    return user


@app.route("/staff")
@admin_required
def staff():
    rows = query("""SELECT u.*, (SELECT COUNT(*) FROM leads l WHERE l.entered_by = u.id) lead_count,
                      (SELECT MAX(created_at) FROM password_tokens t WHERE t.user_id = u.id AND t.used_at IS NULL
                         AND t.expires_at > ?) pending_link
                    FROM users u WHERE u.org_id = ? ORDER BY u.active DESC, u.name COLLATE NOCASE""", (now(), org_id()))
    return render_template("staff.html", staff=rows)


def staff_form_data():
    data = {k: clean(request.form.get(k)) for k in ("name", "username", "phone", "email", "designation", "role")}
    data["role"] = ORG_ADMIN if data["role"] == ORG_ADMIN else STAFF
    data["active"] = 1 if request.form.get("active") else 0
    return data


def validate_user(data, uid=None):
    errors = []
    if not data["name"] or not data["username"]:
        errors.append("Name and username are required.")
    elif query("SELECT 1 FROM users WHERE username = ? AND id != ?", (data["username"], uid or 0), one=True):
        errors.append("That username is already taken.")
    if data["email"]:
        if "@" not in data["email"]:
            errors.append("Email address looks invalid.")
        elif query("SELECT 1 FROM users WHERE email = ? COLLATE NOCASE AND id != ?", (data["email"], uid or 0), one=True):
            errors.append("That email is already used by another account.")
    return errors


def create_user(org, data, onboarding, password):
    """Create an account. Returns (user_id, invite_link_or_None, expiry) or raises ValueError."""
    if onboarding == "password":
        problem = password_problem(password)
        if problem:
            raise ValueError(problem)
        pw_hash, must_change = generate_password_hash(password), 1
    else:
        # Unusable random password until the invite link is used.
        pw_hash, must_change = generate_password_hash(secrets.token_urlsafe(32)), 0
    uid = execute("""INSERT INTO users (org_id, name, username, phone, email, designation, password_hash, role, active,
                     must_change_password, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (org, data["name"], data["username"], data["phone"], data["email"], data["designation"], pw_hash,
                   data["role"], data["active"], must_change, now()))
    if onboarding == "password":
        return uid, None, None
    link, expires = issue_password_link(uid, "invite")
    return uid, link, expires


@app.route("/staff/new", methods=["GET", "POST"])
@admin_required
def staff_new():
    data = {"role": STAFF, "active": 1}
    if request.method == "POST":
        data = staff_form_data()
        errors = validate_user(data)
        for e in errors:
            flash(e, "error")
        if not errors:
            try:
                uid, link, expires = create_user(org_id(), data, request.form.get("onboarding"),
                                                 request.form.get("password") or "")
            except ValueError as e:
                flash(str(e), "error")
            else:
                if link:
                    return render_template("password_link.html", user=data, link=link, expires=expires,
                                           purpose="invite", back=url_for("staff"))
                flash(f"Account for {data['name']} created. They must change the initial password at first login.",
                      "success")
                return redirect(url_for("staff"))
    return render_template("staff_form.html", data=data, member=None)


@app.route("/staff/<int:uid>/edit", methods=["GET", "POST"])
@admin_required
def staff_edit(uid):
    member = manageable_user(uid)
    data = dict(member)
    if request.method == "POST":
        data = staff_form_data()
        if member["role"] == SUPER_ADMIN or uid == g.user["id"]:
            data["role"], data["active"] = member["role"], 1  # nobody can demote or lock themselves out
        errors = validate_user(data, uid)
        for e in errors:
            flash(e, "error")
        if not errors:
            bump = 1 if (not data["active"] and member["active"]) or data["role"] != member["role"] else 0
            execute("""UPDATE users SET name=?, username=?, phone=?, email=?, designation=?, role=?, active=?,
                       session_version = session_version + ? WHERE id = ? AND org_id = ?""",
                    (data["name"], data["username"], data["phone"], data["email"], data["designation"], data["role"],
                     data["active"], bump, uid, org_id()))
            flash("User updated.", "success")
            return redirect(url_for("staff"))
    return render_template("staff_form.html", data=data, member=member)


def password_action(user, back):
    """Handle an admin's reset request: a single-use link, or a temporary password the user must change."""
    if request.form.get("action") == "temp":
        password = request.form.get("temp_password") or ""
        problem = password_problem(password)
        if problem:
            flash(problem, "error")
            return redirect(back)
        set_password(user["id"], password, must_change=True)
        flash(f"Temporary password set for {user['name']}. They must change it at next login; "
              "all their existing sessions were signed out.", "success")
        return redirect(back)
    purpose = "invite" if request.form.get("action") == "invite" else "reset"
    link, expires = issue_password_link(user["id"], purpose)
    return render_template("password_link.html", user=user, link=link, expires=expires, purpose=purpose, back=back)


@app.route("/staff/<int:uid>/password", methods=["POST"])
@admin_required
def staff_password(uid):
    user = manageable_user(uid)
    if user["id"] == g.user["id"]:
        return redirect(url_for("account"))
    return password_action(user, url_for("staff"))


@app.route("/staff/<int:uid>/toggle", methods=["POST"])
@admin_required
def staff_toggle(uid):
    user = manageable_user(uid)
    if user["id"] == g.user["id"]:
        flash("You cannot deactivate your own account.", "error")
    else:
        execute("UPDATE users SET active = ?, session_version = session_version + 1 WHERE id = ? AND org_id = ?",
                (0 if user["active"] else 1, uid, org_id()))
        flash(f"{user['name']} {'deactivated' if user['active'] else 'activated'}.", "success")
    return redirect(url_for("staff"))


# ---------------------------------------------------------------- organization settings

@app.route("/organization", methods=["GET", "POST"])
@admin_required
def organization_settings():
    org = g.org
    if request.method == "POST":
        data = {k: clean(request.form.get(k)) for k in ("name", "contact_name", "phone", "email", "address")}
        if not data["name"]:
            flash("Organization name is required.", "error")
        else:
            execute("UPDATE organizations SET name=?, contact_name=?, phone=?, email=?, address=?, updated_at=?"
                    " WHERE id = ?", (*data.values(), now(), org_id()))
            flash("Organization settings saved.", "success")
            return redirect(url_for("organization_settings"))
    counts = dict(users=scalar("SELECT COUNT(*) FROM users WHERE org_id = ?", (org_id(),)),
                  projects=scalar("SELECT COUNT(*) FROM projects WHERE org_id = ?", (org_id(),)),
                  products=scalar("SELECT COUNT(*) FROM products WHERE org_id = ?", (org_id(),)),
                  leads=scalar("SELECT COUNT(*) FROM leads WHERE org_id = ?", (org_id(),)))
    access_log = query("""SELECT a.*, u.name user_name FROM audit_log a JOIN users u ON u.id = a.user_id
                          WHERE a.org_id = ? ORDER BY a.id DESC LIMIT 30""", (org_id(),))
    return render_template("organization.html", org=org, counts=counts, access_log=access_log)


# ---------------------------------------------------------------- super admin: organizations

def org_or_404(oid):
    return query("SELECT * FROM organizations WHERE id = ?", (oid,), one=True) or abort(404)


@app.route("/admin/organizations")
@super_required
def admin_orgs():
    rows = query("""SELECT o.*, (SELECT COUNT(*) FROM users u WHERE u.org_id = o.id) users,
                      (SELECT COUNT(*) FROM users u WHERE u.org_id = o.id AND u.active = 1) active_users,
                      (SELECT GROUP_CONCAT(u.name, ', ') FROM users u WHERE u.org_id = o.id
                         AND u.role IN ('org_admin', 'super_admin')) admins,
                      (SELECT COUNT(*) FROM leads l WHERE l.org_id = o.id) leads,
                      (SELECT COUNT(*) FROM projects p WHERE p.org_id = o.id) projects
                    FROM organizations o ORDER BY o.kind, o.name COLLATE NOCASE""")
    return render_template("admin_orgs.html", orgs=rows)


def org_form_data():
    data = {k: clean(request.form.get(k)) for k in ("name", "code", "contact_name", "phone", "email", "address", "notes")}
    data["code"] = "".join(ch for ch in data["code"].upper() if ch.isalnum() or ch in "-_")
    return data


@app.route("/admin/organizations/new", methods=["GET", "POST"])
@super_required
def admin_org_new():
    data, admin = {}, {"role": ORG_ADMIN, "active": 1}
    if request.method == "POST":
        data = org_form_data()
        admin = {k: clean(request.form.get("admin_" + k)) for k in ("name", "username", "email", "phone")}
        admin.update(role=ORG_ADMIN, active=1, designation="Administrator")
        errors = []
        if not data["name"] or not data["code"]:
            errors.append("Organization name and a short code are required.")
        elif query("SELECT 1 FROM organizations WHERE code = ?", (data["code"],), one=True):
            errors.append(f"Organization code {data['code']} is already used.")
        errors += validate_user(admin)
        if request.form.get("onboarding") == "password":
            problem = password_problem(request.form.get("password"))
            if problem:
                errors.append(problem)
        for e in errors:
            flash(e, "error")
        if not errors:
            ts = now()
            oid = execute("""INSERT INTO organizations (name, code, kind, status, contact_name, phone, email, address,
                             notes, created_by, created_at, updated_at) VALUES (?,?, 'client', 'Active', ?,?,?,?,?,?,?,?)""",
                          (data["name"], data["code"], data["contact_name"], data["phone"], data["email"],
                           data["address"], data["notes"], g.user["id"], ts, ts))
            uid, link, expires = create_user(oid, admin, request.form.get("onboarding"), request.form.get("password") or "")
            if link:
                return render_template("password_link.html", user=admin, link=link, expires=expires,
                                       purpose="invite", back=url_for("admin_orgs"), org=data)
            flash(f"Organization '{data['name']}' created with admin {admin['username']}. "
                  "They must change the initial password at first login.", "success")
            return redirect(url_for("admin_orgs"))
    return render_template("admin_org_form.html", data=data, admin=admin, org=None)


@app.route("/admin/organizations/<int:oid>/edit", methods=["GET", "POST"])
@super_required
def admin_org_edit(oid):
    org = org_or_404(oid)
    data = dict(org)
    if request.method == "POST":
        data = org_form_data()
        if not data["name"] or not data["code"]:
            flash("Organization name and code are required.", "error")
        elif query("SELECT 1 FROM organizations WHERE code = ? AND id != ?", (data["code"], oid), one=True):
            flash(f"Organization code {data['code']} is already used.", "error")
        else:
            execute("""UPDATE organizations SET name=?, code=?, contact_name=?, phone=?, email=?, address=?, notes=?,
                       updated_at=? WHERE id = ?""", (*data.values(), now(), oid))
            flash("Organization updated.", "success")
            return redirect(url_for("admin_orgs"))
    return render_template("admin_org_form.html", data=data, admin=None, org=org)


@app.route("/admin/organizations/<int:oid>/toggle", methods=["POST"])
@super_required
def admin_org_toggle(oid):
    org = org_or_404(oid)
    if org["kind"] == "agency":
        flash("The agency organization cannot be deactivated.", "error")
    else:
        new = "Inactive" if org["status"] == "Active" else "Active"
        execute("UPDATE organizations SET status = ?, updated_at = ? WHERE id = ?", (new, now(), oid))
        flash(f"'{org['name']}' is now {new}." + (" Its users can no longer log in." if new == "Inactive" else ""),
              "success")
    return redirect(url_for("admin_orgs"))


@app.route("/admin/organizations/<int:oid>/view", methods=["POST"])
@super_required
def admin_view_enter(oid):
    """Open a client's workspace read-only. Every visit is written to that client's access log."""
    org = org_or_404(oid)
    home = g.get("home_org") or g.org
    if org["id"] == home["id"]:
        return admin_view_exit()
    if session.get("view_org") != oid:
        if g.get("viewing"):
            audit("Super Admin closed the workspace", org=g.org["id"])
        session["view_org"] = oid
        g.org = org
        audit("Super Admin opened the workspace (read-only)", org=oid)
    return redirect(url_for("dashboard"))


@app.route("/admin/view/exit", methods=["POST"])
@super_required
def admin_view_exit():
    if g.get("viewing"):
        audit("Super Admin closed the workspace")
    session.pop("view_org", None)
    return redirect(url_for("admin_orgs"))


@app.route("/admin/organizations/<int:oid>/users")
@super_required
def admin_org_users(oid):
    """Account management only - Super Admin sees user accounts, not the client's leads."""
    org = org_or_404(oid)
    users = query("SELECT id, name, username, email, role, active, last_login_at, created_at FROM users"
                  " WHERE org_id = ? ORDER BY role, name COLLATE NOCASE", (oid,))
    return render_template("admin_org_users.html", org=org, users=users)


def super_target_user(oid, uid):
    user = query("SELECT * FROM users WHERE id = ? AND org_id = ?", (uid, oid), one=True) or abort(404)
    if user["id"] == g.user["id"]:
        abort(400)
    return user


@app.route("/admin/organizations/<int:oid>/users/<int:uid>/password", methods=["POST"])
@super_required
def admin_user_password(oid, uid):
    return password_action(super_target_user(oid, uid), url_for("admin_org_users", oid=oid))


@app.route("/admin/organizations/<int:oid>/users/<int:uid>/toggle", methods=["POST"])
@super_required
def admin_user_toggle(oid, uid):
    user = super_target_user(oid, uid)
    execute("UPDATE users SET active = ?, session_version = session_version + 1 WHERE id = ?",
            (0 if user["active"] else 1, uid))
    flash(f"{user['name']} {'deactivated' if user['active'] else 'activated'}.", "success")
    return redirect(url_for("admin_org_users", oid=oid))


@app.route("/admin/organizations/<int:oid>/admins/new", methods=["POST"])
@super_required
def admin_org_add_admin(oid):
    org = org_or_404(oid)
    admin = {k: clean(request.form.get("admin_" + k)) for k in ("name", "username", "email", "phone")}
    admin.update(role=ORG_ADMIN, active=1, designation="Administrator")
    errors = validate_user(admin)
    for e in errors:
        flash(e, "error")
    if errors:
        return redirect(url_for("admin_org_users", oid=oid))
    _, link, expires = create_user(oid, admin, "invite", "")
    return render_template("password_link.html", user=admin, link=link, expires=expires, purpose="invite",
                           back=url_for("admin_org_users", oid=oid), org=org)


# ---------------------------------------------------------------- public website enquiries

_enquiry_hits = {}


@app.route("/enquiry", methods=["POST", "OPTIONS"])
def website_enquiry():
    """Save a website contact-form submission as a lead in the agency workspace.

    Only accepted from the origins in CRM_ENQUIRY_ORIGINS. No session or CSRF token is involved:
    the lead's organization, project and creator are fixed server-side, never taken from the form.
    """
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if not ENQUIRY_ORIGINS or origin not in ENQUIRY_ORIGINS:
        abort(403)
    headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    if request.method == "OPTIONS":
        return "", 204, dict(headers, **{"Access-Control-Allow-Methods": "POST"})

    now_ts = time.time()
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    hits = [t for t in _enquiry_hits.get(ip, []) if now_ts - t < 3600]
    if len(hits) >= 10:
        return {"ok": False, "error": "rate_limited"}, 429, headers
    _enquiry_hits[ip] = hits + [now_ts]

    f = request.form
    if f.get("website"):  # honeypot field humans never see
        return {"ok": True}, 200, headers
    name, phone = clean(f.get("name"))[:120], clean(f.get("phone"))[:40]
    business, service = clean(f.get("business"))[:120], clean(f.get("service"))[:120]
    message = clean(f.get("message"))[:2000]
    if not name or len(phone_key(phone)) < 6:
        return {"ok": False, "error": "name_and_phone_required"}, 400, headers

    db = get_db()
    org = db.execute("SELECT id FROM organizations WHERE kind = 'agency' ORDER BY id LIMIT 1").fetchone()
    owner = org and db.execute("SELECT id FROM users WHERE org_id = ? AND role = ? AND active = 1 ORDER BY id LIMIT 1",
                               (org[0], SUPER_ADMIN)).fetchone()
    if not owner:
        return {"ok": False, "error": "not_set_up"}, 503, headers
    oid, uid, ts = org[0], owner[0], now()
    project = db.execute("SELECT id FROM projects WHERE org_id = ? AND code = 'WEB'", (oid,)).fetchone()
    pid = project[0] if project else db.execute(
        "INSERT INTO projects (org_id, name, code, campaign_type, description, status, created_by, created_at,"
        " updated_at) VALUES (?, 'Website Enquiries', 'WEB', 'Lead Generation',"
        " 'Leads from the website contact form', 'Active', ?, ?, ?)", (oid, uid, ts, ts)).lastrowid
    requirement = " · ".join(x for x in (service, business and f"Business: {business}") if x)
    lead_id = db.execute(
        "INSERT INTO leads (org_id, project_id, customer_name, phone, whatsapp, phone_norm, requirement, notes,"
        " status, priority, source, country, entered_by, assigned_to, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?, 'New', 'High', 'Website', 'India', ?,?,?,?)",
        (oid, pid, name, phone, phone, phone_key(phone), requirement, message, uid, uid, ts, ts)).lastrowid
    product = db.execute("SELECT id FROM products WHERE org_id = ? AND active = 1 AND name = ? COLLATE NOCASE",
                         (oid, service)).fetchone() if service else None
    if product:
        db.execute("INSERT INTO lead_products (lead_id, product_id, org_id, stage, created_at, updated_at)"
                   " VALUES (?,?,?, 'Interested', ?, ?)", (lead_id, product[0], oid, ts, ts))
    db.execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at) VALUES (?,?,?,?)",
               (lead_id, uid, "Enquiry received from the website contact form", ts))
    db.commit()
    return {"ok": True}, 201, headers


# ---------------------------------------------------------------- errors

@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, message=getattr(e, "description", "Bad request.")), 400


@app.errorhandler(403)
def forbidden(e):
    desc = getattr(e, "description", "") or ""
    message = desc if desc.startswith("Read-only") else "You don't have permission to do that."
    return render_template("error.html", code=403, message=message), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="Page not found."), 404


@app.errorhandler(sqlite3.IntegrityError)
def integrity_error(e):
    app.logger.warning("Integrity error: %s", e)
    return render_template("error.html", code=409, message="That change was rejected to protect data integrity."), 409


crm_db.migrate(DB_PATH)

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
