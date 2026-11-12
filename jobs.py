"""Website Careers form → agency CRM ("Job Applications"), with a phone notification to the agency's admins.

The website posts here from the careers page (only from CRM_ENQUIRY_ORIGINS, like the contact form). No login,
no form token: the organization, campaign and creator are fixed here, never taken from the form.
Admins turn on phone notifications on My Account (Web Push, the same helper as the cafe and family alerts).
"""
import json
import re
import time

from flask import g, request, url_for

H = None
_hits = {}
TEMPLATES = [
    ("Application received",
     "Hi {name}, thank you for applying to {org_name}. We have received your application and will contact you "
     "soon.\n\n– {my_name}"),
    ("Interview call",
     "Hi {name}, this is {my_name} from {org_name}. We liked your application and would like to talk to you. "
     "When is a good time to call you?"),
]


def init(host):
    global H
    H = host
    app = host.app
    r = app.add_url_rule
    r("/job-application", "website_job", website_job, methods=["POST", "OPTIONS"])
    r("/notify/key", "notify_key", host.admin_required(notify_key))
    r("/notify/subscribe", "notify_subscribe", host.admin_required(notify_subscribe), methods=["POST"])
    r("/notify/unsubscribe", "notify_unsubscribe", host.admin_required(notify_unsubscribe), methods=["POST"])
    r("/notify/test", "notify_test", host.admin_required(notify_test), methods=["POST"])


def _clean(name, n):
    return H.clean(request.form.get(name))[:n]


def website_job():
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
    name, phone, place, role = _clean("name", 120), _clean("whatsapp", 40), _clean("place", 120), _clean("role", 120)
    if not name or len(H.phone_key(phone)) < 10 or not role:
        return {"ok": False, "error": "name_phone_role_required"}, 400, headers
    pick = lambda k: ", ".join(H.clean(x)[:30] for x in f.getlist(k)[:5] if H.clean(x))
    link = _clean("portfolio", 500)
    details = [("Role", role), ("Job type", pick("job_type")), ("Work location", pick("location")),
               ("Experience", _clean("experience", 30)), ("Portfolio / Resume / Instagram", link),
               ("About", _clean("about", 600))]
    notes = "\n".join(f"{k}: {v}" for k, v in details if v)

    db = H.get_db()
    org = db.execute("SELECT * FROM organizations WHERE kind = 'agency' ORDER BY id LIMIT 1").fetchone()
    owner = org and db.execute("SELECT id FROM users WHERE org_id = ? AND role = ? AND active = 1 ORDER BY id LIMIT 1",
                               (org["id"], H.SUPER_ADMIN)).fetchone()
    if not owner:
        return {"ok": False, "error": "not_set_up"}, 503, headers
    oid, uid, ts = org["id"], owner[0], H.now()
    project = db.execute("SELECT id FROM projects WHERE org_id = ? AND code = 'JOBS'", (oid,)).fetchone()
    if project:
        pid = project[0]
    else:
        pid = db.execute(
            "INSERT INTO projects (org_id, name, code, campaign_type, description, status, created_by, created_at,"
            " updated_at) VALUES (?, 'Job Applications', 'JOBS', 'Other', 'Applications from the website careers page',"
            " 'Active', ?, ?, ?)", (oid, uid, ts, ts)).lastrowid
        _add_templates(db, oid, uid, ts)
    key = H.phone_key(phone)
    old = db.execute("SELECT id FROM leads WHERE org_id = ? AND project_id = ? AND phone_norm = ? ORDER BY id DESC LIMIT 1",
                     (oid, pid, key)).fetchone()
    if old:
        lid = old[0]
        db.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (ts, lid))
        db.execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at) VALUES (?,?,?,?)",
                   (lid, uid, "Applied again on the website — " + notes.replace("\n", " · "), ts))
    else:
        opts = lambda kind, want: want if want in H.opt_all(kind, oid) else ""
        lid = db.execute(
            "INSERT INTO leads (org_id, project_id, customer_name, phone, whatsapp, phone_norm, place, requirement,"
            " notes, status, priority, source, country, entered_by, assigned_to, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'India', ?,?,?,?)",
            (oid, pid, name, phone, phone, key, place, role, notes, H.default_status(oid), H.default_priority(oid),
             opts("source", "Website"), uid, uid, ts, ts)).lastrowid
        db.execute("INSERT INTO lead_activity (lead_id, user_id, action, created_at) VALUES (?,?,?,?)",
                   (lid, uid, "Job application from the website careers page", ts))
    db.commit()
    notify_admins(oid, f"🧑‍💼 New job application: {name}", f"{role} · {place} — tap to open",
                  f"/leads/{lid}", f"job-{lid}")
    return {"ok": True}, 201, headers


def _add_templates(db, oid, uid, ts):
    have = {r[0] for r in db.execute("SELECT title FROM wa_templates WHERE org_id = ? AND scope = 'org'", (oid,))}
    for i, (title, body) in enumerate(TEMPLATES):
        if title not in have:
            db.execute("INSERT INTO wa_templates (org_id, scope, title, body, active, sort, created_by, created_at)"
                       " VALUES (?, 'org', ?, ?, 1, ?, ?, ?)", (oid, title, body, 50 + i, uid, ts))


# ---------------------------------------------------------------- phone notifications for admins

def _admin_subs(oid):
    return [dict(r) for r in H.query("""SELECT s.* FROM push_subscriptions s JOIN users u ON u.id = s.user_id
                                        WHERE s.org_id = ? AND u.active = 1 AND u.role IN ('org_admin', 'super_admin')""",
                                     (oid,))]


def notify_admins(oid, title, body, url, tag):
    """Never stops the form: a failed notification is ignored."""
    try:
        import cafe_push
        subs = _admin_subs(oid)
        if subs:
            cafe_push._dispatch(subs, json.dumps({"title": title, "body": body, "url": url, "tag": tag},
                                                 ensure_ascii=False))
        return len(subs)
    except Exception:  # noqa: BLE001
        return 0


def notify_key():
    import cafe_push
    return {"ok": True, "key": cafe_push.vapid()[1]}


def notify_subscribe():
    if g.get("viewing"):
        return {"ok": False}, 403
    try:
        sub = json.loads(request.form.get("payload") or "{}")
        endpoint = str(sub["endpoint"])[:1000]
        keys = sub.get("keys") or {}
        p256dh, auth = str(keys["p256dh"])[:200], str(keys["auth"])[:100]
        assert re.match(r"https://", endpoint)
    except (ValueError, KeyError, TypeError, AssertionError):
        return {"ok": False, "error": "This browser cannot get notifications."}, 400
    db = H.get_db()
    db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    db.execute("INSERT INTO push_subscriptions (org_id, user_id, endpoint, p256dh, auth, created_at) VALUES (?,?,?,?,?,?)",
               (g.org["id"], g.user["id"], endpoint, p256dh, auth, H.now()))
    db.commit()
    return {"ok": True}


def notify_unsubscribe():
    H.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
              ((request.form.get("endpoint") or "")[:1000], g.user["id"]))
    return {"ok": True}


def notify_test():
    import cafe_push
    rows = [dict(r) for r in H.query("SELECT * FROM push_subscriptions WHERE user_id = ?", (g.user["id"],))]
    if not rows:
        return {"ok": False, "error": "Notifications are not on for this phone yet."}
    cafe_push._dispatch(rows, json.dumps({"title": "🔔 Test notification", "body": "New job applications will show like this.",
                                          "url": url_for("leads"), "tag": "notify-test"}, ensure_ascii=False))
    return {"ok": True}
