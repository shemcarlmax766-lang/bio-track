"""Departments, categories, spare parts, users, and the audit trail."""

import json
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

import core
from core import (
    audit,
    current_user,
    engineer_or_admin,
    execute,
    form,
    form_float,
    form_int,
    login_required,
    notify_role,
    query,
    roles_required,
)
from db import get_connection, now_iso

bp = Blueprint("admin", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------------
# departments and categories
# --------------------------------------------------------------------------
@bp.get("/admin/departments")
@roles_required("administrator")
def departments():
    hospital_id = current_user()["hospital_id"]
    rows = query(
        """SELECT d.*, (SELECT COUNT(*) FROM equipment e WHERE e.department_id = d.id)
                      AS equipment_count,
                   (SELECT COUNT(*) FROM users u WHERE u.department_id = d.id) AS staff_count
             FROM departments d WHERE d.hospital_id = ? ORDER BY d.name""",
        (hospital_id,),
    )
    return render_template("departments.html", departments=rows)


@bp.post("/admin/departments")
@roles_required("administrator")
def department_create():
    hospital_id = current_user()["hospital_id"]
    name = form("name")
    if len(name) < 2:
        flash("Enter a department name.", "error")
    elif query("SELECT id FROM departments WHERE hospital_id = ? AND lower(name) = ?",
               (hospital_id, name.lower()), one=True):
        flash("That department already exists.", "error")
    else:
        did = execute(
            "INSERT INTO departments (hospital_id, name, created_at) VALUES (?, ?, ?)",
            (hospital_id, name, now_iso()),
        )
        audit("department.create", "department", did, name)
        flash(f"Department '{name}' added.", "success")
    return redirect(url_for("admin.departments"))


@bp.post("/admin/departments/<int:did>/rename")
@roles_required("administrator")
def department_rename(did):
    hospital_id = current_user()["hospital_id"]
    new_name = form("name")
    if not query("SELECT id FROM departments WHERE id = ? AND hospital_id = ?",
                 (did, hospital_id), one=True):
        abort(404)
    if len(new_name) < 2:
        flash("Enter a department name.", "error")
    else:
        execute("UPDATE departments SET name = ? WHERE id = ? AND hospital_id = ?",
                (new_name, did, hospital_id))
        audit("department.rename", "department", did, f"Renamed to {new_name}")
        flash("Department renamed.", "success")
    return redirect(url_for("admin.departments"))


@bp.get("/admin/categories")
@roles_required("administrator")
def categories():
    hospital_id = current_user()["hospital_id"]
    rows = query(
        """SELECT c.*, (SELECT COUNT(*) FROM equipment e WHERE e.category_id = c.id)
                      AS equipment_count
             FROM equipment_categories c WHERE c.hospital_id = ? ORDER BY c.name""",
        (hospital_id,),
    )
    return render_template("categories.html", categories=rows)


@bp.post("/admin/categories")
@roles_required("administrator")
def category_create():
    hospital_id = current_user()["hospital_id"]
    name = form("name")
    description = form("description")
    if len(name) < 2:
        flash("Enter a category name.", "error")
    elif query("SELECT id FROM equipment_categories WHERE hospital_id = ? AND lower(name) = ?",
               (hospital_id, name.lower()), one=True):
        flash("That category already exists.", "error")
    else:
        cid = execute(
            """INSERT INTO equipment_categories (hospital_id, name, description, created_at)
               VALUES (?, ?, ?, ?)""",
            (hospital_id, name, description, now_iso()),
        )
        audit("category.create", "equipment_category", cid, name)
        flash(f"Category '{name}' added.", "success")
    return redirect(url_for("admin.categories"))


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------
@bp.get("/admin/users")
@roles_required("administrator")
def users():
    hospital_id = current_user()["hospital_id"]
    rows = query(
        """SELECT u.*, r.code AS role_code, r.name AS role_name, d.name AS department_name
             FROM users u JOIN roles r ON r.id = u.role_id
             LEFT JOIN departments d ON d.id = u.department_id
            WHERE u.hospital_id = ? ORDER BY u.full_name""",
        (hospital_id,),
    )
    return render_template(
        "users.html", users=rows,
        departments=query("SELECT * FROM departments WHERE hospital_id = ? ORDER BY name",
                          (hospital_id,)),
        roles=query("SELECT * FROM roles ORDER BY id"),
    )


@bp.post("/admin/users")
@roles_required("administrator")
def user_create():
    hospital_id = current_user()["hospital_id"]
    email = form("email").lower()
    full_name = form("full_name")
    password = request.form.get("password") or ""
    role_code = form("role_code")
    department_id = form_int("department_id")
    errors = []
    if not full_name:
        errors.append("Enter the user's full name.")
    if not EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    if len(password) < 10:
        errors.append("Initial password must be at least 10 characters.")
    role = query("SELECT * FROM roles WHERE code = ?", (role_code,), one=True)
    if role is None:
        errors.append("Choose a valid role.")
    if department_id and not query(
        "SELECT id FROM departments WHERE id = ? AND hospital_id = ?",
        (department_id, hospital_id), one=True,
    ):
        errors.append("Choose a department from this hospital.")
    if query("SELECT id FROM users WHERE lower(email) = ?", (email,), one=True):
        errors.append("That email is already registered.")
    if errors:
        core.flash_errors(errors)
        return redirect(url_for("admin.users"))
    uid = execute(
        """INSERT INTO users (hospital_id, department_id, role_id, full_name, email,
                password_hash, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (hospital_id, department_id, role["id"], full_name, email,
         generate_password_hash(password, method="pbkdf2:sha256"), now_iso()),
    )
    audit("user.create", "user", uid, f"Created {email} as {role['name']}")
    flash(f"User {full_name} created.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/admin/users/<int:uid>/update")
@roles_required("administrator")
def user_update(uid):
    hospital_id = current_user()["hospital_id"]
    actor = current_user()
    target = query("SELECT * FROM users WHERE id = ? AND hospital_id = ?",
                   (uid, hospital_id), one=True)
    if target is None:
        abort(404)
    role = query("SELECT * FROM roles WHERE code = ?", (form("role_code"),), one=True)
    department_id = form_int("department_id")
    if role is None:
        flash("Choose a valid role.", "error")
        return redirect(url_for("admin.users"))
    if target["id"] == actor["id"] and role["id"] != target["role_id"]:
        flash("You cannot change your own role.", "error")
        return redirect(url_for("admin.users"))
    if department_id and not query(
        "SELECT id FROM departments WHERE id = ? AND hospital_id = ?",
        (department_id, hospital_id), one=True,
    ):
        flash("Choose a department from this hospital.", "error")
        return redirect(url_for("admin.users"))
    execute(
        """UPDATE users SET role_id = ?, department_id = ?, full_name = ?, phone = ?
            WHERE id = ? AND hospital_id = ?""",
        (role["id"], department_id, form("full_name") or target["full_name"],
         form("phone"), uid, hospital_id),
    )
    audit("user.update", "user", uid,
          f"Role set to {role['name']}; department {department_id}")
    flash("User updated. Existing sessions keep the old permissions until next sign-in.",
          "info")
    return redirect(url_for("admin.users"))


@bp.post("/admin/users/<int:uid>/active")
@roles_required("administrator")
def user_active(uid):
    hospital_id = current_user()["hospital_id"]
    actor = current_user()
    target = query("SELECT * FROM users WHERE id = ? AND hospital_id = ?",
                   (uid, hospital_id), one=True)
    if target is None:
        abort(404)
    if target["id"] == actor["id"]:
        flash("You cannot suspend your own account.", "error")
        return redirect(url_for("admin.users"))
    active = 0 if target["is_active"] else 1
    execute(
        """UPDATE users SET is_active = ?, session_epoch = session_epoch + 1
            WHERE id = ? AND hospital_id = ?""",
        (active, uid, hospital_id),
    )
    audit("user.active", "user", uid, "Activated" if active else "Suspended")
    flash("Account activated." if active else "Account suspended and sessions revoked.",
          "info")
    return redirect(url_for("admin.users"))


@bp.post("/admin/users/<int:uid>/reset-password")
@roles_required("administrator")
def user_reset_password(uid):
    hospital_id = current_user()["hospital_id"]
    target = query("SELECT * FROM users WHERE id = ? AND hospital_id = ?",
                   (uid, hospital_id), one=True)
    if target is None:
        abort(404)
    new_password = request.form.get("password") or ""
    if len(new_password) < 10:
        flash("The new password must be at least 10 characters.", "error")
        return redirect(url_for("admin.users"))
    execute(
        """UPDATE users SET password_hash = ?, failed_logins = 0, locked_until = NULL,
                  session_epoch = session_epoch + 1
            WHERE id = ? AND hospital_id = ?""",
        (generate_password_hash(new_password, method="pbkdf2:sha256"), uid, hospital_id),
    )
    audit("user.password_reset", "user", uid, "Administrator reset the password")
    flash("Password reset. All existing sessions for that user were revoked.", "success")
    return redirect(url_for("admin.users"))


# --------------------------------------------------------------------------
# spare parts
# --------------------------------------------------------------------------
@bp.get("/parts")
@engineer_or_admin
def parts():
    hospital_id = current_user()["hospital_id"]
    rows = query(
        """SELECT p.*, (SELECT COUNT(*) FROM spare_part_transactions t
                        WHERE t.part_id = p.id) AS movements
             FROM spare_parts p WHERE p.hospital_id = ? ORDER BY p.name""",
        (hospital_id,),
    )
    return render_template("parts.html", parts=rows)


@bp.post("/parts")
@engineer_or_admin
def part_create():
    user = current_user()
    hospital_id = user["hospital_id"]
    name, part_number = form("name"), form("part_number")
    errors = []
    if not name:
        errors.append("Part name is required.")
    if not part_number:
        errors.append("Part number is required.")
    if part_number and query(
        "SELECT id FROM spare_parts WHERE hospital_id = ? AND part_number = ?",
        (hospital_id, part_number), one=True,
    ):
        errors.append("That part number already exists in this hospital.")
    opening = form_int("quantity", 0) or 0
    min_stock = form_int("min_stock", 0) or 0
    if opening < 0:
        errors.append("Quantity cannot be negative.")
    if min_stock < 0:
        errors.append("Minimum stock cannot be negative.")
    if errors:
        core.flash_errors(errors)
        return redirect(url_for("admin.parts"))

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """INSERT INTO spare_parts (hospital_id, name, part_number, manufacturer,
                    compatible_equipment, quantity, min_stock, supplier, purchase_date,
                    storage_location, unit_cost_ugx, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hospital_id, name, part_number, form("manufacturer"),
             form("compatible_equipment"), opening, min_stock, form("supplier"),
             form("purchase_date") or None, form("storage_location"),
             form_float("unit_cost_ugx"), now_iso()),
        )
        part_id = cur.lastrowid
        conn.execute(
            """INSERT INTO spare_part_transactions (hospital_id, part_id, kind,
                    quantity_delta, balance_after, reason, user_id, created_at)
               VALUES (?, ?, 'opening', ?, ?, 'Opening balance', ?, ?)""",
            (hospital_id, part_id, opening, opening, user["id"], now_iso()),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    audit("part.create", "spare_part", part_id,
          f"{name} ({part_number}) opening balance {opening}")
    flash("Spare part added.", "success")
    return redirect(url_for("admin.parts"))


def _part_or_404(part_id, hospital_id):
    row = query("SELECT * FROM spare_parts WHERE id = ? AND hospital_id = ?",
                (part_id, hospital_id), one=True)
    if row is None:
        abort(404)
    return row


@bp.post("/parts/<int:part_id>/movement")
@engineer_or_admin
def part_movement(part_id):
    user = current_user()
    hospital_id = user["hospital_id"]
    part = _part_or_404(part_id, hospital_id)
    kind = form("kind")
    quantity = form_int("quantity")
    reason = form("reason")
    work_order_id = form_int("work_order_id")
    if kind not in ("receipt", "issue", "correction"):
        abort(400)
    if not quantity or quantity <= 0:
        flash("Enter a quantity greater than zero.", "error")
        return redirect(request.referrer or url_for("admin.parts"))
    if not reason:
        flash("A reason is required for every stock movement.", "error")
        return redirect(request.referrer or url_for("admin.parts"))
    if kind == "correction" and form("direction") == "decrease":
        delta = -quantity
    else:
        delta = quantity if kind in ("receipt", "correction") else -quantity

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT quantity FROM spare_parts WHERE id = ? AND hospital_id = ?",
            (part_id, hospital_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            abort(404)
        balance = row["quantity"] + delta
        if balance < 0:
            conn.execute("ROLLBACK")
            flash("Insufficient stock: that issue would take the balance below zero.",
                  "error")
            return redirect(request.referrer or url_for("admin.parts"))
        conn.execute(
            "UPDATE spare_parts SET quantity = ? WHERE id = ? AND hospital_id = ?",
            (balance, part_id, hospital_id),
        )
        conn.execute(
            """INSERT INTO spare_part_transactions (hospital_id, part_id, kind,
                    quantity_delta, balance_after, reason, work_order_id, user_id,
                    created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hospital_id, part_id, kind, delta, balance, reason, work_order_id,
             user["id"], now_iso()),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    audit("part.movement", "spare_part", part_id,
          f"{kind} {delta:+d} -> {balance} ({reason})")
    if balance <= part["min_stock"]:
        notify_role(
            hospital_id, "engineer", "low_stock",
            f"Low stock: {part['name']} ({part['part_number']}) - {balance} left, "
            f"minimum {part['min_stock']}",
            url_for("admin.part_detail", part_id=part_id),
            dedupe_key=f"low_stock:{part_id}:{balance}",
        )
    flash("Stock movement recorded.", "success")
    return redirect(request.referrer or url_for("admin.parts"))


@bp.get("/parts/<int:part_id>")
@engineer_or_admin
def part_detail(part_id):
    hospital_id = current_user()["hospital_id"]
    part = _part_or_404(part_id, hospital_id)
    ledger = query(
        """SELECT t.*, u.full_name AS user_name FROM spare_part_transactions t
             LEFT JOIN users u ON u.id = t.user_id
            WHERE t.part_id = ? AND t.hospital_id = ? ORDER BY t.created_at DESC""",
        (part_id, hospital_id),
    )
    work_orders = query(
        """SELECT w.id, e.name AS equipment_name FROM work_orders w
             JOIN equipment e ON e.id = w.equipment_id
            WHERE w.hospital_id = ? AND w.status != 'closed' ORDER BY w.opened_at DESC""",
        (hospital_id,),
    )
    return render_template("part_detail.html", part=part, ledger=ledger,
                           work_orders=work_orders)


# --------------------------------------------------------------------------
# audit trail
# --------------------------------------------------------------------------
@bp.get("/admin/audit")
@roles_required("administrator")
def audit_log():
    hospital_id = current_user()["hospital_id"]
    term = (request.args.get("q") or "").strip()
    sql = """SELECT * FROM audit_logs WHERE hospital_id = ?"""
    params = [hospital_id]
    if term:
        sql += " AND (action LIKE ? OR actor_email LIKE ? OR detail LIKE ?)"
        params.extend([f"%{term}%"] * 3)
    sql += " ORDER BY created_at DESC LIMIT 500"
    return render_template("audit.html", logs=query(sql, params), term=term)
