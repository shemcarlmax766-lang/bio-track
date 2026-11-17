"""BioTrack database layer: schema, connection handling, and demo seeding.

Storage is a single SQLite file (WAL mode) at ``data/biotrack.sqlite3`` by
default. Every operational table carries a ``hospital_id`` so that tenants are
isolated from one another at the query layer.
"""

import datetime as dt
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BIOTRACK_DATA_DIR", os.path.join(BASE_DIR, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.environ.get("BIOTRACK_DB", os.path.join(DATA_DIR, "biotrack.sqlite3"))

DEMO_PASSWORD = "BioTrack2026!"

# Departments commonly found in Ugandan health facilities, used when a hospital
# is first provisioned. Administrators may add or rename departments afterwards.
DEFAULT_DEPARTMENTS = [
    "Accident & Emergency",
    "Outpatient Department (OPD)",
    "Intensive Care Unit (ICU)",
    "Neonatal Intensive Care Unit (NICU)",
    "Theatre / Surgery",
    "Maternity",
    "Paediatrics",
    "Radiology",
    "Laboratory",
    "Pharmacy",
    "Renal / Dialysis",
    "Dental",
    "Physiotherapy",
    "Central Sterile Services (CSSD)",
    "Biomedical Engineering Workshop",
]

DEFAULT_CATEGORIES = [
    "Patient monitors",
    "Infusion pumps",
    "Defibrillators",
    "ECG machines",
    "Ultrasound machines",
    "MRI scanners",
    "CT scanners",
    "X-ray machines",
    "Laboratory equipment",
    "Anaesthesia machines",
    "Ventilators",
    "Sterilisation equipment",
]

ROLES = [
    ("administrator", "Hospital Administrator"),
    ("engineer", "Biomedical Engineer"),
    ("staff", "Hospital Staff"),
]

EQUIPMENT_STATUSES = [
    "operational",
    "faulty",
    "under_repair",
    "awaiting_parts",
    "out_of_service",
    "retired",
]

FAULT_STATUSES = [
    "reported",
    "under_review",
    "assigned",
    "under_repair",
    "awaiting_spare_parts",
    "resolved",
    "closed",
]

SEVERITIES = ["low", "medium", "high", "critical"]

OBSTRUCTIVE_FAULT_STATUSES = ("reported", "under_review", "assigned", "under_repair", "awaiting_spare_parts")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS hospitals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE,
    district TEXT,
    timezone TEXT NOT NULL DEFAULT 'Africa/Kampala',
    currency TEXT NOT NULL DEFAULT 'UGX',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (hospital_id, name)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    role_id INTEGER NOT NULL REFERENCES roles(id),
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    phone TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    session_epoch INTEGER NOT NULL DEFAULT 1,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_login_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (hospital_id, name)
);

CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    category_id INTEGER REFERENCES equipment_categories(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    manufacturer TEXT,
    model_number TEXT,
    serial_number TEXT,
    asset_number TEXT,
    location TEXT,
    installation_date TEXT,
    purchase_date TEXT,
    warranty_expiry TEXT,
    purchase_cost_ugx REAL,
    status TEXT NOT NULL DEFAULT 'operational',
    assigned_engineer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    last_maintenance_date TEXT,
    next_maintenance_date TEXT,
    image_path TEXT,
    notes TEXT,
    qr_token TEXT NOT NULL UNIQUE,
    clinical_use_blocked INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT,
    blocked_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    blocked_at TEXT,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fault_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    reported_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reporter_name TEXT,
    description TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    photo_path TEXT,
    reported_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reported',
    assigned_engineer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    preliminary_findings TEXT,
    diagnosis TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    fault_id INTEGER REFERENCES fault_reports(id) ON DELETE SET NULL,
    assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'open',
    priority TEXT NOT NULL DEFAULT 'medium',
    summary TEXT,
    opened_at TEXT NOT NULL,
    started_at TEXT,
    closed_at TEXT,
    closed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    downtime_minutes INTEGER,
    safety_flag TEXT
);

