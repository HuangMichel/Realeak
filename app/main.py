import json
import threading
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import catalog, collect
from app.db import ROOT, init_db, q, x
from app.db import q as dbq

app = FastAPI(title="Resale Radar")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "time": now()}


def _brand(brand: str):
    if brand not in catalog.BRANDS:
        raise HTTPException(404, f"marque inconnue: {brand}")


@app.post("/api/catalog/{brand}/load")
def load_catalog(brand: str):
    _brand(brand)
    return {"products": catalog.load_shopify_catalog(brand)}


@app.post("/api/collect/{brand}")
def collect_brand(brand: str, countries: str = "fr,de", pages: int = 2):
    _brand(brand)
    cs = [c for c in countries.split(",") if c in collect.COUNTRIES]
    return collect.collect_all(brand, cs, pages)


@app.get("/api/report/{brand}")
def report(brand: str):
    _brand(brand)
    n_list = q("SELECT COUNT(*) AS n FROM listings WHERE brand=:b", b=brand)[0]["n"]
    n_matched = q("""SELECT COUNT(*) AS n FROM matches m JOIN listings l ON l.id=m.listing_id
                     WHERE l.brand=:b AND m.product_id IS NOT NULL""", b=brand)[0]["n"]
    n_unknown = q("""SELECT COUNT(*) AS n FROM matches m JOIN listings l ON l.id=m.listing_id
                     WHERE l.brand=:b AND m.product_id IS NULL""", b=brand)[0]["n"]
    rows = q("""SELECT p.id AS product_id, p.title, p.image_url, p.price_eur, COUNT(l.id) AS n_listings,
        SUM(CASE WHEN l.condition_norm='new_with_tags' THEN 1 ELSE 0 END) AS n_new_with_tags,
        SUM(CASE WHEN l.condition_norm='new_without_tags' THEN 1 ELSE 0 END) AS n_new_without_tags,
        AVG(l.price_eur) AS avg_resale_price
        FROM matches m JOIN listings l ON l.id=m.listing_id JOIN products p ON p.id=m.product_id
        WHERE l.brand=:b GROUP BY p.id, p.title, p.image_url, p.price_eur
        ORDER BY COUNT(l.id) DESC""", b=brand)
    cc = defaultdict(lambda: defaultdict(int))
    for r in q("""SELECT m.product_id AS pid, l.country AS c, COUNT(*) AS n FROM matches m
                  JOIN listings l ON l.id=m.listing_id WHERE l.brand=:b AND m.product_id IS NOT NULL
                  GROUP BY m.product_id, l.country""", b=brand):
        cc[r["pid"]][r["c"]] = r["n"]
    tot_new = 0
    for r in rows:
        n = r["n_listings"] or 0
        nn = (r["n_new_with_tags"] or 0) + (r["n_new_without_tags"] or 0)
        tot_new += nn
        r["n_new_with_tags"] = int(r["n_new_with_tags"] or 0)
        r["n_new_without_tags"] = int(r["n_new_without_tags"] or 0)
        r["pct_new"] = round(100 * nn / n, 1) if n else 0
        avg = r["avg_resale_price"]
        r["avg_resale_price"] = round(avg, 2) if avg is not None else None
        pr = r["price_eur"]
        r["discount_pct"] = round(100 * (1 - avg / pr), 1) if avg is not None and pr else None
        r["countries"] = dict(cc[r["product_id"]])
    return {
        "totals": {"listings": n_list, "matched": n_matched, "unknown": n_unknown,
                   "unmatched": n_list - n_matched - n_unknown,
                   "pct_new": round(100 * tot_new / n_matched, 1) if n_matched else 0},
        "products": rows,
    }


@app.get("/api/listings")
def listings(brand: str = "cabaia", product_id: str | None = None, unmatched: int = 0, limit: int = 50):
    sql = """SELECT l.*, m.product_id, m.confidence, m.method, m.reasoning, p.title AS product_title
             FROM listings l LEFT JOIN matches m ON m.listing_id=l.id LEFT JOIN products p ON p.id=m.product_id
             WHERE l.brand=:b"""
    params = {"b": brand, "lim": limit}
    if product_id:
        sql += " AND m.product_id=:pid"
        params["pid"] = product_id
    if unmatched:
        sql += " AND (m.listing_id IS NULL OR m.product_id IS NULL)"
    return q(sql + " ORDER BY l.last_seen DESC LIMIT :lim", **params)


