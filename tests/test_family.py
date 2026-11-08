"""Family module: family start (root couple + branches), public registration (This is me / Son, Daughter, Husband,
Wife of ...), approval, tree + relation text, fee by age, accounts, permissions, merge, Clear all, tenant isolation.

Run:  python -m pytest -q tests/test_family.py
"""
import re

import pytest

from test_crm import PW, Browser, crm, crm_db  # noqa: F401

import family

HEADS = [("Moideenkutti", "son"), ("Ayishumma", "daughter"), ("Kadhiyamu", "daughter"), ("Pathummu", "daughter"),
         ("Abdul Rahman", "son"), ("Maimoona", "daughter"), ("Sakeena", "daughter")]


@pytest.fixture()
def fw(tmp_path, monkeypatch):
    db_path = str(tmp_path / "crm.db")
    monkeypatch.setattr(crm, "DB_PATH", db_path)
    crm._failed_logins.clear()
    family._hits.clear()
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

    W = dict(sup=sup, db_path=db_path, make_org=make_org)
    W["fam"] = make_org("KMH Family", "KMH", "kmh_admin", fam=1)
    W["other"] = make_org("Other Family", "OTH", "oth_admin", fam=1)
    return W


def db(W):
    return crm_db.connect(W["db_path"])


def member(W, name, org="KMH"):
    return db(W).execute("""SELECT m.* FROM fam_members m JOIN organizations o ON o.id = m.org_id
                            WHERE o.code = ? AND m.name = ? ORDER BY m.id DESC""", (org, name)).fetchone()


def start(b):
    data = {"what": "start", "root_h": "KUNJIMUHAMMED HAJI", "root_h_late": "1", "root_w": "THAYUMMA",
            "root_w_late": "1", "head_name": [h for h, _ in HEADS], "head_rel": [r for _, r in HEADS]}
    return b.post("/family/settings", data)


def register(W, **kw):
    pub = kw.pop("browser", None) or Browser()
    data = {"age": "30", "phone": "9876543210", "wa_same": "1", "attending": "1"}
    data.update(kw)
    return pub.post("/f/kmh/register", data)


def test_module_switch_and_start(fw):
    b = fw["fam"]
    r = b.get("/", follow_redirects=True)
    assert "Start the family tree first" in r.text
    r = start(b)
    assert "Family tree started: KUNJIMUHAMMED HAJI &amp; THAYUMMA with 7 children" in r.text
    c = db(fw)
    assert c.execute("SELECT COUNT(*) FROM fam_members").fetchone()[0] == 9
    assert c.execute("SELECT COUNT(*) FROM fam_members WHERE registered = 0").fetchone()[0] == 7
    # Existing orgs (agency) keep Family off
    assert c.execute("SELECT mod_fam FROM organizations WHERE kind = 'agency'").fetchone()[0] == 0
    tree = Browser().get("/f/kmh")
    assert tree.status_code == 200 and "Moideenkutti" in tree.text and "Not registered" in tree.text
    form = Browser().get("/f/kmh/register")
    assert form.status_code == 200 and "Sakeena" in form.text
    # second start is refused
    assert "already exists" in start(b).text


