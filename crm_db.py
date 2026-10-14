"""Database schema and versioned, backward-compatible migrations.

The schema version lives in SQLite's PRAGMA user_version:
  0 = original single-workspace CRM
  1 = multi-tenant organizations, products, follow-ups, password tokens
Before migrating a database that already holds data, a full backup is written to
instance/backups/. Migrations only ADD tables/columns; no existing data is dropped.
"""
import os
import sqlite3
from datetime import datetime

from crm_constants import DEFAULT_COUNTRY, ORG_ADMIN, SUPER_ADMIN, normalize_location, phone_key

SCHEMA_VERSION = 1

# Original (v0) schema. Kept so a brand-new database goes through the same migration path.
SCHEMA_V0 = """
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

SCHEMA_V1_TABLES = """
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL DEFAULT 'client' CHECK (kind IN ('agency', 'client')),
    status TEXT NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Inactive')),
    contact_name TEXT,
    phone TEXT,
    email TEXT,
    address TEXT,
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    code TEXT,
    category TEXT,
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS lead_products (
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    org_id INTEGER NOT NULL REFERENCES organizations(id),
    stage TEXT NOT NULL DEFAULT 'Interested'
        CHECK (stage IN ('Interested', 'Ordered', 'Purchased', 'Dropped')),
    created_at TEXT NOT NULL,
    updated_at TEXT,
    updated_by INTEGER REFERENCES users(id),
    PRIMARY KEY (lead_id, product_id)
);
CREATE TABLE IF NOT EXISTS project_staff (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, user_id)
);
CREATE TABLE IF NOT EXISTS project_products (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, product_id)
);
CREATE TABLE IF NOT EXISTS follow_ups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES organizations(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    due_date TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Done', 'Cancelled')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    completed_by INTEGER REFERENCES users(id),
    completed_at TEXT,
    outcome TEXT
);
CREATE TABLE IF NOT EXISTS password_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL CHECK (purpose IN ('invite', 'reset')),
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
"""

SCHEMA_V1_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_users_org ON users(org_id);
CREATE INDEX IF NOT EXISTS idx_projects_org ON projects(org_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_org_code ON projects(org_id, code COLLATE NOCASE)
    WHERE code IS NOT NULL AND code != '';
CREATE INDEX IF NOT EXISTS idx_leads_org_creator ON leads(org_id, entered_by);
CREATE INDEX IF NOT EXISTS idx_leads_org_assigned ON leads(org_id, assigned_to);
CREATE INDEX IF NOT EXISTS idx_leads_org_state ON leads(org_id, state);
CREATE INDEX IF NOT EXISTS idx_leads_org_phone ON leads(org_id, phone_norm);
CREATE INDEX IF NOT EXISTS idx_products_org ON products(org_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_products_org_code ON products(org_id, code COLLATE NOCASE)
    WHERE code IS NOT NULL AND code != '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_products_org_name ON products(org_id, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_lead_products_product ON lead_products(product_id);
CREATE INDEX IF NOT EXISTS idx_lead_products_org ON lead_products(org_id);
CREATE INDEX IF NOT EXISTS idx_follow_ups_org ON follow_ups(org_id, status, due_date);
CREATE INDEX IF NOT EXISTS idx_follow_ups_lead ON follow_ups(lead_id);
CREATE INDEX IF NOT EXISTS idx_password_tokens_user ON password_tokens(user_id);
"""

# Database-level tenant guards (SQLite has no row-level security, so triggers make it
# impossible to write a row that points at another organization's data, even if a bug
# in application code tried to).
TENANT_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_users_org_required BEFORE INSERT ON users
WHEN NEW.org_id IS NULL OR NOT EXISTS (SELECT 1 FROM organizations WHERE id = NEW.org_id)
BEGIN SELECT RAISE(ABORT, 'tenant: user must belong to an organization'); END;

CREATE TRIGGER IF NOT EXISTS trg_users_org_update BEFORE UPDATE OF org_id ON users
WHEN NEW.org_id IS NULL OR NEW.org_id IS NOT OLD.org_id
BEGIN SELECT RAISE(ABORT, 'tenant: a user cannot move between organizations'); END;

CREATE TRIGGER IF NOT EXISTS trg_projects_org_required BEFORE INSERT ON projects
WHEN NEW.org_id IS NULL
BEGIN SELECT RAISE(ABORT, 'tenant: project must belong to an organization'); END;