CREATE TABLE IF NOT EXISTS repair_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    work_order_id INTEGER REFERENCES work_orders(id) ON DELETE SET NULL,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    engineer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    diagnosis TEXT,
    repair_performed TEXT,
    components_replaced TEXT,
    spare_parts_used TEXT,
    repair_date TEXT NOT NULL,
    duration_minutes INTEGER,
    downtime_minutes INTEGER,
    verification_notes TEXT,
    verified_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    verified_at TEXT,
    returned_to_service_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    frequency_days INTEGER NOT NULL,
    reference_type TEXT NOT NULL,
    reference TEXT NOT NULL,
    checklist TEXT,
    last_completed_date TEXT,
    next_due_date TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    schedule_id INTEGER REFERENCES maintenance_schedules(id) ON DELETE SET NULL,
    performed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    performed_at TEXT NOT NULL,
    checklist_results TEXT,
    notes TEXT,
    verified_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    verified_at TEXT,
    returned_to_service INTEGER NOT NULL DEFAULT 0,
    return_to_service_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spare_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    part_number TEXT NOT NULL,
    manufacturer TEXT,
    compatible_equipment TEXT,
    quantity INTEGER NOT NULL DEFAULT 0,
    min_stock INTEGER NOT NULL DEFAULT 0,
    supplier TEXT,
    purchase_date TEXT,
    storage_location TEXT,
    unit_cost_ugx REAL,
    created_at TEXT NOT NULL,
    UNIQUE (hospital_id, part_number)
);

