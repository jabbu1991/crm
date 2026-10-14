"""End-to-end tests for tenant isolation, roles, state-wise and product-wise reporting, and migration.

Run:  python -m pytest -q
"""
import io
import os
import re
import sqlite3
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("CRM_DB_PATH", os.path.join(tempfile.mkdtemp(), "bootstrap.db"))
import app as crm  # noqa: E402
import crm_db  # noqa: E402

PW = "password123"


class Browser:
    """Small wrapper around Flask's test client that handles CSRF tokens."""

    def __init__(self):
        self.c = crm.app.test_client()

    def token(self):
        with self.c.session_transaction() as s:
            if "csrf_token" not in s:
                s["csrf_token"] = "test-token"
            return s["csrf_token"]

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, follow=True, **kw):
        data = dict(data or {})
        data.setdefault("csrf_token", self.token())
        return self.c.post(url, data=data, follow_redirects=follow, **kw)

    def login(self, username, password=PW):
        r = self.post("/login", {"username": username, "password": password})
        return r


def lead_ids(html):
    return set(int(x) for x in re.findall(r'href="/leads/(\d+)"', html))


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """Super Admin + agency, Org A (admin, staff1, staff2), Org B (admin, staff)."""
    db_path = str(tmp_path / "crm.db")
    monkeypatch.setattr(crm, "DB_PATH", db_path)
    crm._failed_logins.clear()
    crm_db.migrate(db_path)

    sup = Browser()
    r = sup.post("/setup", {"agency": "Bright Ads", "name": "Owner", "username": "super", "password": PW})
    assert r.status_code == 200

    def make_org(name, code, admin_user):
        r = sup.post("/admin/organizations/new", {
            "name": name, "code": code, "admin_name": f"{name} Admin", "admin_username": admin_user,
            "onboarding": "password", "password": "TempPass123"})
        assert f"created with admin {admin_user}" in r.text, r.text[:3000]
        b = Browser()
        b.login(admin_user, "TempPass123")
        r = b.post("/account", {"current_password": "TempPass123", "new_password": PW, "confirm": PW})
        assert "Password changed" in r.text
        return b

    a_admin = make_org("Org A", "ORGA", "a_admin")
    b_admin = make_org("Org B", "ORGB", "b_admin")

    def make_staff(admin, username, name):
        r = admin.post("/staff/new", {"name": name, "username": username, "role": "staff", "active": "1",
                                      "onboarding": "password", "password": "TempPass123"})
        assert "created" in r.text, r.text[:3000]
        b = Browser()
        b.login(username, "TempPass123")
        b.post("/account", {"current_password": "TempPass123", "new_password": PW, "confirm": PW})
        return b

    a_s1 = make_staff(a_admin, "a_staff1", "Anu Staff1")
    a_s2 = make_staff(a_admin, "a_staff2", "Ben Staff2")
    b_s1 = make_staff(b_admin, "b_staff1", "Cara StaffB")

    def db():
        return sqlite3.connect(db_path)

    def ids(sql, args=()):
        with db() as conn:
            return [r[0] for r in conn.execute(sql, args)]

    # Each org: one project and two products created by its admin.
    for admin, prefix in ((a_admin, "A"), (b_admin, "B")):
        admin.post("/products/new", {"name": f"{prefix} Product 1", "code": f"{prefix}P1", "active": "1"})
        admin.post("/products/new", {"name": f"{prefix} Product 2", "code": f"{prefix}P2", "active": "1"})
        admin.post("/projects/new", {"name": f"{prefix} Campaign", "code": f"{prefix}C", "status": "Active"})

    W = dict(sup=sup, a_admin=a_admin, b_admin=b_admin, a_s1=a_s1, a_s2=a_s2, b_s1=b_s1, db=db, ids=ids,
             db_path=db_path)
    W["org_a"], W["org_b"] = ids("SELECT id FROM organizations WHERE code IN ('ORGA','ORGB') ORDER BY code")
    W["proj_a"] = ids("SELECT id FROM projects WHERE code = 'AC'")[0]
    W["proj_b"] = ids("SELECT id FROM projects WHERE code = 'BC'")[0]
    W["prod_a1"], W["prod_a2"] = ids("SELECT id FROM products WHERE code IN ('AP1','AP2') ORDER BY code")
    W["prod_b1"] = ids("SELECT id FROM products WHERE code = 'BP1'")[0]
    W["uid"] = lambda username: ids("SELECT id FROM users WHERE username = ?", (username,))[0]
    return W


