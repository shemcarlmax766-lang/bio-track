"""Dashboard, analytics, and hospital-scoped export."""

from datetime import date, timedelta

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file

from core import (
    current_user,
    execute,
    login_required,
    query,
    refresh_low_stock_notifications,
    refresh_maintenance_notifications,
    roles_required,
)
from db import FAULT_STATUSES, OBSTRUCTIVE_FAULT_STATUSES, today_iso

DONUT_COLOURS = {
    "operational": "#199a5c",
    "under_repair": "#2a72d4",
    "faulty": "#e8930c",
    "awaiting_parts": "#8a4cc7",
    "out_of_service": "#dc3a34",
    "retired": "#9fb0c1",
}
DONUT_LABELS = {
    "operational": "Operational",
    "under_repair": "Under Repair",
    "faulty": "Faulty",
    "awaiting_parts": "Awaiting Parts",
    "out_of_service": "Out of Service",
    "retired": "Retired",
}
DONUT_ORDER = ["operational", "under_repair", "faulty", "awaiting_parts", "out_of_service"]


def _donut(by_status, total):
    """Pre-compute stroke properties for a simple SVG donut (r=48 => C=301.6)."""
    circumference = 2 * 3.14159265 * 48
    segments, offset = [], 0.0
    for key in DONUT_ORDER:
        value = by_status.get(key, 0)
        if not value:
            continue
        length = circumference * (value / total) if total else 0
        segments.append({
            "key": key,
            "label": DONUT_LABELS[key],
            "colour": DONUT_COLOURS[key],
            "value": value,
            "pct": round(value / total * 100, 1) if total else 0,
            "dash": round(length, 2),
            "gap": round(circumference - length, 2),
            "offset": round(-offset, 2),
        })
        offset += length
    return segments


