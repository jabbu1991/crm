"""Per-organization Excel (org_excel.py) and the pruning of pre-upgrade backups on the server disk.

Run:  python -m pytest -q tests/test_org_excel.py
"""
import io
import os
import sqlite3

from openpyxl import load_workbook

from test_crm import add_lead, crm, crm_db, world  # noqa: F401

import org_excel


def book(data):
    return load_workbook(io.BytesIO(data), read_only=True)


def all_text(wb):
    out = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            out.extend("" if v is None else str(v) for v in row)
    return "\n".join(out)


def headers(wb):
    return {ws.title: [c for c in next(ws.iter_rows(values_only=True))] for ws in wb.worksheets}


def test_org_excel_is_isolated_readable_and_has_no_secrets(world):
    W = world
    la = add_lead(W["a_admin"], W["proj_a"], "Alpha Customer", "9000000001", products=[W["prod_a1"], W["prod_a2"]])
    add_lead(W["b_admin"], W["proj_b"], "Bravo Customer", "9000000002", products=[W["prod_b1"]])
    W["a_admin"].post(f"/leads/{la}/followups", {"due_date": "2030-01-05", "note": "call back"})
    r = W["a_admin"].get("/export/all-excel")
    assert r.status_code == 200 and r.data[:2] == b"PK"
    assert "Org A - " in r.headers["Content-Disposition"]
    wb = book(r.data)
    text = all_text(wb)
    assert "Alpha Customer" in text and "Org A" in text
    for other in ("Bravo Customer", "B Product 1", "B Campaign", "Org B Admin", "Cara StaffB", "b_staff1"):
        assert other not in text, other
    # names instead of ids, products joined, India dates
    leads = list(wb["Leads"].iter_rows(values_only=True))
    hdr, row = leads[0], next(r for r in leads[1:] if r[1] == "Alpha Customer")
    rec = dict(zip(hdr, row))
    assert rec["Project / campaign"] == "A Campaign"
    assert rec["Products"] in ("A Product 1, A Product 2", "A Product 2, A Product 1")
    assert rec["Entered by"] == "Org A Admin"
    assert len(rec["Created"]) == 16 and rec["Created"][2] == "-" and rec["Created"][5] == "-"   # dd-mm-yyyy HH:MM
    assert {"Leads", "Follow-ups", "Activity history", "Products & Services", "Projects & Campaigns",
            "Staff & Users"} <= set(wb.sheetnames)
    assert "Menu items" not in wb.sheetnames and "Production orders" not in wb.sheetnames   # modules not used
    # no secret columns or values anywhere
    for title, cols in headers(wb).items():
        for c in cols:
            assert not any(w in (c or "").lower() for w in org_excel.SECRET_WORDS), (title, c)
    with sqlite3.connect(W["db_path"]) as c:
        hashes = [h for (h,) in c.execute("SELECT password_hash FROM users WHERE password_hash IS NOT NULL")]
    assert hashes and not any(h in text for h in hashes)


def test_org_excel_includes_used_modules_only_for_that_org(world):
    W = world
    with sqlite3.connect(W["db_path"]) as c:
        c.execute("UPDATE organizations SET mod_cafe = 1, mod_prod = 1 WHERE id = ?", (W["org_a"],))
        c.execute("""INSERT INTO prod_makers (org_id, name, active, created_at) VALUES (?, 'Maker A', 1, '2026-01-01')""",
                  (W["org_a"],))
        c.execute("""INSERT INTO prod_makers (org_id, name, active, created_at) VALUES (?, 'Maker B', 1, '2026-01-01')""",
                  (W["org_b"],))
        c.execute("""INSERT INTO customers (org_id, name, whatsapp, wa_norm, referral_code, card_token, created_at)
                     VALUES (?, 'Cafe Cust B', '9111111111', '9111111111', 'RB1', 'SECRETCARD', '2026-01-01')""",
                  (W["org_b"],))
        c.execute("""INSERT INTO customers (org_id, name, whatsapp, wa_norm, referral_code, card_token, created_at)
                     VALUES (?, 'Cafe Cust A', '9222222222', '9222222222', 'RA1', 'SECRETCARDA', '2026-01-01')""",
                  (W["org_a"],))
    wb = book(W["a_admin"].get("/export/all-excel").data)
    assert "Menu items" in wb.sheetnames and "Production orders" in wb.sheetnames and "Makers" in wb.sheetnames
    text = all_text(wb)
    assert "Maker A" in text and "Maker B" not in text
    assert "Cafe Cust A" in text and "Cafe Cust B" not in text and "SECRETCARD" not in text


