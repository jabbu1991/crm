"""Database schema and versioned, backward-compatible migrations.

The schema version lives in SQLite's PRAGMA user_version:
  0 = original single-workspace CRM
  1 = multi-tenant organizations, products, follow-ups, password tokens
  2 = audit log (Super Admin access to client workspaces)
Before migrating a database that already holds data, a full backup is written to
instance/backups/. Migrations only ADD tables/columns; no existing data is dropped.
"""
import os
import sqlite3
from datetime import datetime

from crm_constants import DEFAULT_COUNTRY, ORG_ADMIN, SUPER_ADMIN, normalize_location, phone_key

SCHEMA_VERSION = 6

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


def _migrate_v2(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL REFERENCES organizations(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        action TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_org ON audit_log(org_id, id)")


def _migrate_v3(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
    if "company_name" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN company_name TEXT")


DEFAULT_WA_TEMPLATES = [
    ("Welcome", "Hi {name} 🙏\nThis is {my_name} from {org_name}. Thank you for contacting us!\n"
                "We'd love to know more about your requirement for {product}. When would be a good time to talk?\n"
                "📞 {my_phone}"),
    ("Missed call", "Hi {name},\nThis is {my_name} from {org_name}. I tried calling you but couldn't reach you.\n"
                    "Please let me know a convenient time, or call me at 📞 {my_phone}"),
    ("Product details", "Hi {name},\nPlease find the details of {product} below 👇\n"
                        "Feel free to ask if you have any questions.\n– {my_name}, {org_name}"),
    ("Quotation", "Hi {name},\nPlease find attached the quotation for {product}.\n"
                  "For any questions about pricing or details, call me at 📞 {my_phone}\n– {my_name}, {org_name}"),
    ("Follow-up", "Hi {name},\nJust following up on {product} that we discussed. Have you made a decision?\n"
                  "Let me know if you need any help 😊\n– {my_name}, {org_name}"),
    ("Meeting / Demo confirmation", "Hi {name},\nYour meeting / demo is confirmed.\n📅 Date: ____  ⏰ Time: ____\n"
                                    "If anything changes, please let me know: 📞 {my_phone}"),
    ("Order confirmation", "Hi {name},\nWe have received your order for {product}. Thank you! 🎉\n"
                           "We will update you on the delivery details shortly.\n– {org_name}"),
    ("Thank you", "Hi {name},\nThank you for choosing {org_name} 🙏\nIf you need any help, feel free to reach out "
                  "anytime. We'd be grateful if you recommend us to your friends 😊"),
    ("Re-connect", "Hi {name},\nYou had enquired about {product} some time ago.\n"
                   "We have some new offers now. Would you like to know more?\n– {my_name}, {org_name}"),
    ("Offer / Festival", "Hi {name},\nSpecial offer from {org_name}! 🎉\n____ (offer details)\nTo know more: 📞 {my_phone}"),
]


def copy_default_templates(conn, org_id, user_id, ts):
    """Give an organization its own editable copy of the agency's default WhatsApp templates."""
    rows = conn.execute("SELECT title, body, sort FROM wa_templates WHERE scope = 'default' ORDER BY sort, id").fetchall()
    conn.executemany("INSERT INTO wa_templates (org_id, scope, title, body, active, sort, created_by, created_at)"
                     " VALUES (?, 'org', ?, ?, 0, ?, ?, ?)", [(org_id, r[0], r[1], r[2], user_id, ts) for r in rows])


def _migrate_v4(conn):
    """WhatsApp templates and settings, and the recycle bin for deleted leads. Adds only; changes no data."""
    org_cols = {r[1] for r in conn.execute("PRAGMA table_info(organizations)")}
    if "wa_templates_allowed" not in org_cols:  # set by the Super Admin per client
        conn.execute("ALTER TABLE organizations ADD COLUMN wa_templates_allowed INTEGER NOT NULL DEFAULT 1")
    if "wa_templates_on" not in org_cols:  # set by the organization's admin
        conn.execute("ALTER TABLE organizations ADD COLUMN wa_templates_on INTEGER NOT NULL DEFAULT 1")
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "contact_phone" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN contact_phone TEXT")
    if "wa_templates_on" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN wa_templates_on INTEGER NOT NULL DEFAULT 1")
    conn.execute("""CREATE TABLE IF NOT EXISTS wa_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL REFERENCES organizations(id),
        scope TEXT NOT NULL CHECK (scope IN ('default', 'org', 'user')),
        user_id INTEGER REFERENCES users(id),
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        sort INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL,
        updated_at TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_templates_org ON wa_templates(org_id, scope)")
    conn.execute("""CREATE TABLE IF NOT EXISTS deleted_leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL REFERENCES organizations(id),
        lead_id INTEGER NOT NULL,
        customer_name TEXT,
        phone TEXT,
        project_name TEXT,
        deleted_by INTEGER NOT NULL REFERENCES users(id),
        deleted_at TEXT NOT NULL,
        data TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deleted_leads_org ON deleted_leads(org_id, deleted_at)")
    agency = conn.execute("SELECT id FROM organizations WHERE kind = 'agency' ORDER BY id LIMIT 1").fetchone()
    if agency and not conn.execute("SELECT 1 FROM wa_templates WHERE scope = 'default'").fetchone():
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.executemany("INSERT INTO wa_templates (org_id, scope, title, body, active, sort, created_at)"
                         " VALUES (?, 'default', ?, ?, 1, ?, ?)",
                         [(agency[0], t, b, i, ts) for i, (t, b) in enumerate(DEFAULT_WA_TEMPLATES)])
        for (oid,) in conn.execute("SELECT id FROM organizations").fetchall():
            if not conn.execute("SELECT 1 FROM wa_templates WHERE org_id = ? AND scope = 'org'", (oid,)).fetchone():
                copy_default_templates(conn, oid, None, ts)


def _migrate_v5(conn):
    """Company templates start switched off, so WhatsApp sends "Hi <name>," in one click until an admin turns some on."""
    conn.execute("UPDATE wa_templates SET active = 0 WHERE scope = 'org'")


def _migrate_v6(conn):
    """Digital catalogue: product photos and links, a public page per organization, and who viewed it."""
    def add(table, col, ddl):
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    add("products", "price", "TEXT")
    add("products", "video_url", "TEXT")
    add("products", "brochure_url", "TEXT")
    add("products", "show_in_catalog", "INTEGER NOT NULL DEFAULT 1")
    add("organizations", "catalog_allowed", "INTEGER NOT NULL DEFAULT 1")  # Super Admin, per client plan
    add("organizations", "catalog_on", "INTEGER NOT NULL DEFAULT 0")       # the organization publishes it
    add("organizations", "catalog_about", "TEXT")
    add("organizations", "catalog_logo", "TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS product_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL REFERENCES organizations(id),
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_images ON product_images(product_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS catalog_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL REFERENCES organizations(id),
        lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
        product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_views ON catalog_views(org_id, created_at)")


MIGRATIONS = [(1, _migrate_v1), (2, _migrate_v2), (3, _migrate_v3), (4, _migrate_v4), (5, _migrate_v5),
              (6, _migrate_v6)]


def migrate(db_path):
    """Bring the database at db_path up to SCHEMA_VERSION. Returns the backup path, if one was made."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = connect(db_path)
    conn.isolation_level = None  # manual transactions so DDL + data changes are atomic
    backup_path = None
    try:
        conn.executescript(SCHEMA_V0)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        pending = [(v, fn) for v, fn in MIGRATIONS if v > version]
        if pending and conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            backup_path = backup_database(db_path, f"pre-v{pending[0][0]}")
        for v, fn in pending:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            try:
                fn(conn)
                conn.execute(f"PRAGMA user_version = {v}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()
    return backup_path
