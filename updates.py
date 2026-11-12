"""🆕 What's new: notes about new features for each company's people, and 🙋 feature requests to the Super Admin.

Notes come from updates_list.SHIPPED (stored once per server) or are typed by the Super Admin. Each person sees the
notes for the modules they use (admins-only notes only for admins); a new person does not get old notes.
"""
from datetime import datetime, timedelta

from flask import abort, flash, g, redirect, render_template, request, url_for

import updates_list

H = None
_synced = {"db": None}   # the database the shipped notes were stored in
MODULE_LABELS = {"all": "Everyone", "crm": "CRM", "cafe": "Cafe", "prod": "Order Tracker", "fam": "Family"}


def init(host):
    global H
    H = host
    app = host.app
    app.add_url_rule("/whats-new", "whats_new", host.login_required(page))
    app.add_url_rule("/whats-new/<int:uid_>/seen", "whats_new_seen", host.login_required(seen), methods=["POST"])
    app.add_url_rule("/whats-new/request/<feature>", "feature_request", host.admin_required(request_feature),
                     methods=["POST"])
    app.add_url_rule("/admin/updates", "admin_updates", host.super_required(admin_page), methods=["GET", "POST"])
    app.add_url_rule("/admin/requests/<int:rid>/<action>", "admin_request_action",
                     host.super_required(admin_request_action), methods=["POST"])
    app.before_request(_sync_once)
    app.jinja_env.globals["whats_new"] = whats_new
    app.jinja_env.globals["open_requests"] = open_requests
    app.jinja_env.globals["UPDATE_MODULES"] = MODULE_LABELS


def _sync_once():
    if _synced["db"] == H.DB_PATH:
        return
    try:
        sync_shipped()
        _synced["db"] = H.DB_PATH
    except Exception:
        pass


def sync_shipped():
    db = H.get_db()
    for n in updates_list.SHIPPED:
        db.execute("""INSERT OR IGNORE INTO app_updates (code_key, module, audience, title, body, feature, created_at)
                      VALUES (?,?,?,?,?,?,?)""", (n["key"], n.get("module", "all"), n.get("audience", "all"),
                                                n["title"], n.get("body", ""), n.get("feature"),
                                                n["date"] + " 09:00:00"))
    db.commit()


def _visible_sql(user):
    """WHERE part + args: notes this person should see."""
    mods = ["all"] + list(H.user_modules(user))
    where = [f"u.module IN ({','.join('?' * len(mods))})", "(u.org_id IS NULL OR u.org_id = ?)"]
    args = mods + [user["org_id"]]
    if not (H.is_admin() or H.is_super()):
        where.append("u.audience = 'all'")
    since = (user["created_at"] or "")[:10]
    if since:   # a new person does not get notes from long before they joined
        try:
            since = (datetime.fromisoformat(since) - timedelta(days=14)).strftime("%Y-%m-%d")
            where.append("u.created_at >= ?")
            args.append(since)
        except ValueError:
            pass
    return " AND ".join(where), args


def visible(user, limit=40):
    where, args = _visible_sql(user)
    return H.query(f"""SELECT u.*, r.read_at FROM app_updates u
                       LEFT JOIN app_update_reads r ON r.update_id = u.id AND r.user_id = ?
                       WHERE {where} ORDER BY u.created_at DESC, u.id DESC LIMIT ?""", (user["id"], *args, limit))


def whats_new():
    """For the header: (unread count, newest unread note or None). Cached per request."""
    if g.get("_wn") is not None:
        return g._wn
    user = g.get("user")
    out = (0, None)
    if user and not g.get("viewing") and not user["must_change_password"]:
        try:
            where, args = _visible_sql(user)
            rows = H.query(f"""SELECT u.* FROM app_updates u WHERE {where} AND NOT EXISTS
                               (SELECT 1 FROM app_update_reads r WHERE r.update_id = u.id AND r.user_id = ?)
                               ORDER BY u.created_at DESC, u.id DESC""", (*args, user["id"]))
            out = (len(rows), rows[0] if rows else None)
        except Exception:
            out = (0, None)
    g._wn = out
    return out


def has_feature(org, feature):
    if feature == "capture":
        import capture
        return capture.capture_on(org=org)
    col = updates_list.FEATURES.get(feature, {}).get("column")
    return bool(col and org is not None and col in org.keys() and org[col])


def request_state(feature):
    """'on' (company has it), 'requested' (waiting for the Super Admin) or '' (can request)."""
    org = g.get("org")
    if has_feature(org, feature):
        return "on"
    if H.query("SELECT 1 FROM feature_requests WHERE org_id = ? AND feature = ? AND status = 'open'",
               (org["id"], feature), one=True):
        return "requested"
    return ""


