"""Report previews with PDF (reportlab) and Excel (openpyxl) export."""

import io
from datetime import date, timedelta

from flask import Blueprint, Response, abort, render_template, request

from core import audit, current_user, login_required, query, roles_required

bp = Blueprint("reports", __name__)

REPORTS = {
    "inventory": {
        "title": "Complete Equipment Inventory",
        "roles": ("administrator", "engineer"),
        "description": "Every registered equipment item with identifiers and status.",
    },
    "condition": {
        "title": "Equipment Condition Report",
        "roles": ("administrator", "engineer"),
        "description": "Operational status, clinical-use restrictions and maintenance dates.",
    },
    "maintenance": {
        "title": "Maintenance History",
        "roles": ("administrator", "engineer"),
        "description": "Completed preventive maintenance against each schedule.",
    },
    "faults": {
        "title": "Fault Reports",
        "roles": ("administrator", "engineer", "staff"),
        "description": "Reported faults with severity, status and turnaround.",
    },
    "repairs": {
        "title": "Repair History",
        "roles": ("administrator", "engineer"),
        "description": "Repairs performed, downtime and return-to-service verification.",
    },
    "activity": {
        "title": "Monthly Biomedical Engineering Activity",
        "roles": ("administrator", "engineer"),
        "description": "Faults, repairs and maintenance within a chosen month.",
    },
    "parts": {
        "title": "Spare-Parts Inventory",
        "roles": ("administrator", "engineer"),
        "description": "Stock levels, minimums and stock value in UGX.",
    },
}


def _columns():
    return {
        "inventory": (
            ["Name", "Category", "Manufacturer", "Model", "Serial", "Asset No.",
             "Department", "Location", "Status", "Next maintenance", "Warranty expiry"],
            lambda r: [r["name"], r["category_name"], r["manufacturer"], r["model_number"],
                       r["serial_number"], r["asset_number"], r["department_name"],
                       r["location"], r["status"].replace("_", " "), r["next_maintenance_date"],
                       r["warranty_expiry"]],
        ),
        "condition": (
            ["Name", "Asset No.", "Department", "Status", "Clinical use blocked",
             "Blocked reason", "Last maintenance", "Next maintenance"],
            lambda r: [r["name"], r["asset_number"], r["department_name"], r["status"],
                       "Yes" if r["clinical_use_blocked"] else "No", r["blocked_reason"],
                       r["last_maintenance_date"], r["next_maintenance_date"]],
        ),
        "maintenance": (
            ["Equipment", "Schedule", "Performed", "Engineer", "Verified",
             "Returned to service", "Notes"],
            lambda r: [r["equipment_name"], r["schedule_name"], r["performed_at"],
                       r["engineer_name"], "Yes" if r["verified_at"] else "No",
                       "Yes" if r["returned_to_service"] else "No", r["notes"]],
        ),
        "faults": (
            ["Equipment", "Department", "Severity", "Status", "Reported", "Reporter",
             "Assigned engineer", "Description"],
            lambda r: [r["equipment_name"], r["department_name"], r["severity"], r["status"],
                       r["reported_at"], r["reporter_name"], r["engineer_name"],
                       r["description"]],
        ),
        "repairs": (
            ["Equipment", "Repair date", "Engineer", "Diagnosis", "Repair performed",
             "Parts used", "Downtime (min)", "Verified"],
            lambda r: [r["equipment_name"], r["repair_date"], r["engineer_name"],
                       r["diagnosis"], r["repair_performed"], r["spare_parts_used"],
                       r["downtime_minutes"], "Yes" if r["verified_at"] else "No"],
        ),
        "activity": (
            ["Activity", "Equipment", "When", "Detail"],
            lambda r: [r["activity"], r["equipment_name"], r["when"], r["detail"]],
        ),
        "parts": (
            ["Part name", "Part number", "Manufacturer", "Compatible equipment",
             "Quantity", "Minimum", "Supplier", "Storage", "Unit cost UGX"],
            lambda r: [r["name"], r["part_number"], r["manufacturer"],
                       r["compatible_equipment"], r["quantity"], r["min_stock"],
                       r["supplier"], r["storage_location"], r["unit_cost_ugx"]],
        ),
    }


