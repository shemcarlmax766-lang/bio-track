"""End-to-end workflow tests.

The suite runs against a throw-away SQLite database and temporary upload folder,
so it never touches the demo dataset in ``data/``.

Run with:  python -m unittest discover -s tests -v
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

TMP = tempfile.mkdtemp(prefix="biotrack-tests-")
os.environ["BIOTRACK_DATA_DIR"] = TMP
os.environ["BIOTRACK_DB"] = os.path.join(TMP, "test.sqlite3")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ.pop("BIOASSIST_API_KEY", None)

from app import create_app  # noqa: E402
import db as dbmod  # noqa: E402


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        self.client = self.app.test_client()

    # -- helpers ---------------------------------------------------------
    def csrf(self, path="/login"):
        html = self.client.get(path).get_data(as_text=True)
        match = re.search(r'name="_csrf" value="([^"]+)"', html)
        return match.group(1) if match else ""

    def login(self, email, password=dbmod.DEMO_PASSWORD):
        token = self.csrf()
        response = self.client.post(
            "/login", data={"email": email, "password": password, "_csrf": token}
        )
        self.assertEqual(response.status_code, 302, f"login failed for {email}")
        return response

    def token(self, path="/"):
        return self.csrf(path)

    def post(self, path, data=None, follow=False):
        payload = dict(data or {})
        payload.setdefault("_csrf", self.token(path if path.endswith("/new") else "/"))
        return self.client.post(path, data=payload, follow_redirects=follow)

    def db(self):
        return dbmod.get_connection()

    def hospital_id(self, code="DEMO-NRH"):
        conn = self.db()
        try:
            return conn.execute(
                "SELECT id FROM hospitals WHERE code = ?", (code,)
            ).fetchone()["id"]
        finally:
            conn.close()


class AuthenticationTests(BaseCase):
    def test_login_required_redirects(self):
        response = self.client.get("/equipment")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_login_with_wrong_password_is_rejected(self):
        token = self.csrf()
        response = self.client.post(
            "/login",
            data={"email": "admin@biotrack.ug", "password": "wrong", "_csrf": token},
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn(b"incorrect", response.data)

    def test_csrf_token_is_required_for_posts(self):
        response = self.client.post("/login", data={"email": "admin@biotrack.ug"})
        self.assertEqual(response.status_code, 400)

    def test_successful_login_reaches_dashboard(self):
        self.login("admin@biotrack.ug")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Dashboard", response.data)

    def test_logout_clears_session(self):
        self.login("staff@biotrack.ug")
        response = self.post("/logout", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/equipment").status_code, 302)

    def test_session_revoked_when_account_suspended(self):
        """Suspending an account must invalidate its existing cookie-based session."""
        admin = self.app.test_client()
        token = re.search(
            r'name="_csrf" value="([^"]+)"',
            admin.get("/login").get_data(as_text=True),
        ).group(1)
        admin.post(
            "/login",
            data={"email": "admin@biotrack.ug", "password": dbmod.DEMO_PASSWORD,
                  "_csrf": token},
        )
        email = f"suspend-{os.urandom(3).hex()}@biotrack.ug"
        page = admin.get("/admin/users").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        admin.post(
            "/admin/users",
            data={"_csrf": token, "full_name": "Suspension Check", "email": email,
                  "password": "TempPassword2026!", "role_code": "staff"},
        )
        conn = self.db()
        try:
            target = conn.execute(
                "SELECT id FROM users WHERE email = ?", (email,)
            ).fetchone()["id"]
        finally:
            conn.close()

        staff_client = self.app.test_client()
        token = re.search(
            r'name="_csrf" value="([^"]+)"',
            staff_client.get("/login").get_data(as_text=True),
        ).group(1)
        staff_client.post(
            "/login",
            data={"email": email, "password": "TempPassword2026!", "_csrf": token},
        )
        self.assertEqual(staff_client.get("/").status_code, 200)

        page = admin.get("/admin/users").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        response = admin.post(f"/admin/users/{target}/active", data={"_csrf": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(staff_client.get("/").status_code, 302)


class RoleBoundaryTests(BaseCase):
    def test_staff_cannot_register_equipment(self):
        self.login("staff@biotrack.ug")
        self.assertEqual(self.client.get("/equipment/new").status_code, 403)

    def test_staff_cannot_open_maintenance_or_admin(self):
        self.login("staff@biotrack.ug")
        for path in ("/maintenance", "/parts", "/admin/users", "/bioassist", "/backup"):
            self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_engineer_cannot_manage_users(self):
        self.login("engineer@biotrack.ug")
        self.assertEqual(self.client.get("/admin/users").status_code, 403)
        self.assertEqual(self.client.get("/equipment/new").status_code, 200)

    def test_administrator_cannot_change_own_role(self):
        self.login("admin@biotrack.ug")
        conn = self.db()
        try:
            admin = conn.execute(
                "SELECT id FROM users WHERE email = 'admin@biotrack.ug'"
            ).fetchone()["id"]
        finally:
            conn.close()
        token = self.csrf("/admin/users")
        self.client.post(
            f"/admin/users/{admin}/update",
            data={"_csrf": token, "role_code": "engineer", "full_name": "Sarah Nabukeera"},
            follow_redirects=True,
        )
        conn = self.db()
        try:
            role = conn.execute(
                """SELECT r.code FROM users u JOIN roles r ON r.id = u.role_id
                    WHERE u.id = ?""",
                (admin,),
            ).fetchone()["code"]
        finally:
            conn.close()
        self.assertEqual(role, "administrator")


class TenantIsolationTests(BaseCase):
    def _create_other_hospital(self):
        import uuid

        suffix = uuid.uuid4().hex[:8]
        name = f"Isolation Test Hospital {suffix}"
        email = f"other-{suffix}@isolation.ug"
        client = self.app.test_client()
        token = re.search(
            r'name="_csrf" value="([^"]+)"',
            client.get("/register-hospital").get_data(as_text=True),
        ).group(1)
        response = client.post(
            "/register-hospital",
            data={
                "_csrf": token,
                "hospital_name": name,
                "district": "Mbarara",
                "full_name": "Other Admin",
                "email": email,
                "password": "AnotherPass2026!",
                "password_confirm": "AnotherPass2026!",
            },
        )
        self.assertEqual(response.status_code, 302)
        self._email = email
        return client

    def test_new_hospital_gets_isolated_workspace(self):
        client = self._create_other_hospital()
        token = re.search(
            r'name="_csrf" value="([^"]+)"',
            client.get("/login").get_data(as_text=True),
        ).group(1)
        client.post(
            "/login",
            data={"email": self._email, "password": "AnotherPass2026!",
                  "_csrf": token},
        )
        page = client.get("/equipment").get_data(as_text=True)
        self.assertNotIn("Infusion Pump", page)
        self.assertIn("No equipment matches", page)
        # demo equipment ids belong to the other tenant
        self.assertIn(client.get("/equipment/1").status_code, (403, 404))

    def test_other_tenant_cannot_report_against_demo_equipment(self):
        other = self._create_other_hospital()
        token = re.search(
            r'name="_csrf" value="([^"]+)"',
            other.get("/login").get_data(as_text=True),
        ).group(1)
        other.post(
            "/login",
            data={"email": self._email, "password": "AnotherPass2026!",
                  "_csrf": token},
        )
        token = re.search(
            r'name="_csrf" value="([^"]+)"',
            other.get("/faults/new").get_data(as_text=True),
        ).group(1)
        response = other.post(
            "/faults/new",
            data={"_csrf": token, "equipment_id": "1",
                  "description": "Cross tenant attempt on demo equipment.",
                  "severity": "high"},
        )
        self.assertEqual(response.status_code, 400)


class EquipmentTests(BaseCase):
    def test_register_equipment_generates_qr_token(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/equipment/new")
        response = self.client.post(
            "/equipment/new",
            data={
                "_csrf": token, "name": "Syringe Pump", "manufacturer": "Fresenius",
                "model_number": "Injectomat", "asset_number": "TEST-SP-900",
                "location": "ICU Bay 9", "status": "operational",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT id, qr_token FROM equipment WHERE asset_number = 'TEST-SP-900'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertTrue(len(row["qr_token"]) > 10)
        qr = self.client.get(f"/equipment/{row['id']}/qr.png")
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.mimetype, "image/png")

    def test_edit_form_renders_and_saves(self):
        self.login("engineer@biotrack.ug")
        page = self.client.get("/equipment/1/edit")
        self.assertEqual(page.status_code, 200)
        token = re.search(
            r'name="_csrf" value="([^"]+)"', page.get_data(as_text=True)
        ).group(1)
        response = self.client.post(
            "/equipment/1/edit",
            data={
                "_csrf": token, "name": "Infusion Pump (edited)",
                "asset_number": "ICU-IP-014", "status": "operational",
                "location": "ICU Bay 4",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT name, location FROM equipment WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["name"], "Infusion Pump (edited)")
        self.assertEqual(row["location"], "ICU Bay 4")

    def test_duplicate_asset_number_is_rejected(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/equipment/new")
        response = self.client.post(
            "/equipment/new",
            data={"_csrf": token, "name": "Duplicate", "asset_number": "ICU-PM-007"},
        )
        self.assertEqual(response.status_code, 400)

    def test_public_qr_shows_limited_summary_only(self):
        conn = self.db()
        try:
            row = conn.execute(
                """SELECT qr_token, serial_number FROM equipment
                    WHERE serial_number IS NOT NULL AND serial_number != ''
                    ORDER BY id LIMIT 1"""
            ).fetchone()
        finally:
            conn.close()
        page = self.client.get(f"/q/{row['qr_token']}").get_data(as_text=True)
        self.assertEqual(self.client.get(f"/q/{row['qr_token']}").status_code, 200)
        self.assertIn("limited summary", page)
        self.assertNotIn(row["serial_number"], page)

    def test_staff_only_see_their_department(self):
        self.login("staff@biotrack.ug")
        page = self.client.get("/equipment").get_data(as_text=True)
        body = page.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        self.assertIn("Patient Monitor", body)
        self.assertNotIn("Ultrasound Machine", body)
        self.assertNotIn("X-ray Machine", body)
        # the department filter is narrowed too
        self.assertNotIn('<option value="8" >Radiology</option>', page)

    def test_engineer_can_flag_equipment_unsafe(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/equipment/1")
        self.client.post(
            "/equipment/1/clinical-use",
            data={"_csrf": token, "action": "block", "reason": "Earth leakage test failed"},
            follow_redirects=True,
        )
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT clinical_use_blocked, status FROM equipment WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["clinical_use_blocked"], 1)
        self.assertEqual(row["status"], "out_of_service")


class FaultWorkflowTests(BaseCase):
    def test_staff_report_fault_and_engineer_resolves_it(self):
        # 1. staff reports a fault
        self.login("staff@biotrack.ug")
        conn = self.db()
        try:
            equipment = conn.execute(
                """SELECT e.id FROM equipment e JOIN departments d ON d.id = e.department_id
                    WHERE d.name = 'Intensive Care Unit (ICU)' AND e.name = 'Patient Monitor'"""
            ).fetchone()
        finally:
            conn.close()
        token = self.csrf("/faults/new")
        response = self.client.post(
            "/faults/new",
            data={
                "_csrf": token, "equipment_id": equipment["id"],
                "description": "Pump beeps continuously and the occlusion alarm will not reset.",
                "severity": "high",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        conn = self.db()
        try:
            fault = conn.execute(
                "SELECT * FROM fault_reports WHERE description LIKE 'Pump beeps%'"
            ).fetchone()
            status = conn.execute(
                "SELECT status FROM equipment WHERE id = ?", (equipment["id"],)
            ).fetchone()["status"]
            notification = conn.execute(
                """SELECT COUNT(*) AS n FROM notifications WHERE kind = 'fault_reported'"""
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(fault["status"], "reported")
        self.assertEqual(status, "faulty")
        self.assertGreater(notification, 0)

        # 2. engineer assigns and repairs
        client = self.app.test_client()
        t = re.search(r'name="_csrf" value="([^"]+)"',
                      client.get("/login").get_data(as_text=True)).group(1)
        client.post("/login", data={"email": "engineer@biotrack.ug",
                                    "password": dbmod.DEMO_PASSWORD, "_csrf": t})
        conn = self.db()
        try:
            engineer = conn.execute(
                "SELECT id FROM users WHERE email = 'engineer@biotrack.ug'"
            ).fetchone()["id"]
        finally:
            conn.close()
        page = client.get(f"/faults/{fault['id']}").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        client.post(
            f"/faults/{fault['id']}/assign",
            data={"_csrf": token, "assigned_engineer_id": engineer},
        )
        client.post(
            f"/faults/{fault['id']}/status",
            data={"_csrf": token, "status": "under_repair"},
        )
        # a repair without verification must not return equipment to service
        client.post(
            f"/faults/{fault['id']}/repair",
            data={
                "_csrf": token, "diagnosis": "Occlusion sensor failure",
                "repair_performed": "Replaced occlusion sensor and recalibrated",
                "duration_minutes": "45",
            },
        )
        conn = self.db()
        try:
            status = conn.execute(
                "SELECT status FROM equipment WHERE id = ?", (equipment["id"],)
            ).fetchone()["status"]
            repair = conn.execute(
                "SELECT * FROM repair_records WHERE equipment_id = ?",
                (equipment["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(status, "out_of_service")
        self.assertIsNone(repair["verified_at"])

        # 3. verified repair with return to service
        client.post(
            f"/faults/{fault['id']}/repair",
            data={
                "_csrf": token, "diagnosis": "Occlusion sensor failure",
                "repair_performed": "Replaced sensor, flow test passed within limits",
                "verification_notes": "Flow accuracy 2%, within +/-5% specification",
                "return_to_service": "1",
            },
        )
        conn = self.db()
        try:
            status = conn.execute(
                "SELECT status FROM equipment WHERE id = ?", (equipment["id"],)
            ).fetchone()["status"]
            fault_status = conn.execute(
                "SELECT status FROM fault_reports WHERE id = ?", (fault["id"],)
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(status, "operational")
        self.assertEqual(fault_status, "resolved")

    def test_return_to_service_requires_verification(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/faults/1")
        before = self.db()
        try:
            count = before.execute(
                "SELECT COUNT(*) AS n FROM repair_records WHERE equipment_id = 2"
            ).fetchone()["n"]
        finally:
            before.close()
        self.client.post(
            "/faults/1/repair",
            data={
                "_csrf": token, "diagnosis": "Sensor",
                "repair_performed": "Replaced sensor",
                "return_to_service": "1",
            },
        )
        after = self.db()
        try:
            count_after = after.execute(
                "SELECT COUNT(*) AS n FROM repair_records WHERE equipment_id = 2"
            ).fetchone()["n"]
        finally:
            after.close()
        self.assertEqual(count, count_after)

    def test_closing_a_fault_requires_a_note(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/faults/2")
        self.client.post(
            "/faults/2/status", data={"_csrf": token, "status": "closed"}
        )
        conn = self.db()
        try:
            status = conn.execute(
                "SELECT status FROM fault_reports WHERE id = 2"
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertNotEqual(status, "closed")

    def test_staff_cannot_change_fault_status(self):
        self.login("staff@biotrack.ug")
        token = self.csrf("/")
        response = self.client.post(
            "/faults/1/status", data={"_csrf": token, "status": "closed",
                                      "note": "attempt"}
        )
        self.assertEqual(response.status_code, 403)


class MaintenanceTests(BaseCase):
    def test_schedule_requires_manual_or_policy_reference(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/maintenance")
        before = self.db()
        try:
            n = before.execute("SELECT COUNT(*) AS n FROM maintenance_schedules").fetchone()["n"]
        finally:
            before.close()
        response = self.client.post(
            "/maintenance/schedules",
            data={
                "_csrf": token, "equipment_id": "1", "name": "No reference",
                "frequency_days": "90", "reference_type": "manufacturer_manual",
                "reference": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        after = self.db()
        try:
            n_after = after.execute(
                "SELECT COUNT(*) AS n FROM maintenance_schedules"
            ).fetchone()["n"]
        finally:
            after.close()
        self.assertEqual(n, n_after)

    def test_interval_must_be_configured_not_assumed(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/maintenance")
        response = self.client.post(
            "/maintenance/schedules",
            data={
                "_csrf": token, "equipment_id": "1", "name": "No interval",
                "frequency_days": "", "reference_type": "facility_policy",
                "reference": "Biomed policy 2024-04",
            },
            follow_redirects=True,
        )
        self.assertIn(b"interval", response.data.lower())

    def test_completing_maintenance_rolls_next_due_date(self):
        self.login("engineer@biotrack.ug")
        conn = self.db()
        try:
            schedule = conn.execute(
                "SELECT * FROM maintenance_schedules ORDER BY id LIMIT 1"
            ).fetchone()
            checklist = json.loads(schedule["checklist"] or "[]")
        finally:
            conn.close()
        data = {"_csrf": self.csrf("/maintenance"), "performed_at": "2026-01-05",
                "notes": "All checks passed"}
        for i in range(len(checklist)):
            data[f"check_{i}"] = "pass"
        self.client.post(
            f"/maintenance/schedules/{schedule['id']}/complete", data=data,
            follow_redirects=True,
        )
        conn = self.db()
        try:
            updated = conn.execute(
                "SELECT * FROM maintenance_schedules WHERE id = ?", (schedule["id"],)
            ).fetchone()
            record = conn.execute(
                "SELECT * FROM maintenance_records WHERE schedule_id = ?",
                (schedule["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(record)
        self.assertEqual(updated["last_completed_date"], "2026-01-05")
        expected = dbmod.dt.date(2026, 1, 5) + dbmod.dt.timedelta(
            days=schedule["frequency_days"]
        )
        self.assertEqual(updated["next_due_date"], expected.isoformat())


class SparePartTests(BaseCase):
    def test_issue_cannot_drive_stock_negative(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/parts")
        conn = self.db()
        try:
            part = conn.execute(
                "SELECT * FROM spare_parts WHERE part_number = 'O2-CELL-300'"
            ).fetchone()
        finally:
            conn.close()
        self.client.post(
            f"/parts/{part['id']}/movement",
            data={"_csrf": token, "kind": "issue", "quantity": "99",
                  "reason": "Attempt to over-issue"},
            follow_redirects=True,
        )
        conn = self.db()
        try:
            after = conn.execute(
                "SELECT quantity FROM spare_parts WHERE id = ?", (part["id"],)
            ).fetchone()["quantity"]
        finally:
            conn.close()
        self.assertEqual(after, part["quantity"])

    def test_receipt_and_issue_write_ledger_balances(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/parts")
        conn = self.db()
        try:
            part = conn.execute(
                "SELECT * FROM spare_parts WHERE part_number = 'SPO2-ADT-01'"
            ).fetchone()
        finally:
            conn.close()
        self.client.post(
            f"/parts/{part['id']}/movement",
            data={"_csrf": token, "kind": "receipt", "quantity": "5",
                  "reason": "Delivery note 4412"},
            follow_redirects=True,
        )
        self.client.post(
            f"/parts/{part['id']}/movement",
            data={"_csrf": token, "kind": "issue", "quantity": "2",
                  "reason": "Used on ICU monitor repair"},
            follow_redirects=True,
        )
        conn = self.db()
        try:
            part_after = conn.execute(
                "SELECT quantity FROM spare_parts WHERE id = ?", (part["id"],)
            ).fetchone()["quantity"]
            ledger = conn.execute(
                """SELECT kind, quantity_delta, balance_after FROM spare_part_transactions
                    WHERE part_id = ? ORDER BY id""",
                (part["id"],),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(part_after, part["quantity"] + 3)
        self.assertEqual(ledger[-1]["balance_after"], part_after)
        self.assertEqual(ledger[-1]["kind"], "issue")

    def test_low_stock_raises_a_notification(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/parts")
        conn = self.db()
        try:
            part = conn.execute(
                "SELECT * FROM spare_parts WHERE part_number = 'BAT-IP-02'"
            ).fetchone()
        finally:
            conn.close()
        self.client.post(
            f"/parts/{part['id']}/movement",
            data={"_csrf": token, "kind": "issue", "quantity": "3",
                  "reason": "Fitted to infusion pumps"},
            follow_redirects=True,
        )
        conn = self.db()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE kind = 'low_stock'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertGreater(n, 0)


class ReportingTests(BaseCase):
    def test_pdf_and_excel_exports(self):
        self.login("admin@biotrack.ug")
        pdf = self.client.get("/reports/inventory.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        xlsx = self.client.get("/reports/faults.xlsx")
        self.assertEqual(xlsx.status_code, 200)
        self.assertTrue(xlsx.data.startswith(b"PK"))

    def test_staff_cannot_export_maintenance_history(self):
        self.login("staff@biotrack.ug")
        self.assertEqual(self.client.get("/reports/maintenance.pdf").status_code, 403)
        self.assertEqual(self.client.get("/reports/faults").status_code, 200)

    def test_backup_is_scoped_to_the_hospital(self):
        self.login("admin@biotrack.ug")
        response = self.client.get("/backup")
        self.assertEqual(response.status_code, 200)
        path = os.path.join(TMP, "backup-check.sqlite3")
        with open(path, "wb") as fh:
            fh.write(response.data)
        import sqlite3

        conn = sqlite3.connect(path)
        try:
            hospitals = conn.execute("SELECT COUNT(*) FROM hospitals").fetchone()[0]
            equipment = conn.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(hospitals, 1)
        self.assertGreater(equipment, 0)


class BioAssistTests(BaseCase):
    def test_without_provider_public_answer_is_refused(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/bioassist")
        response = self.client.post(
            "/api/bioassist/ask",
            data=json.dumps({"question": "Ventilator shows O2 supply failure"}),
            headers={"Content-Type": "application/json", "X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn(payload["mode"], ("provider_error", "manual_only"))
        self.assertIn("does not replace", payload["disclaimer"])

    def test_staff_cannot_use_bioassist(self):
        self.login("staff@biotrack.ug")
        token = self.csrf("/")
        response = self.client.post(
            "/api/bioassist/ask",
            data=json.dumps({"question": "How do I service a ventilator?"}),
            headers={"Content-Type": "application/json", "X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 403)

    def test_manual_upload_indexes_text_and_deletion_removes_it(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/documents")
        pdf = self._make_pdf("Replacing the O2 sensor cell requires a calibration check.")
        response = self.client.post(
            "/documents/upload",
            data={
                "_csrf": token, "title": "Test Service Manual", "equipment_id": "4",
                "manual": (io.BytesIO(pdf), "manual.pdf"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        conn = self.db()
        try:
            doc = conn.execute(
                "SELECT * FROM service_documents WHERE title = 'Test Service Manual'"
            ).fetchone()
            pages = conn.execute(
                "SELECT COUNT(*) AS n FROM document_pages WHERE document_id = ?",
                (doc["id"],),
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertIsNotNone(doc)
        self.assertGreaterEqual(pages, 1)

        token = self.csrf("/documents")
        self.client.post(f"/documents/{doc['id']}/delete", data={"_csrf": token})
        conn = self.db()
        try:
            gone = conn.execute(
                "SELECT COUNT(*) AS n FROM service_documents WHERE id = ?", (doc["id"],)
            ).fetchone()["n"]
            orphan = conn.execute(
                "SELECT COUNT(*) AS n FROM document_pages WHERE document_id = ?",
                (doc["id"],),
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(gone, 0)
        self.assertEqual(orphan, 0)

    @staticmethod
    def _make_pdf(text):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        y = 780
        for line in text.split(". "):
            c.drawString(60, y, line.strip()[:90])
            y -= 20
        c.showPage()
        c.save()
        return buf.getvalue()


class UploadSafetyTests(BaseCase):
    def test_non_pdf_upload_rejected(self):
        self.login("engineer@biotrack.ug")
        token = self.csrf("/documents")
        response = self.client.post(
            "/documents/upload",
            data={"_csrf": token, "title": "Bad file",
                  "manual": (io.BytesIO(b"MZ\x90\x00malware"), "evil.pdf")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"Only PDF", response.data)

    def test_fake_image_rejected(self):
        self.login("staff@biotrack.ug")
        token = self.csrf("/faults/new")
        response = self.client.post(
            "/faults/new",
            data={"_csrf": token, "equipment_id": "2",
                  "description": "Screen goes blank after two minutes of use.",
                  "severity": "medium",
                  "photo": (io.BytesIO(b"not-an-image"), "photo.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Only JPEG", response.data)


class AuditTests(BaseCase):
    def test_actions_are_audited(self):
        self.login("admin@biotrack.ug")
        token = self.csrf("/admin/departments")
        self.client.post("/admin/departments", data={"_csrf": token, "name": "Burns Unit"})
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT * FROM audit_logs WHERE action = 'department.create' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["detail"], "Burns Unit")


if __name__ == "__main__":
    unittest.main()
