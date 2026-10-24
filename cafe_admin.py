"""Cafe settings, menu design, QR codes, menu items, offer banners and tables."""
import io
import json
import re

from flask import Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

from cafe import (BANNER_PHOTO_HINT, DEFAULT_SETTINGS, FONTS, FOOD_TYPES, GST_RATES, ITEM_PHOTO_HINT, ITEM_TAGS,
                  LAYOUTS, PAPER_SIZES, SHAPES, THEMES, bp, cafe_name, cafe_view, get_category, get_item,
                  get_settings, host as H, menu_url, paise_str, save_photo, save_settings, to_float, to_int, to_paise,
                  valid_birthday)


def clean(v, n=200):
    return (v or "").strip()[:n]


def checked(name):
    return bool(request.form.get(name))


# ---------------------------------------------------------------- settings

@bp.route("/cafe/settings", methods=["GET", "POST"])
@cafe_view("settings_edit")
def settings():
    st = json.loads(json.dumps(get_settings()))  # deep copy
    if request.method == "POST":
        f = request.form
        for k, n in (("name", 80), ("tagline", 120), ("address", 300), ("phone", 20), ("whatsapp", 20),
                     ("about", 600), ("bill_footer", 200), ("bill_prefix", 8), ("gstin", 20), ("delivery_note", 200),
                     ("fssai", 20)):
            st[k] = clean(f.get(k), n)
        st["map_url"] = H.web_link(f.get("map_url"))
        st["paper"] = f.get("paper") if f.get("paper") in PAPER_SIZES else "80"
        for k in ("kot_on", "kot_with_bill", "gst_on", "gst_inclusive", "menu_on", "online_on", "order_dine_in",
                  "order_takeaway", "order_delivery", "birthday_on"):
            st[k] = checked(k)
        st["default_gst"] = to_float(f.get("default_gst"), 5, 0, 28)
        lo = st["loyalty"]
        lo["on"] = checked("loyalty_on")
        lo["mode"] = "per_amount" if f.get("loyalty_mode") == "per_amount" else "percent"
        lo["percent"] = to_float(f.get("loyalty_percent"), 5, 0, 100)
        lo["per_amount"] = to_int(f.get("loyalty_per_amount"), 100, 1, 100000)
        lo["per_points"] = to_int(f.get("loyalty_per_points"), 1, 0, 100000)
        pv = to_paise(f.get("loyalty_point_value"), 100)
        lo["point_value"] = pv if pv and pv > 0 else 100
        lo["min_bill"] = to_int(f.get("loyalty_min_bill"), 0, 0, 1000000)
        lo["min_redeem"] = to_int(f.get("loyalty_min_redeem"), 0, 0, 1000000)
        lo["max_redeem_pct"] = to_int(f.get("loyalty_max_redeem_pct"), 50, 0, 100)
        lo["expiry_days"] = to_int(f.get("loyalty_expiry_days"), 0, 0, 3650)
        rf = st["referral"]
        rf["on"] = checked("referral_on")
        rf["referrer_points"] = to_int(f.get("referral_referrer_points"), 0, 0, 100000)
        rf["new_points"] = to_int(f.get("referral_new_points"), 0, 0, 100000)
        if st["online_on"] and not (st["order_dine_in"] or st["order_takeaway"] or st["order_delivery"]):
            st["order_takeaway"] = True
        for field, key, box, fit in (("logo", "logo", (400, 400), False), ("cover", "cover", (1200, 500), True)):
            name = save_photo(field, box, fit)
            if name:
                H.remove_media(st[key])
                st[key] = name
            elif checked("remove_" + key) and st[key]:
                H.remove_media(st[key])
                st[key] = ""
        save_settings(st)
        flash("Settings save ചെയ്തു.", "success")
        return redirect(url_for("cafe.settings") + (("#" + f.get("section")) if f.get("section") else ""))
    return render_template("cafe/settings.html", st=st, PAPER_SIZES=PAPER_SIZES, GST_RATES=GST_RATES,
                           menu_link=menu_url(), paise_str=paise_str)


# ---------------------------------------------------------------- menu design