def _rows(kind, hospital_id, month=None):
    if kind == "inventory":
        return query(
            """SELECT e.*, d.name AS department_name, c.name AS category_name
                 FROM equipment e
                 LEFT JOIN departments d ON d.id = e.department_id
                 LEFT JOIN equipment_categories c ON c.id = e.category_id
                WHERE e.hospital_id = ? ORDER BY e.name""",
            (hospital_id,),
        )
    if kind == "condition":
        return query(
            """SELECT e.*, d.name AS department_name FROM equipment e
                 LEFT JOIN departments d ON d.id = e.department_id
                WHERE e.hospital_id = ? ORDER BY
                      CASE e.status WHEN 'out_of_service' THEN 0 WHEN 'faulty' THEN 1
                                    WHEN 'under_repair' THEN 2 ELSE 3 END, e.name""",
            (hospital_id,),
        )
    if kind == "maintenance":
        return query(
            """SELECT m.*, e.name AS equipment_name, s.name AS schedule_name,
                      u.full_name AS engineer_name
                 FROM maintenance_records m
                 JOIN equipment e ON e.id = m.equipment_id
                 LEFT JOIN maintenance_schedules s ON s.id = m.schedule_id
                 LEFT JOIN users u ON u.id = m.performed_by
                WHERE m.hospital_id = ? ORDER BY m.performed_at DESC""",
            (hospital_id,),
        )
    if kind == "faults":
        return query(
            """SELECT f.*, e.name AS equipment_name, d.name AS department_name,
                      eng.full_name AS engineer_name
                 FROM fault_reports f
                 JOIN equipment e ON e.id = f.equipment_id
                 LEFT JOIN departments d ON d.id = f.department_id
                 LEFT JOIN users eng ON eng.id = f.assigned_engineer_id
                WHERE f.hospital_id = ? ORDER BY f.reported_at DESC""",
            (hospital_id,),
        )
    if kind == "repairs":
        return query(
            """SELECT r.*, e.name AS equipment_name, u.full_name AS engineer_name
                 FROM repair_records r
                 JOIN equipment e ON e.id = r.equipment_id
                 LEFT JOIN users u ON u.id = r.engineer_id
                WHERE r.hospital_id = ? ORDER BY r.repair_date DESC""",
            (hospital_id,),
        )
    if kind == "activity":
        month = month or date.today().strftime("%Y-%m")
        prefix = month
        faults = query(
            """SELECT 'Fault reported' AS activity, e.name AS equipment_name,
                      f.reported_at AS "when", f.severity || ' / ' || f.status AS detail
                 FROM fault_reports f JOIN equipment e ON e.id = f.equipment_id
                WHERE f.hospital_id = ? AND f.reported_at LIKE ?""",
            (hospital_id, prefix + "%"),
        )
        repairs = query(
            """SELECT 'Repair documented' AS activity, e.name AS equipment_name,
                      r.repair_date AS "when", r.repair_performed AS detail
                 FROM repair_records r JOIN equipment e ON e.id = r.equipment_id
                WHERE r.hospital_id = ? AND r.repair_date LIKE ?""",
            (hospital_id, prefix + "%"),
        )
        maintenance = query(
            """SELECT 'Maintenance completed' AS activity, e.name AS equipment_name,
                      m.performed_at AS "when", COALESCE(s.name, '') AS detail
                 FROM maintenance_records m JOIN equipment e ON e.id = m.equipment_id
                 LEFT JOIN maintenance_schedules s ON s.id = m.schedule_id
                WHERE m.hospital_id = ? AND m.performed_at LIKE ?""",
            (hospital_id, prefix + "%"),
        )
        return sorted(list(faults) + list(repairs) + list(maintenance),
                      key=lambda r: r["when"] or "", reverse=True)
    if kind == "parts":
        return query(
            "SELECT * FROM spare_parts WHERE hospital_id = ? ORDER BY name",
            (hospital_id,),
        )
    abort(404)


def _allowed(kind, user):
    meta = REPORTS.get(kind)
    if meta is None:
        abort(404)
    if user["role_code"] not in meta["roles"]:
        abort(403)
    return meta


@bp.get("/reports")
@login_required
def reports_index():
    user = current_user()
    available = [
        (kind, meta)
        for kind, meta in REPORTS.items()
        if user["role_code"] in meta["roles"]
    ]
    return render_template("reports.html", reports=available,
                           month=date.today().strftime("%Y-%m"))


