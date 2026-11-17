"""Fault intake, work orders, and repair documentation."""

from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

import core
import uploads
from core import (
    audit,
    current_user,
    engineer_or_admin,
    execute,
    form,
    form_int,
    login_required,
    notify,
    notify_role,
    query,
    roles_required,
)
from db import FAULT_STATUSES, OBSTRUCTIVE_FAULT_STATUSES, SEVERITIES, now_iso

bp = Blueprint("faults", __name__)

STATUS_TO_EQUIPMENT = {
    "reported": "faulty",
    "under_review": "faulty",
    "assigned": "faulty",
    "under_repair": "under_repair",
    "awaiting_spare_parts": "awaiting_parts",
    "resolved": "operational",
    "closed": "operational",
}


def _fault_or_404(fid, hospital_id):
    row = query(
        """SELECT f.*, e.name AS equipment_name, e.asset_number, e.id AS equipment_id,
                  e.location, e.clinical_use_blocked, d.name AS department_name,
                  rep.full_name AS reporter, eng.full_name AS engineer_name
             FROM fault_reports f
             JOIN equipment e ON e.id = f.equipment_id
             LEFT JOIN departments d ON d.id = f.department_id
             LEFT JOIN users rep ON rep.id = f.reported_by
             LEFT JOIN users eng ON eng.id = f.assigned_engineer_id
            WHERE f.id = ? AND f.hospital_id = ?""",
        (fid, hospital_id),
        one=True,
    )
    if row is None:
        abort(404)
    return row


def _can_view_fault(user, fault):
    if user["role_code"] in ("administrator", "engineer"):
        return True
    return fault["department_id"] == user["department_id"] or fault["reported_by"] == user["id"]


@bp.get("/faults")
@login_required
def fault_list():
    user = current_user()
    sql = """SELECT f.*, e.name AS equipment_name, e.asset_number,
                    d.name AS department_name, eng.full_name AS engineer_name
               FROM fault_reports f
               JOIN equipment e ON e.id = f.equipment_id
               LEFT JOIN departments d ON d.id = f.department_id
               LEFT JOIN users eng ON eng.id = f.assigned_engineer_id
              WHERE f.hospital_id = ?"""
    params = [user["hospital_id"]]
    if user["role_code"] == "staff":
        sql += " AND (f.department_id = ? OR f.reported_by = ?)"
        params.extend([user["department_id"], user["id"]])
    status = request.args.get("status") or ""
    severity = request.args.get("severity") or ""
    dept = request.args.get("department") or ""
    if status in FAULT_STATUSES:
        sql += " AND f.status = ?"
        params.append(status)
    if severity in SEVERITIES:
        sql += " AND f.severity = ?"
        params.append(severity)
    if dept.isdigit():
        sql += " AND f.department_id = ?"
        params.append(int(dept))
    sql += " ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 " \
           "WHEN 'medium' THEN 2 ELSE 3 END, f.reported_at DESC"
    rows = query(sql, params)
    departments = query(
        "SELECT * FROM departments WHERE hospital_id = ? ORDER BY name",
        (user["hospital_id"],),
    )
    return render_template(
        "fault_list.html", faults=rows, departments=departments,
        filters={"status": status, "severity": severity, "department": dept},
    )


def _equipment_options(user):
    sql = """SELECT e.id, e.name, e.asset_number, d.name AS department_name
               FROM equipment e LEFT JOIN departments d ON d.id = e.department_id
              WHERE e.hospital_id = ? AND e.status != 'retired'"""
    params = [user["hospital_id"]]
    if user["role_code"] == "staff":
        sql += " AND e.department_id IS ?"
        params.append(user["department_id"])
    return query(sql + " ORDER BY e.name", params)


