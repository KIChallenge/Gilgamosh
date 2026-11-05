import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gilgamosh import create_app
from gilgamosh.models import Actor, db
from gilgamosh.services import create_simulation, initialize
from gilgamosh.worker import claim, execute

TEST_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:gilgamosh-test@localhost:55432/gilgamosh_test"
)


@pytest.fixture
def app():
    if "test" not in TEST_URL.rsplit("/", 1)[-1]:
        raise RuntimeError("Tests require a dedicated database with test in its name.")
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "SQLALCHEMY_DATABASE_URI": TEST_URL,
            "WTF_CSRF_ENABLED": False,
            "LLM_MODE": "fake",
        }
    )
    with app.app_context():
        db.drop_all()
        db.create_all()
    initialize(app, "admin", "test-password-long-enough")
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def client(app):
    client = app.test_client()
    response = client.post("/login", data={"username": "admin", "password": "test-password-long-enough"})
    assert response.status_code == 302
    return client


@pytest.fixture
def simulation(app):
    def create(**overrides):
        with app.app_context():
            ids = list(
                db.session.scalars(
                    select(Actor.id).where(Actor.id.in_(["aerztekammer", "finanzministerium"])).order_by(Actor.id)
                )
            )
            sim = create_simulation(
                db.session,
                {"question": "Wie kann die regionale Versorgung verbessert werden?", "actors": ids, **overrides},
                app,
            )
            db.session.commit()
            return sim.id

    return create


@pytest.fixture
def factory(app):
    with app.app_context():
        return sessionmaker(db.engine, expire_on_commit=False)


@pytest.fixture
def drain(app, factory):
    def run(limit=100):
        for _ in range(limit):
            job = claim(factory)
            if not job:
                return
            execute(app, factory, *job)
        raise AssertionError("Worker did not quiesce")

    return run
