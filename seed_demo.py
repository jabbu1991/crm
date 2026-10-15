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
    print("Demo data created.")


if __name__ == "__main__":
    sys.exit(main())
