from datetime import UTC, date, datetime
from uuid import uuid4

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import JSON, Computed, Date, DateTime, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid4())


db = SQLAlchemy()
json_type = JSON().with_variant(JSONB, "postgresql")


class User(UserMixin, db.Model):
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]


class Endpoint(db.Model):
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(unique=True)
    base_url: Mapped[str] = mapped_column(default="https://openrouter.ai/api/v1")
    secret: Mapped[str] = mapped_column(default="")
    model: Mapped[str] = mapped_column(default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    timeout: Mapped[int] = mapped_column(default=120)


class Setting(db.Model):
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[dict] = mapped_column(json_type, default=dict)


class Actor(db.Model):
    id: Mapped[str] = mapped_column(primary_key=True)  # stable import code
    name: Mapped[str]
    parent_id: Mapped[str | None] = mapped_column(db.ForeignKey("actor.id"))
    profile: Mapped[dict] = mapped_column(json_type)
    endpoint_id: Mapped[str | None] = mapped_column(db.ForeignKey("endpoint.id"))
    model: Mapped[str] = mapped_column(default="")


class Prompt(db.Model):
    __table_args__ = (UniqueConstraint("role", "version"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    role: Mapped[str]
    version: Mapped[int]
    text: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Simulation(db.Model):
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    question: Mapped[str]
    snapshot: Mapped[dict] = mapped_column(json_type)
    status: Mapped[str] = mapped_column(default="running")
    run: Mapped[int] = mapped_column(default=1)
    block: Mapped[int] = mapped_column(default=1)
    round: Mapped[int] = mapped_column(default=1)
    phase: Mapped[str] = mapped_column(default="start")
    state: Mapped[dict] = mapped_column(json_type, default=dict)
    error: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(db.Model):
    __table_args__ = (UniqueConstraint("simulation_id", "key"),)
    key: Mapped[str | None]
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    simulation_id: Mapped[str] = mapped_column(db.ForeignKey("simulation.id"), index=True)
    run: Mapped[int]
    block: Mapped[int]
    round: Mapped[int]
    phase: Mapped[str]
    kind: Mapped[str]
    actor: Mapped[str] = mapped_column(default="")
    group: Mapped[str] = mapped_column(default="")
    visibility: Mapped[str] = mapped_column(default="public")
    payload: Mapped[dict] = mapped_column(json_type)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Call(db.Model):
    __table_args__ = (UniqueConstraint("simulation_id", "key"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    simulation_id: Mapped[str] = mapped_column(db.ForeignKey("simulation.id"), index=True)
    key: Mapped[str]
    run: Mapped[int]
    block: Mapped[int]
    round: Mapped[int]
    phase: Mapped[str]
    role: Mapped[str]
    actor: Mapped[str] = mapped_column(default="")
    group: Mapped[str] = mapped_column(default="")
    status: Mapped[str] = mapped_column(default="queued")
    request: Mapped[dict] = mapped_column(json_type)
    response: Mapped[dict] = mapped_column(json_type, default=dict)
    result: Mapped[dict] = mapped_column(json_type, default=dict)
    usage: Mapped[dict] = mapped_column(json_type, default=dict)
    duration: Mapped[float] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(db.Model):
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    simulation_id: Mapped[str] = mapped_column(db.ForeignKey("simulation.id"), index=True)
    kind: Mapped[str]  # phase, watcher, report
    payload: Mapped[dict] = mapped_column(json_type, default=dict)
    status: Mapped[str] = mapped_column(default="queued", index=True)
    token: Mapped[str] = mapped_column(default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Source(db.Model):
    """A press article or legal provision. The full text never leaves through the regular views."""

    __table_args__ = (
        Index("ix_source_search", "search", postgresql_using="gin"),
        Index("ix_source_kind", "kind"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(unique=True)
    kind: Mapped[str]  # press, law
    title: Mapped[str]
    publisher: Mapped[str]
    citation: Mapped[str]
    published: Mapped[date | None] = mapped_column(Date)
    body: Mapped[str]
    search: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('german', title || ' ' || body)", persisted=True)
    )


class Command(db.Model):
    __table_args__ = (UniqueConstraint("simulation_id", "key"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    simulation_id: Mapped[str] = mapped_column(db.ForeignKey("simulation.id"))
    key: Mapped[str]
    action: Mapped[str]
