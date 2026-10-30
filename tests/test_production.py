"""Production module + module switches: converted lead -> Queue order, stages (maker, GST bill at dispatch,
delivered), expected dates, warranty with parts, customer link, unit codes + referral points, stage sets,
production sheet, Excel export / import of old deliveries, staff permissions and tenant isolation.

Run:  python -m pytest -q tests/test_production.py
"""
import io
import re
import sqlite3
from datetime import date, timedelta

import pytest

from test_crm import PW, Browser, crm, crm_db  # noqa: F401

import production


@pytest.fixture()
def pw(tmp_path, monkeypatch):
    db_path = str(tmp_path / "crm.db")
    monkeypatch.setattr(crm, "DB_PATH", db_path)
    crm._failed_logins.clear()
    crm_db.migrate(db_path)
    sup = Browser()
    sup.post("/setup", {"agency": "Real View", "name": "Owner", "username": "super", "password": PW})

    def make_org(name, code, admin_user, **mods):
        data = {"name": name, "code": code, "admin_name": f"{name} Admin", "admin_username": admin_user,
                "onboarding": "password", "password": "TempPass123", "mods_form": "1"}
        data.update({f"mod_{k}": "1" for k, v in mods.items() if v})
        r = sup.post("/admin/organizations/new", data)
        assert f"created with admin {admin_user}" in r.text, r.text[:2000]
        b = Browser()
        b.login(admin_user, "TempPass123")
        b.post("/account", {"current_password": "TempPass123", "new_password": PW, "confirm": PW})
        return b

    W = dict(sup=sup, db_path=db_path)
    W["star"] = make_org("Starway Tech", "STAR", "star_admin", crm=1, prod=1)
    W["only"] = make_org("Crm Only", "ONLY", "only_admin", crm=1)
    W["pure"] = make_org("Factory", "FACT", "fact_admin", prod=1)

    def db():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c
    W["db"] = db
    W["oid"] = lambda code: db().execute("SELECT id FROM organizations WHERE code = ?", (code,)).fetchone()[0]
    return W


def project_id(b):
    r = b.post("/projects/new", {"name": "Machines", "status": "Active"})
    m = re.search(r'/leads\?project=(\d+)', r.text) or re.search(r'project=(\d+)', r.text)
    return int(m.group(1))


def add_lead(b, pid, name="Shreesalasar Agro", phone="9876543210", status="New", **extra):
    data = {"project_id": pid, "customer_name": name, "phone": phone, "whatsapp": phone, "status": status,
            "country": "India", "place": "Kolkata", "state": "West Bengal", "confirm_duplicate": "1"}
    data.update(extra)
    return b.post("/leads/new", data)


def orders(W, code):
    return W["db"]().execute("""SELECT o.*, s.name stage FROM prod_orders o JOIN prod_stages s ON s.id = o.stage_id
                                WHERE o.org_id = ? ORDER BY o.id""", (W["oid"](code),)).fetchall()


def stage_id(W, code, name):
    return W["db"]().execute("SELECT id FROM prod_stages WHERE org_id = ? AND name = ? AND active = 1",
                             (W["oid"](code), name)).fetchone()[0]


# ---------------------------------------------------------------- module switches

def test_migration_keeps_modules_and_production_starts_off(pw):
    c = pw["db"]()
    rows = {r["code"]: r for r in c.execute("SELECT * FROM organizations")}
    assert c.execute("PRAGMA user_version").fetchone()[0] == crm_db.SCHEMA_VERSION == 17
    assert rows["STAR"]["mod_crm"] == 1 and rows["STAR"]["mod_prod"] == 1 and rows["STAR"]["mod_cafe"] == 0
    assert rows["ONLY"]["mod_prod"] == 0 and rows["ONLY"]["app_type"] == "crm"
    assert rows["FACT"]["mod_crm"] == 0 and rows["FACT"]["mod_prod"] == 1


def test_module_switch_and_menus_never_mix(pw):
    star = pw["star"]
    html = star.get("/").text
    assert "💼 CRM" in html and "🏭 Production" in html          # two buttons in the top bar
    assert "Today's Leads" in html and "📋 Orders" not in html     # CRM menu only
    r = star.post("/module/prod")
    assert "📋 Orders" in r.text and "Today's Leads" not in r.text   # Production menu only
    assert "🏭 Production" in r.text and "mod-prod" in r.text
    # the module opened last comes back after logging in again
    b = Browser()
    b.login("star_admin")
    assert "mod-prod" in b.get("/", follow_redirects=True).text
    # a CRM-only client has no switch and no Production
    only = pw["only"]
    assert "mode-switch" not in only.get("/").text
    assert only.get("/production").status_code == 404
    # a Production-only client never sees CRM pages
    pure = pw["pure"]
    r = pure.get("/leads")
    assert r.status_code == 302 and r.headers["Location"].endswith("/production")


