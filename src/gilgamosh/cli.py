import argparse
import os


def main():
    parser = argparse.ArgumentParser(description="Gilgamosh")
    parser.add_argument("command", choices=["web", "worker", "init", "seed"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    from . import create_app

    app = create_app()
    if args.command == "init":
        from .services import initialize

        initialize(app, os.environ.get("ADMIN_USERNAME", "admin"), os.environ.get("ADMIN_PASSWORD", ""))
    elif args.command == "seed":
        from .models import db
        from .stakeholders import seed_stakeholders

        with app.app_context():
            imported, preserved = seed_stakeholders(db.session)
            db.session.commit()
            print(f"{len(imported)} Profile übernommen, {len(preserved)} manuell bearbeitete Profile beibehalten.")
    elif args.command == "worker":
        from .worker import run_worker

        run_worker(app, once=args.once)
    else:
        app.run(host="127.0.0.1", port=5000, threaded=True)