def add_lead(browser, project, name, phone, state="Kerala", products=(), extra=None):
    data = {"project_id": project, "customer_name": name, "phone": phone, "state": state, "country": "India",
            "status": "New", "lead_quality": "Hot", "buying_possibility": "High", "confirm_duplicate": "1",
            "product_ids": [str(p) for p in products]}
    data.update(extra or {})
    r = browser.post("/leads/new", data)
    assert "saved" in r.text, r.text[:3000]
    m = re.search(r'action="/leads/(\d+)/status"', r.text)
    return int(m.group(1))


# ------------------------------------------------------------------ roles & visibility

def test_staff_cannot_see_other_staff_leads(world):
    w = world
    l1 = add_lead(w["a_s1"], w["proj_a"], "Lead of S1", "9000000001")
    l2 = add_lead(w["a_s2"], w["proj_a"], "Lead of S2", "9000000002")

    page = w["a_s1"].get("/leads").get_data(as_text=True)
    assert l1 in lead_ids(page) and l2 not in lead_ids(page)
    assert w["a_s1"].get(f"/leads/{l2}").status_code == 404
    assert w["a_s1"].get(f"/leads/{l2}/edit").status_code == 404
    r = w["a_s1"].post(f"/leads/{l2}/status", {"status": "Lost"})
    assert r.status_code == 404
    r = w["a_s1"].post(f"/leads/{l2}/edit", {"project_id": w["proj_a"], "customer_name": "hijack", "phone": "1"})
    assert r.status_code == 404
    # search and staff filter cannot reveal it either
    assert l2 not in lead_ids(w["a_s1"].get("/leads?q=S2").get_data(as_text=True))
    assert l2 not in lead_ids(w["a_s1"].get(f"/leads?staff={w['uid']('a_staff2')}").get_data(as_text=True))
    # staff may not export, see reports, staff list or org settings
    for url in ("/leads/export", "/reports", "/staff", "/organization", "/admin/organizations"):
        assert w["a_s1"].get(url).status_code == 403, url
    # staff can delete nothing
    assert w["a_s1"].post(f"/leads/{l1}/delete").status_code == 403


def test_org_admin_sees_all_staff_leads_and_filters(world):
    w = world
    l1 = add_lead(w["a_s1"], w["proj_a"], "Lead of S1", "9000000001")
    l2 = add_lead(w["a_s2"], w["proj_a"], "Lead of S2", "9000000002")
    page = w["a_admin"].get("/leads").get_data(as_text=True)
    assert {l1, l2} <= lead_ids(page)
    only_s1 = lead_ids(w["a_admin"].get(f"/leads?staff={w['uid']('a_staff1')}").get_data(as_text=True))
    assert l1 in only_s1 and l2 not in only_s1
    dash = w["a_admin"].get("/").get_data(as_text=True)
    assert "Anu Staff1" in dash and "Ben Staff2" in dash
    csv_text = w["a_admin"].get("/leads/export").get_data(as_text=True)
    assert "Lead of S1" in csv_text and "Lead of S2" in csv_text


def test_lead_records_creator_and_cannot_be_impersonated(world):
    w = world
    s2 = w["uid"]("a_staff2")
    lid = add_lead(w["a_s1"], w["proj_a"], "Owned", "9000000003",
                   extra={"entered_by": s2, "org_id": w["org_b"], "assigned_to": s2})
    with w["db"]() as conn:
        org, creator, assigned, created, updated = conn.execute(
            "SELECT org_id, entered_by, assigned_to, created_at, updated_at FROM leads WHERE id = ?", (lid,)).fetchone()
    assert org == w["org_a"] and creator == w["uid"]("a_staff1") and assigned == w["uid"]("a_staff1")
    assert created and updated
    # editing as admin records updated_by, creator unchanged
    w["a_admin"].post(f"/leads/{lid}/edit", {"project_id": w["proj_a"], "customer_name": "Owned 2", "phone": "9000000003",
                                             "status": "Contacted", "entered_by": s2, "assigned_to": s2})
    with w["db"]() as conn:
        creator, assigned, upd_by = conn.execute(
            "SELECT entered_by, assigned_to, updated_by FROM leads WHERE id = ?", (lid,)).fetchone()
    assert creator == w["uid"]("a_staff1") and assigned == s2 and upd_by == w["uid"]("a_admin")
    # once reassigned to staff2, staff2 can see it (assigned) - staff1 still sees it (creator)
    assert w["a_s2"].get(f"/leads/{lid}").status_code == 200
    assert w["a_s1"].get(f"/leads/{lid}").status_code == 200