def test_super_admin_switches_modules_off_without_losing_data(pw):
    star, sup = pw["star"], pw["sup"]
    star.post("/production/orders/new", {"name": "A Customer", "phone": "9000000001", "product_name": "Pump",
                                         "quantity": "1", "unit_price": "100"})
    oid = pw["oid"]("STAR")
    sup.post(f"/admin/organizations/{oid}/edit", {"name": "Starway Tech", "code": "STAR", "mods_form": "1",
                                                  "mod_crm": "1"})
    assert star.get("/production").status_code == 404
    assert len(orders(pw, "STAR")) == 1                  # data kept
    r = sup.post(f"/admin/organizations/{oid}/edit", {"name": "Starway Tech", "code": "STAR", "mods_form": "1"})
    assert "Tick at least one module" in r.text
    sup.post(f"/admin/organizations/{oid}/edit", {"name": "Starway Tech", "code": "STAR", "mods_form": "1",
                                                  "mod_crm": "1", "mod_prod": "1"})
    assert "A Customer" in star.get("/production/orders").text


# ---------------------------------------------------------------- converted lead -> Queue

def test_converted_lead_goes_to_queue_once(pw):
    star = pw["star"]
    pid = project_id(star)
    r = add_lead(star, pid, status="Converted")
    assert "sent to Production (Queue)" in r.text
    o = orders(pw, "STAR")
    assert len(o) == 1 and o[0]["stage"] == "Queue" and o[0]["lead_id"]
    lid = o[0]["lead_id"]
    # converted again (status change back and forth) -> still one order
    star.post(f"/leads/{lid}/status", {"status": "Negotiation"})
    star.post(f"/leads/{lid}/status", {"status": "Converted"})
    assert len(orders(pw, "STAR")) == 1
    # quick status in a list converts too
    r = add_lead(star, pid, name="Second Co", phone="9123456780")
    lid2 = max(l["id"] for l in pw["db"]().execute("SELECT id FROM leads"))
    star.post(f"/leads/{lid2}/status", {"status": "Converted"})
    assert len(orders(pw, "STAR")) == 2
    # lead page has "Open order", order page has "Open lead"
    html = star.get(f"/leads/{lid}").text
    assert "Open order #1" in html
    assert "Open lead" in star.get(f"/production/orders/{o[0]['id']}").text
    # a CRM-only client converting a lead makes no order
    only = pw["only"]
    add_lead(only, project_id(only), name="X", phone="9999911111", status="Converted")
    assert orders(pw, "ONLY") == []


def test_old_converted_leads_can_be_brought_in(pw):
    star, sup = pw["star"], pw["sup"]
    oid = pw["oid"]("STAR")
    sup.post(f"/admin/organizations/{oid}/edit", {"name": "Starway Tech", "code": "STAR", "mods_form": "1",
                                                  "mod_crm": "1"})
    pid = project_id(star)
    add_lead(star, pid, status="Converted")
    assert orders(pw, "STAR") == []
    sup.post(f"/admin/organizations/{oid}/edit", {"name": "Starway Tech", "code": "STAR", "mods_form": "1",
                                                  "mod_crm": "1", "mod_prod": "1"})
    html = star.get("/production").text
    assert "already \"Converted\"" in html
    star.post("/production/convert-leads")
    assert len(orders(pw, "STAR")) == 1


# ---------------------------------------------------------------- stages, maker, bill, delivered

