"""📊 Staff performance (CRM), for companies where the Super Admin switched it on (organizations.perf_allowed).

Records, only for those companies: time actively using the app (screen open and used in the last 5 minutes) per
person per day, and taps on 📞 Call / 🟢 WhatsApp and lead pages opened. The rest comes from data the CRM already
keeps (leads, follow-ups, lead history). Admins see everyone; each person sees their own report with tips.
"""
import csv
import io
import re
import threading
import time
from datetime import date, datetime, timedelta

from flask import Response, abort, g, jsonify, redirect, render_template, request, url_for

H = None
_last_ping = {}           # user id -> time of the last counted ping
_last_open = {}           # (user id, lead id) -> time the lead page was last counted
_lock = threading.Lock()
PING_SECONDS = 60
GOOD_NOTE = 20            # letters for a note to count as a proper note
WEIGHTS = {"followups": 25, "work": 20, "notes": 15, "won": 25, "time": 10, "pending": 5}


def init(host):
    global H
    H = host
    app = host.app
    app.add_url_rule("/perf/ping", "perf_ping", host.login_required(ping), methods=["POST"])
    app.add_url_rule("/perf/tap", "perf_tap", host.login_required(tap), methods=["POST"])
    app.add_url_rule("/performance", "performance", host.admin_required(team))
    app.add_url_rule("/performance.csv", "performance_csv", host.admin_required(team_csv))
    app.add_url_rule("/performance/<int:uid>", "performance_person", host.login_required(person))
    app.add_url_rule("/my-performance", "my_performance", host.login_required(mine))
    app.before_request(_count_lead_open)
    app.jinja_env.globals["perf_on"] = perf_on


def perf_on(org=None):
    org = org if org is not None else g.get("org")
    return bool(org is not None and "perf_allowed" in org.keys() and org["perf_allowed"])


def _tracking():
    """Record only real people of a company with the feature on (not the Super Admin viewing a company)."""
    return (g.get("user") is not None and perf_on() and not g.get("viewing") and not H.is_super()
            and g.org["kind"] != "agency")


# ---------------------------------------------------------------- recording

def ping():
    """Sent every minute by the page while it is visible and was used in the last 5 minutes."""
    if not _tracking():
        return jsonify(ok=True)
    uid = g.user["id"]
    now = time.time()
    with _lock:
        last = _last_ping.get(uid, 0)
        if now - last < 40:          # several tabs open: count the time once
            return jsonify(ok=True)
        _last_ping[uid] = now
    secs = PING_SECONDS if not last or now - last > PING_SECONDS else int(now - last)
    day = H.today()
    H.execute("""INSERT INTO staff_time (org_id, user_id, day, seconds) VALUES (?,?,?,?)
                 ON CONFLICT(user_id, day) DO UPDATE SET seconds = seconds + excluded.seconds""",
              (g.org["id"], uid, day, secs))
    return jsonify(ok=True)


def tap():
    """📞 Call or 🟢 WhatsApp tapped for a lead."""
    kind = request.form.get("kind")
    if kind not in ("call", "wa") or not _tracking():
        return jsonify(ok=True)
    lid = request.form.get("lead", type=int)
    if lid and not H.query("SELECT 1 FROM leads WHERE id = ? AND org_id = ?", (lid, g.org["id"]), one=True):
        lid = None
    H.execute("INSERT INTO staff_events (org_id, user_id, lead_id, kind, created_at) VALUES (?,?,?,?,?)",
              (g.org["id"], g.user["id"], lid, kind, H.now()))
    return jsonify(ok=True)


def _count_lead_open():
    if request.method != "GET" or request.endpoint != "lead_view" or not g.get("user"):
        return
    try:
        if not _tracking():
            return
    except Exception:
        return
    lid = (request.view_args or {}).get("lid")
    key = (g.user["id"], lid)
    now = time.time()
    with _lock:
        if now - _last_open.get(key, 0) < 600:
            return
        _last_open[key] = now
        if len(_last_open) > 5000:
            _last_open.clear()
    if H.query("SELECT 1 FROM leads WHERE id = ? AND org_id = ?", (lid, g.org["id"]), one=True):
        H.execute("INSERT INTO staff_events (org_id, user_id, lead_id, kind, created_at) VALUES (?,?,?,?,?)",
                  (g.org["id"], g.user["id"], lid, "open", H.now()))


