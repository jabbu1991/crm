"""Instagram stories on the website (Super Admin -> Instagram).

The official Instagram API only shows the stories of the last 24 hours (highlights are not available), so the CRM
keeps its own copy of every story:
  - once a day (automatic, a little under 24 hours apart so no story is missed) and on "Check Instagram now",
    the CRM asks Instagram for the current stories and downloads every new one (photo or video) to the server disk,
  - the website (www.realviewcreativestudio.in) reads /ig/stories.json and shows them newest first, forever,
  - the Super Admin can hide a story or give it a short title; only the latest KEEP stories are kept on the disk.

Instagram access: a long-lived "Instagram API with Instagram Login" token (Meta app -> Generate token), pasted once
on the Instagram page. It is kept in a file next to the database (not inside it, so it never ends up in a backup)
and refreshed automatically (it would expire after 60 days otherwise). Uses only the Python standard library for HTTP.
"""
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from flask import abort, flash, jsonify, redirect, render_template, request, send_from_directory, url_for

H = None
GRAPH = "https://graph.instagram.com"              # Instagram Login tokens (IGAA...)
GRAPH_FB = "https://graph.facebook.com/v23.0"      # Unified / Facebook Login tokens (EAA..., never expire)
PROFILE = "realview_creative_studio"
CHECK_EVERY_HOURS = 22      # automatic check: a little under 24 h, so a story is never missed
RETRY_AFTER_HOURS = 1       # after a failed automatic check
REFRESH_TOKEN_DAYS = 7      # refresh the 60-day token when it is older than this
KEEP = 60                   # stories kept on the disk (oldest removed first)
MAX_VIDEO_MB = 40
FILE_NAME = re.compile(r"^[0-9a-f]{32}(_t)?\.(jpg|mp4)$")
_lock = threading.Lock()
_loop = {"started": False}


def init(host):
    global H
    H = host
    app = host.app
    app.add_url_rule("/admin/instagram", "admin_instagram", host.super_required(page))
    app.add_url_rule("/admin/instagram/token", "admin_instagram_token", host.super_required(save_token),
                     methods=["POST"])
    app.add_url_rule("/admin/instagram/disconnect", "admin_instagram_disconnect", host.super_required(disconnect),
                     methods=["POST"])
    app.add_url_rule("/admin/instagram/check", "admin_instagram_check", host.super_required(check_now),
                     methods=["POST"])
    app.add_url_rule("/admin/instagram/story/<int:sid>", "admin_instagram_story", host.super_required(edit_story),
                     methods=["POST"])
    app.add_url_rule("/ig/stories.json", "ig_stories_json", stories_json)
    app.add_url_rule("/ig/media/<name>", "ig_media", media)
    app.before_request(_ensure_loop)


# ---------------------------------------------------------------- storage

def data_dir():
    return os.path.dirname(os.path.abspath(H.DB_PATH))


def media_dir():
    path = os.path.join(data_dir(), "instagram")
    os.makedirs(path, exist_ok=True)
    return path


def _state_path():
    return os.path.join(data_dir(), "instagram.json")


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


def connected():
    return bool(load_state().get("token"))


# ---------------------------------------------------------------- HTTP (replaceable in tests)

class IgError(Exception):
    pass