@bp.route("/cafe/design", methods=["GET", "POST"])
@cafe_view("settings_edit")
def design():
    st = json.loads(json.dumps(get_settings()))
    if request.method == "POST":
        t = st["theme"]
        preset = request.form.get("preset")
        t["preset"] = preset if preset in THEMES else "custom"
        for k in ("primary", "secondary", "bg", "text"):
            v = (request.form.get(k) or "").strip()
            if re.match(r"^#[0-9a-fA-F]{6}$", v):
                t[k] = v.lower()
        t["layout"] = request.form.get("layout") if request.form.get("layout") in LAYOUTS else "grid"
        t["font"] = request.form.get("font") if request.form.get("font") in FONTS else "modern"
        t["shape"] = request.form.get("shape") if request.form.get("shape") in SHAPES else "round"
        save_settings(st)
        flash("Menu design save ചെയ്തു. Customers-ന് ഇപ്പോൾ ഈ design കാണും.", "success")
        return redirect(url_for("cafe.design"))
    return render_template("cafe/design.html", st=st, THEMES=THEMES, FONTS=FONTS, LAYOUTS=LAYOUTS, SHAPES=SHAPES,
                           preview_url=url_for("cafe.public_menu", code=g.org["code"].lower(), preview=1))


@bp.route("/cafe/design/logo-colors")
@cafe_view("settings_edit")
def logo_colors():
    """Two strong colours from the logo, for the 'pick colours from the logo' button."""
    st = get_settings()
    if not st["logo"]:
        return jsonify(ok=False, error="ആദ്യം Settings-ൽ logo upload ചെയ്യുക.")
    from PIL import Image
    import os
    try:
        img = Image.open(os.path.join(H.media_dir(), st["logo"])).convert("RGB")
    except Exception:
        return jsonify(ok=False, error="Logo വായിക്കാൻ പറ്റിയില്ല.")
    img.thumbnail((120, 120))
    pal = img.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette = pal.getpalette()[:24]
    counts = sorted(pal.getcolors(), reverse=True)
    picks = []
    for count, idx in counts:
        r, gr, b = palette[idx * 3: idx * 3 + 3]
        mx, mn = max(r, gr, b), min(r, gr, b)
        sat = (mx - mn) / mx if mx else 0
        light = (mx + mn) / 510
        if light > 0.93 or light < 0.06 or sat < 0.18:  # skip white / black / grey backgrounds
            continue
        hexv = "#%02x%02x%02x" % (r, gr, b)
        if all(sum(abs(int(hexv[i:i + 2], 16) - int(p[i:i + 2], 16)) for i in (1, 3, 5)) > 90 for p in picks):
            picks.append(hexv)
        if len(picks) == 2:
            break
    if not picks:
        return jsonify(ok=False, error="Logo-യിൽ തെളിഞ്ഞ നിറങ്ങൾ കണ്ടില്ല. നിറം കൈകൊണ്ട് തിരഞ്ഞെടുക്കാം.")
    return jsonify(ok=True, primary=picks[0], secondary=picks[1] if len(picks) > 1 else picks[0])


# ---------------------------------------------------------------- QR codes

def _qr(url):
    import segno
    return segno.make(url, error="m")


@bp.route("/cafe/qr.<kind>")
@cafe_view()
def qr_image(kind):
    table = request.args.get("t", type=int)
    url = menu_url(t=table) if table else menu_url()
    q = _qr(url)
    buf = io.BytesIO()
    color = get_settings()["theme"].get("text") if request.args.get("brand") else "#000000"
    if kind == "svg":
        q.save(buf, kind="svg", scale=10, border=2, dark=color or "#000000", xmldecl=False)
        return Response(buf.getvalue(), mimetype="image/svg+xml")
    if kind == "png":
        q.save(buf, kind="png", scale=16, border=3)
        name = re.sub(r"[^A-Za-z0-9]+", "-", cafe_name()).strip("-") or "menu"
        return Response(buf.getvalue(), mimetype="image/png",
                        headers={"Content-Disposition": f'attachment; filename="{name}-menu-qr.png"'})
    abort(404)


@bp.route("/cafe/qr-poster")
@cafe_view()
def qr_poster():
    st = get_settings()
    tables = []
    if request.args.get("tables"):
        tables = H.query("SELECT * FROM cafe_tables WHERE org_id = ? AND active = 1 ORDER BY sort, id", (H.org_id(),))
    one = request.args.get("t", type=int)
    if one:
        tables = H.query("SELECT * FROM cafe_tables WHERE org_id = ? AND id = ?", (H.org_id(), one))
    return render_template("cafe/qr_poster.html", st=st, tables=tables, name=cafe_name(st), link=menu_url())