# ------------------------------------------------------------------ tenant isolation

def test_org_a_cannot_access_org_b(world):
    w = world
    lb = add_lead(w["b_admin"], w["proj_b"], "Secret B customer", "8000000001", products=[w["prod_b1"]])
    la = add_lead(w["a_admin"], w["proj_a"], "A customer", "8000000002")
    a = w["a_admin"]
    for url in (f"/leads/{lb}", f"/leads/{lb}/edit", f"/projects/{w['proj_b']}/edit", f"/products/{w['prod_b1']}/edit",
                f"/product-leads/{w['prod_b1']}", f"/leads?project={w['proj_b']}",
                f"/staff/{w['uid']('b_admin')}/edit"):
        assert a.get(url).status_code == 404, url
    for url, data in ((f"/leads/{lb}/delete", {}), (f"/projects/{w['proj_b']}/delete", {}),
                      (f"/products/{w['prod_b1']}/toggle", {}), (f"/staff/{w['uid']('b_staff1')}/toggle", {}),
                      (f"/staff/{w['uid']('b_staff1')}/password", {"action": "reset"}),
                      (f"/leads/{lb}/followups", {"due_date": "2030-01-01"})):
        assert a.post(url, data).status_code == 404, url
    # Listings, search, exports and reports never include B's data.
    for url in ("/leads", "/leads?q=Secret", "/states", "/product-leads", "/products", "/followups",
                "/reports?by=project", "/leads/export", "/"):
        text = a.get(url).get_data(as_text=True)
        assert "Secret B customer" not in text and "B Product 1" not in text and "B Campaign" not in text, url
    # Forged ids in a new lead: B's project is rejected, B's product silently dropped.
    r = a.post("/leads/new", {"project_id": w["proj_b"], "customer_name": "x", "phone": "1234567", "status": "New"})
    assert "Please choose a project" in r.text
    lid = add_lead(a, w["proj_a"], "Forged", "8000000003", products=[w["prod_b1"], w["prod_a1"]],
                   extra={"assigned_to": w["uid"]("b_admin")})
    with w["db"]() as conn:
        prods = {r[0] for r in conn.execute("SELECT product_id FROM lead_products WHERE lead_id = ?", (lid,))}
        assigned = conn.execute("SELECT assigned_to FROM leads WHERE id = ?", (lid,)).fetchone()[0]
    assert prods == {w["prod_a1"]} and assigned == w["uid"]("a_admin")
    # B still sees its own lead and not A's
    bt = w["b_admin"].get("/leads").get_data(as_text=True)
    assert lb in lead_ids(bt) and la not in lead_ids(bt)


def test_org_b_admin_manages_only_org_b(world):
    w = world
    staff_page = w["b_admin"].get("/staff").get_data(as_text=True)
    assert "Cara StaffB" in staff_page and "Anu Staff1" not in staff_page and "Owner" not in staff_page
    assert w["b_admin"].get(f"/staff/{w['uid']('a_staff1')}/edit").status_code == 404
    assert w["b_admin"].get("/admin/organizations").status_code == 403
    # an org admin cannot create a super admin or move a user to another org
    w["b_admin"].post("/staff/new", {"name": "Sneaky", "username": "sneaky", "role": "super_admin", "active": "1",
                                     "onboarding": "password", "password": "TempPass123", "org_id": w["org_a"]})
    with w["db"]() as conn:
        role, org = conn.execute("SELECT role, org_id FROM users WHERE username = 'sneaky'").fetchone()
    assert role == "staff" and org == w["org_b"]