@bp.route("/faults/new", methods=["GET", "POST"])
@login_required
def fault_new():
    user = current_user()
    preselect = request.args.get("equipment_id", type=int)
    options = _equipment_options(user)
    if request.method == "POST":
        errors = []
        equipment_id = form_int("equipment_id")
        equipment = query(
            "SELECT * FROM equipment WHERE id = ? AND hospital_id = ?",
            (equipment_id, user["hospital_id"]),
            one=True,
        )
        if equipment is None:
            errors.append("Select the equipment that is faulty.")
        elif user["role_code"] == "staff" and equipment["department_id"] != user["department_id"]:
            errors.append("You can only report faults for equipment in your department.")
        description = form("description")
        if len(description) < 10:
            errors.append("Describe the problem in at least 10 characters.")
        severity = form("severity", "medium")
        if severity not in SEVERITIES:
            errors.append("Choose a valid severity.")
        photo_path = None
        photo = request.files.get("photo")
        if photo and photo.filename:
            try:
                photo_path, _ = uploads.save_image(photo)
            except uploads.UploadError as exc:
                errors.append(str(exc))
        if errors:
            core.flash_errors(errors)
            return render_template(
                "fault_form.html", equipment=options, selected=equipment_id,
                form_data=request.form,
            ), 400

        fid = execute(
            """INSERT INTO fault_reports (hospital_id, equipment_id, department_id,
                    reported_by, reporter_name, description, severity, photo_path,
                    reported_at, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reported', ?)""",
            (user["hospital_id"], equipment_id, equipment["department_id"], user["id"],
             user["full_name"], description, severity, photo_path, now_iso(), now_iso()),
        )
        execute(
            "UPDATE equipment SET status = 'faulty', updated_at = ? WHERE id = ?",
            (now_iso(), equipment_id),
        )
        audit("fault.create", "fault_report", fid,
              f"{severity} fault on {equipment['name']}")
        notify_role(
            user["hospital_id"], "engineer", "fault_reported",
            f"New {severity} fault reported for {equipment['name']} "
            f"({equipment['asset_number'] or 'no asset no.'})",
            url_for("faults.fault_detail", fid=fid),
            dedupe_key=f"fault_reported:{fid}",
        )
        notify_role(
            user["hospital_id"], "administrator", "fault_reported",
            f"New {severity} fault reported for {equipment['name']}",
            url_for("faults.fault_detail", fid=fid),
            dedupe_key=f"fault_reported_admin:{fid}",
        )
        flash("Fault reported. The biomedical engineering team has been notified.",
              "success")
        return redirect(url_for("faults.fault_detail", fid=fid))
    return render_template(
        "fault_form.html", equipment=options, selected=preselect, form_data={}
    )


@bp.get("/faults/<int:fid>")
@login_required
def fault_detail(fid):
    user = current_user()
    fault = _fault_or_404(fid, user["hospital_id"])
    if not _can_view_fault(user, fault):
        abort(403)
    work_orders = query(
        """SELECT w.*, u.full_name AS engineer_name FROM work_orders w
             LEFT JOIN users u ON u.id = w.assigned_to
            WHERE w.fault_id = ? AND w.hospital_id = ? ORDER BY w.opened_at DESC""",
        (fid, user["hospital_id"]),
    )
    repairs = query(
        """SELECT r.*, u.full_name AS engineer_name, v.full_name AS verifier
             FROM repair_records r
             LEFT JOIN users u ON u.id = r.engineer_id
             LEFT JOIN users v ON v.id = r.verified_by
            WHERE r.equipment_id = ? AND r.hospital_id = ?
            ORDER BY r.repair_date DESC""",
        (fault["equipment_id"], user["hospital_id"]),
    )
    engineers = query(
        """SELECT u.id, u.full_name FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.hospital_id = ? AND r.code = 'engineer' AND u.is_active = 1
            ORDER BY u.full_name""",
        (user["hospital_id"],),
    )
    timeline = _timeline(fault["equipment_id"], user["hospital_id"])
    return render_template(
        "fault_detail.html", fault=fault, work_orders=work_orders, repairs=repairs,
        engineers=engineers, timeline=timeline,
    )


