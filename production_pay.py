"""Production: the month an order counts in for its maker and its order taker, and the amounts shown per month.

Maker: a date chosen by hand (maker_month, when the maker is assigned or later), otherwise the month the order reached
Ready for Dispatch (done_date). Order taker: a date chosen by hand on the order form (taker_month), otherwise the
Dispatch (GST bill) date once the order is dispatched, and the order date before that. Amounts are only typed in:
the maker amount and the order taker's incentive (taker_amount); the month tables add them up. Nothing is required.
(Columns are stored as dates; older values may hold only YYYY-MM.)
"""
import re

import production as P

MONTH_RE = re.compile(r"\d{4}-\d{2}")
MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def month_label(k):
    return f"{MN[int(k[5:7]) - 1]} {k[:4]}" if k and MONTH_RE.match(k) else ""


def maker_month(o):
    """YYYY-MM the order counts in for its maker, or None (not finished and no date chosen)."""
    if o["maker_month"]:
        return o["maker_month"][:7]
    d = P.done_date(o)
    return d.isoformat()[:7] if d else None


def taker_month(o):
    if o["taker_month"]:
        return o["taker_month"][:7]
    if o["bill_date"] and P.phase(o) in ("dispatched", "delivered"):
        return o["bill_date"][:7]
    return (o["order_date"] or "")[:7] or None


def init_templates(app):
    app.jinja_env.globals.update(month_label=month_label)
