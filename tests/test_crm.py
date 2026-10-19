"""End-to-end tests for tenant isolation, roles, state-wise and product-wise reporting, and migration.

Run:  python -m pytest -q
"""
import csv
import io
import os
import json
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
    for url in ("/reports", "/staff", "/organization", "/admin/organizations"):
        assert w["a_s1"].get(url).status_code == 403, url
    # staff can export, but only their own leads
    exported = w["a_s1"].get("/leads/export").get_data(as_text=True)
    assert "Lead of S1" in exported and "Lead of S2" not in exported
    # staff can delete their own leads (to the Recycle Bin) but not another staff member's
    assert w["a_s1"].post(f"/leads/{l2}/delete").status_code == 404
    w["a_s1"].post(f"/leads/{l1}/delete")
    assert w["a_s1"].get(f"/leads/{l1}").status_code == 404
    assert w["a_s1"].get("/leads/deleted").status_code == 403  # only admins open the Recycle Bin


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


def test_super_admin_views_client_workspace_read_only(world):
    w = world
    lid = add_lead(w["a_admin"], w["proj_a"], "Private A lead", "7000000001")
    sup = w["sup"]
    # Not visible from the agency workspace by default.
    for url in ("/", "/leads", "/admin/organizations", "/states"):
        assert "Private A lead" not in sup.get(url).get_data(as_text=True), url
    assert sup.get(f"/leads/{lid}").status_code == 404
    # Only a Super Admin can open a client workspace.
    assert w["b_admin"].post(f"/admin/organizations/{w['org_a']}/view").status_code == 403
    # Open Org A: its leads, reports and staff become visible.
    r = sup.post(f"/admin/organizations/{w['org_a']}/view")
    assert "Viewing <b>Org A</b>" in r.text and "Private A lead" in r.text
    assert sup.get(f"/leads/{lid}").status_code == 200
    assert "Private A lead" in sup.get("/states/view?state=Kerala").get_data(as_text=True)
    assert "Anu Staff1" in sup.get("/staff").get_data(as_text=True)
    assert "Private A lead" in sup.get("/leads/export").get_data(as_text=True)
    # Read-only: every write and every form page is refused.
    for url, data in ((f"/leads/{lid}/status", {"status": "Lost"}), (f"/leads/{lid}/delete", {}),
                      ("/products/new", {"name": "X"}), (f"/staff/{w['uid']('a_staff1')}/toggle", {}),
                      ("/organization", {"name": "Hacked"})):
        assert sup.post(url, data).status_code == 403, url
    for url in ("/leads/new", f"/leads/{lid}/edit", "/projects/new", "/staff/new"):
        assert sup.get(url).status_code == 403, url
    with w["db"]() as conn:
        assert conn.execute("SELECT status FROM leads WHERE id = ?", (lid,)).fetchone()[0] == "New"
    # Org B's data is still not visible while viewing Org A.
    lb = add_lead(w["b_admin"], w["proj_b"], "Private B lead", "7000000002")
    assert sup.get(f"/leads/{lb}").status_code == 404
    # Exit returns to the agency workspace.
    sup.post("/admin/view/exit")
    assert "Private A lead" not in sup.get("/leads").get_data(as_text=True)
    # Org A's admin sees every visit and the export in the access log.
    log = w["a_admin"].get("/organization").get_data(as_text=True)
    assert "opened the workspace" in log and "exported leads" in log and "closed the workspace" in log
    assert "No Super Admin access recorded" in w["b_admin"].get("/organization").get_data(as_text=True)

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
    p8 = w["a_s1"].get("/leads?sort=name&dir=asc&page=8").get_data(as_text=True)
    assert len(lead_ids(p1)) == 15 and len(lead_ids(p8)) == 15 and "Page 1 of 8" in p1
    assert "Bulk 000" in p1 and "Bulk 119" in p8
    p3 = w["a_s1"].get("/leads?sort=name&dir=asc&per=50&page=3").get_data(as_text=True)
    assert len(lead_ids(p3)) == 20 and "Page 3 of 3" in p3 and "Bulk 119" in p3
    assert len(lead_ids(w["a_s1"].get("/leads?per=999").get_data(as_text=True))) == 15  # unknown size: default
    assert "Bulk 000" in w["a_s1"].get("/states/view?state=Kerala&sort=name&dir=asc").get_data(as_text=True)


# ------------------------------------------------------------------ products & product-wise report

def product_row(html, name):
    row = re.search(rf"<b>{re.escape(name)}</b>.*?</tr>", html, re.S).group(0)
    return [int(x) for x in re.findall(r'class="num">(?:<b>)?(\d+)', row)][:6]


