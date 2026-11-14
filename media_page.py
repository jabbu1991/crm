"""Website page "Realview Media Production" (www.realviewcreativestudio.in/realview-media-production/).

Wedding and live-show photography / videography of Real View. Everything on that page is edited here, in the CRM:
  - Recent work: title, category, date, place, a YouTube or Instagram link and/or photos (shrunk automatically),
    Featured / Hidden. Newest first on the website.
  - Page settings: lead photographer (photo, name, bio, phone, WhatsApp, Instagram, YouTube), the ready WhatsApp
    messages, services, packages, FAQ.
  - Editors: the Super Admin gives someone a username + password that opens ONLY this page (/media). Editors are
    not CRM users: they never see leads, clients or settings.
The website reads /mp/data.json (public) and posts its booking form to /mp/enquiry, which saves a lead in the
agency CRM (campaign "Media Production Enquiries"). Photos are kept with the CRM uploads (so they are backed up).
"""
import io
import json
import re
import time
import urllib.parse
import urllib.request
from functools import wraps
from urllib.parse import parse_qs, urlparse

from flask import abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

H = None
MAX_PHOTOS = 40          # per work
_hits, _fails = {}, {}

DEFAULTS = {
    "brand": "Realview Media Production",
    "hero_title": "Stories you'll relive forever.",
    "hero_text": "Cinematic wedding photography, films and live-show coverage from Kerala.",
    "showreel": "",
    "name": "Azeez Kodinhi",
    "role": "Lead Photographer & Cinematographer",
    "bio": ("Azeez leads every Realview shoot, from intimate weddings to packed live stages, with a calm presence "
            "and a cinematic eye for the real moments in between."),
    "photo": "",
    "phone": "+91 70255 04627",
    "whatsapp": "+91 70255 04627",
    "instagram": "https://www.instagram.com/realview__wedding/",
    "youtube": "https://www.youtube.com/@azeezrealview4509",
    "area": "Malappuram, Kerala",
    "categories": "Wedding\nLive Show\nEvent",
    "services": ("Wedding Photography | Candid and traditional coverage of every ritual, smile and tear.\n"
                 "Wedding Films | Cinematic highlight films and full-length videos to watch for years.\n"
                 "Save the Date & Pre-wedding | Styled couple shoots and short films before the big day.\n"
                 "Engagement & Reception | Complete coverage of every celebration around the wedding.\n"
                 "Live Shows & Concerts | Multi-camera stage coverage, artist moments and after-movies.\n"
                 "Events | Birthdays, launches and corporate events, photographed and filmed."),
    "packages_on": "0",
    "packages": "",
    "faq": "",
    "wa_default": ("Hi Azeez, I saw your work on the Realview Media Production website. I'd like to know more about "
                   "your photography & videography packages.\n\nEvent: \nDate: \nLocation: "),
    "wa_work": ("Hi Azeez, I loved your work \"{work}\" on your website. I'd like a similar shoot for my event."
                "\n\nDate: \nLocation: "),
}
TEXT_LIMITS = {"brand": 80, "hero_title": 90, "hero_text": 220, "showreel": 300, "name": 80, "role": 90,
               "bio": 600, "phone": 30, "whatsapp": 30, "instagram": 300, "youtube": 300, "area": 120,
               "categories": 400, "services": 2500, "packages": 2500, "faq": 5000, "wa_default": 700, "wa_work": 700}


