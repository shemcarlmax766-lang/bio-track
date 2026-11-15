# BioTrack

Hospital biomedical engineering management for Ugandan health facilities. BioTrack is a
low-dependency Flask + SQLite application with a server-rendered, responsive client, built to
track medical equipment, faults, repairs, preventive maintenance, spare parts and reporting
across multiple hospitals without cross-tenant data exposure.

Data lives in `data/biotrack.sqlite3` (WAL mode); uploaded photographs and service manuals
live under `data/uploads/`. A demonstration tenant is seeded on first start.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

Open `http://localhost:8000`. Set `PORT` to change the listening port.

For a WSGI deployment:

```bash
gunicorn --preload --workers 2 --threads 4 --bind 0.0.0.0:8000 server:app
```

`python server.py` is for local development only.

### Demonstration accounts

All demo accounts use the password `BioTrack2026!`:

| Role | Email |
|---|---|
| Hospital Administrator | `admin@biotrack.ug` |
| Biomedical Engineer | `engineer@biotrack.ug` |
| Hospital Staff | `staff@biotrack.ug` |
| Biomedical Engineer (second assignee) | `engineer2@biotrack.ug` |

The seeded hospital and its records are clearly marked demonstration data. **Do not enter
patient-identifiable information** in fault descriptions, notes or photographs.

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite runs against a temporary SQLite database and upload directory, so it never touches
`data/`. It covers authentication and CSRF, session revocation, role boundaries and tenant
isolation, equipment registration and QR codes, the fault → work order → repair →
return-to-service workflow, maintenance scheduling rules, the spare-parts ledger, PDF/XLSX
exports, the hospital-scoped backup, upload validation, BioAssist answer modes, and the audit
trail.

## What is implemented

**Roles and access.** Three roles — Hospital Administrator, Biomedical Engineer, Hospital
Staff — enforced server-side on every route, not just hidden in the navigation. Staff see only
their own department's equipment and faults.

**Security.** Server-side sessions, PBKDF2 password hashing, CSRF tokens on every
state-changing request, login throttling with temporary lockout, HTTPS-aware `Secure` cookies,
session-epoch revocation on suspension/password change/privilege change, an administrator
cannot change their own role, uploads validated by magic bytes as well as extension, uploaded
files served only to signed-in hospital users, and a full audit trail.

**Equipment inventory.** Registration and editing with name, category, manufacturer, model,
serial, identification (asset) number, department, physical location, installation/purchase/
warranty dates, cost in UGX, operational status, assigned engineer, maintenance dates,
photograph and notes. Twelve standard categories plus administrator-defined ones.

**QR codes.** Every item gets a unique QR token and a printable label. The public scan page
shows a deliberately limited summary; reporting a fault requires sign-in, and staff may only
report for their own department.

**Faults and repairs.** Report with severity and optional photograph; engineer reviews,
assigns, records findings and diagnosis, opens a linked work order, documents the repair and
closes it. Full status set: reported, under review, assigned, under repair, awaiting spare
parts, resolved, closed. Closing a fault or work order requires a note, and equipment returns
to service only with documented verification. Engineers can flag equipment as not safe for
clinical use, which notifies administrators.

**Preventive maintenance.** Per-equipment intervals with **no assumed default** — a schedule
requires an explicit interval plus a manufacturer-manual or approved-facility-policy
reference. Checklist templates, next-due rollover, overdue tracking, completion history, and
verification before return to service.

**Spare parts.** Inventory with an auditable ledger: opening balances, receipts, issues,
reasoned signed corrections, running balances, atomic transactions that refuse to go negative,
and low-stock notifications.

**Dashboard and reporting.** Equipment counts and status distribution, overdue/upcoming
maintenance, open faults, average turnaround and recorded downtime, department distribution,
and a six-month faults-vs-repairs trend. Filters by department, category and date range. Seven
reports (inventory, condition, maintenance, faults, repairs, monthly activity, spare parts)
preview on screen and export to PDF and Excel.

**Notifications.** In-app notifications for new faults, assignments, upcoming/overdue
maintenance, awaiting-parts and completed repairs, and low stock. Deduplicated so repeated
page loads do not spam the inbox. The notification module is the single insertion point for a
future email channel.

**Backup.** Administrators can download a SQLite snapshot containing only their own hospital's
rows. Uploaded files are not included and must be backed up separately.

**Uganda specifics.** UGX formatting, Africa/Kampala orientation, standard departments from
Ugandan facilities, exportable records, low-bandwidth-friendly server rendering and a service
worker that caches only static assets.

## BioAssist provider configuration

Set these on the server process — never in browser JavaScript:

```bash
export BIOASSIST_API_KEY="your-provider-key"
export BIOASSIST_API_URL="https://api.openai.com/v1/chat/completions"  # optional
export BIOASSIST_MODEL="gpt-4o-mini"                                  # optional
```

BioAssist searches the extracted text of PDF manuals uploaded by your hospital, then sends the
question, the selected equipment record, and the top matching excerpts to the provider. It
never sends a full database dump. **Every answer is labelled** by how it was produced:

| Mode | Meaning |
|---|---|
| AI answer citing your manuals | Generated text, with the manual excerpts shown |
| Manual excerpts only | No provider configured or reachable; verbatim pages shown |
| General AI explanation | Generated, but no manual in this hospital matched |
| AI unavailable | The provider failed; the reason is stated |

Scanned-image PDFs need OCR, which is **not configured** — such uploads index zero pages and
the app says so rather than pretending the manual is searchable.

BioAssist will not certify equipment as safe, authorise repairs or override safety warnings,
and the UI repeats that a qualified engineer remains responsible.

## Not configured in this build

- **Email/SMS delivery** — notifications are in-app only.
- **OCR** for scanned manuals.
- **AI provider** unless `BIOASSIST_API_KEY` is set.

## Security and deployment notes

This is a deployable **MVP foundation**, not a claim of regulatory approval and not an
independently security-audited clinical service. Before handling live hospital records:

1. Use HTTPS and a managed secrets service. Set `SECRET_KEY` in the environment; without it a
   key file is generated in `data/`. Set `COOKIE_SECURE=1` if a trusted TLS proxy does not
   forward the secure scheme, `COOKIE_SECURE=0` only for local HTTP.
2. Put a production WSGI server behind a configured reverse proxy; do not use Flasks's
   development server in production.
3. Add a restrictive Content Security Policy and `frame-ancestors` for trusted origins. The
   preview intentionally avoids frame restrictions so the hosted demo can render.
4. Move SQLite to a managed relational database for multi-process or high-concurrency
   deployments; configure encrypted backups, restore drills, monitoring and retention.
5. Add malware scanning, access reviews, document-retention controls and OCR only if approved.
   Apply a formal threat model and penetration test.
6. Have Uganda-based counsel and the facility's biomedical/clinical governance team verify
   applicable health-sector, medical-equipment, privacy and data-protection requirements.
7. Do not upload patient-identifiable data.

To reset the demonstration dataset, stop the server and remove `data/biotrack.sqlite3` and
`data/uploads/`; the next start reseeds the demo tenant. Always back up the database and
uploads together.