def test_product_report_counts_and_interest_vs_purchase(world):
    w = world
    p1, p2 = w["prod_a1"], w["prod_a2"]
    l1 = add_lead(w["a_s1"], w["proj_a"], "Multi", "4000000001", products=[p1, p2])
    l2 = add_lead(w["a_s1"], w["proj_a"], "Multi again", "4000000001", products=[p1])  # same customer phone
    l3 = add_lead(w["a_s2"], w["proj_a"], "Other", "4000000003", products=[p1], extra={"lead_quality": "Cold"})
    html = w["a_admin"].get("/product-leads").get_data(as_text=True)
    # total, customers, still interested, ordered, purchased, lost
    assert product_row(html, "A Product 1") == [3, 2, 3, 0, 0, 0]
    assert product_row(html, "A Product 2") == [1, 1, 1, 0, 0, 0]

    # Converting the lead does NOT mark products as purchased.
    w["a_admin"].post(f"/leads/{l1}/status", {"status": "Converted"})
    assert product_row(w["a_admin"].get("/product-leads").get_data(as_text=True), "A Product 1")[4] == 0
    # Explicit stages are counted separately.
    w["a_admin"].post(f"/leads/{l1}/products", {f"stage_{p1}": "Purchased", f"stage_{p2}": "Ordered"})
    w["a_admin"].post(f"/leads/{l3}/status", {"status": "Lost"})
    html = w["a_admin"].get("/product-leads").get_data(as_text=True)
    assert product_row(html, "A Product 1") == [3, 2, 1, 0, 1, 1]
    assert product_row(html, "A Product 2") == [1, 1, 0, 1, 0, 0]

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
        assert c.execute("PRAGMA user_version").fetchone()[0] == crm_db.SCHEMA_VERSION
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


