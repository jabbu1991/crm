"""Website Careers form → agency CRM page 🧑‍💼 Job Applications (not leads), with a phone notification to the admins.

The website posts here from the careers page (only from CRM_ENQUIRY_ORIGINS, like the contact form). No login,
no form token: the organization, campaign and creator are fixed here, never taken from the form.
Admins turn on phone notifications on My Account (Web Push, the same helper as the cafe and family alerts).
"""
import io
import json
import re
import time

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for

H = None
_hits = {}
STATUSES = ["New", "Shortlisted", "Interview", "Selected", "Rejected"]
# Tapping an applicant's WhatsApp number opens WhatsApp with this message ({name}, {role}, {org_name}).
DEFAULT_MESSAGE = ("Hi {name},\n\nThank you for applying to {org_name} for the {role} role. We have received your "
                   "application. Our team will review it and contact you soon.\n\nRegards,\n{org_name}")


def init(host):
    global H
    H = host
    app = host.app
    r = app.add_url_rule
    r("/job-application", "website_job", website_job, methods=["POST", "OPTIONS"])
    r("/job-applications", "job_apps", host.admin_required(page))
    r("/job-applications/<int:aid>", "job_app_update", host.admin_required(update), methods=["POST"])
    r("/job-applications.xlsx", "job_apps_xlsx", host.admin_required(export))
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

    db = H.get_db()
    org = db.execute("SELECT * FROM organizations WHERE kind = 'agency' ORDER BY id LIMIT 1").fetchone()
    if not org:
        return {"ok": False, "error": "not_set_up"}, 503, headers
    oid, ts, key = org["id"], H.now(), H.phone_key(phone)
    before = db.execute("SELECT 1 FROM job_applications WHERE org_id = ? AND phone_norm = ?", (oid, key)).fetchone()
    aid = db.execute(
        "INSERT INTO job_applications (org_id, name, whatsapp, phone_norm, place, role, job_type, work_location,"
        " experience, link, about, applied_before, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, name, phone, key, place, role, pick("job_type"), pick("location"), _clean("experience", 30), link,
         _clean("about", 600), 1 if before else 0, ts, ts)).lastrowid
    db.commit()
    notify_admins(oid, f"🧑‍💼 New job application: {name}", f"{role} · {place} — tap to open",
                  f"/job-applications#app{aid}", f"job-{aid}")
    return {"ok": True}, 201, headers


# ---------------------------------------------------------------- the Job Applications page (agency admins)

def _agency_only():
    if g.org["kind"] != "agency":
        abort(404)


def page():
    _agency_only()
    if not g.get("viewing"):
        _move_old_leads()
    oid = g.org["id"]
    a = request.args
    q, role, status = (a.get("q") or "").strip(), (a.get("role") or "").strip(), a.get("status") or ""
    where, args = ["org_id = ?"], [oid]
    if q:
        where.append("(name LIKE ? OR whatsapp LIKE ? OR place LIKE ? OR role LIKE ? OR about LIKE ?)")
        args += [f"%{q}%"] * 5
    if role:
        where.append("role = ? COLLATE NOCASE")
        args.append(role)
    if status in STATUSES:
        where.append("status = ?")
        args.append(status)
    rows = H.query(f"SELECT * FROM job_applications WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC "
                   "LIMIT 500", args)
    roles = [r[0] for r in H.query("SELECT DISTINCT role FROM job_applications WHERE org_id = ? AND role != '' "
                                   "ORDER BY role COLLATE NOCASE", (oid,))]
    counts = {r["status"]: r["n"] for r in H.query("SELECT status, COUNT(*) n FROM job_applications WHERE org_id = ? "
                                                    "GROUP BY status", (oid,))}
    return render_template("job_applications.html", rows=rows, roles=roles, counts=counts, STATUSES=STATUSES,
                           q=q, role=role, status=status, message=DEFAULT_MESSAGE)


def update(aid):
    _agency_only()
    row = H.query("SELECT * FROM job_applications WHERE id = ? AND org_id = ?", (aid, g.org["id"]), one=True) or abort(404)
    f = request.form
    status = f.get("status") if f.get("status") in STATUSES else row["status"]
    note = H.clean(f.get("note"))[:2000] if "note" in f else row["note"]
    H.execute("UPDATE job_applications SET status = ?, note = ?, updated_at = ?, updated_by = ? WHERE id = ?",
              (status, note, H.now(), g.user["id"], aid))
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True}
    flash("Saved.", "success")
    return redirect(url_for("job_apps") + f"#app{aid}")


def export():
    _agency_only()
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Job applications"
    ws.append(["Applied", "Name", "WhatsApp", "Place", "Role", "Job type", "Work location", "Experience",
               "Portfolio / Resume / Instagram", "About", "Status", "Note", "Applied before"])
    for r in H.query("SELECT * FROM job_applications WHERE org_id = ? ORDER BY created_at DESC, id DESC", (g.org["id"],)):
        ws.append([H.ist_time(r["created_at"], "%d-%m-%Y %H:%M"), r["name"], r["whatsapp"], r["place"], r["role"],
                   r["job_type"], r["work_location"], r["experience"], r["link"], r["about"], r["status"], r["note"],
                   "Yes" if r["applied_before"] else ""])
    for c in ws[1]:
        c.font = Font(bold=True)
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(40, max(10, max(len(str(c.value or "")) for c in col) + 2))
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name="job-applications.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


NOTE_KEYS = {"Role": "role", "Job type": "job_type", "Work location": "work_location", "Experience": "experience",
             "Portfolio / Resume / Instagram": "link", "About": "about"}


def _move_old_leads():
    """Applications saved as leads (campaign JOBS, before this page existed) move here; the leads go to the
    Recycle Bin (restorable for 30 days), so nothing is lost."""
    db = H.get_db()
    oid = g.org["id"]
    old = H.query(f"SELECT {H.LEAD_COLUMNS} {H.LEAD_FROM} WHERE l.org_id = ? AND p.code = 'JOBS'", (oid,))
    if not old:
        return 0
    ts = H.now()
    for lead in old:
        if db.execute("SELECT 1 FROM job_applications WHERE from_lead = ?", (lead["id"],)).fetchone():
            continue
        vals = {v: "" for v in NOTE_KEYS.values()}
        for line in (lead["notes"] or "").splitlines():
            k, _, v = line.partition(": ")
            if k in NOTE_KEYS:
                vals[NOTE_KEYS[k]] = v.strip()
        extra = [h for (h,) in db.execute("SELECT action FROM lead_activity WHERE lead_id = ? AND action LIKE "
                                          "'Applied again%' ORDER BY id", (lead["id"],))]
        db.execute(
            "INSERT INTO job_applications (org_id, name, whatsapp, phone_norm, place, role, job_type, work_location,"
            " experience, link, about, note, from_lead, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, lead["customer_name"], lead["whatsapp"] or lead["phone"], lead["phone_norm"], lead["place"],
             vals["role"] or lead["requirement"] or "", vals["job_type"], vals["work_location"], vals["experience"],
             vals["link"], vals["about"], "\n".join(extra), lead["id"], lead["created_at"], ts))
        H.move_to_recycle_bin(db, lead, ts)
    db.commit()
    flash(f"{len(old)} earlier application(s) were moved here from Leads. The old lead copies are in the Recycle Bin.",
          "success")
    return len(old)


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