# ---------------------------------------------------------------- periods

def period():
    """(from, to, label, key) from ?p=today|week|month|last_month|custom&from=&to= (India dates)."""
    t = datetime.now(H.IST).date()
    p = request.args.get("p", "month")
    if p == "today":
        a, b, label = t, t, "Today"
    elif p == "week":
        a, b, label = t - timedelta(days=t.weekday()), t, "This week"
    elif p == "last_month":
        b = t.replace(day=1) - timedelta(days=1)
        a, label = b.replace(day=1), "Last month"
    elif p == "custom":
        a = _d(request.args.get("from")) or t.replace(day=1)
        b = _d(request.args.get("to")) or t
        if b < a:
            a, b = b, a
        b = min(b, t)
        label = f"{a:%d %b} – {b:%d %b %Y}"
    else:
        p, a, b, label = "month", t.replace(day=1), t, "This month"
    return a, b, label, p


def _d(v):
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def previous(a, b):
    n = (b - a).days + 1
    return a - timedelta(days=n), a - timedelta(days=1)


def _bounds(a, b):
    return H.ist_day_bounds(a)[0], H.ist_day_bounds(b)[1]


def work_days(a, b):
    """Days in the period, Sundays left out (at least 1)."""
    n = sum(1 for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() != 6)
    return max(1, n)


# ---------------------------------------------------------------- the numbers

def people(oid):
    return H.query("""SELECT id, name, role, designation FROM users WHERE org_id = ? AND active = 1
                      AND role != 'super_admin' AND COALESCE(crm_off, 0) = 0 ORDER BY name COLLATE NOCASE""", (oid,))


def _note_ok(text, seen):
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) < GOOD_NOTE:
        return False
    words = re.findall(r"\w+", t.lower())
    if words and len(set(words)) / len(words) < 0.4:     # "ok ok ok ok …"
        return False
    key = t.lower()
    if key in seen:                                       # copied from an earlier note
        return False
    seen.add(key)
    return True