def http(url, max_bytes=None):
    """GET url. Returns (status, body bytes). Raises IgError on a network problem."""
    req = urllib.request.Request(url, headers={"User-Agent": "RealViewCRM/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read(max_bytes + 1) if max_bytes else resp.read()
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise IgError(f"Network problem: {e}")


def api(path, token, base=None, **params):
    params["access_token"] = token
    status, body = http(f"{base or GRAPH}/{path}?" + urllib.parse.urlencode(params))
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = {}
    if status >= 300 or "error" in data:
        err = data.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        if status in (400, 401) and isinstance(err, dict) and err.get("code") == 190:
            raise IgError("The Instagram token expired or was removed. Generate a new token and save it again.")
        raise IgError(f"Instagram said ({status}): {msg or body[:200]!r}")
    return data


# ---------------------------------------------------------------- the check itself

def _parse_ts(ts):
    """Instagram time '2026-11-05T10:20:30+0000' -> server-style 'YYYY-MM-DD HH:MM:SS' in UTC."""
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _save_photo(raw, box, max_kb):
    """Any image bytes -> a light JPEG on the disk. Returns the file name."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    img.thumbnail(box)
    for quality in (82, 74, 66, 58, 50):
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
        if out.tell() <= max_kb * 1024:
            break
    return out.getvalue()


def _download(url, max_mb):
    status, body = http(url, max_bytes=max_mb * 1048576)
    if status >= 300 or not body:
        raise IgError(f"Could not download a story file ({status}).")
    if len(body) > max_mb * 1048576:
        raise IgError(f"A story file is bigger than {max_mb} MB; skipped.")
    return body


def _store(story):
    """Download one story; returns (file_name, thumb_name)."""
    base = uuid.uuid4().hex
    folder = media_dir()
    if story.get("media_type") == "VIDEO":
        raw = _download(story["media_url"], MAX_VIDEO_MB)
        name = base + ".mp4"
        with open(os.path.join(folder, name), "wb") as f:
            f.write(raw)
        thumb = None
        if story.get("thumbnail_url"):
            try:
                tb = _save_photo(_download(story["thumbnail_url"], 10), (240, 427), 30)
                thumb = base + "_t.jpg"
                with open(os.path.join(folder, thumb), "wb") as f:
                    f.write(tb)
            except Exception:  # noqa: BLE001  (a missing poster is fine)
                thumb = None
        return name, thumb
    raw = _download(story["media_url"], 15)
    big = _save_photo(raw, (1080, 1920), 260)
    small = _save_photo(raw, (240, 427), 30)
    name, thumb = base + ".jpg", base + "_t.jpg"
    with open(os.path.join(folder, name), "wb") as f:
        f.write(big)
    with open(os.path.join(folder, thumb), "wb") as f:
        f.write(small)
    return name, thumb


def _remove_files(*names):
    for n in names:
        if n and FILE_NAME.match(n):
            try:
                os.remove(os.path.join(media_dir(), n))
            except OSError:
                pass


def _base(st):
    return GRAPH_FB if st.get("api") == "fb" else GRAPH


def discover(token, ig_id=""):
    """Which kind of token is this, and which Instagram account does it reach?
    Returns (api, user_id, username). Instagram Login tokens use graph.instagram.com; Unified / Facebook Login
    (system-user) tokens use graph.facebook.com with the Instagram business account id."""
    if ig_id:
        me = api(ig_id, token, base=GRAPH_FB, fields="id,username")
        return "fb", str(me.get("id") or ig_id), me.get("username") or ""
    try:
        me = api("me", token, fields="user_id,username")
        return "ig", str(me.get("user_id") or me.get("id") or ""), me.get("username") or ""
    except IgError as first:
        try:
            pages = api("me/accounts", token, base=GRAPH_FB, fields="instagram_business_account{id,username}")
        except IgError:
            raise first
        for pg in pages.get("data", []):
            acc = pg.get("instagram_business_account") or {}
            if acc.get("id"):
                return "fb", str(acc["id"]), acc.get("username") or ""
        raise IgError("The token works, but no Instagram account was found. Paste the Instagram account ID "
                      "too (Meta app -> Instagram -> API setup: the number shown under the account).")


def _refresh_token(st):
    """Instagram Login tokens last 60 days; refresh them weekly so they never run out.
    (Unified / Facebook Login system-user tokens never expire.)"""
    if st.get("api") == "fb":
        return
    saved = st.get("token_saved_at") or ""
    if saved and saved > (datetime.now() - timedelta(days=REFRESH_TOKEN_DAYS)).strftime("%Y-%m-%d %H:%M:%S"):
        return
    try:
        data = api("refresh_access_token", st["token"], grant_type="ig_refresh_token")
    except IgError:
        return  # the old token may still work; the check below reports a real problem
    if data.get("access_token"):
        st["token"] = data["access_token"]
        st["token_saved_at"] = H.now()
        save_state(st)


def run_check(kind="auto"):
    """Ask Instagram for the current stories and save the new ones. Returns {"ok", "message", "new"}."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "message": "A check is already running.", "new": 0}
    log_id = None
    try:
        db = H.get_db()
        log_id = db.execute("INSERT INTO ig_check_log (kind, started_at, ok, message) VALUES (?,?,0,'running')",
                            (kind, H.now())).lastrowid
        db.commit()
        st = load_state()
        if not st.get("token"):
            raise IgError("Instagram is not connected.")
        _refresh_token(st)
        user = st.get("user_id") or "me"
        data = api(f"{user}/stories", st["token"], base=_base(st),
                   fields="id,media_type,media_url,thumbnail_url,timestamp,permalink")
        known = {r[0] for r in db.execute("SELECT ig_id FROM ig_stories")}
        new, skipped = 0, []
        for s in sorted(data.get("data", []), key=lambda x: x.get("timestamp") or ""):
            sid = str(s.get("id") or "")
            if not sid or sid in known or not s.get("media_url"):
                continue
            try:
                name, thumb = _store(s)
            except Exception as e:  # noqa: BLE001  (one bad file must not stop the others)
                skipped.append(str(e) if isinstance(e, IgError) else "a file could not be read")
                continue
            db.execute("INSERT INTO ig_stories (ig_id, media_type, file_name, thumb_name, posted_at, permalink,"
                       " fetched_at) VALUES (?,?,?,?,?,?,?)",
                       (sid, "VIDEO" if s.get("media_type") == "VIDEO" else "IMAGE", name, thumb,
                        _parse_ts(s.get("timestamp")), s.get("permalink"), H.now()))
            db.commit()
            new += 1
        # keep only the latest KEEP stories on the disk
        removed = 0
        for r in db.execute("SELECT id, file_name, thumb_name FROM ig_stories ORDER BY posted_at DESC, id DESC"
                            " LIMIT -1 OFFSET ?", (KEEP,)).fetchall():
            _remove_files(r["file_name"], r["thumb_name"])
            db.execute("DELETE FROM ig_stories WHERE id = ?", (r["id"],))
            removed += 1
        db.commit()
        live = len(data.get("data", []))
        msg = f"{new} new stor{'y' if new == 1 else 'ies'} saved ({live} live on Instagram now)"
        msg += f"; {removed} old removed" if removed else ""
        msg += f"; skipped: {skipped[0]}" if skipped else ""
        db.execute("UPDATE ig_check_log SET ok = 1, new_count = ?, message = ? WHERE id = ?", (new, msg, log_id))
        db.commit()
        return {"ok": True, "message": msg, "new": new}
    except Exception as e:  # noqa: BLE001  (record every failure)
        msg = str(e) if isinstance(e, IgError) else f"Unexpected error: {e}"
        try:
            db = H.get_db()
            if log_id:
                db.execute("UPDATE ig_check_log SET ok = 0, message = ? WHERE id = ?", (msg[:500], log_id))
                db.commit()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "message": msg, "new": 0}
    finally:
        _lock.release()


def due(now=None):
    """The automatic check should run: connected, and no good automatic check in the last 22 hours
    (after a failure: again after 1 hour)."""
    if not connected():
        return False
    now = now or datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    last_ok = H.scalar("SELECT MAX(started_at) FROM ig_check_log WHERE ok = 1")
    if last_ok and last_ok > (now - timedelta(hours=CHECK_EVERY_HOURS)).strftime(fmt):
        return False
    last_try = H.scalar("SELECT MAX(started_at) FROM ig_check_log WHERE kind = 'auto'")
    return not (last_try and last_try > (now - timedelta(hours=RETRY_AFTER_HOURS)).strftime(fmt))


def _ensure_loop():
    if _loop["started"] or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    _loop["started"] = True
    app = H.app

    def loop():
        while True:
            time.sleep(600)  # look every 10 minutes whether a check is due
            try:
                with app.app_context():
                    if due():
                        run_check("auto")
            except Exception:  # noqa: BLE001  (keep the loop alive)
                pass
    threading.Thread(target=loop, daemon=True, name="instagram-stories").start()


# ---------------------------------------------------------------- public (website)

def stories_json():
    rows = H.query("SELECT id, media_type, file_name, thumb_name, posted_at, title FROM ig_stories WHERE hidden = 0"
                   " ORDER BY posted_at DESC, id DESC")
    base = request.url_root.rstrip("/")
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    out = [{"id": r["id"], "type": "video" if r["media_type"] == "VIDEO" else "image",
            "src": f"{base}/ig/media/{r['file_name']}",
            "thumb": f"{base}/ig/media/{r['thumb_name']}" if r["thumb_name"] else None,
            "posted": (r["posted_at"] or "").replace(" ", "T") + "Z",
            "title": r["title"] or "", "new": (r["posted_at"] or "") >= day_ago} for r in rows]
    resp = jsonify({"profile": f"https://www.instagram.com/{load_state().get('username') or PROFILE}/",
                    "stories": out})
    resp.headers["Access-Control-Allow-Origin"] = "*"   # public data, read by the website
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


def media(name):
    if not FILE_NAME.match(name):
        abort(404)
    mime = "video/mp4" if name.endswith(".mp4") else "image/jpeg"
    resp = send_from_directory(media_dir(), name, mimetype=mime, max_age=365 * 24 * 3600, conditional=True)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------------------------------------------------------------- pages (Super Admin)

def page():
    st = load_state()
    stories = H.query("SELECT * FROM ig_stories WHERE hidden < 2 ORDER BY posted_at DESC, id DESC")
    log = H.query("SELECT * FROM ig_check_log ORDER BY id DESC LIMIT 20")
    size = 0
    for n in os.listdir(media_dir()):
        try:
            size += os.path.getsize(os.path.join(media_dir(), n))
        except OSError:
            pass
    return render_template("admin_instagram.html", st=st, connected=connected(), stories=stories, log=log,
                           running=_lock.locked(), keep=KEEP, hours=CHECK_EVERY_HOURS,
                           size_mb=round(size / 1048576, 1), profile=PROFILE)


def save_token():
    token = re.sub(r"\s+", "", request.form.get("token", ""))
    if len(token) < 20:
        flash("Paste the whole Instagram token.", "error")
        return redirect(url_for("admin_instagram"))
    ig_id = re.sub(r"\D", "", request.form.get("ig_id", ""))
    try:
        kind, user_id, username = discover(token, ig_id)
    except IgError as e:
        flash(f"Instagram did not accept this token: {e}", "error")
        return redirect(url_for("admin_instagram"))
    save_state({"token": token, "token_saved_at": H.now(), "api": kind, "user_id": user_id,
                "username": username, "connected_at": H.now()})
    res = run_check("manual")
    flash(f"Instagram connected (@{username or '?'}). {res['message']}",
          "success" if res["ok"] else "error")
    return redirect(url_for("admin_instagram"))


def disconnect():
    try:
        os.remove(_state_path())
    except OSError:
        pass
    flash("Instagram disconnected. Saved stories stay on the website.", "success")
    return redirect(url_for("admin_instagram"))


def check_now():
    res = run_check("manual")
    flash(("Checked Instagram: " if res["ok"] else "Check failed: ") + res["message"],
          "success" if res["ok"] else "error")
    return redirect(url_for("admin_instagram"))


def edit_story(sid):
    db = H.get_db()
    row = db.execute("SELECT * FROM ig_stories WHERE id = ?", (sid,)).fetchone()
    if not row:
        abort(404)
    action = request.form.get("action")
    if action == "delete":
        _remove_files(row["file_name"], row["thumb_name"])
        db.execute("UPDATE ig_stories SET hidden = 2 WHERE id = ?", (sid,))  # kept as a marker: never saved again
        flash("Story deleted from the website.", "success")
    elif action in ("hide", "show"):
        db.execute("UPDATE ig_stories SET hidden = ? WHERE id = ?", (1 if action == "hide" else 0, sid))
        flash("Story hidden from the website." if action == "hide" else "Story shown on the website.", "success")
    else:
        db.execute("UPDATE ig_stories SET title = ? WHERE id = ?", (H.clean(request.form.get("title"))[:30], sid))
        flash("Title saved.", "success")
    db.commit()
    return redirect(url_for("admin_instagram"))