def test_full_flow_maker_bill_warranty_points(pw):
    star = pw["star"]
    # referrer customer (delivered earlier, imported) -> has a unit code
    star.post("/production/orders/new", {"name": "Referrer", "phone": "9000000009", "product_name": "Gate",
                                         "quantity": "1", "unit_price": "5000"})
    ref_code = pw["db"]().execute("SELECT unit_code FROM prod_customers WHERE name = 'Referrer'").fetchone()[0]
    assert re.fullmatch(r"U\d{5}", ref_code)
    r = star.post("/production/orders/new", {"name": "Shreesalasar Agro", "phone": "9876543210",
                                             "address": "West Bengal", "product_name": "Vertical Electric with Stand",
                                             "category": "Crompton 7.5", "machine_type": "New",
                                             "power_source": "Electric", "quantity": "1", "unit_price": "14000",
                                             "prod_days": "10", "approved_by": "Abdul Salam",
                                             "referral_code": ref_code.lower(),
                                             "adv_date": [date.today().isoformat()], "adv_amount": ["2000"],
                                             "adv_note": ["UPI"],
                                             "part_name": ["Motor"], "part_company": ["Crompton"], "part_months": ["24"],
                                             "part_serial": ["CR1"]})
    assert "Order saved in the Queue" in r.text
    o = orders(pw, "STAR")[-1]
    assert o["stage"] == "Queue" and o["referrer_id"]
    # Queue -> In Production needs the maker: one tap opens the small form
    r = star.post(f"/production/orders/{o['id']}/move", {"to": stage_id(pw, "STAR", "In Production")})
    assert "Who makes it?" in r.text
    r = star.post(f"/production/orders/{o['id']}/move", {"to": stage_id(pw, "STAR", "In Production"), "confirm": "1"})
    assert "Choose who makes it" in r.text
    start = date.today().isoformat()
    r = star.post(f"/production/orders/{o['id']}/move", {"to": stage_id(pw, "STAR", "In Production"), "confirm": "1",
                                                         "new_maker": "Babu", "new_maker_phone": "9000011111",
                                                         "mfg_start": start, "exp_finish": "", "maker_amount": ""})
    assert "→ In Production" in r.text
    o = orders(pw, "STAR")[-1]
    assert o["maker_id"] and o["maker_amount"] is None
    assert o["exp_finish"] == (date.today() + timedelta(days=10)).isoformat()
    # maker amount can be added later
    star.post(f"/production/orders/{o['id']}/maker-amount", {"maker_amount": "3500", "maker_id": o["maker_id"]})
    assert orders(pw, "STAR")[-1]["maker_amount"] == 350000
    # production sheet: maker amount yes, customer price / advance no
    sheet = star.get(f"/production/orders/{o['id']}/sheet").text
    assert "₹3,500" in sheet and "Babu" in sheet and "Abdul Salam" in sheet and "14,000" not in sheet
    assert "2,000" not in sheet and "Advance payments" in sheet
    # In Production -> Ready (one tap, nothing asked)
    star.post(f"/production/orders/{o['id']}/move", {"to": stage_id(pw, "STAR", "Ready for Dispatch")})
    # Ready -> Dispatch asks the GST bill number
    r = star.post(f"/production/orders/{o['id']}/move", {"to": stage_id(pw, "STAR", "Dispatch")})
    assert "GST bill no" in r.text
    bill_day = (date.today() - timedelta(days=5)).isoformat()
    r = star.post(f"/production/orders/{o['id']}/move", {"to": stage_id(pw, "STAR", "Dispatch"), "confirm": "1",
                                                         "bill_no": "GST/26/101", "bill_date": bill_day})
    assert "→ Dispatch" in r.text
    # Dispatch -> Delivered: referral points to the code owner
    star.post(f"/production/orders/{o['id']}/move", {"to": stage_id(pw, "STAR", "Delivered")})
    c = pw["db"]()
    assert c.execute("SELECT points FROM prod_customers WHERE unit_code = ?", (ref_code,)).fetchone()[0] == 1
    o = orders(pw, "STAR")[-1]
    assert o["points_given"] == 1 and o["delivered_date"] == date.today().isoformat()
    # customer link: warranty page from the bill date, parts too, never the maker
    page = Browser().get(f"/t/star/{o['token']}").text
    days_left = (date.fromisoformat(bill_day).replace(year=date.fromisoformat(bill_day).year + 1)
                 - timedelta(days=1) - date.today()).days
    assert "days of warranty left" in page and f">{days_left}<" in page
    assert "Motor" in page and "Crompton" in page and "GST/26/101" in page
    assert "Babu" not in page and "3,500" not in page
    # moving back from Delivered takes the points back
    star.post(f"/production/orders/{o['id']}/move", {"to": stage_id(pw, "STAR", "Dispatch")})
    assert c.execute("SELECT points FROM prod_customers WHERE unit_code = ?", (ref_code,)).fetchone()[0] == 0
    # the same bill number cannot be used twice
    r = star.post("/production/orders/new", {"name": "Z", "phone": "9000000077", "product_name": "Pump"})
    zid = orders(pw, "STAR")[-1]["id"]
    star.post(f"/production/orders/{zid}/move", {"to": stage_id(pw, "STAR", "In Production"), "confirm": "1",
                                                 "maker_id": o["maker_id"]})
    r = star.post(f"/production/orders/{zid}/move", {"to": stage_id(pw, "STAR", "Dispatch"), "confirm": "1",
                                                     "bill_no": "GST/26/101"})
    assert "already on another order" in r.text