@bp.get("/reports/<kind>")
@login_required
def report_preview(kind):
    user = current_user()
    meta = _allowed(kind, user)
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    rows = _rows(kind, user["hospital_id"], month)
    headers, rowfn = _columns()[kind]
    table = [[str(cell) if cell is not None else "" for cell in rowfn(r)] for r in rows]
    summary = _summary(kind, rows)
    return render_template(
        "report_preview.html", kind=kind, meta=meta, headers=headers, rows=table,
        summary=summary, month=month,
    )


def _summary(kind, rows):
    if kind == "parts":
        value = sum((r["quantity"] or 0) * (r["unit_cost_ugx"] or 0) for r in rows)
        low = sum(1 for r in rows if (r["quantity"] or 0) <= (r["min_stock"] or 0))
        return [("Parts tracked", len(rows)), ("Below minimum", low),
                ("Stock value", f"UGX {value:,.0f}")]
    if kind == "faults":
        open_n = sum(1 for r in rows if r["status"] not in ("resolved", "closed"))
        return [("Total faults", len(rows)), ("Still open", open_n),
                ("Critical", sum(1 for r in rows if r["severity"] == "critical"))]
    if kind == "inventory":
        return [("Total equipment", len(rows)),
                ("Operational", sum(1 for r in rows if r["status"] == "operational")),
                ("Not operational",
                 sum(1 for r in rows if r["status"] != "operational"))]
    if kind == "activity":
        return [("Recorded activities", len(rows))]
    return [("Records", len(rows))]


@bp.get("/reports/<kind>.pdf")
@login_required
def report_pdf(kind):
    user = current_user()
    meta = _allowed(kind, user)
    rows = _rows(kind, user["hospital_id"], request.args.get("month"))
    headers, rowfn = _columns()[kind]
    table = [[str(cell) if cell is not None else "" for cell in rowfn(r)] for r in rows]

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                        TableStyle)
    except ImportError:
        abort(503, "PDF export requires the reportlab package.")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), title=meta["title"],
                            leftMargin=24, rightMargin=24, topMargin=28, bottomMargin=24)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"BioTrack - {meta['title']}", styles["Title"]),
        Paragraph(f"{user['hospital_name']} &nbsp;|&nbsp; generated "
                  f"{date.today().isoformat()} &nbsp;|&nbsp; {len(table)} records",
                  styles["Normal"]),
        Spacer(1, 12),
    ]
    data = [headers] + table
    if len(table) == 0:
        data.append(["No records"] + [""] * (len(headers) - 1))
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f2b46")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8d4de")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f6f9")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Demonstration system. Not a regulatory approval or a security certification.",
        styles["Italic"]))
    doc.build(story)
    audit("report.export", "report", None, f"Exported {kind} as PDF")
    return Response(
        buf.getvalue(), mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="biotrack-{kind}.pdf"'},
    )


@bp.get("/reports/<kind>.xlsx")
@login_required
def report_xlsx(kind):
    user = current_user()
    meta = _allowed(kind, user)
    rows = _rows(kind, user["hospital_id"], request.args.get("month"))
    headers, rowfn = _columns()[kind]
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        abort(503, "Excel export requires the openpyxl package.")

    wb = Workbook()
    ws = wb.active
    ws.title = kind.title()[:31]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0F2B46")
        cell.alignment = Alignment(vertical="top")
    for r in rows:
        ws.append([("" if c is None else c) for c in rowfn(r)])
    for column in ws.columns:
        width = max((len(str(c.value)) if c.value is not None else 0) for c in column)
        ws.column_dimensions[column[0].column_letter].width = min(48, max(12, width + 2))
    ws.freeze_panes = "A2"

    info = wb.create_sheet("Report info")
    info.append(["Report", meta["title"]])
    info.append(["Hospital", user["hospital_name"]])
    info.append(["Generated", date.today().isoformat()])
    info.append(["Records", len(rows)])
    info.append(["Note", "Demonstration system; not a regulatory approval."])

    buf = io.BytesIO()
    wb.save(buf)
    audit("report.export", "report", None, f"Exported {kind} as XLSX")
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="biotrack-{kind}.xlsx"'},
    )