def test_super_admin_does_not_see_client_leads(world):
    w = world
    add_lead(w["a_admin"], w["proj_a"], "Private A lead", "7000000001")
    for url in ("/", "/leads", "/admin/organizations", f"/admin/organizations/{w['org_a']}/users", "/states"):
        assert "Private A lead" not in w["sup"].get(url).get_data(as_text=True), url
    dash = w["sup"].get("/").get_data(as_text=True)
    assert "Org A" in dash and "Agency control panel" in dash


def test_database_triggers_reject_cross_tenant_rows(world):
    w = world
    lb = add_lead(w["b_admin"], w["proj_b"], "B", "6000000001")
    conn = w["db"]()
    conn.execute("PRAGMA foreign_keys = ON")
    bad = [
        ("INSERT INTO leads (org_id, project_id, customer_name, entered_by, created_at, updated_at)"
         " VALUES (?,?, 'x', ?, 'now', 'now')", (w["org_a"], w["proj_b"], w["uid"]("a_admin"))),
        ("INSERT INTO lead_products (lead_id, product_id, org_id, created_at) VALUES (?,?,?, 'now')",
         (lb, w["prod_a1"], w["org_b"])),
        ("UPDATE leads SET org_id = ? WHERE id = ?", (w["org_a"], lb)),
        ("UPDATE leads SET entered_by = ? WHERE id = ?", (w["uid"]("b_staff1"), lb)),
        ("UPDATE users SET org_id = ? WHERE username = 'a_staff1'", (w["org_b"],)),
        ("INSERT INTO follow_ups (org_id, lead_id, due_date, created_by, created_at) VALUES (?,?, '2030-01-01', ?, 'now')",
         (w["org_a"], lb, w["uid"]("a_admin"))),
    ]
    for sql, args in bad:
        with pytest.raises(sqlite3.IntegrityError, match="tenant"):
            conn.execute(sql, args)
    conn.close()


# ------------------------------------------------------------------ super admin & accounts

def test_super_admin_deactivates_and_reactivates_org(world):
    w = world
    assert w["b_s1"].get("/leads").status_code == 200
    r = w["sup"].post(f"/admin/organizations/{w['org_b']}/toggle")
    assert "is now Inactive" in r.text
    # existing session is ended and new logins are refused
    assert w["b_s1"].get("/leads").status_code == 302
    fresh = Browser()
    assert "inactive" in fresh.login("b_admin").get_data(as_text=True)
    # agency org cannot be deactivated
    agency = w["ids"]("SELECT id FROM organizations WHERE kind = 'agency'")[0]
    assert "cannot be deactivated" in w["sup"].post(f"/admin/organizations/{agency}/toggle").get_data(as_text=True)
    w["sup"].post(f"/admin/organizations/{w['org_b']}/toggle")
    assert "Dashboard" in Browser().login("b_admin").get_data(as_text=True)


def test_password_reset_link_and_temp_password(world):
    w = world
    s1 = w["uid"]("a_staff1")
    r = w["a_admin"].post(f"/staff/{s1}/password", {"action": "reset"})
    link = re.search(r'value="(http[^"]+/reset/[^"]+)"', r.get_data(as_text=True)).group(1)
    path = link.split("localhost", 1)[1]
    with w["db"]() as conn:  # only a hash is stored, never the raw token or a password
        stored = conn.execute("SELECT token_hash FROM password_tokens WHERE user_id = ?", (s1,)).fetchall()
    assert all(path.rsplit("/", 1)[1] not in h for (h,) in stored)
    b = Browser()
    r = b.post(path, {"password": "BrandNew123", "confirm": "BrandNew123"})
    assert "Password set" in r.text
    assert b.post(path, {"password": "Another123", "confirm": "Another123"}).status_code == 410  # single use
    # the old session of staff1 was signed out; old password fails, new works
    assert w["a_s1"].get("/leads").status_code == 302
    assert "Invalid username or password" in Browser().login("a_staff1").get_data(as_text=True)
    assert "Dashboard" in Browser().login("a_staff1", "BrandNew123").get_data(as_text=True)
    # temporary password forces a change before anything else
    w["a_admin"].post(f"/staff/{s1}/password", {"action": "temp", "temp_password": "TempTemp99"})
    t = Browser()
    t.login("a_staff1", "TempTemp99")
    assert t.get("/leads").status_code == 302 and "/account" in t.get("/leads").headers["Location"]
    # expired link is refused
    w["a_admin"].post(f"/staff/{s1}/password", {"action": "reset"})
    with w["db"]() as conn:
        conn.execute("UPDATE password_tokens SET expires_at = '2000-01-01 00:00:00' WHERE user_id = ?", (s1,))
    r = w["a_admin"].post(f"/staff/{s1}/password", {"action": "reset"})
    path = re.search(r'value="http[^"]+?(/reset/[^"]+)"', r.get_data(as_text=True)).group(1)
    with w["db"]() as conn:
        conn.execute("UPDATE password_tokens SET expires_at = '2000-01-01 00:00:00' WHERE user_id = ?", (s1,))
    assert Browser().get(path).status_code == 410


