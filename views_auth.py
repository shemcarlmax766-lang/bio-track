"""Authentication, hospital onboarding, and password management."""

import re
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import core
from core import (
    csrf_token,
    current_user,
    execute,
    form,
    login_required,
    query,
    roles_required,
)
from db import get_connection, now_iso, provision_hospital

bp = Blueprint("auth", __name__)

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        email = form("email").lower()
        password = request.form.get("password") or ""
        row = query(
            """SELECT u.*, r.code AS role_code FROM users u
                 JOIN roles r ON r.id = u.role_id
                WHERE lower(u.email) = ?""",
            (email,),
            one=True,
        )
        generic = "Email or password is incorrect."
        if row is None:
            flash(generic, "error")
            return render_template("login.html", email=email), 401
        if row["locked_until"] and row["locked_until"] > now_iso():
            flash("Too many failed attempts. Try again in a few minutes.", "error")
            return render_template("login.html", email=email), 429
        if not row["is_active"]:
            flash("This account is suspended. Contact your administrator.", "error")
            return render_template("login.html", email=email), 403
        if not check_password_hash(row["password_hash"], password):
            failures = row["failed_logins"] + 1
            lock = None
            if failures >= MAX_FAILED_LOGINS:
                lock = (
                    datetime.now(timezone.utc).replace(tzinfo=None)
                    + timedelta(minutes=LOCK_MINUTES)
                ).replace(microsecond=0).isoformat()
                failures = 0
            execute(
                "UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                (failures, lock, row["id"]),
            )
            flash(generic, "error")
            return render_template("login.html", email=email), 401

        execute(
            """UPDATE users SET failed_logins = 0, locked_until = NULL,
                      last_login_at = ? WHERE id = ?""",
            (now_iso(), row["id"]),
        )
        session.clear()
        session["uid"] = row["id"]
        session["epoch"] = row["session_epoch"]
        csrf_token()
        execute(
            """INSERT INTO audit_logs (hospital_id, user_id, actor_email, action,
                    entity, detail, ip, created_at)
               VALUES (?, ?, ?, 'login', 'user', ?, ?, ?)""",
            (row["hospital_id"], row["id"], row["email"], "Signed in",
             request.remote_addr, now_iso()),
        )
        nxt = request.args.get("next") or request.form.get("next")
        return redirect(nxt if core.is_safe_redirect(nxt) else url_for("dashboard.index"))
    return render_template("login.html", email="")


@bp.post("/logout")
@login_required
def logout():
    core.audit("logout", "user", current_user()["id"], "Signed out")
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/register-hospital", methods=["GET", "POST"])
def register_hospital():
    """Self-service hospital onboarding. Creates the tenant and its first admin."""
    if current_user():
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        hospital_name = form("hospital_name")
        district = form("district")
        full_name = form("full_name")
        email = form("email").lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("password_confirm") or ""
        errors = []
        if len(hospital_name) < 3:
            errors.append("Enter the hospital name (at least 3 characters).")
        if not full_name:
            errors.append("Enter your full name.")
        if not EMAIL_RE.match(email):
            errors.append("Enter a valid email address.")
        if len(password) < MIN_PASSWORD:
            errors.append(f"Password must be at least {MIN_PASSWORD} characters.")
        if password != confirm:
            errors.append("The two passwords do not match.")
        if not errors and query("SELECT id FROM users WHERE lower(email) = ?", (email,), one=True):
            errors.append("An account with that email already exists.")
        if errors:
            core.flash_errors(errors)
            return render_template("register_hospital.html", form_data=request.form), 400

        code = _hospital_code(hospital_name)
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            hid = provision_hospital(conn, hospital_name, code, district or None)
            role_id = conn.execute(
                "SELECT id FROM roles WHERE code = 'administrator'"
            ).fetchone()["id"]
            cur = conn.execute(
                """INSERT INTO users (hospital_id, role_id, full_name, email,
                        password_hash, is_active, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (hid, role_id, full_name, email,
                 generate_password_hash(password, method="pbkdf2:sha256"), now_iso()),
            )
            conn.execute(
                """INSERT INTO audit_logs (hospital_id, user_id, actor_email, action,
                        entity, detail, created_at)
                   VALUES (?, ?, ?, 'hospital.create', 'hospital', ?, ?)""",
                (hid, cur.lastrowid, email, f"Onboarded {hospital_name}", now_iso()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        flash("Hospital created. Sign in with your administrator account.", "success")
        return redirect(url_for("auth.login"))
    return render_template("register_hospital.html", form_data={})


def _hospital_code(name):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").upper()[:20] or "HOSP"
    base, n = slug, 1
    while query("SELECT id FROM hospitals WHERE code = ?", (base,), one=True):
        n += 1
        base = f"{slug}-{n}"
    return base


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("password_confirm") or ""
        user = current_user()
        errors = []
        if not check_password_hash(user["password_hash"], current):
            errors.append("Your current password is incorrect.")
        if len(new) < MIN_PASSWORD:
            errors.append(f"New password must be at least {MIN_PASSWORD} characters.")
        if new != confirm:
            errors.append("The new passwords do not match.")
        if errors:
            core.flash_errors(errors)
        else:
            execute(
                """UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1
                    WHERE id = ?""",
                (generate_password_hash(new, method="pbkdf2:sha256"), user["id"]),
            )
            core.audit("user.password_change", "user", user["id"], "Changed own password")
            session.clear()
            flash("Password changed. Please sign in again.", "success")
            return redirect(url_for("auth.login"))
    return render_template("account.html")


@bp.post("/account/profile")
@login_required
def update_profile():
    phone = form("phone")
    execute("UPDATE users SET phone = ? WHERE id = ?", (phone, current_user()["id"]))
    core.audit("user.profile_update", "user", current_user()["id"], "Updated contact details")
    flash("Contact details updated.", "success")
    return redirect(url_for("auth.account"))


# --------------------------------------------------------------------------
# notifications inbox
# --------------------------------------------------------------------------
@bp.get("/notifications")
@login_required
def notifications():
    user = current_user()
    rows = query(
        """SELECT * FROM notifications
            WHERE hospital_id = ? AND (user_id = ? OR user_id IS NULL)
            ORDER BY created_at DESC LIMIT 100""",
        (user["hospital_id"], user["id"]),
    )
    return render_template("notifications.html", notifications=rows)


@bp.post("/notifications/read")
@login_required
def mark_all_read():
    user = current_user()
    execute(
        """UPDATE notifications SET read_at = ?
            WHERE hospital_id = ? AND read_at IS NULL
              AND (user_id = ? OR user_id IS NULL)""",
        (now_iso(), user["hospital_id"], user["id"]),
    )
    return redirect(url_for("auth.notifications"))


@bp.post("/notifications/<int:note_id>/read")
@login_required
def mark_read(note_id):
    user = current_user()
    execute(
        """UPDATE notifications SET read_at = ?
            WHERE id = ? AND hospital_id = ? AND (user_id = ? OR user_id IS NULL)""",
        (now_iso(), note_id, user["hospital_id"], user["id"]),
    )
    nxt = request.form.get("next") or url_for("auth.notifications")
    return redirect(nxt if core.is_safe_redirect(nxt) else url_for("auth.notifications"))
