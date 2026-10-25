"""Billing screen, orders (counter + online), KOT, bills, and the public digital menu with online ordering."""
import json
import threading
import time

import cafe_escpos
from datetime import timedelta

from flask import abort, flash, g, jsonify, make_response, redirect, render_template, request, session, url_for

from cafe import (ORDER_TYPES, PAYMENT_MODES, BILL_PAY_MODES, PAY_COLS, parse_payment, pay_text, card_url, qr_svg, fmt_measure, line_amount_text, UNIT_PER, add_points, bp, cafe_name, cafe_view, can, compute_totals,
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
    """[{line_id?, item_id, qty, note, measure?, price?}] -> clean list of dicts.

    line_id: an existing line of the order. measure: kg / litre for items sold by weight or volume.
    price: a price changed on the bill (paise per piece / pack); needs the price-change permission."""
    if not isinstance(raw, list):
        raise BillError("The items are not valid.")
    out = []
    for r in raw[:MAX_LINES]:
        if not isinstance(r, dict):
            continue
        iid = to_int(r.get("item_id"), 0)
        qty = to_int(r.get("qty"), 0, 0, MAX_QTY)
        if not iid or not qty:
            continue
        measure = to_float(r.get("measure"), 0, 0, 1000) or None
        if measure is not None:
            measure = round(measure, 3)
            if measure < 0.001:
                measure = None
        price = r.get("price")
        price = to_paise(price) if price not in (None, "") else None
        if price is not None and (price < 0 or price > 10000000):
            raise BillError("A price is not valid.")
        out.append({"line_id": to_int(r.get("line_id"), 0) or None, "item_id": iid, "qty": qty,
                    "note": str(r.get("note") or "").strip()[:80], "measure": measure, "price": price})
    return out


def _price_change_ok(public):
    if public or not can("price_edit"):
        raise BillError("You are not allowed to change item prices.")


def set_order_lines(db, order_id, wanted, allow_unavailable=False, public=False):
    """Make the order's lines match `wanted`. Existing lines keep their price unless it is changed (permission);
    new lines take today's menu price (per kg / litre x measure for weighed items)."""
    existing = {r["id"]: r for r in db.execute("SELECT * FROM cafe_order_items WHERE order_id = ?", (order_id,))}
    day = today()
    uid = g.user["id"] if g.get("user") else None
    keep = set()
    # Lines sent without line_id (older pages, the customer menu) match an existing line with the same item, note
    # and weight, so a repeat save never cancels and re-sends what the kitchen already has.
    claimed = {w["line_id"] for w in wanted if w["line_id"] in existing}
    for w in wanted:
        if w["line_id"] not in existing:
            w["line_id"] = None
            for rid, r in existing.items():
                if (rid not in claimed and r["item_id"] == w["item_id"] and (r["note"] or "") == w["note"]
                        and (r["measure"] or None) == w["measure"] and not r["price_by"]):
                    w["line_id"] = rid
                    claimed.add(rid)
                    break
    for w in wanted:
        row = existing.get(w["line_id"]) if w["line_id"] else None
        if row is not None:
            keep.add(row["id"])
            price, price_by = row["price"], row["price_by"]
            measure = row["measure"]
            if row["unit"] in ("kg", "l") and w["measure"] and w["measure"] != row["measure"]:
                measure = w["measure"]
                price = int(round((row["unit_price"] or row["price"]) * measure))
            if w["price"] is not None and w["price"] != price:
                _price_change_ok(public)
                price, price_by = w["price"], uid
            if (row["qty"], row["price"], row["measure"]) != (w["qty"], price, measure):
                db.execute("UPDATE cafe_order_items SET qty = ?, price = ?, measure = ?, price_by = ? WHERE id = ?",
                           (w["qty"], price, measure, price_by, row["id"]))
            continue
        item = db.execute("SELECT * FROM menu_items WHERE id = ? AND org_id = ? AND active = 1",
                          (w["item_id"], _org())).fetchone()
        if not item:
            raise BillError("An item is no longer on the menu. Reload the page.")
        if not item["available"] and not allow_unavailable:
            raise BillError(f"'{item['name']}' is not available now (switched OFF).")
        unit = item["unit"] or "pc"
        unit_price = effective_price(item, day)
        if unit in ("kg", "l"):
            measure = w["measure"] or 1.0
            price = int(round(unit_price * measure))
        else:
            measure, price = None, unit_price
        price_by = None
        if w["price"] is not None and w["price"] != price:
            _price_change_ok(public)
            price, price_by = w["price"], uid
        db.execute("""INSERT INTO cafe_order_items (org_id, order_id, item_id, name, price, gst_rate, qty, note, unit,
                      measure, unit_price, price_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (_org(), order_id, item["id"], item["name"], price, item["gst_rate"], w["qty"], w["note"] or None,
                    unit, measure, unit_price if unit in ("kg", "l") else None, price_by))
    for rid, r in existing.items():
        if rid not in keep:
            if r["kot_qty"]:
                db.execute("UPDATE cafe_order_items SET qty = 0 WHERE id = ?", (rid,))  # the KOT shows the cancel
            else:
                db.execute("DELETE FROM cafe_order_items WHERE id = ?", (rid,))


def make_kot(db, order_id):
    """A KOT for everything not yet sent to the kitchen (and cancellations). Returns the KOT id or None."""
    rows = db.execute("SELECT * FROM cafe_order_items WHERE order_id = ? AND qty != kot_qty ORDER BY id",
                      (order_id,)).fetchall()
    if not rows:
        return None
    lines = []
    for r in rows:
        ln = {"name": r["name"], "qty": r["qty"] - r["kot_qty"], "note": r["note"] or ""}
        if r["measure"]:
            ln["measure"] = fmt_measure(r["measure"], r["unit"])
        lines.append(ln)
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
    if mode not in BILL_PAY_MODES:
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
    try:
        mode, p_cash, p_upi, p_card, due = parse_payment(mode, payload.get("payment_split"), total)
    except ValueError as e:
        raise BillError(str(e))
    table_name = None
    if order["table_id"]:
        row = db.execute("SELECT name FROM cafe_tables WHERE id = ?", (order["table_id"],)).fetchone()
        table_name = row["name"] if row else None
    kot_id = make_kot(db, order["id"]) if st["kot_on"] and st["kot_with_bill"] else None
    ts, day = H.now(), today()
    cur = db.execute("""INSERT INTO cafe_bills (org_id, bill_no, bill_date, order_id, order_type, table_name,
                        customer_id, cust_name, cust_phone, subtotal, discount, taxable, cgst, sgst, points_redeemed,
                        points_value, round_off, total, points_earned, payment_mode, gst_on, gst_inclusive, gstin,
                        status, token, created_by, created_at, pay_cash, pay_upi, pay_card, due)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'paid', ?,?,?,?,?,?,?)""",
                     (oid, next_no(db, "cafe_bills", "bill_no"), day, order["id"], order["order_type"], table_name,
                      cid, customer["name"] if customer else (cust.get("name") or order["cust_name"] or None),
                      customer["whatsapp"] if customer else None, t["subtotal"], t["discount"], t["taxable"],
                      t["cgst"], t["sgst"], redeem, pv, total - payable, total, earned, mode,
                      1 if st["gst_on"] else 0, 1 if st["gst_inclusive"] else 0, st["gstin"] if st["gst_on"] else None,
                      token(), g.user["id"], ts, p_cash, p_upi, p_card, due))
    bill_id = cur.lastrowid
    db.executemany("""INSERT INTO cafe_bill_items (org_id, bill_id, item_id, name, qty, price, gst_rate, amount, unit,
                      measure, unit_price, price_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                   [(oid, bill_id, l["item_id"], l["name"], l["qty"], l["price"], l["gst_rate"] if st["gst_on"] else 0,
                     l["price"] * l["qty"], l["unit"] or "pc", l["measure"], l["unit_price"], l["price_by"])
                    for l in lines])
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
    cats, items = menu_data(oid, sort=st["sort_pos"])
    if st["pos_tiles"] == "text":
        for it in items:
            it["photo"] = None   # no photo downloads on the billing screen
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
                 "items": [{"line_id": r["id"], "item_id": r["item_id"], "name": r["name"], "price": r["price"],
                            "qty": r["qty"], "sent": r["kot_qty"], "note": r["note"] or "", "unit": r["unit"] or "pc",
                            "measure": r["measure"], "unit_price": r["unit_price"], "edited": bool(r["price_by"])}
                           for r in order_lines(o["id"]) if r["qty"]]}
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
        "referral_on": st["referral"]["on"], "modes": PAYMENT_MODES, "org": g.org["code"].lower(),
        "perms": {p: can(p) for p in ("bill_discount", "points_redeem", "customers_view", "price_edit")},
        "urls": {"save": url_for("cafe.pos_save"), "lookup": url_for("cafe.customer_lookup")},
    }
    return render_template("cafe/pos.html", cfg=cfg, st=st)