def test_invite_link_onboarding(world):
    w = world
    r = w["a_admin"].post("/staff/new", {"name": "Invited", "username": "invited", "role": "staff", "active": "1",
                                         "onboarding": "invite"})
    path = re.search(r'value="http[^"]+?(/reset/[^"]+)"', r.get_data(as_text=True)).group(1)
    assert "Invalid username" in Browser().login("invited", "").get_data(as_text=True)
    b = Browser()
    b.post(path, {"password": "Invited123", "confirm": "Invited123"})
    assert "Dashboard" in b.login("invited", "Invited123").get_data(as_text=True)


def test_deactivated_user_is_signed_out_and_blocked(world):
    w = world
    assert w["a_s2"].get("/leads").status_code == 200
    w["a_admin"].post(f"/staff/{w['uid']('a_staff2')}/toggle")
    assert w["a_s2"].get("/leads").status_code == 302
    assert "deactivated" in Browser().login("a_staff2").get_data(as_text=True)
    # an admin cannot deactivate themselves or touch the super admin
    assert "cannot deactivate your own" in w["a_admin"].post(
        f"/staff/{w['uid']('a_admin')}/toggle").get_data(as_text=True)


def test_csrf_and_login_throttle(world):
    w = world
    r = w["a_admin"].c.post("/products/new", data={"name": "No token"})
    assert r.status_code == 400
    b = Browser()
    for _ in range(5):
        b.login("a_staff1", "wrong-password")
    assert b.login("a_staff1").status_code == 429


# ------------------------------------------------------------------ state-wise

def test_state_normalisation_and_permissions(world):
    w = world
    add_lead(w["a_s1"], w["proj_a"], "K1", "5000000001", state="Kerala")
    add_lead(w["a_s1"], w["proj_a"], "K2", "5000000002", state="__other__", extra={"state_other": "kerala"})
    add_lead(w["a_s1"], w["proj_a"], "K3", "5000000003", state="__other__", extra={"state_other": "KL"})
    add_lead(w["a_s2"], w["proj_a"], "K4", "5000000004", state="Kerala")
    add_lead(w["a_s2"], w["proj_a"], "TN1", "5000000005", state="Tamil Nadu")
    add_lead(w["a_s1"], w["proj_a"], "Dubai", "5000000006", state="__other__",
             extra={"state_other": "dubai", "country": "UAE"})
    with w["db"]() as conn:
        states = {r[0] for r in conn.execute("SELECT DISTINCT state FROM leads")}
        dubai = conn.execute("SELECT state, country FROM leads WHERE customer_name = 'Dubai'").fetchone()
    assert "kerala" not in states and "KL" not in states and "Kerala" in states
    assert dubai == ("Dubai", "United Arab Emirates")

    admin_states = w["a_admin"].get("/states").get_data(as_text=True)
    assert re.search(r"<b>Kerala</b>.*?4 leads", admin_states, re.S)
    s1_states = w["a_s1"].get("/states").get_data(as_text=True)
    assert re.search(r"<b>Kerala</b>.*?3 leads", s1_states, re.S) and "Tamil Nadu" not in s1_states
    view = w["a_s1"].get("/states/view?state=Kerala").get_data(as_text=True)
    assert len(lead_ids(view)) == 3 and "K4" not in view
    view = w["a_admin"].get("/states/view?state=Kerala&q=K4").get_data(as_text=True)
    assert len(lead_ids(view)) == 1
    view = w["a_admin"].get(f"/states/view?state=Kerala&staff={w['uid']('a_staff2')}").get_data(as_text=True)
    assert len(lead_ids(view)) == 1
    # B sees nothing of A's states
    assert "Kerala" not in w["b_admin"].get("/states").get_data(as_text=True)