def test_claim_new_relations_and_tree(fw):
    b = fw["fam"]
    start(b)
    moi = member(fw, "Moideenkutti")
    r = register(fw, mode="me", claim_id=moi["id"], name="Moideenkutti", age="62", branch=moi["id"])
    assert "Registered!" in r.text and "Waiting for approval" in r.text and "₹800" in r.text
    # not approved yet: still not registered in the tree
    assert db(fw).execute("SELECT registered FROM fam_members WHERE id = ?", (moi["id"],)).fetchone()[0] == 0
    pend = db(fw).execute("SELECT * FROM fam_members WHERE status = 'pending'").fetchone()
    assert pend["claim_of"] == moi["id"] and pend["fee"] == 80000
    r = b.get("/family/approvals")
    assert "This is me" in r.text
    r = b.post(f"/family/members/{pend['id']}/approve", {})
    assert "approved" in r.text
    m = member(fw, "Moideenkutti")
    assert m["registered"] == 1 and m["phone"] == "9876543210" and m["age"] == 62 and m["reg_no"] == pend["reg_no"]
    assert db(fw).execute("SELECT COUNT(*) FROM fam_members WHERE status = 'pending'").fetchone()[0] == 0

    # wife of Moideenkutti (married in), then a son, then the son's daughter
    register(fw, mode="new", relation="wife", of_id=moi["id"], name="Fathima", age="55")
    fat = member(fw, "Fathima")
    b.post(f"/family/members/{fat['id']}/approve", {})
    register(fw, mode="new", relation="son", of_id=fat["id"], name="Shafi", age="35")   # chose the mother
    shafi = member(fw, "Shafi")
    assert shafi["parent_id"] == moi["id"] and shafi["other_parent_id"] == fat["id"]
    b.post(f"/family/members/{shafi['id']}/approve", {})
    register(fw, mode="new", relation="daughter", of_id=shafi["id"], name="Ziya", age="4")
    ziya = member(fw, "Ziya")
    assert ziya["fee"] == 0    # 5 years and below: free
    b.post(f"/family/members/{ziya['id']}/approve", {})

    with crm.app.test_request_context():
        t = family.build(ziya["org_id"])
    n = t["nodes"]
    assert n[shafi["id"]]["rel_text"] == "Son of Moideenkutti & Fathima"
    assert "Grandson of KUNJIMUHAMMED HAJI" in n[shafi["id"]]["rel_sub"]
    assert n[ziya["id"]]["gen"] == 4 and "Great-granddaughter of KUNJIMUHAMMED HAJI" in n[ziya["id"]]["rel_sub"]
    assert "Moideenkutti branch" in n[ziya["id"]]["rel_sub"]
    assert n[fat["id"]]["rel_text"] == "Wife of Moideenkutti" and n[fat["id"]]["gen"] == 2
    tree = Browser().get(f"/f/kmh?me={ziya['id']}")
    assert "Ziya" in tree.text and "9876543210" not in tree.text   # phone never on the public tree


def test_validation_and_reject(fw):
    b = fw["fam"]
    start(b)
    moi = member(fw, "Moideenkutti")
    r = register(fw, mode="new", relation="son", of_id=moi["id"], name="", age="", phone="", attending="")
    assert "Enter the name." in r.text and "Enter the age" in r.text and "Enter the phone" in r.text
    # husband of a person who married in is refused
    register(fw, mode="new", relation="wife", of_id=moi["id"], name="Fathima", age="55")
    fat = member(fw, "Fathima")
    b.post(f"/family/members/{fat['id']}/approve", {})
    r = register(fw, mode="new", relation="husband", of_id=fat["id"], name="X", age="50")
    assert "married into the family" in r.text
    # late person: no phone, no age, no fee
    r = register(fw, mode="new", relation="son", of_id=moi["id"], name="Old Son", age="", phone="", attending="",
                 is_late="1")
    assert "Registered!" in r.text and member(fw, "Old Son")["fee"] == 0
    # reject needs a reason
    oid = member(fw, "Old Son")["id"]
    assert "Write the reason" in b.post(f"/family/members/{oid}/reject", {"reason": ""}).text
    b.post(f"/family/members/{oid}/reject", {"reason": "Duplicate"})
    assert member(fw, "Old Son")["status"] == "rejected"
    assert "Old Son" not in Browser().get("/f/kmh").text


def test_fee_paid_accounts_and_permissions(fw):
    b = fw["fam"]
    start(b)
    moi = member(fw, "Moideenkutti")
    register(fw, mode="me", claim_id=moi["id"], name="Moideenkutti", age="62", upi_ref="UPI123")
    pend = db(fw).execute("SELECT id FROM fam_members WHERE status = 'pending'").fetchone()
    b.post(f"/family/members/{pend['id']}/approve", {})
    b.post(f"/family/members/{moi['id']}/paid", {"paid": "1"})
    assert member(fw, "Moideenkutti")["paid"] == 1
    b.post("/family/accounts/save", {"kind": "income", "amount": "5000", "category": "Sponsorship"})
    b.post("/family/accounts/save", {"kind": "expense", "amount": "1200", "category": "Food"})
    r = b.get("/family/accounts")
    assert "₹5,800" in r.text and "₹1,200" in r.text and "₹4,600" in r.text   # 800 fee + 5000; balance 4600
    x = b.get("/family/export.xlsx")
    assert x.status_code == 200 and x.data[:2] == b"PK"

    # staff: only "add income / expense" → sees only own entries, no members
    b.post("/staff/new", {"name": "Helper", "username": "helper", "role": "staff", "onboarding": "password",
                          "password": "TempPass123", "fam_perm": ["acc_add"], "mods_form": "1", "active": "1"})
    h = Browser()
    h.login("helper", "TempPass123")
    h.post("/account", {"current_password": "TempPass123", "new_password": PW, "confirm": PW})
    assert h.get("/family/members").status_code == 403
    r = h.get("/family/accounts")
    assert r.status_code == 200 and "+₹5,000" not in r.text
    h.post("/family/accounts/save", {"kind": "expense", "amount": "300", "category": "Printing"})
    assert "Printing" in h.get("/family/accounts").text
    eid = db(fw).execute("SELECT id FROM fam_accounts WHERE category = 'Food'").fetchone()[0]
    assert h.post(f"/family/accounts/{eid}/delete", {}).status_code == 403
    assert h.post("/family/clear", {"confirm": "CLEAR"}).status_code == 403
    # staff with no Family ticks never see the module
    b.post("/staff/new", {"name": "Nobody", "username": "nobody", "role": "staff", "onboarding": "password",
                          "password": "TempPass123", "mods_form": "1", "active": "1"})
    n = Browser()
    n.login("nobody", "TempPass123")
    n.post("/account", {"current_password": "TempPass123", "new_password": PW, "confirm": PW})
    assert n.get("/family").status_code == 404


