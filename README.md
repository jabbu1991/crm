# Realview CRM

*A product of Realview Creative Studio.*

A multi-tenant lead-management CRM for a **marketing agency and the client businesses it works for**. Everyone uses the same web address. After login, each user lands in their own organization's private workspace.

## Roles

| Role | Who | Can do |
|---|---|---|
| **Super Admin** | Agency owner | Everything an Organization Admin can do in the agency workspace. Also creates, edits, activates and deactivates client organizations; manages their admin accounts (invite links, password resets, deactivation); sees totals per organization. **Open workspace** shows a client's leads, reports and staff read-only; every visit and export is written to that client's access log (Organization Settings). |
| **Organization Admin** | Agency or client administrator | Sees all leads in their organization. Manages projects, products and staff accounts; runs reports and CSV export; edits organization settings. |
| **Staff** | Telecallers, sales executives | Adds leads. Sees and updates only the leads they created or that are assigned to them, plus their own dashboard and follow-ups. |

## Features

- **Organizations:** one agency workspace plus any number of client workspaces, each with fully isolated data.
- **Accounts:** individual logins (username or email). New users join through a **single-use invitation link**, or an initial password they must change at first login. Admins can create a reset link or set a temporary password. Passwords are hashed and never shown. Deactivating a user or an organization signs them out immediately. Repeated failed logins are throttled.
- **Projects / campaigns:** code, client, campaign type, start and end dates, status, assigned staff and linked products. If staff are assigned, only those staff (and admins) can add leads to that project.
- **Leads:** customer name, phone, WhatsApp, email, place, district, state (a standard list of Indian states and UTs, plus an option for outside India), country, project, products (multi-select), requirement description, source, quality, buying possibility, priority, status, assigned staff, next follow-up date.
  - **Created by**, organization, and created/updated date and time are recorded by the server from the login. They can't be typed in or faked.
  - Duplicate-customer warning across the organization.
  - Search, sorting, filters, date ranges, pagination (50 per page), CSV export (admins), CSV import.
- **Products & Services:** per-organization catalogue (name, code, category, description, active/inactive).
- **Interest vs. order:** ticking a product on a lead records *interest*. Each lead/product pair has its own stage (Interested → Ordered → Purchased, or Dropped). Converting a lead never marks a product as purchased automatically.
- **State-wise Leads:** every state with its lead count. Click a state to see its leads, with search, filters, sorting, pagination and export. Spellings like "kerala" or "KL" are stored as "Kerala".
- **Product-wise Leads:** for each product: interested leads, distinct customers, hot, still interested, ordered, purchased, lost/dropped and conversion rate. Click a product to see its leads.
- **Follow-ups:** schedule, complete or cancel follow-ups. Separate lists for overdue, today, upcoming and completed.
- **Export:** every role can download its own scope as CSV files or one ZIP: leads, product interest, follow-ups, activity history, projects, products and users. A Super Admin exports all organizations at once. Spreadsheet formulas in cell values are neutralised.
- **Dashboards:** separate views for Super Admin, Organization Admin and Staff, all built from live queries within the user's permissions.

## Run it

**Windows (easiest):** download and unzip the project, then double-click **`start.bat`**. It installs what's needed, starts the CRM and opens your browser. Keep its window open while you use the CRM.

**Try it with sample data:** double-click **`demo.bat`** (Windows). It opens the CRM on a separate demo database (`instance/demo.db`) with a Super Admin, two client companies, their admins, staff, products and leads. Every demo password is `demo1234`, and the usernames are printed in the window. Delete `instance/demo.db` to start the demo again from scratch.

**Any system** (Python 3.9+):

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000.

- **New install:** the first screen creates your agency workspace and the Super Admin account.
- **Upgrading from the earlier single-workspace version:** just start the new version. The database is upgraded automatically (see below). Your existing admin logins still work. The first admin becomes Super Admin; other admins become Organization Admins of the agency workspace.

Then, as Super Admin: **Client Organizations → New client organization** creates a client workspace and its first admin. Send them the invitation link it shows.

## Database and upgrades

- SQLite file: `instance/crm.db`. Back up the whole `instance/` folder regularly.
- The schema version is stored in the database (`PRAGMA user_version`). When the app starts on an older database, it:
  1. **copies the whole database** to `instance/backups/crm-<date>-pre-v1.db`;
  2. adds the new tables and columns in one transaction per version (nothing is deleted);
  3. moves existing users, projects and leads into the agency organization, standardises state names, and turns each distinct old "requirement" into a product, linked to its leads as *Interested*.
- Tenant safety is enforced twice: in the application, where every query is filtered by the logged-in user's organization and role, and in the database, where triggers reject any row that links data across organizations or changes a lead's creator.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests create the accounts Super Admin, Org A Admin, Org A Staff 1, Org A Staff 2, Org B Admin and Org B Staff. They check isolation between staff and between organizations, admin visibility, account deactivation, invite and reset links, temporary passwords, CSRF protection, login throttling, the database triggers, state normalisation and permissions, product report counts (interest vs. order), pagination, import, and migration from the original version.

## Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5000` | Port to listen on |
| `HOST` | `127.0.0.1` | Use `0.0.0.0` to allow other computers on your network |
| `CRM_DB_PATH` | `instance/crm.db` | Database file location |
| `CRM_SECRET_KEY` | auto-generated | Session signing key (set a fixed value in production) |
| `CRM_SECURE_COOKIES` | off | Set to `1` when served over HTTPS |
| `CRM_BEHIND_PROXY` | off | Set to `1` behind an HTTPS proxy/load balancer (Render, nginx) so links use https |
| `CRM_SETUP_KEY` | none | If set, the first-run setup page requires this key |
| `CRM_ENQUIRY_ORIGINS` | none | Comma-separated website origins allowed to post to `/enquiry` |
| `CRM_PRODUCT_NAME` / `CRM_COMPANY_NAME` / `CRM_COMPANY_URL` | Real View CRM / Real View Creative Studio / realviewcreativestudio.in | Branding |

## Agency website

`website/` is the Real View Creative Studio website: one static page with no build step, published free on Render as `realview-website`. Edit `website/index.html` for text and `website/main.js` for the WhatsApp number.

The contact form opens WhatsApp with the visitor's details filled in. When the CRM is online, the form also posts to `/enquiry` on the CRM, which saves the enquiry as a lead in the agency workspace (project "Website Enquiries", source "Website"). This only works for the origins listed in `CRM_ENQUIRY_ORIGINS`, is rate-limited per IP, and uses a honeypot field to catch bots.

## Hosting online (Render)

`render.yaml` describes the whole setup: a web service, a 1 GB persistent disk for the database, and generated secrets.

1. Sign up at render.com with your GitHub account and give Render access to this repository.
2. Add a payment method. The Starter plan plus the disk is about US$7–8 a month.
3. Go to **New → Blueprint**, pick this repository, and click **Apply**.
4. When it's live, open **realview-crm → Environment**, reveal **CRM_SETUP_KEY**, and enter it on the first-run setup page.

Every push to the branch named in `render.yaml` redeploys automatically. Data on the disk is kept between deploys. Render takes daily disk snapshots, and the app writes its own backup before any schema upgrade.

## Production

Run behind HTTPS with a WSGI server, for example:

```bash
pip install gunicorn
CRM_SECURE_COOKIES=1 gunicorn -w 1 -b 0.0.0.0:8000 app:app
```

Use one worker: the login throttle is kept in memory, and SQLite works best with a single writer process.
