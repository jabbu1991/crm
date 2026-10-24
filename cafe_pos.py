"""Billing screen, orders (counter + online), KOT, bills, and the public digital menu with online ordering."""
import json
import time
from datetime import timedelta

from flask import abort, flash, g, jsonify, make_response, redirect, render_template, request, session, url_for

from cafe import (ORDER_TYPES, PAYMENT_MODES, add_points, bp, cafe_name, cafe_view, can, compute_totals,
                  customer_by_phone, expire_points, get_settings, has_cafe, host as H, live_banners, max_redeemable,
                  menu_data, menu_url, offer_active, effective_price, phone10, point_value, points_for_amount,
                  round_rupee, rupees, take_points, theme_css, to_int, to_paise, to_float, today, today_date, token,
                  upsert_customer, valid_birthday)

MAX_LINES, MAX_QTY = 60, 500


class BillError(Exception):
    pass


def _org():
    return H.org_id()


def get_order(oid_, statuses=None):
    o = H.query("SELECT * FROM cafe_orders WHERE id = ? AND org_id = ?", (oid_, _org()), one=True) or abort(404)
    if statuses and o["status"] not in statuses:
        abort(409)
    return o


def order_lines(order_id):
    return H.query("SELECT * FROM cafe_order_items WHERE order_id = ? ORDER BY id", (order_id,))


def next_no(db, table, col, date_col=None, day=None):
    sql = f"SELECT COALESCE(MAX({col}), 0) + 1 FROM {table} WHERE org_id = ?"
    args = [_org()]
    if date_col:
        sql += f" AND {date_col} = ?"
        args.append(day or today())
    return db.execute(sql, args).fetchone()[0]