def init(host):
    global H
    H = host
    r = host.app.add_url_rule
    r("/media", "mp_home", need(home))
    r("/media/login", "mp_login", login, methods=["GET", "POST"])
    r("/media/logout", "mp_logout", logout, methods=["POST"])
    r("/media/work/new", "mp_work_new", need(work_form), methods=["GET", "POST"])
    r("/media/work/<int:wid>", "mp_work_edit", need(work_form), methods=["GET", "POST"])
    r("/media/work/<int:wid>/quick", "mp_work_quick", need(work_quick), methods=["POST"])
    r("/media/link", "mp_link", need(quick_link), methods=["POST"])
    r("/media/settings", "mp_settings", need(settings_page), methods=["GET", "POST"])
    r("/media/editors", "mp_editors", need(editors_page, super_only=True), methods=["GET", "POST"])
    r("/mp/data.json", "mp_data", public_data)
    r("/mp/enquiry", "mp_enquiry", enquiry, methods=["POST", "OPTIONS"])
    host.PUBLIC_POSTS.add("mp_enquiry")


# ---------------------------------------------------------------- who is editing

def current():
    """The Super Admin (CRM login) or an active media editor; None otherwise."""
    if g.get("user") and H.is_super() and not g.get("viewing"):
        return {"kind": "super", "name": g.user["name"], "id": None}
    eid = session.get("mp_eid")
    if eid:
        row = H.query("SELECT * FROM media_editors WHERE id = ?", (eid,), one=True)
        if row and row["active"] and row["session_version"] == session.get("mp_sv"):
            return {"kind": "editor", "name": row["name"], "id": row["id"]}
        session.pop("mp_eid", None)
    return None


def need(view, super_only=False):
    @wraps(view)
    def wrapped(*a, **kw):
        g.mp = current()
        if not g.mp:
            return redirect(url_for("mp_login", next=request.path))
        if super_only and g.mp["kind"] != "super":
            abort(403)
        return view(*a, **kw)
    return wrapped


def log(action):
    H.execute("INSERT INTO media_log (who, action, created_at) VALUES (?,?,?)",
              (g.mp["name"] if g.get("mp") else "", action[:300], H.now()))


# ---------------------------------------------------------------- settings

def settings():
    out = dict(DEFAULTS)
    for r in H.query("SELECT key, value FROM media_settings"):
        if r["key"] in out and r["value"] is not None:
            out[r["key"]] = r["value"]
    return out


def lines(text, parts):
    """'A | B | C' lines -> list of lists (blank lines skipped)."""
    out = []
    for ln in (text or "").splitlines():
        if not ln.strip():
            continue
        bits = [b.strip() for b in ln.split("|")]
        out.append((bits + [""] * parts)[:parts])
    return out


def categories(st=None):
    st = st or settings()
    cats = [c.strip() for c in st["categories"].splitlines() if c.strip()]
    return cats or ["Wedding"]


# ---------------------------------------------------------------- links

YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_link(url):
    """(kind, id) for a YouTube / Instagram link; ('link', '') for any other https link; (None, '') if empty."""
    url = (url or "").strip()
    if not url:
        return None, ""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    u = urlparse(url)
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in u.path.split("/") if p]
    if host in ("youtube.com", "music.youtube.com"):
        vid = parse_qs(u.query).get("v", [""])[0]
        if not vid and len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
            vid = parts[1]
        if YT_ID.match(vid or ""):
            return "youtube", vid
    if host == "youtu.be" and parts and YT_ID.match(parts[0]):
        return "youtube", parts[0]
    if host == "instagram.com" and len(parts) >= 2 and parts[0] in ("p", "reel", "reels", "tv") \
            and re.fullmatch(r"[A-Za-z0-9_-]{5,40}", parts[1]):
        return "instagram", ("reel" if parts[0] in ("reel", "reels") else parts[0]) + "/" + parts[1]
    return "link", ""


def clean_url(url):
    url = (url or "").strip()[:300]
    if url and not re.match(r"^https?://", url):
        url = "https://" + url
    return url if re.match(r"^https://[^\s<>\"']+$", url) else ""


# ---------------------------------------------------------------- photos

def save_photo(upload):
    """One upload -> (big, thumb, w, h) or None. Stored with the CRM uploads (backed up)."""
    from PIL import Image
    big = H.save_image(upload, box=(1800, 1800), max_kb=320)
    if not big:
        return None
    upload.stream.seek(0)
    thumb = H.save_image(upload, box=(720, 720), max_kb=80)
    try:
        with Image.open(f"{H.media_dir()}/{big}") as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        w, h = 4, 3
    return big, thumb or big, w, h