# The billing screen retries a save when the network drops. Each tap carries a random ref; a retry of a save that
# already went through gets the first answer again instead of making a second bill. (One app process, so memory is ok.)
_done_saves = {}
_done_lock = threading.Lock()


def _remember_save(key, out):
    now_ = time.time()
    with _done_lock:
        for k in [k for k, (t, _) in _done_saves.items() if now_ - t > 1800]:
            del _done_saves[k]
        _done_saves[key] = (now_, out)


@bp.route("/cafe/pos/save", methods=["POST"])
@cafe_view("bill_create")
def pos_save():
    try:
        payload = json.loads(request.form.get("payload") or "{}")
    except ValueError:
        return jsonify(ok=False, error="Invalid data. Reload the page."), 400
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="Invalid data."), 400
    ref = str(payload.get("client_ref") or "")[:40]
    key = (_org(), g.user["id"], ref) if ref else None
    if key:
        with _done_lock:
            hit = _done_saves.get(key)
            if not hit:
                _done_saves[key] = (time.time(), None)   # in progress
        if hit:
            if hit[1] is None:
                return jsonify(ok=False, busy=True, error="Still saving…"), 409
            return jsonify(hit[1])
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
        if key:
            with _done_lock:
                _done_saves.pop(key, None)
        return jsonify(ok=False, error=str(e)), 400
    except Exception:
        if key:
            with _done_lock:
                _done_saves.pop(key, None)
        raise
    out = {"ok": True, "order_id": order_id}
    if bill_id:
        out["print"] = url_for("cafe.bill_print", bid=bill_id, kot=kot_id or None, auto=1)
        out["embed"] = url_for("cafe.bill_print", bid=bill_id, kot=kot_id or None, auto=1, embed=1)
        out["view"] = url_for("cafe.bill_view", bid=bill_id)
    elif kot_id:
        out["print"] = url_for("cafe.kot_print", kid=kot_id, auto=1)
    if key:
        _remember_save(key, out)
    return jsonify(out)