def new_order(db, order_type, table_id=None, source="counter", **extra):
    if order_type not in ORDER_TYPES:
        raise BillError("The order type is not valid.")
    if order_type == "dine_in":
        if not table_id:
            raise BillError("Choose a table for dine-in.")
        if not db.execute("SELECT 1 FROM cafe_tables WHERE id = ? AND org_id = ? AND active = 1",
                          (table_id, _org())).fetchone():
            raise BillError("Table not found.")
        busy = db.execute("SELECT id FROM cafe_orders WHERE table_id = ? AND status = 'open' AND org_id = ?",
                          (table_id, _org())).fetchone()
        if busy and source == "counter":
            raise BillError("This table already has an order. Open it from Tables.")
    else:
        table_id = None
    ts, day = H.now(), today()
    cols = dict(org_id=_org(), order_no=next_no(db, "cafe_orders", "order_no", "order_date", day), order_date=day,
                source=source, order_type=order_type, table_id=table_id, status=extra.pop("status", "open"),
                token=token(), created_by=g.user["id"] if g.get("user") else None, created_at=ts, updated_at=ts)
    cols.update(extra)
    cur = db.execute(f"INSERT INTO cafe_orders ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     list(cols.values()))
    return cur.lastrowid


def parse_items(raw):
    """[{item_id, qty, note}] -> {(item_id, note): qty} (merged)."""
    out = {}
    if not isinstance(raw, list):
        raise BillError("The items are not valid.")
    for r in raw[:MAX_LINES]:
        if not isinstance(r, dict):
            continue
        iid = to_int(r.get("item_id"), 0)
        qty = to_int(r.get("qty"), 0, 0, MAX_QTY)
        note = str(r.get("note") or "").strip()[:80]
        if iid and qty:
            out[(iid, note)] = min(MAX_QTY, out.get((iid, note), 0) + qty)
    return out


def set_order_lines(db, order_id, wanted, allow_unavailable=False):
    """Make the order's lines match wanted. Existing lines keep their price; new lines take today's price."""
    existing = {(r["item_id"], r["note"] or ""): r for r in
                db.execute("SELECT * FROM cafe_order_items WHERE order_id = ?", (order_id,)).fetchall()}
    day = today()
    for key, qty in wanted.items():
        if key in existing:
            if existing[key]["qty"] != qty:
                db.execute("UPDATE cafe_order_items SET qty = ? WHERE id = ?", (qty, existing[key]["id"]))
            continue
        item = db.execute("SELECT * FROM menu_items WHERE id = ? AND org_id = ? AND active = 1",
                          (key[0], _org())).fetchone()
        if not item:
            raise BillError("An item is no longer on the menu. Reload the page.")
        if not item["available"] and not allow_unavailable:
            raise BillError(f"'{item['name']}' is not available now (switched OFF).")
        db.execute("""INSERT INTO cafe_order_items (org_id, order_id, item_id, name, price, gst_rate, qty, note)
                      VALUES (?,?,?,?,?,?,?,?)""", (_org(), order_id, item["id"], item["name"],
                                                     effective_price(item, day), item["gst_rate"], qty, key[1] or None))
    for key, r in existing.items():
        if key not in wanted:
            if r["kot_qty"]:
                db.execute("UPDATE cafe_order_items SET qty = 0 WHERE id = ?", (r["id"],))  # KOT shows the cancel
            else:
                db.execute("DELETE FROM cafe_order_items WHERE id = ?", (r["id"],))


def make_kot(db, order_id):
    """A KOT for everything not yet sent to the kitchen (and cancellations). Returns the KOT id or None."""
    rows = db.execute("SELECT * FROM cafe_order_items WHERE order_id = ? AND qty != kot_qty ORDER BY id",
                      (order_id,)).fetchall()
    if not rows:
        return None
    lines = [{"name": r["name"], "qty": r["qty"] - r["kot_qty"], "note": r["note"] or ""} for r in rows]
    day = today()
    cur = db.execute("INSERT INTO cafe_kots (org_id, order_id, kot_no, kot_date, lines, created_by, created_at)"
                     " VALUES (?,?,?,?,?,?,?)", (_org(), order_id, next_no(db, "cafe_kots", "kot_no", "kot_date", day),
                                                 day, json.dumps(lines, ensure_ascii=False),
                                                 g.user["id"] if g.get("user") else None, H.now()))
    db.execute("UPDATE cafe_order_items SET kot_qty = qty WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM cafe_order_items WHERE order_id = ? AND qty = 0", (order_id,))
    return cur.lastrowid


def discount_paise(payload, subtotal):
    d = payload.get("discount") or {}
    if not isinstance(d, dict):
        return 0
    if d.get("mode") == "pct":
        pct = to_float(d.get("value"), 0, 0, 100)
        return int(round(subtotal * pct / 100))
    v = to_paise(d.get("value"), 0)
    if v is None:
        raise BillError("The discount amount is not valid.")
    return v


def settle(db, order, payload):
    """Turn an open order into a paid bill. Returns (bill_id, kot_id)."""
    st = get_settings()
    oid = _org()
    lines = [dict(r) for r in db.execute("SELECT * FROM cafe_order_items WHERE order_id = ? AND qty > 0 ORDER BY id",
                                         (order["id"],)).fetchall()]
    if not lines:
        raise BillError("A bill needs at least one item.")
    mode = payload.get("payment_mode")
    if mode not in PAYMENT_MODES:
        raise BillError("Choose the payment mode (Cash / UPI / Card).")
    subtotal = sum(l["price"] * l["qty"] for l in lines)
    disc = discount_paise(payload, subtotal)
    if disc and not can("bill_discount"):
        raise BillError("You are not allowed to give discounts.")
    t = compute_totals(lines, st["gst_on"], st["gst_inclusive"], disc)

    cust = payload.get("customer") or {}
    cid = order["customer_id"]
    phone = cust.get("phone") or order["cust_phone"]
    if phone and not phone10(phone):
        raise BillError("The WhatsApp number must have 10 digits.")
    if phone10(phone):
        cid = upsert_customer(db, oid, phone, cust.get("name") or order["cust_name"],
                              cust.get("birthday") or order["birthday"], cust.get("ref") or order["referral_code"],
                              g.user["id"])
    customer = db.execute("SELECT * FROM customers WHERE id = ? AND org_id = ?", (cid, oid)).fetchone() if cid else None

    redeem = to_int(payload.get("redeem_points"), 0, 0)
    if redeem:
        if not customer:
            raise BillError("Enter the customer's WhatsApp number to use points.")
        if not can("points_redeem"):
            raise BillError("You are not allowed to redeem points.")
        cap = max_redeemable(customer["points"], t["gross"], st)
        if redeem > cap:
            raise BillError(f"At most {cap} points can be used on this bill.")
    pv = redeem * point_value(st)
    payable = max(0, t["gross"] - pv)
    total = round_rupee(payable)
    earned = points_for_amount(total, st) if customer else 0
    table_name = None
    if order["table_id"]:
        row = db.execute("SELECT name FROM cafe_tables WHERE id = ?", (order["table_id"],)).fetchone()
        table_name = row["name"] if row else None
    kot_id = make_kot(db, order["id"]) if st["kot_on"] and st["kot_with_bill"] else None
    ts, day = H.now(), today()
    cur = db.execute("""INSERT INTO cafe_bills (org_id, bill_no, bill_date, order_id, order_type, table_name,
                        customer_id, cust_name, cust_phone, subtotal, discount, taxable, cgst, sgst, points_redeemed,
                        points_value, round_off, total, points_earned, payment_mode, gst_on, gst_inclusive, gstin,
                        status, token, created_by, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'paid', ?,?,?)""",
                     (oid, next_no(db, "cafe_bills", "bill_no"), day, order["id"], order["order_type"], table_name,
                      cid, customer["name"] if customer else (cust.get("name") or order["cust_name"] or None),
                      customer["whatsapp"] if customer else None, t["subtotal"], t["discount"], t["taxable"],
                      t["cgst"], t["sgst"], redeem, pv, total - payable, total, earned, mode,
                      1 if st["gst_on"] else 0, 1 if st["gst_inclusive"] else 0, st["gstin"] if st["gst_on"] else None,
                      token(), g.user["id"], ts))
    bill_id = cur.lastrowid
    db.executemany("""INSERT INTO cafe_bill_items (org_id, bill_id, item_id, name, qty, price, gst_rate, amount)
                      VALUES (?,?,?,?,?,?,?,?)""",
                   [(oid, bill_id, l["item_id"], l["name"], l["qty"], l["price"], l["gst_rate"] if st["gst_on"] else 0,
                     l["price"] * l["qty"]) for l in lines])
    if customer:
        if redeem:
            take_points(db, oid, cid, redeem, "redeem", f"Bill {bill_label(st, None, db=db, bill_id=bill_id)}",
                        bill_id, g.user["id"])
        if earned:
            add_points(db, oid, cid, earned, "earn", f"Bill {bill_label(st, None, db=db, bill_id=bill_id)}", bill_id,
                       g.user["id"])
        db.execute("UPDATE customers SET visits = visits + 1, total_spent = total_spent + ?, last_visit = ?,"
                   " updated_at = ? WHERE id = ?", (total, day, ts, cid))
        rf = st["referral"]
        if rf["on"] and customer["referred_by"] and not customer["referral_rewarded"]:
            if rf["referrer_points"]:
                add_points(db, oid, customer["referred_by"], rf["referrer_points"], "referral",
                           f"Referred {customer['name']}", bill_id, g.user["id"])
            if rf["new_points"]:
                add_points(db, oid, cid, rf["new_points"], "referral_welcome", "Welcome points (referral)", bill_id,
                           g.user["id"])
            db.execute("UPDATE customers SET referral_rewarded = 1 WHERE id = ?", (cid,))
    db.execute("UPDATE cafe_orders SET status = 'billed', bill_id = ?, customer_id = ?, updated_at = ? WHERE id = ?",
               (bill_id, cid, ts, order["id"]))
    return bill_id, kot_id


def bill_label(st, bill, db=None, bill_id=None):
    if bill is None:
        bill = db.execute("SELECT bill_no FROM cafe_bills WHERE id = ?", (bill_id,)).fetchone()
    return f"{st['bill_prefix']}{bill['bill_no']}"


# ---------------------------------------------------------------- billing screen

@bp.route("/cafe/pos")
@cafe_view("bill_create")
def pos():
    oid = _org()
    expire_points(oid)
    st = get_settings()
    cats, items = menu_data(oid)
    tables = H.query("""SELECT t.id, t.name, (SELECT o.id FROM cafe_orders o WHERE o.table_id = t.id AND o.status = 'open'
                          LIMIT 1) busy FROM cafe_tables t WHERE t.org_id = ? AND t.active = 1 ORDER BY t.sort, t.id""",
                     (oid,))
    order = None
    if request.args.get("order", type=int):
        o = get_order(request.args.get("order", type=int), ("open",))
        cust = H.query("SELECT * FROM customers WHERE id = ?", (o["customer_id"],), one=True) if o["customer_id"] else None
        order = {"id": o["id"], "no": o["order_no"], "type": o["order_type"], "table": o["table_id"],
                 "source": o["source"], "note": o["note"] or "", "address": o["address"] or "",
                 "customer": {"phone": (cust["whatsapp"] if cust else o["cust_phone"]) or "",
                              "name": (cust["name"] if cust else o["cust_name"]) or ""},
                 "items": [{"item_id": r["item_id"], "name": r["name"], "price": r["price"], "qty": r["qty"],
                            "sent": r["kot_qty"], "note": r["note"] or ""} for r in order_lines(o["id"]) if r["qty"]]}
    elif request.args.get("table", type=int):
        t = request.args.get("table", type=int)
        busy = H.scalar("SELECT id FROM cafe_orders WHERE table_id = ? AND org_id = ? AND status = 'open'", (t, oid))
        if busy:
            return redirect(url_for("cafe.pos", order=busy))
    cfg = {
        "cats": cats, "items": items, "tables": [dict(t) for t in tables], "order": order,
        "start_type": request.args.get("type") or ("dine_in" if request.args.get("table") else "takeaway"),
        "start_table": request.args.get("table", type=int),
        "gst_on": st["gst_on"], "gst_inclusive": st["gst_inclusive"], "kot_on": st["kot_on"],
        "loyalty": st["loyalty"]["on"], "point_value": point_value(st), "birthday_on": st["birthday_on"],
        "referral_on": st["referral"]["on"], "modes": PAYMENT_MODES,
        "perms": {p: can(p) for p in ("bill_discount", "points_redeem", "customers_view")},
        "urls": {"save": url_for("cafe.pos_save"), "lookup": url_for("cafe.customer_lookup")},
    }
    return render_template("cafe/pos.html", cfg=cfg, st=st)


@bp.route("/cafe/pos/save", methods=["POST"])
@cafe_view("bill_create")
def pos_save():
    try:
        payload = json.loads(request.form.get("payload") or "{}")
    except ValueError:
        return jsonify(ok=False, error="Invalid data. Reload the page."), 400
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="Invalid data."), 400
    action = payload.get("action")
    if action not in ("kot", "save", "bill"):
        return jsonify(ok=False, error="Invalid action."), 400
    db = H.get_db()
    try:
        wanted = parse_items(payload.get("items"))
        if not wanted and action != "save":
            raise BillError("Add at least one item.")
        cust = payload.get("customer") or {}
        phone = (cust.get("phone") or "").strip()
        if phone and not phone10(phone):
            raise BillError("The WhatsApp number must have 10 digits.")
        if payload.get("order_id"):
            order = db.execute("SELECT * FROM cafe_orders WHERE id = ? AND org_id = ?",
                               (to_int(payload["order_id"]), _org())).fetchone()
            if not order or order["status"] != "open":
                raise BillError("This order was already billed or cancelled. Open it again from Tables.")
            otype = payload.get("order_type") or order["order_type"]
            table_id = to_int(payload.get("table_id"), 0) or None
            if otype not in ORDER_TYPES:
                raise BillError("The order type is not valid.")
            if otype == "dine_in":
                if not table_id:
                    raise BillError("Choose a table for dine-in.")
                if table_id != order["table_id"]:
                    if not db.execute("SELECT 1 FROM cafe_tables WHERE id = ? AND org_id = ? AND active = 1",
                                      (table_id, _org())).fetchone():
                        raise BillError("Table not found.")
                    if db.execute("SELECT 1 FROM cafe_orders WHERE table_id = ? AND status = 'open' AND id != ?",
                                  (table_id, order["id"])).fetchone():
                        raise BillError("That table has another order.")
            else:
                table_id = None
            db.execute("UPDATE cafe_orders SET order_type = ?, table_id = ?, cust_name = ?, cust_phone = ?, address = ?,"
                       " note = ?, updated_at = ? WHERE id = ?",
                       (otype, table_id, (cust.get("name") or "").strip()[:80] or order["cust_name"],
                        phone or order["cust_phone"], (payload.get("address") or "").strip()[:300] or order["address"],
                        (payload.get("note") or "").strip()[:300] or order["note"], H.now(), order["id"]))
            order_id = order["id"]
        else:
            order_id = new_order(db, payload.get("order_type") or "takeaway", to_int(payload.get("table_id"), 0) or None,
                                 cust_name=(cust.get("name") or "").strip()[:80] or None, cust_phone=phone or None,
                                 address=(payload.get("address") or "").strip()[:300] or None,
                                 note=(payload.get("note") or "").strip()[:300] or None,
                                 birthday=valid_birthday(cust.get("birthday")) or None,
                                 referral_code=(cust.get("ref") or "").strip()[:20] or None)
        set_order_lines(db, order_id, wanted)
        kot_id = bill_id = None
        if action == "kot":
            kot_id = make_kot(db, order_id)
            if not kot_id:
                raise BillError("No new items to send to the kitchen.")
        elif action == "bill":
            order = db.execute("SELECT * FROM cafe_orders WHERE id = ?", (order_id,)).fetchone()
            bill_id, kot_id = settle(db, order, payload)
        db.commit()
    except BillError as e:
        db.rollback()
        return jsonify(ok=False, error=str(e)), 400
    out = {"ok": True, "order_id": order_id}
    if bill_id:
        out["print"] = url_for("cafe.bill_print", bid=bill_id, kot=kot_id or None, auto=1)
        out["view"] = url_for("cafe.bill_view", bid=bill_id)
    elif kot_id:
        out["print"] = url_for("cafe.kot_print", kid=kot_id, auto=1)
    return jsonify(out)


