"""Mode file d'attente : l'appli hebergee met le run en 'queued', le worker (PC, Codex) le prend."""
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_queue_mode_route_and_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    monkeypatch.setenv("AGENT_MODE", "queue")
    from app import db
    importlib.reload(db)
    for m in ("app.main", "app.worker"):
        sys.modules.pop(m, None)
    from fastapi.testclient import TestClient
    from app import main, worker
    db.init_db()
    with TestClient(main.app) as c:
        rid = c.post("/api/agent/cabaia/run").json()["run_id"]
        run = c.get(f"/api/runs/{rid}").json()
    assert run["status"] == "queued" and run["log"][0]["msg"] == "Waiting for the Codex worker"

    called = []
    import app.agent as agent
    monkeypatch.setattr(agent, "run_agent", lambda brand, run_id, **kw: called.append((brand, run_id)) or db.x(
        "UPDATE runs SET status='done' WHERE id=:i", i=run_id))
    assert worker.process_next() == rid and called == [("cabaia", rid)]
    r = db.q("SELECT status, log FROM runs WHERE id=:i", i=rid)[0]
    assert r["status"] == "done" and "worker Codex" in r["log"]
    assert worker.process_next() is None