# ---------------------------------------------------------------- orders

@bp.route("/cafe/orders")
@cafe_view("bill_create")
def orders():
    oid = _org()
    base = """SELECT o.*, t.name table_name, u.name created_name,
                (SELECT COALESCE(SUM(price * qty), 0) FROM cafe_order_items i WHERE i.order_id = o.id) amount,
                (SELECT GROUP_CONCAT(qty || ' × ' || name || CASE WHEN measure IS NOT NULL THEN ' ' || measure || ' ' || unit ELSE '' END, ', ') FROM cafe_order_items i WHERE i.order_id = o.id
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
                db.execute("""INSERT INTO cafe_order_items (org_id, order_id, item_id, name, price, gst_rate, qty, note,
                              unit, measure, unit_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                           (r["org_id"], target["id"], r["item_id"], r["name"], r["price"], r["gst_rate"], r["qty"],
                            r["note"], r["unit"], r["measure"], r["unit_price"]))
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
    paper = request.args.get("paper") or st["paper"]
    kots = [(k, json.loads(k["lines"]), o)]
    bt = cafe_escpos.build(st, cafe_name(st), paper, kots=kots, order_types=ORDER_TYPES) if st["bt_print"] else None
    return render_template("cafe/print.html", st=st, kots=kots, bill=None, items=[], bt_link=bt,
                           auto=request.args.get("auto"), name=cafe_name(st), ORDER_TYPES=ORDER_TYPES,
                           paper=paper, back=url_for("cafe.orders"))


