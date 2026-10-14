"""Agency CRM - lead management for marketing agencies.

Run:  python app.py   (then open http://localhost:5000)
"""
import csv
import io
import os
import secrets
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.environ.get("CRM_DB_PATH", os.path.join(INSTANCE_DIR, "crm.db"))

# ---------------------------------------------------------------- options

LEAD_STATUSES = ["New", "Contacted", "Interested", "Follow-up", "Negotiation",
                 "Converted", "Not Interested", "Lost"]
BUYING_POSSIBILITIES = ["High", "Medium", "Low"]
LEAD_QUALITIES = ["Hot", "Warm", "Cold"]
LEAD_SOURCES = ["Facebook", "Instagram", "Google Ads", "Website", "WhatsApp",
                "Referral", "Walk-in", "Phone Call", "Other"]

# Columns the lead list can be sorted by (whitelist -> SQL expression).
SORTABLE = {
    "created": "l.created_at",
    "name": "l.customer_name COLLATE NOCASE",
    "phone": "l.phone",
    "email": "l.email COLLATE NOCASE",
    "place": "l.place COLLATE NOCASE",
    "state": "l.state COLLATE NOCASE",
    "requirement": "l.requirement COLLATE NOCASE",
    "status": "l.status",
    "possibility": ("CASE l.buying_possibility WHEN 'High' THEN 1 "
                    "WHEN 'Medium' THEN 2 WHEN 'Low' THEN 3 ELSE 4 END"),
    "quality": ("CASE l.lead_quality WHEN 'Hot' THEN 1 WHEN 'Warm' THEN 2 "
                "WHEN 'Cold' THEN 3 ELSE 4 END"),
    "staff": "u.name COLLATE NOCASE",
    "project": "p.name COLLATE NOCASE",
}

# Fields that can be used for "group by" reports.
GROUPABLE = {
    "requirement": ("Requirement / Product", "l.requirement"),
    "place": ("Place", "l.place"),
    "state": ("State", "l.state"),
    "status": ("Status", "l.status"),
    "possibility": ("Buying Possibility", "l.buying_possibility"),
    "quality": ("Lead Quality", "l.lead_quality"),
    "source": ("Lead Source", "l.source"),
    "staff": ("Entered By (Staff)", "u.name"),
    "project": ("Project / Client", "p.name"),
}

# Lead filters accepted from the query string -> SQL column.
FILTERS = {
    "project": "l.project_id",
    "status": "l.status",
    "possibility": "l.buying_possibility",
    "quality": "l.lead_quality",
    "state": "l.state",
    "place": "l.place",
    "requirement": "l.requirement",
    "source": "l.source",
    "staff": "l.entered_by",
}

LEAD_FIELDS = ["customer_name", "phone", "whatsapp", "email", "place", "state",
               "requirement", "status", "buying_possibility", "lead_quality",
               "source", "notes"]

# Header aliases accepted by the CSV import.
CSV_ALIASES = {
    "customer_name": ["customer_name", "name", "customer", "customer name", "full name", "full_name"],
    "phone": ["phone", "phone number", "mobile", "phone_number", "contact"],
    "whatsapp": ["whatsapp", "whatsapp number", "whatsapp_number"],
    "email": ["email", "email address", "e-mail"],
    "place": ["place", "city", "location", "town"],
    "state": ["state", "region"],
    "requirement": ["requirement", "product", "requirements", "product details", "interest"],
    "status": ["status"],
    "buying_possibility": ["buying_possibility", "buying possibility", "possibility"],
    "lead_quality": ["lead_quality", "lead quality", "quality"],
    "source": ["source", "lead source"],
    "notes": ["notes", "remarks", "comment", "comments"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone TEXT,
    email TEXT,
    designation TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'staff',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    client_name TEXT,
    client_phone TEXT,
    client_email TEXT,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'Active',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    customer_name TEXT NOT NULL,
    phone TEXT,
    whatsapp TEXT,
    email TEXT,
    place TEXT,
    state TEXT,
    requirement TEXT,
    status TEXT NOT NULL DEFAULT 'New',
    buying_possibility TEXT,
    lead_quality TEXT,
    source TEXT,
    notes TEXT,
    entered_by INTEGER NOT NULL REFERENCES users(id),
    updated_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lead_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_project ON leads(project_id);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
CREATE INDEX IF NOT EXISTS idx_activity_lead ON lead_activity(lead_id);
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
app.config["SECRET_KEY"] = load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB CSV uploads


# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def log_activity(lead_id, action):
    execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at) VALUES (?,?,?,?)",
            (lead_id, g.user["id"], action, now()))