# ---------------------------------------------------------------- menu: categories and items

@bp.route("/cafe/menu")
@cafe_view("menu_edit")
def menu():
    oid = H.org_id()
    cats = H.query("""SELECT c.*, (SELECT COUNT(*) FROM menu_items i WHERE i.category_id = c.id AND i.active = 1) n
                      FROM menu_categories c WHERE c.org_id = ? AND c.active = 1 ORDER BY c.sort, c.name COLLATE NOCASE""",
                   (oid,))
    items = H.query("SELECT * FROM menu_items WHERE org_id = ? AND active = 1 ORDER BY sort, name COLLATE NOCASE",
                    (oid,))
    q = (request.args.get("q") or "").strip().lower()
    if q:
        items = [i for i in items if q in i["name"].lower()]
    by_cat = {}
    for i in items:
        by_cat.setdefault(i["category_id"], []).append(i)
    return render_template("cafe/menu.html", cats=cats, by_cat=by_cat, st=get_settings(), q=q,
                           total=len(items), FOOD_TYPES=FOOD_TYPES, ITEM_TAGS=ITEM_TAGS)


@bp.route("/cafe/menu/category", methods=["POST"])
@cafe_view("menu_edit")
def category_save():
    oid = H.org_id()
    action = request.form.get("action")
    cid = request.form.get("id", type=int)
    db = H.get_db()
    if action == "add":
        name = clean(request.form.get("name"), 60)
        if not name:
            flash("Category-യുടെ പേര് കൊടുക്കുക.", "error")
        else:
            sort = (H.scalar("SELECT MAX(sort) FROM menu_categories WHERE org_id = ?", (oid,)) or 0) + 1
            db.execute("INSERT INTO menu_categories (org_id, name, sort, created_at) VALUES (?,?,?,?)",
                       (oid, name, sort, H.now()))
            db.commit()
            flash(f"Category '{name}' ചേർത്തു.", "success")
        return redirect(url_for("cafe.menu"))
    cat = get_category(cid)
    if action == "rename":
        name = clean(request.form.get("name"), 60)
        if name:
            db.execute("UPDATE menu_categories SET name = ? WHERE id = ? AND org_id = ?", (name, cid, oid))
    elif action in ("up", "down"):
        rows = H.query("SELECT id FROM menu_categories WHERE org_id = ? AND active = 1 ORDER BY sort, name COLLATE NOCASE",
                       (oid,))
        ids = [r["id"] for r in rows]
        i = ids.index(cid)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        for n, x in enumerate(ids):
            db.execute("UPDATE menu_categories SET sort = ? WHERE id = ?", (n, x))
    elif action == "delete":
        db.execute("UPDATE menu_items SET category_id = NULL WHERE category_id = ? AND org_id = ?", (cid, oid))
        db.execute("UPDATE menu_categories SET active = 0 WHERE id = ? AND org_id = ?", (cid, oid))
        flash(f"Category '{cat['name']}' മാറ്റി. അതിലെ items 'Other' ഭാഗത്തേക്ക് മാറി.", "success")
    db.commit()
    return redirect(url_for("cafe.menu"))