def _line_chart(counts, labels):
    """Coordinates for an inline SVG line chart of monthly fault counts."""
    width, height = 340, 200
    left, right, top, bottom = 38, 334, 14, 176
    peak = max(counts + [1])
    nice = max(4, ((peak + 3) // 4) * 4)
    points = []
    for index, value in enumerate(counts):
        x = left + (right - left) * (index / max(1, len(counts) - 1))
        y = bottom - (bottom - top) * (value / nice)
        points.append({"x": round(x, 1), "y": round(y, 1), "label": labels[index], "value": value})
    line = " ".join(f"{p['x']},{p['y']}" for p in points)
    area = f"{left},{bottom} {line} {right},{bottom}"
    grid = []
    for step in range(5):
        value = nice - nice / 4 * step
        grid.append({
            "y": round(top + (bottom - top) * (step / 4), 1),
            "label": int(value),
        })
    return {"points": points, "line_points": line, "area_points": area, "grid": grid}


def _parse_month(iso_value):
    """Return a (year, month) tuple for an ISO date string, or None."""
    try:
        return int(iso_value[0:4]), int(iso_value[5:7])
    except (TypeError, ValueError):
        return None

bp = Blueprint("dashboard", __name__)


def _filters():
    return {
        "department": request.args.get("department", type=int),
        "category": request.args.get("category", type=int),
        "start": request.args.get("start") or "",
        "end": request.args.get("end") or "",
    }


def _equipment_filter(filters, alias="e"):
    clauses, params = [], []
    if filters["department"]:
        clauses.append(f"{alias}.department_id = ?")
        params.append(filters["department"])
    if filters["category"]:
        clauses.append(f"{alias}.category_id = ?")
        params.append(filters["category"])
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _date_filter(filters, column):
    clauses, params = [], []
    if filters["start"]:
        clauses.append(f"{column} >= ?")
        params.append(filters["start"])
    if filters["end"]:
        clauses.append(f"{column} <= ?")
        params.append(filters["end"] + "T23:59:59")
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _staff_scope(user):
    if user["role_code"] == "staff":
        return " AND e.department_id IS ?", [user["department_id"]]
    return "", []


@bp.get("/")
@login_required
def index():
    user = current_user()
    hid = user["hospital_id"]
    filters = _filters()
    scope_sql, scope_params = _staff_scope(user)
    ef_sql, ef_params = _equipment_filter(filters)

    def scalar(sql, params=()):
        row = query(sql, list(params), one=True)
        return (row[0] if row and row[0] is not None else 0)

    active_filter = " AND e.status != 'retired'"
    total = scalar(
        f"SELECT COUNT(*) FROM equipment e WHERE e.hospital_id = ?{active_filter}"
        f"{scope_sql}{ef_sql}",
        [hid] + scope_params + ef_params,
    )
    by_status = {
        r["status"]: r["n"]
        for r in query(
            f"""SELECT e.status, COUNT(*) AS n FROM equipment e
                 WHERE e.hospital_id = ? AND e.status != 'retired'{scope_sql}{ef_sql}
                 GROUP BY e.status""",
            [hid] + scope_params + ef_params,
        )
    }
    month_start = date.today().replace(day=1).isoformat()
    new_this_month = scalar(
        f"""SELECT COUNT(*) FROM equipment e
             WHERE e.hospital_id = ? AND e.created_at >= ?{scope_sql}{ef_sql}""",
        [hid, month_start] + scope_params + ef_params,
    )
    today = today_iso()
    overdue = scalar(
        f"""SELECT COUNT(*) FROM maintenance_schedules s
              JOIN equipment e ON e.id = s.equipment_id
             WHERE s.hospital_id = ? AND s.is_active = 1 AND s.next_due_date < ?{scope_sql}{ef_sql}""",
        [hid, today] + scope_params + ef_params,
    )
    upcoming = scalar(
        f"""SELECT COUNT(*) FROM maintenance_schedules s
              JOIN equipment e ON e.id = s.equipment_id
             WHERE s.hospital_id = ? AND s.is_active = 1
               AND s.next_due_date >= ? AND s.next_due_date <= ?{scope_sql}{ef_sql}""",
        [hid, today, (date.today() + timedelta(days=30)).isoformat()] + scope_params + ef_params,
    )

    fault_date_sql, fault_date_params = _date_filter(filters, "f.reported_at")
    open_faults = scalar(
        f"""SELECT COUNT(*) FROM fault_reports f JOIN equipment e ON e.id = f.equipment_id
             WHERE f.hospital_id = ?
               AND f.status IN ({','.join('?' * len(OBSTRUCTIVE_FAULT_STATUSES))})
               {scope_sql}{ef_sql}{fault_date_sql}""",
        [hid, *OBSTRUCTIVE_FAULT_STATUSES] + scope_params + ef_params + fault_date_params,
    )
    avg_turnaround = query(
        f"""SELECT AVG((julianday(w.closed_at) - julianday(w.opened_at)) * 1440.0) AS m
              FROM work_orders w JOIN equipment e ON e.id = w.equipment_id
             WHERE w.hospital_id = ? AND w.closed_at IS NOT NULL
               AND w.closed_at >= COALESCE(w.opened_at, w.closed_at){scope_sql}{ef_sql}""",
        [hid] + scope_params + ef_params,
        one=True,
    )
    downtime = query(
        f"""SELECT SUM(COALESCE(w.downtime_minutes, 0)) AS m FROM work_orders w
              JOIN equipment e ON e.id = w.equipment_id
             WHERE w.hospital_id = ? AND w.status = 'closed'{scope_sql}{ef_sql}""",
        [hid] + scope_params + ef_params,
        one=True,
    )

    dept_dist = query(
        f"""SELECT COALESCE(d.name, 'Unassigned') AS name, COUNT(*) AS n
              FROM equipment e LEFT JOIN departments d ON d.id = e.department_id
             WHERE e.hospital_id = ? AND e.status != 'retired'{scope_sql}{ef_sql}
             GROUP BY d.name ORDER BY n DESC""",
        [hid] + scope_params + ef_params,
    )

    # monthly fault trend (last 6 months)
    months, fault_counts, repair_counts = [], [], []
    first_of_month = date.today().replace(day=1)
    starts = []
    cursor = first_of_month
    for _ in range(6):
        starts.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    for first in reversed(starts):
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        months.append(first.strftime("%b"))
        fault_counts.append(
            scalar(
                f"""SELECT COUNT(*) FROM fault_reports f
                      JOIN equipment e ON e.id = f.equipment_id
                     WHERE f.hospital_id = ? AND f.reported_at >= ? AND f.reported_at < ?
                     {scope_sql}{ef_sql}""",
                [hid, first.isoformat(), nxt.isoformat()] + scope_params + ef_params,
            )
        )
        repair_counts.append(
            scalar(
                f"""SELECT COUNT(*) FROM repair_records r
                      JOIN equipment e ON e.id = r.equipment_id
                     WHERE r.hospital_id = ? AND r.repair_date >= ? AND r.repair_date < ?
                     {scope_sql}{ef_sql}""",
                [hid, first.isoformat(), nxt.isoformat()] + scope_params + ef_params,
            )
        )

    maintenance_completion = query(
        f"""SELECT strftime('%Y-%m', m.performed_at) AS month, COUNT(*) AS n
              FROM maintenance_records m JOIN equipment e ON e.id = m.equipment_id
             WHERE m.hospital_id = ? AND m.performed_at >= ?
             {scope_sql}{ef_sql}
             GROUP BY month ORDER BY month""",
        [hid, (date.today() - timedelta(days=180)).isoformat()] + scope_params + ef_params,
    )

    recent_faults = query(
        f"""SELECT f.*, e.name AS equipment_name, d.name AS department_name
              FROM fault_reports f
              JOIN equipment e ON e.id = f.equipment_id
              LEFT JOIN departments d ON d.id = f.department_id
             WHERE f.hospital_id = ?{scope_sql}
             ORDER BY f.reported_at DESC LIMIT 8""",
        [hid] + scope_params,
    )
    due_rows = query(
        f"""SELECT s.*, e.name AS equipment_name, e.asset_number
              FROM maintenance_schedules s JOIN equipment e ON e.id = s.equipment_id
             WHERE s.hospital_id = ? AND s.is_active = 1 AND s.next_due_date <= ?
             {scope_sql}
             ORDER BY s.next_due_date LIMIT 6""",
        [hid, (date.today() + timedelta(days=30)).isoformat()] + scope_params,
    )
    due_soon = []
    for row in due_rows:
        entry = dict(row)
        try:
            due_date = date.fromisoformat(row["next_due_date"])
        except (TypeError, ValueError):
            entry["overdue_days"] = None
            entry["days_away"] = 999
        else:
            delta = (due_date - date.today()).days
            entry["overdue_days"] = -delta if delta < 0 else None
            entry["days_away"] = delta
        due_soon.append(entry)
    low_stock = query(
        """SELECT * FROM spare_parts WHERE hospital_id = ? AND quantity <= min_stock
            ORDER BY quantity LIMIT 6""",
        (hid,),
    )

    if user["role_code"] in ("engineer", "administrator"):
        refresh_maintenance_notifications(hid)
        refresh_low_stock_notifications(hid)

    def pct(value):
        return round(value / total * 100, 1) if total else 0.0

    return render_template(
        "dashboard.html",
        today_pretty=date.today().strftime("%a, %d %b %Y"),
        donut=_donut(by_status, total),
        chart=_line_chart(fault_counts, months),
        metrics={
            "total": total,
            "operational": by_status.get("operational", 0),
            "faulty": by_status.get("faulty", 0),
            "under_repair": by_status.get("under_repair", 0),
            "awaiting_parts": by_status.get("awaiting_parts", 0),
            "out_of_service": by_status.get("out_of_service", 0),
            "overdue": overdue,
            "upcoming": upcoming,
            "open_faults": open_faults,
            "avg_turnaround_hours": round((avg_turnaround["m"] or 0) / 60, 1),
            "downtime_hours": round((downtime["m"] or 0) / 60, 1),
            "new_this_month": new_this_month,
            "pct_operational": pct(by_status.get("operational", 0)),
            "pct_faulty": pct(by_status.get("faulty", 0)),
            "pct_under_repair": pct(by_status.get("under_repair", 0)),
            "pct_overdue": pct(overdue),
        },
        by_status=by_status,
        dept_dist=dept_dist,
        chart_data={
            "months": months,
            "faults": fault_counts,
            "repairs": repair_counts,
            "status": by_status,
            "maintenance": [{"month": r["month"], "n": r["n"]} for r in maintenance_completion],
            "departments": [{"name": r["name"], "n": r["n"]} for r in dept_dist],
        },
        recent_faults=recent_faults,
        due_soon=due_soon,
        low_stock=low_stock,
        today=today,
        filters=filters,
        departments=query(
            "SELECT * FROM departments WHERE hospital_id = ? ORDER BY name", (hid,)
        ),
        categories=query(
            "SELECT * FROM equipment_categories WHERE hospital_id = ? ORDER BY name", (hid,)
        ),
    )


@bp.get("/api/dashboard")
@login_required
def api_dashboard():
    """Small JSON feed used by the dashboard charts for live refresh."""
    user = current_user()
    hid = user["hospital_id"]
    scope_sql, scope_params = _staff_scope(user)
    rows = query(
        f"""SELECT e.status, COUNT(*) AS n FROM equipment e
             WHERE e.hospital_id = ?{scope_sql} GROUP BY e.status""",
        [hid] + scope_params,
    )
    return jsonify({"status": {r["status"]: r["n"] for r in rows}})


@bp.get("/api/equipment/lookup")
@login_required
def api_equipment_lookup():
    """Typeahead source for fault intake."""
    user = current_user()
    term = (request.args.get("q") or "").strip()
    sql = """SELECT e.id, e.name, e.asset_number, d.name AS department
               FROM equipment e LEFT JOIN departments d ON d.id = e.department_id
              WHERE e.hospital_id = ? AND e.status != 'retired'"""
    params = [user["hospital_id"]]
    if user["role_code"] == "staff":
        sql += " AND e.department_id IS ?"
        params.append(user["department_id"])
    if term:
        sql += " AND (e.name LIKE ? OR e.asset_number LIKE ?)"
        params.extend([f"%{term}%"] * 2)
    sql += " ORDER BY e.name LIMIT 25"
    return jsonify([dict(r) for r in query(sql, params)])


@bp.get("/backup")
@roles_required("administrator")
def backup():
    """Hospital-scoped SQLite snapshot. Other tenants are excluded."""
    import os
    import sqlite3
    import tempfile

    from flask import after_this_request

    from db import DB_PATH
    from core import audit

    user = current_user()
    hid = user["hospital_id"]
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(path)
    try:
        hospital_tables = [
            "hospitals", "departments", "users", "equipment_categories", "equipment",
            "fault_reports", "work_orders", "repair_records", "maintenance_schedules",
            "maintenance_records", "spare_parts", "spare_part_transactions",
            "service_documents", "document_pages", "notifications", "audit_logs",
            "roles",
        ]
        for table in hospital_tables:
            create = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if create:
                dst.execute(create[0])
        for table in hospital_tables:
            if table == "roles":
                data = src.execute("SELECT * FROM roles").fetchall()
            elif table == "hospitals":
                data = src.execute("SELECT * FROM hospitals WHERE id = ?", (hid,)).fetchall()
            elif table == "document_pages":
                data = src.execute(
                    """SELECT * FROM document_pages WHERE document_id IN
                       (SELECT id FROM service_documents WHERE hospital_id = ?)""", (hid,)
                ).fetchall()
            else:
                data = src.execute(
                    f"SELECT * FROM {table} WHERE hospital_id = ?", (hid,)
                ).fetchall()
            if data:
                cols = len(data[0])
                dst.executemany(
                    f"INSERT INTO {table} VALUES ({','.join('?' * cols)})", data
                )
        dst.commit()
    finally:
        src.close()
        dst.close()
    audit("backup.export", "hospital", hid, "Downloaded hospital database snapshot")

    @after_this_request
    def cleanup(resp):
        try:
            os.remove(path)
        except OSError:
            pass
        return resp

    return send_file(
        path, as_attachment=True,
        download_name=f"biotrack-{user['hospital_code'].lower()}-{today_iso()}.sqlite3",
        mimetype="application/x-sqlite3",
    )
