"""Equipment inventory, QR labels, and PDF service manuals."""

import os
from datetime import date

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    render_template,
    request,
    send_file,
    url_for,
)

import core
import uploads
from core import (
    audit,
    current_user,
    engineer_or_admin,
    execute,
    form,
    form_float,
    form_int,
    login_required,
    notify,
    notify_role,
    query,
    roles_required,
)
from db import EQUIPMENT_STATUSES, new_token, now_iso

bp = Blueprint("equipment", __name__)


def _load_equipment(eid, hospital_id):
    row = query(
        """SELECT e.*, d.name AS department_name, c.name AS category_name,
                  u.full_name AS engineer_name
             FROM equipment e
             LEFT JOIN departments d ON d.id = e.department_id
             LEFT JOIN equipment_categories c ON c.id = e.category_id
             LEFT JOIN users u ON u.id = e.assigned_engineer_id
            WHERE e.id = ? AND e.hospital_id = ?""",
        (eid, hospital_id),
        one=True,
    )
    if row is None:
        abort(404)
    return row


def _departments(hospital_id, user=None):
    """Departments offered in filters. Staff only see their own department."""
    if user is not None and user["role_code"] == "staff":
        return query(
            "SELECT * FROM departments WHERE hospital_id = ? AND id = ?",
            (hospital_id, user["department_id"]),
        )
    return query(
        "SELECT * FROM departments WHERE hospital_id = ? ORDER BY name", (hospital_id,)
    )


def _categories(hospital_id):
    return query(
        "SELECT * FROM equipment_categories WHERE hospital_id = ? ORDER BY name",
        (hospital_id,),
    )


def _engineers(hospital_id):
    return query(
        """SELECT u.id, u.full_name FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.hospital_id = ? AND r.code = 'engineer' AND u.is_active = 1
            ORDER BY u.full_name""",
        (hospital_id,),
    )


def visible_equipment(hospital_id, user):
    """Equipment rows the current user may see, filtered by department for staff."""
    sql = """SELECT e.*, d.name AS department_name, c.name AS category_name,
                    u.full_name AS engineer_name
               FROM equipment e
               LEFT JOIN departments d ON d.id = e.department_id
               LEFT JOIN equipment_categories c ON c.id = e.category_id
               LEFT JOIN users u ON u.id = e.assigned_engineer_id
              WHERE e.hospital_id = ?"""
    params = [hospital_id]
    if user["role_code"] == "staff":
        sql += " AND e.department_id IS ?"
        params.append(user["department_id"])
    return sql, params


@bp.get("/equipment")
@login_required
def equipment_list():
    user = current_user()
    base, params = visible_equipment(user["hospital_id"], user)

    dept = request.args.get("department") or ""
    cat = request.args.get("category") or ""
    status = request.args.get("status") or ""
    term = (request.args.get("q") or "").strip()

    if dept.isdigit():
        base += " AND e.department_id = ?"
        params.append(int(dept))
    if cat.isdigit():
        base += " AND e.category_id = ?"
        params.append(int(cat))
    if status in EQUIPMENT_STATUSES:
        base += " AND e.status = ?"
        params.append(status)
    if term:
        base += """ AND (e.name LIKE ? OR e.asset_number LIKE ? OR e.serial_number LIKE ?
                        OR e.manufacturer LIKE ? OR e.model_number LIKE ?)"""
        params.extend([f"%{term}%"] * 5)

    base += " ORDER BY e.name"
    rows = query(base, params)
    return render_template(
        "equipment_list.html",
        equipment=rows,
        departments=_departments(user["hospital_id"], user),
        categories=_categories(user["hospital_id"]),
        filters={"department": dept, "category": cat, "status": status, "q": term},
        today=date.today().isoformat(),
    )


def _view(mapping):
    """Template-friendly view of a form row.

    ``sqlite3.Row`` objects have no ``.get``, so templates receive this dict-like
    callable instead. Empty values fall back to the supplied default.
    """
    data = dict(mapping or {})
    return lambda key, default="": (data.get(key) if data.get(key) not in (None, "") else default)


def _form_choices(hospital_id):
    return {
        "departments": _departments(hospital_id),
        "categories": _categories(hospital_id),
        "engineers": _engineers(hospital_id),
    }


