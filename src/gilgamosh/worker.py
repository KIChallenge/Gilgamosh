import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import sessionmaker

from .models import Job, Simulation, now, uid
from .runtime import Runtime, Stopped
from .security import PublicError
from .services import event, settings

log = logging.getLogger(__name__)
LEASE_SECONDS = 90


def claim(factory):
    with factory.begin() as s:
        # All state transitions lock simulation before its jobs.
        candidates = s.scalars(
            select(Simulation)
            .where(Simulation.status != "ended")
            .order_by(Simulation.created_at)
            .with_for_update(skip_locked=True)
        ).all()
        for sim in candidates:
            jobs = s.scalars(
                select(Job)
                .where(
                    Job.simulation_id == sim.id,
                    or_(
                        and_(Job.status == "queued", Job.available_at <= now()),
                        and_(Job.status == "running", Job.lease_until < now()),
                    ),
                )
                .order_by(Job.status.desc(), Job.updated_at)
                .with_for_update(skip_locked=True)
            ).all()
            for job in jobs:
                if job.kind == "phase" and sim.status != "running":
                    continue
                if job.payload.get("run", sim.run) != sim.run or job.payload.get("block", sim.block) != sim.block:
                    job.status = "superseded"
                    continue
                active = s.scalar(
                    select(Job.id)
                    .where(
                        Job.simulation_id == sim.id,
                        Job.id != job.id,
                        Job.status == "running",
                        Job.lease_until >= now(),
                        Job.kind.in_(["watcher", "report"]) if job.kind != "phase" else Job.kind == "phase",
                    )
                    .limit(1)
                )
                if active:
                    continue
                job.status, job.token = "running", uid()
                job.lease_until, job.updated_at = now() + timedelta(seconds=LEASE_SECONDS), now()
                return job.id, job.token
    return None


def heartbeat(factory, job_id, token, stop):
    while not stop.wait(10):
        try:
            with factory.begin() as s:
                job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
                if not job or job.token != token or job.status != "running":
                    return
                job.lease_until, job.updated_at = now() + timedelta(seconds=LEASE_SECONDS), now()
        except Exception:
            log.exception("Heartbeat konnte nicht gespeichert werden.")


def execute(app, factory, job_id, token):
    stop = threading.Event()
    pulse = threading.Thread(target=heartbeat, args=(factory, job_id, token, stop), daemon=True)
    pulse.start()
    runtime = None
    try:
        runtime = Runtime(app, factory, job_id, token)
        if runtime.kind == "phase":
            runtime.execute_phase()
        else:
            runtime.execute_observation()
    except Stopped:
        with factory.begin() as s:
            job = s.get(Job, job_id)
            if job and job.token == token:
                job.status, job.token = "queued", ""
    except Exception as exc:
        log.exception("Arbeitsschritt fehlgeschlagen: %s", job_id)
        message = (
            str(exc)
            if isinstance(exc, PublicError)
            else "Interner Fehler. Serverprotokoll prüfen und erneut fortsetzen."
        )
        if runtime:
            with factory.begin() as s:
                sim, job = runtime.locked(s, allow_paused=True)
                job.status, job.token = "failed", ""
                if job.kind == "phase":
                    sim.status, sim.error = "paused", message
                    event(s, sim, "error", {"text": message})
                else:
                    event(s, sim, "watcher_error", {"text": message, "kind": job.kind})
    finally:
        stop.set()
        pulse.join(timeout=2)


def run_worker(app, once=False, stop=None):
    from .models import db

    with app.app_context():
        factory = sessionmaker(db.engine, expire_on_commit=False)
        limit = settings(db.session)["parallelism"]
        db.session.remove()
    app.extensions["llm_slots"] = threading.BoundedSemaphore(limit)
    stop = stop or threading.Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = set()
        while not stop.is_set():
            for future in list(pending):
                if future.done():
                    try:
                        future.result()
                    except Exception:
                        log.exception("Worker-Auftrag unerwartet abgebrochen.")
                    pending.remove(future)
            if len(pending) < 2:
                job = claim(factory)
                if job:
                    pending.add(executor.submit(execute, app, factory, *job))
                    if once:
                        for f in pending:
                            f.result()
                        return
                    continue
            if once:
                return
            stop.wait(0.5)
