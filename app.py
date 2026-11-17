"""BioTrack application factory."""

import os

from flask import Flask, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

import core
import db as dbmod


def create_app():
    app = Flask(__name__, instance_relative_config=False)

    secret = os.environ.get("SECRET_KEY")
    if not secret:
        secret = _persisted_secret()
    app.config.update(
        SECRET_KEY=secret,
        SESSION_COOKIE_NAME="biotrack_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        BIOASSIST_API_KEY=os.environ.get("BIOASSIST_API_KEY", ""),
        BIOASSIST_API_URL=os.environ.get(
            "BIOASSIST_API_URL", "https://api.openai.com/v1/chat/completions"
        ),
        BIOASSIST_MODEL=os.environ.get("BIOASSIST_MODEL", "gpt-4o-mini"),
        SMTP_CONFIGURED=False,
    )

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    dbmod.init_db()

    # ---- lifecycle hooks -------------------------------------------------
    app.before_request(_before_request)
    app.teardown_appcontext(core.close_db)

    # ---- template helpers ------------------------------------------------
    app.jinja_env.filters["label"] = core.label
    app.jinja_env.filters["ugx"] = _format_ugx
    app.jinja_env.filters["short_dt"] = _short_dt
    app.jinja_env.globals.update(
        csrf_token=core.csrf_token,
        open_faults=core.open_fault_count,
        current_user=core.current_user,
        unread_notifications=core.unread_notification_count,
        EQUIPMENT_STATUSES=dbmod.EQUIPMENT_STATUSES,
        FAULT_STATUSES=dbmod.FAULT_STATUSES,
        SEVERITIES=dbmod.SEVERITIES,
        label=core.label,
        app_name="BioTrack",
    )

    # ---- blueprints ------------------------------------------------------
    from views_admin import bp as admin_bp
    from views_auth import bp as auth_bp
    from views_bioassist import bp as bio_bp
    from views_dashboard import bp as dash_bp
    from views_equipment import bp as equip_bp
    from views_faults import bp as fault_bp
    from views_maintenance import bp as maint_bp
    from views_reports import bp as report_bp

    for bp in (auth_bp, dash_bp, equip_bp, fault_bp, maint_bp, report_bp, bio_bp,
               admin_bp):
        app.register_blueprint(bp)

    # ---- error handlers --------------------------------------------------
    for code in (400, 403, 404, 405, 413, 500):
        app.register_error_handler(code, _error_page)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.path.startswith("/static/"):
            resp.headers.setdefault("Cache-Control", "public, max-age=86400")
        return resp

    return app


def _before_request():
    if request.endpoint == "static":
        return
    # Secure cookie flag follows the effective request scheme (ProxyFix has already
    # applied X-Forwarded-Proto). COOKIE_SECURE=1/0 overrides it for TLS proxies
    # that do not forward the scheme.
    force = os.environ.get("COOKIE_SECURE")
    session.permanent = False
    if force == "1" or (force != "0" and request.is_secure):
        from flask import current_app

        current_app.config["SESSION_COOKIE_SECURE"] = True
    core.load_user()
    core.check_csrf()


def _persisted_secret():
    """Fall back to a key file so sessions survive restarts without env config."""
    path = os.path.join(dbmod.DATA_DIR, ".secret_key")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        pass
    value = os.urandom(32).hex()
    os.makedirs(dbmod.DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value)
    os.chmod(path, 0o600)
    return value


def _format_ugx(value):
    if value in (None, ""):
        return "-"
    try:
        return f"UGX {float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _short_dt(value):
    if not value:
        return "-"
    return str(value).replace("T", " ")[:16]


def _error_page(err):
    code = getattr(err, "code", 500)
    message = {
        400: "That request could not be processed. Please check the form and try again.",
        403: "Your role does not permit this action.",
        404: "The page or record you asked for does not exist.",
        405: "That action is not allowed on this address.",
        413: "The file is too large. Uploads are limited to 25 MB.",
        500: "Something went wrong on our side. The error has not changed your data.",
    }.get(code, "Unexpected error.")
    if request.path.startswith("/api/"):
        return {"error": message}, code
    return render_template("error.html", code=code, message=message), code


app = create_app()
