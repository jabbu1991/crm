"""Customers, loyalty points, WhatsApp marketing templates, expenses / other income, analysis and CSV exports."""
import csv
import io
import json
from datetime import date, timedelta

from flask import Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

import cafe_charts as charts
from cafe import (ITEM_PHOTO_HINT, ORDER_TYPES, PAYMENT_MODES, add_points, bp, cafe_name, cafe_view, can,
                  ensure_categories, ensure_templates, expire_points, get_settings, host as H, max_redeemable,
                  menu_url, phone10, point_value, rupees, take_points, to_int, to_paise, today, today_date,
                  valid_birthday)


def _org():
    return H.org_id()


def get_customer(cid):
    return H.query("SELECT * FROM customers WHERE id = ? AND org_id = ?", (cid, _org()), one=True) or abort(404)


# ---------------------------------------------------------------- customers

FILTERS = {
    "all": "All", "inactive": "Not visited in 30 days", "top": "Top spenders", "points": "Most points",
    "birthday": "Birthdays this week", "expiring": "Points expiring in 7 days", "new": "New this month",
}


def fill_template(body, c, st, org):
    ref_link = menu_url(org, ref=c["referral_code"]) if st["referral"]["on"] else menu_url(org)
    values = {"name": (c["name"] or "").split(" ")[0] if c["name"] != "Customer" else "", "points": str(c["points"]),
              "menu_link": menu_url(org), "referral_link": ref_link, "cafe_name": cafe_name(st, org),
              "full_name": c["name"] or ""}
    out = body
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    return out