def stats(oid, a, b, only=None):
    """{user_id: {...numbers...}} for the period a..b (India dates)."""
    s0, s1 = _bounds(a, b)
    won, lost = H.stage_sql("won", oid), H.stage_sql("lost", oid)
    today = datetime.now(H.IST).date().isoformat()
    users = [u for u in people(oid) if only is None or u["id"] == only]
    out = {u["id"]: {"user": u, "added": 0, "fu_done": 0, "fu_ontime": 0, "fu_overdue": 0, "untouched": 0,
                     "updates": 0, "calls": 0, "wa": 0, "opens": 0, "won": 0, "lost": 0, "open_leads": 0, "hot": 0,
                     "seconds": 0, "days_active": 0, "notes_good": 0, "resp_hours": None, "resp_n": 0}
           for u in users}

    def put(rows, key, col="n"):
        for r in rows:
            if r["uid"] in out:
                out[r["uid"]][key] = r[col] or 0

    put(H.query("SELECT entered_by uid, COUNT(*) n FROM leads WHERE org_id = ? AND created_at >= ? AND created_at < ?"
                " GROUP BY entered_by", (oid, s0, s1)), "added")
    owner = "COALESCE(l.assigned_to, l.entered_by)"
    put(H.query(f"""SELECT {owner} uid, SUM(l.status IN ({won})) won, SUM(l.status IN ({lost})) lost FROM leads l
                    WHERE l.org_id = ? AND l.updated_at >= ? AND l.updated_at < ? GROUP BY uid""", (oid, s0, s1)),
        "won", "won")
    put(H.query(f"""SELECT {owner} uid, SUM(l.status IN ({lost})) n FROM leads l
                    WHERE l.org_id = ? AND l.updated_at >= ? AND l.updated_at < ? GROUP BY uid""", (oid, s0, s1)),
        "lost")
    put(H.query(f"""SELECT {owner} uid, COUNT(*) n, SUM(l.lead_quality = 'Hot') hot FROM leads l
                    WHERE l.org_id = ? AND l.status NOT IN ({won}) AND l.status NOT IN ({lost}) GROUP BY uid""",
                (oid,)), "open_leads")
    put(H.query(f"""SELECT {owner} uid, SUM(l.lead_quality = 'Hot') n FROM leads l
                    WHERE l.org_id = ? AND l.status NOT IN ({won}) AND l.status NOT IN ({lost}) GROUP BY uid""",
                (oid,)), "hot")
    # follow-ups done in the period, and on time (done on or before the due day, India time)
    rows = H.query("""SELECT completed_by uid, due_date, completed_at, outcome FROM follow_ups
                      WHERE org_id = ? AND status = 'Done' AND completed_at >= ? AND completed_at < ?
                      ORDER BY completed_at""", (oid, s0, s1))
    seen = {}
    for r in rows:
        o = out.get(r["uid"])
        if not o:
            continue
        o["fu_done"] += 1
        if H.ist_time(r["completed_at"], "%Y-%m-%d") <= (r["due_date"] or "9999"):
            o["fu_ontime"] += 1
        if _note_ok(r["outcome"], seen.setdefault(r["uid"], set())):
            o["notes_good"] += 1
    # waiting now: follow-ups past their day, and open leads nobody touched for 7 days
    put(H.query(f"""SELECT COALESCE(l.assigned_to, f.created_by) uid, COUNT(*) n FROM follow_ups f
                    JOIN leads l ON l.id = f.lead_id WHERE f.org_id = ? AND f.status = 'Pending' AND f.due_date < ?
                    GROUP BY uid""", (oid, today)), "fu_overdue")
    week_ago = H.ist_day_bounds(datetime.now(H.IST).date() - timedelta(days=7))[0]
    put(H.query(f"""SELECT {owner} uid, COUNT(*) n FROM leads l WHERE l.org_id = ? AND l.status NOT IN ({won})
                    AND l.status NOT IN ({lost}) AND COALESCE(l.updated_at, l.created_at) < ?
                    AND NOT EXISTS (SELECT 1 FROM lead_activity a WHERE a.lead_id = l.id AND a.created_at >= ?)
                    GROUP BY uid""", (oid, week_ago, week_ago)), "untouched")
    put(H.query("""SELECT a.user_id uid, COUNT(*) n FROM lead_activity a JOIN leads l ON l.id = a.lead_id
                   WHERE l.org_id = ? AND a.created_at >= ? AND a.created_at < ? GROUP BY a.user_id""",
                (oid, s0, s1)), "updates")
    for kind, key in (("call", "calls"), ("wa", "wa"), ("open", "opens")):
        put(H.query("""SELECT user_id uid, COUNT(*) n FROM staff_events WHERE org_id = ? AND kind = ?
                       AND created_at >= ? AND created_at < ? GROUP BY user_id""", (oid, kind, s0, s1)), key)
    for r in H.query("""SELECT user_id uid, SUM(seconds) secs, SUM(seconds >= 300) days FROM staff_time
                        WHERE org_id = ? AND day BETWEEN ? AND ? GROUP BY user_id""",
                     (oid, a.isoformat(), b.isoformat())):
        if r["uid"] in out:
            out[r["uid"]]["seconds"] = r["secs"] or 0
            out[r["uid"]]["days_active"] = r["days"] or 0
    # response time: leads given to someone else (assigned) → their first action on it
    for r in H.query("""SELECT l.assigned_to uid, l.created_at,
                          (SELECT MIN(a.created_at) FROM lead_activity a WHERE a.lead_id = l.id
                             AND a.user_id = l.assigned_to AND a.action != 'Lead created') first_act
                        FROM leads l WHERE l.org_id = ? AND l.assigned_to IS NOT NULL
                        AND COALESCE(l.entered_by, 0) != l.assigned_to AND l.created_at >= ? AND l.created_at < ?""",
                     (oid, s0, s1)):
        o = out.get(r["uid"])
        if not o or not r["first_act"]:
            continue
        try:
            h = (datetime.fromisoformat(r["first_act"][:19]) - datetime.fromisoformat(r["created_at"][:19])
                 ).total_seconds() / 3600
        except ValueError:
            continue
        o["resp_hours"] = ((o["resp_hours"] or 0) * o["resp_n"] + max(0, h)) / (o["resp_n"] + 1)
        o["resp_n"] += 1
    days = work_days(a, b)
    for o in out.values():
        score(o, days)
    return out


