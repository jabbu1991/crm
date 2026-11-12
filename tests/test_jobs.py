"""Website careers form → agency CRM 'Job Applications', WhatsApp templates and admin phone notifications."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
from test_crm import crm, world  # noqa: E402,F401
import cafe_push  # noqa: E402
import jobs  # noqa: E402

SITE = {"Origin": "https://www.realviewcreativestudio.in"}
FORM = {"name": "Asha K", "whatsapp": "9847011223", "place": "Tirur", "role": "Graphic Designer",
        "job_type": ["Full-time", "Internship"], "location": ["Remote"], "experience": "Fresher",
        "portfolio": "https://behance.net/asha https://instagram.com/asha", "about": "I love design."}


def test_job_application_to_agency_crm(world, monkeypatch):
    w = world
    jobs._hits.clear()
    c = crm.app.test_client()
    assert c.post("/job-application", data=FORM, headers=SITE).status_code == 403      # origins not set
    monkeypatch.setattr(crm, "ENQUIRY_ORIGINS", {"https://www.realviewcreativestudio.in"})
    assert c.post("/job-application", data=FORM, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/job-application", data=dict(FORM, role=""), headers=SITE).status_code == 400
    # the Super Admin's phone is subscribed: it gets a notification
    sent = []
    monkeypatch.setattr(cafe_push, "sender", lambda s, data, k: sent.append(json.loads(data)))
    sup_id = w["uid"]("super")
    with w["db"]() as conn:
        agency = conn.execute("SELECT id FROM organizations WHERE kind = 'agency'").fetchone()[0]
        conn.execute("INSERT INTO push_subscriptions (org_id, user_id, endpoint, p256dh, auth, created_at)"
                     " VALUES (?,?,?,?,?, '2026-11-13')", (agency, sup_id, "https://push.example/1", "k", "a"))
    r = c.post("/job-application", data=dict(FORM, org_id=w["org_a"]), headers=SITE)
    assert r.status_code == 201 and r.headers["Access-Control-Allow-Origin"] == SITE["Origin"]
    with w["db"]() as conn:
        row = conn.execute("SELECT l.id, o.kind, p.code, p.name, l.requirement, l.place, l.notes, l.source"
                           " FROM leads l JOIN organizations o ON o.id = l.org_id JOIN projects p ON p.id = l.project_id"
                           " WHERE l.customer_name = 'Asha K'").fetchone()
        tpl = [t for (t,) in conn.execute("SELECT title FROM wa_templates WHERE org_id = ? AND scope = 'org'", (agency,))]
    lid = row[0]
    assert row[1:6] == ("agency", "JOBS", "Job Applications", "Graphic Designer", "Tirur") and row[7] == "Website"
    assert "Job type: Full-time, Internship" in row[6] and "instagram.com/asha" in row[6]
    assert "Application received" in tpl and "Interview call" in tpl
    assert sent and sent[0]["title"] == "🧑‍💼 New job application: Asha K" and sent[0]["url"] == f"/leads/{lid}"
    assert "Asha K" in w["sup"].get("/leads").get_data(as_text=True)
    assert "Asha K" not in w["a_admin"].get("/leads").get_data(as_text=True)
    # the same number again: no second lead, a line in its history; templates not added twice
    c.post("/job-application", data=dict(FORM, role="Video Editor"), headers=SITE)
    with w["db"]() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads WHERE phone_norm = '9847011223'").fetchone()[0] == 1
        hist = [h for (h,) in conn.execute("SELECT action FROM lead_activity WHERE lead_id = ?", (lid,))]
        assert conn.execute("SELECT COUNT(*) FROM wa_templates WHERE title = 'Interview call'").fetchone()[0] == 1
    assert any(h.startswith("Applied again on the website — Role: Video Editor") for h in hist)
    # honeypot and rate limit
    c.post("/job-application", data=dict(FORM, name="Bot", whatsapp="9847000999", website="x"), headers=SITE)
    with w["db"]() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads WHERE customer_name = 'Bot'").fetchone()[0] == 0
    jobs._hits.clear()
    codes = [c.post("/job-application", data=FORM, headers=SITE).status_code for _ in range(11)]
    assert codes[-1] == 429


def test_admin_notification_switch(world):
    w = world
    page = w["sup"].get("/account").get_data(as_text=True)
    assert "📲 Phone notifications" in page
    assert "📲 Phone notifications" not in w["a_admin"].get("/account").get_data(as_text=True)   # agency only
    assert w["a_s1"].get("/notify/key").status_code == 403
    sub = {"endpoint": "https://push.example/2", "keys": {"p256dh": "k", "auth": "a"}}
    assert w["sup"].post("/notify/subscribe", {"payload": json.dumps(sub)}).get_json()["ok"]
    assert w["sup"].post("/notify/subscribe", {"payload": json.dumps({"endpoint": "http://x"})}).status_code == 400
    with w["db"]() as conn:
        assert conn.execute("SELECT COUNT(*) FROM push_subscriptions WHERE endpoint = 'https://push.example/2'").fetchone()[0] == 1
    w["sup"].post("/notify/unsubscribe", {"endpoint": "https://push.example/2"})
    with w["db"]() as conn:
        assert conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0] == 0
