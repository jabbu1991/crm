"""Daily Google Drive backup: Super Admin only, connect (OAuth), backup uploads the database + only new photos,
removes copies older than 30 days, nightly timing, failures shown, direct download. Google is faked here.

Run:  python -m pytest -q tests/test_backup.py
"""
import json
import os
import re
import sqlite3
import urllib.parse
from datetime import datetime, timedelta

import pytest

from test_crm import PW, Browser, crm, crm_db  # noqa: F401

import backup_drive


class FakeDrive:
    """A tiny in-memory Google Drive + token endpoint."""

    def __init__(self):
        self.files = {}   # id -> {name, parents, mime, size}
        self.n = 0
        self.pending = {}
        self.fail = False

    def new_id(self):
        self.n += 1
        return f"f{self.n}"

    def __call__(self, method, url, data=None, headers=None):
        if self.fail:
            raise backup_drive.DriveError("Network problem: offline")
        if url == backup_drive.TOKEN_URL:
            form = dict(urllib.parse.parse_qsl(data.decode()))
            out = {"access_token": "AT"}
            if form.get("grant_type") == "authorization_code":
                out["refresh_token"] = "RT"
            return 200, {}, json.dumps(out).encode()
        assert (headers or {}).get("Authorization") == "Bearer AT"
        if url.startswith(backup_drive.API + "/about"):
            return 200, {}, json.dumps({"user": {"emailAddress": "owner@example.com"}}).encode()
        if url.startswith(backup_drive.API + "/files?") and method == "GET":
            q = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))["q"]
            name = re.search(r"name = '([^']+)'", q)
            parent = re.search(r"'([^']+)' in parents", q)
            hits = [{"id": i, "name": f["name"]} for i, f in self.files.items()
                    if (not name or f["name"] == name.group(1)) and (not parent or parent.group(1) in f["parents"])]
            return 200, {}, json.dumps({"files": hits}).encode()
        if url.startswith(backup_drive.API + "/files?fields=id") and method == "POST":
            meta = json.loads(data)
            i = self.new_id()
            self.files[i] = {"name": meta["name"], "parents": meta.get("parents", []), "mime": meta["mimeType"]}
            return 200, {}, json.dumps({"id": i}).encode()
        if url.startswith(backup_drive.UPLOAD) and method == "POST":
            meta = json.loads(data)
            key = f"https://upload.example/{len(self.pending)}"
            self.pending[key] = meta
            return 200, {"Location": key}, b"{}"
        if url in self.pending and method == "PUT":
            meta = self.pending.pop(url)
            i = self.new_id()
            self.files[i] = {"name": meta["name"], "parents": meta["parents"], "size": len(data)}
            return 200, {}, json.dumps({"id": i}).encode()
        if method == "DELETE":
            self.files.pop(url.rsplit("/", 1)[1], None)
            return 204, {}, b""
        raise AssertionError(f"unexpected call {method} {url}")

    def names(self, folder):
        fid = next(i for i, f in self.files.items() if f["name"] == folder)
        return sorted(f["name"] for f in self.files.values() if fid in f["parents"])


@pytest.fixture()
def bw(tmp_path, monkeypatch):
    db_path = str(tmp_path / "crm.db")
    monkeypatch.setattr(crm, "DB_PATH", db_path)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    crm._failed_logins.clear()
    crm_db.migrate(db_path)
    fake = FakeDrive()
    monkeypatch.setattr(backup_drive, "http", fake)
    sup = Browser()
    sup.post("/setup", {"agency": "Real View", "name": "Owner", "username": "super", "password": PW})
    r = sup.post("/admin/organizations/new", {"name": "Client", "code": "CLI", "admin_name": "C Admin",
                                              "admin_username": "c_admin", "onboarding": "password",
                                              "password": "TempPass123"})
    assert "created" in r.text
    return {"sup": sup, "fake": fake, "db": db_path}