# ---------------------------------------------------------------- auth / csrf

@app.before_request
def load_user():
    g.user = None
    uid = session.get("user_id")
    if uid:
        user = query("SELECT * FROM users WHERE id = ? AND active = 1", (uid,), one=True)
        if user is None:
            session.clear()
        g.user = user

    if request.method == "POST":
        token = session.get("csrf_token")
        if not token or token != request.form.get("csrf_token"):
            abort(400, "Invalid form token. Please reload the page and try again.")


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if query("SELECT COUNT(*) c FROM users", one=True)["c"] == 0:
                return redirect(url_for("setup"))
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    ctx = dict(LEAD_STATUSES=LEAD_STATUSES, BUYING_POSSIBILITIES=BUYING_POSSIBILITIES,
               LEAD_QUALITIES=LEAD_QUALITIES, LEAD_SOURCES=LEAD_SOURCES, GROUPABLE=GROUPABLE)
    if g.get("user"):
        ctx["nav_projects"] = query(
            "SELECT id, name FROM projects WHERE status = 'Active' ORDER BY name COLLATE NOCASE")
    return ctx


@app.template_filter("badge")
def badge_class(value):
    return "b-" + (value or "none").lower().replace(" ", "-")


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First run: create the admin account."""
    if query("SELECT COUNT(*) c FROM users", one=True)["c"] > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        name, username = clean(request.form.get("name")), clean(request.form.get("username"))
        password = request.form.get("password") or ""
        if not name or not username or len(password) < 6:
            flash("Name, username and a password of at least 6 characters are required.", "error")
        else:
            uid = execute(
                "INSERT INTO users (name, username, phone, email, designation, password_hash, role, created_at)"
                " VALUES (?,?,?,?,?,?, 'admin', ?)",
                (name, username, clean(request.form.get("phone")), clean(request.form.get("email")),
                 "Administrator", generate_password_hash(password), now()))
            session.clear()
            session["user_id"] = uid
            flash("Admin account created. Start by adding a project (client).", "success")
            return redirect(url_for("projects"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if query("SELECT COUNT(*) c FROM users", one=True)["c"] == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        user = query("SELECT * FROM users WHERE username = ?",
                     (clean(request.form.get("username")),), one=True)
        if user and user["active"] and check_password_hash(user["password_hash"],
                                                           request.form.get("password") or ""):
            session.clear()
            session["user_id"] = user["id"]
            nxt = request.args.get("next") or ""
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("dashboard")
            return redirect(nxt)
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        if not check_password_hash(g.user["password_hash"], current):
            flash("Current password is incorrect.", "error")
        elif len(new) < 6:
            flash("New password must be at least 6 characters.", "error")
        else:
            execute("UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new), g.user["id"]))
            flash("Password changed.", "success")
            return redirect(url_for("dashboard"))
    return render_template("account.html")


# ---------------------------------------------------------------- dashboard

def count_by(column, where="", args=()):
    return query(f"SELECT COALESCE(NULLIF(TRIM({column}),''),'(not set)') k, COUNT(*) c "
                 f"FROM leads l {where} GROUP BY k ORDER BY c DESC", args)


@app.route("/")
@login_required
def dashboard():
    project_id = request.args.get("project", type=int)
    where, args = ("WHERE l.project_id = ?", (project_id,)) if project_id else ("", ())
    project = query("SELECT * FROM projects WHERE id = ?", (project_id,), one=True) if project_id else None
    total = query(f"SELECT COUNT(*) c FROM leads l {where}", args, one=True)["c"]
    today = query(f"SELECT COUNT(*) c FROM leads l {where} {'AND' if where else 'WHERE'} "
                  "date(l.created_at) = date('now','localtime')", args, one=True)["c"]
    converted = query(f"SELECT COUNT(*) c FROM leads l {where} {'AND' if where else 'WHERE'} "
                      "l.status = 'Converted'", args, one=True)["c"]
    hot = query(f"SELECT COUNT(*) c FROM leads l {where} {'AND' if where else 'WHERE'} "
                "l.lead_quality = 'Hot'", args, one=True)["c"]
    recent = query(f"""SELECT l.*, p.name project_name, u.name staff_name FROM leads l
                      JOIN projects p ON p.id = l.project_id JOIN users u ON u.id = l.entered_by
                      {where} ORDER BY l.created_at DESC, l.id DESC LIMIT 8""", args)
    project_stats = query("""SELECT p.id, p.name, p.client_name, COUNT(l.id) total,
                               SUM(l.status = 'Converted') converted, SUM(l.lead_quality = 'Hot') hot
                             FROM projects p LEFT JOIN leads l ON l.project_id = p.id
                             WHERE p.status = 'Active' GROUP BY p.id ORDER BY total DESC""")
    staff_stats = query(f"""SELECT u.id, u.name, COUNT(l.id) total FROM leads l
                           JOIN users u ON u.id = l.entered_by {where}
                           GROUP BY u.id ORDER BY total DESC""", args)
    return render_template(
        "dashboard.html", project=project, total=total, today=today, converted=converted, hot=hot,
        recent=recent, project_stats=project_stats, staff_stats=staff_stats,
        by_status=count_by("l.status", where, args), by_quality=count_by("l.lead_quality", where, args),
        by_possibility=count_by("l.buying_possibility", where, args),
        by_requirement=count_by("l.requirement", where, args)[:8])


# ---------------------------------------------------------------- projects

@app.route("/projects")
@login_required
def projects():
    rows = query("""SELECT p.*, u.name creator, COUNT(l.id) lead_count
                    FROM projects p LEFT JOIN users u ON u.id = p.created_by
                    LEFT JOIN leads l ON l.project_id = p.id
                    GROUP BY p.id ORDER BY p.status, p.name COLLATE NOCASE""")
    return render_template("projects.html", projects=rows)


def project_form_data():
    return {k: clean(request.form.get(k)) for k in
            ("name", "client_name", "client_phone", "client_email", "description", "status")}


@app.route("/projects/new", methods=["GET", "POST"])
@admin_required
def project_new():
    data = {"status": "Active"}
    if request.method == "POST":
        data = project_form_data()
        if not data["name"]:
            flash("Project name is required.", "error")
        else:
            execute("""INSERT INTO projects (name, client_name, client_phone, client_email, description,
                       status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (data["name"], data["client_name"], data["client_phone"], data["client_email"],
                     data["description"], data["status"] or "Active", g.user["id"], now()))
            flash(f"Project '{data['name']}' created.", "success")
            return redirect(url_for("projects"))
    return render_template("project_form.html", data=data, project=None)