def add_photos(wid, files):
    have = H.scalar("SELECT COUNT(*) FROM media_photos WHERE work_id = ?", (wid,))
    sort = H.scalar("SELECT COALESCE(MAX(sort), 0) FROM media_photos WHERE work_id = ?", (wid,))
    added = bad = 0
    for f in files:
        if not f or not f.filename:
            continue
        if have + added >= MAX_PHOTOS:
            flash(f"Only {MAX_PHOTOS} photos per work; the rest were skipped.", "warning")
            break
        got = save_photo(f)
        if not got:
            bad += 1
            continue
        sort += 1
        H.execute("INSERT INTO media_photos (work_id, file, thumb, w, h, sort, created_at) VALUES (?,?,?,?,?,?,?)",
                  (wid, got[0], got[1], got[2], got[3], sort, H.now()))
        added += 1
    if bad:
        flash(f"{bad} file(s) were not photos and were skipped.", "warning")
    return added


def drop_photo(row):
    H.remove_media(row["file"])
    if row["thumb"] != row["file"]:
        H.remove_media(row["thumb"])
    H.execute("DELETE FROM media_photos WHERE id = ?", (row["id"],))


# ---------------------------------------------------------------- pages

def home():
    st = settings()
    cat = request.args.get("cat", "")
    sql = "SELECT * FROM media_works" + (" WHERE category = ?" if cat else "") + \
          " ORDER BY (pinned_at IS NULL), pinned_at DESC, COALESCE(event_date, substr(created_at, 1, 10)) DESC, id DESC"
    works = H.query(sql, (cat,) if cat else ())
    covers = {}
    for w in works:
        covers[w["id"]] = cover_of(w)
    counts = {r["work_id"]: r["n"] for r in H.query("SELECT work_id, COUNT(*) n FROM media_photos GROUP BY work_id")}
    return render_template("media/home.html", st=st, works=works, covers=covers, counts=counts, cat=cat,
                           cats=categories(st), site=site_url())


def cover_of(w, photos=None):
    """(thumb, w, h, big) for a work's cover: the chosen photo, else the first photo, else the YouTube picture."""
    if photos is None:
        photos = H.query("SELECT * FROM media_photos WHERE work_id = ? ORDER BY sort, id", (w["id"],))
    chosen = next((p for p in photos if str(p["id"]) == str(w["cover"] or "")), None) or (photos[0] if photos else None)
    if chosen:
        return f"/media/{chosen['thumb']}", chosen["w"] or 4, chosen["h"] or 3, f"/media/{chosen['file']}"
    if w["link_kind"] == "youtube" and w["link_id"]:
        yt = f"https://i.ytimg.com/vi/{w['link_id']}/"
        return yt + "hqdefault.jpg", 480, 360, yt + "maxresdefault.jpg"
    return "", 4, 3, ""