@bp.route("/cafe/customers")
@cafe_view("customers_view")
def customers():
    oid = _org()
    expire_points(oid)
    st = get_settings()
    f = request.args.get("f") if request.args.get("f") in FILTERS else "all"
    q = (request.args.get("q") or "").strip()
    where, args, order = ["c.org_id = ?"], [oid], "c.last_visit DESC, c.id DESC"
    t = today_date()
    if f == "inactive":
        where.append("c.visits > 0 AND (c.last_visit IS NULL OR c.last_visit < ?)")
        args.append((t - timedelta(days=30)).isoformat())
        order = "c.last_visit"
    elif f == "top":
        where.append("c.total_spent > 0")
        order = "c.total_spent DESC"
    elif f == "points":
        where.append("c.points > 0")
        order = "c.points DESC"
    elif f == "birthday":
        week = [(t + timedelta(days=i)).isoformat()[5:] for i in range(7)]
        where.append(f"c.birthday IN ({','.join('?' * 7)})")
        args += week
        order = "c.birthday"
    elif f == "expiring":
        where.append("""EXISTS (SELECT 1 FROM points_ledger pl WHERE pl.customer_id = c.id AND pl.remaining > 0
                        AND pl.expires_at IS NOT NULL AND pl.expires_at BETWEEN ? AND ?)""")
        args += [t.isoformat(), (t + timedelta(days=7)).isoformat()]
    elif f == "new":
        where.append("c.created_at >= ?")
        args.append(t.replace(day=1).isoformat())
    if q:
        where.append("(c.name LIKE ? OR c.whatsapp LIKE ? OR c.wa_norm LIKE ? OR c.referral_code = ?)")
        args += [f"%{q}%", f"%{q}%", f"%{phone10(q) or q}%", q.upper()]
    w = " AND ".join(where)
    page = max(1, request.args.get("page", 1, type=int))
    total = H.scalar(f"SELECT COUNT(*) FROM customers c WHERE {w}", args)
    rows = H.query(f"""SELECT c.*, (SELECT MIN(expires_at) FROM points_ledger pl WHERE pl.customer_id = c.id
                         AND pl.remaining > 0 AND pl.expires_at IS NOT NULL) next_expiry
                       FROM customers c WHERE {w} ORDER BY {order} LIMIT 50 OFFSET ?""", args + [(page - 1) * 50])
    templates = []
    tpl = None
    if can("marketing"):
        ensure_templates(oid)
        templates = H.query("SELECT * FROM cafe_templates WHERE org_id = ? ORDER BY sort, id", (oid,))
        tid = request.args.get("tpl", type=int)
        tpl = next((x for x in templates if x["id"] == tid), None)
    messages = {c["id"]: fill_template(tpl["body"], c, st, g.org) for c in rows} if tpl else {}
    counts = {"all": H.scalar("SELECT COUNT(*) FROM customers WHERE org_id = ?", (oid,))}
    params = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("cafe/customers.html", rows=rows, f=f, q=q, FILTERS=FILTERS, templates=templates, tpl=tpl,
                           messages=messages, page=page, pages=max(1, -(-total // 50)), total=total, params=params,
                           st=st, counts=counts)


@bp.route("/cafe/customers/new", methods=["POST"])
@cafe_view("customers_view")
def customer_new():
    if not (can("bill_create") or can("marketing")):
        abort(403)
    f = request.form
    phone = phone10(f.get("whatsapp"))
    name = (f.get("name") or "").strip()[:80]
    if not phone or not name:
        flash("Enter a name and a 10-digit WhatsApp number.", "error")
        return redirect(url_for("cafe.customers"))
    oid = _org()
    existing = H.query("SELECT id FROM customers WHERE org_id = ? AND wa_norm = ?", (oid, phone), one=True)
    if existing:
        flash("A customer with this number already exists.", "warning")
        return redirect(url_for("cafe.customer_view", cid=existing["id"]))
    from cafe import upsert_customer
    db = H.get_db()
    bday = valid_birthday(f.get("birthday"))
    cid = upsert_customer(db, oid, f.get("whatsapp"), name, bday, f.get("ref"), g.user["id"])
    db.commit()
    flash(f"{name} added.", "success")
    return redirect(url_for("cafe.customer_view", cid=cid))


KIND_LABELS = {"earn": "Earned on bill", "redeem": "Used on bill", "referral": "Referral reward",
               "referral_welcome": "Referral welcome", "manual_add": "Added by admin", "manual_sub": "Removed by admin",
               "reverse": "Bill cancelled (taken back)", "refund": "Bill cancelled (returned)",
               "expired": "Expired"}


@bp.route("/cafe/customers/<int:cid>", methods=["GET", "POST"])
@cafe_view("customers_view")
def customer_view(cid):
    c = get_customer(cid)
    oid = _org()
    st = get_settings()
    if request.method == "POST":
        f = request.form
        name = (f.get("name") or "").strip()[:80]
        phone = phone10(f.get("whatsapp"))
        bday = valid_birthday(f.get("birthday"))
        if not name or not phone:
            flash("A name and a 10-digit WhatsApp number are required.", "error")
        elif bday is None:
            flash("The birthday is not valid.", "error")
        elif H.scalar("SELECT 1 FROM customers WHERE org_id = ? AND wa_norm = ? AND id != ?", (oid, phone, cid)):
            flash("Another customer already has this WhatsApp number.", "error")
        else:
            H.execute("UPDATE customers SET name = ?, whatsapp = ?, wa_norm = ?, birthday = ?, notes = ?, updated_at = ?"
                      " WHERE id = ? AND org_id = ?", (name, (f.get("whatsapp") or "").strip()[:20], phone, bday or None,
                                                       (f.get("notes") or "").strip()[:500], H.now(), cid, oid))
            flash("Customer saved.", "success")
        return redirect(url_for("cafe.customer_view", cid=cid))
    ledger = H.query("""SELECT p.*, u.name by_name, b.bill_no FROM points_ledger p LEFT JOIN users u ON u.id = p.created_by
                        LEFT JOIN cafe_bills b ON b.id = p.bill_id WHERE p.customer_id = ? ORDER BY p.id DESC LIMIT 100""",
                     (cid,))
    bills_ = H.query("""SELECT b.*, (SELECT GROUP_CONCAT(qty || ' × ' || name || CASE WHEN measure IS NOT NULL THEN ' ' || measure || ' ' || unit
                          ELSE '' END, ', ') FROM cafe_bill_items i
                          WHERE i.bill_id = b.id) summary FROM cafe_bills b WHERE b.customer_id = ? AND b.org_id = ?
                        ORDER BY b.id DESC LIMIT 50""", (cid, oid))
    referrer = H.query("SELECT id, name FROM customers WHERE id = ?", (c["referred_by"],), one=True) \
        if c["referred_by"] else None
    referred = H.query("SELECT id, name, referral_rewarded FROM customers WHERE referred_by = ? AND org_id = ?", (cid, oid))
    expiring = H.query("""SELECT expires_at, SUM(remaining) pts FROM points_ledger WHERE customer_id = ? AND remaining > 0
                          AND expires_at IS NOT NULL GROUP BY expires_at ORDER BY expires_at LIMIT 5""", (cid,))
    templates = []
    if can("marketing"):
        ensure_templates(oid)
        templates = [(t["title"], fill_template(t["body"], c, st, g.org))
                     for t in H.query("SELECT * FROM cafe_templates WHERE org_id = ? ORDER BY sort, id", (oid,))]
    return render_template("cafe/customer_view.html", c=c, ledger=ledger, bills=bills_, st=st, referrer=referrer,
                           referred=referred, expiring=expiring, KIND_LABELS=KIND_LABELS, templates=templates,
                           ref_link=menu_url(ref=c["referral_code"]), pv=point_value(st))


@bp.route("/cafe/customers/<int:cid>/points", methods=["POST"])
@cafe_view("points_adjust")
def customer_points(cid):
    c = get_customer(cid)
    pts = to_int(request.form.get("points"), 0, 0, 1000000)
    reason = (request.form.get("reason") or "").strip()[:200]
    direction = request.form.get("direction")
    if not pts or not reason:
        flash("Points and a reason are required.", "error")
        return redirect(url_for("cafe.customer_view", cid=cid))
    db = H.get_db()
    if direction == "sub":
        if pts > c["points"]:
            flash(f"The balance is only {c['points']} points.", "error")
            return redirect(url_for("cafe.customer_view", cid=cid))
        take_points(db, _org(), cid, pts, "manual_sub", reason, None, g.user["id"])
    else:
        add_points(db, _org(), cid, pts, "manual_add", reason, None, g.user["id"])
    db.commit()
    flash(f"{pts} points {'removed' if direction == 'sub' else 'added'}.", "success")
    return redirect(url_for("cafe.customer_view", cid=cid))


@bp.route("/cafe/customers/lookup.json")
@cafe_view("bill_create")
def customer_lookup():
    c = customer_by_phone_(request.args.get("phone"))
    if not c:
        return jsonify(found=False)
    st = get_settings()
    payable = to_int(request.args.get("payable"), 0, 0)
    return jsonify(found=True, id=c["id"], name=c["name"], points=c["points"], visits=c["visits"],
                   birthday=c["birthday"] or "", max_points=max_redeemable(c["points"], payable, st) if payable else None,
                   min_redeem=to_int(st["loyalty"].get("min_redeem"), 0), max_pct=st["loyalty"].get("max_redeem_pct"))


def customer_by_phone_(phone):
    from cafe import customer_by_phone
    return customer_by_phone(phone)


# ---------------------------------------------------------------- marketing templates

@bp.route("/cafe/templates", methods=["GET", "POST"])
@cafe_view("marketing")
def templates():
    oid = _org()
    ensure_templates(oid)
    if request.method == "POST":
        f = request.form
        tid = f.get("id", type=int)
        if f.get("action") == "delete" and tid:
            H.execute("DELETE FROM cafe_templates WHERE id = ? AND org_id = ?", (tid, oid))
            flash("Template deleted.", "success")
            return redirect(url_for("cafe.templates"))
        title, body = (f.get("title") or "").strip()[:60], (f.get("body") or "").strip()[:1000]
        if not title or not body:
            flash("Enter a name and a message.", "error")
        elif tid:
            H.query("SELECT 1 FROM cafe_templates WHERE id = ? AND org_id = ?", (tid, oid), one=True) or abort(404)
            H.execute("UPDATE cafe_templates SET title = ?, body = ?, updated_at = ? WHERE id = ?",
                      (title, body, H.now(), tid))
            flash("Template saved.", "success")
        else:
            sort = (H.scalar("SELECT MAX(sort) FROM cafe_templates WHERE org_id = ?", (oid,)) or 0) + 1
            H.execute("INSERT INTO cafe_templates (org_id, title, body, sort, created_at) VALUES (?,?,?,?,?)",
                      (oid, title, body, sort, H.now()))
            flash("Template added.", "success")
        return redirect(url_for("cafe.templates"))
    rows = H.query("SELECT * FROM cafe_templates WHERE org_id = ? ORDER BY sort, id", (oid,))
    return render_template("cafe/templates.html", rows=rows)


# ---------------------------------------------------------------- expenses & other income

@bp.route("/cafe/expenses")
@cafe_view()
def expenses():
    if not (can("expense_add") or can("expense_view")):
        abort(403)
    oid = _org()
    ensure_categories(oid)
    cats = H.query("SELECT * FROM cafe_ledger_categories WHERE org_id = ? ORDER BY kind, sort, name", (oid,))
    t = today()
    kind = request.args.get("kind") if request.args.get("kind") in ("expense", "income", "all") else "all"
    if can("expense_view"):
        d_from = H.valid_date(request.args.get("from")) or today_date().replace(day=1).isoformat()
        d_to = H.valid_date(request.args.get("to")) or t
        where, args = ["e.org_id = ?", "e.deleted_at IS NULL", "e.entry_date BETWEEN ? AND ?"], [oid, d_from, d_to]
        cat = request.args.get("cat") or ""
        if cat:
            where.append("e.category = ?")
            args.append(cat)
    else:  # can only add: sees what they entered in the last 7 days
        d_from, d_to, cat = (today_date() - timedelta(days=7)).isoformat(), t, ""
        where = ["e.org_id = ?", "e.deleted_at IS NULL", "e.entry_date >= ?", "e.created_by = ?"]
        args = [oid, d_from, g.user["id"]]
    if kind != "all":
        where.append("e.kind = ?")
        args.append(kind)
    w = " AND ".join(where)
    rows = H.query(f"""SELECT e.*, u.name by_name FROM cafe_ledger e LEFT JOIN users u ON u.id = e.created_by
                       WHERE {w} ORDER BY e.entry_date DESC, e.id DESC LIMIT 300""", args)
    sums = H.query(f"""SELECT e.kind, COALESCE(SUM(e.amount), 0) total FROM cafe_ledger e WHERE {w}
                       GROUP BY e.kind""", args)
    sums = {r["kind"]: r["total"] for r in sums}
    edit = None
    if request.args.get("edit", type=int) and can("expense_view"):
        edit = H.query("SELECT * FROM cafe_ledger WHERE id = ? AND org_id = ? AND deleted_at IS NULL",
                       (request.args.get("edit", type=int), oid), one=True)
    return render_template("cafe/expenses.html", rows=rows, cats=cats, sums=sums, d_from=d_from, d_to=d_to, cat=cat,
                           kind=kind, edit=edit, PAYMENT_MODES=PAYMENT_MODES + ["Bank"], today_=t)


@bp.route("/cafe/expenses/save", methods=["POST"])
@cafe_view("expense_add")
def expense_save():
    oid = _org()
    f = request.form
    kind = "income" if f.get("kind") == "income" else "expense"
    amount = to_paise(f.get("amount"))
    d = H.valid_date(f.get("entry_date")) or today()
    category = (f.get("category") or "").strip()[:60]
    if not amount or amount <= 0:
        flash("Enter a valid amount.", "error")
        return redirect(url_for("cafe.expenses"))
    if not category:
        flash("Choose a category.", "error")
        return redirect(url_for("cafe.expenses"))
    if d > today():
        flash("The date cannot be in the future.", "error")
        return redirect(url_for("cafe.expenses"))
    mode = f.get("payment_mode") if f.get("payment_mode") in PAYMENT_MODES + ["Bank"] else "Cash"
    note = (f.get("note") or "").strip()[:300]
    up = request.files.get("photo")
    photo = H.save_image(up, box=(1400, 1400), max_kb=120) if up and up.filename else None
    eid = f.get("id", type=int)
    if eid:
        if not can("expense_view"):
            abort(403)
        row = H.query("SELECT * FROM cafe_ledger WHERE id = ? AND org_id = ? AND deleted_at IS NULL", (eid, oid),
                      one=True) or abort(404)
        if photo:
            H.remove_media(row["photo"])
        H.execute("UPDATE cafe_ledger SET kind=?, entry_date=?, amount=?, category=?, payment_mode=?, note=?, photo=?,"
                  " updated_at=? WHERE id=?", (kind, d, amount, category, mode, note, photo or row["photo"], H.now(), eid))
        flash("Saved.", "success")
    else:
        H.execute("INSERT INTO cafe_ledger (org_id, kind, entry_date, amount, category, payment_mode, note, photo,"
                  " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (oid, kind, d, amount, category, mode, note, photo, g.user["id"], H.now()))
        flash(f"{'Expense' if kind == 'expense' else 'Income'} {rupees(amount)} added.", "success")
    return redirect(url_for("cafe.expenses"))


@bp.route("/cafe/expenses/<int:eid>/delete", methods=["POST"])
@cafe_view("expense_view")
def expense_delete(eid):
    H.query("SELECT 1 FROM cafe_ledger WHERE id = ? AND org_id = ?", (eid, _org()), one=True) or abort(404)
    H.execute("UPDATE cafe_ledger SET deleted_at = ? WHERE id = ?", (H.now(), eid))
    flash("Entry deleted.", "success")
    return redirect(url_for("cafe.expenses"))


@bp.route("/cafe/expenses/categories", methods=["POST"])
@cafe_view("expense_view")
def expense_categories():
    oid = _org()
    f = request.form
    if f.get("action") == "delete":
        H.execute("DELETE FROM cafe_ledger_categories WHERE id = ? AND org_id = ?", (f.get("id", type=int), oid))
    else:
        name = (f.get("name") or "").strip()[:60]
        kind = "income" if f.get("kind") == "income" else "expense"
        if name and not H.scalar("SELECT 1 FROM cafe_ledger_categories WHERE org_id = ? AND kind = ? AND name = ?"
                                 " COLLATE NOCASE", (oid, kind, name)):
            H.execute("INSERT INTO cafe_ledger_categories (org_id, kind, name, sort) VALUES (?,?,?,?)",
                      (oid, kind, name, 99))
    return redirect(url_for("cafe.expenses") + "#categories")


# ---------------------------------------------------------------- analysis

def period_from_args():
    t = today_date()
    p = request.args.get("p") or "month"
    if p == "today":
        a, b = t, t
    elif p == "week":
        a, b = t - timedelta(days=6), t
    elif p == "last_month":
        b = t.replace(day=1) - timedelta(days=1)
        a = b.replace(day=1)
    elif p == "custom":
        a = date.fromisoformat(H.valid_date(request.args.get("from")) or t.isoformat())
        b = date.fromisoformat(H.valid_date(request.args.get("to")) or t.isoformat())
        if b < a:
            a, b = b, a
        if (b - a).days > 366:
            a = b - timedelta(days=366)
    else:
        p = "month"
        a, b = t.replace(day=1), t
    return p, a, b


def daily_rows(oid, a, b):
    sales = {r["d"]: r for r in H.query("""SELECT bill_date d, COUNT(*) n, SUM(total) total FROM cafe_bills
                                           WHERE org_id = ? AND status = 'paid' AND bill_date BETWEEN ? AND ?
                                           GROUP BY bill_date""", (oid, a.isoformat(), b.isoformat()))}
    ledger = {}
    for r in H.query("""SELECT entry_date d, kind, SUM(amount) total FROM cafe_ledger WHERE org_id = ?
                        AND deleted_at IS NULL AND entry_date BETWEEN ? AND ? GROUP BY entry_date, kind""",
                     (oid, a.isoformat(), b.isoformat())):
        ledger.setdefault(r["d"], {})[r["kind"]] = r["total"]
    out = []
    d = a
    while d <= b:
        k = d.isoformat()
        s = sales.get(k)
        sale = s["total"] if s else 0
        inc = ledger.get(k, {}).get("income", 0)
        exp = ledger.get(k, {}).get("expense", 0)
        out.append({"date": k, "bills": s["n"] if s else 0, "sales": sale, "other": inc, "expense": exp,
                    "profit": sale + inc - exp})
        d += timedelta(days=1)
    return out


@bp.route("/cafe/analysis")
@cafe_view("reports_view")
def analysis():
    oid = _org()
    p, a, b = period_from_args()
    days = daily_rows(oid, a, b)
    A, B = a.isoformat(), b.isoformat()
    tot = {k: sum(r[k] for r in days) for k in ("bills", "sales", "other", "expense", "profit")}
    tot["avg"] = tot["sales"] // tot["bills"] if tot["bills"] else 0
    tot["income"] = tot["sales"] + tot["other"]
    exp_cats = H.query("""SELECT category, SUM(amount) total FROM cafe_ledger WHERE org_id = ? AND kind = 'expense'
                          AND deleted_at IS NULL AND entry_date BETWEEN ? AND ? GROUP BY category ORDER BY total DESC""",
                       (oid, A, B))
    top_items = H.query("""SELECT bi.name, SUM(bi.qty) qty, SUM(bi.amount) amount, MAX(bi.unit) unit,
                             SUM(COALESCE(bi.measure, 0) * bi.qty) meas FROM cafe_bill_items bi
                           JOIN cafe_bills b ON b.id = bi.bill_id WHERE b.org_id = ? AND b.status = 'paid'
                           AND b.bill_date BETWEEN ? AND ? GROUP BY bi.name ORDER BY qty DESC LIMIT 10""", (oid, A, B))
    modes = H.query("""SELECT payment_mode k, COUNT(*) n, SUM(total) total FROM cafe_bills WHERE org_id = ?
                       AND status = 'paid' AND bill_date BETWEEN ? AND ? GROUP BY payment_mode ORDER BY total DESC""",
                    (oid, A, B))
    types = H.query("""SELECT order_type k, COUNT(*) n, SUM(total) total FROM cafe_bills WHERE org_id = ?
                       AND status = 'paid' AND bill_date BETWEEN ? AND ? GROUP BY order_type ORDER BY total DESC""",
                    (oid, A, B))
    staff = H.query("""SELECT u.name, COUNT(*) n, SUM(b.total) total FROM cafe_bills b JOIN users u ON u.id = b.created_by
                       WHERE b.org_id = ? AND b.status = 'paid' AND b.bill_date BETWEEN ? AND ? GROUP BY u.id
                       ORDER BY total DESC""", (oid, A, B))
    extra = H.query("""SELECT COALESCE(SUM(discount), 0) discount, COALESCE(SUM(cgst + sgst), 0) tax,
                         COALESCE(SUM(points_value), 0) points_value,
                         (SELECT COUNT(*) FROM cafe_bills x WHERE x.org_id = ? AND x.status = 'cancelled'
                            AND x.bill_date BETWEEN ? AND ?) cancelled
                       FROM cafe_bills WHERE org_id = ? AND status = 'paid' AND bill_date BETWEEN ? AND ?""",
                    (oid, A, B, oid, A, B), one=True)
    # last 6 months, whatever period is chosen
    t = today_date()
    months = []
    m = t.replace(day=1)
    for _ in range(6):
        months.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    months.reverse()
    month_rows = []
    for m in months:
        nxt = (m + timedelta(days=32)).replace(day=1)
        s = H.scalar("SELECT COALESCE(SUM(total), 0) FROM cafe_bills WHERE org_id = ? AND status = 'paid'"
                     " AND bill_date >= ? AND bill_date < ?", (oid, m.isoformat(), nxt.isoformat()))
        led = {r["kind"]: r["t"] for r in H.query("""SELECT kind, SUM(amount) t FROM cafe_ledger WHERE org_id = ?
                     AND deleted_at IS NULL AND entry_date >= ? AND entry_date < ? GROUP BY kind""",
                                                  (oid, m.isoformat(), nxt.isoformat()))}
        inc = s + led.get("income", 0)
        month_rows.append({"label": m.strftime("%b %Y"), "short": m.strftime("%b"), "income": inc,
                           "expense": led.get("expense", 0), "profit": inc - led.get("expense", 0)})
    labels = [date.fromisoformat(r["date"]).strftime("%d %b") for r in days]
    line = charts.line_chart(labels, [
        {"name": "Income", "color": charts.BLUE, "values": [r["sales"] + r["other"] for r in days], "area": True},
        {"name": "Expense", "color": charts.ORANGE, "values": [r["expense"] for r in days]}])
    parts = charts.fold([(r["category"], r["total"]) for r in exp_cats])
    donut = charts.donut(parts) if parts else None
    bars = charts.bar_chart([r["short"] for r in month_rows], [
        {"name": "Income", "color": charts.BLUE, "values": [r["income"] for r in month_rows]},
        {"name": "Expense", "color": charts.ORANGE, "values": [r["expense"] for r in month_rows]},
        {"name": "Profit", "color": charts.AQUA, "values": [r["profit"] for r in month_rows]}])
    mode_parts = [(r["k"] or "—", r["total"]) for r in modes]
    mode_donut = charts.donut(mode_parts) if mode_parts else None
    return render_template("cafe/analysis.html", p=p, a=A, b=B, days=days, tot=tot, exp_cats=exp_cats, parts=parts,
                           top_items=top_items, modes=modes, types=types, staff=staff, extra=extra,
                           month_rows=month_rows, line=line, donut=donut, bars=bars, mode_donut=mode_donut,
                           mode_parts=mode_parts, SERIES=charts.SERIES, ORDER_TYPES=ORDER_TYPES)


# ---------------------------------------------------------------- CSV exports

def _csv(name, head, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    for r in rows:
        w.writerow([("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for v in r])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}-{today()}.csv"'})


def _r(p):
    return f"{(p or 0) / 100:.2f}"


@bp.route("/cafe/export/<name>.csv")
@cafe_view()
def export(name):
    oid = _org()
    p, a, b = period_from_args()
    A, B = a.isoformat(), b.isoformat()
    st = get_settings()
    if name == "daily":
        if not can("reports_view"):
            abort(403)
        return _csv("daily-summary", ["Date", "Bills", "Sales", "Other income", "Expense", "Profit"],
                    [(r["date"], r["bills"], _r(r["sales"]), _r(r["other"]), _r(r["expense"]), _r(r["profit"]))
                     for r in daily_rows(oid, a, b)])
    if name == "bills":
        if not can("bills_view"):
            abort(403)
        rows = H.query("""SELECT b.*, u.name by_name FROM cafe_bills b LEFT JOIN users u ON u.id = b.created_by
                          WHERE b.org_id = ? AND b.bill_date BETWEEN ? AND ? ORDER BY b.id""", (oid, A, B))
        return _csv("bills", ["Bill No", "Date", "Time", "Type", "Table", "Customer", "WhatsApp", "Subtotal", "Discount",
                              "Taxable", "CGST", "SGST", "Points used", "Points value", "Round off", "Total", "Payment",
                              "Points earned", "Status", "Cancel reason", "Billed by"],
                    [(f"{st['bill_prefix']}{r['bill_no']}", r["bill_date"], r["created_at"][11:16],
                      ORDER_TYPES.get(r["order_type"], r["order_type"]), r["table_name"] or "", r["cust_name"] or "",
                      r["cust_phone"] or "", _r(r["subtotal"]), _r(r["discount"]), _r(r["taxable"]), _r(r["cgst"]),
                      _r(r["sgst"]), r["points_redeemed"], _r(r["points_value"]), _r(r["round_off"]), _r(r["total"]),
                      r["payment_mode"], r["points_earned"], r["status"], r["cancel_reason"] or "", r["by_name"] or "")
                     for r in rows])
    if name == "items":
        if not can("reports_view"):
            abort(403)
        rows = H.query("""SELECT bi.name, SUM(bi.qty) qty, SUM(bi.amount) amount, MAX(bi.unit) unit,
                            SUM(COALESCE(bi.measure, 0) * bi.qty) meas FROM cafe_bill_items bi
                          JOIN cafe_bills b ON b.id = bi.bill_id WHERE b.org_id = ? AND b.status = 'paid'
                          AND b.bill_date BETWEEN ? AND ? GROUP BY bi.name ORDER BY qty DESC""", (oid, A, B))
        return _csv("item-sales", ["Item", "Qty / packs", "Unit", "Total kg / litre", "Amount"],
                    [(r["name"], r["qty"], {"kg": "Kg", "l": "Litre"}.get(r["unit"], "Piece"),
                      round(r["meas"], 3) if r["unit"] in ("kg", "l") else "", _r(r["amount"])) for r in rows])
    if name == "expenses":
        if not can("expense_view"):
            abort(403)
        rows = H.query("""SELECT e.*, u.name by_name FROM cafe_ledger e LEFT JOIN users u ON u.id = e.created_by
                          WHERE e.org_id = ? AND e.deleted_at IS NULL AND e.entry_date BETWEEN ? AND ?
                          ORDER BY e.entry_date, e.id""", (oid, A, B))
        return _csv("expenses-income", ["Date", "Type", "Category", "Amount", "Payment", "Note", "Entered by"],
                    [(r["entry_date"], "Expense" if r["kind"] == "expense" else "Income", r["category"], _r(r["amount"]),
                      r["payment_mode"] or "", r["note"] or "", r["by_name"] or "") for r in rows])
    if name == "customers":
        if not can("customers_view"):
            abort(403)
        rows = H.query("SELECT * FROM customers WHERE org_id = ? ORDER BY name COLLATE NOCASE", (oid,))
        return _csv("customers", ["Name", "WhatsApp", "Birthday (MM-DD)", "Points", "Visits", "Total spent",
                                  "Last visit", "Referral code", "Joined"],
                    [(r["name"], r["whatsapp"], r["birthday"] or "", r["points"], r["visits"], _r(r["total_spent"]),
                      r["last_visit"] or "", r["referral_code"], r["created_at"][:10]) for r in rows])
    abort(404)
