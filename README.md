# BioTrack

A full-stack demonstration MVP for hospital biomedical engineering management. The app is built as a low-dependency Flask + SQLite service with a dependency-free responsive client. Data is stored persistently in `data/biotrack.sqlite3`; uploaded documents and images are stored under `data/uploads/`.

## Run locally

```bash
cd biotrack
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
python server.py
```

Open `http://localhost:8000`. Set `PORT` to change the listening port. For a WSGI deployment, use `gunicorn --preload --workers 2 --threads 4 --bind 0.0.0.0:${PORT:-8000} server:app` behind HTTPS. The standalone `python server.py` command is for local development/demo use.

### Demonstration accounts

All demo accounts use `BioTrack2026!`:

| Role | Email |
|---|---|
| Hospital Administrator | `admin@biotrack.ug` |
| Biomedical Engineer | `engineer@biotrack.ug` |
| Hospital Staff | `staff@biotrack.ug` |
| Biomedical Engineer (second assignee) | `engineer2@biotrack.ug` |

The seeded hospital and its sample records are clearly marked as demonstration data. Do not enter real patient-identifiable information.

## Tests

Run the seeded workflow suite with `python -m unittest discover -s tests -v`. The tests use a temporary SQLite database and cover authentication/role boundaries, tenant isolation, equipment QR, fault/repair safety gating, equipment-specific maintenance scheduling and verification, spare-part ledger concurrency, report exports, department onboarding, and BioAssist/manual workflows.

## Included workflows

- Server-side session authentication, password hashing, CSRF token checks, login throttling, HTTPS-aware secure cookies and hospital-scoped API queries. Account suspension, password resets and privilege changes revoke existing sessions; an administrator cannot change their own role.
- Hospital onboarding, department/user management, role-based navigation and an administrator audit trail.
- Equipment registration/editing, custom categories, image uploads, equipment service history and QR labels.
- Public QR lookup exposes only a limited equipment summary; reporting faults requires a signed-in hospital account and department permissions are checked server-side.
- Fault intake, severity, photos, engineer assignment, status transitions, linked work orders, repair notes and return-to-service documentation.
- Per-equipment preventive-maintenance intervals with no assumed default, a required manufacturer-manual or approved facility-policy reference, checklist templates, due-date tracking and completion history. Return-to-service records require documented verification.
- Spare-part inventory with an auditable, tenant-scoped movement ledger, opening-balance records, atomic receipt/issue transactions, reasoned signed stock corrections, and low-stock in-app notifications. Notifications are in-app; email delivery is not configured.
- Dashboard, inventory/condition/fault/maintenance/repair/monthly/parts report previews, PDF and XLSX exports, and hospital-scoped administrator SQLite snapshots (other tenants are excluded; uploaded files require a separate backup).
- PDF service-manual upload, equipment linking, optional per-question manual selection, local text search with filename/page references, and audited deletion of both the stored file and extracted text. A searchable PDF is required; scanned-image OCR is not configured.
- An OpenAI-compatible BioAssist integration point. Each response is labelled as manual-cited AI, general AI, manual-only excerpts, or an unavailable/error state. Without provider configuration, BioAssist reports that generated AI is unavailable and can return only matching manual excerpts; it does not pretend to have generated a response.
- Responsive web app shell/PWA assets and a small IndexedDB queue for plain fault reports entered during short connectivity loss. The server database remains the system of record.

## BioAssist provider configuration

Set these on the server process (never in browser JavaScript):

```bash
export BIOASSIST_API_KEY="your-provider-key"
export BIOASSIST_API_URL="https://api.openai.com/v1/chat/completions"  # optional
export BIOASSIST_MODEL="gpt-4o-mini"                                  # optional
```

The server sends the engineer's question, a limited selected-equipment record when one is chosen, the top matching manual excerpts in this hospital tenant, and optional tenant-scoped rows for a requested report. It does not send a full database dump. Review the provider's data-processing terms before using it with operational data. Responses are labelled as decision support; qualified engineers remain responsible for procedures, verification and clinical-use decisions.

## Security / deployment notes

This is a deployable **MVP foundation**, not a claim of regulatory approval or an independently security-audited clinical production service. Before handling live hospital records:

1. Use HTTPS and a managed secrets service. The session cookie is marked `Secure` when the request is HTTPS; set `COOKIE_SECURE=1` explicitly if a trusted TLS proxy does not forward the secure scheme. Set `COOKIE_SECURE=0` only for local HTTP development.
2. Place a production WSGI server (e.g. Gunicorn) behind a configured reverse proxy; do not use Flask's development server for production.
3. Configure a restrictive Content Security Policy and `frame-ancestors` for trusted deployment origins. The preview deliberately avoids frame restrictions so the hosted demo can render.
4. Move SQLite to a managed relational database for multi-process/high-concurrency deployments; configure automated encrypted backups, restore drills, monitoring and retention.
5. Add malware scanning, access review, document-retention controls and OCR only if approved. Apply a formal threat model and penetration test.
6. Have Uganda-based counsel and the facility's biomedical/clinical governance team verify applicable health-sector, medical-equipment, privacy and data-protection requirements. This software does not claim regulatory approval or compliance.
7. Do not upload patient-identifiable data. BioTrack stores maintenance and engineering records; avoid recording patient information in fault descriptions or photos.

Default demo state is stored in `data/biotrack.sqlite3`. To reset the demonstration dataset, stop the server and remove that file and `data/uploads/`; initialization will reseed the demo tenant on next start. Preserve/backup the database and uploads together when moving a real instance.