def test_redeem_points_and_cancel_gives_no_points(pw):
    star = pw["star"]
    star.post("/production/orders/new", {"name": "Owner", "phone": "9000000001", "product_name": "Gate"})
    code = pw["db"]().execute("SELECT unit_code FROM prod_customers WHERE name = 'Owner'").fetchone()[0]
    star.post("/production/orders/new", {"name": "Friend", "phone": "9000000002", "product_name": "Gate",
                                         "referral_code": code})
    fid = orders(pw, "STAR")[-1]["id"]
    star.post(f"/production/orders/{fid}/cancel", {"reason": "Customer changed mind"})
    assert pw["db"]().execute("SELECT points FROM prod_customers WHERE unit_code = ?", (code,)).fetchone()[0] == 0
    # settings: 2 points per referral, ₹1000 per point
    star.post("/production/settings", {"customer_link": "1", "warranty": "1", "parts": "1", "referral": "1",
                                       "referrer_points": "2", "new_points": "0", "point_value": "1000",
                                       "default_warranty": "12", "default_days": "7", "sheet_title": "PRODUCTION ORDER"})
    star.post("/production/orders/new", {"name": "Friend2", "phone": "9000000003", "product_name": "Gate",
                                         "referral_code": code})
    o = orders(pw, "STAR")[-1]
    mk = star.post(f"/production/orders/{o['id']}/move", {"to": stage_id(pw, "STAR", "Delivered"), "confirm": "1",
                                                          "new_maker": "Ravi", "bill_no": "B-1"})
    assert "→ Delivered" in mk.text
    c = pw["db"]()
    cid, pts = c.execute("SELECT id, points FROM prod_customers WHERE unit_code = ?", (code,)).fetchone()
    assert pts == 2
    r = star.post(f"/production/customers/{cid}", {"action": "redeem", "points": "2", "amount": "", "pay_mode": "UPI",
                                                   "pay_date": date.today().isoformat(), "note": "ref 123"})
    assert "Redeemed 2 points (₹2,000 paid)" in r.text
    assert c.execute("SELECT points FROM prod_customers WHERE id = ?", (cid,)).fetchone()[0] == 0
    tok = c.execute("SELECT token FROM prod_orders WHERE customer_id = ? ORDER BY id LIMIT 1", (cid,)).fetchone()[0]
    page = Browser().get(f"/t/star/{tok}").text
    assert "Redeemed · ₹2,000 paid" in page and code in page


# ---------------------------------------------------------------- expected dates

def test_queue_dates_follow_what_is_really_in_production(pw):
    star = pw["star"]
    for i in range(3):
        star.post("/production/orders/new", {"name": f"C{i}", "phone": f"900000010{i}", "product_name": "Pump",
                                             "prod_days": "10"})
    ids = [o["id"] for o in orders(pw, "STAR")]
    with crm.app.test_request_context():
        from flask import g
        g.org = pw["db"]().execute("SELECT * FROM organizations WHERE code = 'STAR'").fetchone()
        sc = production.schedule(g.org["id"])
    t = date.today()
    # nothing in production: one after another
    assert [sc[i]["finish"] for i in ids] == [t + timedelta(days=10 * n) for n in (1, 2, 3)]
    # two orders put into production -> two places; the third starts when the first place frees up
    for i in ids[:2]:
        star.post(f"/production/orders/{i}/move", {"to": stage_id(pw, "STAR", "In Production"), "confirm": "1",
                                                   "new_maker": "Babu"})
    with crm.app.test_request_context():
        from flask import g
        g.org = pw["db"]().execute("SELECT * FROM organizations WHERE code = 'STAR'").fetchone()
        sc = production.schedule(g.org["id"])
    assert sc[ids[2]]["start"] == t + timedelta(days=10) and sc[ids[2]]["finish"] == t + timedelta(days=20)
    page = Browser().get(f"/t/star/{orders(pw, 'STAR')[2]['token']}").text
    assert "Expected dispatch" in page and (t + timedelta(days=20)).strftime("%d-%m-%Y") in page


# ---------------------------------------------------------------- stage sets