def score(o, days):
    """Marks out of 100 (see WEIGHTS) and the parts, kept in o."""
    w = WEIGHTS
    due = o["fu_done"] + o["fu_overdue"]
    p_fu = (o["fu_ontime"] / due) if due else 0.5
    actions = o["calls"] + o["wa"] + o["updates"] + o["fu_done"]
    p_work = min(1.0, actions / (days * 10))                       # about 10 actions a working day = full marks
    p_notes = (o["notes_good"] / o["fu_done"]) if o["fu_done"] else 0.5
    decided = o["won"] + o["lost"]
    p_won = (o["won"] / decided) if decided else 0.5
    p_time = min(1.0, (o["seconds"] / 60) / (days * 120))          # 2 hours a working day = full marks
    p_pend = max(0.0, 1 - (o["fu_overdue"] + o["untouched"]) / 10)
    parts = {"followups": p_fu, "work": p_work, "notes": p_notes, "won": p_won, "time": p_time, "pending": p_pend}
    o["parts"] = {k: round(v * w[k]) for k, v in parts.items()}
    o["score"] = sum(o["parts"].values())
    # nothing done at all in the period: no free marks for "nothing waiting"
    o["no_work"] = not (actions or o["seconds"] or o.get("added") or decided)
    if o["no_work"]:
        o["parts"] = {k: 0 for k in parts}
        o["score"] = 0
    o["ontime_pct"] = round(100 * o["fu_ontime"] / o["fu_done"]) if o["fu_done"] else None
    o["conv_pct"] = round(100 * o["won"] / decided) if decided else None
    o["notes_pct"] = round(100 * o["notes_good"] / o["fu_done"]) if o["fu_done"] else None
    o["hours"] = round(o["seconds"] / 3600, 1)
    o["actions"] = actions
    return o


def tips(o, prev=None):
    """Friendly suggestions for one person (shown to them and to the admin)."""
    out = []
    if o["fu_overdue"]:
        out.append(f"📅 {o['fu_overdue']} follow-up{'s are' if o['fu_overdue'] != 1 else ' is'} past the date — "
                   "start with the Hot leads, then the oldest.")
    if o["untouched"]:
        out.append(f"🕑 {o['untouched']} open lead{'s have' if o['untouched'] != 1 else ' has'} not been touched for a week. "
                   "A quick call or WhatsApp keeps them warm.")
    if o["resp_hours"] is not None and o["resp_hours"] > 2:
        out.append("⚡ New leads were first contacted after about "
                   f"{round(o['resp_hours'])} hours. Calling within 1 hour gives a much better chance to win.")
    if o["fu_done"] and o["notes_pct"] is not None and o["notes_pct"] < 60:
        out.append("📝 Write a short note after each follow-up: what the customer said and the next step "
                   "(e.g. 'Wants price for 2 units, call Friday').")
    if o["hot"] and o["won"] == 0:
        out.append(f"🔥 {o['hot']} Hot lead{'s are' if o['hot'] != 1 else ' is'} waiting — a visit or a quotation can close them.")
    if o["actions"] and o["calls"] + o["wa"] == 0:
        out.append("📞 Use the Call / WhatsApp buttons in the app, so your calls are counted.")
    if prev is not None:
        if o["won"] > prev["won"]:
            out.append(f"👏 {o['won']} won this period — {o['won'] - prev['won']} more than the period before. Great work!")
        if o["score"] >= prev["score"] + 5:
            out.append(f"📈 Your score went up from {prev['score']} to {o['score']}. Keep going!")
    if not out:
        out.append("🌟 All follow-ups are on time and leads are moving. Keep it up!")
    return out


