"""Phone push notifications for new online orders (Web Push, free, no outside service account needed).

Each staff phone that allows notifications stores a subscription here. When a customer places an online order, every
subscribed phone of that cafe whose user may handle online orders gets "New online order #12", even with the screen
off or Chrome closed (Android). The keys (VAPID) are made once and kept next to the database on the disk.
"""
import base64
import json
import os
import sqlite3
import threading

from flask import Response, g, jsonify, request, send_from_directory, url_for

from cafe import bp, cafe_view, can, cafe_name, get_settings, host as H, user_perms

_lock = threading.Lock()
_keys = {}
sender = None   # tests replace this with a fake


def _key_path():
    return os.path.join(os.path.dirname(os.path.abspath(H.DB_PATH)), "vapid_private.pem")


def vapid():
    """(private key PEM path, public key as base64url for the browser). Made on first use."""
    path = _key_path()
    with _lock:
        if _keys.get("path") == path:
            return path, _keys["public"]
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        if os.path.exists(path):
            with open(path, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        _keys.update(path=path, public=base64.urlsafe_b64encode(raw).rstrip(b"=").decode())
        return path, _keys["public"]


def _webpush(sub, data, key_path):
    from pywebpush import webpush
    webpush(subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=data, vapid_private_key=key_path, vapid_claims={"sub": "mailto:support@realviewcreativestudio.in"},
            ttl=600, timeout=10)


def _send_all(subs, data, key_path, db_path):
    dead, ok = [], []
    send = sender or _webpush
    for s in subs:
        try:
            send(s, data, key_path)
            ok.append(s["id"])
        except Exception as e:  # noqa: BLE001  (one bad phone must not stop the others)
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):   # the phone unsubscribed / reinstalled the browser
                dead.append(s["id"])
    if dead or ok:
        try:
            conn = sqlite3.connect(db_path, timeout=10)
            with conn:
                for i in dead:
                    conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (i,))
                for i in ok:
                    conn.execute("UPDATE push_subscriptions SET last_ok_at = datetime('now') WHERE id = ?", (i,))
            conn.close()
        except sqlite3.Error:
            pass


def new_order(org, order_id):
    """Tell the cafe's phones about a new online order (in the background, so the customer is not kept waiting)."""
    rows = H.query("""SELECT s.*, u.role, u.cafe_role, u.cafe_perms FROM push_subscriptions s
                      JOIN users u ON u.id = s.user_id WHERE s.org_id = ? AND u.active = 1""", (org["id"],))
    subs = [dict(r) for r in rows if "orders_online" in user_perms(r)]
    if not subs:
        return 0
    o = H.query("""SELECT o.order_no, o.order_type, o.cust_name, t.name table_name,
                     (SELECT COALESCE(SUM(price * qty), 0) FROM cafe_order_items i WHERE i.order_id = o.id) amount
                   FROM cafe_orders o LEFT JOIN cafe_tables t ON t.id = o.table_id WHERE o.id = ?""", (order_id,), one=True)
    kind = {"dine_in": f"Table {o['table_name'] or ''}".strip(), "takeaway": "Takeaway", "delivery": "Delivery"}
    st = get_settings(org["id"])
    data = json.dumps({
        "title": f"🔔 New online order #{o['order_no']}",
        "body": f"{kind.get(o['order_type'], '')} · {o['cust_name'] or ''} · ₹{o['amount'] // 100}".strip(" ·"),
        "url": url_for("cafe.orders"), "tag": f"order-{org['id']}", "cafe": st["name"] or org["name"],
    }, ensure_ascii=False)
    key_path, _ = vapid()
    args = (subs, data, key_path, H.DB_PATH)
    if sender is not None:      # tests: run now
        _send_all(*args)
    else:
        threading.Thread(target=_send_all, args=args, daemon=True).start()
    return len(subs)


# ---------------------------------------------------------------- routes

@bp.route("/sw.js")
def service_worker():
    """The phone's background helper that shows the notifications (must be served from the site root)."""
    resp = send_from_directory(H.app.static_folder, "sw.js", mimetype="text/javascript", max_age=0)
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/cafe/push/key")
@cafe_view()
def push_key():
    if not can("orders_online"):
        return jsonify(ok=False), 403
    return jsonify(ok=True, key=vapid()[1])


@bp.route("/cafe/push/subscribe", methods=["POST"])
@cafe_view()
def push_subscribe():
    if not can("orders_online"):
        return jsonify(ok=False, error="Online orders are not part of your work."), 403
    try:
        sub = json.loads(request.form.get("payload") or "{}")
        endpoint = str(sub["endpoint"])[:1000]
        keys = sub.get("keys") or {}
        p256dh, auth = str(keys["p256dh"])[:200], str(keys["auth"])[:100]
        assert endpoint.startswith("https://")
    except (ValueError, KeyError, TypeError, AssertionError):
        return jsonify(ok=False, error="This browser cannot get notifications."), 400
    db = H.get_db()
    db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    db.execute("""INSERT INTO push_subscriptions (org_id, user_id, endpoint, p256dh, auth, created_at)
                  VALUES (?,?,?,?,?,?)""", (g.org["id"], g.user["id"], endpoint, p256dh, auth, H.now()))
    db.commit()
    return jsonify(ok=True)


@bp.route("/cafe/push/unsubscribe", methods=["POST"])
@cafe_view()
def push_unsubscribe():
    endpoint = (request.form.get("endpoint") or "")[:1000]
    H.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?", (endpoint, g.user["id"]))
    return jsonify(ok=True)


@bp.route("/cafe/push/test", methods=["POST"])
@cafe_view()
def push_test():
    """'Send a test' button: only to this user's own phones."""
    rows = [dict(r) for r in H.query("SELECT * FROM push_subscriptions WHERE user_id = ?", (g.user["id"],))]
    if not rows:
        return jsonify(ok=False, error="Notifications are not on for this phone yet.")
    data = json.dumps({"title": "🔔 Test from " + cafe_name(), "body": "Order notifications work on this phone.",
                       "url": url_for("cafe.orders"), "tag": "test"}, ensure_ascii=False)
    key_path, _ = vapid()
    args = (rows, data, key_path, H.DB_PATH)
    if sender is not None:
        _send_all(*args)
    else:
        threading.Thread(target=_send_all, args=args, daemon=True).start()
    return jsonify(ok=True)
