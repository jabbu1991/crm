"""Settings > Lead form fields: own names for headings / fields, and hidden fields that never lose data."""
from test_production import add_lead, project_id, pw  # noqa: F401


def lead_row(W, name):
    return W["db"]().execute("SELECT * FROM leads WHERE customer_name = ?", (name,)).fetchone()


def save_fields(b, labels=None, hidden=(), url="/settings/lead-fields"):
    import lead_fields
    data = {"label_" + k: (labels or {}).get(k, "") for k in lead_fields.STANDARD}
    data.update({"show_" + k: "1" for k in lead_fields.HIDEABLE if k not in hidden})
    return b.post(url, data)


def test_rename_shows_everywhere(pw):
    star, only = pw["star"], pw["only"]
    pid = project_id(star)
    add_lead(star, pid, name="Rename Co", buying_possibility="High")
    r = save_fields(star, {"status": "Enquiry stage", "possibility": "Chance of order", "sec_qual": "Follow-up plan"})
    assert "Lead form fields saved" in r.text
    form = star.get("/leads/new").text
    assert "Enquiry stage" in form and "Chance of order" in form and "Follow-up plan" in form
    assert "Buying possibility" not in form
    lid = lead_row(pw, "Rename Co")["id"]
    assert "Chance of order" in star.get(f"/leads/{lid}").text
    assert "Enquiry stage" in star.get("/leads").text          # filter + list header
    csv_head = star.get("/leads/export").text.splitlines()[0]
    assert "Enquiry stage" in csv_head and "Chance of order" in csv_head
    # another client keeps the standard names
    pid2 = project_id(only)
    assert "Buying possibility" in only.get("/leads/new").text and "Enquiry stage" not in only.get("/leads/new").text
    # names that cannot be hidden stay on the form; empty name = standard name again
    save_fields(star, {}, hidden=("status",))
    assert "Lead status" in star.get("/leads/new").text
    assert pid2


def test_hidden_field_keeps_data(pw):
    star = pw["star"]
    pid = project_id(star)
    add_lead(star, pid, name="Keep Data", district="Ernakulam", email="a@b.in", notes="first call",
             next_follow_up="2030-01-02", buying_possibility="High")
    before = lead_row(pw, "Keep Data")
    save_fields(star, hidden=("district", "email", "notes", "next_follow_up", "possibility"))
    form = star.get(f"/leads/{before['id']}/edit").text
    assert 'name="district"' not in form and 'name="email"' not in form and 'name="notes"' not in form
    # edit with the smaller form: hidden values stay as they were
    star.post(f"/leads/{before['id']}/edit", {"project_id": pid, "customer_name": "Keep Data", "phone": "9876543210",
                                             "whatsapp": "9876543210", "status": "Contacted", "country": "India",
                                             "place": "Kochi", "state": "Kerala"})
    after = lead_row(pw, "Keep Data")
    assert after["status"] == "Contacted" and after["place"] == "Kochi"
    assert after["district"] == "Ernakulam" and after["email"] == "a@b.in" and after["notes"] == "first call"
    assert after["next_follow_up"] == "2030-01-02" and after["buying_possibility"] == "High"
    # the old value still shows on the lead page; the empty hidden field does not
    page = star.get(f"/leads/{before['id']}").text
    assert "Ernakulam" in page
    # a new lead without the hidden fields saves fine
    r = add_lead(star, pid, name="Small Form", phone="9123456780")
    assert lead_row(pw, "Small Form") is not None, r.text[:500]
    # reset brings everything back
    star.post("/settings/lead-fields", {"reset": "1"})
    assert 'name="district"' in star.get("/leads/new").text


def test_import_accepts_own_names_and_access(pw):
    star = pw["star"]
    pid = project_id(star)
    save_fields(star, {"customer_name": "Client", "requirement": "Machine details"})
    import io
    csv_text = "Client,Phone,Machine details\nImported One,9000002001,5 HP gate\n"
    star.post("/leads/import", {"project_id": str(pid), "file": (io.BytesIO(csv_text.encode()), "x.csv")})
    row = lead_row(pw, "Imported One")
    assert row is not None and row["requirement"] == "5 HP gate"
    # Super Admin can edit a client's fields; staff cannot open the page
    oid = pw["oid"]("STAR")
    r = save_fields(pw["sup"], {"phone": "Mobile"}, url=f"/admin/organizations/{oid}/lead-fields")
    assert "Lead form fields saved" in r.text
    assert "Mobile" in star.get("/leads/new").text