def _validate_equipment(hospital_id, exclude_id=None):
    errors = []
    name = form("name")
    if not name:
        errors.append("Equipment name is required.")
    dept_id = form_int("department_id")
    if dept_id and not query(
        "SELECT id FROM departments WHERE id = ? AND hospital_id = ?",
        (dept_id, hospital_id),
        one=True,
    ):
        errors.append("Select a department from this hospital.")
    cat_id = form_int("category_id")
    if cat_id and not query(
        "SELECT id FROM equipment_categories WHERE id = ? AND hospital_id = ?",
        (cat_id, hospital_id),
        one=True,
    ):
        errors.append("Select a category from this hospital.")
    eng_id = form_int("assigned_engineer_id")
    if eng_id and not query(
        """SELECT u.id FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.id = ? AND u.hospital_id = ? AND r.code = 'engineer'""",
        (eng_id, hospital_id),
        one=True,
    ):
        errors.append("Assigned engineer must be an active engineer in this hospital.")
    status = form("status", "operational")
    if status not in EQUIPMENT_STATUSES:
        errors.append("Unknown equipment status.")
    asset = form("asset_number")
    if asset:
        dup = query(
            "SELECT id FROM equipment WHERE hospital_id = ? AND asset_number = ?"
            + (" AND id != ?" if exclude_id else ""),
            (hospital_id, asset) + ((exclude_id,) if exclude_id else ()),
            one=True,
        )
        if dup:
            errors.append("That equipment identification number is already in use.")
    for field, label_text in (
        ("installation_date", "Installation date"),
        ("purchase_date", "Purchase date"),
        ("warranty_expiry", "Warranty expiry"),
        ("next_maintenance_date", "Next maintenance date"),
    ):
        value = form(field)
        if value and not _is_date(value):
            errors.append(f"{label_text} must be a valid date (YYYY-MM-DD).")
    return errors


def _is_date(value):
    from datetime import date

    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