def test_who_can_download(world):
    W = world
    assert W["a_s1"].get("/export/all-excel").status_code == 403          # staff: never
    assert "Download all our data (Excel)" not in W["a_s1"].get("/export").text
    assert "Download all our data (Excel)" in W["a_admin"].get("/export").text
    # Org B admin gets only Org B
    add_lead(W["a_admin"], W["proj_a"], "Only In A", "9000000011")
    text = all_text(book(W["b_admin"].get("/export/all-excel").data))
    assert "Only In A" not in text and "Org B" in text
    # Super Admin: button per organization, logged in that client's access log
    assert f"/admin/organizations/{W['org_a']}/excel" in W["sup"].get("/admin/organizations").text
    r = W["sup"].get(f"/admin/organizations/{W['org_a']}/excel")
    assert r.status_code == 200 and "Only In A" in all_text(book(r.data))
    with sqlite3.connect(W["db_path"]) as c:
        logged = c.execute("SELECT COUNT(*) FROM audit_log WHERE org_id = ? AND action LIKE 'Super Admin downloaded%'",
                           (W["org_a"],)).fetchone()[0]
    assert logged == 1
    assert W["a_admin"].get(f"/admin/organizations/{W['org_b']}/excel").status_code == 403


def test_pre_upgrade_backups_keep_newest_five(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    live = tmp_path / "crm.db"
    live.write_bytes(b"live")
    names = [f"crm-202601{d:02d}-101500-pre-v{d}.db" for d in range(1, 9)]
    for n in names:
        (folder / n).write_bytes(b"x")
    (folder / "notes.txt").write_text("keep me")
    (folder / "crm-manual-copy.db").write_bytes(b"keep")
    just_made = folder / "crm-20260109-090000-pre-v9.db"
    just_made.write_bytes(b"new")
    deleted = crm_db.prune_pre_upgrade_backups(str(folder), protect=str(just_made), live_db=str(live))
    left = sorted(p.name for p in folder.iterdir())
    pre = [n for n in left if "-pre-v" in n]
    assert len(pre) == 5 and just_made.name in pre
    assert pre == sorted([just_made.name] + names[-4:])
    assert len(deleted) == 4 and "notes.txt" in left and "crm-manual-copy.db" in left
    assert live.exists() and live.read_bytes() == b"live"
    # protected copy survives even when it would not be among the newest
    old = folder / "crm-20200101-000000-pre-v1.db"
    old.write_bytes(b"old")
    crm_db.prune_pre_upgrade_backups(str(folder), protect=str(old))
    assert old.exists()


def test_migrate_prunes_after_making_a_backup(tmp_path):
    db_path = str(tmp_path / "crm.db")
    crm_db.migrate(db_path)
    with sqlite3.connect(db_path) as c:
        oid = c.execute("INSERT INTO organizations (name, code, kind, status, created_at) "
                        "VALUES ('Agency', 'AG', 'agency', 'Active', '2026-01-01')").lastrowid
        c.execute("INSERT INTO users (name, username, password_hash, role, active, created_at, org_id) "
                  "VALUES ('x', 'x', 'h', 'super_admin', 1, '2026-01-01', ?)", (oid,))
        c.execute("PRAGMA user_version = 19")
    folder = tmp_path / "backups"
    folder.mkdir(exist_ok=True)
    for d in range(1, 9):
        (folder / f"crm-202601{d:02d}-101500-pre-v{d}.db").write_bytes(b"x")
    made = crm_db.migrate(db_path)
    pre = sorted(n for n in os.listdir(folder) if "-pre-v" in n)
    assert made and os.path.basename(made) in pre and len(pre) == 5
    assert os.path.exists(db_path)
