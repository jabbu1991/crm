"""What's new: short notes shipped with the software. Add one at the TOP of SHIPPED with every user-visible change.

key       unique, never reused (the note is stored once per server)
date      'YYYY-MM-DD' (India date) — shown, and used to skip notes older than a new account
module    all | crm | cafe | prod | fam   (who has that module sees it)
audience  all | admins
feature   optional: 'perf' → companies without it get a 🙋 Request button
Screens are in English, so the notes are too.
"""

SHIPPED = [
    {"key": "2026-11-11-perf", "date": "2026-11-11", "module": "crm", "audience": "admins", "feature": "perf",
     "title": "📊 New: Staff performance",
     "body": "See each staff member's work: leads added, follow-ups done on time, calls and WhatsApp taps, notes, "
             "leads won, time in the app, and a score out of 100 with friendly suggestions. Staff see only their own "
             "report. Tap Request and we will switch it on for your company."},
    {"key": "2026-11-11-most-sold", "date": "2026-11-11", "module": "cafe", "audience": "all",
     "title": "⭐ Billing: Most sold tab",
     "body": "The billing screen opens on '⭐ Most sold' — the 20 items billed most in the last 30 days, best first. "
             "'All' and the categories are next to it as before."},
    {"key": "2026-11-11-packing", "date": "2026-11-11", "module": "cafe", "audience": "admins",
     "title": "📦 Packing charge for takeaway and delivery",
     "body": "Settings → 📦 Packing charge: switch it on, choose takeaway and/or delivery, and tick the items (each "
             "item can have its own amount). Bills add it with GST; it can be taken off one bill."},
    {"key": "2026-11-11-catalogue", "date": "2026-11-11", "module": "crm", "audience": "admins",
     "title": "📒 Catalogue: new grid look",
     "body": "Your digital catalogue now shows 2 products in a row on phones, with category buttons, a search box and "
             "prices like ₹2,55,000. Organization Settings → Digital catalogue → 'How products are shown'."},
    {"key": "2026-11-10-india-time", "date": "2026-11-10", "module": "all", "audience": "all",
     "title": "🕒 Times are now in India time",
     "body": "Bills, KOTs, PDFs, follow-ups and history now show the correct India time (earlier some showed 5½ "
             "hours less)."},
]

FEATURES = {
    "perf": {"label": "📊 Staff performance", "column": "perf_allowed"},
}