def test_stage_set_editing_only_changes_that_set(pw):
    star = pw["star"]
    star.get("/production")
    c = pw["db"]()
    oid = pw["oid"]("STAR")
    sid = c.execute("SELECT id FROM prod_stage_sets WHERE org_id = ?", (oid,)).fetchone()[0]
    st = c.execute("SELECT * FROM prod_stages WHERE set_id = ? ORDER BY sort", (sid,)).fetchall()
    star.post("/production/orders/new", {"name": "Q", "phone": "9000000100", "product_name": "Pump"})
    # removing the Queue stage (it has an order) is refused
    r = star.post(f"/production/stage-sets/{sid}", {"set_name": "Standard",
                                                    "stage_id": [str(s["id"]) for s in st[1:]],
                                                    "stage_name": [s["name"] for s in st[1:]],
                                                    "stage_mark": [s["mark"] or "" for s in st[1:]]})
    assert "has 1 order(s)" in r.text
    # rename Queue -> Waiting and add Painting before Ready
    ids = [str(s["id"]) for s in st]
    names = [s["name"] for s in st]
    marks = [s["mark"] or "" for s in st]
    ids.insert(2, "")
    names.insert(2, "Painting")
    marks.insert(2, "")
    names[0] = "Waiting"
    r = star.post(f"/production/stage-sets/{sid}", {"set_name": "Standard", "stage_id": ids, "stage_name": names,
                                                    "stage_mark": marks})
    assert "Only this set changed" in r.text
    assert orders(pw, "STAR")[0]["stage"] == "Waiting"
    # a second set is independent
    star.post("/production/stage-sets/new", {"name": "Gates"})
    sid2 = c.execute("SELECT id FROM prod_stage_sets WHERE org_id = ? AND name = 'Gates'", (oid,)).fetchone()[0]
    assert c.execute("SELECT name FROM prod_stages WHERE set_id = ? ORDER BY sort LIMIT 1", (sid2,)).fetchone()[0] == "Queue"
    # marks must be valid
    r = star.post(f"/production/stage-sets/{sid2}", {"set_name": "Gates", "stage_id": ["", ""],
                                                     "stage_name": ["A", "B"], "stage_mark": ["", ""]})
    assert "Mark the stage where production starts" in r.text


# ---------------------------------------------------------------- Excel

