from alembic import context

from gilgamosh import create_app
from gilgamosh.models import db

app = create_app()
with app.app_context(), db.engine.connect() as connection:
    context.configure(connection=connection, target_metadata=db.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