def _timeline(equipment_id, hospital_id):
    rows = []
    for r in query(
        """SELECT reported_at AS at, 'Fault reported: ' || severity AS what
             FROM fault_reports WHERE equipment_id = ? AND hospital_id = ?""",
        (equipment_id, hospital_id),
    ):
        rows.append({"at": r["at"], "what": r["what"]})
    for r in query(
        """SELECT performed_at AS at, 'Maintenance: ' || name AS what
             FROM maintenance_records m
             LEFT JOIN maintenance_schedules s ON s.id = m.schedule_id
            WHERE m.equipment_id = ? AND m.hospital_id = ?""",
        (equipment_id, hospital_id),
    ):
        rows.append({"at": r["at"], "what": r["what"]})
    for r in query(
        """SELECT repair_date AS at, 'Repair: ' || COALESCE(repair_performed, 'recorded') AS what
             FROM repair_records WHERE equipment_id = ? AND hospital_id = ?""",
        (equipment_id, hospital_id),
    ):
        rows.append({"at": r["at"], "what": r["what"]})
    return sorted(rows, key=lambda row: row["at"] or "", reverse=True)


@bp.post("/faults/<int:fid>/status")
@engineer_or_admin
def fault_status(fid):
    user = current_user()
    fault = _fault_or_404(fid, user["hospital_id"])
    new_status = form("status")
    note = form("note")
    if new_status not in FAULT_STATUSES:
        abort(400)
    if new_status == "closed" and not note:
        flash("Closing a fault requires a closing note describing the outcome.", "error")
        return redirect(url_for("faults.fault_detail", fid=fid))

    execute(
        "UPDATE fault_reports SET status = ?, updated_at = ? WHERE id = ? AND hospital_id = ?",
        (new_status, now_iso(), fid, user["hospital_id"]),
    )
    equipment_status = STATUS_TO_EQUIPMENT[new_status]
    if new_status == "resolved" and fault["clinical_use_blocked"]:
        equipment_status = "out_of_service"
    execute(
        "UPDATE equipment SET status = ?, updated_at = ? WHERE id = ? AND hospital_id = ?",
        (equipment_status, now_iso(), fault["equipment_id"], user["hospital_id"]),
    )
    if new_status == "closed":
        execute(
            """UPDATE work_orders SET status = 'closed', closed_at = ?, closed_by = ?
                WHERE fault_id = ? AND hospital_id = ? AND status != 'closed'""",
            (now_iso(), user["id"], fid, user["hospital_id"]),
        )
    if new_status in ("under_repair", "awaiting_spare_parts", "resolved"):
        execute(
            """UPDATE work_orders SET status = ?,
                      started_at = COALESCE(started_at, ?)
                WHERE fault_id = ? AND hospital_id = ? AND status != 'closed'""",
            ("in_progress" if new_status != "resolved" else "awaiting_verification",
             now_iso(), fid, user["hospital_id"]),
        )
    audit("fault.status", "fault_report", fid,
          f"Status {fault['status']} -> {new_status}" + (f" ({note})" if note else ""))
    if fault["reported_by"]:
        notify(
            user["hospital_id"], fault["reported_by"], "fault_status",
            f"Fault on {fault['equipment_name']} is now {new_status.replace('_', ' ')}.",
            url_for("faults.fault_detail", fid=fid),
            dedupe_key=f"fault_status:{fid}:{new_status}",
        )
    flash(f"Fault status updated to {new_status.replace('_', ' ')}.", "success")
    return redirect(url_for("faults.fault_detail", fid=fid))