def test_pagination(world):
    w = world
    with w["db"]() as conn:  # bulk insert 120 leads for staff1
        uid = w["uid"]("a_staff1")
        conn.executemany(
            "INSERT INTO leads (org_id, project_id, customer_name, phone, state, status, entered_by, assigned_to,"
            " created_at, updated_at) VALUES (?,?,?,?, 'Kerala', 'New', ?, ?, '2026-01-01 10:00:00', '2026-01-01 10:00:00')",
            [(w["org_a"], w["proj_a"], f"Bulk {i:03d}", f"9{i:09d}", uid, uid) for i in range(120)])
    p1 = w["a_s1"].get("/leads?sort=name&dir=asc").get_data(as_text=True)
    p3 = w["a_s1"].get("/leads?sort=name&dir=asc&page=3").get_data(as_text=True)
    assert len(lead_ids(p1)) == 50 and len(lead_ids(p3)) == 20
    assert "Bulk 000" in p1 and "Bulk 119" in p3 and "Page 1 of 3" in p1
    assert "Bulk 000" in w["a_s1"].get("/states/view?state=Kerala&sort=name&dir=asc").get_data(as_text=True)


# ------------------------------------------------------------------ products & product-wise report

def product_row(html, name):
    row = re.search(rf"<b>{re.escape(name)}</b>.*?</tr>", html, re.S).group(0)
    return [int(x) for x in re.findall(r'class="num">(?:<b>)?(\d+)', row)][:7]


def test_product_report_counts_and_interest_vs_purchase(world):
    w = world
    p1, p2 = w["prod_a1"], w["prod_a2"]
    l1 = add_lead(w["a_s1"], w["proj_a"], "Multi", "4000000001", products=[p1, p2])
    l2 = add_lead(w["a_s1"], w["proj_a"], "Multi again", "4000000001", products=[p1])  # same customer phone
    l3 = add_lead(w["a_s2"], w["proj_a"], "Other", "4000000003", products=[p1], extra={"lead_quality": "Cold"})
    html = w["a_admin"].get("/product-leads").get_data(as_text=True)
    # total, customers, hot, still interested, ordered, purchased, lost
    assert product_row(html, "A Product 1") == [3, 2, 2, 3, 0, 0, 0]
    assert product_row(html, "A Product 2") == [1, 1, 1, 1, 0, 0, 0]

    # Converting the lead does NOT mark products as purchased.
    w["a_admin"].post(f"/leads/{l1}/status", {"status": "Converted"})
    assert product_row(w["a_admin"].get("/product-leads").get_data(as_text=True), "A Product 1")[5] == 0
    # Explicit stages are counted separately.
    w["a_admin"].post(f"/leads/{l1}/products", {f"stage_{p1}": "Purchased", f"stage_{p2}": "Ordered"})
    w["a_admin"].post(f"/leads/{l3}/status", {"status": "Lost"})
    html = w["a_admin"].get("/product-leads").get_data(as_text=True)
    assert product_row(html, "A Product 1") == [3, 2, 2, 1, 0, 1, 1]
    assert product_row(html, "A Product 2") == [1, 1, 1, 0, 1, 0, 0]

    # Staff1 sees only their own leads in the report.
    html = w["a_s1"].get("/product-leads").get_data(as_text=True)
    assert product_row(html, "A Product 1")[0] == 2
    # Drill-down lists exactly the linked leads, filtered by stage.
    detail = w["a_admin"].get(f"/product-leads/{p1}").get_data(as_text=True)
    assert lead_ids(detail) >= {l1, l2, l3}
    detail = w["a_admin"].get(f"/product-leads/{p1}?stage=Purchased").get_data(as_text=True)
    assert l1 in lead_ids(detail) and l2 not in lead_ids(detail)
    # Filter by state / staff on the report.
    html = w["a_admin"].get(f"/product-leads?staff={w['uid']('a_staff2')}").get_data(as_text=True)
    assert product_row(html, "A Product 1")[0] == 1


