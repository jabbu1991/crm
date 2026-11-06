"""One Excel file (.xlsx) with all the data of ONE client organization.

Used by the nightly Google Drive backup (one file per active organization), the Super Admin "⬇️ Excel" button on
Client Organizations and the Organization Admin "Download all our data (Excel)" button on the Export page.

Rules:
- Every query is limited to the one organization (org_id = ?; activity rows through their lead).
- Columns are listed one by one (never SELECT *): no password hashes, session versions, link tokens (customer links,
  bill links, points cards), password / invite tokens, push subscriptions or other secrets ever reach the file.
- Names instead of ids, dates as dd-mm-yyyy, times in India time, money in rupees.
- openpyxl write-only mode and row-by-row cursors keep memory low (the server has 512 MB).
"""
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font

BOLD = Font(bold=True)
MAX_CELL = 32000   # Excel's cell limit is 32,767 characters

# Column names that must never appear in any sheet (checked by the tests too).
SECRET_WORDS = ("password", "hash", "token", "secret", "session", "p256dh", "endpoint", "auth")


# ---------------------------------------------------------------- value formatting

def _d(v):
    """'YYYY-MM-DD…' -> 'dd-mm-yyyy' (a plain date: no time-zone change)."""
    if not v or not isinstance(v, str) or not re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v or ""
    return f"{v[8:10]}-{v[5:7]}-{v[:4]}"


def _ts(v):
    """A server-time timestamp 'YYYY-MM-DD HH:MM:SS' as 'dd-mm-yyyy HH:MM' India time."""
    if not v or not isinstance(v, str):
        return v or ""
    try:
        local = datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return _d(v)
    offset = time.localtime().tm_gmtoff
    return (local - timedelta(seconds=offset) + timedelta(hours=5, minutes=30)).strftime("%d-%m-%Y %H:%M")


def _rs(v):
    """Paise -> rupees (a number Excel can add up)."""
    if v is None or v == "":
        return ""
    try:
        return round(int(v) / 100, 2)
    except (TypeError, ValueError):
        return v


def _yn(v):
    return "Yes" if v else "No"


def _t(v):
    if isinstance(v, str) and len(v) > MAX_CELL:
        return v[:MAX_CELL] + "…"
    return "" if v is None else v


def _month(v):
    return f"{v[5:7]}-{v[:4]}" if v and isinstance(v, str) and len(v) >= 7 else ""


# ---------------------------------------------------------------- sheet definitions
# Each sheet: (title, [(header, width, formatter)], sql, needs) — the SQL returns the columns in the same order and
# takes the organization id for every "?". `needs`: None = always, or a module key ('crm', 'cafe', 'prod').

D, TS, RS, YN, T, M = _d, _ts, _rs, _yn, _t, _month