# ---------------------------------------------------------------- bills

def get_bill(bid):
    return H.query("""SELECT b.*, u.name created_name, cu.name cancelled_name FROM cafe_bills b
                      LEFT JOIN users u ON u.id = b.created_by LEFT JOIN users cu ON cu.id = b.cancelled_by
                      WHERE b.id = ? AND b.org_id = ?""", (bid, _org()), one=True) or abort(404)


def bill_whatsapp_text(st, b, items, link, balance=None, card=None):
    lines = [f"🧾 *{cafe_name(st)}*", f"Bill No: {st['bill_prefix']}{b['bill_no']} · {b['bill_date'][8:10]}-"
             f"{b['bill_date'][5:7]}-{b['bill_date'][:4]}", ""]
    for i in items:
        lines.append(f"{line_amount_text(i['qty'], i['measure'], i['unit'])} × {i['name']}  {rupees(i['amount'])}"
                     if i["unit"] in ("kg", "l") else f"{i['qty']} × {i['name']}  {rupees(i['amount'])}")
    lines.append("")
    if b["discount"]:
        lines.append(f"Discount: -{rupees(b['discount'])}")
    if b["points_redeemed"]:
        lines.append(f"Points used: {b['points_redeemed']} (-{rupees(b['points_value'])})")
    lines.append(f"*Total: {rupees(b['total'])}* ({pay_text(b)})")
    if b["points_earned"]:
        lines.append(f"🎁 Points earned: {b['points_earned']}" + (f" · Balance: {balance}" if balance is not None else ""))
    lines += ["", f"View bill: {link}"]
    if card:
        lines.append(f"🎁 Your points & bills: {card}")
    lines.append(st["bill_footer"] or "Thank you!")
    return "\n".join(lines)