CREATE TRIGGER IF NOT EXISTS trg_projects_org_update BEFORE UPDATE OF org_id ON projects
WHEN NEW.org_id IS NOT OLD.org_id
BEGIN SELECT RAISE(ABORT, 'tenant: a project cannot move between organizations'); END;

CREATE TRIGGER IF NOT EXISTS trg_leads_tenant_insert BEFORE INSERT ON leads
WHEN NEW.org_id IS NULL
  OR (SELECT org_id FROM projects WHERE id = NEW.project_id) IS NOT NEW.org_id
  OR (SELECT org_id FROM users WHERE id = NEW.entered_by) IS NOT NEW.org_id
  OR (NEW.assigned_to IS NOT NULL AND (SELECT org_id FROM users WHERE id = NEW.assigned_to) IS NOT NEW.org_id)
BEGIN SELECT RAISE(ABORT, 'tenant: lead references another organization'); END;

CREATE TRIGGER IF NOT EXISTS trg_leads_tenant_update BEFORE UPDATE ON leads
WHEN NEW.org_id IS NOT OLD.org_id
  OR NEW.entered_by IS NOT OLD.entered_by
  OR (SELECT org_id FROM projects WHERE id = NEW.project_id) IS NOT NEW.org_id
  OR (NEW.assigned_to IS NOT NULL AND (SELECT org_id FROM users WHERE id = NEW.assigned_to) IS NOT NEW.org_id)
  OR (NEW.updated_by IS NOT NULL AND (SELECT org_id FROM users WHERE id = NEW.updated_by) IS NOT NEW.org_id)
BEGIN SELECT RAISE(ABORT, 'tenant: invalid lead update'); END;

CREATE TRIGGER IF NOT EXISTS trg_products_org_required BEFORE INSERT ON products
WHEN NEW.org_id IS NULL
BEGIN SELECT RAISE(ABORT, 'tenant: product must belong to an organization'); END;

CREATE TRIGGER IF NOT EXISTS trg_products_org_update BEFORE UPDATE OF org_id ON products
WHEN NEW.org_id IS NOT OLD.org_id
BEGIN SELECT RAISE(ABORT, 'tenant: a product cannot move between organizations'); END;

CREATE TRIGGER IF NOT EXISTS trg_lead_products_tenant BEFORE INSERT ON lead_products
WHEN (SELECT org_id FROM leads WHERE id = NEW.lead_id) IS NOT NEW.org_id
  OR (SELECT org_id FROM products WHERE id = NEW.product_id) IS NOT NEW.org_id
BEGIN SELECT RAISE(ABORT, 'tenant: lead/product organization mismatch'); END;

CREATE TRIGGER IF NOT EXISTS trg_project_staff_tenant BEFORE INSERT ON project_staff
WHEN (SELECT org_id FROM projects WHERE id = NEW.project_id) IS NOT (SELECT org_id FROM users WHERE id = NEW.user_id)
BEGIN SELECT RAISE(ABORT, 'tenant: project/staff organization mismatch'); END;

CREATE TRIGGER IF NOT EXISTS trg_project_products_tenant BEFORE INSERT ON project_products
WHEN (SELECT org_id FROM projects WHERE id = NEW.project_id) IS NOT (SELECT org_id FROM products WHERE id = NEW.product_id)
BEGIN SELECT RAISE(ABORT, 'tenant: project/product organization mismatch'); END;

CREATE TRIGGER IF NOT EXISTS trg_follow_ups_tenant BEFORE INSERT ON follow_ups
WHEN (SELECT org_id FROM leads WHERE id = NEW.lead_id) IS NOT NEW.org_id
  OR (SELECT org_id FROM users WHERE id = NEW.created_by) IS NOT NEW.org_id
