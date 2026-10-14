# Agency CRM

A lead-management CRM web application built for **marketing agencies** that run campaigns for many clients.

## Features

- **Projects (clients):** one agency, many clients. Every lead belongs to a project, and the dashboard and lead list can be filtered by project.
- **Staff accounts:** each person logs in with their own account. The staff member who enters a lead is recorded automatically ("Entered by") along with the date and time. Later edits record who changed what.
  - **Admin:** manages projects and staff, and can delete records.
  - **Staff:** adds and edits leads.
- **Lead data entry:** customer name, phone, WhatsApp number (with a "same as phone" shortcut), email, place, state, requirement (product), status, buying possibility (High / Medium / Low), lead quality (Hot / Warm / Cold), lead source and notes.
  - A warning appears when the same phone, WhatsApp number or email already exists in the project.
  - "Save & add another" speeds up bulk entry.
- **Sorting:** one click sorts by name, number, email, place, state, requirement, lead quality, buying possibility, status or date. You can also click any column header.
- **Filtering and search:** filter by project, requirement, status, quality, possibility, state, place, source, staff and date range, or search across all fields at once.
- **Reports:** leads grouped by requirement (each product listed separately), place, state, status, buying possibility, lead quality, source, staff or project. Each group shows Hot/Warm/Cold, high-possibility and converted counts, plus a link to that group's leads.
- **Lead page:** click-to-call, a WhatsApp link (wa.me), quick status change, follow-up notes and a full history.
- **CSV export** of any filtered list (opens in Excel) and **CSV import** for leads from Facebook, Google and similar sources.
- Works on mobile phones.

## Run it

**Windows (easiest):** download and unzip the project, then double-click **`start.bat`**. It installs what's needed, starts the CRM and opens your browser. Keep its window open while you use the CRM.

**Any system:**

Requires Python 3.9+.

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. On first launch you create the admin account. Then:

1. **Projects → New project** for each client.
2. **Staff → Add staff** for each team member.
3. Staff log in and use **+ New Lead**.

Data is stored in `instance/crm.db` (SQLite). Back up that file to back up everything.

### Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5000` | Port to listen on |
| `HOST` | `127.0.0.1` | Use `0.0.0.0` to allow other computers on your network |
| `CRM_DB_PATH` | `instance/crm.db` | Database file location |
| `CRM_SECRET_KEY` | auto-generated | Session signing key |

### Production

Use a WSGI server instead of the built-in one, for example:

```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

Put it behind HTTPS (nginx, Caddy, or a platform such as Render or Railway).