@bp.route("/cafe/bills")
@cafe_view("bills_view")
def bills():
    oid = _org()
    t = today()
    d_from = H.valid_date(request.args.get("from")) or t
    d_to = H.valid_date(request.args.get("to")) or d_from
    mode = request.args.get("mode")
    if mode == "due":   # every bill still to be paid, whatever the date
        where, args = ["b.org_id = ?", "b.due > 0", "b.status = 'paid'"], [oid]
    else:
        where = ["b.org_id = ?", "b.bill_date BETWEEN ? AND ?"]
        args = [oid, d_from, d_to]
    if mode in PAYMENT_MODES:
        where.append(f"b.{dict(PAY_COLS)[mode]} > 0")
    elif mode == "Split":
        where.append("b.payment_mode = 'Split'")
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
    rows = H.query(f"""SELECT b.*, u.name created_name, (SELECT GROUP_CONCAT(qty || ' × ' || name || CASE WHEN measure IS NOT NULL THEN ' ' || measure || ' ' || unit ELSE '' END, ', ')
                         FROM cafe_bill_items i WHERE i.bill_id = b.id) summary
                       FROM cafe_bills b LEFT JOIN users u ON u.id = b.created_by WHERE {w}
                       ORDER BY b.id DESC LIMIT 50 OFFSET ?""", args + [(page - 1) * 50])
    sums = H.query(f"""SELECT COUNT(*) n, COALESCE(SUM(total), 0) total,
                         COALESCE(SUM(pay_cash), 0) cash, COALESCE(SUM(pay_upi), 0) upi,
                         COALESCE(SUM(pay_card), 0) card, COALESCE(SUM(due), 0) due,
                         COALESCE(SUM(discount), 0) discount, COALESCE(SUM(cgst + sgst), 0) tax
                       FROM cafe_bills b WHERE {w} AND b.status = 'paid'""", args, one=True)
    params = {k: v for k, v in request.args.items() if k != "page"}
    unpaid = H.query("SELECT COUNT(*) n, COALESCE(SUM(due), 0) due FROM cafe_bills WHERE org_id = ? AND due > 0"
                     " AND status = 'paid'", (oid,), one=True)
    return render_template("cafe/bills.html", unpaid=unpaid, rows=rows, sums=sums, d_from=d_from, d_to=d_to, mode=mode, status=status,
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
    wa_text = bill_whatsapp_text(st, b, items, link, cust["points"] if cust else None,
                                 card_url(cust["card_token"]) if cust and cust["card_token"] else None)
    kots = H.query("SELECT * FROM cafe_kots WHERE order_id = ? ORDER BY id", (b["order_id"],)) if b["order_id"] else []
    bt = cafe_escpos.build(st, cafe_name(st), st["paper"], bill=b, items=items, balance=cust["points"] if cust else None,
                           order_types=ORDER_TYPES) if st["bt_print"] else None
    return render_template("cafe/bill_view.html", b=b, items=items, cust=cust, st=st, link=link, wa_text=wa_text, bt_link=bt,
                           kots=kots, ORDER_TYPES=ORDER_TYPES, new=request.args.get("new"),
                           can_pay=can_change_payment(b), pay_log=json.loads(b["pay_log"] or "[]"),
                           card=card_url(cust["card_token"]) if cust and cust["card_token"] else None)


def can_change_payment(b):
    """Same-day bills and unpaid (Pay later) bills: anyone who bills. Older paid bills: the 'payment_edit' tick."""
    if b["status"] != "paid" or not can("bill_create"):
        return False
    return b["bill_date"] == today() or b["due"] > 0 or can("payment_edit")


@bp.route("/cafe/bills/<int:bid>/payment", methods=["POST"])
@cafe_view("bills_view")
def bill_payment(bid):
    """Change how a bill was paid (or receive the money for a Pay later bill). Items and total never change."""
    b = get_bill(bid)
    if not can_change_payment(b):
        flash("You are not allowed to change the payment of this bill." if b["status"] == "paid"
              else "A cancelled bill cannot be changed.", "error")
        return redirect(url_for("cafe.bill_view", bid=bid))
    f = request.form
    try:
        mode, cash, upi, card, due = parse_payment(f.get("mode"), {"cash": f.get("cash"), "upi": f.get("upi"),
                                                                   "card": f.get("card")}, b["total"])
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("cafe.bill_view", bid=bid))
    before = pay_text(b)
    new = {"payment_mode": mode, "pay_cash": cash, "pay_upi": upi, "pay_card": card, "due": due}
    after = pay_text(new)
    if (b["payment_mode"], b["pay_cash"], b["pay_upi"], b["pay_card"], b["due"]) == (mode, cash, upi, card, due):
        flash("Nothing changed.", "info")
        return redirect(url_for("cafe.bill_view", bid=bid))
    log = json.loads(b["pay_log"] or "[]")
    log.append({"at": H.now(), "by": g.user["name"], "from": before, "to": after})
    H.execute("UPDATE cafe_bills SET payment_mode = ?, pay_cash = ?, pay_upi = ?, pay_card = ?, due = ?, pay_log = ?"
              " WHERE id = ?", (mode, cash, upi, card, due, json.dumps(log[-50:], ensure_ascii=False), bid))
    flash(("Payment received: " if b["due"] and not due else "Payment changed: ") + after, "success")
    return redirect(url_for("cafe.bill_view", bid=bid))


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
    paper = request.args.get("paper") or st["paper"]
    bt = cafe_escpos.build(st, cafe_name(st), paper, bill=b, items=items, kots=kots,
                           balance=cust["points"] if cust else None, order_types=ORDER_TYPES) if st["bt_print"] else None
    embed = bool(request.args.get("embed"))
    card = None
    if b["customer_id"] and st.get("card_on", True):
        tok = H.scalar("SELECT card_token FROM customers WHERE id = ?", (b["customer_id"],))
        card = card_url(tok) if tok else None
    resp = make_response(render_template(
        "cafe/print.html", st=st, bill=b, items=items, kots=kots, auto=request.args.get("auto"), embed=embed,
        name=cafe_name(st), ORDER_TYPES=ORDER_TYPES, paper=paper, bt_link=bt, line_qty=line_amount_text,
        card=card, card_qr=qr_svg(card) if card and st.get("card_qr") else None,
        balance=cust["points"] if cust else None, back=url_for("cafe.bill_view", bid=bid, new=1)
        if request.args.get("auto") else url_for("cafe.bill_view", bid=bid)))
    if embed:
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"   # shown inside the billing screen
    return resp


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
                                         line_qty=line_amount_text,
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
    data = menu_data(org["id"], for_public=True, sort=st["sort_menu"])
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
    if any(w["price"] is not None or w["line_id"] for w in wanted):
        return jsonify(ok=False, error="Invalid data."), 400
    if sum(w["qty"] for w in wanted) > 100:
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
        set_order_lines(db, order_id, wanted, public=True)
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