@bp.post("/faults/<int:fid>/assign")
@engineer_or_admin
def fault_assign(fid):
    user = current_user()
    fault = _fault_or_404(fid, user["hospital_id"])
    engineer_id = form_int("assigned_engineer_id")
    engineer = query(
        """SELECT u.* FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.id = ? AND u.hospital_id = ? AND r.code = 'engineer' AND u.is_active = 1""",
        (engineer_id, user["hospital_id"]),
        one=True,
    )
    if engineer is None:
        flash("Select an active biomedical engineer.", "error")
        return redirect(url_for("faults.fault_detail", fid=fid))
    execute(
        """UPDATE fault_reports SET assigned_engineer_id = ?,
                  status = CASE WHEN status = 'reported' THEN 'assigned' ELSE status END,
                  updated_at = ? WHERE id = ? AND hospital_id = ?""",
        (engineer_id, now_iso(), fid, user["hospital_id"]),
    )
    work_order = query(
        "SELECT id FROM work_orders WHERE fault_id = ? AND hospital_id = ? AND status != 'closed'",
        (fid, user["hospital_id"]),
        one=True,
    )
    if work_order is None:
        wid = execute(
            """INSERT INTO work_orders (hospital_id, equipment_id, fault_id, assigned_to,
                    status, priority, summary, opened_at)
               VALUES (?, ?, ?, ?, 'open', ?, ?, ?)""",
            (user["hospital_id"], fault["equipment_id"], fid, engineer_id,
             fault["severity"], f"Repair work order for {fault['equipment_name']}", now_iso()),
        )
        audit("work_order.create", "work_order", wid, f"Opened for fault {fid}")
    else:
        execute(
            "UPDATE work_orders SET assigned_to = ? WHERE id = ?",
            (engineer_id, work_order["id"]),
        )
    execute(
        "UPDATE equipment SET assigned_engineer_id = ?, updated_at = ? WHERE id = ?",
        (engineer_id, now_iso(), fault["equipment_id"]),
    )
    audit("fault.assign", "fault_report", fid, f"Assigned to {engineer['full_name']}")
    notify(
        user["hospital_id"], engineer_id, "repair_assigned",
        f"You were assigned the {fault['severity']} fault on {fault['equipment_name']}.",
        url_for("faults.fault_detail", fid=fid),
        dedupe_key=f"assigned:{fid}:{engineer_id}",
    )
    flash(f"Fault assigned to {engineer['full_name']}.", "success")
    return redirect(url_for("faults.fault_detail", fid=fid))


@bp.post("/faults/<int:fid>/findings")
@engineer_or_admin
def fault_findings(fid):
    user = current_user()
    fault = _fault_or_404(fid, user["hospital_id"])
    execute(
        """UPDATE fault_reports SET preliminary_findings = ?, diagnosis = ?,
                  updated_at = ? WHERE id = ? AND hospital_id = ?""",
        (form("preliminary_findings"), form("diagnosis"), now_iso(), fid,
         user["hospital_id"]),
    )
    audit("fault.findings", "fault_report", fid, "Recorded findings and diagnosis")
    flash("Findings and diagnosis saved.", "success")
    return redirect(url_for("faults.fault_detail", fid=fid))


