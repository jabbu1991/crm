"""Per-client dropdown options for leads: Lead status, Buying possibility, Priority, Lead source.

A client that never changed a list uses the built-in defaults (crm_constants). Once a client edits a list, the whole
list (in order) is stored in org_options. Options are never deleted: "Hide" keeps old leads intact. Every status has a
stage (new / open / won / lost) so the dashboard and reports count Converted / Lost correctly whatever the names are.
"""
from crm_constants import BUYING_POSSIBILITIES, CLOSED_LOST_STATUSES, LEAD_SOURCES, LEAD_STATUSES, PRIORITIES

KINDS = {
    "status": ("Lead status", LEAD_STATUSES),
    "buying_possibility": ("Buying possibility", BUYING_POSSIBILITIES),
    "priority": ("Priority", PRIORITIES),
    "source": ("Lead source", LEAD_SOURCES),
}
STAGES = {"new": "New (fresh lead)", "open": "Open (working on it)", "won": "Won ✅ (converted / ordered)",
          "lost": "Lost ❌ (not interested / lost)"}
MAX_OPTIONS = 40
MAX_LEN = 40


def default_stage(value):
    if value == "New":
        return "new"
    if value == "Converted":
        return "won"
    if value in CLOSED_LOST_STATUSES:
        return "lost"
    return "open"


def defaults(kind):
    return [{"value": v, "hidden": False, "stage": default_stage(v) if kind == "status" else None, "custom": False}
            for v in KINDS[kind][1]]


def load(db, org_id, kind):
    """All options of a kind for an organization, in order (hidden ones included, marked)."""
    rows = db.execute("SELECT value, hidden, stage FROM org_options WHERE org_id = ? AND kind = ? ORDER BY sort, id",
                      (org_id, kind)).fetchall()
    if not rows:
        return defaults(kind)
    return [{"value": r["value"], "hidden": bool(r["hidden"]), "stage": r["stage"] if kind == "status" else None,
             "custom": True} for r in rows]


def save(db, org_id, kind, options, now):
    """Store the full list (in this order)."""
    db.execute("DELETE FROM org_options WHERE org_id = ? AND kind = ?", (org_id, kind))
    db.executemany("INSERT INTO org_options (org_id, kind, value, sort, hidden, stage, created_at)"
                   " VALUES (?,?,?,?,?,?,?)",
                   [(org_id, kind, o["value"], i, 1 if o["hidden"] else 0,
                     (o.get("stage") or "open") if kind == "status" else None, now) for i, o in enumerate(options)])


def reset(db, org_id, kind):
    db.execute("DELETE FROM org_options WHERE org_id = ? AND kind = ?", (org_id, kind))


def visible(options):
    return [o["value"] for o in options if not o["hidden"]]


def all_values(options):
    return [o["value"] for o in options]


def stage_values(options, stage):
    return [o["value"] for o in options if o.get("stage") == stage]


def sql_list(values):
    """'a','b' for an IN (...) clause; values come from the client's own option list (quotes escaped)."""
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in values) or "''"


def case_order(column, values):
    """ORDER BY expression following the client's list order."""
    if not values:
        return column
    parts = " ".join(f"WHEN {sql_list([v])} THEN {i}" for i, v in enumerate(values))
    return f"CASE {column} {parts} ELSE {len(values)} END"


def with_current(values, current):
    """Dropdown values plus the lead's current value if that option is hidden now (so saving never changes it)."""
    if current and current not in values:
        return list(values) + [current]
    return list(values)