@app.route("/projects/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def project_edit(pid):
    project = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True) or abort(404)
    data = dict(project)
    if request.method == "POST":
        data = project_form_data()
        if not data["name"]:
            flash("Project name is required.", "error")
        else:
            execute("""UPDATE projects SET name=?, client_name=?, client_phone=?, client_email=?,
                       description=?, status=? WHERE id=?""",
                    (data["name"], data["client_name"], data["client_phone"], data["client_email"],
                     data["description"], data["status"] or "Active", pid))
            flash("Project updated.", "success")
            return redirect(url_for("projects"))
    return render_template("project_form.html", data=data, project=project)


@app.route("/projects/<int:pid>/delete", methods=["POST"])
@admin_required
def project_delete(pid):
    project = query("SELECT * FROM projects WHERE id = ?", (pid,), one=True) or abort(404)
    execute("DELETE FROM projects WHERE id = ?", (pid,))
    flash(f"Project '{project['name']}' and all its leads were deleted.", "success")
    return redirect(url_for("projects"))


# ---------------------------------------------------------------- leads

def build_lead_query(params):
    """Return (where_sql, args) from filter/search params."""
    clauses, args = [], []
    for key, column in FILTERS.items():
        value = params.get(key, "")
        if value == "":
            continue
        if value == "(not set)":
            clauses.append(f"({column} IS NULL OR TRIM({column}) = '')")
        else:
            clauses.append(f"{column} = ?" if key in ("project", "staff") else f"{column} = ? COLLATE NOCASE")
            args.append(value)
    q = params.get("q", "").strip()
    if q:
        like = f"%{q}%"
        clauses.append("(l.customer_name LIKE ? OR l.phone LIKE ? OR l.whatsapp LIKE ? OR l.email LIKE ?"
                       " OR l.place LIKE ? OR l.state LIKE ? OR l.requirement LIKE ? OR l.notes LIKE ?)")
        args.extend([like] * 8)
    if params.get("date_from"):
        clauses.append("date(l.created_at) >= date(?)")
        args.append(params["date_from"])
    if params.get("date_to"):
        clauses.append("date(l.created_at) <= date(?)")
        args.append(params["date_to"])
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", args


