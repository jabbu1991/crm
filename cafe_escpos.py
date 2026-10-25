"""Receipts for thermal printers (ESC/POS), sent to the phone's RawBT app with one tap ("Bluetooth Print").

The page opens an Android intent link; RawBT receives the bytes and prints them on the paired Bluetooth printer,
with no print dialog. 58 mm paper = 32 characters per line, 80 mm = 48.
"""
import base64
import json
import re
import unicodedata

from cafe import fmt_measure

ESC, GS = b"\x1b", b"\x1d"


def _ascii(text):
    """Thermal printers print plain Latin text. Rupee -> Rs., accents dropped, anything else -> '?'."""
    text = (text or "").replace("₹", "Rs.").replace("–", "-").replace("—", "-").replace("×", "x")
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    text = re.sub(r"[\U00010000-\U0010ffff☀-➿️]", "", text)  # emoji
    return text.encode("ascii", "replace").decode("ascii")


def _money(paise):
    neg = paise < 0
    r, p = divmod(abs(int(paise)), 100)
    return ("-" if neg else "") + f"{r}.{p:02d}"


class Receipt:
    def __init__(self, paper="80"):
        self.width = 32 if paper == "58" else 48
        self.buf = bytearray(ESC + b"@" + ESC + b"t\x00")  # reset, code page 437

    def raw(self, b):
        self.buf += b

    def text(self, s="", align="left", bold=False, big=False):
        self.raw(ESC + b"a" + {"left": b"\x00", "center": b"\x01", "right": b"\x02"}[align])
        self.raw(ESC + b"E" + (b"\x01" if bold else b"\x00"))
        self.raw(GS + b"!" + (b"\x11" if big else b"\x00"))
        width = self.width // 2 if big else self.width
        for line in self._wrap(_ascii(s), width) or [""]:
            self.raw(line.encode("ascii") + b"\n")
        self.raw(GS + b"!\x00" + ESC + b"E\x00" + ESC + b"a\x00")

    @staticmethod
    def _wrap(s, width):
        out = []
        for part in s.split("\n"):
            while len(part) > width:
                cut = part.rfind(" ", 0, width + 1)
                cut = cut if cut > 0 else width
                out.append(part[:cut].rstrip())
                part = part[cut:].lstrip()
            out.append(part)
        return out

    def rule(self, ch="-"):
        self.raw((ch * self.width).encode() + b"\n")

    def pair(self, left, right, bold=False, big=False):
        left, right = _ascii(left), _ascii(right)
        width = self.width // 2 if big else self.width
        if len(left) + len(right) + 1 > width:
            left = left[:max(1, width - len(right) - 1)]
        self.raw(ESC + b"E" + (b"\x01" if bold else b"\x00") + GS + b"!" + (b"\x11" if big else b"\x00"))
        self.raw((left + " " * (width - len(left) - len(right)) + right).encode("ascii") + b"\n")
        self.raw(GS + b"!\x00" + ESC + b"E\x00")

    def columns(self, cells, widths, aligns):
        """One table row; the first cell may wrap onto more lines."""
        first = self._wrap(_ascii(cells[0]), widths[0]) or [""]
        rest = [_ascii(c)[:w] for c, w in zip(cells[1:], widths[1:])]
        line = first[0].ljust(widths[0])
        for c, w, a in zip(rest, widths[1:], aligns[1:]):
            line += c.rjust(w) if a == "right" else c.ljust(w)
        self.raw(line.encode("ascii") + b"\n")
        for extra in first[1:]:
            self.raw(("  " + extra).encode("ascii")[:self.width] + b"\n")

    def cut(self):
        self.raw(b"\n\n\n" + GS + b"V\x42\x00")  # feed and (partial) cut; printers without a cutter just feed

    def bytes(self):
        return bytes(self.buf)


def _qty_text(qty, measure, unit):
    if unit in ("kg", "l") and measure:
        m = fmt_measure(measure, unit).replace(" ", "")
        return f"{qty}x{m}" if qty != 1 else m
    return str(qty)