def test_product_management_and_inactive_products(world):
    w = world
    r = w["a_admin"].post("/products/new", {"name": "A Product 1", "active": "1"})
    assert "already exists" in r.text
    w["a_admin"].post(f"/products/{w['prod_a2']}/toggle")
    form = w["a_s1"].get("/leads/new").get_data(as_text=True)
    assert "A Product 1" in form and "A Product 2" not in form
    assert "A Product 2" not in w["a_s1"].get("/products").get_data(as_text=True)
    assert w["a_s1"].get("/products/new").status_code == 403
    search = w["a_admin"].get("/products?q=AP1").get_data(as_text=True)
    assert "A Product 1" in search and "A Product 2" not in search


# ------------------------------------------------------------------ projects & follow-ups

def test_projects_assignment_and_fields(world):
    w = world
    s1 = w["uid"]("a_staff1")
    r = w["a_admin"].post("/projects/new", {
        "name": "Restricted", "code": "RST", "campaign_type": "Google Ads", "start_date": "2026-01-01",
        "end_date": "2026-03-31", "status": "Active", "staff_ids": [str(s1)], "product_ids": [str(w["prod_a1"])]})
    assert "created" in r.text
    restricted = w["ids"]("SELECT id FROM projects WHERE code = 'RST'")[0]
    assert "Restricted" in w["a_s1"].get("/projects").get_data(as_text=True)
    assert "Restricted" not in w["a_s2"].get("/projects").get_data(as_text=True)
    r = w["a_s2"].post("/leads/new", {"project_id": restricted, "customer_name": "x", "phone": "123456", "status": "New"})
    assert "Please choose a project" in r.text
    assert "already used" in w["a_admin"].post("/projects/new", {"name": "Dup", "code": "rst"}).get_data(as_text=True)
    assert "End date cannot be before" in w["a_admin"].post(
        "/projects/new", {"name": "Bad", "start_date": "2026-02-01", "end_date": "2026-01-01"}).get_data(as_text=True)


def test_follow_ups(world):
    w = world
    lid = add_lead(w["a_s1"], w["proj_a"], "Follow me", "3000000001", extra={"next_follow_up": "2020-01-01"})
    page = w["a_s1"].get("/followups").get_data(as_text=True)
    assert "Follow me" in page
    assert "Follow me" not in w["a_s2"].get("/followups").get_data(as_text=True)
    assert "Follow me" in w["a_admin"].get("/followups").get_data(as_text=True)
    fid = w["ids"]("SELECT id FROM follow_ups WHERE lead_id = ?", (lid,))[0]
    assert w["a_s2"].post(f"/followups/{fid}/complete", {"action": "done"}).status_code == 404
    w["a_s1"].post(f"/followups/{fid}/complete", {"action": "done", "outcome": "Called"})
    with w["db"]() as conn:
        status, nxt = conn.execute("SELECT f.status, l.next_follow_up FROM follow_ups f JOIN leads l ON l.id = f.lead_id"
                                   " WHERE f.id = ?", (fid,)).fetchone()
    assert status == "Done" and nxt is None
    dash = w["a_s1"].get("/").get_data(as_text=True)
    assert "My dashboard" in dash


def test_import_respects_tenant_and_creator(world):
    w = world
    csv_data = ("Name,Phone,State,Products,Lead Quality\n"
                "Imp One,1111111111,kerala,A Product 1,Hot\nImp Two,2222222222,TN,B Product 1,Warm\n,,,,\n")
    r = w["a_s1"].post("/leads/import", {"project_id": w["proj_a"],
                                         "file": (io.BytesIO(csv_data.encode()), "leads.csv")},
                       content_type="multipart/form-data")
    assert "Imported 2 lead(s)" in r.text and "Skipped 1" in r.text
    with w["db"]() as conn:
        rows = conn.execute("SELECT customer_name, state, org_id, entered_by, requirement,"
                            " (SELECT COUNT(*) FROM lead_products lp WHERE lp.lead_id = leads.id)"
                            " FROM leads WHERE customer_name LIKE 'Imp%' ORDER BY customer_name").fetchall()
    assert rows[0][1:] == ("Kerala", w["org_a"], w["uid"]("a_staff1"), "", 1)
    assert rows[1][1] == "Tamil Nadu" and rows[1][5] == 0 and rows[1][4] == "B Product 1"
    # import into another org's project is refused
    r = w["a_s1"].post("/leads/import", {"project_id": w["proj_b"], "file": (io.BytesIO(csv_data.encode()), "x.csv")},
                       content_type="multipart/form-data")
    assert "Please choose a project" in r.text