def test_merge_and_clear_all(fw):
    b = fw["fam"]
    start(b)
    moi = member(fw, "Moideenkutti")
    for nm in ("Shafi", "Shafi M"):
        register(fw, mode="new", relation="son", of_id=moi["id"], name=nm, age="35")
        b.post(f"/family/members/{member(fw, nm)['id']}/approve", {})
    a, dup = member(fw, "Shafi"), member(fw, "Shafi M")
    register(fw, mode="new", relation="son", of_id=dup["id"], name="Kid", age="8")
    b.post(f"/family/members/{member(fw, 'Kid')['id']}/approve", {})
    b.post(f"/family/members/{dup['id']}/merge", {"into": a["id"]})
    assert member(fw, "Shafi M") is None and member(fw, "Kid")["parent_id"] == a["id"]
    b.post("/family/accounts/save", {"kind": "expense", "amount": "100", "category": "Food"})
    assert "Type CLEAR" in b.post("/family/clear", {"confirm": "no"}).text
    r = b.post("/family/clear", {"confirm": "CLEAR"})
    assert "Cleared" in r.text
    c = db(fw)
    assert c.execute("SELECT COUNT(*) FROM fam_members").fetchone()[0] == 9
    assert c.execute("SELECT COUNT(*) FROM fam_accounts").fetchone()[0] == 0


def test_tenant_isolation(fw):
    start(fw["fam"])
    start(fw["other"])
    moi = member(fw, "Moideenkutti", "KMH")
    o = fw["other"]
    assert o.get(f"/family/members/{moi['id']}/edit").status_code == 404
    assert o.post(f"/family/members/{moi['id']}/paid", {"paid": "1"}).status_code == 404
    # a registration on another family's form cannot attach to this family's members
    r = Browser().post("/f/oth/register", {"mode": "new", "relation": "son", "of_id": moi["id"], "name": "Sneaky",
                                           "age": "20", "phone": "9876543210", "wa_same": "1", "attending": "1"})
    assert "Choose the person" in r.text
    # orgs without the module have no public pages
    assert Browser().get("/f/realview").status_code == 404
    with crm.app.app_context():
        assert re.match(r"^\d+$", str(family.int_in("12")))


def test_admin_pages_open(fw):
    b = fw["fam"]
    start(b)
    moi = member(fw, "Moideenkutti")
    register(fw, mode="new", relation="son", of_id=moi["id"], name="Shafi", age="35", achievement="MBA",
             program="Song")
    sid = member(fw, "Shafi")["id"]
    for url in ("/family", "/family/approvals", "/family/members", "/family/members?show=not_paid",
                f"/family/members/{sid}/edit", "/family/members/new", f"/family/members/new?of={moi['id']}&rel=son",
                "/family/achievers", "/family/programs", "/family/accounts", "/family/settings", "/family/qr",
                "/staff/new"):
        r = b.get(url)
        assert r.status_code == 200, url
    b.post(f"/family/members/{sid}/approve", {})
    assert "MBA" in b.get("/family/achievers").text and "Song" in b.get("/family/programs").text
    # admin adds a child directly (name only → "Not registered")
    b.post("/family/members/new", {"relation": "daughter", "of_id": sid, "name": "Baby", "age": "1"})
    baby = member(fw, "Baby")
    assert baby["status"] == "approved" and baby["registered"] == 0 and baby["parent_id"] == sid
    assert "Family permissions" in b.get("/staff/new").text


