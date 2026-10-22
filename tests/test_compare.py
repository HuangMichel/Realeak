"""Comparaison "depuis" des cartes KPI : calcul a une date (first_seen <= as_of), delta, synchros."""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

D1, D2 = "2026-10-20T10:00:00", "2026-10-22T10:00:00"


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    from app import db
    importlib.reload(db)
    sys.modules.pop("app.main", None)
    from fastapi.testclient import TestClient
    from app import main
    db.init_db()
    # jour 1 : 20 annonces dont 10 neuves ; jour 2 : +10 neuves  -> 30 annonces, 20 neuves
    for i in range(30):
        day, cond = (D1, "new_with_tags" if i < 10 else "good") if i < 20 else (D2, "new_without_tags")
        db.x("INSERT INTO listings(id, brand, condition_norm, first_seen) VALUES(:i,'cabaia',:c,:f)",
             i=f"l{i}", c=cond, f=day + ".123456+00:00")
        db.x("INSERT INTO matches(listing_id, category) VALUES(:i,'Tote')", i=f"l{i}")
    for k, fin in enumerate([D1, D1, D2, "2026-10-21T09:00:00"]):  # doublon de date volontaire
        db.x("INSERT INTO runs(id, brand, status, started_at, finished_at) VALUES(:i,'cabaia','done',:f,:f)", i=f"r{k}", f=fin + "+00:00")
    db.x("INSERT INTO runs(id, brand, status, started_at, finished_at) VALUES('rx','cabaia','error','x','2026-10-23T00:00:00+00:00')")
    return TestClient(main.app), main


def test_report_as_of_and_compare(monkeypatch, tmp_path):
    c, _ = _client(monkeypatch, tmp_path)
    with c:
        assert c.get("/api/report/cabaia").json()["totals"]["listings"] == 30
        old = c.get("/api/report/cabaia", params={"as_of": "2026-10-21T00:00:00Z"}).json()
        assert old["totals"]["listings"] == 20 and old["totals"]["pct_new"] == 0
        assert old["categories"][0]["n_listings"] == 20 and old["categories"][0]["pct_new"] == 50.0
        assert c.get("/api/report/cabaia", params={"as_of": "2026-10-19"}).json()["totals"]["listings"] == 0
        r = c.get("/api/report/cabaia/compare", params={"since": "2026-10-21T00:00:00+00:00"}).json()
        assert r["now"] == {"listings": 30, "resold_new": 20, "pct_new": 66.7, "most_unwanted": {"category": "Tote", "pct_new": 66.7}}
        assert r["then"] == {"listings": 20, "resold_new": 10, "pct_new": 50.0, "most_unwanted": {"category": "Tote", "pct_new": 50.0}}
        assert r["delta"] == {"listings": 10, "resold_new": 10, "pct_new_pts": 16.7}
        empty = c.get("/api/report/cabaia/compare", params={"since": "2026-10-19T00:00:00Z"}).json()
        assert empty["then"]["listings"] == 0 and empty["then"]["pct_new"] is None and empty["delta"]["pct_new_pts"] is None
        assert c.get("/api/report/cabaia/compare", params={"since": "pas une date"}).status_code == 400
        assert c.get("/api/report/inconnue/compare", params={"since": D1}).status_code == 404


def test_syncs_distinct_done_newest_first(monkeypatch, tmp_path):
    c, main = _client(monkeypatch, tmp_path)
    with c:
        r = c.get("/api/syncs/cabaia").json()
        assert r["syncs"] == [D2 + "+00:00", "2026-10-21T09:00:00+00:00", D1 + "+00:00"]  # sans doublon, sans le run en erreur
        assert r["earliest"] == D1 + ".123456+00:00"
    assert main._asof("2026-10-22T14:36:00 00:00") == "2026-10-22T14:36:00+00:00"  # "+" non encode dans l'URL
    assert main._asof("2026-10-22T14:36:00") == "2026-10-22T14:36:00+00:00"  # naif = UTC
    assert main._asof(None) is None