CREATE TABLE IF NOT EXISTS spare_part_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES spare_parts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    quantity_delta INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason TEXT,
    work_order_id INTEGER REFERENCES work_orders(id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    equipment_id INTEGER REFERENCES equipment(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    mime TEXT,
    page_count INTEGER NOT NULL DEFAULT 0,
    uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES service_documents(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    link TEXT,
    dedupe_key TEXT,
    read_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER REFERENCES hospitals(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor_email TEXT,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id INTEGER,
    detail TEXT,
    ip TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_equipment_hospital ON equipment(hospital_id);
CREATE INDEX IF NOT EXISTS idx_faults_hospital ON fault_reports(hospital_id, status);
CREATE INDEX IF NOT EXISTS idx_wo_hospital ON work_orders(hospital_id, status);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(hospital_id, user_id, read_at);
CREATE INDEX IF NOT EXISTS idx_audit_hospital ON audit_logs(hospital_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pages_doc ON document_pages(document_id);
"""


def now_iso():
    """Current UTC timestamp (naive ISO, seconds precision)."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()


def today_iso():
    return dt.date.today().isoformat()


def new_token():
    return secrets.token_urlsafe(24)


def get_connection():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def transaction():
    """Serialised write transaction. Use for multi-statement invariants."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def init_db(seed=True):
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        for code, name in ROLES:
            conn.execute(
                "INSERT OR IGNORE INTO roles (code, name) VALUES (?, ?)", (code, name)
            )
        existing = conn.execute("SELECT COUNT(*) AS n FROM hospitals").fetchone()["n"]
    finally:
        conn.close()
    if seed and existing == 0:
        seed_demo()


def _hospital(conn, name, code, district):
    cur = conn.execute(
        "INSERT INTO hospitals (name, code, district, created_at) VALUES (?, ?, ?, ?)",
        (name, code, district, now_iso()),
    )
    return cur.lastrowid


def provision_hospital(conn, name, code, district=None):
    """Create a hospital with default departments and categories."""
    hid = _hospital(conn, name, code, district)
    for dept in DEFAULT_DEPARTMENTS:
        conn.execute(
            "INSERT INTO departments (hospital_id, name, created_at) VALUES (?, ?, ?)",
            (hid, dept, now_iso()),
        )
    for cat in DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT INTO equipment_categories (hospital_id, name, created_at) VALUES (?, ?, ?)",
            (hid, cat, now_iso()),
        )
    return hid


def _user(conn, hospital_id, role, name, email, department=None, password=DEMO_PASSWORD):
    role_id = conn.execute("SELECT id FROM roles WHERE code = ?", (role,)).fetchone()["id"]
    dept_id = None
    if department:
        row = conn.execute(
            "SELECT id FROM departments WHERE hospital_id = ? AND name = ?",
            (hospital_id, department),
        ).fetchone()
        dept_id = row["id"] if row else None
    cur = conn.execute(
        """INSERT INTO users (hospital_id, department_id, role_id, full_name, email,
                              password_hash, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (hospital_id, dept_id, role_id, name, email,
         generate_password_hash(password, method="pbkdf2:sha256"), now_iso()),
    )
    return cur.lastrowid


def seed_demo():
    """Create a demonstration tenant with sample equipment, faults and parts."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        hid = provision_hospital(
            conn, "Demo National Referral Hospital", "DEMO-NRH", "Kampala"
        )

        dept = {
            r["name"]: r["id"]
            for r in conn.execute(
                "SELECT id, name FROM departments WHERE hospital_id = ?", (hid,)
            )
        }
        cat = {
            r["name"]: r["id"]
            for r in conn.execute(
                "SELECT id, name FROM equipment_categories WHERE hospital_id = ?", (hid,)
            )
        }
        admin = _user(conn, hid, "administrator", "Sarah Nabukeera", "admin@biotrack.ug")
        eng = _user(conn, hid, "engineer", "Daniel Okello", "engineer@biotrack.ug",
                    "Biomedical Engineering Workshop")
        eng2 = _user(conn, hid, "engineer", "Grace Atim", "engineer2@biotrack.ug",
                     "Biomedical Engineering Workshop")
        staff = _user(conn, hid, "staff", "Peter Mugisha", "staff@biotrack.ug",
                      "Intensive Care Unit (ICU)")

        today = dt.date.today()
        equip_rows = [
            ("Infusion Pump", "Infusion pumps", "B. Braun", "Perfusor Space", "BB-IP-1042",
             "ICU-IP-014", "Intensive Care Unit (ICU)", "ICU Bay 3", "operational",
             -30, 180),
            ("Patient Monitor", "Patient monitors", "Mindray", "uMEC12", "MR-PM-7781",
             "ICU-PM-007", "Intensive Care Unit (ICU)", "ICU Bed 2", "faulty",
             -20, 12),
            ("Defibrillator", "Defibrillators", "Zoll", "R Series", "ZL-DF-3320",
             "A&E-DF-002", "Accident & Emergency", "Resus Room", "operational",
             -45, 240),
            ("Ventilator", "Ventilators", "Dräger", "Savina 300", "DR-VN-9011",
             "ICU-VN-001", "Intensive Care Unit (ICU)", "ICU Bed 1", "under_repair",
             -10, 5),
            ("Ultrasound Machine", "Ultrasound machines", "GE", "Voluson P8", "GE-US-5521",
             "RAD-US-003", "Radiology", "Ultrasound Room 1", "operational", -60, 90),
            ("X-ray Machine", "X-ray machines", "Siemens", "Multix Impact", "SM-XR-2201",
             "RAD-XR-001", "Radiology", "X-ray Room", "operational", -90, 30),
            ("Haematology Analyser", "Laboratory equipment", "Sysmex", "XN-550", "SY-LB-4410",
             "LAB-LB-004", "Laboratory", "Main Lab Bench 2", "awaiting_parts", -15, 7),
            ("Anaesthesia Machine", "Anaesthesia machines", "Dräger", "Fabius Plus", "DR-AN-1180",
             "THE-AN-001", "Theatre / Surgery", "Theatre 2", "operational", -40, 120),
            ("Autoclave", "Sterilisation equipment", "Tuttnauer", "3870EA", "TT-ST-6650",
             "CSSD-ST-003", "Central Sterile Services (CSSD)", "CSSD Bay 1", "operational",
             -25, 60),
            ("ECG Machine", "ECG machines", "Schiller", "Cardiovit AT-1", "SC-EC-7788",
             "OPD-EC-005", "Outpatient Department (OPD)", "OPD Room 4", "operational",
             -35, 200),
        ]
        equip_ids = {}
        for (name, category, mfr, model, serial, asset, dept_name, location,
             status, installed_days_ago, next_maint_days) in equip_rows:
            installed = (today + dt.timedelta(days=installed_days_ago)).isoformat()
            cur = conn.execute(
                """INSERT INTO equipment (hospital_id, department_id, category_id, name,
                        manufacturer, model_number, serial_number, asset_number, location,
                        installation_date, purchase_date, warranty_expiry, status,
                        assigned_engineer_id, last_maintenance_date, next_maintenance_date,
                        qr_token, notes, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?)""",
                (hid, dept.get(dept_name), cat.get(category), name, mfr, model, serial,
                 asset, location, installed, installed,
                 (today + dt.timedelta(days=365)).isoformat(), status,
                 eng if status != "operational" else eng,
                 (today - dt.timedelta(days=30)).isoformat(),
                 (today + dt.timedelta(days=next_maint_days)).isoformat(),
                 new_token(),
                 "Demonstration record. Not for clinical use.",
                 admin, now_iso(), now_iso()),
            )
            equip_ids[name] = cur.lastrowid

        # Fault history for the demo tenant.
        faults = [
            ("Patient Monitor", staff, "Screen flickers and SpO2 readings drop out "
             "intermittently during monitoring.", "high", "under_repair", eng),
            ("Ventilator", staff, "Ventilator alarms 'O2 supply failure' after 10 minutes "
             "of use. Removed from clinical use.", "critical", "assigned", eng),
            ("Haematology Analyser", staff, "Reagent aspiration error on startup; "
             "sampling probe suspected.", "medium", "awaiting_spare_parts", eng2),
        ]
        for name, reporter, desc, severity, status, engineer in faults:
            eid = equip_ids[name]
            row = conn.execute(
                "SELECT department_id FROM equipment WHERE id = ?", (eid,)
            ).fetchone()
            cur = conn.execute(
                """INSERT INTO fault_reports (hospital_id, equipment_id, department_id,
                        reported_by, reporter_name, description, severity, reported_at,
                        status, assigned_engineer_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (hid, eid, row["department_id"], reporter,
                 "Peter Mugisha" if reporter == staff else "Staff",
                 desc, severity,
                 (today - dt.timedelta(days=3)).isoformat() + "T09:15:00",
                 status, engineer, now_iso()),
            )
            fid = cur.lastrowid
            wo_status = "in_progress" if status == "under_repair" else "open"
            conn.execute(
                """INSERT INTO work_orders (hospital_id, equipment_id, fault_id,
                        assigned_to, status, priority, summary, opened_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (hid, eid, fid, engineer, wo_status, severity,
                 f"Repair work order for {name}",
                 (today - dt.timedelta(days=3)).isoformat() + "T10:00:00"),
            )

        # Preventive maintenance schedule with an explicit reference.
        conn.execute(
            """INSERT INTO maintenance_schedules (hospital_id, equipment_id, name,
                    frequency_days, reference_type, reference, checklist, next_due_date,
                    created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hid, equip_ids["Ventilator"], "Quarterly performance verification", 90,
             "manufacturer_manual",
             "Dräger Savina 300 Service Manual, section 7.2 (demo reference)",
             json.dumps(["Check O2 sensor calibration",
                         "Verify alarm limits",
                         "Inspect breathing circuit",
                         "Run leak test"]),
             (today + dt.timedelta(days=5)).isoformat(), eng, now_iso()),
        )

        parts = [
            ("SpO2 Sensor (adult)", "SPO2-ADT-01", "Mindray", "Patient monitors",
             4, 2, "Mediquip Uganda", "Shelf A2", 850000),
            ("Reagent Probe Assembly", "RGT-PRB-77", "Sysmex",
             "Haematology analysers", 0, 1, "Lab Supply EA", "Shelf C1", 2400000),
            ("O2 Sensor Cell", "O2-CELL-300", "Dräger", "Ventilators / Anaesthesia",
             1, 2, "Mediquip Uganda", "Shelf B3", 1200000),
            ("Infusion Pump Battery Pack", "BAT-IP-02", "B. Braun", "Infusion pumps",
             6, 3, "Biomedical Workshop", "Shelf D4", 320000),
        ]
        for (name, pn, mfr, compatible, qty, min_stock, supplier, loc, cost) in parts:
            cur = conn.execute(
                """INSERT INTO spare_parts (hospital_id, name, part_number, manufacturer,
                        compatible_equipment, quantity, min_stock, supplier, purchase_date,
                        storage_location, unit_cost_ugx, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (hid, name, pn, mfr, compatible, qty, min_stock, supplier,
                 (today - dt.timedelta(days=60)).isoformat(), loc, cost, now_iso()),
            )
            conn.execute(
                """INSERT INTO spare_part_transactions (hospital_id, part_id, kind,
                        quantity_delta, balance_after, reason, created_at)
                   VALUES (?, ?, 'opening', ?, ?, 'Opening balance (demo)', ?)""",
                (hid, cur.lastrowid, qty, qty, now_iso()),
            )

        conn.execute(
            """INSERT INTO audit_logs (hospital_id, user_id, actor_email, action, entity,
                    detail, created_at)
               VALUES (?, ?, ?, 'seed', 'hospital', 'Demonstration dataset created', ?)""",
            (hid, admin, "admin@biotrack.ug", now_iso()),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