# ---------------------------------------------------------------- orders

@bp.route("/cafe/orders")
@cafe_view("bill_create")
def orders():
    oid = _org()
    base = """SELECT o.*, t.name table_name, u.name created_name,
                (SELECT COALESCE(SUM(price * qty), 0) FROM cafe_order_items i WHERE i.order_id = o.id) amount,
                (SELECT GROUP_CONCAT(qty || ' × ' || name, ', ') FROM cafe_order_items i WHERE i.order_id = o.id
                   AND i.qty > 0) summary,
                (SELECT MAX(id) FROM cafe_kots k WHERE k.order_id = o.id) last_kot
              FROM cafe_orders o LEFT JOIN cafe_tables t ON t.id = o.table_id LEFT JOIN users u ON u.id = o.created_by
              WHERE o.org_id = ? """
    pending = H.query(base + "AND o.status = 'pending' ORDER BY o.id", (oid,)) if can("orders_online") else []
    running = H.query(base + "AND o.status = 'open' ORDER BY o.id", (oid,))
    done = H.query(base + "AND o.status IN ('billed', 'rejected', 'cancelled') AND o.order_date = ? ORDER BY o.id DESC"
                   " LIMIT 60", (oid, today()))
    return render_template("cafe/orders.html", pending=pending, running=running, done=done, ORDER_TYPES=ORDER_TYPES,
                           st=get_settings())