def test_export_and_import_old_deliveries(pw):
    star = pw["star"]
    from openpyxl import load_workbook, Workbook
    tpl = star.get("/production/import/template.xlsx")
    assert tpl.status_code == 200
    wb = load_workbook(io.BytesIO(tpl.data))
    assert wb.sheetnames[:2] == ["Deliveries", "Parts"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Deliveries"
    ws.append(["Customer name", "Phone", "WhatsApp", "Address / Place", "Product", "Category", "Machine type",
               "Power source", "Weight", "Quantity", "Unit price", "Order date", "Delivered date", "GST bill no",
               "Bill date", "Warranty months", "Maker", "Note"])
    ws.append(["Old One", "9000000501", "", "Calicut", "Pump", "", "New", "Electric", "", 1, 12000, "2025-01-02",
               "2025-01-20", "OLD-1", "18-01-2025", 24, "Babu", ""])
    ws.append(["Old One", "9000000501", "", "Calicut", "Gate", "", "", "", "", 1, "", "", "", "OLD-2", "2025-06-01",
               "", "", ""])
    ws.append(["", "9000000502", "", "", "Pump", "", "", "", "", 1, "", "", "", "OLD-3", "2025-06-01", "", "", ""])
    ps = wb.create_sheet("Parts")
    ps.append(["GST bill no", "Part name", "Company", "Warranty months", "Serial no"])
    ps.append(["OLD-1", "Motor", "Crompton", 36, "M1"])
    buf = io.BytesIO()
    wb.save(buf)
    r = star.c.post("/production/import", data={"csrf_token": star.token(),
                                                "sheet": (io.BytesIO(buf.getvalue()), "old.xlsx")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "customer name is empty" in r.text and "Import 2 deliveries" in r.text
    tok = re.search(r'name="tok" value="([0-9a-f]{24})"', r.text).group(1)
    r = star.post("/production/import/confirm", {"tok": tok})
    assert "2 old deliveries imported" in r.text
    o = orders(pw, "STAR")
    assert [x["stage"] for x in o] == ["Delivered", "Delivered"] and all(x["imported"] for x in o)
    assert o[0]["bill_date"] == "2025-01-18" and o[0]["warranty_months"] == 24
    assert len({x["customer_id"] for x in o}) == 1          # same phone = one customer
    c = pw["db"]()
    assert c.execute("SELECT name, months FROM prod_parts WHERE order_id = ?", (o[0]["id"],)).fetchone()[:] == ("Motor", 36)
    assert c.execute("SELECT COUNT(*) FROM prod_makers WHERE name = 'Babu'").fetchone()[0] == 1
    # uploading again: same bill numbers are skipped
    r = star.c.post("/production/import", data={"csrf_token": star.token(),
                                                "sheet": (io.BytesIO(buf.getvalue()), "old.xlsx")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "Already in the software" in r.text and "Import 2" not in r.text
    # imported orders don't count in the queue estimate and gave no points
    assert all(x["points_given"] == 0 for x in orders(pw, "STAR"))
    # export: one workbook with all sheets
    r = star.get("/production/export.xlsx")
    wb = load_workbook(io.BytesIO(r.data))
    assert {"Orders", "Stage history", "Parts warranty", "Customers", "Points history", "Makers"} <= set(wb.sheetnames)
    rows = list(wb["Orders"].iter_rows(values_only=True))
    assert rows[1][2] == "Old One" and "OLD-1" in rows[1]


# ---------------------------------------------------------------- staff & isolation

def make_staff(admin, username, **extra):
    data = {"name": username.title(), "username": username, "role": "staff", "active": "1", "onboarding": "password",
            "password": "TempPass123", "mods_form": "1", "uses_crm": "1"}
    data.update(extra)
    r = admin.post("/staff/new", data)
    assert "created" in r.text, r.text[:1500]
    b = Browser()
    b.login(username, "TempPass123")
    b.post("/account", {"current_password": "TempPass123", "new_password": PW, "confirm": PW})
    return b


def test_staff_permissions(pw):
    star = pw["star"]
    star.post("/production/orders/new", {"name": "Cust", "phone": "9000000600", "product_name": "Pump"})
    oid_ = orders(pw, "STAR")[0]["id"]
    sales = make_staff(star, "sales1")                      # no production ticks
    assert "🏭 Production" not in sales.get("/").text
    assert sales.get("/production").status_code == 404
    worker = make_staff(star, "worker1", prod_role="worker")
    assert "🏭 Production" in worker.get("/").text
    worker.post("/module/prod")
    html = worker.get(f"/production/orders/{oid_}").text
    assert "Cust" in html and "Edit details" not in html
    assert worker.get("/production/makers").status_code == 403
    assert worker.get("/production/export.xlsx").status_code == 403
    floor = make_staff(star, "floor1", prod_role="worker", uses_crm="")   # Production only
    r = floor.get("/leads")
    assert r.status_code == 302 and "/production" in r.headers["Location"]
    assert "mode-switch" not in floor.get("/production").text


def test_tenant_isolation(pw):
    star, pure = pw["star"], pw["pure"]
    star.post("/production/orders/new", {"name": "Secret", "phone": "9000000700", "product_name": "Pump"})
    o = orders(pw, "STAR")[0]
    assert pure.get(f"/production/orders/{o['id']}").status_code == 404
    assert "Secret" not in pure.get("/production/orders?show=all").text
    assert Browser().get(f"/t/fact/{o['token']}").status_code == 404
    assert Browser().get(f"/t/star/{o['token']}").status_code == 200
    assert Browser().get("/t/star/not-a-real-token-123").status_code == 404
    # the database refuses an order pointing at another organization's customer
    c = sqlite3.connect(pw["db_path"])
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("""INSERT INTO prod_orders (org_id, order_no, customer_id, set_id, stage_id, order_date, token,
                     created_at) VALUES (?, 99, ?, ?, ?, '2026-01-01', 'x', '2026-01-01')""",
                  (pw["oid"]("FACT"), o["customer_id"], o["set_id"], o["stage_id"]))


# ---------------------------------------------------------------- round 2: queue priority, move to top, maker page

def queue_positions(W):
    with crm.app.test_request_context():
        from flask import g
        g.org = W["db"]().execute("SELECT * FROM organizations WHERE code = 'STAR'").fetchone()
        sc = production.schedule(g.org["id"])
    names = {o["id"]: W["db"]().execute("SELECT name FROM prod_customers WHERE id = ?", (o["customer_id"],)).fetchone()[0]
             for o in orders(W, "STAR")}
    return [names[i] for i, _ in sorted(((i, v) for i, v in sc.items() if v["phase"] == "queue"),
                                        key=lambda t: t[1]["pos"])]


def test_queue_priority_first_advance_then_pin(pw):
    star = pw["star"]
    t = date.today()
    # A ordered first but no advance; B paid an advance 3 days ago; C paid 10 days ago (small amount is enough)
    star.post("/production/orders/new", {"name": "A", "phone": "9000000801", "product_name": "Pump"})
    star.post("/production/orders/new", {"name": "B", "phone": "9000000802", "product_name": "Pump",
                                         "adv_date": [(t - timedelta(days=3)).isoformat()], "adv_amount": ["5000"]})
    star.post("/production/orders/new", {"name": "C", "phone": "9000000803", "product_name": "Pump",
                                         "adv_date": [(t - timedelta(days=10)).isoformat()], "adv_amount": ["100"]})
    assert queue_positions(pw) == ["C", "B", "A"]
    html = star.get("/production/orders").text
    assert html.index("<b>C</b>") < html.index("<b>B</b>") < html.index("<b>A</b>")
    assert "No advance" in html and "Advance " in html
    a_id = orders(pw, "STAR")[0]["id"]
    # only the Admin can move to top; a reason is required
    worker = make_staff(star, "worker2", prod_role="manager")
    assert worker.post(f"/production/orders/{a_id}/pin", {"reason": "urgent"}).status_code == 403
    r = star.post(f"/production/orders/{a_id}/pin", {"reason": ""})
    assert "Write why" in r.text
    r = star.post(f"/production/orders/{a_id}/pin", {"reason": "Harvest season"})
    assert "moved to the top" in r.text and "Harvest season" in star.get(f"/production/orders/{a_id}").text
    assert queue_positions(pw) == ["A", "C", "B"]
    star.post(f"/production/orders/{a_id}/pin", {"action": "unpin"})
    assert queue_positions(pw) == ["C", "B", "A"]
    # a new advance re-orders by itself: A pays an advance 20 days back-dated? (entered today -> after C and B)
    star.post(f"/production/orders/{a_id}/edit", {"name": "A", "phone": "9000000801", "product_name": "Pump",
                                                   "quantity": "1", "adv_date": [t.isoformat()], "adv_amount": ["1000"]})
    assert queue_positions(pw) == ["C", "B", "A"]


def test_maker_page_counts_month_production_finished(pw):
    star = pw["star"]
    for i in range(3):
        star.post("/production/orders/new", {"name": f"M{i}", "phone": f"900000090{i}", "product_name": "Pump"})
    ids = [o["id"] for o in orders(pw, "STAR")]
    for i in ids:
        star.post(f"/production/orders/{i}/move", {"to": stage_id(pw, "STAR", "In Production"), "confirm": "1",
                                                   "new_maker": "Babu", "maker_amount": "2000"})
    mid = orders(pw, "STAR")[0]["maker_id"]
    for i in ids[:2]:
        star.post(f"/production/orders/{i}/move", {"to": stage_id(pw, "STAR", "Ready for Dispatch")})
    o = orders(pw, "STAR")
    assert o[0]["finished_at"] and o[1]["finished_at"] and o[2]["finished_at"] is None
    # one of them finished last month (as if)
    last = (date.today().replace(day=1) - timedelta(days=1)).isoformat() + " 10:00:00"
    c = sqlite3.connect(pw["db_path"])
    c.execute("UPDATE prod_orders SET finished_at = ? WHERE id = ?", (last, ids[0]))
    c.commit()
    html = star.get(f"/production/makers/{mid}").text
    assert "Month by month" in html and "Making now" in html
    this_m, last_m = date.today().isoformat()[:7], last[:7]
    page = star.get(f"/production/makers/{mid}?month={this_m}").text
    assert "<b>#2</b>" in page and "<b>#1</b>" not in page and "<b>#3</b>" not in page
    page = star.get(f"/production/makers/{mid}?month={last_m}").text
    assert "<b>#1</b>" in page and "<b>#2</b>" not in page
    # moving back before Ready clears the finished day
    star.post(f"/production/orders/{ids[1]}/move", {"to": stage_id(pw, "STAR", "In Production")})
    assert orders(pw, "STAR")[1]["finished_at"] is None
    assert star.get("/production/makers").text.count(f"/production/makers/{mid}") >= 1


# ---------------------------------------------------------------- round 3: maker month by bill date, order takers

def test_old_order_counts_in_bill_month_and_date_can_be_set(pw):
    star = pw["star"]
    star.post("/production/orders/new", {"name": "Late Entry", "phone": "9000001001", "product_name": "Pump"})
    oid_ = orders(pw, "STAR")[0]["id"]
    star.post(f"/production/orders/{oid_}/move", {"to": stage_id(pw, "STAR", "In Production"), "confirm": "1",
                                                  "new_maker": "Babu", "maker_amount": "2500"})
    star.post(f"/production/orders/{oid_}/move", {"to": stage_id(pw, "STAR", "Ready for Dispatch")})
    star.post(f"/production/orders/{oid_}/move", {"to": stage_id(pw, "STAR", "Dispatch"), "confirm": "1",
                                                  "bill_no": "OLD-77", "bill_date": "2026-03-18"})
    mid = orders(pw, "STAR")[0]["maker_id"]
    assert "<b>#1</b>" in star.get(f"/production/makers/{mid}?month=2026-03").text
    assert "<b>#1</b>" not in star.get(f"/production/makers/{mid}?month={date.today().isoformat()[:7]}").text
    # set by hand wins
    star.post(f"/production/orders/{oid_}/finished", {"finished": "2026-02-10"})
    assert "<b>#1</b>" in star.get(f"/production/makers/{mid}?month=2026-02").text
    # old delivery by hand with a maker counts in the bill month
    r = star.post("/production/orders/new", {"old": "1", "name": "Old Hand", "phone": "9000001002", "product_name": "Gate",
                                             "bill_no": "OLD-78", "bill_date": "2026-01-05", "maker_id": str(mid),
                                             "maker_amount": "1800"})
    assert "Old delivery saved" in r.text
    o = orders(pw, "STAR")[-1]
    assert o["maker_id"] == mid and o["maker_amount"] == 180000
    assert "<b>#2</b>" in star.get(f"/production/makers/{mid}?month=2026-01").text


def test_order_taker_pages(pw):
    star = pw["star"]
    sales = make_staff(star, "sales9")                       # CRM only, no production ticks
    sid = pw["db"]().execute("SELECT id FROM users WHERE username = 'sales9'").fetchone()[0]
    aid = pw["db"]().execute("SELECT id FROM users WHERE username = 'star_admin'").fetchone()[0]
    star.post("/production/orders/new", {"name": "By Sales", "phone": "9000001101", "product_name": "Pump",
                                         "taken_by": str(sid)})
    star.post("/production/orders/new", {"name": "By Sales Old", "phone": "9000001102", "product_name": "Pump",
                                         "taken_by": str(sid), "order_date": "2026-05-04"})
    star.post("/production/orders/new", {"name": "By Admin", "phone": "9000001103", "product_name": "Pump",
                                         "taken_by": str(aid)})
    html = star.get("/production/order-takers").text
    assert "Sales9" in html and "Order takers" in html
    page = star.get(f"/production/order-takers/{sid}?month=2026-05").text
    assert "By Sales Old" in page and "By Sales<" not in page and "₹" not in page
    # staff: own page only (also from the CRM menu), never others'
    assert "📦 My orders" in sales.get("/").text
    mine = sales.get(f"/production/order-takers/{sid}").text
    assert "By Sales" in mine and "By Admin" not in mine
    assert sales.get(f"/production/order-takers/{aid}").status_code == 403
    r = sales.get("/production/order-takers")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/production/order-takers/{sid}")
    # a CRM-only client has no such page
    assert pw["only"].get("/production/order-takers").status_code == 404


# ---------------------------------------------------------------- board

def test_board_columns_drag_and_filters(pw):
    star = pw["star"]
    star.post("/production/orders/new", {"name": "Board A", "phone": "9000001201", "product_name": "Pump"})
    star.post("/production/orders/new", {"name": "Board B", "phone": "9000001202", "product_name": "Gate"})
    a, b = [o["id"] for o in orders(pw, "STAR")]
    html = star.get("/production/board").text
    assert 'data-stage="Queue"' in html and 'data-stage="In Production"' in html
    assert 'data-stage="Delivered"' not in html               # hidden by default
    # drop on "In Production" asks the maker; drop on Ready (no form needed when coming from production) moves
    r = star.post(f"/production/orders/{a}/move", {"to_name": "In Production", "next": "/production/board"})
    assert "Who makes it?" in r.text
    star.post(f"/production/orders/{a}/move", {"to": stage_id(pw, "STAR", "In Production"), "confirm": "1",
                                               "new_maker": "Babu", "next": "/production/board"})
    r = star.post(f"/production/orders/{a}/move", {"to_name": "ready for dispatch", "next": "/production/board"})
    assert "→ Ready for Dispatch" in r.text and "Production board" in r.text
    r = star.post(f"/production/orders/{a}/move", {"to_name": "No Such Stage", "next": "/production/board"})
    assert "has no stage called" in r.text
    # filters
    assert "Board B" in star.get("/production/board?product=Gate").text
    assert "Board A" not in star.get("/production/board?product=Gate").text
    mid = orders(pw, "STAR")[0]["maker_id"]
    page = star.get(f"/production/board?maker={mid}").text
    assert "Board A" in page and "Board B" not in page
    # columns: Admin only, for everyone
    r = star.post("/production/board/columns", {"col": ["Queue", "Delivered"]})
    assert "saved for everyone" in r.text
    html = star.get("/production/board").text
    assert 'data-stage="Delivered"' in html and 'data-stage="In Production"' not in html
    assert "Tick at least one" in star.post("/production/board/columns", {}).text
    worker = make_staff(star, "bworker", prod_role="manager")
    assert worker.post("/production/board/columns", {"col": ["Queue"]}).status_code == 403
    wh = worker.get("/production/board").text
    assert 'data-stage="Delivered"' in wh and "⚙️ Columns" not in wh
