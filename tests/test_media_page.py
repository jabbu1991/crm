"""Realview Media Production page: Super Admin and editor logins (editors see only this page), works with
YouTube / Instagram links and photos, page settings, the public JSON and the booking enquiry -> agency lead.

Run:  python -m pytest -q tests/test_media_page.py
"""
import io
import sqlite3

import pytest
from PIL import Image

from test_crm import PW, Browser, crm, crm_db  # noqa: F401

import media_page as mp

SITE = "https://www.realviewcreativestudio.in"


def jpeg(w=1200, h=800, color="red"):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "JPEG")
    out.seek(0)
    return out


@pytest.fixture()
def mw(tmp_path, monkeypatch):
    db_path = str(tmp_path / "crm.db")
    monkeypatch.setattr(crm, "DB_PATH", db_path)
    monkeypatch.setattr(crm, "ENQUIRY_ORIGINS", {SITE})
    crm._failed_logins.clear()
    mp._fails.clear()
    mp._hits.clear()
    crm_db.migrate(db_path)
    sup = Browser()
    sup.post("/setup", {"agency": "Real View", "name": "Owner", "username": "super", "password": PW})
    return {"sup": sup, "db": db_path}


def q(db, sql, args=()):
    with sqlite3.connect(db) as c:
        return c.execute(sql, args).fetchall()


def test_parse_link():
    assert mp.parse_link("https://youtu.be/dQw4w9WgXcQ?si=x") == ("youtube", "dQw4w9WgXcQ")
    assert mp.parse_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3") == ("youtube", "dQw4w9WgXcQ")
    assert mp.parse_link("youtube.com/shorts/dQw4w9WgXcQ") == ("youtube", "dQw4w9WgXcQ")
    assert mp.parse_link("https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=abc") == ("instagram", "reel/C1a2B3c4D5e")
    assert mp.parse_link("https://instagram.com/p/C1a2B3c4D5e/") == ("instagram", "p/C1a2B3c4D5e")
    assert mp.parse_link("https://example.com/x") == ("link", "")
    assert mp.parse_link("") == (None, "")


def test_login_needed_and_super_admin_opens_it(mw):
    r = Browser().get("/media")
    assert r.status_code == 302 and "/media/login" in r.headers["Location"]
    r = mw["sup"].get("/media")
    assert r.status_code == 200 and "Recent work" in r.text and "Editors" in r.text
    assert "🎬 Media Page" in mw["sup"].get("/").text


def test_editor_sees_only_the_media_page(mw):
    sup = mw["sup"]
    r = sup.post("/media/editors", {"action": "add", "name": "Azeez Kodinhi", "username": "Azeez", "password": "photo1234"})
    assert "Editor added" in r.text
    ed = Browser()
    r = ed.post("/media/login", {"username": "azeez", "password": "wrong-pass"})
    assert "Wrong username or password" in r.text
    r = ed.post("/media/login", {"username": "azeez", "password": "photo1234"})
    assert "Recent work" in r.text and "Editors" not in r.text.split("<main>")[0]
    assert ed.get("/media/editors").status_code == 403
    r = ed.get("/")                               # not a CRM user
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert ed.get("/leads").status_code == 302
    # disable -> logged out at once
    eid = q(mw["db"], "SELECT id FROM media_editors")[0][0]
    sup.post("/media/editors", {"action": "toggle", "id": eid})
    assert ed.get("/media").status_code == 302
    r = ed.post("/media/login", {"username": "azeez", "password": "photo1234"})
    assert "disabled" in r.text


def test_add_work_with_photos_and_youtube_and_public_json(mw):
    sup = mw["sup"]
    r = sup.post("/media/work/new", {"title": "Wedding at Tirur", "category": "Wedding", "event_date": "2026-11-01",
                                      "place": "Tirur", "link_url": "https://youtu.be/dQw4w9WgXcQ", "featured": "on",
                                      "photos": [(jpeg(), "a.jpg"), (jpeg(800, 1200, "blue"), "b.jpg"),
                                                 (io.BytesIO(b"not an image"), "x.txt")]},
                 content_type="multipart/form-data")
    assert "Work added to the website. 2 photo(s) added." in r.text, r.text[:3000]
    assert "1 file(s) were not photos" in r.text
    sup.post("/media/work/new", {"title": "Live Show Kozhikode", "category": "Live Show", "event_date": "2026-10-01",
                                  "link_url": "https://www.instagram.com/reel/C1a2B3c4D5e/"})
    d = Browser().get("/mp/data.json")
    assert d.status_code == 200 and d.headers["Access-Control-Allow-Origin"] == "*"
    works = d.json["works"]
    assert [w["title"] for w in works] == ["Wedding at Tirur", "Live Show Kozhikode"]   # newest first
    w0 = works[0]
    assert w0["kind"] == "youtube" and w0["lid"] == "dQw4w9WgXcQ" and w0["featured"] and len(w0["photos"]) == 2
    assert w0["cover"]["src"].endswith(".jpg") and "/media/" in w0["cover"]["src"]
    assert works[1]["kind"] == "instagram" and works[1]["lid"] == "reel/C1a2B3c4D5e" and works[1]["cover"] is None
    assert d.json["settings"]["name"] == "Azeez Kodinhi" and d.json["settings"]["whatsapp"] == "+91 70255 04627"
    assert d.json["settings"]["categories"] == ["Wedding", "Live Show"]
    assert Browser().get(w0["photos"][0]["src"].replace("http://localhost", "")).status_code == 200
    # hide, then delete with its photos
    wid = q(mw["db"], "SELECT id FROM media_works WHERE title = 'Wedding at Tirur'")[0][0]
    sup.post(f"/media/work/{wid}/quick", {"action": "hide"})
    assert [w["title"] for w in Browser().get("/mp/data.json").json["works"]] == ["Live Show Kozhikode"]
    sup.post(f"/media/work/{wid}/quick", {"action": "delete"})
    assert q(mw["db"], "SELECT COUNT(*) FROM media_photos")[0][0] == 0


