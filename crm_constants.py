"""Option lists, roles and state normalisation shared by the app and migrations."""
import re

# ---------------------------------------------------------------- roles

SUPER_ADMIN, ORG_ADMIN, STAFF = "super_admin", "org_admin", "staff"
ROLE_LABELS = {SUPER_ADMIN: "Super Admin", ORG_ADMIN: "Organization Admin", STAFF: "Staff"}

# ---------------------------------------------------------------- lead options

LEAD_STATUSES = ["New", "Contacted", "Interested", "Follow-up", "Negotiation",
                 "Converted", "Not Interested", "Lost"]
CLOSED_LOST_STATUSES = ("Lost", "Not Interested")
BUYING_POSSIBILITIES = ["High", "Medium", "Low"]
LEAD_QUALITIES = ["Hot", "Warm", "Cold"]
PRIORITIES = ["Urgent", "High", "Normal", "Low"]
LEAD_SOURCES = ["Facebook", "Instagram", "Google Ads", "Website", "WhatsApp",
                "Referral", "Walk-in", "Phone Call", "Other"]

# Interest in a product is NOT an order: each lead/product link carries its own stage.
PRODUCT_STAGES = ["Interested", "Ordered", "Purchased", "Dropped"]

PROJECT_STATUSES = ["Planned", "Active", "Paused", "Completed"]
CAMPAIGN_TYPES = ["Lead Generation", "Social Media", "Google Ads", "SEO", "Email / WhatsApp",
                  "Brand Awareness", "Event", "Other"]

COUNTRIES = ["India", "United Arab Emirates", "Saudi Arabia", "Qatar", "Oman", "Kuwait", "Bahrain",
             "United States", "United Kingdom", "Canada", "Australia", "Singapore", "Malaysia"]
DEFAULT_COUNTRY = "India"

# ---------------------------------------------------------------- Indian states

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan",
    "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    # Union territories
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]

_STATE_ALIASES = {
    "ap": "Andhra Pradesh", "ar": "Arunachal Pradesh", "as": "Assam", "br": "Bihar",
    "cg": "Chhattisgarh", "ct": "Chhattisgarh", "chattisgarh": "Chhattisgarh", "ga": "Goa",
    "gj": "Gujarat", "hr": "Haryana", "hp": "Himachal Pradesh", "jh": "Jharkhand",
    "ka": "Karnataka", "kar": "Karnataka", "karnatka": "Karnataka",
    "kl": "Kerala", "ker": "Kerala", "kerela": "Kerala", "keralam": "Kerala",
    "mp": "Madhya Pradesh", "mh": "Maharashtra", "mn": "Manipur", "ml": "Meghalaya",
    "mz": "Mizoram", "nl": "Nagaland", "od": "Odisha", "or": "Odisha", "orissa": "Odisha",
    "pb": "Punjab", "rj": "Rajasthan", "sk": "Sikkim", "tn": "Tamil Nadu", "ts": "Telangana",
    "tg": "Telangana", "tr": "Tripura", "up": "Uttar Pradesh", "uk": "Uttarakhand",
    "ua": "Uttarakhand", "uttaranchal": "Uttarakhand", "wb": "West Bengal",
    "an": "Andaman and Nicobar Islands", "andaman": "Andaman and Nicobar Islands",
    "ch": "Chandigarh", "dn": "Dadra and Nagar Haveli and Daman and Diu",
    "dd": "Dadra and Nagar Haveli and Daman and Diu", "daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "dl": "Delhi", "new delhi": "Delhi", "nct of delhi": "Delhi", "nct delhi": "Delhi",
    "jk": "Jammu and Kashmir", "j and k": "Jammu and Kashmir", "jammu kashmir": "Jammu and Kashmir",
    "la": "Ladakh", "ld": "Lakshadweep", "py": "Puducherry", "pondicherry": "Puducherry",
    "pondy": "Puducherry",
}


def _key(value):
    v = (value or "").strip().lower().replace("&", " and ").replace(".", "")
    return re.sub(r"\s+", " ", v).strip()


_STATE_LOOKUP = {}
for _s in INDIAN_STATES:
    _STATE_LOOKUP[_key(_s)] = _s
    _STATE_LOOKUP[_key(_s).replace(" ", "")] = _s
for _alias, _s in _STATE_ALIASES.items():
    _STATE_LOOKUP[_alias] = _s
    _STATE_LOOKUP[_alias.replace(" ", "")] = _s


def indian_state(value):
    """Return the canonical Indian state for value (name, abbreviation, common misspelling), else None."""
    k = _key(value)
    return _STATE_LOOKUP.get(k) or _STATE_LOOKUP.get(k.replace(" ", "")) if k else None


def normalize_country(value):
    v = re.sub(r"\s+", " ", (value or "").strip())
    if not v:
        return ""
    if v.lower() in ("india", "in", "ind", "bharat"):
        return "India"
    for c in COUNTRIES:
        if c.lower() == v.lower():
            return c
    if v.lower() in ("uae", "u.a.e", "u.a.e."):
        return "United Arab Emirates"
    if v.lower() in ("usa", "us", "u.s.a", "united states of america"):
        return "United States"
    if v.lower() in ("uk", "u.k", "england"):
        return "United Kingdom"
    return v.title() if v.islower() or v.isupper() else v


def normalize_location(state, country):
    """Return (state, country) with Indian states mapped to one canonical spelling.

    An unrecognised state is kept (title-cased) so international locations still work.
    """
    country = normalize_country(country)
    canonical = indian_state(state)
    if canonical and country in ("", "India"):
        return canonical, "India"
    s = re.sub(r"\s+", " ", (state or "").strip())
    if s and (s.islower() or s.isupper()) and len(s) > 3:
        s = s.title()
    return s, country


def phone_key(value):
    """Digits-only phone used for duplicate detection (last 10 digits)."""
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits
