"""Instagram stories for the website: Super Admin only, token connect, new stories saved once (photo + video),
kept after they leave Instagram, newest first in the public JSON, hide / title / delete, daily timing.
Instagram is faked here.

Run:  python -m pytest -q tests/test_instagram.py
"""
import io
import json
import os
import urllib.parse
from datetime import datetime, timedelta

import pytest
from PIL import Image

from test_crm import PW, Browser, crm, crm_db  # noqa: F401

import instagram_stories as ig


def jpeg(color="red"):
    out = io.BytesIO()
    Image.new("RGB", (1080, 1920), color).save(out, "JPEG")
    return out.getvalue()


class FakeInstagram:
    def __init__(self):
        self.live = []
        self.calls = []
        self.files = {"https://cdn/p1.jpg": jpeg(), "https://cdn/p2.jpg": jpeg("blue"),
                      "https://cdn/v1.mp4": b"\x00\x00\x00\x18ftypmp42video", "https://cdn/v1.jpg": jpeg("green")}

    def __call__(self, url, max_bytes=None):
        self.calls.append(url)
        if url in self.files:
            return 200, self.files[url]
        if url.startswith(ig.GRAPH_FB + "/"):
            path, q = url[len(ig.GRAPH_FB) + 1:].split("?", 1)
            q = dict(urllib.parse.parse_qsl(q))
            if not q["access_token"].startswith("EAA"):
                return 400, json.dumps({"error": {"message": "Invalid OAuth access token", "code": 190}}).encode()
            if path == "me/accounts":
                return 200, json.dumps({"data": [{"id": "p1"}, {"id": "p2", "instagram_business_account": {
                    "id": "1789", "username": "realview_creative_studio"}}]}).encode()
            if path == "1789/stories":
                return 200, json.dumps({"data": self.live}).encode()
            if path == "1789":
                return 200, json.dumps({"id": "1789", "username": "realview_creative_studio"}).encode()
            raise AssertionError(f"unexpected call {url}")
        path, q = url[len(ig.GRAPH) + 1:].split("?", 1)
        q = dict(urllib.parse.parse_qsl(q))
        if q.get("access_token", "").startswith(("BAD", "EAA")):
            return 400, json.dumps({"error": {"message": "Invalid OAuth access token", "code": 190}}).encode()
        if path == "me":
            return 200, json.dumps({"user_id": "1784", "username": "realview_creative_studio"}).encode()
        if path == "refresh_access_token":
            return 200, json.dumps({"access_token": "IGAA-renewed-token-xxxxxxxxxxxxxxxx", "expires_in": 5184000}).encode()
        if path == "1784/stories":
            return 200, json.dumps({"data": self.live}).encode()
        raise AssertionError(f"unexpected call {url}")


@pytest.fixture()
def iw(tmp_path, monkeypatch):
    db_path = str(tmp_path / "crm.db")
    monkeypatch.setattr(crm, "DB_PATH", db_path)
    crm._failed_logins.clear()
    crm_db.migrate(db_path)
    fake = FakeInstagram()
    monkeypatch.setattr(ig, "http", fake)
    sup = Browser()
    sup.post("/setup", {"agency": "Real View", "name": "Owner", "username": "super", "password": PW})
    r = sup.post("/admin/organizations/new", {"name": "Client", "code": "CLI", "admin_name": "C Admin",
                                              "admin_username": "c_admin", "onboarding": "password",
                                              "password": "TempPass123"})
    assert "created" in r.text
    return {"sup": sup, "fake": fake, "db": db_path, "dir": tmp_path}


def story(i, kind="IMAGE", ts="2026-11-05T08:00:00+0000"):
    s = {"id": str(i), "media_type": kind, "timestamp": ts, "permalink": f"https://instagram.com/s/{i}"}
    if kind == "VIDEO":
        s.update(media_url="https://cdn/v1.mp4", thumbnail_url="https://cdn/v1.jpg")
    else:
        s["media_url"] = f"https://cdn/p{i}.jpg"
    return s


def test_only_super_admin_and_public_json(iw):
    c = Browser()
    c.login("c_admin", "TempPass123")
    c.post("/account", {"current_password": "TempPass123", "new_password": PW, "confirm": PW})
    assert c.get("/admin/instagram").status_code == 403
    assert c.post("/admin/instagram/check").status_code == 403
    r = Browser().get("/ig/stories.json")             # public, no login
    assert r.status_code == 200 and r.json["stories"] == []
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    assert Browser().get("/ig/media/..%2Fcrm.db").status_code == 404
    assert "📸 Instagram" in iw["sup"].get("/admin/instagram").text


def test_bad_token_is_refused(iw):
    r = iw["sup"].post("/admin/instagram/token", {"token": "short"})
    assert "Paste the whole Instagram token" in r.text
    r = iw["sup"].post("/admin/instagram/token", {"token": "BAD" + "x" * 40})
    assert "Instagram did not accept this token" in r.text
    assert not os.path.exists(os.path.join(iw["dir"], "instagram.json"))


