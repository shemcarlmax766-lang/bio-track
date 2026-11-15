"""Shared application plumbing: sessions, CSRF, RBAC, auditing, notifications."""

import functools
import os
import secrets
import sqlite3

from flask import (
    abort,
    current_app,
    flash,
    g,
    redirect,
    request,
    session,
    url_for,
)

from db import get_connection, now_iso


# --------------------------------------------------------------------------
# connection per request
# --------------------------------------------------------------------------
def open_db():
    if "db" not in g:
        g.db = get_connection()
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(sql, params=(), one=False):
    cur = open_db().execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, params=()):
    conn = open_db()
    cur = conn.execute(sql, params)
    rowid = cur.lastrowid
    cur.close()
    return rowid


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------
def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def check_csrf():
    """Reject state-changing requests without a matching CSRF token."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf")
    if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
        abort(400, "Invalid or missing CSRF token")


# --------------------------------------------------------------------------
# authentication / authorisation
# --------------------------------------------------------------------------
def load_user():
    g.user = None
    uid = session.get("uid")
    if not uid:
        return
    row = query(
        """SELECT u.*, r.code AS role_code, r.name AS role_name,
                  h.name AS hospital_name, h.currency, h.code AS hospital_code,
                  d.name AS department_name
             FROM users u
             JOIN roles r ON r.id = u.role_id
             JOIN hospitals h ON h.id = u.hospital_id
             LEFT JOIN departments d ON d.id = u.department_id
            WHERE u.id = ?""",
        (uid,),
        one=True,
    )
    if (row is None or not row["is_active"]
            or int(row["session_epoch"]) != int(session.get("epoch") or -1)):
        session.clear()
        return
    g.user = row


def current_user():
    return getattr(g, "user", None)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            if request.path.startswith("/api/"):
                return {"error": "authentication required"}, 401
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles):
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user()["role_code"] not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def engineer_or_admin(view):
    return roles_required("engineer", "administrator")(view)


def hospital_scope():
    """Tenant id for every query. Callers must always pass this to WHERE clauses."""
    user = current_user()
    if user is None:
        abort(401)
    return user["hospital_id"]


# --------------------------------------------------------------------------
# audit + notifications
# --------------------------------------------------------------------------
def audit(action, entity=None, entity_id=None, detail=None):
    user = current_user()
    execute(
        """INSERT INTO audit_logs (hospital_id, user_id, actor_email, action, entity,
                entity_id, detail, ip, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user["hospital_id"] if user else None,
            user["id"] if user else None,
            user["email"] if user else None,
            action,
            entity,
            entity_id,
            detail,
            request.remote_addr,
            now_iso(),
        ),
    )


def notify(hospital_id, user_id, kind, message, link=None, dedupe_key=None):
    """Create an in-app notification. dedupe_key makes re-runs idempotent."""
    if dedupe_key:
        existing = query(
            "SELECT id FROM notifications WHERE hospital_id = ? AND dedupe_key = ?",
            (hospital_id, dedupe_key),
            one=True,
        )
        if existing:
            return
    execute(
        """INSERT INTO notifications (hospital_id, user_id, kind, message, link,
                dedupe_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (hospital_id, user_id, kind, message, link, dedupe_key, now_iso()),
    )


def notify_role(hospital_id, role_code, kind, message, link=None, dedupe_key=None):
    for row in query(
        """SELECT u.id FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.hospital_id = ? AND r.code = ? AND u.is_active = 1""",
        (hospital_id, role_code),
    ):
        notify(hospital_id, row["id"], kind, message, link, dedupe_key)


def refresh_maintenance_notifications(hospital_id):
    """Raise deduped reminders for upcoming and overdue maintenance."""
    from datetime import date, timedelta

    today = date.today()
    for row in query(
        """SELECT ms.id, ms.next_due_date, e.name, e.id AS equipment_id
             FROM maintenance_schedules ms
             JOIN equipment e ON e.id = ms.equipment_id
            WHERE ms.hospital_id = ? AND ms.is_active = 1
              AND e.status != 'retired'
              AND ms.next_due_date <= ?""",
        (hospital_id, (today + timedelta(days=14)).isoformat()),
    ):
        overdue = row["next_due_date"] < today.isoformat()
        kind = "maintenance_overdue" if overdue else "maintenance_upcoming"
        word = "overdue" if overdue else "due soon"
        notify_role(
            hospital_id,
            "engineer",
            kind,
            f"Preventive maintenance {word}: {row['name']} ({row['next_due_date']})",
            "/maintenance",
            dedupe_key=f"{kind}:{row['id']}:{row['next_due_date']}",
        )


def refresh_low_stock_notifications(hospital_id):
    for row in query(
        """SELECT id, name, part_number, quantity, min_stock FROM spare_parts
            WHERE hospital_id = ? AND quantity <= min_stock""",
        (hospital_id,),
    ):
        notify_role(
            hospital_id,
            "engineer",
            "low_stock",
            f"Low stock: {row['name']} ({row['part_number']}) - "
            f"{row['quantity']} left, minimum {row['min_stock']}",
            "/parts",
            dedupe_key=f"low_stock:{row['id']}:{row['quantity']}",
        )


def unread_notification_count():
    user = current_user()
    if not user:
        return 0
    row = query(
        """SELECT COUNT(*) AS n FROM notifications
            WHERE hospital_id = ? AND read_at IS NULL
              AND (user_id = ? OR user_id IS NULL)""",
        (user["hospital_id"], user["id"]),
        one=True,
    )
    return row["n"] if row else 0


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def form(name, default=""):
    return (request.form.get(name) or default).strip()


def form_int(name, default=None):
    raw = form(name)
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def form_float(name, default=None):
    raw = form(name)
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def label(value):
    return (value or "").replace("_", " ").title()


def flash_errors(errors):
    for err in errors:
        flash(err, "error")


def is_safe_redirect(target):
    return bool(target) and target.startswith("/") and not target.startswith("//")