def item_form_data(item=None):
    f = request.form
    data = {"name": clean(f.get("name"), 80), "description": clean(f.get("description"), 300)}
    data["category_id"] = f.get("category_id", type=int)
    data["price"] = to_paise(f.get("price"))
    data["offer_price"] = to_paise(f.get("offer_price"), default="")
    data["offer_from"] = H.valid_date(f.get("offer_from"))
    data["offer_to"] = H.valid_date(f.get("offer_to"))
    data["food_type"] = f.get("food_type") if f.get("food_type") in FOOD_TYPES else ""
    data["tags"] = ",".join(t for t in f.getlist("tags") if t in ITEM_TAGS)
    data["gst_rate"] = to_float(f.get("gst_rate"), get_settings()["default_gst"], 0, 28)
    data["available"] = 1 if f.get("available") else 0
    data["sort"] = to_int(f.get("sort"), 0, 0, 9999)
    errors = []
    if not data["name"]:
        errors.append("Item-ന്റെ പേര് കൊടുക്കുക.")
    if data["price"] is None:
        errors.append("വില ശരിയായി കൊടുക്കുക (ഉദാ: 120 അല്ലെങ്കിൽ 120.50).")
    if data["offer_price"] is None:
        errors.append("Offer വില ശരിയല്ല.")
    elif data["offer_price"] == "":
        data["offer_price"] = None
    elif data["price"] is not None and data["offer_price"] >= data["price"]:
        errors.append("Offer വില സാധാരണ വിലയേക്കാൾ കുറവായിരിക്കണം.")
    if data["offer_from"] is None or data["offer_to"] is None:
        errors.append("Offer തീയതി ശരിയല്ല.")
    elif data["offer_from"] and data["offer_to"] and data["offer_to"] < data["offer_from"]:
        errors.append("Offer തീരുന്ന തീയതി തുടങ്ങുന്ന തീയതിക്ക് ശേഷമായിരിക്കണം.")
    if data["category_id"]:
        get_category(data["category_id"])
    new_cat = clean(f.get("new_category"), 60)
    return data, errors, new_cat


def _item_form(item, data, errors):
    cats = H.query("SELECT * FROM menu_categories WHERE org_id = ? AND active = 1 ORDER BY sort, name COLLATE NOCASE",
                   (H.org_id(),))
    for e in errors:
        flash(e, "error")
    return render_template("cafe/item_form.html", item=item, data=data, cats=cats, st=get_settings(),
                           FOOD_TYPES=FOOD_TYPES, ITEM_TAGS=ITEM_TAGS, GST_RATES=GST_RATES, hint=ITEM_PHOTO_HINT,
                           paise_str=paise_str)


def _apply_new_category(data, new_cat):
    if new_cat:
        oid = H.org_id()
        row = H.query("SELECT id FROM menu_categories WHERE org_id = ? AND active = 1 AND name = ? COLLATE NOCASE",
                      (oid, new_cat), one=True)
        if row:
            data["category_id"] = row["id"]
        else:
            sort = (H.scalar("SELECT MAX(sort) FROM menu_categories WHERE org_id = ?", (oid,)) or 0) + 1
            data["category_id"] = H.execute("INSERT INTO menu_categories (org_id, name, sort, created_at)"
                                            " VALUES (?,?,?,?)", (oid, new_cat, sort, H.now()))


ITEM_COLS = ("name", "description", "category_id", "price", "offer_price", "offer_from", "offer_to", "food_type",
             "tags", "gst_rate", "available", "sort")


@bp.route("/cafe/menu/item/new", methods=["GET", "POST"])
@cafe_view("menu_edit")
def item_new():
    data = {"available": 1, "gst_rate": get_settings()["default_gst"], "category_id": request.args.get("cat", type=int),
            "price": None, "offer_price": None}
    if request.method == "POST":
        data, errors, new_cat = item_form_data()
        if errors:
            return _item_form(None, data, errors)
        _apply_new_category(data, new_cat)
        photo = save_photo("photo", (600, 600), True)
        iid = H.execute(f"INSERT INTO menu_items (org_id, {', '.join(ITEM_COLS)}, photo, created_by, created_at,"
                        f" updated_at) VALUES ({', '.join('?' * (len(ITEM_COLS) + 5))})",
                        [H.org_id()] + [data[c] for c in ITEM_COLS] + [photo, g.user["id"], H.now(), H.now()])
        flash(f"'{data['name']}' menu-വിൽ ചേർത്തു.", "success")
        if request.form.get("another"):
            return redirect(url_for("cafe.item_new", cat=data["category_id"] or None))
        return redirect(url_for("cafe.menu") + f"#item{iid}")
    return _item_form(None, data, [])