# ---------------------------------------------------------------- My Card (customer's own page)

CARD_KINDS = {"earn": "Earned", "redeem": "Used on a bill", "referral": "Friend joined (Refer & Earn)",
              "referral_welcome": "Welcome points", "manual_add": "Added by the cafe", "manual_sub": "Removed by the cafe",
              "expired": "Expired", "reverse": "Bill cancelled", "refund": "Bill cancelled (points back)"}


def _card_customer(code, tok):
    org = public_org(code)
    c = H.query("SELECT * FROM customers WHERE card_token = ? AND org_id = ?", (tok, org["id"]), one=True) or abort(404)
    return org, c


@bp.route("/m/<code>/me/<tok>")
def my_card(code, tok):
    org, c = _card_customer(code, tok)
    expire_points(org["id"])
    c = H.query("SELECT * FROM customers WHERE id = ?", (c["id"],), one=True)
    st = get_settings(org["id"])
    soon = (today_date() + timedelta(days=7)).isoformat()
    expiring = H.query("""SELECT COALESCE(SUM(remaining), 0) pts, MIN(expires_at) first FROM points_ledger
                          WHERE customer_id = ? AND remaining > 0 AND expires_at IS NOT NULL AND expires_at <= ?""",
                       (c["id"], soon), one=True)
    history = [dict(r, label=CARD_KINDS.get(r["kind"], "Points"),
                    note=r["note"] if r["kind"] in ("earn", "redeem", "referral", "reverse", "refund") else "")
               for r in H.query("""SELECT change, kind, note, created_at FROM points_ledger WHERE customer_id = ?
                                   ORDER BY id DESC LIMIT 40""", (c["id"],))]
    bills = H.query("""SELECT bill_no, bill_date, total, token, points_earned, points_redeemed FROM cafe_bills
                       WHERE customer_id = ? AND status = 'paid' ORDER BY id DESC LIMIT 40""", (c["id"],))
    friends = H.scalar("SELECT COUNT(*) FROM customers WHERE referred_by = ? AND referral_rewarded = 1", (c["id"],))
    ref_link = menu_url(org, ref=c["referral_code"]) if st["referral"]["on"] else None
    colors, layout, font, shape = theme_css(st["theme"])
    resp = make_response(render_template(
        "cafe/my_card.html", org=org, st=st, c=c, expiring=expiring, history=history, bills=bills, friends=friends,
        ref_link=ref_link, value=c["points"] * point_value(st), colors=colors, font=font, shape=shape,
        menu_link=menu_url(org) if st["menu_on"] else None, name=cafe_name(st, org),
        wa=H.wa_number(st["whatsapp"] or st["phone"] or org["phone"])))
    resp.headers["X-Robots-Tag"] = "noindex"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/m/<code>/me/<tok>/birthday", methods=["POST"])
def my_card_birthday(code, tok):
    org, c = _card_customer(code, tok)
    st = get_settings(org["id"])
    bday = valid_birthday(request.form.get("birthday"))
    if st["birthday_on"] and bday and not c["birthday"]:
        H.execute("UPDATE customers SET birthday = ?, updated_at = ? WHERE id = ?", (bday, H.now(), c["id"]))
    return redirect(url_for("cafe.my_card", code=code, tok=tok))


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