LEAD_SELECT = """SELECT l.*, p.name project_name, u.name staff_name, uu.name updated_by_name
                 FROM leads l JOIN projects p ON p.id = l.project_id
                 JOIN users u ON u.id = l.entered_by LEFT JOIN users uu ON uu.id = l.updated_by"""


def filtered_leads(params):
    where, args = build_lead_query(params)
    sort = params.get("sort") if params.get("sort") in SORTABLE else "created"
    direction = "ASC" if params.get("dir") == "asc" else "DESC"
    rows = query(f"{LEAD_SELECT} {where} ORDER BY {SORTABLE[sort]} {direction}, l.id DESC", args)
    return rows, sort, direction.lower()


def distinct_values(column, project_id=None):
    where = "AND project_id = ?" if project_id else ""
    args = (project_id,) if project_id else ()
    return [r["v"] for r in query(
        f"SELECT DISTINCT TRIM({column}) v FROM leads WHERE TRIM(COALESCE({column},'')) != '' {where} "
        f"ORDER BY v COLLATE NOCASE", args)]


@app.route("/leads")
@login_required
def leads():
    params = request.args.to_dict()
    rows, sort, direction = filtered_leads(params)
    staff = query("SELECT id, name FROM users ORDER BY name COLLATE NOCASE")
    projects_all = query("SELECT id, name FROM projects ORDER BY name COLLATE NOCASE")
    project = None
    if params.get("project", "").isdigit():
        project = query("SELECT * FROM projects WHERE id = ?", (int(params["project"]),), one=True)
    return render_template(
        "leads.html", leads=rows, params=params, sort=sort, direction=direction, staff=staff,
        projects_all=projects_all, project=project,
        states=distinct_values("state"), places=distinct_values("place"),
        requirements=distinct_values("requirement"))


@app.route("/leads/export")
@login_required
def leads_export():
    rows, _, _ = filtered_leads(request.args.to_dict())
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ID", "Project", "Customer Name", "Phone", "WhatsApp", "Email", "Place", "State",
                     "Requirement", "Status", "Buying Possibility", "Lead Quality", "Source", "Notes",
                     "Entered By", "Created At", "Updated At"])
    for r in rows:
        writer.writerow([r["id"], r["project_name"], r["customer_name"], r["phone"], r["whatsapp"],
                         r["email"], r["place"], r["state"], r["requirement"], r["status"],
                         r["buying_possibility"], r["lead_quality"], r["source"], r["notes"],
                         r["staff_name"], r["created_at"], r["updated_at"]])
    filename = f"leads-{datetime.now():%Y%m%d-%H%M}.csv"
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def lead_form_data():
    data = {k: clean(request.form.get(k)) for k in LEAD_FIELDS}
    data["project_id"] = request.form.get("project_id", type=int)
    return data