@bp.route("/cafe/orders/pending.json")
@cafe_view()
def orders_pending_json():
    if not can("orders_online"):
        return jsonify(count=0, latest=0)
    row = H.query("SELECT COUNT(*) n, COALESCE(MAX(id), 0) latest FROM cafe_orders WHERE org_id = ? AND status = 'pending'",
                  (_org(),), one=True)
    resp = jsonify(count=row["n"], latest=row["latest"])
    return resp


@bp.route("/cafe/orders/<int:order_id>/<action>", methods=["POST"])
@cafe_view("bill_create")
def order_action(order_id, action):
    db = H.get_db()
    o = get_order(order_id)
    st = get_settings()
    ts = H.now()
    if action in ("confirm", "reject") and not can("orders_online"):
        abort(403)
    if action == "confirm":
        if o["status"] != "pending":
            flash("This order was already handled.", "warning")
            return redirect(url_for("cafe.orders"))
        if o["order_type"] == "dine_in" and o["table_id"] and db.execute(
                "SELECT 1 FROM cafe_orders WHERE table_id = ? AND status = 'open' AND id != ?",
                (o["table_id"], o["id"])).fetchone():
            # the table already has a running order: add these items to it
            target = db.execute("SELECT * FROM cafe_orders WHERE table_id = ? AND status = 'open' AND id != ?",
                                (o["table_id"], o["id"])).fetchone()
            for r in db.execute("SELECT * FROM cafe_order_items WHERE order_id = ?", (o["id"],)).fetchall():
                db.execute("""INSERT INTO cafe_order_items (org_id, order_id, item_id, name, price, gst_rate, qty, note)
                              VALUES (?,?,?,?,?,?,?,?)""", (r["org_id"], target["id"], r["item_id"], r["name"],
                                                             r["price"], r["gst_rate"], r["qty"], r["note"]))
            db.execute("UPDATE cafe_orders SET status = 'confirmed_merged', bill_id = NULL, reject_reason = ?,"
                       " confirmed_by = ?, updated_at = ? WHERE id = ?",
                       (f"Added to order #{target['order_no']}", g.user["id"], ts, o["id"]))
            run_id = target["id"]
        else:
            db.execute("UPDATE cafe_orders SET status = 'open', confirmed_by = ?, updated_at = ? WHERE id = ?",
                       (g.user["id"], ts, o["id"]))
            run_id = o["id"]
        if o["cust_phone"]:
            cid = upsert_customer(db, _org(), o["cust_phone"], o["cust_name"], o["birthday"], o["referral_code"],
                                  g.user["id"])
            db.execute("UPDATE cafe_orders SET customer_id = COALESCE(customer_id, ?) WHERE id = ?", (cid, run_id))
        kot = make_kot(db, run_id) if st["kot_on"] else None
        db.commit()
        flash(f"Order #{o['order_no']} confirmed." + (" The KOT is ready." if kot else ""), "success")
        if kot:
            return redirect(url_for("cafe.orders", kot=kot))
        return redirect(url_for("cafe.orders"))
    if action == "reject":
        if o["status"] != "pending":
            abort(409)
        reason = (request.form.get("reason") or "").strip()[:200] or "Not available"
        db.execute("UPDATE cafe_orders SET status = 'rejected', reject_reason = ?, confirmed_by = ?, updated_at = ?"
                   " WHERE id = ?", (reason, g.user["id"], ts, o["id"]))
        db.commit()
        flash(f"Order #{o['order_no']} rejected.", "success")
        return redirect(url_for("cafe.orders"))
    if action == "ready":
        if o["status"] != "open":
            abort(409)
        db.execute("UPDATE cafe_orders SET ready_at = ?, updated_at = ? WHERE id = ?", (ts, ts, o["id"]))
        db.commit()
        return redirect(url_for("cafe.orders"))
    if action == "cancel":
        if o["status"] != "open":
            abort(409)
        if not can("bill_cancel"):
            abort(403)
        reason = (request.form.get("reason") or "").strip()[:200]
        if not reason:
            flash("Enter the reason for cancelling.", "error")
            return redirect(url_for("cafe.orders"))
        db.execute("UPDATE cafe_orders SET status = 'cancelled', reject_reason = ?, updated_at = ? WHERE id = ?",
                   (reason, ts, o["id"]))
        db.commit()
        flash(f"Order #{o['order_no']} cancelled.", "success")
        return redirect(url_for("cafe.orders"))
    if action == "kot":
        if o["status"] != "open":
            abort(409)
        kot = make_kot(db, o["id"])
        db.commit()
        if not kot:
            flash("No new items.", "warning")
            return redirect(url_for("cafe.orders"))
        return redirect(url_for("cafe.kot_print", kid=kot, auto=1))
    abort(404)


