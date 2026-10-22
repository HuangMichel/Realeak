"""Copie les 6 tables de data.db (SQLite local) vers Postgres (variable d'environnement PG_URL).

    python scripts/sqlite_to_pg.py --dry-run     # compte les lignes locales, n'ecrit rien
    PG_URL=postgresql://user:pass@host/db python scripts/sqlite_to_pg.py

Les tables sont creees sur la cible via app.db.init_db (meme normalisation d'URL que l'appli), puis les lignes sont
inserees par lots avec upsert (relancable sans doublons).
"""
import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TABLES = {"products": "id", "listings": "id", "matches": "listing_id", "memory": "id", "runs": "id", "labels": "listing_id"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(ROOT / "data.db"), help="base SQLite source")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true", help="affiche le nombre de lignes par table, n'ecrit rien")
    a = ap.parse_args()

    src = create_engine(f"sqlite:///{Path(a.src).as_posix()}")
    with src.connect() as c:
        counts = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in TABLES}
    print("Lignes locales :", ", ".join(f"{t}={n}" for t, n in counts.items()))
    pg = (os.environ.get("PG_URL") or "").strip()
    if a.dry_run:
        print("Dry-run : rien n'est ecrit." + ("" if pg else " (PG_URL absent : cible non verifiee)"))
        return
    if not pg:
        sys.exit("PG_URL est vide : rien a faire (utilise --dry-run pour compter les lignes locales).")

    os.environ["DATABASE_URL"] = pg          # app.db normalise l'URL (postgresql+psycopg, sans uselibpqcompat)
    from app import db
    if not db.URL.startswith("postgresql"):
        sys.exit(f"La cible n'est pas Postgres : {db.URL.split(':')[0]}")
    db.init_db()
    insp = inspect(db.engine)
    for table, pk in TABLES.items():
        dst_cols = {c["name"] for c in insp.get_columns(table)}
        with src.connect() as c:
            res = c.execute(text(f"SELECT * FROM {table}"))
            cols = [k for k in res.keys() if k in dst_cols]
            sets = ", ".join(f"{k}=excluded.{k}" for k in cols if k != pk)
            sql = text(f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join(':' + k for k in cols)}) "
                       f"ON CONFLICT ({pk}) DO UPDATE SET {sets}")
            done = 0
            while True:
                rows = res.fetchmany(a.batch)
                if not rows:
                    break
                with db.engine.begin() as w:
                    w.execute(sql, [{k: r._mapping[k] for k in cols} for r in rows])
                done += len(rows)
        n_dst = db.q(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        print(f"{table}: {done} lignes copiees (cible : {n_dst})")
        if done != counts[table]:
            print(f"  ATTENTION : {counts[table]} lignes en local, {done} copiees")


if __name__ == "__main__":
    main()