def validate_lead(data):
    errors = []
    if not data["project_id"] or not query("SELECT 1 FROM projects WHERE id = ?", (data["project_id"],), one=True):
        errors.append("Please choose a project.")
    if not data["customer_name"]:
        errors.append("Customer name is required.")
    if not data["phone"] and not data["whatsapp"] and not data["email"]:
        errors.append("Enter at least one contact: phone, WhatsApp or email.")
    if data["email"] and "@" not in data["email"]:
        errors.append("Email address looks invalid.")
    if data["status"] not in LEAD_STATUSES:
        data["status"] = "New"
    if data["buying_possibility"] not in BUYING_POSSIBILITIES:
        data["buying_possibility"] = ""
    if data["lead_quality"] not in LEAD_QUALITIES:
        data["lead_quality"] = ""
    return errors


def find_duplicates(data, exclude_id=None):
    """Leads in the same project sharing phone/whatsapp/email."""
    conds, args = [], [data["project_id"]]
    for field in ("phone", "whatsapp"):
        if data[field]:
            conds.append("l.phone = ? OR l.whatsapp = ?")
            args.extend([data[field], data[field]])
    if data["email"]:
        conds.append("l.email = ? COLLATE NOCASE")
        args.append(data["email"])
    if not conds:
        return []
    sql = f"{LEAD_SELECT} WHERE l.project_id = ? AND ({' OR '.join(conds)})"
    if exclude_id:
        sql += " AND l.id != ?"
        args.append(exclude_id)
    return query(sql, args)


def lead_form_context(data, lead=None, duplicates=None):
    pid = data.get("project_id")
    return dict(data=data, lead=lead, duplicates=duplicates or [],
                projects_all=query("SELECT id, name, client_name FROM projects WHERE status = 'Active' "
                                   "OR id = ? ORDER BY name COLLATE NOCASE", (pid or 0,)),
                requirements=distinct_values("requirement", pid), places=distinct_values("place"),
                states=distinct_values("state"))


@app.route("/leads/new", methods=["GET", "POST"])
@login_required
def lead_new():
    data = {k: "" for k in LEAD_FIELDS}
    data.update(status="New", project_id=request.args.get("project", type=int))
    if request.method == "POST":
        data = lead_form_data()
        errors = validate_lead(data)
        dups = [] if errors else find_duplicates(data)
        if dups and not request.form.get("confirm_duplicate"):
            flash("A lead with the same phone / WhatsApp / email already exists in this project. "
                  "Review below, then tick 'Save anyway' if this is a new enquiry.", "warning")
            return render_template("lead_form.html", **lead_form_context(data, duplicates=dups))
        for e in errors:
            flash(e, "error")
        if not errors:
            ts = now()
            lead_id = execute(
                f"INSERT INTO leads (project_id, {', '.join(LEAD_FIELDS)}, entered_by, created_at, updated_at)"
                f" VALUES (?, {', '.join('?' * len(LEAD_FIELDS))}, ?, ?, ?)",
                (data["project_id"], *[data[k] for k in LEAD_FIELDS], g.user["id"], ts, ts))
            log_activity(lead_id, "Lead created")
            flash(f"Lead '{data['customer_name']}' saved. Entered by {g.user['name']}.", "success")
            if request.form.get("save_and_new"):
                return redirect(url_for("lead_new", project=data["project_id"]))
            return redirect(url_for("lead_view", lid=lead_id))
    return render_template("lead_form.html", **lead_form_context(data))


@app.route("/leads/<int:lid>")
@login_required
def lead_view(lid):
    lead = query(f"{LEAD_SELECT} WHERE l.id = ?", (lid,), one=True) or abort(404)
    activity = query("""SELECT a.*, u.name user_name FROM lead_activity a JOIN users u ON u.id = a.user_id
                        WHERE a.lead_id = ? ORDER BY a.id DESC""", (lid,))
    return render_template("lead_view.html", lead=lead, activity=activity)