@bp.post("/faults/<int:fid>/repair")
@engineer_or_admin
def repair_create(fid):
    user = current_user()
    fault = _fault_or_404(fid, user["hospital_id"])
    repair_performed = form("repair_performed")
    verification = form("verification_notes")
    errors = []
    if not repair_performed:
        errors.append("Describe the repair that was performed.")
    if not form("diagnosis"):
        errors.append("Record the diagnosis for the repair record.")
    work_order = query(
        "SELECT * FROM work_orders WHERE fault_id = ? AND hospital_id = ? "
        "ORDER BY opened_at DESC LIMIT 1",
        (fid, user["hospital_id"]),
        one=True,
    )
    if form("return_to_service") == "1" and not verification:
        errors.append("Return-to-service documentation and verification notes are required.")
    if errors:
        core.flash_errors(errors)
        return redirect(url_for("faults.fault_detail", fid=fid))

    downtime = None
    if work_order and work_order["opened_at"]:
        downtime = _minutes_between(work_order["opened_at"], now_iso())
    returned = form("return_to_service") == "1"
    repair_id = execute(
        """INSERT INTO repair_records (hospital_id, work_order_id, equipment_id,
                engineer_id, diagnosis, repair_performed, components_replaced,
                spare_parts_used, repair_date, duration_minutes, downtime_minutes,
                verification_notes, verified_by, verified_at, returned_to_service_at,
                created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user["hospital_id"], work_order["id"] if work_order else None,
         fault["equipment_id"], user["id"], form("diagnosis"), repair_performed,
         form("components_replaced"), form("spare_parts_used"), now_iso(),
         form_int("duration_minutes"), downtime, verification,
         user["id"] if returned else None,
         now_iso() if returned else None,
         now_iso() if returned else None, now_iso()),
    )
    execute(
        "UPDATE fault_reports SET diagnosis = ?, status = ?, updated_at = ? "
        "WHERE id = ? AND hospital_id = ?",
        (form("diagnosis"), "resolved", now_iso(), fid, user["hospital_id"]),
    )
    execute(
        "UPDATE equipment SET status = ?, updated_at = ? WHERE id = ? AND hospital_id = ?",
        ("operational" if returned and not fault["clinical_use_blocked"] else "out_of_service",
         now_iso(), fault["equipment_id"], user["hospital_id"]),
    )
    if work_order:
        execute(
            """UPDATE work_orders SET status = 'awaiting_verification',
                      downtime_minutes = ? WHERE id = ? AND hospital_id = ?""",
            (downtime, work_order["id"], user["hospital_id"]),
        )
    audit("repair.create", "repair_record", repair_id,
          f"Repair documented for fault {fid}"
          + (" with return-to-service verification" if returned else ""))
    flash("Repair documented." + (" Equipment verified and returned to service."
                                  if returned else
                                  " Equipment is not returned to service until verified."),
          "success")
    return redirect(url_for("faults.fault_detail", fid=fid))


def _minutes_between(start, end):
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
    except ValueError:
        return None
    return max(0, int((b - a).total_seconds() // 60))


@bp.post("/work-orders/<int:wid>/close")
@engineer_or_admin
def work_order_close(wid):
    user = current_user()
    wo = query(
        "SELECT * FROM work_orders WHERE id = ? AND hospital_id = ?",
        (wid, user["hospital_id"]),
        one=True,
    )
    if wo is None:
        abort(404)
    note = form("closing_note")
    if not note:
        flash("A closing note is required to close a work order.", "error")
        return redirect(request.referrer or url_for("faults.fault_list"))
    downtime = wo["downtime_minutes"]
    if not downtime and wo["opened_at"]:
        downtime = _minutes_between(wo["opened_at"], now_iso())
    execute(
        """UPDATE work_orders SET status = 'closed', closed_at = ?, closed_by = ?,
                  downtime_minutes = ? WHERE id = ? AND hospital_id = ?""",
        (now_iso(), user["id"], downtime, wid, user["hospital_id"]),
    )
    execute(
        "UPDATE repair_records SET downtime_minutes = ? WHERE work_order_id = ?",
        (downtime, wid),
    )
    if wo["fault_id"]:
        execute(
            "UPDATE fault_reports SET status = 'closed', updated_at = ? WHERE id = ?",
            (now_iso(), wo["fault_id"]),
        )
    audit("work_order.close", "work_order", wid, note)
    flash("Work order closed.", "success")
    return redirect(request.referrer or url_for("faults.fault_list"))


@bp.get("/work-orders")
@engineer_or_admin
def work_order_list():
    user = current_user()
    rows = query(
        """SELECT w.*, e.name AS equipment_name, e.asset_number,
                  u.full_name AS engineer_name
             FROM work_orders w
             JOIN equipment e ON e.id = w.equipment_id
             LEFT JOIN users u ON u.id = w.assigned_to
            WHERE w.hospital_id = ? ORDER BY
                  CASE w.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                  w.opened_at DESC""",
        (user["hospital_id"],),
    )
    return render_template("work_orders.html", work_orders=rows)


@bp.post("/work-orders/<int:wid>/status")
@engineer_or_admin
def work_order_status(wid):
    user = current_user()
    new_status = form("status")
    if new_status not in ("open", "in_progress", "awaiting_parts", "awaiting_verification",
                          "closed"):
        abort(400)
    values = ["started_at = COALESCE(started_at, ?)" if new_status == "in_progress"
              else "started_at = started_at", now_iso()]
    execute(
        f"UPDATE work_orders SET status = ?, {values[0]} WHERE id = ? AND hospital_id = ?",
        (new_status, values[1], wid, user["hospital_id"]),
    )
    audit("work_order.status", "work_order", wid, f"Status set to {new_status}")
    flash("Work order updated.", "success")
    return redirect(request.referrer or url_for("faults.work_order_list"))