BEGIN SELECT RAISE(ABORT, 'tenant: follow-up organization mismatch'); END;
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backup_database(db_path, label):
    """Write a consistent copy of the database to instance/backups/ and return its path."""
    folder = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, f"crm-{datetime.now():%Y%m%d-%H%M%S}-{label}.db")
    src = sqlite3.connect(db_path)
    try:
        with sqlite3.connect(dest) as out:
            src.backup(out)
    finally:
        src.close()
    return dest


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, definition):
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migrate_v1(conn):
    for table, cols in {
        "users": ["org_id INTEGER REFERENCES organizations(id)",
                  "must_change_password INTEGER NOT NULL DEFAULT 0",
                  "session_version INTEGER NOT NULL DEFAULT 1",
                  "last_login_at TEXT"],
        "projects": ["org_id INTEGER REFERENCES organizations(id)", "code TEXT", "campaign_type TEXT",
                     "start_date TEXT", "end_date TEXT", "updated_at TEXT"],
        "leads": ["org_id INTEGER REFERENCES organizations(id)", "district TEXT", "country TEXT",
                  "priority TEXT", "assigned_to INTEGER REFERENCES users(id)", "next_follow_up TEXT",
                  "phone_norm TEXT"],
    }.items():
        for col in cols:
            _add_column(conn, table, col)
    # executescript() would commit mid-transaction, so run statements one by one.
    for stmt in _split(SCHEMA_V1_TABLES):
        conn.execute(stmt)

    has_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
    if has_users:
        ts = now()
        first_admin = conn.execute(
            "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
        cur = conn.execute(
            "INSERT INTO organizations (name, code, kind, status, created_at) VALUES (?,?, 'agency', 'Active', ?)",
            ("My Agency", "AGENCY", ts))
        agency_id = cur.lastrowid
        conn.execute("UPDATE users SET org_id = ? WHERE org_id IS NULL", (agency_id,))
        if first_admin:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (SUPER_ADMIN, first_admin[0]))
            conn.execute("UPDATE organizations SET created_by = ? WHERE id = ?", (first_admin[0], agency_id))
        conn.execute("UPDATE users SET role = ? WHERE role = 'admin'", (ORG_ADMIN,))
        conn.execute("UPDATE projects SET org_id = ? WHERE org_id IS NULL", (agency_id,))
        conn.execute("UPDATE projects SET status = 'Active' WHERE status IS NULL OR status = ''")
        conn.execute("UPDATE leads SET org_id = ? WHERE org_id IS NULL", (agency_id,))
        conn.execute("UPDATE leads SET assigned_to = entered_by WHERE assigned_to IS NULL")

        # Normalise states (Kerala / kerala / KL -> Kerala) and fill phone keys.
        for row in conn.execute("SELECT id, state, country, phone, whatsapp FROM leads").fetchall():
            state, country = normalize_location(row["state"], row["country"] or "")
            if not country and (not state or state in _indian_states()):
                country = DEFAULT_COUNTRY
            conn.execute("UPDATE leads SET state = ?, country = ?, phone_norm = ? WHERE id = ?",
                         (state, country, phone_key(row["phone"] or row["whatsapp"]), row["id"]))

        # Each distinct free-text requirement becomes a product; leads are linked as "Interested"
        # (interest only - never treated as an order). The original text is kept on the lead.
        creator = first_admin[0] if first_admin else None
        reqs = conn.execute(
            "SELECT DISTINCT TRIM(requirement) r FROM leads WHERE TRIM(COALESCE(requirement,'')) != ''").fetchall()
        for (req,) in reqs:
            pid = conn.execute("SELECT id FROM products WHERE org_id = ? AND name = ? COLLATE NOCASE",
                               (agency_id, req)).fetchone()
            if pid:
                pid = pid[0]
            else:
                pid = conn.execute(
                    "INSERT INTO products (org_id, name, category, active, created_by, created_at)"
                    " VALUES (?,?, 'Imported from requirements', 1, ?, ?)", (agency_id, req, creator, ts)).lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO lead_products (lead_id, product_id, org_id, stage, created_at)"
                " SELECT id, ?, org_id, 'Interested', ? FROM leads WHERE TRIM(requirement) = ? COLLATE NOCASE",
                (pid, ts, req))

    for stmt in _split(SCHEMA_V1_INDEXES):
        conn.execute(stmt)
    for stmt in _split_triggers(TENANT_TRIGGERS):
        conn.execute(stmt)


def _indian_states():
    from crm_constants import INDIAN_STATES
    return set(INDIAN_STATES)


def _split(script):
    return [s.strip() for s in script.split(";") if s.strip()]


def _split_triggers(script):
    return [s.strip() + " END;" for s in script.split("END;") if s.strip()]


def migrate(db_path):
    """Bring the database at db_path up to SCHEMA_VERSION. Returns the backup path, if one was made."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = connect(db_path)
    conn.isolation_level = None  # manual transactions so DDL + data changes are atomic
    backup_path = None
    try:
        conn.executescript(SCHEMA_V0)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
                backup_path = backup_database(db_path, "pre-v1")
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            try:
                _migrate_v1(conn)
                conn.execute("PRAGMA user_version = 1")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()
    return backup_path