def test_connect_saves_new_stories_once_and_keeps_them(iw):
    sup, fake = iw["sup"], iw["fake"]
    fake.live = [story(1, ts="2026-11-05T08:00:00+0000"), story(3, "VIDEO", ts="2026-11-05T09:00:00+0000")]
    r = sup.post("/admin/instagram/token", {"token": "IGAA" + "t" * 40})
    assert "Instagram connected (@realview_creative_studio)" in r.text
    assert "2 new stories saved" in r.text, r.text[:3000]
    st = json.load(open(os.path.join(iw["dir"], "instagram.json")))
    assert st["user_id"] == "1784" and st["token"].startswith("IGAA")
    files = sorted(os.listdir(os.path.join(iw["dir"], "instagram")))
    assert len(files) == 4                                # photo + thumb, video + poster
    photo = [f for f in files if f.endswith(".jpg") and not f.endswith("_t.jpg")][0]
    assert os.path.getsize(os.path.join(iw["dir"], "instagram", photo)) <= 260 * 1024
    # newest first
    data = Browser().get("/ig/stories.json").json
    assert [s["type"] for s in data["stories"]] == ["video", "image"]
    assert data["profile"] == "https://www.instagram.com/realview_creative_studio/"
    m = Browser().get(data["stories"][0]["src"].replace("http://localhost", ""))
    assert m.status_code == 200 and m.mimetype == "video/mp4" and "immutable" in m.headers["Cache-Control"]
    # same stories again: nothing downloaded twice
    n = len(fake.calls)
    r = sup.post("/admin/instagram/check")
    assert "0 new stories saved (2 live on Instagram now)" in r.text
    assert not any(u.startswith("https://cdn/") for u in fake.calls[n:])
    # the stories leave Instagram after 24 h, a new one comes: old ones stay
    fake.live = [story(2, ts="2026-11-06T08:00:00+0000")]
    r = sup.post("/admin/instagram/check")
    assert "1 new story saved" in r.text
    assert len(Browser().get("/ig/stories.json").json["stories"]) == 3


def test_hide_title_delete(iw):
    sup, fake = iw["sup"], iw["fake"]
    fake.live = [story(1), story(2, ts="2026-11-05T10:00:00+0000")]
    sup.post("/admin/instagram/token", {"token": "IGAA" + "t" * 40})
    rows = sqlite_rows(iw["db"])
    newest, older = rows[0], rows[1]
    sup.post(f"/admin/instagram/story/{newest}", {"action": "title", "title": "Offer"})
    assert Browser().get("/ig/stories.json").json["stories"][0]["title"] == "Offer"
    sup.post(f"/admin/instagram/story/{newest}", {"action": "hide"})
    assert [s["id"] for s in Browser().get("/ig/stories.json").json["stories"]] == [older]
    sup.post(f"/admin/instagram/story/{newest}", {"action": "show"})
    r = sup.post(f"/admin/instagram/story/{older}", {"action": "delete"})
    assert "deleted" in r.text
    assert [s["id"] for s in Browser().get("/ig/stories.json").json["stories"]] == [newest]
    # still live on Instagram, but a deleted story is never saved again
    r = sup.post("/admin/instagram/check")
    assert "0 new stories saved" in r.text


def sqlite_rows(db_path):
    import sqlite3
    with sqlite3.connect(db_path) as c:
        return [r[0] for r in c.execute("SELECT id FROM ig_stories ORDER BY posted_at DESC")]


def test_expired_token_shown_and_daily_timing(iw):
    sup, fake = iw["sup"], iw["fake"]
    sup.post("/admin/instagram/token", {"token": "IGAA" + "t" * 40})
    with crm.app.app_context():
        assert not ig.due()                                # just checked
        assert ig.due(datetime.now() + timedelta(hours=23))
    st = ig_state(iw)
    st["token"] = "BAD" + "x" * 40
    json.dump(st, open(os.path.join(iw["dir"], "instagram.json"), "w"))
    r = sup.post("/admin/instagram/check")
    assert "token expired" in r.text


def ig_state(iw):
    return json.load(open(os.path.join(iw["dir"], "instagram.json")))


def test_token_renews_itself_after_a_week(iw):
    sup = iw["sup"]
    sup.post("/admin/instagram/token", {"token": "IGAA" + "t" * 40})
    st = ig_state(iw)
    st["token_saved_at"] = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    json.dump(st, open(os.path.join(iw["dir"], "instagram.json"), "w"))
    r = sup.post("/admin/instagram/check")
    assert "Checked Instagram" in r.text
    assert ig_state(iw)["token"] == "IGAA-renewed-token-xxxxxxxxxxxxxxxx"


def test_unified_login_system_user_token(iw):
    """Unified / Facebook Login token (EAA..., never expires): found through graph.facebook.com, never refreshed."""
    sup, fake = iw["sup"], iw["fake"]
    fake.live = [story(1)]
    r = sup.post("/admin/instagram/token", {"token": "EAA" + "u" * 40})
    assert "Instagram connected (@realview_creative_studio)" in r.text and "1 new story saved" in r.text, r.text[:2000]
    st = ig_state(iw)
    assert st["api"] == "fb" and st["user_id"] == "1789"
    st["token_saved_at"] = "2020-01-01 00:00:00"
    json.dump(st, open(os.path.join(iw["dir"], "instagram.json"), "w"))
    r = sup.post("/admin/instagram/check")
    assert "Checked Instagram" in r.text
    assert not any("refresh_access_token" in u for u in fake.calls)
    # the account id typed by hand also works
    r = sup.post("/admin/instagram/token", {"token": "EAA" + "v" * 40, "ig_id": "1789"})
    assert "Instagram connected" in r.text