@app.route("/leads/<int:lid>/edit", methods=["GET", "POST"])
@login_required
def lead_edit(lid):
    lead = query(f"{LEAD_SELECT} WHERE l.id = ?", (lid,), one=True) or abort(404)
    data = dict(lead)
    if request.method == "POST":
        data = lead_form_data()
        errors = validate_lead(data)
        for e in errors:
            flash(e, "error")
        if not errors:
            changes = [f"{k.replace('_', ' ')}: '{lead[k] or ''}' → '{data[k]}'"
                       for k in LEAD_FIELDS if k != "notes" and (lead[k] or "") != data[k]]
            if lead["notes"] != data["notes"] and (lead["notes"] or data["notes"]):
                changes.append("notes updated")
            if lead["project_id"] != data["project_id"]:
                changes.append("moved to another project")
            execute(f"UPDATE leads SET project_id = ?, {', '.join(k + ' = ?' for k in LEAD_FIELDS)},"
                    " updated_by = ?, updated_at = ? WHERE id = ?",
                    (data["project_id"], *[data[k] for k in LEAD_FIELDS], g.user["id"], now(), lid))
            if changes:
                log_activity(lid, "Updated " + "; ".join(changes))
            flash("Lead updated.", "success")
            return redirect(url_for("lead_view", lid=lid))
    return render_template("lead_form.html", **lead_form_context(data, lead=lead))


@app.route("/leads/<int:lid>/status", methods=["POST"])
@login_required
def lead_quick_status(lid):
    lead = query("SELECT * FROM leads WHERE id = ?", (lid,), one=True) or abort(404)
    status = request.form.get("status")
    if status in LEAD_STATUSES and status != lead["status"]:
        execute("UPDATE leads SET status = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                (status, g.user["id"], now(), lid))
        log_activity(lid, f"Status: '{lead['status']}' → '{status}'")
    note = clean(request.form.get("note"))
    if note:
        log_activity(lid, f"Note: {note}")
    return redirect(url_for("lead_view", lid=lid))


@app.route("/leads/<int:lid>/delete", methods=["POST"])
@admin_required
def lead_delete(lid):
    lead = query("SELECT * FROM leads WHERE id = ?", (lid,), one=True) or abort(404)
    execute("DELETE FROM leads WHERE id = ?", (lid,))
    flash(f"Lead '{lead['customer_name']}' deleted.", "success")
    return redirect(url_for("leads", project=lead["project_id"]))


@app.route("/leads/import", methods=["GET", "POST"])
@login_required
def leads_import():
    projects_all = query("SELECT id, name FROM projects WHERE status = 'Active' ORDER BY name COLLATE NOCASE")
    if request.method == "POST":
        pid = request.form.get("project_id", type=int)
        upload = request.files.get("file")
        if not pid or not query("SELECT 1 FROM projects WHERE id = ?", (pid,), one=True):
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
                imported, skipped, ts = 0, 0, now()
                db = get_db()
                for row in reader:
                    data = {f: clean(row.get(mapping[f])) if f in mapping else "" for f in LEAD_FIELDS}
                    data["project_id"] = pid
                    if validate_lead(data):
                        skipped += 1
                        continue
                    cur = db.execute(
                        f"INSERT INTO leads (project_id, {', '.join(LEAD_FIELDS)}, entered_by, created_at,"
                        f" updated_at) VALUES (?, {', '.join('?' * len(LEAD_FIELDS))}, ?, ?, ?)",
                        (pid, *[data[k] for k in LEAD_FIELDS], g.user["id"], ts, ts))
                    db.execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at)"
                               " VALUES (?,?,?,?)", (cur.lastrowid, g.user["id"], "Imported from CSV", ts))
                    imported += 1
                db.commit()
                flash(f"Imported {imported} lead(s)." + (f" Skipped {skipped} row(s) missing a name or "
                                                         "contact." if skipped else ""), "success")
                return redirect(url_for("leads", project=pid))
    return render_template("import.html", projects_all=projects_all)