USER = "(SELECT name FROM users WHERE id = {})"
SHEETS = [
    ("Leads", [("Lead ID", 8, T), ("Customer name", 24, T), ("Company", 22, T), ("Phone", 14, T), ("WhatsApp", 14, T),
               ("Email", 24, T), ("Place", 16, T), ("District", 14, T), ("State", 14, T), ("Country", 10, T),
               ("Project / campaign", 22, T), ("Products", 28, T), ("Requirement", 34, T), ("Status", 14, T),
               ("Buying possibility", 14, T), ("Priority", 10, T), ("Source", 14, T), ("Next follow-up", 12, D),
               ("Assigned to", 18, T), ("Entered by", 18, T), ("Created", 16, TS), ("Updated by", 18, T),
               ("Updated", 16, TS), ("Notes", 40, T), ("Referral code", 12, T)],
     f"""SELECT l.id, l.customer_name, l.company_name, l.phone, l.whatsapp, l.email, l.place, l.district, l.state,
                l.country, p.name,
                (SELECT GROUP_CONCAT(pr.name, ', ') FROM lead_products lp JOIN products pr ON pr.id = lp.product_id
                  WHERE lp.lead_id = l.id),
                l.requirement, l.status, l.buying_possibility, l.priority, l.source, l.next_follow_up,
                {USER.format('l.assigned_to')}, {USER.format('l.entered_by')}, l.created_at,
                {USER.format('l.updated_by')}, l.updated_at, l.notes, l.referral_code
           FROM leads l LEFT JOIN projects p ON p.id = l.project_id
          WHERE l.org_id = ? ORDER BY l.id""", "crm"),
    ("Follow-ups", [("Lead ID", 8, T), ("Customer", 24, T), ("Phone", 14, T), ("Due date", 12, D), ("Note", 36, T),
                    ("Status", 12, T), ("Outcome", 30, T), ("Created by", 18, T), ("Created", 16, TS),
                    ("Completed by", 18, T), ("Completed", 16, TS)],
     f"""SELECT f.lead_id, l.customer_name, l.phone, f.due_date, f.note, f.status, f.outcome,
                {USER.format('f.created_by')}, f.created_at, {USER.format('f.completed_by')}, f.completed_at
           FROM follow_ups f LEFT JOIN leads l ON l.id = f.lead_id
          WHERE f.org_id = ? ORDER BY f.due_date, f.id""", "crm"),
    ("Activity history", [("When", 16, TS), ("Lead ID", 8, T), ("Customer", 24, T), ("Staff", 18, T),
                          ("What happened", 60, T)],
     f"""SELECT a.created_at, a.lead_id, l.customer_name, {USER.format('a.user_id')}, a.action
           FROM lead_activity a JOIN leads l ON l.id = a.lead_id
          WHERE l.org_id = ? ORDER BY a.id""", "crm"),
    ("Deleted leads", [("Lead ID", 8, T), ("Customer", 24, T), ("Phone", 14, T), ("Project", 22, T),
                       ("Deleted by", 18, T), ("Deleted", 16, TS)],
     f"""SELECT d.lead_id, d.customer_name, d.phone, d.project_name, {USER.format('d.deleted_by')}, d.deleted_at
           FROM deleted_leads d WHERE d.org_id = ? ORDER BY d.id""", "crm"),
    ("Products & Services", [("Name", 28, T), ("Code", 10, T), ("Category", 16, T), ("Description", 40, T),
                             ("Price (catalogue)", 14, T), ("Active", 8, YN), ("In catalogue", 10, YN),
                             ("Production days", 10, T), ("Warranty (months)", 10, T), ("Care tips", 40, T),
                             ("Video link", 24, T), ("Brochure link", 24, T), ("Created", 16, TS)],
     """SELECT name, code, category, description, price, active, show_in_catalog, prod_days, warranty_months,
               care_tips, video_url, brochure_url, created_at
          FROM products WHERE org_id = ? ORDER BY name COLLATE NOCASE""", None),
    ("Projects & Campaigns", [("Name", 26, T), ("Code", 10, T), ("Type", 14, T), ("Client", 20, T),
                              ("Client phone", 14, T), ("Client email", 22, T), ("Status", 10, T), ("Start", 12, D),
                              ("End", 12, D), ("Staff", 30, T), ("Products", 30, T), ("Description", 36, T),
                              ("Created by", 18, T), ("Created", 16, TS)],
     f"""SELECT p.name, p.code, p.campaign_type, p.client_name, p.client_phone, p.client_email, p.status,
                p.start_date, p.end_date,
                (SELECT GROUP_CONCAT(u.name, ', ') FROM project_staff ps JOIN users u ON u.id = ps.user_id
                  WHERE ps.project_id = p.id),
                (SELECT GROUP_CONCAT(pr.name, ', ') FROM project_products pp JOIN products pr ON pr.id = pp.product_id
                  WHERE pp.project_id = p.id),
                p.description, {USER.format('p.created_by')}, p.created_at
           FROM projects p WHERE p.org_id = ? ORDER BY p.name COLLATE NOCASE""", "crm"),
    ("Staff & Users", [("Name", 22, T), ("Username", 16, T), ("Designation", 18, T), ("Role", 10, T),
                       ("Phone", 14, T), ("Contact phone", 14, T), ("Email", 24, T), ("Active", 8, YN),
                       ("Cafe role", 12, T), ("Production role", 14, T), ("Last login", 16, TS), ("Created", 16, TS)],
     """SELECT name, username, designation, role, phone, contact_phone, email, active, cafe_role, prod_role,
               last_login_at, created_at
          FROM users WHERE org_id = ? ORDER BY name COLLATE NOCASE""", None),
    # ---- Cafe / restaurant
    ("Menu items", [("Category", 18, T), ("S.No", 6, T), ("Item", 26, T), ("Description", 34, T), ("Price ₹", 10, RS),
                    ("Offer price ₹", 10, RS), ("Offer from", 12, D), ("Offer to", 12, D), ("Veg / non-veg", 10, T),
                    ("GST %", 6, T), ("Unit", 8, T), ("Barcode", 14, T), ("Available", 9, YN), ("Active", 8, YN)],
     """SELECT c.name, m.serial, m.name, m.description, m.price, m.offer_price, m.offer_from, m.offer_to, m.food_type,
               m.gst_rate, m.unit, m.barcode, m.available, m.active
          FROM menu_items m LEFT JOIN menu_categories c ON c.id = m.category_id
         WHERE m.org_id = ? ORDER BY c.sort, c.name, m.sort, m.name""", "cafe"),
    ("Bills", [("Bill no", 9, T), ("Date", 12, D), ("Type", 10, T), ("Table", 10, T), ("Customer", 20, T),
               ("Phone", 14, T), ("Subtotal ₹", 11, RS), ("Discount ₹", 10, RS), ("Taxable ₹", 11, RS),
               ("CGST ₹", 9, RS), ("SGST ₹", 9, RS), ("Points used ₹", 10, RS), ("Round off ₹", 9, RS),
               ("Total ₹", 11, RS), ("Payment", 10, T), ("Cash ₹", 10, RS), ("UPI ₹", 10, RS), ("Card ₹", 10, RS),
               ("Due ₹", 9, RS), ("Status", 10, T), ("Cancel reason", 20, T), ("Billed by", 16, T), ("Time", 16, TS)],
     f"""SELECT bill_no, bill_date, order_type, table_name, cust_name, cust_phone, subtotal, discount, taxable, cgst,
                sgst, points_value, round_off, total, payment_mode, pay_cash, pay_upi, pay_card, due, status,
                cancel_reason, {USER.format('created_by')}, created_at
           FROM cafe_bills WHERE org_id = ? ORDER BY id""", "cafe"),
    ("Bill items", [("Bill no", 9, T), ("Date", 12, D), ("Item", 26, T), ("Qty", 7, T), ("Unit", 7, T),
                    ("Price ₹", 10, RS), ("GST %", 6, T), ("Amount ₹", 11, RS)],
     """SELECT b.bill_no, b.bill_date, i.name, i.qty, i.unit, i.price, i.gst_rate, i.amount
          FROM cafe_bill_items i JOIN cafe_bills b ON b.id = i.bill_id
         WHERE i.org_id = ? AND b.org_id = i.org_id ORDER BY b.id, i.id""", "cafe"),
    ("Orders", [("Order no", 9, T), ("Date", 12, D), ("Source", 10, T), ("Type", 10, T), ("Customer", 20, T),
                ("Phone", 14, T), ("Address", 30, T), ("Note", 24, T), ("Status", 10, T), ("Bill no", 9, T),
                ("Created", 16, TS)],
     """SELECT o.order_no, o.order_date, o.source, o.order_type, o.cust_name, o.cust_phone, o.address, o.note, o.status,
               (SELECT bill_no FROM cafe_bills b WHERE b.id = o.bill_id), o.created_at
          FROM cafe_orders o WHERE o.org_id = ? ORDER BY o.id""", "cafe"),
    ("Customers", [("Name", 22, T), ("WhatsApp", 14, T), ("Birthday", 12, D), ("Points", 8, T), ("Visits", 7, T),
                   ("Total spent ₹", 12, RS), ("Last visit", 12, D), ("Referral code", 12, T), ("Notes", 30, T),
                   ("Created", 16, TS)],
     """SELECT name, whatsapp, birthday, points, visits, total_spent, last_visit, referral_code, notes, created_at
          FROM customers WHERE org_id = ? ORDER BY name COLLATE NOCASE""", "cafe"),
    ("Income & Expenses", [("Date", 12, D), ("Kind", 9, T), ("Category", 18, T), ("Amount ₹", 11, RS),
                           ("Payment", 10, T), ("Note", 30, T), ("Entered by", 16, T), ("Deleted", 8, YN)],
     f"""SELECT entry_date, kind, category, amount, payment_mode, note, {USER.format('created_by')},
                deleted_at IS NOT NULL
           FROM cafe_ledger WHERE org_id = ? ORDER BY entry_date, id""", "cafe"),
    # ---- Production / Order Tracker
    ("Production orders", [("Order no", 8, T), ("Order date", 12, D), ("Customer", 22, T), ("Phone", 14, T),
                           ("WhatsApp", 14, T), ("Address", 28, T), ("Unit code", 10, T), ("Product", 24, T),
                           ("Category / model", 16, T), ("Machine type", 14, T), ("Power source", 12, T),
                           ("Weight", 10, T), ("Qty", 5, T), ("Unit price ₹", 12, RS), ("Specification", 36, T),
                           ("Stage", 16, T), ("Stage set", 14, T), ("Maker", 16, T), ("Maker amount ₹", 12, RS),
                           ("Maker month", 9, M), ("MFG start", 12, D), ("Expected finish", 12, D),
                           ("Production finished", 14, TS), ("GST bill no", 14, T), ("Bill date", 12, D),
                           ("Delivered", 12, D), ("Warranty (months)", 9, T), ("Order taken by", 18, T),
                           ("Incentive ₹", 10, RS), ("Order taker month", 9, M), ("Approved by", 16, T),
                           ("Referral code", 11, T), ("Status", 10, T), ("Cancel reason", 20, T), ("Note", 30, T),
                           ("Created", 16, TS)],
     """SELECT o.order_no, o.order_date, c.name, c.phone, c.whatsapp, c.address, c.unit_code, o.product_name,
               o.category, o.machine_type, o.power_source, o.weight, o.quantity, o.unit_price, o.details,
               s.name, ss.name, mk.name, o.maker_amount, o.maker_month, o.mfg_start, o.exp_finish, o.finished_at,
               o.bill_no, o.bill_date, o.delivered_date, o.warranty_months,
               COALESCE((SELECT name FROM users WHERE id = o.taken_by), o.taken_by_name), o.taker_amount,
               o.taker_month, o.approved_by, o.referral_code, o.status, o.cancel_reason, o.note, o.created_at
          FROM prod_orders o
          LEFT JOIN prod_customers c ON c.id = o.customer_id
          LEFT JOIN prod_stages s ON s.id = o.stage_id
          LEFT JOIN prod_stage_sets ss ON ss.id = o.set_id
          LEFT JOIN prod_makers mk ON mk.id = o.maker_id
         WHERE o.org_id = ? ORDER BY o.order_no""", "prod"),
    ("Stage history", [("Order no", 8, T), ("When", 16, TS), ("From", 16, T), ("To", 16, T), ("Note", 40, T),
                       ("By", 18, T)],
     f"""SELECT o.order_no, l.created_at, l.from_name, l.stage_name, l.note, {USER.format('l.user_id')}
           FROM prod_stage_log l JOIN prod_orders o ON o.id = l.order_id
          WHERE l.org_id = ? AND o.org_id = l.org_id ORDER BY l.id""", "prod"),
    ("Advances", [("Order no", 8, T), ("Date", 12, D), ("Amount ₹", 11, RS), ("Note", 30, T)],
     """SELECT o.order_no, a.pay_date, a.amount, a.note
          FROM prod_advances a JOIN prod_orders o ON o.id = a.order_id
         WHERE a.org_id = ? AND o.org_id = a.org_id ORDER BY o.order_no, a.pay_date""", "prod"),
    ("Parts & warranty", [("Order no", 8, T), ("Part", 24, T), ("Company", 18, T), ("Warranty (months)", 9, T),
                          ("Serial no", 16, T)],
     """SELECT o.order_no, p.name, p.company, p.months, p.serial
          FROM prod_parts p JOIN prod_orders o ON o.id = p.order_id
         WHERE p.org_id = ? AND o.org_id = p.org_id ORDER BY o.order_no, p.sort""", "prod"),
    ("Makers", [("Name", 20, T), ("Phone", 14, T), ("Work", 18, T), ("Note", 30, T), ("Active", 8, YN)],
     """SELECT name, phone, work, note, active FROM prod_makers WHERE org_id = ? ORDER BY name COLLATE NOCASE""",
     "prod"),
    ("Stages", [("Stage set", 18, T), ("Order", 6, T), ("Stage", 20, T), ("Marks", 14, T), ("Active", 8, YN)],
     """SELECT ss.name, s.sort, s.name, s.mark, s.active
          FROM prod_stages s JOIN prod_stage_sets ss ON ss.id = s.set_id
         WHERE s.org_id = ? ORDER BY ss.name, s.sort""", "prod"),
]