@bp.route("/cafe/menu/item/<int:iid>/edit", methods=["GET", "POST"])
@cafe_view("menu_edit")
def item_edit(iid):
    item = get_item(iid)
    data = dict(item)
    if request.method == "POST":
        data, errors, new_cat = item_form_data(item)
        if errors:
            return _item_form(item, data, errors)
        _apply_new_category(data, new_cat)
        photo = item["photo"]
        new_photo = save_photo("photo", (600, 600), True)
        if new_photo:
            H.remove_media(photo)
            photo = new_photo
        elif request.form.get("remove_photo") and photo:
            H.remove_media(photo)
            photo = None
        H.execute(f"UPDATE menu_items SET {', '.join(c + ' = ?' for c in ITEM_COLS)}, photo = ?, updated_at = ?"
                  " WHERE id = ? AND org_id = ?", [data[c] for c in ITEM_COLS] + [photo, H.now(), iid, H.org_id()])
        flash(f"'{data['name']}' save ചെയ്തു.", "success")
        return redirect(url_for("cafe.menu") + f"#item{iid}")
    return _item_form(item, data, [])


@bp.route("/cafe/menu/item/<int:iid>/<action>", methods=["POST"])
@cafe_view("menu_edit")
def item_action(iid, action):
    item = get_item(iid)
    if action == "toggle":
        H.execute("UPDATE menu_items SET available = ?, updated_at = ? WHERE id = ? AND org_id = ?",
                  (0 if item["available"] else 1, H.now(), iid, H.org_id()))
    elif action == "delete":
        H.execute("UPDATE menu_items SET active = 0, updated_at = ? WHERE id = ? AND org_id = ?",
                  (H.now(), iid, H.org_id()))
        flash(f"'{item['name']}' menu-വിൽ നിന്ന് മാറ്റി. പഴയ bills-ൽ അത് അതുപോലെ ഉണ്ടാകും.", "success")
    else:
        abort(404)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, available=not item["available"])
    return redirect(H.safe_next(url_for("cafe.menu")) + f"#item{iid}")


# ---------------------------------------------------------------- offer banners

@bp.route("/cafe/banners")
@cafe_view("menu_edit")
def banners():
    oid = H.org_id()
    rows = H.query("""SELECT b.*, i.name item_name FROM menu_banners b LEFT JOIN menu_items i ON i.id = b.item_id
                      WHERE b.org_id = ? ORDER BY b.active DESC, b.sort, b.id""", (oid,))
    items = H.query("SELECT id, name FROM menu_items WHERE org_id = ? AND active = 1 ORDER BY name COLLATE NOCASE",
                    (oid,))
    return render_template("cafe/banners.html", banners=rows, items=items, hint=BANNER_PHOTO_HINT)


@bp.route("/cafe/banners/save", methods=["POST"])
@cafe_view("menu_edit")
def banner_save():
    oid = H.org_id()
    f = request.form
    bid = f.get("id", type=int)
    banner = H.query("SELECT * FROM menu_banners WHERE id = ? AND org_id = ?", (bid, oid), one=True) if bid else None
    if bid and not banner:
        abort(404)
    item_id = f.get("item_id", type=int)
    if item_id:
        get_item(item_id)
    start, end = H.valid_date(f.get("start_date")), H.valid_date(f.get("end_date"))
    if start is None or end is None:
        flash("തീയതി ശരിയല്ല.", "error")
        return redirect(url_for("cafe.banners"))
    photo = save_photo("photo", (1200, 500), True)
    data = (clean(f.get("title"), 80), item_id, start or None, end or None, 1 if f.get("active") else 0)
    if banner:
        if photo:
            H.remove_media(banner["photo"])
        H.execute("UPDATE menu_banners SET title=?, item_id=?, start_date=?, end_date=?, active=?, photo=? WHERE id=?",
                  data + (photo or banner["photo"], bid))
        flash("Banner save ചെയ്തു.", "success")
    else:
        if not photo and not item_id:
            flash("Banner-ന് ഒരു ഫോട്ടോ അല്ലെങ്കിൽ ഒരു item തിരഞ്ഞെടുക്കുക.", "error")
            return redirect(url_for("cafe.banners"))
        sort = (H.scalar("SELECT MAX(sort) FROM menu_banners WHERE org_id = ?", (oid,)) or 0) + 1
        H.execute("INSERT INTO menu_banners (org_id, title, item_id, start_date, end_date, active, photo, sort,"
                  " created_at) VALUES (?,?,?,?,?,?,?,?,?)", (oid,) + data + (photo, sort, H.now()))
        flash("Banner ചേർത്തു.", "success")
    return redirect(url_for("cafe.banners"))