@bp.route("/cafe/kot/<int:kid>/print")
@cafe_view("bill_create")
def kot_print(kid):
    k = H.query("SELECT * FROM cafe_kots WHERE id = ? AND org_id = ?", (kid, _org()), one=True) or abort(404)
    o = H.query("""SELECT o.*, t.name table_name FROM cafe_orders o LEFT JOIN cafe_tables t ON t.id = o.table_id
                   WHERE o.id = ?""", (k["order_id"],), one=True)
    st = get_settings()
    return render_template("cafe/print.html", st=st, kots=[(k, json.loads(k["lines"]), o)], bill=None, items=[],
                           auto=request.args.get("auto"), name=cafe_name(st), ORDER_TYPES=ORDER_TYPES,
                           paper=request.args.get("paper") or st["paper"], back=url_for("cafe.orders"))


# ---------------------------------------------------------------- bills

def get_bill(bid):
    return H.query("""SELECT b.*, u.name created_name, cu.name cancelled_name FROM cafe_bills b
                      LEFT JOIN users u ON u.id = b.created_by LEFT JOIN users cu ON cu.id = b.cancelled_by
                      WHERE b.id = ? AND b.org_id = ?""", (bid, _org()), one=True) or abort(404)


def bill_whatsapp_text(st, b, items, link, balance=None):
    lines = [f"🧾 *{cafe_name(st)}*", f"Bill No: {st['bill_prefix']}{b['bill_no']} · {b['bill_date'][8:10]}-"
             f"{b['bill_date'][5:7]}-{b['bill_date'][:4]}", ""]
    for i in items:
        lines.append(f"{i['qty']} × {i['name']}  {rupees(i['amount'])}")
    lines.append("")
    if b["discount"]:
        lines.append(f"Discount: -{rupees(b['discount'])}")
    if b["points_redeemed"]:
        lines.append(f"Points used: {b['points_redeemed']} (-{rupees(b['points_value'])})")
    lines.append(f"*Total: {rupees(b['total'])}* ({b['payment_mode']})")
    if b["points_earned"]:
        lines.append(f"🎁 Points earned: {b['points_earned']}" + (f" · Balance: {balance}" if balance is not None else ""))
    lines += ["", f"View bill: {link}", st["bill_footer"] or "Thank you!"]
    return "\n".join(lines)