# ---------------------------------------------------------------- reports

@app.route("/reports")
@login_required
def reports():
    by = request.args.get("by", "requirement")
    if by not in GROUPABLE:
        by = "requirement"
    label, column = GROUPABLE[by]
    params = {k: v for k, v in request.args.items() if k != "by"}
    where, args = build_lead_query(params)
    groups = query(f"""
        SELECT COALESCE(NULLIF(TRIM({column}),''),'(not set)') grp, COUNT(*) total,
          SUM(l.lead_quality = 'Hot') hot, SUM(l.lead_quality = 'Warm') warm,
          SUM(l.lead_quality = 'Cold') cold,
          SUM(l.buying_possibility = 'High') high_possibility,
          SUM(l.status = 'Converted') converted,
          MIN(l.entered_by) staff_id, MIN(l.project_id) project_id
        FROM leads l JOIN projects p ON p.id = l.project_id JOIN users u ON u.id = l.entered_by
        {where} GROUP BY grp ORDER BY total DESC, grp COLLATE NOCASE""", args)
    projects_all = query("SELECT id, name FROM projects ORDER BY name COLLATE NOCASE")
    return render_template("reports.html", by=by, label=label, groups=groups, params=params,
                           projects_all=projects_all, grand_total=sum(r["total"] for r in groups))


# ---------------------------------------------------------------- staff

@app.route("/staff")
@admin_required
def staff():
    rows = query("""SELECT u.*, COUNT(l.id) lead_count FROM users u
                    LEFT JOIN leads l ON l.entered_by = u.id GROUP BY u.id ORDER BY u.active DESC, u.name""")
    return render_template("staff.html", staff=rows)


def staff_form_data():
    data = {k: clean(request.form.get(k)) for k in ("name", "username", "phone", "email", "designation", "role")}
    data["role"] = "admin" if data["role"] == "admin" else "staff"
    data["active"] = 1 if request.form.get("active") else 0
    return data


@app.route("/staff/new", methods=["GET", "POST"])
@admin_required
def staff_new():
    data = {"role": "staff", "active": 1}
    if request.method == "POST":
        data = staff_form_data()
        password = request.form.get("password") or ""
        if not data["name"] or not data["username"]:
            flash("Name and username are required.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        elif query("SELECT 1 FROM users WHERE username = ?", (data["username"],), one=True):
            flash("That username is already taken.", "error")
        else:
            execute("""INSERT INTO users (name, username, phone, email, designation, password_hash, role,
                       active, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (data["name"], data["username"], data["phone"], data["email"], data["designation"],
                     generate_password_hash(password), data["role"], data["active"], now()))
            flash(f"Staff member '{data['name']}' added.", "success")
            return redirect(url_for("staff"))
    return render_template("staff_form.html", data=data, member=None)


@app.route("/staff/<int:uid>/edit", methods=["GET", "POST"])
@admin_required
def staff_edit(uid):
    member = query("SELECT * FROM users WHERE id = ?", (uid,), one=True) or abort(404)
    data = dict(member)
    if request.method == "POST":
        data = staff_form_data()
        password = request.form.get("password") or ""
        if uid == g.user["id"]:
            data["role"], data["active"] = "admin", 1  # can't lock yourself out
        if not data["name"] or not data["username"]:
            flash("Name and username are required.", "error")
        elif password and len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        elif query("SELECT 1 FROM users WHERE username = ? AND id != ?", (data["username"], uid), one=True):
            flash("That username is already taken.", "error")
        else:
            execute("""UPDATE users SET name=?, username=?, phone=?, email=?, designation=?, role=?, active=?
                       WHERE id=?""", (data["name"], data["username"], data["phone"], data["email"],
                                       data["designation"], data["role"], data["active"], uid))
            if password:
                execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), uid))
            flash("Staff member updated.", "success")
            return redirect(url_for("staff"))
    return render_template("staff_form.html", data=data, member=member)


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403, message="Only an admin can do that."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="Page not found."), 404


init_db()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
