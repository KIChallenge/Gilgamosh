"""Add searchable press and legal sources."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a7c3f1d29b40"
down_revision = "2e243267b5dd"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("publisher", sa.String(), nullable=False),
        sa.Column("citation", sa.String(), nullable=False),
        sa.Column("published", sa.Date(), nullable=True),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('german', title || ' ' || body)", persisted=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_source_search", "source", ["search"], postgresql_using="gin")
    op.create_index("ix_source_kind", "source", ["kind"])


def downgrade():
    op.drop_index("ix_source_kind", table_name="source")
    op.drop_index("ix_source_search", table_name="source")
    op.drop_table("source")