def warnings(rows):
    out = []
    for o in rows:
        n = o["user"]["name"]
        if o["fu_overdue"] >= 5:
            out.append(f"⚠️ {n}: {o['fu_overdue']} follow-ups past the date")
        if o["untouched"] >= 5:
            out.append(f"⚠️ {n}: {o['untouched']} open leads not touched for 7 days")
        if o["resp_hours"] is not None and o["resp_hours"] > 24:
            out.append(f"⚠️ {n}: new leads first contacted after ~{round(o['resp_hours'])} hours")
    return out


# ---------------------------------------------------------------- pages

def _need_on():
    if not perf_on():
        abort(404)


def team():
    _need_on()
    a, b, label, p = period()
    pa, pb = previous(a, b)
    cur = stats(g.org["id"], a, b)
    prev = stats(g.org["id"], pa, pb)
    rows = sorted(cur.values(), key=lambda o: -o["score"])
    for o in rows:
        pv = prev.get(o["user"]["id"])
        o["prev_score"] = pv["score"] if pv and not pv["no_work"] and not o["no_work"] else None
    return render_template("performance.html", rows=rows, label=label, p=p, a=a, b=b, warn=warnings(rows),
                           WEIGHTS=WEIGHTS)


def team_csv():
    _need_on()
    a, b, label, p = period()
    rows = sorted(stats(g.org["id"], a, b).values(), key=lambda o: -o["score"])
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["Name", "Score", "Leads added", "Follow-ups done", "On time %", "Past the date", "Not touched 7 days",
                 "Calls", "WhatsApp", "Lead updates", "Won", "Lost", "Win %", "Proper notes %", "Hours in app",
                 "Active days", "First contact (hours)"])
    for o in rows:
        wr.writerow([o["user"]["name"], o["score"], o["added"], o["fu_done"], o["ontime_pct"] if o["ontime_pct"] is not None else "",
                     o["fu_overdue"], o["untouched"], o["calls"], o["wa"], o["updates"], o["won"], o["lost"],
                     o["conv_pct"] if o["conv_pct"] is not None else "", o["notes_pct"] if o["notes_pct"] is not None else "",
                     o["hours"], o["days_active"], round(o["resp_hours"], 1) if o["resp_hours"] is not None else ""])
    H.audit("Downloaded staff performance", label)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="staff-performance-{a}-{b}.csv"'})


def person(uid):
    _need_on()
    if uid != g.user["id"] and not H.is_admin():
        abort(403)
    a, b, label, p = period()
    pa, pb = previous(a, b)
    cur = stats(g.org["id"], a, b, only=uid)
    if uid not in cur:
        abort(404)
    o = cur[uid]
    prev = stats(g.org["id"], pa, pb, only=uid).get(uid)
    daily = {r["day"]: r["seconds"] for r in H.query(
        "SELECT day, seconds FROM staff_time WHERE org_id = ? AND user_id = ? AND day BETWEEN ? AND ?",
        (g.org["id"], uid, a.isoformat(), b.isoformat()))}
    days = [(a + timedelta(days=i)) for i in range((b - a).days + 1)][-31:]
    chart = [{"d": d, "min": round(daily.get(d.isoformat(), 0) / 60)} for d in days]
    top = max([c["min"] for c in chart] + [1])
    today = datetime.now(H.IST).date().isoformat()
    overdue = H.query("""SELECT f.due_date, l.id, l.customer_name, l.lead_quality FROM follow_ups f JOIN leads l ON l.id = f.lead_id
                         WHERE f.org_id = ? AND f.status = 'Pending' AND f.due_date < ?
                         AND COALESCE(l.assigned_to, f.created_by) = ? ORDER BY f.due_date LIMIT 20""",
                      (g.org["id"], today, uid))
    return render_template("performance_person.html", o=o, prev=prev, tips=tips(o, prev), label=label, p=p, a=a, b=b,
                           chart=chart, top=top, overdue=overdue, WEIGHTS=WEIGHTS, own=uid == g.user["id"])


def mine():
    return redirect(url_for("performance_person", uid=g.user["id"], **request.args))