def test_setup_key_required_when_configured(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fresh.db")
    monkeypatch.setattr(crm, "DB_PATH", db_path)
    monkeypatch.setattr(crm, "SETUP_KEY", "s3cret-key")
    crm_db.migrate(db_path)
    b = Browser()
    assert "Setup key" in b.get("/setup").get_data(as_text=True)
    form = {"agency": "Ag", "name": "Owner", "username": "owner", "password": PW}
    r = b.post("/setup", dict(form, setup_key="wrong"))
    assert "Setup key is incorrect" in r.text
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    r = b.post("/setup", dict(form, setup_key="s3cret-key"))
    assert "Agency control panel" in r.text


def test_website_enquiry_creates_agency_lead(world, monkeypatch):
    w = world
    c = crm.app.test_client()
    form = {"name": "Web Visitor", "phone": "+91 98470 12345", "business": "Cafe X",
            "service": "Performance Ads", "message": "Need Meta ads"}
    # disabled unless origins are configured
    assert c.post("/enquiry", data=form, headers={"Origin": "https://realviewcreativestudio.in"}).status_code == 403
    monkeypatch.setattr(crm, "ENQUIRY_ORIGINS", {"https://realviewcreativestudio.in"})
    assert c.post("/enquiry", data=form, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/enquiry", data={"name": "x"}, headers={"Origin": "https://realviewcreativestudio.in"}).status_code == 400
    # a field trying to target another organization is ignored
    r = c.post("/enquiry", data=dict(form, org_id=w["org_a"], project_id=w["proj_a"]),
               headers={"Origin": "https://realviewcreativestudio.in"})
    assert r.status_code == 201 and r.headers["Access-Control-Allow-Origin"] == "https://realviewcreativestudio.in"
    with w["db"]() as conn:
        org_kind, project_code, source, creator_role = conn.execute(
            "SELECT o.kind, p.code, l.source, u.role FROM leads l JOIN organizations o ON o.id = l.org_id"
            " JOIN projects p ON p.id = l.project_id JOIN users u ON u.id = l.entered_by"
            " WHERE l.customer_name = 'Web Visitor'").fetchone()
    assert (org_kind, project_code, source, creator_role) == ("agency", "WEB", "Website", "super_admin")
    assert "Web Visitor" in w["sup"].get("/leads").get_data(as_text=True)
    assert "Web Visitor" not in w["a_admin"].get("/leads").get_data(as_text=True)
    # honeypot: pretend success, store nothing
    c.post("/enquiry", data=dict(form, name="Bot", website="http://spam"), headers={"Origin": "https://realviewcreativestudio.in"})
    with w["db"]() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads WHERE customer_name = 'Bot'").fetchone()[0] == 0
    # rate limit: 10 per hour per IP
    crm._enquiry_hits.clear()
    codes = [c.post("/enquiry", data=form, headers={"Origin": "https://realviewcreativestudio.in"}).status_code
             for _ in range(11)]
    assert codes[-1] == 429


def test_exports_follow_each_role_scope(world):
    import zipfile as zf
    w = world
    add_lead(w["a_s1"], w["proj_a"], "=HYPERLINK(evil)", "6100000001", products=[w["prod_a1"]],
             extra={"next_follow_up": "2030-01-01"})
    add_lead(w["a_s2"], w["proj_a"], "S2 customer", "6100000002")
    add_lead(w["b_admin"], w["proj_b"], "B customer", "6100000003")

    def csv_of(b, name):
        r = b.get(f"/export/{name}.csv")
        assert r.status_code == 200, (name, r.status_code)
        return r.get_data(as_text=True)

    # Staff: own leads only, and only their own user row.
    leads = csv_of(w["a_s1"], "leads")
    assert "HYPERLINK" in leads and "S2 customer" not in leads and "B customer" not in leads
    assert "'=HYPERLINK(evil)" in leads  # formula neutralised for Excel
    users = csv_of(w["a_s1"], "users")
    assert "a_staff1" in users and "a_staff2" not in users
    assert "2030-01-01" in csv_of(w["a_s1"], "followups")
    assert "Interested" in csv_of(w["a_s1"], "lead-products")
    assert w["a_s1"].get("/export/organizations.csv").status_code == 404
    # Org admin: whole org, nothing from Org B.
    leads = csv_of(w["a_admin"], "leads")
    assert "HYPERLINK" in leads and "S2 customer" in leads and "B customer" not in leads
    assert "a_staff2" in csv_of(w["a_admin"], "users") and "b_admin" not in csv_of(w["a_admin"], "users")
    assert "B Product" not in csv_of(w["a_admin"], "products")
    # Super Admin (agency workspace): every organization, with an Organization column.
    leads = csv_of(w["sup"], "leads")
    assert all(x in leads for x in ("HYPERLINK", "S2 customer", "B customer", "Org A", "Org B"))
    assert "ORGB" in csv_of(w["sup"], "organizations")
    # ZIP holds one CSV per dataset.
    r = w["sup"].get("/export/all.zip")
    names = zf.ZipFile(io.BytesIO(r.data)).namelist()
    assert "leads.csv" in names and "organizations.csv" in names and len(names) == 8
    staff_zip = zf.ZipFile(io.BytesIO(w["a_s1"].get("/export/all.zip").data))
    assert "organizations.csv" not in staff_zip.namelist()
    assert "B customer" not in staff_zip.read("leads.csv").decode("utf-8-sig")
    # Clients see the Super Admin's export in their access log.
    assert "exported" in w["a_admin"].get("/organization").get_data(as_text=True)


def test_quick_update_from_lead_list(world):
    w = world
    lid = add_lead(w["a_s1"], w["proj_a"], "Quick Lead", "6200000001", extra={"whatsapp": "6200000001"})
    with w["db"]() as conn:  # an old Hot/Warm/Cold value stays stored, untouched
        conn.execute("UPDATE leads SET lead_quality = 'Cold' WHERE id = ?", (lid,))
    page = w["a_s1"].get("/leads").get_data(as_text=True)
    assert f'href="/leads/{lid}/edit"' in page and "Lead quality" not in page and "Any quality" not in page
    # the computer table and the phone card each have their own save form, so a change on the
    # phone card is not overridden by the hidden table's value
    assert page.count(f'name="status" form="q{lid}"') == 1 and page.count(f'name="status" form="qc{lid}"') == 1
    assert f'id="q{lid}"' in page and f'id="qc{lid}"' in page
    r = w["a_s1"].post(f"/leads/{lid}/status", {"status": "Negotiation", "buying_possibility": "High",
                                                "priority": "Urgent", "lead_quality": "Hot", "next": "/leads"})
    with w["db"]() as conn:
        row = conn.execute("SELECT status, lead_quality, buying_possibility, priority, updated_by FROM leads"
                           " WHERE id = ?", (lid,)).fetchone()
        history = [a for (a,) in conn.execute("SELECT action FROM lead_activity WHERE lead_id = ?", (lid,))]
    assert row == ("Negotiation", "Cold", "High", "Urgent", w["uid"]("a_staff1"))
    assert any("Status: 'New' → 'Negotiation'" in h for h in history)
    # editing the lead keeps the stored quality too
    assert "lead_quality" not in w["a_s1"].get(f"/leads/{lid}/edit").get_data(as_text=True)
    # other staff are rejected; an off-site "next" is ignored
    assert w["a_s2"].post(f"/leads/{lid}/status", {"status": "Lost"}).status_code == 404
    r = w["a_s1"].post(f"/leads/{lid}/status", {"priority": "Low", "next": "https://evil.example/"}, follow=False)
    assert r.headers["Location"].endswith(f"/leads/{lid}")
    # follow-ups page and dashboard lists: WhatsApp, status change and edit, with their own forms
    w["a_s1"].post(f"/leads/{lid}/followups", {"due_date": "2026-01-01", "note": "call"})
    fu = w["a_s1"].get("/followups").get_data(as_text=True)
    assert f'form="qf{lid}"' in fu and f'form="qfc{lid}"' in fu and 'data-wa="916200000001"' in fu
    dash = w["a_s1"].get("/").get_data(as_text=True)
    assert f'form="qd{lid}"' in dash and f'form="qr{lid}"' in dash and f'href="/leads/{lid}/edit"' in dash
    assert 'class="orgname" href="/"' in dash
    w["a_s1"].post(f"/leads/{lid}/status", {"status": "Quotation", "next": "/followups"})
    with w["db"]() as conn:
        assert conn.execute("SELECT status FROM leads WHERE id = ?", (lid,)).fetchone()[0] == "Quotation"

def test_company_name_quotation_youtube_and_whatsapp_link(world):
    w = world
    lid = add_lead(w["a_s1"], w["proj_a"], "Rahul", "98470 12345",
                   extra={"company_name": "Rahul Traders", "status": "Quotation", "source": "YouTube",
                          "whatsapp": "098470 12345"})
    with w["db"]() as conn:
        row = conn.execute("SELECT company_name, status, source FROM leads WHERE id = ?", (lid,)).fetchone()
    assert row == ("Rahul Traders", "Quotation", "YouTube")
    page = w["a_s1"].get("/leads?q=Traders").get_data(as_text=True)
    assert "Rahul Traders" in page and 'data-wa="919847012345"' in page and 'data-name="Rahul"' in page
    assert "Rahul Traders" in w["a_s1"].get(f"/leads/{lid}").get_data(as_text=True)
    csv_text = w["a_admin"].get("/export/leads.csv").get_data(as_text=True)
    assert "Company" in csv_text.splitlines()[0] and "Rahul Traders" in csv_text
    # end date is optional for projects
    w["a_admin"].post("/projects/new", {"name": "No End", "status": "Active", "start_date": "2026-10-01"})
    with w["db"]() as conn:
        assert conn.execute("SELECT end_date FROM projects WHERE name = 'No End'").fetchone()[0] in ("", None)



def test_bulk_actions_recycle_bin_and_restore(world):
    w = world
    a = add_lead(w["a_s1"], w["proj_a"], "Bulk A", "7100000001")
    b = add_lead(w["a_s1"], w["proj_a"], "Bulk B", "7100000002")
    other = add_lead(w["a_s2"], w["proj_a"], "Other Staff", "7100000003")
    w["a_s1"].post(f"/leads/{a}/followups", {"due_date": "2026-11-01", "note": "call back"})
    # staff bulk update touches only their own leads, even if another id is sent
    w["a_s1"].post("/leads/bulk", {"ids": [str(a), str(b), str(other)], "action": "update", "status": "Quotation",
                                   "source": "YouTube", "next_follow_up": "2026-12-01",
                                   "next": "/leads"})
    with w["db"]() as conn:
        rows = dict((r[0], r[1:]) for r in conn.execute(
            "SELECT id, status, source, next_follow_up FROM leads WHERE id IN (?,?,?)", (a, b, other)))
        hist = [h for (h,) in conn.execute("SELECT action FROM lead_activity WHERE lead_id = ?", (b,))]
    assert rows[a] == ("Quotation", "YouTube", "2026-12-01") and rows[b][0] == "Quotation"
    assert rows[other][0] == "New" and any(h.startswith("Bulk update:") for h in hist)
    # admin bulk-assigns; staff cannot assign
    w["a_s1"].post("/leads/bulk", {"ids": [str(b)], "action": "update", "assigned_to": str(w["uid"]("a_staff2"))})
    with w["db"]() as conn:
        assert conn.execute("SELECT assigned_to FROM leads WHERE id = ?", (b,)).fetchone()[0] == w["uid"]("a_staff1")
    w["a_admin"].post("/leads/bulk", {"ids": [str(b)], "action": "update", "assigned_to": str(w["uid"]("a_staff2"))})
    with w["db"]() as conn:
        assert conn.execute("SELECT assigned_to FROM leads WHERE id = ?", (b,)).fetchone()[0] == w["uid"]("a_staff2")
    # export selected
    csv_text = w["a_s1"].post("/leads/bulk", {"ids": [str(a)], "action": "export"}).get_data(as_text=True)
    assert "Bulk A" in csv_text and "Bulk B" not in csv_text
    # bulk delete goes to the Recycle Bin with products, follow-ups and history
    w["a_s1"].post("/leads/bulk", {"ids": [str(a)], "action": "delete", "next": "/leads"})
    with w["db"]() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads WHERE id = ?", (a,)).fetchone()[0] == 0
        did = conn.execute("SELECT id FROM deleted_leads WHERE lead_id = ?", (a,)).fetchone()[0]
    bin_page = w["a_admin"].get("/leads/deleted").get_data(as_text=True)
    assert "Bulk A" in bin_page and "A Staff 1" in bin_page or "Bulk A" in bin_page
    assert w["b_admin"].post(f"/leads/deleted/{did}/restore").status_code == 404  # other organization
    w["a_admin"].post(f"/leads/deleted/{did}/restore")
    with w["db"]() as conn:
        row = conn.execute("SELECT customer_name, entered_by, status FROM leads WHERE id = ?", (a,)).fetchone()
        fu = conn.execute("SELECT COUNT(*) FROM follow_ups WHERE lead_id = ?", (a,)).fetchone()[0]
        hist = [h for (h,) in conn.execute("SELECT action FROM lead_activity WHERE lead_id = ?", (a,))]
        assert conn.execute("SELECT COUNT(*) FROM deleted_leads").fetchone()[0] == 0
    assert row == ("Bulk A", w["uid"]("a_staff1"), "Quotation") and fu >= 1
    assert "Restored from the Recycle Bin" in hist and any(h.startswith("Bulk update:") for h in hist)
    # permanent delete, and items older than 30 days are purged
    w["a_admin"].post(f"/leads/{a}/delete")
    with w["db"]() as conn:
        conn.execute("UPDATE deleted_leads SET deleted_at = '2000-01-01 00:00:00'")
    assert "Bulk A" not in w["a_admin"].get("/leads/deleted").get_data(as_text=True)


def wa_cfg(browser):
    page = browser.get("/leads").get_data(as_text=True)
    return json.loads(re.search(r'id="wa-config" type="application/json">(.*?)</script>', page, re.S).group(1))


def test_whatsapp_templates_and_switches(world):
    w = world
    lid = add_lead(w["a_s1"], w["proj_a"], "Tara", "9847000001", extra={"whatsapp": "9847000001", "company_name": "Tara Co"})
    assert 'data-company="Tara Co"' in w["a_s1"].get("/leads").get_data(as_text=True)
    # company templates exist but start OFF, so WhatsApp sends "Hi <name>," in one click
    assert wa_cfg(w["a_s1"])["templates"] == []
    with w["db"]() as conn:
        tids = dict(conn.execute("SELECT title, id FROM wa_templates WHERE org_id = ? AND scope = 'org'", (w["org_a"],)))
    assert len(tids) == 10
    # staff cannot change company templates, other organizations cannot either
    assert w["a_s1"].post(f"/templates/{tids['Quotation']}/toggle").status_code == 404
    assert w["b_admin"].post("/templates/save", {"id": tids["Quotation"], "title": "X", "body": "Y"}).status_code == 404
    w["a_admin"].post(f"/templates/{tids['Quotation']}/toggle")
    assert [t["title"] for t in wa_cfg(w["a_s1"])["templates"]] == ["Quotation"]  # one ON: sent in one click
    w["a_admin"].post("/templates/save", {"id": tids["Quotation"], "title": "Quote", "body": "Hi {name}, quote"})
    w["a_admin"].post(f"/templates/{tids['Welcome']}/delete")
    # staff adds a personal template (ON), visible only to them
    w["a_s1"].post("/templates/save", {"scope": "user", "title": "Demo request", "body": "Hi {name}, demo?"})
    assert [t["title"] for t in wa_cfg(w["a_s1"])["templates"]] == ["Quote", "Demo request"]
    assert [t["title"] for t in wa_cfg(w["a_s2"])["templates"]] == ["Quote"]
    with w["db"]() as conn:
        assert conn.execute("SELECT COUNT(*) FROM wa_templates WHERE id = ?", (tids["Welcome"],)).fetchone()[0] == 0
    # user switch, org switch and super admin switch each turn templates off
    w["a_s1"].post("/account", {"form": "profile", "contact_phone": "98470 11111"})
    cfg1 = wa_cfg(w["a_s1"])
    assert cfg1["templates"] == [] and cfg1["my_phone"] == "98470 11111"
    w["a_admin"].post("/organization", {"name": "Org A"})
    assert wa_cfg(w["a_s2"])["templates"] == []
    w["a_admin"].post("/organization", {"name": "Org A", "wa_templates_on": "1"})
    assert wa_cfg(w["a_s2"])["templates"] != []
    w["sup"].post(f"/admin/organizations/{w['org_a']}/edit", {"name": "Org A", "code": "ORGA"})
    assert wa_cfg(w["a_s2"])["templates"] == []
    # which message was opened is logged on the lead
    w["a_s1"].post(f"/leads/{lid}/wa-log", {"template": "Welcome"})
    with w["db"]() as conn:
        assert conn.execute("SELECT COUNT(*) FROM lead_activity WHERE lead_id = ? AND action LIKE 'WhatsApp opened%'",
                            (lid,)).fetchone()[0] == 1
    # a new client gets its own copy of the default templates, switched off
    w["sup"].post("/admin/organizations/new", {"name": "Org C", "code": "ORGC", "admin_name": "C Admin",
                                               "admin_username": "c_admin", "onboarding": "password",
                                               "password": "Passw0rd!x"})
    with w["db"]() as conn:
        oc = conn.execute("SELECT id FROM organizations WHERE code = 'ORGC'").fetchone()[0]
        assert conn.execute("SELECT COUNT(*), SUM(active) FROM wa_templates WHERE org_id = ? AND scope = 'org'",
                            (oc,)).fetchone() == (10, 0)


def big_photo():
    from PIL import Image
    img = Image.new("RGB", (4000, 3000))
    px = img.load()
    for x in range(0, 4000, 7):  # noisy content so it doesn't compress to nothing
        for y in range(0, 3000, 5):
            px[x, y] = ((x * 7) % 256, (y * 3) % 256, (x + y) % 256)
    out = io.BytesIO()
    img.save(out, "PNG")
    out.seek(0)
    return out


def test_digital_catalogue(world, tmp_path, monkeypatch):
    w = world
    monkeypatch.setenv("CRM_UPLOAD_DIR", str(tmp_path / "uploads"))
    admin = w["a_admin"]
    # product with a big photo, links and price
    with admin.c.session_transaction() as s:
        s["csrf_token"] = "t"
    r = admin.c.post("/products/new", data={
        "csrf_token": "t", "name": "Gate Motor X", "active": "1", "show_in_catalog": "1", "price": "₹24,500",
        "video_url": "https://youtu.be/abc", "brochure_url": "javascript:alert(1)",
        "images": [(big_photo(), "big.png"), (io.BytesIO(b"not an image"), "x.png")]},
        content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200
    with w["db"]() as conn:
        pid, brochure, video = conn.execute("SELECT id, brochure_url, video_url FROM products WHERE name = 'Gate Motor X'").fetchone()
        files = [f for (f,) in conn.execute("SELECT filename FROM product_images WHERE product_id = ?", (pid,))]
    assert brochure == "" and video == "https://youtu.be/abc" and len(files) == 1
    size = os.path.getsize(tmp_path / "uploads" / files[0])
    assert size < 250 * 1024, size  # a ~4000x3000 photo is stored small
    assert w["a_s1"].get(f"/media/{files[0]}").status_code == 200
    # not published yet: no public page, no catalogue link in WhatsApp buttons
    assert w["a_s1"].get("/c/orga").status_code == 404
    lid = add_lead(w["a_s1"], w["proj_a"], "Cat Lead", "9847000009", extra={"whatsapp": "9847000009"})
    assert 'data-catalog=""' in w["a_s1"].get("/leads").get_data(as_text=True)
    admin.post("/organization", {"name": "Org A", "catalog_on": "1", "catalog_about": "Gate experts", "phone": "9847011111"})
    page = w["a_s1"].get("/leads").get_data(as_text=True)
    link = re.search(r'data-catalog="([^"]+)"', page).group(1)
    assert "/c/orga?l=" in link
    # a customer (not logged in) opens the personal link and a product page
    visitor = Browser()
    path = link.split("localhost", 1)[-1]
    html = visitor.get(path).get_data(as_text=True)
    assert "Gate Motor X" in html and "Gate experts" in html and "₹24,500" in html and "javascript:" not in html
    html = visitor.get(f"/c/orga/p/{pid}?l={path.split('l=')[1]}").get_data(as_text=True)
    assert "Watch video" in html and "wa.me/919847011111" in html
    with w["db"]() as conn:
        hist = [h for (h,) in conn.execute("SELECT action FROM lead_activity WHERE lead_id = ?", (lid,))]
        views = conn.execute("SELECT COUNT(*) FROM catalog_views WHERE lead_id = ?", (lid,)).fetchone()[0]
    assert views == 2 and "📒 Customer opened the catalogue" in hist and "📒 Customer viewed Gate Motor X in the catalogue" in hist
    visitor.get(path)  # reopening within 30 minutes is not logged twice
    with w["db"]() as conn:
        assert conn.execute("SELECT COUNT(*) FROM catalog_views WHERE lead_id = ?", (lid,)).fetchone()[0] == 2
    assert "Cat Lead" in w["a_s1"].get("/").get_data(as_text=True)  # dashboard hot list
    # a forged token records nothing; another organization's catalogue never shows these products
    visitor.get(f"/c/orga?l={lid}.0000000000aa")
    with w["db"]() as conn:
        assert conn.execute("SELECT COUNT(*) FROM catalog_views").fetchone()[0] == 2
    assert w["b_admin"].post(f"/products/{pid}/images/1/delete").status_code == 404
    # Super Admin switches the catalogue off for this client
    w["sup"].post(f"/admin/organizations/{w['org_a']}/edit", {"name": "Org A", "code": "ORGA", "wa_templates_allowed": "1"})
    assert visitor.get("/c/orga").status_code == 404


# ------------------------------------------------------------------ today's leads, inline notes, list columns, template

def test_todays_leads_page_after_saving(world):
    w = world
    r = w["a_s1"].post("/leads/new", {"project_id": w["proj_a"], "customer_name": "Fresh One", "phone": "8100000001",
                                      "status": "New", "confirm_duplicate": "1"}, follow=False)
    lid = int(re.search(r"new=(\d+)", r.headers["Location"]).group(1))
    assert "/leads/today?new=" in r.headers["Location"]
    page = w["a_s1"].get(r.headers["Location"]).get_data(as_text=True)
    assert "saved" in page and "Today: 1 lead" in page and f'id="lead-{lid}" class="hl"' in page
    assert "➕ Add Lead" in page and f'href="/leads/{lid}/edit"' in page and f'form="q{lid}"' in page
    assert "Today&#39;s Leads" in page or "Today's Leads" in page  # menu
    other = add_lead(w["a_s2"], w["proj_a"], "Other Staff Lead", "8100000002")
    with w["db"]() as conn:  # an older lead is not "today"
        conn.execute("INSERT INTO leads (org_id, project_id, customer_name, phone, status, entered_by, assigned_to,"
                     " created_at, updated_at) VALUES (?,?, 'Old Lead', '8100000003', 'New', ?, ?,"
                     " '2020-01-01 10:00:00', '2020-01-01 10:00:00')",
                     (w["org_a"], w["proj_a"], w["uid"]("a_staff1"), w["uid"]("a_staff1")))
    mine = w["a_s1"].get("/leads/today").get_data(as_text=True)
    assert "Fresh One" in mine and "Other Staff Lead" not in mine and "Old Lead" not in mine and "Added by" not in mine
    # a lead assigned to staff1 but added by someone else is not in staff1's "added today"
    w["a_admin"].post("/leads/bulk", {"ids": [str(other)], "action": "update", "assigned_to": w["uid"]("a_staff1")})
    assert "Other Staff Lead" not in w["a_s1"].get("/leads/today").get_data(as_text=True)
    admin = w["a_admin"].get("/leads/today").get_data(as_text=True)
    assert "Fresh One" in admin and "Other Staff Lead" in admin and "Old Lead" not in admin
    assert "Added by" in admin and "Anu Staff1" in admin and "Today: 2 leads" in admin
    assert "Fresh One" not in w["b_admin"].get("/leads/today").get_data(as_text=True)
    # editing keeps going back to the lead page; Save & new still opens a blank form
    r = w["a_s1"].post(f"/leads/{lid}/edit", {"project_id": w["proj_a"], "customer_name": "Fresh One",
                                              "phone": "8100000001", "status": "Contacted"}, follow=False)
    assert r.headers["Location"].endswith(f"/leads/{lid}")
    r = w["a_s1"].post("/leads/new", {"project_id": w["proj_a"], "customer_name": "Fresh Two", "phone": "8100000004",
                                      "confirm_duplicate": "1", "save_and_new": "1"}, follow=False)
    assert "/leads/new" in r.headers["Location"]
    # Super Admin read-only view of the client
    w["sup"].post(f"/admin/organizations/{w['org_a']}/view")
    view = w["sup"].get("/leads/today").get_data(as_text=True)
    assert "Fresh One" in view and "lead_inline" not in view and f'action="/leads/{lid}/inline"' not in view


def test_inline_notes_and_next_follow_up(world):
    w = world
    lid = add_lead(w["a_s1"], w["proj_a"], "Note Lead", "8200000001",
                   extra={"notes": "First call", "next_follow_up": "2020-01-01", "whatsapp": "8200000001"})
    # editors on every list
    for url in ("/leads", "/leads/today", "/followups", "/", "/states/view?state=Kerala"):
        page = w["a_s1"].get(url).get_data(as_text=True)
        assert f'action="/leads/{lid}/inline"' in page and "Tomorrow" in page, url
    page = w["a_s1"].get("/leads").get_data(as_text=True)
    assert "First call" in page and 'class="overdue">📅 2020-01-01' in page
    # saving notes closes the pending follow-up and schedules the new date (quick button wins over the empty box)
    r = w["a_s1"].post(f"/leads/{lid}/inline", {"notes": "Asked for a quote", "next_follow_up": ["", "2030-05-05"],
                                                "next": "/leads"}, follow=False)
    assert r.headers["Location"].endswith("/leads")
    with w["db"]() as conn:
        notes, nxt = conn.execute("SELECT notes, next_follow_up FROM leads WHERE id = ?", (lid,)).fetchone()
        fus = conn.execute("SELECT due_date, status FROM follow_ups WHERE lead_id = ? ORDER BY id", (lid,)).fetchall()
        hist = [h for (h,) in conn.execute("SELECT action FROM lead_activity a JOIN users u ON u.id = a.user_id"
                                           " WHERE lead_id = ? AND u.username = 'a_staff1'", (lid,))]
    assert notes == "Asked for a quote" and nxt == "2030-05-05"
    assert fus == [("2020-01-01", "Done"), ("2030-05-05", "Pending")]
    assert any("Previous notes: First call" in h for h in hist)
    # changing only the date reschedules the pending follow-up
    w["a_s1"].post(f"/leads/{lid}/inline", {"next_follow_up": "2030-06-06"})
    with w["db"]() as conn:
        assert conn.execute("SELECT next_follow_up FROM leads WHERE id = ?", (lid,)).fetchone()[0] == "2030-06-06"
        assert conn.execute("SELECT COUNT(*) FROM follow_ups WHERE lead_id = ? AND status = 'Pending'",
                            (lid,)).fetchone()[0] == 1
    # notes saved without a date: follow-up done, none pending
    w["a_s1"].post(f"/leads/{lid}/inline", {"notes": "Bought elsewhere", "next_follow_up": ""})
    with w["db"]() as conn:
        assert conn.execute("SELECT next_follow_up FROM leads WHERE id = ?", (lid,)).fetchone()[0] is None
    # same permission and CSRF rules as editing the lead
    assert w["a_s2"].post(f"/leads/{lid}/inline", {"notes": "x"}).status_code == 404
    assert w["b_admin"].post(f"/leads/{lid}/inline", {"notes": "x"}).status_code == 404
    assert w["a_s1"].c.post(f"/leads/{lid}/inline", data={"notes": "no token"}).status_code == 400
    assert w["a_s1"].post(f"/leads/{lid}/inline", {"next_follow_up": "31-31-2030"}).status_code == 200
    w["sup"].post(f"/admin/organizations/{w['org_a']}/view")
    assert w["sup"].post(f"/leads/{lid}/inline", {"notes": "x"}).status_code == 403
    with w["db"]() as conn:
        assert conn.execute("SELECT notes FROM leads WHERE id = ?", (lid,)).fetchone()[0] == "Bought elsewhere"
    # admin can edit notes of any lead in the organization
    w["a_admin"].post(f"/leads/{lid}/inline", {"notes": "Admin note"})
    with w["db"]() as conn:
        assert conn.execute("SELECT notes FROM leads WHERE id = ?", (lid,)).fetchone()[0] == "Admin note"


def test_admin_chooses_list_columns(world):
    w = world
    add_lead(w["a_s1"], w["proj_a"], "Col Lead", "8300000001", extra={"email": "col@example.com"})
    page = w["a_s1"].get("/leads").get_data(as_text=True)
    heads = re.findall(r"<th[^>]*>(?:<a[^>]*>)?([A-Za-z -]+)", page.split("<tbody>")[0])
    assert [h.strip() for h in heads] == ["Phone", "Place", "Status", "Notes", "Next follow-up", "Assigned to"]
    assert "lt-wrap" in page and "hscroll" in page and 'class="name-cell' in page
    assert "⚙️ List columns" in w["a_admin"].get("/").get_data(as_text=True)
    assert "⚙️ List columns" not in w["a_s1"].get("/").get_data(as_text=True)
    assert w["a_s1"].get("/settings/list-columns").status_code == 403
    assert w["a_s1"].post("/settings/list-columns", {"col": ["email"]}).status_code == 403
    r = w["a_admin"].post("/settings/list-columns", {"col": ["email", "status", "bogus", "phone"],
                                                     "card": ["email", "phone", "place", "state", "products", "status"]})
    assert "List columns saved" in r.text
    page = w["a_s1"].get("/leads").get_data(as_text=True)
    head = page.split("<thead>")[1].split("</thead>")[0]
    assert head.index("Email") < head.index("Status") < head.index("Phone") and "Place" not in head
    assert "col@example.com" in page and 'form="q' in page
    with w["db"]() as conn:
        cols, card = conn.execute("SELECT columns, card_fields FROM org_list_settings WHERE org_id = ?",
                                  (w["org_a"],)).fetchone()
    assert json.loads(cols) == ["email", "status", "phone"] and json.loads(card) == ["email", "phone", "place", "state"]
    # other organizations keep the defaults; export and the edit form keep every field
    assert "Assigned to" in w["b_admin"].get("/leads").get_data(as_text=True).split("<tbody>")[0]
    export = w["a_s1"].get("/leads/export").get_data(as_text=True)
    assert "Place" in export and "Notes" in export
    assert 'name="place"' in w["a_s1"].get("/leads/new").get_data(as_text=True)


def test_import_template_round_trip(world):
    w = world
    r = w["a_s1"].get("/leads/import/template.csv")
    raw = r.get_data()
    assert raw.startswith("﻿".encode()) and "attachment" in r.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert len(rows) == 2
    head, example = rows
    assert head[0] == "Customer Name" and "Next Follow-up" in head and "Project" in head and "Lead Quality" not in head
    ex = dict(zip(head, example))
    assert "delete this row" in ex["Customer Name"] and ex["Project"] == "A Campaign" and ex["Products"] == "A Product 1"
    assert ex["Status"] in crm.LEAD_STATUSES and ex["Source"] in crm.LEAD_SOURCES
    assert ex["Priority"] in crm.PRIORITIES and ex["Buying Possibility"] in crm.BUYING_POSSIBILITIES
    # every template header is understood by the importer
    known = {a for aliases in crm.CSV_ALIASES.values() for a in aliases}
    assert all(h.lower() in known for h in head)
    help_page = w["a_s1"].get("/leads/import").get_data(as_text=True)
    assert "⬇️ Download template" in help_page and "YYYY-MM-DD" in help_page and "Quotation" in help_page
    # the template as downloaded: the example row is skipped, nothing fails
    r = w["a_s1"].post("/leads/import", {"project_id": w["proj_a"], "file": (io.BytesIO(raw), "template.csv")},
                       content_type="multipart/form-data")
    assert "Imported 0 lead(s)" in r.text and "Skipped" not in r.text
    # a file made from the template: fill a row like the example
    real = dict(ex, **{"Customer Name": "Template Customer", "Phone": "8400000001", "Next Follow-up": "2030-02-03"})
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(head)
    writer.writerow(example)
    writer.writerow([real[h] for h in head])
    r = w["a_s1"].post("/leads/import", {"project_id": w["proj_a"],
                                         "file": (io.BytesIO(("﻿" + out.getvalue()).encode()), "mine.csv")},
                       content_type="multipart/form-data")
    assert "Imported 1 lead(s)" in r.text and "Skipped" not in r.text
    with w["db"]() as conn:
        row = conn.execute("""SELECT l.project_id, l.status, l.source, l.state, l.next_follow_up, l.notes,
                                (SELECT COUNT(*) FROM lead_products lp WHERE lp.lead_id = l.id),
                                (SELECT COUNT(*) FROM leads WHERE customer_name LIKE '%delete this row%')
                              FROM leads l WHERE customer_name = 'Template Customer'""").fetchone()
    assert row == (w["proj_a"], ex["Status"], ex["Source"], "Kerala", "2030-02-03", ex["Notes"], 1, 0)
