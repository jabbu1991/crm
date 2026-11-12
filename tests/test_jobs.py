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
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0          # not a lead
        row = conn.execute("SELECT id, org_id, role, place, job_type, work_location, experience, link, status,"
                           " applied_before FROM job_applications WHERE name = 'Asha K'").fetchone()
    aid = row[0]
    assert row[1:] == (agency, "Graphic Designer", "Tirur", "Full-time, Internship", "Remote", "Fresher",
                       "https://behance.net/asha https://instagram.com/asha", "New", 0)
    assert sent and sent[0]["title"] == "🧑‍💼 New job application: Asha K" and sent[0]["url"] == f"/job-applications#app{aid}"
    page = w["sup"].get("/job-applications").get_data(as_text=True)
    assert "Asha K" in page and "Our team will review it" in page and 'href="https://instagram.com/asha"' in page
    assert w["a_admin"].get("/job-applications").status_code == 404           # agency only
    assert w["a_s1"].get("/job-applications").status_code == 403
    # status and note
    w["sup"].post(f"/job-applications/{aid}", {"status": "Shortlisted", "note": "Good portfolio"})
    with w["db"]() as conn:
        assert conn.execute("SELECT status, note FROM job_applications WHERE id = ?", (aid,)).fetchone() == (
            "Shortlisted", "Good portfolio")
    assert "Asha K" in w["sup"].get("/job-applications?status=Shortlisted").get_data(as_text=True)
    assert "Asha K" not in w["sup"].get("/job-applications?status=New").get_data(as_text=True)
    assert w["b_admin"].post(f"/job-applications/{aid}", {"status": "Rejected"}).status_code == 404
    x = w["sup"].get("/job-applications.xlsx")
    assert x.status_code == 200 and x.data[:2] == b"PK"
    # the same number again: a new row marked "Applied before"
    c.post("/job-application", data=dict(FORM, role="Video Editor"), headers=SITE)
    with w["db"]() as conn:
        assert conn.execute("SELECT applied_before FROM job_applications WHERE role = 'Video Editor'").fetchone()[0] == 1
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


def test_old_lead_applications_move_to_the_page(world):
    w = world
    sup = w["sup"]
    agency = w["ids"]("SELECT id FROM organizations WHERE kind = 'agency'")[0]
    uid = w["uid"]("super")
    with w["db"]() as conn:
        pid = conn.execute("INSERT INTO projects (org_id, name, code, status, created_by, created_at) VALUES"
                           " (?, 'Job Applications', 'JOBS', 'Active', ?, '2026-11-12 20:00:00')", (agency, uid)).lastrowid
        lid = conn.execute("INSERT INTO leads (org_id, project_id, customer_name, phone, whatsapp, phone_norm, place,"
                           " requirement, notes, status, entered_by, created_at, updated_at) VALUES"
                           " (?,?, 'Old Applicant', '9847022334', '9847022334', '9847022334', 'Kochi', 'Video Editor',"
                           " ?, 'New', ?, '2026-11-12 20:00:00', '2026-11-12 20:00:00')",
                           (agency, pid, "Role: Video Editor\nJob type: Part-time\nExperience: 3+ years\n"
                                         "Portfolio / Resume / Instagram: youtube.com/x", uid)).lastrowid
    page = sup.get("/job-applications").get_data(as_text=True)
    assert "Old Applicant" in page and "moved here from Leads" in page
    with w["db"]() as conn:
        row = conn.execute("SELECT role, job_type, experience, link, place, created_at FROM job_applications").fetchone()
        assert row == ("Video Editor", "Part-time", "3+ years", "youtube.com/x", "Kochi", "2026-11-12 20:00:00")
        assert conn.execute("SELECT COUNT(*) FROM leads WHERE id = ?", (lid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM deleted_leads WHERE lead_id = ?", (lid,)).fetchone()[0] == 1
    # opening again moves nothing twice
    sup.get("/job-applications")
    assert w["ids"]("SELECT COUNT(*) FROM job_applications")[0] == 1
