"""📥 Lead Capture: Super Admin switches, Quick Add, Duplicate Alert, lead forms, catalogue enquiry, Google Forms."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_crm import Browser, add_lead, world  # noqa: E402,F401
import capture  # noqa: E402

ALL = ["quick_add", "contacts", "share", "paste", "dup_alert", "form_link", "catalog_enquiry", "gforms"]


def switch(w, org_id, feats):
    data = {f"f_{org_id}_{k}": "1" for k in feats}
    r = w["sup"].post("/admin/lead-capture", data)
    assert "Saved" in r.text


def one(w, sql, args=()):
    with w["db"]() as conn:
        return conn.execute(sql, args).fetchone()


def test_switches_and_staff_tools(world):
    w = world
    capture._hits.clear()
    admin, s1, s2 = w["a_admin"], w["a_s1"], w["a_s2"]
    # off by default: no page, no ＋ Lead button, no endpoints
    assert admin.get("/lead-capture").status_code == 404
    assert "qa-fab" not in s1.get("/").get_data(as_text=True)
    assert s1.post("/leads/quick", {"customer_name": "X", "phone": "9847000001"}).status_code == 404
    assert s1.get("/admin/lead-capture").status_code == 403
    grid = w["sup"].get("/admin/lead-capture").get_data(as_text=True)
    assert "Org A" in grid and "Meta Lead Ads" in grid and "coming soon" in grid
    switch(w, w["org_a"], ALL + ["meta_ads"])           # "coming soon" ways cannot be switched on
    with w["db"]() as conn:
        on = {r[0] for r in conn.execute("SELECT feature FROM org_capture WHERE org_id = ?", (w["org_a"],))}
    assert on == set(ALL)
    assert one(w, "SELECT capture_token FROM organizations WHERE id = ?", (w["org_a"],))[0]
    # the company is told; the other company sees nothing
    assert "📥 Lead Capture: Quick Add" in s1.get("/whats-new").get_data(as_text=True)
    assert "📥 Lead Capture: Quick Add" not in w["b_s1"].get("/whats-new").get_data(as_text=True)
    assert w["b_admin"].get("/lead-capture").status_code == 404
    assert w["b_s1"].post("/leads/quick", {"customer_name": "X", "phone": "9847000001"}).status_code == 404
    # staff: ＋ Lead and the tools; the Lead Capture page is for admins
    home = s1.get("/").get_data(as_text=True)
    assert "qa-fab" in home and "Paste &amp; Fill" in home and "capture.js" in home
    assert s1.get("/lead-capture").status_code == 403
    page = admin.get("/lead-capture").get_data(as_text=True)
    assert "Quick Add" in page and "Google Forms Connect" in page and "onSubmit" in page
    # Quick Add
    r = s1.post("/leads/quick", {"project_id": w["proj_a"], "customer_name": "Quick One", "phone": "98470 12345",
                                 "place": "Kochi", "product_id": w["prod_a1"], "notes": "wants demo"})
    j = r.get_json()
    assert j["ok"] and j["name"] == "Quick One"
    lid = j["id"]
    row = one(w, "SELECT org_id, entered_by, assigned_to, place, notes, whatsapp, phone_norm FROM leads WHERE id = ?", (lid,))
    assert row[:6] == (w["org_a"], w["uid"]("a_staff1"), w["uid"]("a_staff1"), "Kochi", "wants demo", "98470 12345")
    assert one(w, "SELECT COUNT(*) FROM lead_products WHERE lead_id = ?", (lid,))[0] == 1
    assert s1.post("/leads/quick", {"customer_name": "No project", "phone": "9847000002"}).status_code == 400
    # another company's product is ignored
    j = s1.post("/leads/quick", {"project_id": w["proj_a"], "customer_name": "P", "phone": "9847000003",
                                 "product_id": w["prod_b1"]}).get_json()
    assert j["ok"] and one(w, "SELECT COUNT(*) FROM lead_products WHERE lead_id = ?", (j["id"],))[0] == 0
    # Duplicate Alert: the same phone warns, "save anyway" saves
    j = s1.post("/leads/quick", {"project_id": w["proj_a"], "customer_name": "Again", "phone": "+91 9847012345"}).get_json()
    assert j["duplicate"] and j["matches"][0]["name"] == "Quick One"
    j = s1.post("/leads/quick", {"project_id": w["proj_a"], "customer_name": "Again", "phone": "+91 9847012345",
                                 "confirm_duplicate": "1"}).get_json()
    assert j["ok"]
    d = s1.get("/leads/dup-check?phone=9847012345").get_json()
    assert len(d["matches"]) == 2 and d["hidden"] == 0
    d = s2.get("/leads/dup-check?phone=9847012345").get_json()       # another staff member's lead: count only
    assert d["matches"] == [] and d["hidden"] == 2
    assert w["b_s1"].get("/leads/dup-check?phone=9847012345").status_code == 404
    assert s1.get("/leads/dup-check?phone=12").get_json()["matches"] == []
    # Add Lead page has the tools and the duplicate check
    form = s1.get("/leads/new").get_data(as_text=True)
    assert 'data-dup="/leads/dup-check"' in form and "From Contacts" in form
    # Share to CRM: the app is in the phone's Share menu; the share page opens Quick Add filled
    man = json.loads(s1.get("/orga/app.webmanifest").get_data(as_text=True))
    assert man["share_target"]["action"] == "/share-lead"
    assert "share_target" not in json.loads(s1.get("/orgb/app.webmanifest").get_data(as_text=True))
    page = s1.get("/share-lead?text=Rahul%0A9847099999").get_data(as_text=True)
    assert "RV_SHARED" in page and "9847099999" in page and "qa-dialog" in page
    # Super Admin switches Quick Add off again
    switch(w, w["org_a"], ["dup_alert"])
    assert s1.post("/leads/quick", {"project_id": w["proj_a"], "customer_name": "Z", "phone": "9847000004"}).status_code == 404
    assert "qa-fab" not in s1.get("/").get_data(as_text=True)
    assert s1.get("/share-lead").status_code == 404


def test_lead_form_link(world):
    w = world
    capture._hits.clear()
    admin = w["a_admin"]
    switch(w, w["org_a"], ["form_link"])
    r = admin.post("/lead-capture/forms/new", {"title": "Free Demo", "project_id": w["proj_a"], "fld_place": "1",
                                               "fld_product": "1", "assign_mode": "rotate", "active": "1",
                                               "thank_text": "We will call you today."})
    assert "Form saved" in r.text and "/lf/orga/free-demo?s=facebook" in r.text
    # the same link name again is refused; another company cannot open or edit it
    r = admin.post("/lead-capture/forms/new", {"title": "Free Demo", "project_id": w["proj_a"], "assign_mode": "admin"})
    assert "is used by another form" in r.text
    fid = w["ids"]("SELECT id FROM lead_forms")[0]
    assert w["b_admin"].get(f"/lead-capture/forms/{fid}").status_code == 404
    assert w["a_s1"].get(f"/lead-capture/forms/{fid}").status_code == 403
    qr = admin.get(f"/lead-capture/forms/{fid}/qr.svg?s=poster")
    assert qr.status_code == 200 and b"<svg" in qr.data
    # a customer opens the Facebook link and sends it (no login, no form token)
    v = Browser()
    html = v.get("/lf/orga/free-demo?s=facebook").get_data(as_text=True)
    assert "Free Demo" in html and "A Product 1" in html and 'name="website"' in html
    assert v.get("/lf/orgb/free-demo").status_code == 404
    r = v.c.post("/lf/orga/free-demo", data={"s": "facebook", "name": "Meera", "phone": "9847055555",
                                            "place": "Thrissur", "product_id": w["prod_a2"]}, follow_redirects=True)
    assert "We will call you today." in r.get_data(as_text=True)
    lead = one(w, "SELECT id, org_id, project_id, source, place, assigned_to, entered_by FROM leads WHERE customer_name = 'Meera'")
    staff_ids = sorted([w["uid"]("a_staff1"), w["uid"]("a_staff2")])
    assert lead[1:5] == (w["org_a"], w["proj_a"], "Facebook", "Thrissur")
    assert lead[5] == staff_ids[0] and lead[6] == w["uid"]("a_admin")
    assert one(w, "SELECT COUNT(*) FROM lead_products WHERE lead_id = ? AND product_id = ?", (lead[0], w["prod_a2"]))[0] == 1
    # the same phone again: no second lead, a note on the first
    v.c.post("/lf/orga/free-demo", data={"s": "instagram", "name": "Meera K", "phone": "+91 98470 55555"})
    assert one(w, "SELECT COUNT(*) FROM leads WHERE phone_norm = '9847055555'")[0] == 1
    hist = [h for (h,) in w["db"]().execute("SELECT action FROM lead_activity WHERE lead_id = ?", (lead[0],))]
    assert any(h.startswith("Enquired again via the form") for h in hist)
    # next new customer goes to the next staff member
    v.c.post("/lf/orga/free-demo", data={"name": "Ravi", "phone": "9847066666"})
    assert one(w, "SELECT assigned_to FROM leads WHERE customer_name = 'Ravi'")[0] == staff_ids[1]
    # bad phone, honeypot
    r = v.c.post("/lf/orga/free-demo", data={"name": "Bad", "phone": "123"})
    assert "valid phone" in r.get_data(as_text=True)
    v.c.post("/lf/orga/free-demo", data={"name": "Bot", "phone": "9847077777", "website": "spam"})
    assert one(w, "SELECT COUNT(*) FROM leads WHERE customer_name IN ('Bad', 'Bot')")[0] == 0
    # counts per source on the admin page
    st = dict(((s, (vw, sb)) for s, vw, sb in w["db"]().execute("SELECT src, views, submits FROM lead_form_stats")))
    assert st["facebook"] == (1, 1) and st["instagram"][1] == 1
    # form switched off, or the way switched off: the link stops
    admin.post(f"/lead-capture/forms/{fid}", {"title": "Free Demo", "slug": "free-demo", "project_id": w["proj_a"],
                                              "assign_mode": "admin"})
    assert v.get("/lf/orga/free-demo").status_code == 404
    admin.post(f"/lead-capture/forms/{fid}", {"title": "Free Demo", "slug": "free-demo", "project_id": w["proj_a"],
                                              "assign_mode": "admin", "active": "1"})
    assert v.get("/lf/orga/free-demo").status_code == 200
    switch(w, w["org_a"], [])
    assert v.get("/lf/orga/free-demo").status_code == 404
    # rate limit
    switch(w, w["org_a"], ["form_link"])
    capture._hits.clear()
    codes = [v.c.post("/lf/orga/free-demo", data={"name": f"N{i}", "phone": f"98471{i:05d}"}).status_code
             for i in range(10)]
    assert codes[-1] == 429


def test_catalogue_enquiry_and_google_forms(world):
    w = world
    capture._hits.clear()
    admin = w["a_admin"]
    admin.post(f"/products/{w['prod_a1']}/edit", {"name": "A Product 1", "code": "AP1", "active": "1",
                                                 "show_in_catalog": "1"})
    admin.post("/organization", {"name": "Org A", "catalog_on": "1", "phone": "9847011111"})
    v = Browser()
    assert "Enquire about this product" not in v.get(f"/c/orga/p/{w['prod_a1']}").get_data(as_text=True)
    switch(w, w["org_a"], ["catalog_enquiry", "gforms"])
    assert "Enquire about this product" in v.get(f"/c/orga/p/{w['prod_a1']}").get_data(as_text=True)
    r = v.c.post("/c/orga/enquire", data={"pid": w["prod_a1"], "name": "Sona", "phone": "9847088888",
                                          "message": "Need 2"}, follow_redirects=True)
    assert "We have your enquiry" in r.get_data(as_text=True)
    lead = one(w, "SELECT l.id, p.code, l.notes, l.source FROM leads l JOIN projects p ON p.id = l.project_id "
                  "WHERE customer_name = 'Sona'")
    assert lead[1:] == ("CAT", "Need 2", "Website")
    assert v.c.post("/c/orga/enquire", data={"pid": w["prod_b1"], "name": "X", "phone": "9847088889"}).status_code == 404
    # Google Forms Connect
    token = one(w, "SELECT capture_token FROM organizations WHERE id = ?", (w["org_a"],))[0]
    assert token not in admin.get("/").get_data(as_text=True)
    assert token in admin.get("/lead-capture").get_data(as_text=True)
    body = {"form": "Expo stall", "answers": {"Your Name": "Gopi", "Mobile number": "9847099990",
                                              "City": "Kannur", "Budget": "2 lakh"}}
    r = v.c.post(f"/hook/gform/{token}", data=json.dumps(body), content_type="application/json")
    assert r.get_json() == {"ok": True, "created": True}
    lead = one(w, "SELECT l.place, l.notes, p.code FROM leads l JOIN projects p ON p.id = l.project_id "
                  "WHERE customer_name = 'Gopi'")
    assert lead == ("Kannur", "Budget: 2 lakh", "GFORM")
    assert v.c.post(f"/hook/gform/{token}", data=json.dumps(body),
                    content_type="application/json").get_json()["created"] is False
    assert v.c.post(f"/hook/gform/{'0' * 32}", data=json.dumps(body), content_type="application/json").status_code == 404
    assert v.c.post(f"/hook/gform/{token}", data=json.dumps({"answers": {"Name": "No phone"}}),
                    content_type="application/json").status_code == 400
    # a new address stops the old one
    admin.post("/lead-capture", {"act": "gform_token"})
    assert v.c.post(f"/hook/gform/{token}", data=json.dumps(body), content_type="application/json").status_code == 404


def test_request_lead_capture(world):
    w = world
    admin = w["a_admin"]
    page = admin.get("/whats-new").get_data(as_text=True)
    assert "📥 New: Lead Capture" in page and "Request 📥 Lead Capture" in page
    admin.post("/whats-new/request/capture")
    assert "🙋 requested" in w["sup"].get("/admin/lead-capture").get_data(as_text=True)
    rid = w["ids"]("SELECT id FROM feature_requests WHERE feature = 'capture'")[0]
    r = w["sup"].post(f"/admin/requests/{rid}/on", follow=False)
    assert "/admin/lead-capture" in r.headers["Location"]
    switch(w, w["org_a"], ["quick_add"])
    assert one(w, "SELECT status FROM feature_requests WHERE id = ?", (rid,))[0] == "done"
    assert "✅ On for your company" in admin.get("/whats-new").get_data(as_text=True)