def modules_of(org):
    keys = org.keys() if hasattr(org, "keys") else org
    get = (lambda k: org[k] if k in keys else None)
    mods = {k for k, col in (("crm", "mod_crm"), ("cafe", "mod_cafe"), ("prod", "mod_prod")) if get(col)}
    if not mods:   # older rows: app_type only
        mods = {"cafe"} if get("app_type") == "cafe" else {"crm"}
    return mods


def safe_filename(name):
    """'Starway Tech' -> 'Starway Tech' (no characters Drive / browsers dislike)."""
    s = re.sub(r"[^\w .&()-]+", "", name or "Organization").strip()
    return re.sub(r"\s+", " ", s)[:80] or "Organization"


def file_name(org, day):
    return f"{safe_filename(org['name'])} - {day}.xlsx"


def _header(ws, cols):
    cells = []
    for title, _w, _f in cols:
        c = WriteOnlyCell(ws, value=title)
        c.font = BOLD
        cells.append(c)
    return cells


def build(conn, org, path):
    """Write the Excel file of one organization to `path`. `conn` is a sqlite3 connection; `org` an organizations
    row (or dict). Returns the number of data rows written."""
    oid = org["id"]
    mods = modules_of(org)
    wb = Workbook(write_only=True)
    # first sheet: who / when
    ws = wb.create_sheet("Organization")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 40
    ws.freeze_panes = "A2"
    ws.append(_header(ws, [("Field", 0, T), ("Value", 0, T)]))
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    for k, v in (("Organization", org["name"]), ("Code", org["code"]),
                 ("Modules", ", ".join({"crm": "CRM", "cafe": "Restaurant / Cafe", "prod": "Order Tracker"}[m]
                                       for m in ("crm", "cafe", "prod") if m in mods)),
                 ("Exported on (India time)", now_ist.strftime("%d-%m-%Y %H:%M")),
                 ("Note", "Passwords, private links and other secrets are never included in this file.")):
        ws.append([k, v])
    total = 0
    for title, cols, sql, need in SHEETS:
        if need and need not in mods:
            continue
        ws = wb.create_sheet(title[:31])
        for i, (_h, width, _f) in enumerate(cols):
            ws.column_dimensions[_col_letter(i)].width = width
        ws.freeze_panes = "A2"
        ws.append(_header(ws, cols))
        n_params = sql.count("?")
        cur = conn.execute(sql, (oid,) * n_params)
        while True:
            batch = cur.fetchmany(500)
            if not batch:
                break
            for row in batch:
                ws.append([f(v) for (_h, _w, f), v in zip(cols, tuple(row))])
                total += 1
    wb.save(path)
    return total


def _col_letter(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def build_temp(db_path, org):
    """Build the file in a temp folder from a read-only connection; returns its path (caller deletes it)."""
    fd, path = tempfile.mkstemp(prefix="org-", suffix=".xlsx")
    os.close(fd)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        build(conn, org, path)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    finally:
        conn.close()
    return path
