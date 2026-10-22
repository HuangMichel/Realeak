"""Worker : execute sur CE PC (CLI Codex, abonnement ChatGPT) les runs mis en file par l'appli hebergee (AGENT_MODE=queue).

python -m app.worker [--once]    (DATABASE_URL = la base partagee ; AGENT_MAX_LISTINGS optionnel pour borner un run)
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone

from app.db import init_db, q, x

POLL_SECONDS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_log(run_id: str, phase: str, msg: str):
    r = q("SELECT log FROM runs WHERE id=:i", i=run_id)
    try:
        log = json.loads(r[0]["log"]) if r and r[0]["log"] else []
    except Exception:  # noqa: BLE001
        log = []
    log.append({"t": _now(), "phase": phase, "msg": msg})
    x("UPDATE runs SET log=:l WHERE id=:i", l=json.dumps(log, ensure_ascii=False), i=run_id)


def process_next() -> str | None:
    """Prend le plus ancien run 'queued', le passe 'running', l'execute. Retourne son id (None si file vide)."""
    from app.agent import run_agent
    rows = q("SELECT id, brand FROM runs WHERE status='queued' ORDER BY started_at ASC, id ASC LIMIT 1")
    if not rows:
        return None
    run_id, brand = rows[0]["id"], rows[0]["brand"]
    x("UPDATE runs SET status='running' WHERE id=:i AND status='queued'", i=run_id)
    _append_log(run_id, "queue", "Pris en charge par le worker Codex")
    t0 = time.time()
    try:
        ml = int(os.environ.get("AGENT_MAX_LISTINGS") or 0) or None
        run_agent(brand, run_id, max_listings=ml)
        status = (q("SELECT status FROM runs WHERE id=:i", i=run_id) or [{}])[0].get("status")
    except Exception as e:  # noqa: BLE001
        _append_log(run_id, "error", f"{type(e).__name__}: {str(e)[:300]}")
        x("UPDATE runs SET status='error', finished_at=:t WHERE id=:i", t=_now(), i=run_id)
        status = "error"
    print(f"[worker] run {run_id} ({brand}) -> {status} en {time.time() - t0:.0f} s", flush=True)
    return run_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="traite au plus un run puis s'arrete (tests)")
    a = ap.parse_args()
    init_db()
    print(f"[worker] en attente de runs 'queued' (toutes les {POLL_SECONDS} s)", flush=True)
    while True:
        try:
            done = process_next()
        except Exception as e:  # noqa: BLE001  (base injoignable... : on reessaie)
            print(f"[worker] erreur : {type(e).__name__}: {str(e)[:200]}", flush=True)
            done = None
        if a.once:
            break
        if done is None:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