def connect(sup):
    r = sup.post("/admin/backups/connect", follow=False)
    assert r.status_code == 302 and "accounts.google.com" in r.headers["Location"]
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["Location"]).query))["state"]
    assert "drive.file" in r.headers["Location"]
    return sup.get(f"/admin/backups/google/callback?code=abc&state={state}", follow_redirects=True)


def test_only_super_admin(bw):
    c = Browser()
    c.login("c_admin", "TempPass123")
    c.post("/account", {"current_password": "TempPass123", "new_password": PW, "confirm": PW})
    assert c.get("/admin/backups").status_code == 403
    assert c.get("/admin/backups/download").status_code == 403
    assert "not connected" in bw["sup"].get("/").text      # red reminder for the Super Admin


def test_connect_backup_photos_once_and_retention(bw):
    sup, fake = bw["sup"], bw["fake"]
    r = connect(sup)
    assert "Google Drive connected (owner@example.com)" in r.text
    st = json.load(open(os.path.join(os.path.dirname(bw["db"]), "google_drive.json")))
    assert st["refresh_token"] == "RT"
    media = crm.media_dir()
    for n in ("a" * 32, "b" * 32):
        open(os.path.join(media, n + ".jpg"), "wb").write(b"\xff\xd8photo")
    r = sup.post("/admin/backups/run")
    assert "Backup done" in r.text, r.text[:3000]
    dbs = fake.names("Database")
    assert len(dbs) == 1 and dbs[0].startswith("crm-") and dbs[0].endswith(".db")
    assert fake.names("Photos") == ["a" * 32 + ".jpg", "b" * 32 + ".jpg"]
    up = next(f for f in fake.files.values() if f["name"] == dbs[0])
    assert up["size"] > 1000                               # a real SQLite file
    # second run: no photo uploaded again, only the new one
    open(os.path.join(media, "c" * 32 + ".jpg"), "wb").write(b"\xff\xd8new")
    r = sup.post("/admin/backups/run")
    assert "1 new photo(s)" in r.text
    assert len(fake.names("Photos")) == 3
    # copies older than 30 days are removed
    dbf = next(i for i, f in fake.files.items() if f["name"] == "Database")
    old = (backup_drive.ist_now() - timedelta(days=40)).strftime("%Y-%m-%d")
    fake.files["old1"] = {"name": f"crm-{old}.db", "parents": [dbf]}
    r = sup.post("/admin/backups/run")
    assert "1 old copy(ies) removed" in r.text and "old1" not in fake.files
    page = sup.get("/admin/backups").text
    assert "Connected" in page and "owner@example.com" in page and "By hand" in page


def test_nightly_timing_and_failure_alert(bw):
    sup, fake = bw["sup"], bw["fake"]
    with crm.app.test_request_context():
        assert backup_drive.due() is False                 # not connected
    connect(sup)
    day = datetime(2026, 10, 30, 1, 30)
    with crm.app.test_request_context():
        assert backup_drive.due(day) is False              # before 02:00
        assert backup_drive.due(day.replace(hour=2, minute=10)) is True
    fake.fail = True
    r = sup.post("/admin/backups/run")
    assert "Backup failed: Network problem" in r.text
    assert "Last backup failed" in sup.get("/").text
    fake.fail = False
    sup.post("/admin/backups/run")
    assert "Last backup failed" not in sup.get("/").text
    # the nightly one runs once a day
    with crm.app.app_context():
        res = backup_drive.run_backup("auto")
        assert res["ok"]
        today = backup_drive.ist_now()
        assert backup_drive.due(today.replace(hour=max(today.hour, 2))) is False


def test_download_gives_a_working_database(bw):
    r = bw["sup"].get("/admin/backups/download")
    assert r.status_code == 200 and r.data[:16] == b"SQLite format 3\x00"
    path = os.path.join(os.path.dirname(bw["db"]), "dl.db")
    open(path, "wb").write(r.data)
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 2