def test_edit_cover_and_delete_photo(mw):
    sup = mw["sup"]
    sup.post("/media/work/new", {"title": "Reception", "photos": [(jpeg(), "a.jpg"), (jpeg(color="green"), "b.jpg")]},
             content_type="multipart/form-data")
    wid = q(mw["db"], "SELECT id FROM media_works")[0][0]
    p1, p2 = [r[0] for r in q(mw["db"], "SELECT id FROM media_photos ORDER BY id")]
    assert "Reception" in sup.get(f"/media/work/{wid}").text
    sup.post(f"/media/work/{wid}", {"title": "Reception", "cover": str(p2), "delete_photo": str(p1)},
             content_type="multipart/form-data")
    assert q(mw["db"], "SELECT cover FROM media_works")[0][0] == str(p2)
    assert [r[0] for r in q(mw["db"], "SELECT id FROM media_photos")] == [p2]


def test_settings_saved_and_shown(mw):
    sup = mw["sup"]
    r = sup.post("/media/settings", {"name": "Azeez K", "whatsapp": "+91 99999 88888", "instagram": "instagram.com/x",
                                      "faq": "Do you travel? | Yes, across Kerala.\nbad line without answer",
                                      "packages": "Classic | ₹40,000 | Photos, Film", "packages_on": "on",
                                      "showreel": "https://youtu.be/dQw4w9WgXcQ",
                                      "photo_file": (jpeg(900, 1200), "me.jpg")}, content_type="multipart/form-data")
    assert "Page settings saved" in r.text
    st = Browser().get("/mp/data.json").json["settings"]
    assert st["name"] == "Azeez K" and st["whatsapp"] == "+91 99999 88888"
    assert st["instagram"] == "https://instagram.com/x" and st["showreel"] == "dQw4w9WgXcQ"
    assert st["faq"] == [{"q": "Do you travel?", "a": "Yes, across Kerala."}]
    assert st["packages"] == [{"name": "Classic", "price": "₹40,000", "items": ["Photos", "Film"]}]
    assert st["photo"].endswith(".jpg")


def test_booking_enquiry_becomes_agency_lead(mw):
    c = crm.app.test_client()
    r = c.post("/mp/enquiry", data={"name": "Fathima", "phone": "+91 98470 12345", "event": "Wedding",
                                    "date": "12 December 2026", "place": "Tirur", "message": "Need drone too"},
               headers={"Origin": SITE})
    assert r.status_code == 201, r.data
    lead = q(mw["db"], "SELECT customer_name, requirement, notes, place FROM leads")[0]
    assert lead[0] == "Fathima" and "Wedding shoot" in lead[1] and "12 December 2026" in lead[1] and lead[3] == "Tirur"
    assert q(mw["db"], "SELECT name FROM projects WHERE code = 'MEDIA'")[0][0] == "Media Production Enquiries"
    assert c.post("/mp/enquiry", data={"name": "X", "phone": "123"}, headers={"Origin": SITE}).status_code == 400
    assert c.post("/mp/enquiry", data={"name": "X", "phone": "9847012345"},
                  headers={"Origin": "https://evil.example"}).status_code == 403


def test_paste_link_and_move_to_top(mw, monkeypatch):
    sup = mw["sup"]
    monkeypatch.setattr(mp, "youtube_title", lambda vid: "Ameen & Hiba – Wedding Film")
    r = sup.post("/media/link", {"url": "https://youtu.be/dQw4w9WgXcQ?si=abc", "category": "Wedding"})
    assert "Film added: Ameen &amp; Hiba – Wedding Film" in r.text
    r = sup.post("/media/link", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    assert "already on the page" in r.text
    r = sup.post("/media/link", {"url": "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=x", "category": "Live Show"})
    assert "Instagram post added: Instagram reel" in r.text
    assert "Paste a YouTube" in sup.post("/media/link", {"url": "https://example.com/x"}).text
    works = Browser().get("/mp/data.json").json["works"]
    assert [(w["kind"], w["lid"]) for w in works] == [("instagram", "reel/C1a2B3c4D5e"), ("youtube", "dQw4w9WgXcQ")]
    assert works[1]["cover"]["src"].startswith("https://i.ytimg.com/vi/dQw4w9WgXcQ/")
    # an older work moved to the top comes first
    sup.post("/media/work/new", {"title": "Old album", "event_date": "2020-01-01"})
    wid = q(mw["db"], "SELECT id FROM media_works WHERE title = 'Old album'")[0][0]
    assert Browser().get("/mp/data.json").json["works"][-1]["title"] == "Old album"
    sup.post(f"/media/work/{wid}/quick", {"action": "top"})
    first = Browser().get("/mp/data.json").json["works"][0]
    assert first["title"] == "Old album" and first["pinned"]
    sup.post(f"/media/work/{wid}/quick", {"action": "untop"})
    assert Browser().get("/mp/data.json").json["works"][-1]["title"] == "Old album"