@bp.route("/cafe/banners/<int:bid>/<action>", methods=["POST"])
@cafe_view("menu_edit")
def banner_action(bid, action):
    oid = H.org_id()
    b = H.query("SELECT * FROM menu_banners WHERE id = ? AND org_id = ?", (bid, oid), one=True) or abort(404)
    if action == "toggle":
        H.execute("UPDATE menu_banners SET active = ? WHERE id = ?", (0 if b["active"] else 1, bid))
    elif action == "delete":
        H.remove_media(b["photo"])
        H.execute("DELETE FROM menu_banners WHERE id = ?", (bid,))
        flash("Banner മാറ്റി.", "success")
    else:
        abort(404)
    return redirect(url_for("cafe.banners"))


# ---------------------------------------------------------------- tables

@bp.route("/cafe/tables")
@cafe_view("bill_create")
def tables():
    oid = H.org_id()
    rows = H.query("""SELECT t.*, o.id order_id, o.order_no, o.created_at since, o.cust_name,
                        (SELECT COALESCE(SUM(price * qty), 0) FROM cafe_order_items i WHERE i.order_id = o.id) amount
                      FROM cafe_tables t LEFT JOIN cafe_orders o ON o.table_id = t.id AND o.status = 'open'
                        AND o.order_type = 'dine_in'
                      WHERE t.org_id = ? AND t.active = 1 ORDER BY t.sort, t.id""", (oid,))
    return render_template("cafe/tables.html", tables=rows)


@bp.route("/cafe/tables/save", methods=["POST"])
@cafe_view("menu_edit")
def table_save():
    oid = H.org_id()
    f = request.form
    if f.get("action") == "bulk":
        n = to_int(f.get("count"), 0, 0, 100)
        start = (H.scalar("SELECT COUNT(*) FROM cafe_tables WHERE org_id = ? AND active = 1", (oid,)) or 0) + 1
        db = H.get_db()
        for i in range(n):
            db.execute("INSERT INTO cafe_tables (org_id, name, seats, sort, created_at) VALUES (?,?,?,?,?)",
                       (oid, f"T{start + i}", to_int(f.get("seats"), 4, 1, 50), start + i, H.now()))
        db.commit()
        flash(f"{n} tables ചേർത്തു.", "success")
        return redirect(url_for("cafe.tables"))
    tid = f.get("id", type=int)
    name = clean(f.get("name"), 30)
    if not name:
        flash("Table-ന്റെ പേര്/നമ്പർ കൊടുക്കുക.", "error")
        return redirect(url_for("cafe.tables"))
    if tid:
        H.query("SELECT 1 FROM cafe_tables WHERE id = ? AND org_id = ?", (tid, oid), one=True) or abort(404)
        H.execute("UPDATE cafe_tables SET name = ?, seats = ? WHERE id = ? AND org_id = ?",
                  (name, to_int(f.get("seats"), None, 1, 50), tid, oid))
    else:
        sort = (H.scalar("SELECT MAX(sort) FROM cafe_tables WHERE org_id = ?", (oid,)) or 0) + 1
        H.execute("INSERT INTO cafe_tables (org_id, name, seats, sort, created_at) VALUES (?,?,?,?,?)",
                  (oid, name, to_int(f.get("seats"), None, 1, 50), sort, H.now()))
    flash("Table save ചെയ്തു.", "success")
    return redirect(url_for("cafe.tables"))


@bp.route("/cafe/tables/<int:tid>/delete", methods=["POST"])
@cafe_view("menu_edit")
def table_delete(tid):
    oid = H.org_id()
    t = H.query("SELECT * FROM cafe_tables WHERE id = ? AND org_id = ?", (tid, oid), one=True) or abort(404)
    if H.scalar("SELECT 1 FROM cafe_orders WHERE table_id = ? AND status = 'open'", (tid,)):
        flash(f"{t['name']}-ൽ ഇപ്പോൾ order ഉണ്ട്. Bill ചെയ്ത ശേഷം മാറ്റാം.", "error")
    else:
        H.execute("UPDATE cafe_tables SET active = 0 WHERE id = ?", (tid,))
        flash(f"{t['name']} മാറ്റി.", "success")
    return redirect(url_for("cafe.tables"))