# ------------------------------------------------------------------ migration from the original version

def test_migration_from_v0_preserves_data(tmp_path, monkeypatch):
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(crm_db.SCHEMA_V0)
    from werkzeug.security import generate_password_hash
    ts = "2026-01-01 10:00:00"
    conn.execute("INSERT INTO users (name, username, password_hash, role, created_at) VALUES ('Old Admin','oldadmin',?,'admin',?)",
                 (generate_password_hash("oldpass1"), ts))
    conn.execute("INSERT INTO users (name, username, password_hash, role, created_at) VALUES ('Old Admin 2','oldadmin2',?,'admin',?)",
                 (generate_password_hash("oldpass1"), ts))
    conn.execute("INSERT INTO users (name, username, password_hash, role, created_at) VALUES ('Old Staff','oldstaff',?,'staff',?)",
                 (generate_password_hash("oldpass1"), ts))
    conn.execute("INSERT INTO projects (name, status, created_by, created_at) VALUES ('Old Project','Active',1,?)", (ts,))
    for i, (state, req, by) in enumerate([("kerala", "Hyundai Creta", 1), ("KL", "hyundai creta", 3),
                                          ("Tamil Nadu", "2BHK Flat", 3), ("", "", 1)]):
        conn.execute("INSERT INTO leads (project_id, customer_name, phone, state, requirement, status, entered_by,"
                     " created_at, updated_at) VALUES (1,?,?,?,?, 'New', ?, ?, ?)",
                     (f"Old {i}", f"98470 0000{i}", state, req, by, ts, ts))
    conn.execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at) VALUES (1,1,'Lead created',?)", (ts,))
    conn.commit()
    conn.close()

    backup = crm_db.migrate(db_path)
    assert backup and os.path.exists(backup)
    with sqlite3.connect(backup) as b:  # backup is the untouched original
        assert b.execute("PRAGMA user_version").fetchone()[0] == 0
        assert b.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 4
    assert crm_db.migrate(db_path) is None  # idempotent, no second backup

    with sqlite3.connect(db_path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
        roles = dict(c.execute("SELECT username, role FROM users"))
        assert roles == {"oldadmin": "super_admin", "oldadmin2": "org_admin", "oldstaff": "staff"}
        assert c.execute("SELECT COUNT(*) FROM leads WHERE org_id IS NULL").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 4
        assert {r[0] for r in c.execute("SELECT DISTINCT state FROM leads")} == {"Kerala", "Tamil Nadu", ""}
        products = dict(c.execute("SELECT name, (SELECT COUNT(*) FROM lead_products lp WHERE lp.product_id = p.id)"
                                  " FROM products p"))
        assert products == {"Hyundai Creta": 2, "2BHK Flat": 1}
        assert {r[0] for r in c.execute("SELECT DISTINCT stage FROM lead_products")} == {"Interested"}
        assert c.execute("SELECT kind FROM organizations").fetchall() == [("agency",)]
        assert c.execute("SELECT COUNT(*) FROM lead_activity").fetchone()[0] == 1

    # The old accounts still log in and the old data is visible.
    monkeypatch.setattr(crm, "DB_PATH", db_path)
    crm._failed_logins.clear()
    b = Browser()
    assert "Dashboard" in b.login("oldadmin", "oldpass1").get_data(as_text=True)
    assert "Old 3" in b.get("/leads").get_data(as_text=True)
    staff = Browser()
    staff.login("oldstaff", "oldpass1")
    text = staff.get("/leads").get_data(as_text=True)
    assert "Old 1" in text and "Old 0" not in text  # staff now sees only their own leads


def test_names_are_escaped_in_pages(world):
    w = world
    evil = "x');alert(1);('\"><script>alert(2)</script>"
    w["a_admin"].post("/projects/new", {"name": evil, "status": "Active"})
    w["a_admin"].post("/staff/new", {"name": evil, "username": "evil", "role": "staff", "active": "1",
                                     "onboarding": "password", "password": "TempPass123"})
    for url in ("/projects", "/staff", "/leads/new"):
        html = w["a_admin"].get(url).get_data(as_text=True)
        assert "<script>alert(2)" not in html and "confirm('" not in html, url