@bp.route("/equipment/new", methods=["GET", "POST"])
@engineer_or_admin
def equipment_new():
    hospital_id = current_user()["hospital_id"]
    if request.method == "POST":
        errors = _validate_equipment(hospital_id)
        image_path = None
        photo = request.files.get("photo")
        if photo and photo.filename:
            try:
                image_path, _ = uploads.save_image(photo)
            except uploads.UploadError as exc:
                errors.append(str(exc))
        if errors:
            core.flash_errors(errors)
            return render_template(
                "equipment_form.html", item=request.form, view=_view(request.form),
                mode="new", **_form_choices(hospital_id)
            ), 400
        eid = execute(
            """INSERT INTO equipment (hospital_id, department_id, category_id, name,
                    manufacturer, model_number, serial_number, asset_number, location,
                    installation_date, purchase_date, warranty_expiry, purchase_cost_ugx,
                    status, assigned_engineer_id, last_maintenance_date,
                    next_maintenance_date, image_path, notes, qr_token, created_by,
                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                hospital_id, form_int("department_id"), form_int("category_id"), form("name"),
                form("manufacturer"), form("model_number"), form("serial_number"),
                form("asset_number"), form("location"), form("installation_date") or None,
                form("purchase_date") or None, form("warranty_expiry") or None,
                form_float("purchase_cost_ugx"), form("status", "operational"),
                form_int("assigned_engineer_id"), form("last_maintenance_date") or None,
                form("next_maintenance_date") or None, image_path, form("notes"),
                new_token(), current_user()["id"], now_iso(), now_iso(),
            ),
        )
        audit("equipment.create", "equipment", eid, f"Registered {form('name')}")
        flash("Equipment registered. A QR label is ready to print.", "success")
        return _redirect_after_equipment("equipment.equipment_detail", eid)
    return render_template(
        "equipment_form.html", item={}, view=_view({}), mode="new",
        **_form_choices(hospital_id)
    )


@bp.route("/equipment/<int:eid>/edit", methods=["GET", "POST"])
@engineer_or_admin
def equipment_edit(eid):
    user = current_user()
    item = _load_equipment(eid, user["hospital_id"])
    if request.method == "POST":
        errors = _validate_equipment(user["hospital_id"], exclude_id=eid)
        image_path = item["image_path"]
        photo = request.files.get("photo")
        if photo and photo.filename:
            try:
                image_path, _ = uploads.save_image(photo)
            except uploads.UploadError as exc:
                errors.append(str(exc))
        if errors:
            core.flash_errors(errors)
            return render_template(
                "equipment_form.html", item=request.form, view=_view(request.form),
                mode="edit", equipment_id=eid,
                **_form_choices(user["hospital_id"])
            ), 400
        execute(
            """UPDATE equipment SET department_id = ?, category_id = ?, name = ?,
                    manufacturer = ?, model_number = ?, serial_number = ?, asset_number = ?,
                    location = ?, installation_date = ?, purchase_date = ?,
                    warranty_expiry = ?, purchase_cost_ugx = ?, status = ?,
                    assigned_engineer_id = ?, last_maintenance_date = ?,
                    next_maintenance_date = ?, image_path = ?, notes = ?, updated_at = ?
                WHERE id = ? AND hospital_id = ?""",
            (
                form_int("department_id"), form_int("category_id"), form("name"),
                form("manufacturer"), form("model_number"), form("serial_number"),
                form("asset_number"), form("location"), form("installation_date") or None,
                form("purchase_date") or None, form("warranty_expiry") or None,
                form_float("purchase_cost_ugx"), form("status", "operational"),
                form_int("assigned_engineer_id"), form("last_maintenance_date") or None,
                form("next_maintenance_date") or None, image_path, form("notes"),
                now_iso(), eid, user["hospital_id"],
            ),
        )
        audit("equipment.update", "equipment", eid, f"Updated {form('name')}")
        flash("Equipment record updated.", "success")
        return _redirect_after_equipment("equipment.equipment_detail", eid)
    return render_template(
        "equipment_form.html", item=item, view=_view(item), mode="edit",
        equipment_id=eid, **_form_choices(user["hospital_id"])
    )


def _redirect_after_equipment(endpoint, eid):
    from flask import redirect, url_for

    return redirect(url_for(endpoint, eid=eid))


@bp.get("/equipment/<int:eid>")
@login_required
def equipment_detail(eid):
    user = current_user()
    item = _load_equipment(eid, user["hospital_id"])
    if user["role_code"] == "staff" and item["department_id"] != user["department_id"]:
        abort(403)
    faults = query(
        "SELECT * FROM fault_reports WHERE equipment_id = ? AND hospital_id = ? "
        "ORDER BY reported_at DESC LIMIT 20",
        (eid, user["hospital_id"]),
    )
    repairs = query(
        "SELECT r.*, u.full_name AS engineer_name FROM repair_records r "
        "LEFT JOIN users u ON u.id = r.engineer_id "
        "WHERE r.equipment_id = ? AND r.hospital_id = ? ORDER BY repair_date DESC LIMIT 20",
        (eid, user["hospital_id"]),
    )
    schedules = query(
        "SELECT * FROM maintenance_schedules WHERE equipment_id = ? AND hospital_id = ? "
        "ORDER BY next_due_date",
        (eid, user["hospital_id"]),
    )
    maintenance = query(
        "SELECT m.*, u.full_name AS engineer_name FROM maintenance_records m "
        "LEFT JOIN users u ON u.id = m.performed_by "
        "WHERE m.equipment_id = ? AND m.hospital_id = ? ORDER BY performed_at DESC LIMIT 20",
        (eid, user["hospital_id"]),
    )
    documents = query(
        "SELECT * FROM service_documents WHERE equipment_id = ? AND hospital_id = ? "
        "ORDER BY uploaded_at DESC",
        (eid, user["hospital_id"]),
    )
    work_orders = query(
        "SELECT w.*, u.full_name AS engineer_name FROM work_orders w "
        "LEFT JOIN users u ON u.id = w.assigned_to "
        "WHERE w.equipment_id = ? AND w.hospital_id = ? ORDER BY opened_at DESC LIMIT 20",
        (eid, user["hospital_id"]),
    )
    return render_template(
        "equipment_detail.html",
        item=item,
        faults=faults,
        repairs=repairs,
        schedules=schedules,
        maintenance=maintenance,
        documents=documents,
        work_orders=work_orders,
    )


@bp.post("/equipment/<int:eid>/clinical-use")
@engineer_or_admin
def clinical_use(eid):
    """Flag equipment as unsafe for clinical use, or clear the flag."""
    user = current_user()
    item = _load_equipment(eid, user["hospital_id"])
    action = form("action")
    if action == "block":
        reason = form("reason")
        if not reason:
            flash("A reason is required when flagging equipment as unsafe.", "error")
            return _redirect_after_equipment("equipment.equipment_detail", eid)
        execute(
            """UPDATE equipment SET clinical_use_blocked = 1, blocked_reason = ?,
                      blocked_by = ?, blocked_at = ?, status = 'out_of_service',
                      updated_at = ? WHERE id = ? AND hospital_id = ?""",
            (reason, user["id"], now_iso(), now_iso(), eid, user["hospital_id"]),
        )
        audit("equipment.block_clinical_use", "equipment", eid, reason)
        notify_role(
            user["hospital_id"], "administrator", "equipment_blocked",
            f"{item['name']} flagged as not safe for clinical use: {reason}",
            f"/equipment/{eid}", dedupe_key=f"blocked:{eid}:{now_iso()}",
        )
        flash("Equipment flagged as not safe for clinical use.", "success")
    elif action == "clear":
        execute(
            """UPDATE equipment SET clinical_use_blocked = 0, blocked_reason = NULL,
                      blocked_by = NULL, blocked_at = NULL, status = 'operational',
                      updated_at = ? WHERE id = ? AND hospital_id = ?""",
            (now_iso(), eid, user["hospital_id"]),
        )
        audit("equipment.clear_clinical_use_flag", "equipment", eid,
              "Cleared clinical-use restriction after verification")
        flash("Clinical-use restriction cleared. Verify before returning to service.",
              "success")
    else:
        abort(400)
    return _redirect_after_equipment("equipment.equipment_detail", eid)


# --------------------------------------------------------------------------
# QR
# --------------------------------------------------------------------------
def _qr_payload(item):
    return f"{request.url_root.rstrip('/')}/q/{item['qr_token']}"


@bp.get("/media/<path:filename>")
@login_required
def media(filename):
    """Serve uploaded images and manuals to signed-in hospital users only."""
    path = uploads.absolute(filename)
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path)


@bp.get("/equipment/<int:eid>/qr.png")
@login_required
def equipment_qr(eid):
    item = _load_equipment(eid, current_user()["hospital_id"])
    png = uploads.qr_png(_qr_payload(item))
    if png is None:
        abort(503, "QR rendering is unavailable.")
    return Response(png, mimetype="image/png")


@bp.get("/equipment/<int:eid>/label")
@login_required
def equipment_label(eid):
    item = _load_equipment(eid, current_user()["hospital_id"])
    return render_template("equipment_label.html", item=item, payload=_qr_payload(item))


@bp.route("/q/<token>", methods=["GET"])
def qr_lookup(token):
    """Public QR landing page: a limited summary only, no tenant detail leak."""
    row = query(
        """SELECT e.id, e.name, e.asset_number, e.serial_number, e.status,
                  e.next_maintenance_date, e.clinical_use_blocked, e.hospital_id,
                  d.name AS department_name, h.name AS hospital_name
             FROM equipment e
             LEFT JOIN departments d ON d.id = e.department_id
             JOIN hospitals h ON h.id = e.hospital_id
            WHERE e.qr_token = ?""",
        (token,),
        one=True,
    )
    if row is None:
        abort(404)
    user = current_user()
    can_report = bool(user) and user["hospital_id"] == row["hospital_id"]
    return render_template(
        "qr_public.html", item=row, token=token, can_report=can_report,
        signed_in=bool(user),
    )


@bp.get("/q/<token>/report")
def qr_report_entry(token):
    """Send a signed-in user to the fault form; anonymous users to sign-in."""
    from flask import redirect

    row = query(
        "SELECT id, hospital_id FROM equipment WHERE qr_token = ?", (token,), one=True
    )
    if row is None:
        abort(404)
    user = current_user()
    if user is None:
        return redirect(url_for("auth.login", next=url_for("faults.fault_new",
                                                          equipment_id=row["id"])))
    if user["hospital_id"] != row["hospital_id"]:
        abort(403)
    return redirect(url_for("faults.fault_new", equipment_id=row["id"]))


# --------------------------------------------------------------------------
# service manuals
# --------------------------------------------------------------------------
@bp.get("/documents")
@engineer_or_admin
def document_list():
    return equipment_documents_page()


@bp.post("/documents/upload")
@engineer_or_admin
def document_upload():
    hospital_id = current_user()["hospital_id"]
    title = form("title")
    equipment_id = form_int("equipment_id")
    file = request.files.get("manual")
    errors = []
    if not title:
        errors.append("Give the manual a title.")
    if not file or not file.filename:
        errors.append("Choose a PDF file to upload.")
    if equipment_id and not query(
        "SELECT id FROM equipment WHERE id = ? AND hospital_id = ?",
        (equipment_id, hospital_id),
        one=True,
    ):
        errors.append("Select equipment from this hospital.")
    if errors:
        core.flash_errors(errors)
        return document_list_response(400)

    try:
        rel_path, size = uploads.save_pdf(file)
    except uploads.UploadError as exc:
        flash(str(exc), "error")
        return document_list_response(400)

    doc_id = execute(
        """INSERT INTO service_documents (hospital_id, equipment_id, title, filename,
                stored_name, mime, uploaded_by, uploaded_at)
           VALUES (?, ?, ?, ?, ?, 'application/pdf', ?, ?)""",
        (hospital_id, equipment_id, title, file.filename, rel_path,
         current_user()["id"], now_iso()),
    )
    pages = _extract_pdf_pages(uploads.absolute(rel_path))
    for page_no, text in pages:
        execute(
            "INSERT INTO document_pages (document_id, page_no, text) VALUES (?, ?, ?)",
            (doc_id, page_no, text),
        )
    execute("UPDATE service_documents SET page_count = ? WHERE id = ?", (len(pages), doc_id))
    audit("document.upload", "service_document", doc_id,
          f"Uploaded {title} ({len(pages)} pages, {size} bytes)")
    if not pages:
        flash("Manual uploaded, but no searchable text was extracted. Scanned PDFs need "
              "OCR, which is not configured; BioAssist cannot cite this document.", "warning")
    else:
        flash(f"Manual uploaded and indexed ({len(pages)} pages).", "success")
    return document_list_response()


def document_list_response(status=302):
    """Re-render the manuals page (or redirect) after an upload attempt."""
    if status == 302:
        from flask import redirect

        return redirect(url_for("equipment.document_list"))
    return equipment_documents_page(), status


def equipment_documents_page():
    hospital_id = current_user()["hospital_id"]
    rows = query(
        """SELECT d.*, e.name AS equipment_name, u.full_name AS uploader
             FROM service_documents d
             LEFT JOIN equipment e ON e.id = d.equipment_id
             LEFT JOIN users u ON u.id = d.uploaded_by
            WHERE d.hospital_id = ? ORDER BY d.uploaded_at DESC""",
        (hospital_id,),
    )
    equipment = query(
        "SELECT id, name, asset_number FROM equipment WHERE hospital_id = ? ORDER BY name",
        (hospital_id,),
    )
    return render_template("documents.html", documents=rows, equipment=equipment)


def _extract_pdf_pages(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return []
    try:
        reader = PdfReader(path)
    except Exception:
        return []
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = " ".join(text.split())
        if text:
            pages.append((index, text))
    return pages


@bp.post("/documents/<int:doc_id>/delete")
@engineer_or_admin
def document_delete(doc_id):
    hospital_id = current_user()["hospital_id"]
    doc = query(
        "SELECT * FROM service_documents WHERE id = ? AND hospital_id = ?",
        (doc_id, hospital_id),
        one=True,
    )
    if doc is None:
        abort(404)
    uploads.delete(doc["stored_name"])
    execute("DELETE FROM service_documents WHERE id = ? AND hospital_id = ?",
            (doc_id, hospital_id))
    audit("document.delete", "service_document", doc_id,
          f"Deleted {doc['title']} and its extracted text")
    flash("Manual and its extracted text were deleted.", "success")
    return document_list_response()
