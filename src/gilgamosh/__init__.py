import logging
import os
from datetime import timedelta

from flask import Flask, jsonify, redirect, request, url_for
from flask_login import LoginManager
from flask_wtf.csrf import CSRFError, CSRFProtect

from .models import User, db
from .security import PublicError


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", ""),
        ENCRYPTION_KEY=os.environ.get("ENCRYPTION_KEY", ""),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "postgresql+psycopg://gilgamosh:gilgamosh@localhost:5432/gilgamosh"
        ),
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        LLM_MODE=os.environ.get("LLM_MODE", "openrouter"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        MAX_CONTEXT_CHARS=int(os.environ.get("MAX_CONTEXT_CHARS", "240000")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    app.config.update(config or {})
    if not app.config["SECRET_KEY"] or not app.config["ENCRYPTION_KEY"]:
        raise RuntimeError("SECRET_KEY und ENCRYPTION_KEY müssen konfiguriert sein; siehe docs/development.md.")
    if app.config["LLM_MODE"] not in {"openrouter", "fake"}:
        raise RuntimeError("LLM_MODE muss openrouter oder fake sein.")
    db.init_app(app)
    CSRFProtect(app)
    login = LoginManager(app)
    login.login_view = "web.login"

    @login.user_loader
    def load_user(user_id):
        return db.session.get(User, user_id)

    @login.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api/"):
            return jsonify(error="Anmeldung erforderlich."), 401
        return redirect(url_for("web.login"))

    @app.errorhandler(PublicError)
    def public_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        return jsonify(error="Formular abgelaufen. Seite neu laden."), 400

    @app.errorhandler(500)
    def internal_error(error):
        return jsonify(error="Interner Fehler. Bitte Serverprotokoll prüfen."), 500

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    from .web import web

    app.register_blueprint(web)
    logging.basicConfig(level=logging.INFO)
    return app
