from sqlalchemy import select

from gilgamosh.models import Actor, db
from gilgamosh.security import INSTITUTION_RULE
from gilgamosh.services import import_actors
from gilgamosh.stakeholders import read_profiles, seed_stakeholders


def test_all_stakeholder_prompts_are_loaded_without_background(app):
    with app.app_context():
        rows = list(db.session.scalars(select(Actor)))
        assert len(rows) == 25
        assert len([a for a in rows if a.parent_id == "bundeslaender"]) == 9
        assert db.session.get(Actor, "bundeslaender").profile["selectable"] is False
        public_health = db.session.get(Actor, "public_health")
        assert public_health.name == "Public-Health-Fachvertretung"
        for actor in rows:
            if not actor.profile["selectable"]:
                continue
            assert actor.profile["source_hash"] and actor.profile["source_file"].endswith(".md")
            assert actor.profile["rote_linien"] and actor.profile["verhandelbar"]
            assert actor.profile["demo"] is False
            assert not actor.profile["db_infos"]
            assert "_statuten_aller_agents" not in actor.profile["system_prompt"]
            assert "Landeshauptmann" not in actor.profile["system_prompt"]
        sv = db.session.get(Actor, "sozialversicherung")
        assert "ASVG § 31" in sv.profile["system_prompt"]
        assert len(sv.profile["rote_linien"]) == 4
        assert len(sv.profile["verhandelbar"]) == 8


def test_seeding_is_idempotent_and_preserves_admin_changes(app):
    with app.app_context():
        before = db.session.get(Actor, "sozialversicherung").profile.copy()
        db.session.get(Actor, "sozialversicherung").model = "custom/model"
        seed_stakeholders(db.session)
        db.session.commit()
        assert db.session.get(Actor, "sozialversicherung").model == "custom/model"
        assert db.session.get(Actor, "sozialversicherung").profile == before
        actor = db.session.get(Actor, "sozialversicherung")
        import_actors(
            db.session,
            [
                {
                    "id": actor.id,
                    "name": actor.name,
                    **actor.profile,
                    "system_prompt": actor.profile["system_prompt"] + "\nMANUAL_EDIT",
                }
            ],
        )
        db.session.commit()
        _imported, preserved = seed_stakeholders(db.session)
        db.session.commit()
        assert actor.id in preserved
        assert "MANUAL_EDIT" in actor.profile["system_prompt"]


def test_old_demo_profiles_upgrade_with_new_statutes(app):
    with app.app_context():
        actor = db.session.get(Actor, "sozialversicherung")
        actor.profile = {
            "system_prompt": INSTITUTION_RULE,
            "demo": True,
            "rote_linien": ["Versorgungsqualität darf nicht verschlechtert werden."],
            "verhandelbar": [
                {"position": "Regionale Pilotprojekte statt flächendeckender Einführung.", "prioritaet": 1}
            ],
            "db_infos": [
                {
                    "text": "Synthetisches Demoprofil; keine verifizierten institutionellen Positionen.",
                    "source": "Demo",
                    "date": "2026-11-05",
                }
            ],
        }
        db.session.commit()
        imported, _ = seed_stakeholders(db.session)
        db.session.commit()
        assert actor.id in imported
        assert len(actor.profile["rote_linien"]) == 4 and actor.profile["demo"] is False


def test_profile_sources_do_not_contain_other_institutions_statutes():
    profiles = {p["id"]: p for p in read_profiles()}
    sv = profiles["sozialversicherung"]
    foreign = profiles["finanzministerium"]["rote_linien"][0]
    assert foreign not in sv["system_prompt"]
    assert foreign not in sv["rote_linien"]