@bp.route("/cafe/bills")
@cafe_view("bills_view")
def bills():
    oid = _org()
    t = today()
    d_from = H.valid_date(request.args.get("from")) or t
    d_to = H.valid_date(request.args.get("to")) or d_from
    where = ["b.org_id = ?", "b.bill_date BETWEEN ? AND ?"]
    args = [oid, d_from, d_to]
    mode = request.args.get("mode")
    if mode in PAYMENT_MODES:
        where.append("b.payment_mode = ?")
        args.append(mode)
    status = request.args.get("status")
    if status in ("paid", "cancelled"):
        where.append("b.status = ?")
        args.append(status)
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(CAST(b.bill_no AS TEXT) = ? OR b.cust_phone LIKE ? OR b.cust_name LIKE ?)")
        args += [q.lstrip("#").lstrip(get_settings()["bill_prefix"] or "#"), f"%{q}%", f"%{q}%"]
    w = " AND ".join(where)
    page = max(1, request.args.get("page", 1, type=int))
    total_rows = H.scalar(f"SELECT COUNT(*) FROM cafe_bills b WHERE {w}", args)
    rows = H.query(f"""SELECT b.*, u.name created_name, (SELECT GROUP_CONCAT(qty || ' × ' || name, ', ')
                         FROM cafe_bill_items i WHERE i.bill_id = b.id) summary
                       FROM cafe_bills b LEFT JOIN users u ON u.id = b.created_by WHERE {w}
                       ORDER BY b.id DESC LIMIT 50 OFFSET ?""", args + [(page - 1) * 50])
    sums = H.query(f"""SELECT COUNT(*) n, COALESCE(SUM(total), 0) total,
                         COALESCE(SUM(CASE WHEN payment_mode = 'Cash' THEN total END), 0) cash,
                         COALESCE(SUM(CASE WHEN payment_mode = 'UPI' THEN total END), 0) upi,
                         COALESCE(SUM(CASE WHEN payment_mode = 'Card' THEN total END), 0) card,
                         COALESCE(SUM(discount), 0) discount, COALESCE(SUM(cgst + sgst), 0) tax
                       FROM cafe_bills b WHERE {w} AND b.status = 'paid'""", args, one=True)
    params = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("cafe/bills.html", rows=rows, sums=sums, d_from=d_from, d_to=d_to, mode=mode, status=status,
                           q=q, page=page, pages=max(1, -(-total_rows // 50)), params=params, PAYMENT_MODES=PAYMENT_MODES,
                           ORDER_TYPES=ORDER_TYPES, st=get_settings(), total_rows=total_rows)


@bp.route("/cafe/bills/<int:bid>")
@cafe_view("bills_view")
def bill_view(bid):
    b = get_bill(bid)
    st = get_settings()
    items = H.query("SELECT * FROM cafe_bill_items WHERE bill_id = ? ORDER BY id", (bid,))
    cust = H.query("SELECT * FROM customers WHERE id = ?", (b["customer_id"],), one=True) if b["customer_id"] else None
    link = url_for("cafe.public_bill", tok=b["token"], _external=True)
    wa_text = bill_whatsapp_text(st, b, items, link, cust["points"] if cust else None)
    kots = H.query("SELECT * FROM cafe_kots WHERE order_id = ? ORDER BY id", (b["order_id"],)) if b["order_id"] else []
    return render_template("cafe/bill_view.html", b=b, items=items, cust=cust, st=st, link=link, wa_text=wa_text,
                           kots=kots, ORDER_TYPES=ORDER_TYPES, new=request.args.get("new"))


@bp.route("/cafe/bills/<int:bid>/print")
@cafe_view("bills_view")
def bill_print(bid):
    b = get_bill(bid)
    st = get_settings()
    items = H.query("SELECT * FROM cafe_bill_items WHERE bill_id = ? ORDER BY id", (bid,))
    kots = []
    kid = request.args.get("kot", type=int)
    if kid:
        k = H.query("SELECT * FROM cafe_kots WHERE id = ? AND org_id = ?", (kid, _org()), one=True)
        if k:
            o = H.query("""SELECT o.*, t.name table_name FROM cafe_orders o LEFT JOIN cafe_tables t ON t.id = o.table_id
                           WHERE o.id = ?""", (k["order_id"],), one=True)
            kots = [(k, json.loads(k["lines"]), o)]
    cust = H.query("SELECT points FROM customers WHERE id = ?", (b["customer_id"],), one=True) if b["customer_id"] else None
    return render_template("cafe/print.html", st=st, bill=b, items=items, kots=kots, auto=request.args.get("auto"),
                           name=cafe_name(st), ORDER_TYPES=ORDER_TYPES, paper=request.args.get("paper") or st["paper"],
                           balance=cust["points"] if cust else None, back=url_for("cafe.bill_view", bid=bid, new=1)
                           if request.args.get("auto") else url_for("cafe.bill_view", bid=bid))


@bp.route("/cafe/bills/<int:bid>/cancel", methods=["POST"])
@cafe_view("bill_cancel")
def bill_cancel(bid):
    b = get_bill(bid)
    if b["status"] != "paid":
        flash("This bill is already cancelled.", "warning")
        return redirect(url_for("cafe.bill_view", bid=bid))
    reason = (request.form.get("reason") or "").strip()[:200]
    if not reason:
        flash("Enter the reason for cancelling.", "error")
        return redirect(url_for("cafe.bill_view", bid=bid))
    db = H.get_db()
    oid, ts = _org(), H.now()
    st = get_settings()
    label = f"{st['bill_prefix']}{b['bill_no']}"
    db.execute("UPDATE cafe_bills SET status = 'cancelled', cancel_reason = ?, cancelled_by = ?, cancelled_at = ?"
               " WHERE id = ?", (reason, g.user["id"], ts, bid))
    if b["order_id"]:
        db.execute("UPDATE cafe_orders SET status = 'cancelled', reject_reason = ?, updated_at = ? WHERE id = ?",
                   (f"Bill {label} cancelled: {reason}", ts, b["order_id"]))
    if b["customer_id"]:
        cid = b["customer_id"]
        earned = db.execute("SELECT COALESCE(SUM(change), 0) FROM points_ledger WHERE bill_id = ? AND customer_id = ?"
                            " AND kind = 'earn'", (bid, cid)).fetchone()[0]
        if earned:
            take_points(db, oid, cid, earned, "reverse", f"Bill {label} cancelled", bid, g.user["id"])
        if b["points_redeemed"]:
            add_points(db, oid, cid, b["points_redeemed"], "refund", f"Bill {label} cancelled", bid, g.user["id"])
        db.execute("UPDATE customers SET visits = MAX(0, visits - 1), total_spent = MAX(0, total_spent - ?),"
                   " updated_at = ? WHERE id = ?", (b["total"], ts, cid))
    db.commit()
    flash(f"Bill {label} cancelled. Points were adjusted back.", "success")
    return redirect(url_for("cafe.bill_view", bid=bid))


# ---------------------------------------------------------------- public pages (no login)

def public_org(code):
    org = H.query("SELECT * FROM organizations WHERE code = ? COLLATE NOCASE", (code,), one=True)
    if not org or not has_cafe(org) or org["status"] != "Active":
        abort(404)
    return org


def member_preview(org):
    return bool(g.get("user") and g.get("org") and g.org["id"] == org["id"])


@bp.route("/b/<tok>")
def public_bill(tok):
    b = H.query("SELECT * FROM cafe_bills WHERE token = ?", (tok,), one=True) or abort(404)
    org = H.query("SELECT * FROM organizations WHERE id = ?", (b["org_id"],), one=True)
    if not org or org["status"] != "Active":
        abort(404)
    st = get_settings(org["id"])
    items = H.query("SELECT * FROM cafe_bill_items WHERE bill_id = ? ORDER BY id", (b["id"],))
    resp = make_response(render_template("cafe/print.html", st=st, bill=b, items=items, kots=[], auto=None,
                                         name=st["name"] or org["name"], ORDER_TYPES=ORDER_TYPES,
                                         paper=request.args.get("paper") or "80", balance=None, public=True,
                                         menu_link=url_for("cafe.public_menu", code=org["code"].lower())
                                         if st["menu_on"] else None))
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@bp.route("/m/<code>")
def public_menu(code):
    org = public_org(code)
    st = get_settings(org["id"])
    preview = request.args.get("preview") and member_preview(org)
    if not st["menu_on"] and not preview:
        abort(404)
    data = menu_data(org["id"], for_public=True)
    banners = live_banners(org["id"], data)
    colors, layout, font, shape = theme_css(st["theme"])
    table = None
    if request.args.get("t", type=int):
        table = H.query("SELECT id, name FROM cafe_tables WHERE id = ? AND org_id = ? AND active = 1",
                        (request.args.get("t", type=int), org["id"]), one=True)
    types = [k for k in ("dine_in", "takeaway", "delivery") if st["order_" + k]]
    cfg = {
        "cats": data[0], "items": data[1], "banners": banners,
        "online": bool(st["online_on"] and types), "types": {k: ORDER_TYPES[k] for k in types},
        "table": dict(table) if table else None, "birthday_on": st["birthday_on"],
        "referral": st["referral"]["on"], "ref": (request.args.get("ref") or "")[:20],
        "order_url": url_for("cafe.public_order", code=org["code"].lower()), "delivery_note": st["delivery_note"],
        "name": st["name"] or org["name"], "preview": bool(preview),
        "tables": [dict(r) for r in H.query("SELECT id, name FROM cafe_tables WHERE org_id = ? AND active = 1"
                                            " ORDER BY sort, id", (org["id"],))] if "dine_in" in types else [],
    }
    resp = make_response(render_template("cafe/public_menu.html", org=org, st=st, cfg=cfg, colors=colors,
                                         layout=layout, font=font, shape=shape, wa=H.wa_number(st["whatsapp"] or
                                                                                               st["phone"] or org["phone"])))
    if preview:
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


_order_hits = {}


@bp.route("/m/<code>/order", methods=["POST"])
def public_order(code):
    org = public_org(code)
    st = get_settings(org["id"])
    types = [k for k in ("dine_in", "takeaway", "delivery") if st["order_" + k]]
    if not st["menu_on"] or not st["online_on"] or not types:
        return jsonify(ok=False, error="Online ordering is not available right now."), 400
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    now_ts = time.time()
    hits = [t for t in _order_hits.get(ip, []) if now_ts - t < 3600]
    if len(hits) >= 10:
        return jsonify(ok=False, error="Too many orders. Please try again later."), 429
    try:
        p = json.loads(request.form.get("payload") or "{}")
        assert isinstance(p, dict)
    except (ValueError, AssertionError):
        return jsonify(ok=False, error="Invalid data."), 400
    if p.get("website"):  # honeypot
        return jsonify(ok=True, url=url_for("cafe.public_menu", code=code))
    otype = p.get("type")
    name = (p.get("name") or "").strip()[:80]
    phone = phone10(p.get("phone"))
    if otype not in types:
        return jsonify(ok=False, error="Choose an order type."), 400
    if not name or not phone:
        return jsonify(ok=False, error="Enter a name and a 10-digit WhatsApp number."), 400
    address = (p.get("address") or "").strip()[:300]
    if otype == "delivery" and len(address) < 8:
        return jsonify(ok=False, error="Please enter the delivery address."), 400
    oid = org["id"]
    db = H.get_db()
    if db.execute("SELECT COUNT(*) FROM cafe_orders WHERE org_id = ? AND status = 'pending' AND cust_phone = ?",
                  (oid, phone)).fetchone()[0] >= 3:
        return jsonify(ok=False, error="There are unconfirmed orders from this number. Please wait a little."), 429
    table_id = None
    if otype == "dine_in":
        table_id = to_int(p.get("table"), 0)
        if not db.execute("SELECT 1 FROM cafe_tables WHERE id = ? AND org_id = ? AND active = 1", (table_id, oid)).fetchone():
            return jsonify(ok=False, error="Please choose your table."), 400
    try:
        wanted = parse_items(p.get("items"))
    except BillError as e:
        return jsonify(ok=False, error=str(e)), 400
    if not wanted:
        return jsonify(ok=False, error="Your cart is empty."), 400
    if sum(wanted.values()) > 100:
        return jsonify(ok=False, error="Up to 100 items per order."), 400
    _order_hits[ip] = hits + [now_ts]
    # Run as the organization (no logged-in user here).
    saved_org = g.get("org")
    g.org = org
    try:
        order_id = new_order(db, otype, table_id, source="online", status="pending", cust_name=name, cust_phone=phone,
                             address=address or None, note=(p.get("note") or "").strip()[:300] or None,
                             birthday=valid_birthday(p.get("birthday")) or None if st["birthday_on"] else None,
                             referral_code=(p.get("ref") or "").strip()[:20] or None)
        set_order_lines(db, order_id, wanted)
        db.commit()
    except BillError as e:
        db.rollback()
        return jsonify(ok=False, error=str(e)), 400
    finally:
        g.org = saved_org
    tok = db.execute("SELECT token FROM cafe_orders WHERE id = ?", (order_id,)).fetchone()[0]
    return jsonify(ok=True, url=url_for("cafe.public_order_status", code=code, tok=tok))


def _status_data(o):
    if o["status"] == "pending":
        s = "pending"
    elif o["status"] in ("rejected", "cancelled"):
        s = "rejected"
    elif o["status"] == "billed":
        s = "done"
    elif o["ready_at"]:
        s = "ready"
    else:
        s = "confirmed"
    return {"status": s, "reason": o["reject_reason"] or ""}


@bp.route("/m/<code>/o/<tok>")
def public_order_status(code, tok):
    org = public_org(code)
    o = H.query("SELECT * FROM cafe_orders WHERE token = ? AND org_id = ? AND source = 'online'", (tok, org["id"]),
                one=True) or abort(404)
    if o["status"] == "confirmed_merged":
        o = dict(o, status="open")
    st = get_settings(org["id"])
    items = H.query("SELECT * FROM cafe_order_items WHERE order_id = ? ORDER BY id", (o["id"],))
    table = H.query("SELECT name FROM cafe_tables WHERE id = ?", (o["table_id"],), one=True) if o["table_id"] else None
    colors, layout, font, shape = theme_css(st["theme"])
    return render_template("cafe/public_status.html", org=org, st=st, o=o, items=items, table=table,
                           data=_status_data(o), colors=colors, font=font, shape=shape, ORDER_TYPES=ORDER_TYPES,
                           wa=H.wa_number(st["whatsapp"] or st["phone"] or org["phone"]))


@bp.route("/m/<code>/o/<tok>.json")
def public_order_status_json(code, tok):
    org = public_org(code)
    o = H.query("SELECT * FROM cafe_orders WHERE token = ? AND org_id = ? AND source = 'online'", (tok, org["id"]),
                one=True) or abort(404)
    if o["status"] == "confirmed_merged":
        o = dict(o, status="open")
    return jsonify(_status_data(o))