def work_form(wid=None):
    st = settings()
    w = H.query("SELECT * FROM media_works WHERE id = ?", (wid,), one=True) if wid else None
    if wid and not w:
        abort(404)
    if request.method == "POST":
        f = request.form
        title = H.clean(f.get("title"))[:120]
        cat = H.clean(f.get("category"))[:40] or categories(st)[0]
        date = f.get("event_date", "")[:10]
        date = date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None
        url = clean_url(f.get("link_url"))
        kind, lid = parse_link(url)
        if f.get("link_url", "").strip() and not url:
            flash("That link doesn't look right. Paste the full YouTube or Instagram link.", "error")
        if not title:
            flash("Please give the work a title.", "error")
            return redirect(request.url)
        vals = (title, cat, date, H.clean(f.get("place"))[:120], H.clean(f.get("about"))[:400], url or None, kind,
                lid, 1 if f.get("featured") else 0, 1 if f.get("hidden") else 0)
        ts = H.now()
        if w:
            H.execute("UPDATE media_works SET title=?, category=?, event_date=?, place=?, about=?, link_url=?,"
                      " link_kind=?, link_id=?, featured=?, hidden=?, updated_at=? WHERE id=?", vals + (ts, wid))
        else:
            wid = H.execute("INSERT INTO media_works (title, category, event_date, place, about, link_url, link_kind,"
                            " link_id, featured, hidden, created_by, created_at, updated_at)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", vals + (g.mp["name"], ts, ts))
        for pid in f.getlist("delete_photo"):
            row = H.query("SELECT * FROM media_photos WHERE id = ? AND work_id = ?", (pid, wid), one=True)
            if row:
                drop_photo(row)
        added = add_photos(wid, request.files.getlist("photos"))
        cover = f.get("cover", "")
        if cover and H.query("SELECT 1 FROM media_photos WHERE id = ? AND work_id = ?", (cover, wid), one=True):
            H.execute("UPDATE media_works SET cover = ? WHERE id = ?", (cover, wid))
        if kind is None and not H.scalar("SELECT COUNT(*) FROM media_photos WHERE work_id = ?", (wid,)):
            flash("Saved. Add a YouTube / Instagram link or some photos so it shows nicely on the website.", "warning")
        else:
            flash(("Work updated." if w else "Work added to the website.") + (f" {added} photo(s) added." if added else ""),
                  "success")
        log(("Edited" if w else "Added") + f" work #{wid}: {title}")
        return redirect(url_for("mp_home"))
    photos = H.query("SELECT * FROM media_photos WHERE work_id = ? ORDER BY sort, id", (wid,)) if w else []
    return render_template("media/work_form.html", st=st, w=w, photos=photos, cats=categories(st),
                           max_photos=MAX_PHOTOS, today=H.now()[:10])


def work_quick(wid):
    w = H.query("SELECT * FROM media_works WHERE id = ?", (wid,), one=True) or abort(404)
    act = request.form.get("action")
    if act == "delete":
        for p in H.query("SELECT * FROM media_photos WHERE work_id = ?", (wid,)):
            drop_photo(p)
        H.execute("DELETE FROM media_works WHERE id = ?", (wid,))
        flash("Work deleted.", "success")
        log(f"Deleted work #{wid}: {w['title']}")
    elif act in ("hide", "show"):
        H.execute("UPDATE media_works SET hidden = ? WHERE id = ?", (1 if act == "hide" else 0, wid))
        log(f"{act.title()} work #{wid}")
    elif act in ("top", "untop"):
        H.execute("UPDATE media_works SET pinned_at = ? WHERE id = ?", (H.now() if act == "top" else None, wid))
        log(("Moved to top" if act == "top" else "Unpinned") + f" work #{wid}")
    elif act in ("feature", "unfeature"):
        H.execute("UPDATE media_works SET featured = ? WHERE id = ?", (1 if act == "feature" else 0, wid))
        log(f"{act.title()} work #{wid}")
    return redirect(url_for("mp_home", cat=request.form.get("cat") or None))


def youtube_title(vid):
    """The video's title from YouTube (oEmbed, no key needed); '' if YouTube can't be reached. Replaceable in tests."""
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "RealViewCRM/1.0"}),
                                    timeout=6) as r:
            return (json.loads(r.read(200000) or b"{}").get("title") or "")[:120]
    except Exception:  # noqa: BLE001  (no title is fine)
        return ""