def page():
    rows = visible(g.user)
    states = {f: request_state(f) for f in updates_list.FEATURES}
    if not g.get("viewing"):
        db = H.get_db()
        ts = H.now()
        db.executemany("INSERT OR IGNORE INTO app_update_reads (user_id, update_id, read_at) VALUES (?,?,?)",
                       [(g.user["id"], r["id"], ts) for r in rows if not r["read_at"]])
        db.commit()
    return render_template("whats_new.html", rows=rows, states=states, FEATURES=updates_list.FEATURES,
                           MODULE_LABELS=MODULE_LABELS)


def seen(uid_):
    if not g.get("viewing"):
        db = H.get_db()
        db.execute("INSERT OR IGNORE INTO app_update_reads (user_id, update_id, read_at) VALUES (?,?,?)",
                   (g.user["id"], uid_, H.now()))
        db.commit()
    nxt = request.form.get("next") or ""
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("whats_new"))


def request_feature(feature):
    if feature not in updates_list.FEATURES or g.get("viewing"):
        abort(404)
    state = request_state(feature)
    label = updates_list.FEATURES[feature]["label"]
    if state == "on":
        flash(f"{label} is already on for your company.", "success")
    elif state == "requested":
        flash(f"{label}: your request is already with us.", "success")
    else:
        H.execute("INSERT INTO feature_requests (org_id, feature, requested_by, created_at) VALUES (?,?,?,?)",
                  (g.org["id"], feature, g.user["id"], H.now()))
        H.audit("Feature requested", label)
        flash(f"Thank you! We got your request for {label}. You will see a note here when it is on.", "success")
    return redirect(url_for("whats_new"))


def open_requests():
    """Super Admin: open feature requests (for the nav badge and the Updates page)."""
    if not H.is_super():
        return []
    if g.get("_open_req") is None:
        g._open_req = H.query("""SELECT r.*, o.name org_name, u.name user_name FROM feature_requests r
                                 JOIN organizations o ON o.id = r.org_id LEFT JOIN users u ON u.id = r.requested_by
                                 WHERE r.status = 'open' ORDER BY r.id""")
    return g._open_req


def enable_feature(oid, feature):
    """Switch a feature on for a company: close its requests and tell its people."""
    f = updates_list.FEATURES[feature]
    db = H.get_db()
    db.execute(f"UPDATE organizations SET {f['column']} = 1 WHERE id = ?", (oid,))
    db.execute("UPDATE feature_requests SET status = 'done', handled_at = ? WHERE org_id = ? AND feature = ? "
               "AND status = 'open'", (H.now(), oid, feature))
    if feature == "perf":
        db.execute("""INSERT INTO app_updates (org_id, module, audience, title, body, created_by, created_at)
                      VALUES (?, 'crm', 'all', ?, ?, ?, ?)""",
                   (oid, "✅ Staff performance is on",
                    "From today the app records each person's working time in the app (only while it is open and "
                    "used) and their work on leads — follow-ups, calls and WhatsApp taps, notes and results — for "
                    "the performance report. Admins see everyone; you see your own report under "
                    "📊 My performance, with tips.", g.user["id"], H.now()))
    db.commit()


def admin_page():
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:120]
        body = (request.form.get("body") or "").strip()[:2000]
        module = request.form.get("module") if request.form.get("module") in MODULE_LABELS else "all"
        audience = "admins" if request.form.get("audience") == "admins" else "all"
        oid = request.form.get("org_id", type=int)
        if not title:
            flash("Write a title.", "error")
        else:
            H.execute("""INSERT INTO app_updates (org_id, module, audience, title, body, created_by, created_at)
                         VALUES (?,?,?,?,?,?,?)""", (oid or None, module, audience, title, body, g.user["id"], H.now()))
            flash("Posted. People see it the next time they open the app.", "success")
            return redirect(url_for("admin_updates"))
    rows = H.query("""SELECT u.*, o.name org_name, (SELECT COUNT(*) FROM app_update_reads r WHERE r.update_id = u.id) reads
                      FROM app_updates u LEFT JOIN organizations o ON o.id = u.org_id
                      ORDER BY u.created_at DESC, u.id DESC LIMIT 60""")
    orgs = H.query("SELECT id, name FROM organizations WHERE kind != 'agency' ORDER BY name")
    return render_template("admin_updates.html", rows=rows, orgs=orgs, reqs=open_requests(),
                           FEATURES=updates_list.FEATURES, MODULE_LABELS=MODULE_LABELS)


def admin_request_action(rid, action):
    r = H.query("SELECT * FROM feature_requests WHERE id = ?", (rid,), one=True) or abort(404)
    if action == "on" and r["feature"] == "capture":
        return redirect(url_for("admin_capture") + f"#org{r['org_id']}")   # choose which ways to switch on
    if action == "on":
        enable_feature(r["org_id"], r["feature"])
        flash("Switched on and the company was told.", "success")
    elif action == "decline":
        H.execute("UPDATE feature_requests SET status = 'declined', handled_at = ? WHERE id = ?", (H.now(), rid))
        flash("Request closed.", "success")
    else:
        abort(404)
    return redirect(url_for("admin_updates"))
