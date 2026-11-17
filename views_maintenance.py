"""Preventive maintenance schedules and completion records.

No interval is assumed for any equipment. A schedule may only be created with an
explicit manufacturer-manual or approved facility-policy reference.
"""

import json
from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

import core
from core import (
    audit,
    current_user,
    engineer_or_admin,
    execute,
    form,
    form_int,
    query,
)
from db import now_iso

bp = Blueprint("maintenance", __name__)

REFERENCE_TYPES = {
    "manufacturer_manual": "Manufacturer manual / service documentation",
    "facility_policy": "Approved facility policy",
}


def _schedule_or_404(sid, hospital_id):
    row = query(
        """SELECT s.*, e.name AS equipment_name, e.asset_number, e.location,
                  d.name AS department_name
             FROM maintenance_schedules s
             JOIN equipment e ON e.id = s.equipment_id
             LEFT JOIN departments d ON d.id = e.department_id
            WHERE s.id = ? AND s.hospital_id = ?""",
        (sid, hospital_id),
        one=True,
    )
    if row is None:
        abort(404)
    return row


@bp.get("/maintenance")
@engineer_or_admin
def maintenance_list():
    hospital_id = current_user()["hospital_id"]
    rows = query(
        """SELECT s.*, e.name AS equipment_name, e.asset_number,
                  d.name AS department_name,
                  CASE WHEN s.next_due_date < ? THEN 1 ELSE 0 END AS overdue
             FROM maintenance_schedules s
             JOIN equipment e ON e.id = s.equipment_id
             LEFT JOIN departments d ON d.id = e.department_id
            WHERE s.hospital_id = ? AND s.is_active = 1
            ORDER BY overdue DESC, s.next_due_date""",
        (date.today().isoformat(), hospital_id),
    )
    completed = query(
        """SELECT m.*, e.name AS equipment_name, u.full_name AS engineer_name,
                  s.name AS schedule_name
             FROM maintenance_records m
             JOIN equipment e ON e.id = m.equipment_id
             LEFT JOIN users u ON u.id = m.performed_by
             LEFT JOIN maintenance_schedules s ON s.id = m.schedule_id
            WHERE m.hospital_id = ? ORDER BY m.performed_at DESC LIMIT 30""",
        (hospital_id,),
    )
    equipment = query(
        """SELECT id, name, asset_number FROM equipment
            WHERE hospital_id = ? AND status != 'retired' ORDER BY name""",
        (hospital_id,),
    )
    return render_template(
        "maintenance_list.html", schedules=rows, completed=completed,
        equipment=equipment, reference_types=REFERENCE_TYPES,
        today=date.today().isoformat(),
    )