def quick_link():
    """Paste a YouTube / Instagram link -> a work at once (YouTube title + picture come by themselves)."""
    url = clean_url(request.form.get("url"))
    kind, lid = parse_link(url)
    if kind not in ("youtube", "instagram"):
        flash("Paste a YouTube video link or an Instagram post / reel link.", "error")
        return redirect(url_for("mp_home"))
    dup = H.query("SELECT id, title FROM media_works WHERE link_kind = ? AND link_id = ?", (kind, lid), one=True)
    if dup:
        flash(f"This link is already on the page (\"{dup['title']}\").", "warning")
        return redirect(url_for("mp_home"))
    st = settings()
    cat = H.clean(request.form.get("category"))[:40] or categories(st)[0]
    if kind == "youtube":
        title = youtube_title(lid) or "Film"
    else:
        title = "Instagram reel" if lid.startswith("reel/") else "Instagram post"
    ts = H.now()
    wid = H.execute("INSERT INTO media_works (title, category, event_date, link_url, link_kind, link_id, created_by,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (title, cat, ts[:10], url, kind, lid, g.mp["name"], ts, ts))
    log(f"Added {kind} link #{wid}: {title}")
    flash(("🎬 Film added: " if kind == "youtube" else "📸 Instagram post added: ") + title
          + ". It shows first on the website within a minute.", "success")
    return redirect(url_for("mp_home"))


def settings_page():
    st = settings()
    if request.method == "POST":
        f = request.form
        for key, limit in TEXT_LIMITS.items():
            if key not in f:
                continue
            val = (f.get(key) or "").replace("\r\n", "\n").strip()[:limit]
            if key in ("instagram", "youtube", "showreel"):
                val = clean_url(val)
            if key in ("phone", "whatsapp"):
                val = re.sub(r"[^\d+ ]", "", val)
            H.execute("INSERT INTO media_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                      " value = excluded.value", (key, val))
        H.execute("INSERT INTO media_settings (key, value) VALUES ('packages_on', ?) ON CONFLICT(key) DO UPDATE SET"
                  " value = excluded.value", ("1" if f.get("packages_on") else "0",))
        up = request.files.get("photo_file")
        if up and up.filename:
            name = H.save_image(up, box=(1200, 1500), max_kb=260)
            if name:
                H.remove_media(st["photo"])
                H.execute("INSERT INTO media_settings (key, value) VALUES ('photo', ?) ON CONFLICT(key) DO UPDATE"
                          " SET value = excluded.value", (name,))
            else:
                flash("The photo could not be read. Please choose a JPG or PNG photo.", "error")
        elif f.get("photo_remove"):
            H.remove_media(st["photo"])
            H.execute("DELETE FROM media_settings WHERE key = 'photo'")
        log("Updated page settings")
        flash("Page settings saved. The website shows them within a minute.", "success")
        return redirect(url_for("mp_settings"))
    return render_template("media/settings.html", st=st)


def editors_page():
    if request.method == "POST":
        f = request.form
        act = f.get("action")
        if act == "add":
            name, user = H.clean(f.get("name"))[:80], H.clean(f.get("username")).lower()[:40]
            pw = f.get("password") or ""
            if not name or not re.fullmatch(r"[a-z0-9._-]{3,40}", user):
                flash("Give a name and a username (3+ letters/numbers, no spaces).", "error")
            elif len(pw) < 8:
                flash("The password needs at least 8 characters.", "error")
            elif H.query("SELECT 1 FROM media_editors WHERE username = ?", (user,), one=True):
                flash("That username is already taken.", "error")
            else:
                H.execute("INSERT INTO media_editors (name, username, password_hash, created_at) VALUES (?,?,?,?)",
                          (name, user, generate_password_hash(pw), H.now()))
                log(f"Added editor {user}")
                flash(f"Editor added. Login: {url_for('mp_login', _external=True)} · username {user}", "success")
        else:
            eid = f.get("id", type=int)
            row = H.query("SELECT * FROM media_editors WHERE id = ?", (eid,), one=True) or abort(404)
            if act == "toggle":
                H.execute("UPDATE media_editors SET active = ?, session_version = session_version + 1 WHERE id = ?",
                          (0 if row["active"] else 1, eid))
                flash("Editor " + ("disabled." if row["active"] else "enabled."), "success")
            elif act == "password":
                pw = f.get("password") or ""
                if len(pw) < 8:
                    flash("The password needs at least 8 characters.", "error")
                else:
                    H.execute("UPDATE media_editors SET password_hash = ?, session_version = session_version + 1"
                              " WHERE id = ?", (generate_password_hash(pw), eid))
                    flash(f"New password set for {row['username']}.", "success")
            elif act == "delete":
                H.execute("DELETE FROM media_editors WHERE id = ?", (eid,))
                flash("Editor removed.", "success")
            log(f"Editor {row['username']}: {act}")
        return redirect(url_for("mp_editors"))
    editors = H.query("SELECT * FROM media_editors ORDER BY active DESC, name")
    history = H.query("SELECT * FROM media_log ORDER BY id DESC LIMIT 40")
    return render_template("media/editors.html", editors=editors, history=history,
                           login_url=url_for("mp_login", _external=True))


def login():
    if current():
        return redirect(url_for("mp_home"))
    if request.method == "POST":
        user = H.clean(request.form.get("username")).lower()
        key = (user, request.remote_addr)
        rec = _fails.get(key)
        if rec and rec[0] >= 6 and time.time() - rec[1] < 900:
            flash("Too many tries. Please wait 15 minutes.", "error")
            return render_template("media/login.html"), 429
        row = H.query("SELECT * FROM media_editors WHERE username = ?", (user,), one=True)
        if row and row["active"] and check_password_hash(row["password_hash"], request.form.get("password") or ""):
            _fails.pop(key, None)
            session["mp_eid"], session["mp_sv"] = row["id"], row["session_version"]
            session.permanent = True
            H.execute("UPDATE media_editors SET last_login_at = ? WHERE id = ?", (H.now(), row["id"]))
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/media") else url_for("mp_home"))
        r = _fails.setdefault(key, [0, time.time()])
        r[0] += 1
        flash("This account is disabled." if row and not row["active"] else "Wrong username or password.", "error")
    return render_template("media/login.html")


def logout():
    session.pop("mp_eid", None)
    session.pop("mp_sv", None)
    return redirect(url_for("mp_login"))


def site_url():
    return "https://www.realviewcreativestudio.in/realview-media-production/"


# ---------------------------------------------------------------- public (website)

def public_data():
    st = settings()
    base = request.url_root.rstrip("/")
    works = []
    rows = H.query("SELECT * FROM media_works WHERE hidden = 0"
                   " ORDER BY (pinned_at IS NULL), pinned_at DESC, COALESCE(event_date, substr(created_at, 1, 10)) DESC, id DESC")
    photos = {}
    for p in H.query("SELECT * FROM media_photos ORDER BY sort, id"):
        photos.setdefault(p["work_id"], []).append(p)
    for w in rows:
        ps = photos.get(w["id"], [])
        src, cw, ch, big = cover_of(w, ps)
        full = lambda u: (base + u) if u.startswith("/") else u  # noqa: E731
        works.append({
            "id": w["id"], "pinned": bool(w["pinned_at"]), "title": w["title"], "category": w["category"], "date": w["event_date"] or "",
            "place": w["place"] or "", "about": w["about"] or "", "featured": bool(w["featured"]),
            "kind": w["link_kind"] or "", "link": w["link_url"] or "", "lid": w["link_id"] or "",
            "cover": {"src": full(src), "big": full(big), "w": cw, "h": ch} if src else None,
            "photos": [{"src": f"{base}/media/{p['file']}", "thumb": f"{base}/media/{p['thumb']}",
                        "w": p["w"] or 4, "h": p["h"] or 3} for p in ps],
        })
    pub = {k: st[k] for k in ("brand", "hero_title", "hero_text", "name", "role", "bio", "phone", "whatsapp",
                               "instagram", "youtube", "area", "wa_default", "wa_work")}
    sk, sid = parse_link(st["showreel"])
    pub["showreel"] = sid if sk == "youtube" else ""
    pub["photo"] = f"{base}/media/{st['photo']}" if st["photo"] else ""
    pub["services"] = [{"title": a, "text": b} for a, b in lines(st["services"], 2) if a]
    pub["packages"] = [{"name": a, "price": b, "items": [x.strip() for x in c.split(",") if x.strip()]}
                       for a, b, c in lines(st["packages"], 3) if a] if st["packages_on"] == "1" else []
    pub["faq"] = [{"q": a, "a": b} for a, b in lines(st["faq"], 2) if a and b]
    pub["categories"] = [c for c in categories(st) if any(w["category"] == c for w in works)]
    resp = jsonify({"settings": pub, "works": works})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def enquiry():
    """Booking form of the website page -> a lead in the agency CRM (campaign "Media Production Enquiries")."""
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if not H.ENQUIRY_ORIGINS or origin not in H.ENQUIRY_ORIGINS:
        return {"ok": False}, 403
    headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    if request.method == "OPTIONS":
        return "", 204, dict(headers, **{"Access-Control-Allow-Methods": "POST"})
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    now_ts = time.time()
    hits = [t for t in _hits.get(ip, []) if now_ts - t < 3600]
    if len(hits) >= 10:
        return {"ok": False, "error": "rate_limited"}, 429, headers
    _hits[ip] = hits + [now_ts]
    if len(_hits) > 20000:
        _hits.clear()
    f = request.form
    if f.get("website"):                       # honeypot
        return {"ok": True}, 200, headers
    c = lambda k, n: H.clean(f.get(k))[:n]  # noqa: E731
    name, phone, event, when, place, msg = (c("name", 120), c("phone", 40), c("event", 60), c("date", 30),
                                             c("place", 120), c("message", 1000))
    if not name or len(H.phone_key(phone)) < 10:
        return {"ok": False, "error": "name_and_phone_required"}, 400, headers
    db = H.get_db()
    org = db.execute("SELECT id FROM organizations WHERE kind = 'agency' ORDER BY id LIMIT 1").fetchone()
    owner = org and db.execute("SELECT id FROM users WHERE org_id = ? AND role = ? AND active = 1 ORDER BY id LIMIT 1",
                               (org[0], H.SUPER_ADMIN)).fetchone()
    if not owner:
        return {"ok": False, "error": "not_set_up"}, 503, headers
    oid, uid, ts = org[0], owner[0], H.now()
    project = db.execute("SELECT id FROM projects WHERE org_id = ? AND code = 'MEDIA'", (oid,)).fetchone()
    pid = project[0] if project else db.execute(
        "INSERT INTO projects (org_id, name, code, campaign_type, description, status, created_by, created_at,"
        " updated_at) VALUES (?, 'Media Production Enquiries', 'MEDIA', 'Lead Generation',"
        " 'Bookings from the Realview Media Production website page', 'Active', ?, ?, ?)", (oid, uid, ts, ts)).lastrowid
    requirement = " · ".join(x for x in (event and f"{event} shoot", when and f"Date: {when}",
                                         place and f"Place: {place}") if x)
    lead_id = db.execute(
        "INSERT INTO leads (org_id, project_id, customer_name, phone, whatsapp, phone_norm, place, requirement, notes,"
        " status, priority, source, country, entered_by, assigned_to, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'India', ?,?,?,?)",
        (oid, pid, name, phone, phone, H.phone_key(phone), place, requirement or "Photography / videography",
         msg, H.default_status(oid), "High" if "High" in H.opt_all("priority", oid) else "",
         "Website" if "Website" in H.opt_all("source", oid) else "", uid, uid, ts, ts)).lastrowid
    db.execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at) VALUES (?,?,?,?)",
               (lead_id, uid, "Booking enquiry from the Realview Media Production page", ts))
    db.commit()
    return {"ok": True}, 201, headers