def test_whatsapp_messages_on_approve_and_reject(fw):
    b = fw["fam"]
    start(b)
    b.post("/family/settings", {"what": "event", "event_name": "KMH Family Meetup", "event_date": "28 Dec 2026",
                                "venue": "", "fee": "800", "free_upto_age": "5", "reg_open": "1"})
    moi = member(fw, "Moideenkutti")
    register(fw, mode="me", claim_id=moi["id"], name="Moideenkutti", age="62", phone="9876543210", lang="ml")
    pend = member(fw, "Moideenkutti")
    assert pend["status"] == "pending" and pend["lang"] == "ml"
    page = b.get("/family/approvals").text
    assert "Approve &amp; Send on WhatsApp" in page and "Payment received" in page
    # approve with "payment received" ticked: paid + "received" message (Malayalam, as the form was filled)
    r = b.post(f"/family/members/{pend['id']}/approve", {"ajax": "1", "paid_box": "1", "paid": "1", "send_wa": "1"})
    d = r.get_json()
    assert d["ok"] and d["phone"] == "919876543210"
    assert "ലഭിച്ചു" in d["text"] and "📍" not in d["text"]   # empty venue line left out
    moi = member(fw, "Moideenkutti")
    assert moi["registered"] == 1 and moi["paid"] == 1 and moi["wa_sent_kind"] == "approve" and moi["lang"] == "ml"
    # approve without the tick, English: "not received" + status (pay) link; own text is used when given
    register(fw, relation="son", of_id=moi["id"], name="Shafi", age="30", phone="9000000001")
    sh = member(fw, "Shafi")
    d = b.post(f"/family/members/{sh['id']}/approve", {"ajax": "1", "paid_box": "1", "lang": "en"}).get_json()
    assert d["ok"] and "not received yet" in d["text"] and "/f/kmh/r/" in d["text"]
    assert member(fw, "Shafi")["paid"] == 0
    register(fw, relation="son", of_id=moi["id"], name="Ziya", age="28", phone="9000000002")
    z = member(fw, "Ziya")
    d = b.post(f"/family/members/{z['id']}/approve", {"ajax": "1", "message": "Custom text"}).get_json()
    assert d["text"] == "Custom text"
    # reject: reason goes into the message
    register(fw, relation="son", of_id=moi["id"], name="Wrong", age="20", phone="9000000003")
    w = member(fw, "Wrong")
    d = b.post(f"/family/members/{w['id']}/reject", {"ajax": "1", "reason": "Already registered",
                                                     "send_wa": "1"}).get_json()
    assert d["ok"] and "Reason: Already registered" in d["text"] and "/f/kmh/register" in d["text"]
    assert member(fw, "Wrong")["status"] == "rejected"
    assert b.post(f"/family/members/{w['id']}/reject", {"ajax": "1", "reason": "x"}).get_json()["ok"] is False
    # members list: WhatsApp button carries the approval message
    assert "Send the approval message again" in b.get("/family/members").text
    # own default message in Settings
    b.post("/family/settings", {"what": "event", "event_name": "KMH Family Meetup", "fee": "800",
                                "free_upto_age": "5", "reg_open": "1", "msg_paid_en": "Thanks {name}! #{reg_no}"})
    register(fw, relation="daughter", of_id=moi["id"], name="Nafla", age="25", phone="9000000004")
    nf = member(fw, "Nafla")
    d = b.post(f"/family/members/{nf['id']}/approve", {"ajax": "1", "paid_box": "1", "paid": "1"}).get_json()
    assert d["text"] == f"Thanks Nafla! #{nf['reg_no']}"


def test_form_languages_and_tree_image_button(fw):
    b = fw["fam"]
    start(b)
    form = Browser().get("/f/kmh/register").text
    assert 'data-lang="ml"' in form and "നിങ്ങളുടെ ശാഖ" in form and 'name="lang"' in form
    r = Browser().post("/f/kmh/register", {"mode": "new", "relation": "son", "name": ""})
    assert "പേര് നൽകുക." in r.text
    tree = Browser().get("/f/kmh").text
    assert "⬇️ Image" in tree and "var SHAPE" in tree
