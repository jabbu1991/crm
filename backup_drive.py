"""Daily automatic backup to the owner's Google Drive (Super Admin).

Every night at 02:00 India time (and on "Back up now") the CRM:
  1. makes a consistent copy of the whole SQLite database and uploads it as Database/crm-YYYY-MM-DD.db,
  2. uploads only the NEW photos (products, menu, banners, logos, expense bills) into Photos/ - a photo is
     uploaded once, never again,
  3. deletes database copies older than 30 days from the Drive folder.
No ZIP: the files are plain files in "Real View CRM Backups" on Drive.

Google access: the Super Admin presses "Connect Google Drive" once (OAuth, scope drive.file = the CRM can only see the
files it created itself, never the rest of the Drive). The refresh token is kept in a file next to the database
(not inside the database, so it never ends up in a backup). Needs the Render env vars GOOGLE_CLIENT_ID and
GOOGLE_CLIENT_SECRET (a free Google Cloud "OAuth client"). Uses only the Python standard library for HTTP.
"""
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Response, abort, flash, g, redirect, render_template, request, session, url_for

H = None
SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
ROOT_NAME = "Real View CRM Backups"
KEEP_DAYS = 30
RUN_HOUR = 2  # 02:00 India time
_lock = threading.Lock()
_loop = {"started": False}


def init(host):
    global H
    H = host
    app = host.app
    app.add_url_rule("/admin/backups", "admin_backups", host.super_required(page))
    app.add_url_rule("/admin/backups/connect", "admin_backups_connect", host.super_required(connect), methods=["POST"])
    app.add_url_rule("/admin/backups/google/callback", "admin_backups_callback", host.super_required(callback))
    app.add_url_rule("/admin/backups/disconnect", "admin_backups_disconnect", host.super_required(disconnect),
                     methods=["POST"])
    app.add_url_rule("/admin/backups/run", "admin_backups_run", host.super_required(run_now), methods=["POST"])
    app.add_url_rule("/admin/backups/download", "admin_backups_download", host.super_required(download))
    app.before_request(_ensure_loop)
    app.jinja_env.globals["backup_alert"] = alert
    app.jinja_env.filters["ist"] = to_ist


def to_ist(ts):
    """A server-time timestamp 'YYYY-MM-DD HH:MM:SS' shown in India time (the server may run in UTC)."""
    try:
        local = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ts or ""
    offset = time.localtime().tm_gmtoff
    return (local - timedelta(seconds=offset) + timedelta(hours=5, minutes=30)).strftime("%d-%m-%Y %H:%M")


# ---------------------------------------------------------------- storage of the Google connection (a file)

def data_dir():
    return os.path.dirname(os.path.abspath(H.DB_PATH))


def _state_path():
    return os.path.join(data_dir(), "google_drive.json")