@bp.post("/maintenance/schedules")
@engineer_or_admin
def schedule_create():
    user = current_user()
    hospital_id = user["hospital_id"]
    equipment_id = form_int("equipment_id")
    name = form("name") or "Preventive maintenance"
    frequency = form_int("frequency_days")
    reference_type = form("reference_type")
    reference = form("reference")
    start_date = form("next_due_date") or date.today().isoformat()
    checklist_raw = form("checklist")

    errors = []
    equipment = query(
        "SELECT * FROM equipment WHERE id = ? AND hospital_id = ?",
        (equipment_id, hospital_id),
        one=True,
    )
    if equipment is None:
        errors.append("Select equipment from this hospital.")
    if not frequency or frequency < 1 or frequency > 3650:
        errors.append("Enter a maintenance interval in days (1-3650). Intervals are "
                      "not assumed; use the manufacturer recommendation.")
    if reference_type not in REFERENCE_TYPES:
        errors.append("State whether the interval comes from the manufacturer manual "
                      "or an approved facility policy.")
    if len(reference) < 5:
        errors.append("Record the manual section or approved policy reference "
                      "(at least 5 characters).")
    try:
        date.fromisoformat(start_date)
    except ValueError:
        errors.append("Enter a valid first due date.")
    if errors:
        core.flash_errors(errors)
        return redirect(url_for("maintenance.maintenance_list"))

    checklist = [line.strip() for line in checklist_raw.splitlines() if line.strip()]
    sid = execute(
        """INSERT INTO maintenance_schedules (hospital_id, equipment_id, name,
                frequency_days, reference_type, reference, checklist, next_due_date,
                created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (hospital_id, equipment_id, name, frequency, reference_type, reference,
         json.dumps(checklist), start_date, user["id"], now_iso()),
    )
    execute(
        "UPDATE equipment SET next_maintenance_date = ?, updated_at = ? WHERE id = ?",
        (start_date, now_iso(), equipment_id),
    )
    audit("maintenance.schedule_create", "maintenance_schedule", sid,
          f"{name} every {frequency} days; reference: {reference}")
    flash("Maintenance schedule created.", "success")
    return redirect(url_for("maintenance.schedule_detail", sid=sid))


@bp.get("/maintenance/schedules/<int:sid>")
@engineer_or_admin
def schedule_detail(sid):
    user = current_user()
    schedule = _schedule_or_404(sid, user["hospital_id"])
    checklist = json.loads(schedule["checklist"] or "[]")
    history = query(
        """SELECT m.*, u.full_name AS engineer_name, v.full_name AS verifier
             FROM maintenance_records m
             LEFT JOIN users u ON u.id = m.performed_by
             LEFT JOIN users v ON v.id = m.verified_by
            WHERE m.schedule_id = ? AND m.hospital_id = ?
            ORDER BY m.performed_at DESC""",
        (sid, user["hospital_id"]),
    )
    return render_template(
        "maintenance_detail.html", schedule=schedule, checklist=checklist,
        history=history, reference_types=REFERENCE_TYPES,
    )


@bp.post("/maintenance/schedules/<int:sid>/complete")
@engineer_or_admin
def schedule_complete(sid):
    user = current_user()
    hospital_id = user["hospital_id"]
    schedule = _schedule_or_404(sid, hospital_id)
    checklist = json.loads(schedule["checklist"] or "[]")
    results = {}
    for index, item in enumerate(checklist):
        results[item] = request.form.get(f"check_{index}", "not_checked")
    notes = form("notes")
    performed = form("performed_at") or now_iso()
    verification = form("verification_notes")
    return_to_service = form("return_to_service") == "1"

    errors = []
    if checklist and any(v == "not_checked" for v in results.values()):
        errors.append("Record a result for every checklist item before completing.")
    if any(v == "fail" for v in results.values()) and not notes:
        errors.append("A failed checklist item needs notes describing the follow-up.")
    if return_to_service and not verification:
        errors.append("Return-to-service requires documented verification.")
    if errors:
        core.flash_errors(errors)
        return redirect(url_for("maintenance.schedule_detail", sid=sid))

    record_id = execute(
        """INSERT INTO maintenance_records (hospital_id, equipment_id, schedule_id,
                performed_by, performed_at, checklist_results, notes, verified_by,
                verified_at, returned_to_service, return_to_service_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (hospital_id, schedule["equipment_id"], sid, user["id"], performed,
         json.dumps(results), notes,
         user["id"] if return_to_service else None,
         now_iso() if return_to_service else None,
         1 if return_to_service else 0,
         now_iso() if return_to_service else None, now_iso()),
    )
    next_due = _add_days(performed, schedule["frequency_days"])
    execute(
        """UPDATE maintenance_schedules SET last_completed_date = ?, next_due_date = ?
            WHERE id = ? AND hospital_id = ?""",
        (performed[:10], next_due, sid, hospital_id),
    )
    execute(
        "UPDATE equipment SET last_maintenance_date = ?, next_maintenance_date = ?, "
        "updated_at = ? WHERE id = ? AND hospital_id = ?",
        (performed[:10], next_due, now_iso(), schedule["equipment_id"], hospital_id),
    )
    audit("maintenance.complete", "maintenance_record", record_id,
          f"Completed '{schedule['name']}' for {schedule['equipment_name']}")
    flash(f"Maintenance recorded. Next due {next_due}.", "success")
    return redirect(url_for("maintenance.schedule_detail", sid=sid))


def _add_days(iso_value, days):
    try:
        base = date.fromisoformat(iso_value[:10])
    except ValueError:
        base = date.today()
    return (base + timedelta(days=days)).isoformat()


@bp.post("/maintenance/schedules/<int:sid>/deactivate")
@engineer_or_admin
def schedule_deactivate(sid):
    hospital_id = current_user()["hospital_id"]
    _schedule_or_404(sid, hospital_id)
    execute(
        "UPDATE maintenance_schedules SET is_active = 0 WHERE id = ? AND hospital_id = ?",
        (sid, hospital_id),
    )
    audit("maintenance.schedule_deactivate", "maintenance_schedule", sid, "Deactivated")
    flash("Schedule deactivated. History is retained.", "success")
    return redirect(url_for("maintenance.maintenance_list"))


@bp.get("/maintenance/records")
@engineer_or_admin
def maintenance_records():
    user = current_user()
    rows = query(
        """SELECT m.*, e.name AS equipment_name, e.asset_number,
                  s.name AS schedule_name, u.full_name AS engineer_name
             FROM maintenance_records m
             JOIN equipment e ON e.id = m.equipment_id
             LEFT JOIN maintenance_schedules s ON s.id = m.schedule_id
             LEFT JOIN users u ON u.id = m.performed_by
            WHERE m.hospital_id = ? ORDER BY m.performed_at DESC LIMIT 200""",
        (user["hospital_id"],),
    )
    return render_template("maintenance_records.html", records=rows)