def bill_receipt(r, st, name, bill, items, balance=None, order_types=None):
    w = r.width
    r.text(name, "center", bold=True, big=True)
    for extra in (st.get("address"), st.get("phone") and "Ph: " + st["phone"]):
        if extra:
            r.text(extra, "center")
    if bill["gst_on"] and bill["gstin"]:
        r.text("GSTIN: " + bill["gstin"], "center", bold=True)
    if st.get("fssai"):
        r.text("FSSAI: " + st["fssai"], "center")
    r.text("TAX INVOICE" if bill["gst_on"] else "BILL", "center", bold=True)
    if bill["status"] == "cancelled":
        r.text("*** CANCELLED ***", "center", bold=True)
    r.rule()
    d = bill["bill_date"]
    r.pair(f"Bill: {st.get('bill_prefix') or ''}{bill['bill_no']}", f"{d[8:10]}-{d[5:7]}-{d[:4]} {bill['created_at'][11:16]}")
    kind = (order_types or {}).get(bill["order_type"], "") + (f": {bill['table_name']}" if bill["table_name"] else "")
    r.pair(kind, bill["payment_mode"] or "")
    if bill["cust_name"]:
        r.text("Customer: " + bill["cust_name"])
    r.rule()
    if w >= 48:
        widths, aligns = [20, 8, 10, 10], ["left", "right", "right", "right"]
        r.columns(["Item", "Qty", "Rate", "Amount"], widths, aligns)
    else:
        widths, aligns = [17, 6, 9], ["left", "right", "right"]
        r.columns(["Item", "Qty", "Amount"], widths, aligns)
    r.rule()
    for i in items:
        q = _qty_text(i["qty"], i["measure"], i["unit"])
        if w >= 48:
            if i["unit"] in ("kg", "l") and i["unit_price"] and not i["price_by"]:
                rate = _money(i["unit_price"]) + ("/kg" if i["unit"] == "kg" else "/L")
            else:
                rate = _money(i["price"])
            r.columns([i["name"], q, rate, _money(i["amount"])], widths, aligns)
        else:
            r.columns([i["name"], q, _money(i["amount"])], widths, aligns)
    r.rule()
    r.pair("Subtotal", _money(bill["subtotal"]))
    if bill["discount"]:
        r.pair("Discount", "-" + _money(bill["discount"]))
    if bill["gst_on"]:
        r.pair("Taxable value", _money(bill["taxable"]))
        r.pair("CGST", _money(bill["cgst"]))
        r.pair("SGST", _money(bill["sgst"]))
    if bill["points_redeemed"]:
        r.pair(f"Points used ({bill['points_redeemed']})", "-" + _money(bill["points_value"]))
    if bill["round_off"]:
        r.pair("Round off", _money(bill["round_off"]))
    r.rule("=")
    r.pair("TOTAL", "Rs." + _money(bill["total"]), bold=True, big=w >= 48)
    if w < 48:
        r.text("", "left")
    r.rule("=")
    if bill["gst_on"] and bill["gst_inclusive"]:
        r.text("(Prices inclusive of GST)", "center")
    if bill["points_earned"] or balance is not None:
        r.text(f"Points earned: {bill['points_earned']}" + (f"  Balance: {balance}" if balance is not None else ""),
               "center")
    if st.get("bill_footer"):
        r.text(st["bill_footer"], "center")
    r.cut()


def kot_receipt(r, kot, lines, order, order_types=None):
    r.text(f"KOT #{kot['kot_no']}", "center", bold=True, big=True)
    if order["order_type"] == "dine_in":
        place = order["table_name"] or "Table"
    else:
        place = (order_types or {}).get(order["order_type"], order["order_type"]).upper()
    r.text(place, "center", bold=True, big=True)
    r.pair(f"Order #{order['order_no']}", kot["created_at"][11:16])
    if order["cust_name"]:
        r.text(order["cust_name"])
    r.rule()
    for l in lines:
        qty = l["qty"]
        label = l["name"] + (f" ({l['measure']})" if l.get("measure") else "")
        if qty < 0:
            r.text(f"CANCEL {-qty} x {label}", bold=True)
        else:
            r.text(f"{qty} x {label}", bold=True, big=r.width >= 48)
        if l.get("note"):
            r.text("   -> " + l["note"])
    if order["note"]:
        r.rule()
        r.text("Note: " + order["note"], bold=True)
    r.cut()


def rawbt_link(data):
    """Android intent link that hands the bytes to the RawBT app."""
    b64 = base64.b64encode(data).decode("ascii")
    return f"intent:base64,{b64}#Intent;scheme=rawbt;package=ru.a402d.rawbtprinter;end;"


def build(st, name, paper, bill=None, items=(), kots=(), balance=None, order_types=None):
    r = Receipt(paper if paper in ("58", "80") else "80")
    if bill is not None:
        bill_receipt(r, st, name, bill, items, balance, order_types)
    for k, lines, order in kots:
        kot_receipt(r, k, lines if isinstance(lines, list) else json.loads(lines), order, order_types)
    return rawbt_link(r.bytes())
