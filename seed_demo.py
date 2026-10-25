"""Fill a separate demo database with sample organizations, users, products and leads.

Used by demo.bat. Run:  CRM_DB_PATH=instance/demo.db python seed_demo.py
Does nothing if the demo database already has users.
"""
import os
import random
import re
import sqlite3
import sys

DB = os.environ.setdefault("CRM_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "demo.db"))
os.environ.pop("CRM_SETUP_KEY", None)

import app as crm  # noqa: E402  (reads CRM_DB_PATH on import)
from datetime import date  # noqa: E402

PASSWORD = "demo1234"


class Browser:
    def __init__(self):
        self.c = crm.app.test_client()

    def post(self, url, data):
        with self.c.session_transaction() as s:  # pages and logins replace the token; pin it first
            s["csrf_token"] = "demo"
        return self.c.post(url, data=dict(data, csrf_token="demo"), follow_redirects=True)

    def login(self, username):
        return self.post("/login", {"username": username, "password": PASSWORD})


def make_user_ready(username):
    b = Browser()
    b.login(username)
    b.post("/account", {"current_password": PASSWORD, "new_password": PASSWORD, "confirm": PASSWORD})
    with sqlite3.connect(DB) as conn:  # demo accounts keep the simple password, no forced change
        conn.execute("UPDATE users SET must_change_password = 0 WHERE username = ?", (username,))
    b.login(username)
    return b


def main():
    with sqlite3.connect(DB) as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            print("Demo data already exists.")
            return
    random.seed(7)
    sup = Browser()
    sup.post("/setup", {"agency": "Real View Creative Studio", "name": "Jabbar (Owner)", "username": "superadmin",
                        "password": PASSWORD})

    clients = {
        "ABC Motors": ("ABCM", "abc", ["Hyundai Creta", "Hyundai Venue", "Hyundai i20", "Extended Warranty"]),
        "Green Homes Realty": ("GREEN", "green", ["2BHK Flat", "3BHK Villa", "Commercial Plot"]),
    }
    places = [("Kochi", "Ernakulam", "Kerala"), ("Thrissur", "Thrissur", "Kerala"), ("Kozhikode", "Kozhikode", "Kerala"),
              ("Malappuram", "Malappuram", "Kerala"), ("Chennai", "Chennai", "Tamil Nadu"),
              ("Coimbatore", "Coimbatore", "Tamil Nadu"), ("Bengaluru", "Bengaluru Urban", "Karnataka")]
    names = ["Arun Nair", "Bina Thomas", "Chitra Menon", "Dev Raj", "Esha Pillai", "Farhan Ali", "Gita Iyer",
             "Hari Krishnan", "Irfan Khan", "Jaya Varma", "Kiran Das", "Leela George", "Manu Mohan", "Nisha Paul",
             "Omar Sheikh", "Priya Suresh"]
    for org_name, (code, prefix, products) in clients.items():
        sup.post("/admin/organizations/new", {"name": org_name, "code": code, "admin_name": f"{org_name} Admin",
                                              "admin_username": f"{prefix}_admin", "onboarding": "password",
                                              "password": PASSWORD})
        admin = make_user_ready(f"{prefix}_admin")
        for p in products:
            admin.post("/products/new", {"name": p, "code": re.sub(r"\W", "", p.upper())[:8], "active": "1"})
        admin.post("/projects/new", {"name": "Diwali Lead Campaign", "code": "DIW", "campaign_type": "Lead Generation",
                                     "status": "Active"})
        staff = []
        for i, staff_name in enumerate(["Anu Thomas", "Ben Joseph"], start=1):
            admin.post("/staff/new", {"name": staff_name, "username": f"{prefix}_staff{i}", "role": "staff",
                                      "active": "1", "designation": "Telecaller", "onboarding": "password",
                                      "password": PASSWORD})
            staff.append(make_user_ready(f"{prefix}_staff{i}"))
        with sqlite3.connect(DB) as conn:
            project = conn.execute("SELECT p.id FROM projects p JOIN organizations o ON o.id = p.org_id"
                                   " WHERE o.code = ?", (code,)).fetchone()[0]
            product_ids = [r[0] for r in conn.execute(
                "SELECT pr.id FROM products pr JOIN organizations o ON o.id = pr.org_id WHERE o.code = ?", (code,))]
        for i, n in enumerate(random.sample(names, 10)):
            who = [admin, staff[0], staff[1]][i % 3]
            place, district, state = random.choice(places)
            phone = f"98{random.randint(10000000, 99999999)}"
            who.post("/leads/new", {
                "project_id": project, "customer_name": n, "phone": phone, "whatsapp": phone,
                "email": n.split()[0].lower() + "@example.com", "place": place, "district": district, "state": state,
                "country": "India", "product_ids": [str(p) for p in random.sample(product_ids, random.choice([1, 1, 2]))],
                "status": random.choice(["New", "Contacted", "Interested", "Follow-up", "Negotiation", "Converted"]),
                "lead_quality": random.choice(["Hot", "Warm", "Cold"]),
                "buying_possibility": random.choice(["High", "Medium", "Low"]),
                "priority": random.choice(["High", "Normal", "Low"]),
                "source": random.choice(["Facebook", "Instagram", "Google Ads", "WhatsApp"]),
                "next_follow_up": random.choice(["", "2026-10-10", "2026-10-20"]),
                "requirement": "Wants price details", "confirm_duplicate": "1"})
    seed_cafe(sup)
    print("Demo data created.")


def demo_photo(label, hue, size=(600, 600)):
    """A simple coloured placeholder photo for demo menu items."""
    import io
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    for y in range(size[1]):
        t = y / size[1]
        d.line([(0, y), (size[0], y)], fill=(int(hue[0] * (1 - t) + 40 * t), int(hue[1] * (1 - t) + 30 * t),
                                              int(hue[2] * (1 - t) + 20 * t)))
    d.ellipse([size[0] * .18, size[1] * .2, size[0] * .82, size[1] * .84], fill=(250, 246, 238))
    d.ellipse([size[0] * .28, size[1] * .3, size[0] * .72, size[1] * .74], fill=hue)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


def seed_cafe(sup):
    """A demo cafe client: menu with photos and offers, tables, customers, a month of bills and expenses."""
    import json
    from datetime import date, timedelta
    sup.post("/admin/organizations/new", {"name": "Chai Corner", "code": "CHAI", "admin_name": "Chai Corner Admin",
                                          "admin_username": "cafe_admin", "onboarding": "password",
                                          "password": PASSWORD, "app_type": "cafe"})
    admin = make_user_ready("cafe_admin")
    admin.post("/staff/new", {"name": "Rahul (Cashier)", "username": "cafe_cashier", "role": "staff", "active": "1",
                              "designation": "Cashier", "onboarding": "password", "password": PASSWORD,
                              "cafe_role": "cashier"})
    make_user_ready("cafe_cashier")
    admin.post("/cafe/settings", {
        "name": "Chai Corner", "tagline": "Kerala chai, snacks & shawarma since 2019", "phone": "9847012345",
        "whatsapp": "9847012345", "address": "Main Road, Malappuram, Kerala", "paper": "80", "kot_on": "1",
        "kot_with_bill": "1", "menu_on": "1", "online_on": "1", "order_dine_in": "1", "order_takeaway": "1",
        "order_delivery": "1", "delivery_note": "Delivery within 3 km", "gst_inclusive": "1", "default_gst": "5",
        "bill_footer": "Thank you! Visit again 🙏", "loyalty_on": "1", "loyalty_mode": "percent",
        "loyalty_percent": "5", "loyalty_point_value": "1", "loyalty_max_redeem_pct": "50", "loyalty_min_redeem": "20",
        "loyalty_expiry_days": "180", "referral_on": "1", "referral_referrer_points": "50",
        "referral_new_points": "25", "birthday_on": "1"})
    menu = {
        "Tea & Coffee": [("Kerala Chai", "15", "", "veg", (176, 112, 60)), ("Sulaimani", "12", "", "veg", (150, 70, 20)),
                         ("Filter Coffee", "25", "20", "veg", (110, 70, 40))],
        "Juices": [("Fresh Lime", "30", "", "veg", (140, 190, 60)), ("Mango Shake", "80", "65", "veg", (240, 170, 30)),
                   ("Avil Milk", "90", "", "veg", (220, 190, 150))],
        "Snacks": [("Pazham Pori", "15", "", "veg", (220, 150, 40)), ("Chicken Puffs", "25", "", "nonveg", (200, 120, 50)),
                   ("Egg Puffs", "22", "", "egg", (230, 180, 70))],
        "Shawarma & Meals": [("Chicken Shawarma", "110", "99", "nonveg", (190, 90, 40)),
                             ("Beef Fry & Porotta", "150", "", "nonveg", (130, 50, 30)),
                             ("Veg Fried Rice", "120", "", "veg", (200, 160, 60))],
        "Bakery": [("Kerala Mixture", "480", "", "veg", (200, 140, 50), {"unit": "kg"}),
                   ("Halwa", "400", "", "veg", (170, 40, 40), {"unit": "kg"}),
                   ("Marie Biscuit Pack", "30", "", "veg", (210, 170, 110), {"barcode": "8901063010017"})],
    }
    for cat in menu:
        admin.post("/cafe/menu/category", {"action": "add", "name": cat})
    with sqlite3.connect(DB) as conn:
        cats = {n: i for i, n in conn.execute("SELECT id, name FROM menu_categories")}
    for cat, items in menu.items():
        for name, price, offer, ftype, hue, *extra in items:
            data = {"name": name, "price": price, "offer_price": offer, "food_type": ftype, "available": "1",
                    "category_id": cats[cat], "gst_rate": "5", "photo": (demo_photo(name, hue), "p.png")}
            data.update(extra[0] if extra else {})
            if name in ("Chicken Shawarma", "Kerala Chai"):
                data["tags"] = ["bestseller"]
            with admin.c.session_transaction() as sess:
                sess["csrf_token"] = "demo"
            admin.c.post("/cafe/menu/item/new", data=dict(data, csrf_token="demo"), content_type="multipart/form-data")
    admin.post("/cafe/tables/save", {"action": "bulk", "count": "8", "seats": "4"})
    with sqlite3.connect(DB) as conn:
        items = [r for r in conn.execute("SELECT id, name FROM menu_items")]
        weighed = {r[0] for r in conn.execute("SELECT id FROM menu_items WHERE unit != 'pc'")}
    people = [("Riya", "9847100001"), ("Sameer", "9847100002"), ("Fathima", "9847100003"), ("Arjun", "9847100004"),
              ("Nimmi", "9847100005"), ("Basil", "9847100006"), ("Hisham", "9847100007"), ("Divya", "9847100008")]
    today_ = date.today()
    for day in range(30, -1, -1):
        for n in range(random.randint(14, 26)):
            lines = [{"item_id": i, "qty": random.randint(1, 3)} if i not in weighed else
                     {"item_id": i, "qty": 1, "measure": random.choice([0.1, 0.25, 0.5, 1])}
                     for i, _ in random.sample(items, random.randint(1, 4))]
            who = random.choice(people + [("", "")] * 3)
            admin.post("/cafe/pos/save", {"payload": json.dumps({
                "action": "bill", "order_type": random.choice(["takeaway", "takeaway", "dine_in", "delivery"]),
                "table_id": None, "items": lines, "payment_mode": random.choice(["Cash", "Cash", "UPI", "UPI", "Card"]),
                "customer": {"phone": who[1], "name": who[0]}})})
        d = (today_ - timedelta(days=day)).isoformat()
        with sqlite3.connect(DB) as conn:
            conn.execute("UPDATE cafe_orders SET order_date = ?, created_at = ? || substr(created_at, 11)"
                         " WHERE order_date = ? AND id > COALESCE((SELECT MAX(id) FROM cafe_orders WHERE order_date < ?), 0)",
                         (d, d, today_.isoformat(), today_.isoformat()))
            conn.execute("UPDATE cafe_kots SET kot_date = ? WHERE kot_date = ? AND id > COALESCE("
                         "(SELECT MAX(id) FROM cafe_kots WHERE kot_date < ?), 0)", (d, today_.isoformat(), today_.isoformat()))
            conn.execute("UPDATE cafe_bills SET bill_date = ?, created_at = ? || substr(created_at, 11)"
                         " WHERE bill_date = ? AND id > COALESCE((SELECT MAX(id) FROM cafe_bills WHERE bill_date < ?), 0)",
                         (d, d, today_.isoformat(), today_.isoformat()))
        for cat, lo, hi in (("Raw materials", 600, 1600), ("Gas", 0, 400), ("Packing", 100, 300)):
            amt = random.randint(lo, hi)
            if amt:
                admin.post("/cafe/expenses/save", {"kind": "expense", "amount": str(amt), "category": cat,
                                                   "payment_mode": "Cash", "entry_date": d})
        if day % 15 == 0:
            admin.post("/cafe/expenses/save", {"kind": "expense", "amount": "6000", "category": "Salary",
                                               "payment_mode": "Bank", "entry_date": d})
    admin.post("/cafe/expenses/save", {"kind": "expense", "amount": "8000", "category": "Rent", "payment_mode": "Bank",
                                       "entry_date": today_.replace(day=1).isoformat()})
    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE customers SET birthday = ? WHERE wa_norm = '9847100003'", (today_.isoformat()[5:],))


if __name__ == "__main__":
    sys.exit(main())