def _run(row: dict) -> dict:
    row["stats"] = json.loads(row["stats"]) if row.get("stats") else {}
    row["log"] = json.loads(row["log"]) if row.get("log") else []
    return row


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    r = q("SELECT * FROM runs WHERE id=:i", i=run_id)
    if not r:
        raise HTTPException(404, "run introuvable")
    return _run(r[0])


@app.get("/api/runs")
def list_runs(brand: str = "cabaia"):
    return [_run(r) for r in q("SELECT * FROM runs WHERE brand=:b ORDER BY started_at DESC LIMIT 10", b=brand)]


def _agent_thread(brand: str, run_id: str, run_agent):
    try:
        run_agent(brand, run_id)
        x("UPDATE runs SET status='done', finished_at=:t WHERE id=:i AND status='running'", t=now(), i=run_id)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        r = q("SELECT log FROM runs WHERE id=:i", i=run_id)
        log = json.loads(r[0]["log"]) if r and r[0]["log"] else []
        log.append({"t": now(), "phase": "error", "msg": f"{type(e).__name__}: {e}"})
        x("UPDATE runs SET status='error', finished_at=:t, log=:l WHERE id=:i",
          t=now(), l=json.dumps(log), i=run_id)


@app.post("/api/agent/{brand}/run")
def run_agent_endpoint(brand: str):
    _brand(brand)
    try:
        from app.agent import run_agent
    except ImportError:
        raise HTTPException(501, "app.agent pas encore disponible")
    run_id = uuid.uuid4().hex[:12]
    x("INSERT INTO runs(id, brand, kind, started_at, status, stats, log) VALUES(:i,:b,'agent',:t,'running','{}','[]')",
      i=run_id, b=brand, t=now())
    threading.Thread(target=_agent_thread, args=(brand, run_id, run_agent), daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/labels/next")
def label_next(brand: str = "cabaia"):
    r = q("""SELECT l.*, m.product_id AS match_product_id, m.confidence AS match_confidence,
             p.title AS match_product_title, p.image_url AS match_product_image
             FROM listings l LEFT JOIN labels b ON b.listing_id=l.id
             LEFT JOIN matches m ON m.listing_id=l.id LEFT JOIN products p ON p.id=m.product_id
             WHERE l.brand=:b AND b.listing_id IS NULL ORDER BY RANDOM() LIMIT 1""", b=brand)
    return r[0] if r else None


@app.post("/api/labels")
def label_post(body: dict = Body(...)):
    lid, pid = body.get("listing_id"), body.get("product_id") or "NONE"
    if not lid:
        raise HTTPException(400, "listing_id requis")
    x("""INSERT INTO labels(listing_id, product_id, note, created_at) VALUES(:l,:p,:n,:t)
         ON CONFLICT (listing_id) DO UPDATE SET product_id=excluded.product_id, note=excluded.note,
         created_at=excluded.created_at""", l=lid, p=pid, n=body.get("note"), t=now())
    return {"ok": True}


@app.get("/api/labels/count")
def label_count(brand: str = "cabaia"):
    n = q("""SELECT COUNT(*) AS n FROM labels b JOIN listings l ON l.id=b.listing_id WHERE l.brand=:b""", b=brand)
    return {"count": n[0]["n"]}


@app.get("/api/products")
def products(brand: str = "cabaia", q: str = ""):  # noqa: A002
    rows = dbq("SELECT id, title, product_type, price_eur, image_url FROM products WHERE brand=:b ORDER BY title",
               b=brand)
    s = q.strip().lower()
    if s:
        rows = [r for r in rows if s in (r["title"] or "").lower() or s in (r["product_type"] or "").lower()]
    return rows[:120]