def load_state():
    try:
        with open(_state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def keys():
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip(), os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def connected():
    return bool(load_state().get("refresh_token"))


# ---------------------------------------------------------------- HTTP (replaceable in tests)

class DriveError(Exception):
    pass


def http(method, url, data=None, headers=None):
    """Returns (status, headers dict, body bytes). Raises DriveError on a network problem."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, OSError) as e:
        raise DriveError(f"Network problem: {e}")


def _json(status, body, what):
    if status >= 300:
        try:
            msg = json.loads(body or b"{}").get("error", {})
            msg = msg.get("message") if isinstance(msg, dict) else msg
        except ValueError:
            msg = body[:200]
        raise DriveError(f"{what} failed ({status}): {msg}")
    return json.loads(body or b"{}")


class Drive:
    def __init__(self, st):
        self.st = st
        self.token = None

    def access_token(self):
        if self.token:
            return self.token
        cid, secret = keys()
        if not cid or not secret:
            raise DriveError("Google keys are not set on the server (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
        if not self.st.get("refresh_token"):
            raise DriveError("Google Drive is not connected.")
        data = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "refresh_token",
                                       "refresh_token": self.st["refresh_token"]}).encode()
        status, _, body = http("POST", TOKEN_URL, data, {"Content-Type": "application/x-www-form-urlencoded"})
        if status == 400 and b"invalid_grant" in (body or b""):
            raise DriveError("Google access was removed or expired. Press 'Connect Google Drive' again.")
        self.token = _json(status, body, "Google sign-in")["access_token"]
        return self.token

    def call(self, method, url, data=None, headers=None, what="Drive"):
        h = {"Authorization": f"Bearer {self.access_token()}"}
        h.update(headers or {})
        status, hdrs, body = http(method, url, data, h)
        if status == 204:
            return {}, hdrs
        return _json(status, body, what), hdrs

    def folder(self, name, parent=None):
        """Find (or make) a folder this app created."""
        q = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        if parent:
            q += f" and '{parent}' in parents"
        res, _ = self.call("GET", f"{API}/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id,name)",
                                                                            "spaces": "drive"}), what="Find folder")
        if res.get("files"):
            return res["files"][0]["id"]
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            meta["parents"] = [parent]
        res, _ = self.call("POST", f"{API}/files?fields=id", json.dumps(meta).encode(),
                           {"Content-Type": "application/json"}, what="Make folder")
        return res["id"]

    def upload(self, path, name, parent, mime):
        """Resumable upload (works for big files)."""
        size = os.path.getsize(path)
        meta = json.dumps({"name": name, "parents": [parent]}).encode()
        _, hdrs = self.call("POST", f"{UPLOAD}?uploadType=resumable&fields=id", meta,
                            {"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Type": mime,
                             "X-Upload-Content-Length": str(size)}, what="Start upload")
        loc = hdrs.get("Location") or hdrs.get("location")
        if not loc:
            raise DriveError("Upload did not start (no upload address).")
        with open(path, "rb") as f:
            res, _ = self.call("PUT", loc, f.read(), {"Content-Type": mime}, what=f"Upload {name}")
        return res.get("id")

    def files_in(self, parent):
        out, page = [], None
        while True:
            params = {"q": f"'{parent}' in parents and trashed = false", "fields": "nextPageToken,files(id,name)",
                      "pageSize": "1000"}
            if page:
                params["pageToken"] = page
            res, _ = self.call("GET", f"{API}/files?" + urllib.parse.urlencode(params), what="List files")
            out += res.get("files", [])
            page = res.get("nextPageToken")
            if not page:
                return out

    def delete(self, file_id):
        self.call("DELETE", f"{API}/files/{file_id}", what="Delete old backup")

    def account(self):
        res, _ = self.call("GET", f"{API}/about?fields=user(emailAddress)", what="Read account")
        return (res.get("user") or {}).get("emailAddress", "")


# ---------------------------------------------------------------- the backup itself

def ist_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)


def db_copy(dest):
    """A consistent copy of the live database (SQLite online backup: safe while people keep working)."""
    src = sqlite3.connect(H.DB_PATH)
    try:
        with sqlite3.connect(dest) as out:
            src.backup(out)
        out.close()
    finally:
        src.close()


def run_backup(kind="auto"):
    """Upload today's database copy + new photos, remove copies older than 30 days. Returns the log row dict."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "message": "A backup is already running."}
    started = H.now()
    log_id = None
    try:
        db = H.get_db()
        log_id = db.execute("INSERT INTO backup_log (kind, started_at, ok, message) VALUES (?,?,0,?)",
                            (kind, started, "running")).lastrowid
        db.commit()
        st = load_state()
        drive = Drive(st)
        root = st.get("root_id") or drive.folder(ROOT_NAME)
        dbf = st.get("db_folder") or drive.folder("Database", root)
        phf = st.get("photo_folder") or drive.folder("Photos", root)
        if (st.get("root_id"), st.get("db_folder"), st.get("photo_folder")) != (root, dbf, phf):
            st.update(root_id=root, db_folder=dbf, photo_folder=phf)
            save_state(st)
        day = ist_now().strftime("%Y-%m-%d")
        name = f"crm-{day}.db" if kind == "auto" else f"crm-{ist_now().strftime('%Y-%m-%d-%H%M')}.db"
        tmpdir = tempfile.mkdtemp(prefix="crm-backup-")
        tmp = os.path.join(tmpdir, name)
        try:
            db_copy(tmp)
            size = os.path.getsize(tmp)
            drive.upload(tmp, name, dbf, "application/x-sqlite3")
        finally:
            try:
                os.remove(tmp)
                os.rmdir(tmpdir)
            except OSError:
                pass
        # photos: only the ones never uploaded before
        media = H.media_dir()
        done = {r[0] for r in db.execute("SELECT name FROM backup_uploaded")}
        new = sorted(n for n in os.listdir(media) if H.MEDIA_NAME.match(n) and n not in done)
        photos = 0
        for n in new:
            drive.upload(os.path.join(media, n), n, phf, "image/jpeg")
            db.execute("INSERT OR IGNORE INTO backup_uploaded (name, uploaded_at) VALUES (?,?)", (n, H.now()))
            db.commit()
            photos += 1
        # keep 30 days of database copies
        cutoff = (ist_now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        removed = 0
        for f in drive.files_in(dbf):
            d = f["name"][4:14]
            if f["name"].startswith("crm-") and len(d) == 10 and d < cutoff:
                drive.delete(f["id"])
                removed += 1
        msg = f"Database {round(size / 1048576, 1)} MB uploaded as {name}; {photos} new photo(s)"
        msg += f"; {removed} old copy(ies) removed" if removed else ""
        db.execute("UPDATE backup_log SET finished_at = ?, ok = 1, db_size = ?, photos = ?, message = ? WHERE id = ?",
                   (H.now(), size, photos, msg, log_id))
        db.commit()
        return {"ok": True, "message": msg}
    except Exception as e:  # noqa: BLE001  (record every failure)
        msg = str(e) if isinstance(e, DriveError) else f"Unexpected error: {e}"
        try:
            db = H.get_db()
            if log_id:
                db.execute("UPDATE backup_log SET finished_at = ?, ok = 0, message = ? WHERE id = ?",
                           (H.now(), msg[:500], log_id))
            db.commit()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "message": msg}
    finally:
        _lock.release()


def due(now_ist=None):
    """The nightly backup should run now: connected, past 02:00 India time, and no good backup today yet."""
    if not connected():
        return False
    now_ist = now_ist or ist_now()
    if now_ist.hour < RUN_HOUR:
        return False
    day = now_ist.strftime("%Y-%m-%d")
    last = H.query("SELECT finished_at, message FROM backup_log WHERE ok = 1 AND kind = 'auto' ORDER BY id DESC LIMIT 1",
                   one=True)
    if last and f"crm-{day}.db" in (last["message"] or ""):
        return False
    tried = H.scalar("SELECT COUNT(*) FROM backup_log WHERE kind = 'auto' AND ok = 0 AND started_at >= ?",
                     ((datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),))
    return tried < 3  # after a failure: try again, at most 3 times in 2 hours


def _ensure_loop():
    if _loop["started"] or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    _loop["started"] = True
    app = H.app

    def loop():
        while True:
            time.sleep(600)  # every 10 minutes
            try:
                with app.app_context():
                    if due():
                        run_backup("auto")
            except Exception:  # noqa: BLE001  (keep the loop alive)
                pass
    threading.Thread(target=loop, daemon=True, name="drive-backup").start()


def alert():
    """Text for a red box on the Super Admin pages when backups are not working (else '')."""
    if not g.get("user") or g.user["role"] != H.SUPER_ADMIN or g.get("viewing"):
        return ""
    try:
        if not connected():
            return "Daily backup to Google Drive is not connected."
        last_ok = H.query("SELECT finished_at FROM backup_log WHERE ok = 1 ORDER BY id DESC LIMIT 1", one=True)
        last = H.query("SELECT ok, message FROM backup_log ORDER BY id DESC LIMIT 1", one=True)
    except sqlite3.Error:
        return ""
    if last and not last["ok"] and last["message"] != "running":
        return f"Last backup failed: {last['message']}"
    if last_ok and last_ok["finished_at"] < (datetime.now() - timedelta(hours=36)).strftime("%Y-%m-%d %H:%M:%S"):
        return "No successful backup in the last 36 hours."
    return ""


# ---------------------------------------------------------------- pages (Super Admin)

def _redirect_uri():
    return url_for("admin_backups_callback", _external=True)


def page():
    st = load_state()
    cid, secret = keys()
    log = H.query("SELECT * FROM backup_log ORDER BY id DESC LIMIT 30")
    photos = H.scalar("SELECT COUNT(*) FROM backup_uploaded")
    media = H.media_dir()
    total_photos = sum(1 for n in os.listdir(media) if H.MEDIA_NAME.match(n))
    return render_template("admin_backups.html", st=st, keys_set=bool(cid and secret), log=log, photos=photos,
                           total_photos=total_photos, redirect_uri=_redirect_uri(), connected=connected(),
                           running=_lock.locked(), keep=KEEP_DAYS, hour=RUN_HOUR)


def connect():
    cid, _ = keys()
    if not cid:
        flash("Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Render first (see the steps on this page).", "error")
        return redirect(url_for("admin_backups"))
    state = secrets.token_urlsafe(16)
    session["gdrive_state"] = state
    q = urllib.parse.urlencode({"client_id": cid, "redirect_uri": _redirect_uri(), "response_type": "code",
                                "scope": SCOPE, "access_type": "offline", "prompt": "consent",
                                "include_granted_scopes": "true", "state": state})
    return redirect(f"{AUTH_URL}?{q}")


def callback():
    if request.args.get("error"):
        flash(f"Google did not connect: {request.args.get('error')}", "error")
        return redirect(url_for("admin_backups"))
    state = session.pop("gdrive_state", None)
    if not state or not secrets.compare_digest(state, request.args.get("state", "")):
        abort(400, "The Google sign-in link expired. Press 'Connect Google Drive' again.")
    cid, secret = keys()
    data = urllib.parse.urlencode({"code": request.args.get("code", ""), "client_id": cid, "client_secret": secret,
                                   "redirect_uri": _redirect_uri(), "grant_type": "authorization_code"}).encode()
    try:
        status, _, body = http("POST", TOKEN_URL, data, {"Content-Type": "application/x-www-form-urlencoded"})
        tok = _json(status, body, "Google sign-in")
        if not tok.get("refresh_token"):
            raise DriveError("Google gave no long-term access. Remove the CRM in your Google account's "
                             "'Third-party access' and connect again.")
        st = {"refresh_token": tok["refresh_token"], "connected_at": H.now(), "connected_by": g.user["name"]}
        d = Drive(st)
        d.token = tok.get("access_token")
        st["account"] = d.account()
        save_state(st)
    except DriveError as e:
        flash(str(e), "error")
        return redirect(url_for("admin_backups"))
    H.audit("Google Drive connected for daily backups", st.get("account", ""))
    flash(f"Google Drive connected ({st.get('account') or 'account'}). Press 'Back up now' to make the first backup.",
          "success")
    return redirect(url_for("admin_backups"))


def disconnect():
    st = load_state()
    st.pop("refresh_token", None)
    save_state(st)
    H.audit("Google Drive backups disconnected")
    flash("Disconnected. Daily backups are stopped. Backups already on Drive stay there.", "success")
    return redirect(url_for("admin_backups"))


def run_now():
    if not connected():
        flash("Connect Google Drive first.", "error")
        return redirect(url_for("admin_backups"))
    if _lock.locked():
        flash("A backup is already running.", "warning")
        return redirect(url_for("admin_backups"))
    app = H.app
    if os.environ.get("PYTEST_CURRENT_TEST"):
        res = run_backup("manual")
        flash(("Backup done: " if res["ok"] else "Backup failed: ") + res["message"], "success" if res["ok"] else "error")
        return redirect(url_for("admin_backups"))

    def work():
        with app.app_context():
            run_backup("manual")
    threading.Thread(target=work, daemon=True, name="drive-backup-now").start()
    flash("Backup started. The first one can take a few minutes (all photos). Refresh this page to see the result.",
          "success")
    return redirect(url_for("admin_backups"))


def download():
    """A fresh copy of the database straight to this computer (does not need Google)."""
    tmpdir = tempfile.mkdtemp(prefix="crm-dl-")
    name = f"crm-{ist_now().strftime('%Y-%m-%d-%H%M')}.db"
    path = os.path.join(tmpdir, name)
    db_copy(path)
    H.audit("Database backup downloaded", name)
    with open(path, "rb") as f:
        data = f.read()
    try:
        os.remove(path)
        os.rmdir(tmpdir)
    except OSError:
        pass
    return Response(data, mimetype="application/x-sqlite3",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
