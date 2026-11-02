"""Production: payments to makers and order takers, and the month an order counts in for them.

Maker: the amount is the order's maker amount; the month is the one set by hand (maker_month), otherwise the month
production finished (done_date: Ready for Dispatch day, or a finished date set by hand). Order taker: the amount is the
incentive typed by the Admin (taker_amount); the month is the one set by hand (taker_month), otherwise the order date.
Payments (prod_pay) can be split (advance + balance); the status is worked out from paid vs amount.
Maker money needs the 'maker' tick, order-taker incentives the 'incentive' tick. The customer never sees any of it.
"""
import re

from flask import abort, flash, g, redirect, request, url_for

import production as P
from production import bp


class _Host:
    def __getattr__(self, name):
        return getattr(P.H, name)


H = _Host()

KINDS = {"maker": "maker", "taker": "incentive"}          # kind -> permission
MODES = ["Cash", "UPI", "Bank transfer", "Cheque"]
MONTH_RE = re.compile(r"\d{4}-\d{2}")
MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def rs_text(paise):
    import cafe
    return cafe.rupees(paise)


def month_label(k):
    return f"{MN[int(k[5:]) - 1]} {k[:4]}" if k and MONTH_RE.fullmatch(k) else ""


def maker_month(o):
    """YYYY-MM the order counts in for its maker, or None (not finished and not set by hand)."""
    if o["maker_month"]:
        return o["maker_month"]
    d = P.done_date(o)
    return d.isoformat()[:7] if d else None


def taker_month(o):
    return o["taker_month"] or (o["order_date"] or "")[:7] or None


def due_amount(o, kind):
    return o["maker_amount"] if kind == "maker" else o["taker_amount"]


def paid_totals(oid, kind, order_ids=None):
    """{order_id: paid paise} for one kind (all orders of the organization, or only order_ids)."""
    rows = H.query("SELECT order_id, SUM(amount) s FROM prod_pay WHERE org_id = ? AND kind = ? GROUP BY order_id",
                   (oid, kind))
    out = {r["order_id"]: r["s"] or 0 for r in rows}
    if order_ids is not None:
        out = {k: v for k, v in out.items() if k in set(order_ids)}
    return out


def state(due, paid):
    """'none' (no amount entered), 'unpaid', 'part', 'paid' or 'over' (paid more than the amount)."""
    if due is None:
        return "paid" if paid else "none"
    if paid <= 0:
        return "unpaid" if due > 0 else "paid"
    if paid < due:
        return "part"
    return "over" if paid > due else "paid"


STATE_LABEL = {"none": "No amount", "unpaid": "🔴 Not paid", "part": "🟠 Part paid", "paid": "🟢 Paid",
               "over": "🟣 Paid more"}


def summarise(items, kind):
    """Adds due / paid / balance / state to each item ({'o': order, 'paid': paise}) and returns totals."""
    tot = {"due": 0, "paid": 0, "balance": 0, "unpaid": 0}
    for i in items:
        due = due_amount(i["o"], kind)
        i["due"] = due
        i["balance"] = max((due or 0) - i["paid"], 0)
        i["state"] = state(due, i["paid"])
        tot["due"] += due or 0
        tot["paid"] += i["paid"]
        tot["balance"] += i["balance"]
        tot["unpaid"] += 1 if i["balance"] > 0 else 0
    return tot


def order_ctx(o):
    """Payments, month and status for the order page (only the kinds this person may see)."""
    out = {}
    for kind, perm in KINDS.items():
        if not P.can(perm):
            continue
        pays = H.query("""SELECT p.*, u.name by_name FROM prod_pay p LEFT JOIN users u ON u.id = p.created_by
                          WHERE p.order_id = ? AND p.kind = ? ORDER BY p.pay_date, p.id""", (o["id"], kind))
        paid = sum(p["amount"] for p in pays)
        due = due_amount(o, kind)
        month = maker_month(o) if kind == "maker" else taker_month(o)
        out[kind] = {"pays": pays, "paid": paid, "due": due, "balance": max((due or 0) - paid, 0),
                     "state": state(due, paid), "month": month, "month_label": month_label(month),
                     "manual": bool(o["maker_month"] if kind == "maker" else o["taker_month"])}
    return out


