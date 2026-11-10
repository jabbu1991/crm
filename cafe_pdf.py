"""Bill as a small PDF file (receipt look, 80 mm wide), for the POS "Save" button: it downloads straight to the phone.

Built with fpdf2. English text uses DejaVu Sans; Malayalam names fall back to Noto Sans Malayalam (shaped with
uharfbuzz when it is installed). The page is exactly as tall as the bill.
"""
import os

from fpdf import FPDF

import cafe as _cafe
from cafe import fmt_measure, pay_text


FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
W = 80          # page width, mm
M = 5           # side margin, mm


def _money(paise):
    neg = paise < 0
    r, p = divmod(abs(int(paise)), 100)
    return ("-" if neg else "") + f"{r:,}.{p:02d}"


def _qty(i):
    if i["unit"] in ("kg", "l") and i["measure"]:
        m = fmt_measure(i["measure"], i["unit"])
        return f"{i['qty']} × {m}" if i["qty"] != 1 else m
    return str(i["qty"])


class _Pdf(FPDF):
    def setup(self):
        self.add_font("dv", "", os.path.join(FONT_DIR, "DejaVuSans.ttf"))
        self.add_font("dv", "B", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"))
        self.add_font("ml", "", os.path.join(FONT_DIR, "NotoSansMalayalam-Regular.ttf"))
        self.add_font("ml", "B", os.path.join(FONT_DIR, "NotoSansMalayalam-Bold.ttf"))
        self.set_fallback_fonts(["ml"])
        try:
            self.set_text_shaping(True)     # joins Malayalam letters correctly (needs uharfbuzz)
        except Exception:
            pass
        self.set_margins(M, 5, M)
        self.set_auto_page_break(False)
        self.set_text_color(17, 17, 17)

    def line_center(self, text, size=9, bold=False):
        self.set_font("dv", "B" if bold else "", size)
        self.multi_cell(0, size * 0.45, text, align="C", new_x="LMARGIN", new_y="NEXT")

    def pair(self, left, right, size=9, bold=False):
        self.set_font("dv", "B" if bold else "", size)
        y = self.get_y()
        self.cell(0, size * 0.45, left, new_x="LMARGIN", new_y="TOP")
        self.set_xy(M, y)
        self.cell(0, size * 0.45, right, align="R", new_x="LMARGIN", new_y="NEXT")

    def rule(self, double=False):
        self.ln(1)
        y = self.get_y()
        self.set_draw_color(120, 120, 120)
        self.set_line_width(0.2)
        self.set_dash_pattern(dash=0.8, gap=0.6)
        self.line(M, y, W - M, y)
        if double:
            self.line(M, y + 0.7, W - M, y + 0.7)
        self.set_dash_pattern()
        self.ln(1.6 if double else 1.2)


def _draw(pdf, st, name, bill, items, balance, order_types):
    pdf.line_center(name, 13, True)
    for extra in (st.get("address"), st.get("phone") and "Ph: " + st["phone"]):
        if extra:
            pdf.line_center(extra, 8)
    if bill["gst_on"] and bill["gstin"]:
        pdf.line_center("GSTIN: " + bill["gstin"], 8, True)
    if st.get("fssai"):
        pdf.line_center("FSSAI: " + st["fssai"], 8)
    pdf.ln(0.8)
    pdf.line_center("TAX INVOICE" if bill["gst_on"] else "BILL", 9, True)
    if bill["status"] == "cancelled":
        pdf.line_center("*** CANCELLED ***", 10, True)
    pdf.rule()
    d = bill["bill_date"]
    pdf.pair(f"Bill: {st.get('bill_prefix') or ''}{bill['bill_no']}", f"{d[8:10]}-{d[5:7]}-{d[:4]} {_cafe.H.ist_time(bill['created_at'], '%H:%M')}",
             bold=False)
    kind = (order_types or {}).get(bill["order_type"], "") + (f": {bill['table_name']}" if bill["table_name"] else "")
    pdf.pair(kind, pay_text(bill))
    if bill["cust_name"]:
        pdf.set_font("dv", "", 9)
        pdf.multi_cell(0, 4, "Customer: " + bill["cust_name"], align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.rule()
    inner = W - 2 * M
    cw = [inner - 34, 10, 24]      # item, qty, amount
    pdf.set_font("dv", "B", 8.5)
    pdf.cell(cw[0], 4, "Item")
    pdf.cell(cw[1], 4, "Qty", align="R")
    pdf.cell(cw[2], 4, "Amount", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.rule()
    pdf.set_font("dv", "", 9)
    for i in items:
        y = pdf.get_y()
        pdf.set_xy(M + cw[0], y)
        pdf.cell(cw[1], 4.2, _qty(i), align="R")
        pdf.cell(cw[2], 4.2, _money(i["amount"]), align="R")
        pdf.set_xy(M, y)
        pdf.multi_cell(cw[0] - 1, 4.2, i["name"], align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.4)
    pdf.rule()
    pdf.pair("Subtotal", _money(bill["subtotal"]))
    if bill["discount"]:
        pdf.pair("Discount", "-" + _money(bill["discount"]))
    if bill["gst_on"]:
        pdf.pair("Taxable value", _money(bill["taxable"]))
        pdf.pair("CGST", _money(bill["cgst"]))
        pdf.pair("SGST", _money(bill["sgst"]))
    if bill["points_redeemed"]:
        pdf.pair(f"Points used ({bill['points_redeemed']})", "-" + _money(bill["points_value"]))
    if bill["round_off"]:
        pdf.pair("Round off", _money(bill["round_off"]))
    pdf.rule(double=True)
    pdf.pair("TOTAL", "₹" + _money(bill["total"]), size=12, bold=True)
    pdf.rule(double=True)
    if bill["gst_on"] and bill["gst_inclusive"]:
        pdf.line_center("(Prices inclusive of GST)", 7.5)
    if bill["points_earned"] or balance is not None:
        pdf.line_center(f"Points earned: {bill['points_earned']}" + (f"  ·  Balance: {balance}" if balance is not None else ""), 8.5)
    if bill["due"]:
        pdf.line_center("NOT PAID: ₹" + _money(bill["due"]), 10, True)
    if st.get("bill_footer"):
        pdf.ln(1)
        pdf.line_center(st["bill_footer"], 8.5)


def bill_pdf(st, name, bill, items, balance=None, order_types=None):
    """PDF bytes of one bill. Drawn once to measure the height, then again on a page of that height."""
    probe = _Pdf(unit="mm", format=(W, 2000))
    probe.setup()
    probe.add_page()
    _draw(probe, st, name, bill, items, balance, order_types)
    height = max(60, probe.get_y() + 8)
    pdf = _Pdf(unit="mm", format=(W, height))
    pdf.setup()
    pdf.set_title(f"Bill {st.get('bill_prefix') or ''}{bill['bill_no']}")
    pdf.set_creator("Real View")
    pdf.add_page()
    _draw(pdf, st, name, bill, items, balance, order_types)
    return bytes(pdf.output())
