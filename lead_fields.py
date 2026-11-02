"""Lead form fields per organization (Settings > Lead form fields).

Each client can give the headings and fields of the lead form their own names (shown everywhere the field appears:
form, lead page, list columns, filters, export) and hide fields they never use. Hiding only removes the field from
the form, lists and filters: lead data is never changed, and a hidden field's old value still shows on the lead page.
Stored as JSON in org_lead_fields.config: {"labels": {key: name}, "hidden": [key, ...]}. No row = the standard form.
"""
import json

MAX_LABEL = 40

# (key, standard name, can be hidden, group). Keys match LIST_COLUMNS in app.py where a column exists.
FIELDS = [
    ("sec_project", "Project", False, "Headings"),
    ("sec_customer", "Customer details", False, "Headings"),
    ("sec_products", "Products / services interested in", False, "Headings"),
    ("sec_qual", "Qualification & follow-up", False, "Headings"),
    ("project", "Project / campaign", False, "Project"),
    ("assigned", "Assigned to", False, "Project"),
    ("customer_name", "Customer name", False, "Customer details"),
    ("company", "Company / business name", True, "Customer details"),
    ("phone", "Phone number", False, "Customer details"),
    ("whatsapp", "WhatsApp number", True, "Customer details"),
    ("email", "Email address", True, "Customer details"),
    ("place", "Place / city", True, "Customer details"),
    ("district", "District", True, "Customer details"),
    ("state", "State", True, "Customer details"),
    ("country", "Country", True, "Customer details"),
    ("products", "Products", True, "Products"),
    ("requirement", "Requirement description", True, "Products"),
    ("status", "Lead status", False, "Qualification & follow-up"),
    ("possibility", "Buying possibility", True, "Qualification & follow-up"),
    ("priority", "Priority", True, "Qualification & follow-up"),
    ("source", "Lead source", True, "Qualification & follow-up"),
    ("next_follow_up", "Next follow-up date", True, "Qualification & follow-up"),
    ("notes", "Notes / remarks", True, "Qualification & follow-up"),
]
STANDARD = {k: label for k, label, _h, _g in FIELDS}
HIDEABLE = {k for k, _l, h, _g in FIELDS if h}

# Lead table columns behind each hideable field (kept as they were when a field is hidden in the form).
DATA_KEYS = {
    "company": ["company_name"], "whatsapp": ["whatsapp"], "email": ["email"], "place": ["place"],
    "district": ["district"], "state": ["state"], "country": ["country"], "products": ["product_ids"],
    "requirement": ["requirement"], "possibility": ["buying_possibility"], "priority": ["priority"],
    "source": ["source"], "next_follow_up": ["next_follow_up"], "notes": ["notes"],
}


def load(db, org_id):
    """{'labels': {key: name}, 'hidden': set(keys)} for one organization (only known keys)."""
    row = db.execute("SELECT config FROM org_lead_fields WHERE org_id = ?", (org_id,)).fetchone() if org_id else None
    labels, hidden = {}, set()
    if row:
        try:
            cfg = json.loads(row[0] or "{}")
        except (ValueError, TypeError):
            cfg = {}
        for k, v in (cfg.get("labels") or {}).items():
            if k in STANDARD and isinstance(v, str) and v.strip():
                labels[k] = v.strip()[:MAX_LABEL]
        hidden = {k for k in (cfg.get("hidden") or []) if k in HIDEABLE}
    return {"labels": labels, "hidden": hidden}


def save(db, org_id, labels, hidden, user_id, ts):
    cfg = {"labels": {k: v.strip()[:MAX_LABEL] for k, v in labels.items()
                      if k in STANDARD and v and v.strip() and v.strip() != STANDARD[k]},
           "hidden": sorted(k for k in hidden if k in HIDEABLE)}
    db.execute("""INSERT INTO org_lead_fields (org_id, config, updated_by, updated_at) VALUES (?,?,?,?)
                  ON CONFLICT(org_id) DO UPDATE SET config = excluded.config, updated_by = excluded.updated_by,
                  updated_at = excluded.updated_at""", (org_id, json.dumps(cfg), user_id, ts))
    return cfg


def reset(db, org_id):
    db.execute("DELETE FROM org_lead_fields WHERE org_id = ?", (org_id,))