def need(kind):
    if kind not in KINDS:
        abort(404)
    if g.get("viewing") or not P.can(KINDS[kind]):
        abort(403)


def back(order_id=None):
    nxt = request.form.get("next") or ""
    if nxt.startswith("/production/") and "//" not in nxt:
        return redirect(nxt)
    return redirect(url_for("prod.order_view", order_id=order_id) if order_id else url_for("prod.home"))


def who(kind):
    return "maker" if kind == "maker" else "order taker"


def add_payment(db, o, kind, day, amount, mode, note, ts):
    db.execute("""INSERT INTO prod_pay (org_id, order_id, kind, pay_date, amount, mode, note, created_by, created_at)
                  VALUES (?,?,?,?,?,?,?,?,?)""", (H.org_id(), o["id"], kind, day, amount, mode, note, g.user["id"], ts))
    # amounts stay out of the order history (staff without the money tick can read the history)
    P.log(db, o["id"], o["stage_name"], f"Payment to the {who(kind)} added ({day[8:10]}-{day[5:7]}-{day[:4]}"
          f"{', ' + mode if mode else ''})", ts=ts)


@bp.route("/production/orders/<int:order_id>/pay/<kind>", methods=["POST"])
@P.prod_view()
def order_pay(order_id, kind):
    need(kind)
    o = P.get_order(None, order_id)
    f = request.form
    amount = P.money_in(f.get("amount"))
    day = H.valid_date(P.clean(f.get("pay_date"), 10)) or P.today()
    mode = f.get("mode") if f.get("mode") in MODES else ""
    if amount in (None, "bad") or amount <= 0:
        flash("Enter the amount paid.", "error")
        return back(order_id)
    db = H.get_db()
    add_payment(db, o, kind, day, amount, mode, P.clean(f.get("note"), 200), P._now())
    db.commit()
    flash(f"Payment to the {who(kind)} saved.", "success")
    return back(order_id)


@bp.route("/production/orders/<int:order_id>/pay/<kind>/<int:pay_id>/delete", methods=["POST"])
@P.prod_view()
def order_pay_delete(order_id, kind, pay_id):
    need(kind)
    o = P.get_order(None, order_id)
    p = H.query("SELECT * FROM prod_pay WHERE id = ? AND order_id = ? AND kind = ? AND org_id = ?",
                (pay_id, order_id, kind, H.org_id()), one=True) or abort(404)
    db = H.get_db()
    db.execute("DELETE FROM prod_pay WHERE id = ?", (pay_id,))
    P.log(db, order_id, o["stage_name"], f"Payment to the {who(kind)} of "
          f"{p['pay_date'][8:10]}-{p['pay_date'][5:7]}-{p['pay_date'][:4]} removed", ts=P._now())
    db.commit()
    flash("Payment removed.", "success")
    return back(order_id)


def set_month(db, o, kind, month, ts):
    col = "maker_month" if kind == "maker" else "taker_month"
    if (o[col] or "") == (month or ""):
        return False
    db.execute(f"UPDATE prod_orders SET {col} = ?, updated_by = ?, updated_at = ? WHERE id = ? AND org_id = ?",
               (month or None, g.user["id"], ts, o["id"], H.org_id()))
    P.log(db, o["id"], o["stage_name"], f"Counts in {month_label(month)} for the {who(kind)} (set by hand)" if month
          else f"Month for the {who(kind)}: automatic again", ts=ts)
    return True


@bp.route("/production/orders/<int:order_id>/month/<kind>", methods=["POST"])
@P.prod_view()
def order_month(order_id, kind):
    need(kind)
    o = P.get_order(None, order_id)
    month = (request.form.get("month") or "").strip()[:7]
    if month and not MONTH_RE.fullmatch(month):
        flash("Choose a month.", "error")
        return back(order_id)
    if kind == "maker" and not o["maker_id"]:
        flash("Assign a maker first.", "error")
        return back(order_id)
    db = H.get_db()
    set_month(db, o, kind, month, P._now())
    db.commit()
    flash(f"Counts in {month_label(month)} for the {who(kind)}." if month else "Month is automatic again.", "success")
    return back(order_id)


