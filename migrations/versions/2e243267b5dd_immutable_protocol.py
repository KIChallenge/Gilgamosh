"""Enforce immutable events, prompt versions and completed call records."""

from alembic import op

revision = "2e243267b5dd"
down_revision = "e0bec71e6c72"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE FUNCTION immutable_protocol() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'Protocol records are immutable'; END; $$""")
    op.execute("""CREATE TRIGGER event_immutable BEFORE UPDATE OR DELETE ON event
                  FOR EACH ROW EXECUTE FUNCTION immutable_protocol()""")
    op.execute("""CREATE TRIGGER prompt_immutable BEFORE UPDATE OR DELETE ON prompt
                  FOR EACH ROW EXECUTE FUNCTION immutable_protocol()""")
    op.execute("""CREATE FUNCTION completed_call_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.status IN ('completed', 'invalid') THEN
        RAISE EXCEPTION 'Completed call records are immutable';
      END IF;
      RETURN NEW;
    END; $$""")
    op.execute("""CREATE TRIGGER call_immutable BEFORE UPDATE OR DELETE ON call
                  FOR EACH ROW EXECUTE FUNCTION completed_call_immutable()""")


def downgrade():
    op.execute("DROP TRIGGER call_immutable ON call")
    op.execute("DROP TRIGGER prompt_immutable ON prompt")
    op.execute("DROP TRIGGER event_immutable ON event")
    op.execute("DROP FUNCTION completed_call_immutable()")
    op.execute("DROP FUNCTION immutable_protocol()")