@bp.route("/production/orders/<int:order_id>/incentive", methods=["POST"])
@P.prod_view()
def order_incentive(order_id):
    need("taker")
    o = P.get_order(None, order_id)
    amt = P.money_in(request.form.get("taker_amount"))
    if amt == "bad":
        flash("Incentive is not a number.", "error")
        return back(order_id)
    db = H.get_db()
    db.execute("UPDATE prod_orders SET taker_amount = ?, updated_by = ?, updated_at = ? WHERE id = ? AND org_id = ?",
               (amt, g.user["id"], P._now(), order_id, H.org_id()))
    P.log(db, order_id, o["stage_name"], "Order taker incentive changed", ts=P._now())
    db.commit()
    flash("Incentive saved.", "success")
    return back(order_id)


@bp.route("/production/pay/bulk/<kind>", methods=["POST"])
@P.prod_view()
def pay_bulk(kind):
    """Maker / order-taker month page: move the ticked orders to another month, or pay their balance in one go."""
    need(kind)
    f = request.form
    ids = [int(x) for x in f.getlist("ids") if x.isdigit()]
    if not ids:
        flash("Tick at least one order.", "error")
        return back()
    oid = H.org_id()
    db = H.get_db()
    ts = P._now()
    orders = [P.get_order(None, i) for i in ids]
    done = 0
    if f.get("action") == "move":
        month = (f.get("month") or "").strip()[:7]
        if month and not MONTH_RE.fullmatch(month):
            flash("Choose a month.", "error")
            return back()
        for o in orders:
            if kind == "maker" and not o["maker_id"]:
                continue
            done += set_month(db, o, kind, month, ts)
        db.commit()
        flash(f"{done} order{'s' if done != 1 else ''} moved to {month_label(month)}." if month
              else f"{done} order{'s' if done != 1 else ''} back to the automatic month.", "success")
    elif f.get("action") == "settle":
        day = H.valid_date(P.clean(f.get("pay_date"), 10)) or P.today()
        mode = f.get("mode") if f.get("mode") in MODES else ""
        paid = paid_totals(oid, kind, ids)
        total = 0
        for o in orders:
            bal = (due_amount(o, kind) or 0) - paid.get(o["id"], 0)
            if bal > 0:
                add_payment(db, o, kind, day, bal, mode, "Settled", ts)
                done += 1
                total += bal
        db.commit()
        flash(f"{done} order{'s' if done != 1 else ''} settled: {rs_text(total)} paid." if done
              else "Nothing to pay: the ticked orders have no balance (or no amount entered).",
              "success" if done else "warning")
    else:
        abort(400)
    return back()


def balances(oid, kind):
    """{person id: balance due} (makers by maker_id, order takers by taken_by)."""
    col = "maker_id" if kind == "maker" else "taken_by"
    amt = "maker_amount" if kind == "maker" else "taker_amount"
    rows = H.query(f"""SELECT o.{col} pid, SUM(MAX(COALESCE(o.{amt}, 0) - COALESCE((SELECT SUM(p.amount) FROM prod_pay p
                         WHERE p.order_id = o.id AND p.kind = ?), 0), 0)) bal
                       FROM prod_orders o WHERE o.org_id = ? AND o.status = 'active' AND o.{col} IS NOT NULL
                       GROUP BY o.{col}""", (kind, oid))
    return {r["pid"]: r["bal"] or 0 for r in rows}


def months_options(n=24, ahead=2):
    """Month choices for the 'move to month' boxes: two months ahead, then back n months."""
    t = P.today_date()
    y, m = t.year, t.month
    for _ in range(ahead):
        m += 1
        if m == 13:
            y, m = y + 1, 1
    out = []
    for _ in range(n + ahead):
        k = f"{y:04d}-{m:02d}"
        out.append((k, month_label(k)))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def init_templates(app):
    app.jinja_env.globals.update(pay_modes=MODES, pay_state_label=STATE_LABEL, month_label=month_label,
                                 months_options=months_options)
