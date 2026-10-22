import json
import os
import threading
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import brands, catalog, collect
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


@app.get("/slide")
def slide():
    return FileResponse(ROOT / "static" / "slide.html")


@app.get("/api/health")
def health():
    return {"ok": True, "time": now()}


def _known_slug(domain: str, name: str) -> str | None:
    lab = brands.norm(brands.label_of(domain))
    for slug, cfg in catalog.BRANDS.items():
        if lab == brands.norm(brands.label_of(brands.registrable(cfg["shop_domain"]))) or brands.norm(name) == brands.norm(cfg["display"]):
            return slug
    return None


@app.get("/api/brands/search")
def brands_search(q: str = ""):  # noqa: A002
    q = q.strip()
    if len(q) < 2:
        return {"query": q, "found": False}
    base = brands.search_brand(q)
    if not base.get("found"):
        return base
    cands = base["candidates"]
    verdicts = brands.detect_many([c["domain"] for c in cands])
    results = [{**c, **v, "known_slug": _known_slug(c["domain"], c["name"])} for c, v in zip(cands, verdicts)]
    for r in results:
        if r["known_slug"]:
            r["name"] = catalog.BRANDS[r["known_slug"]]["display"]
    # a name-matching candidate that IS a shop beats a name-matching one that is not
    if not results[0]["is_ecommerce"]:
        shop = next((r for r in results[1:] if r["is_ecommerce"] and r["exact"]), None)
        if shop:
            results.remove(shop)
            results.insert(0, shop)
    return {"query": q, "found": True, **results[0], "results": results}


def _brand(brand: str):
    if brand not in catalog.BRANDS:
        raise HTTPException(404, f"unknown brand: {brand}")


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
    cats, categorized = _categories_report(brand)
    return {
        "totals": {"listings": n_list, "matched": n_matched, "unknown": n_unknown,
                   "unmatched": n_list - n_matched - n_unknown, "categorized": categorized,
                   "pct_new": round(100 * tot_new / n_matched, 1) if n_matched else 0},
        "categories": cats,
        "products": rows,
    }


def _categories_report(brand: str):
    """Niveau CATEGORIE (product_type du catalogue) : robuste meme quand la reference exacte est incertaine."""
    retail = {r["t"]: r["avg_price"] for r in q(
        "SELECT product_type AS t, AVG(price_eur) AS avg_price FROM products WHERE brand=:b GROUP BY product_type", b=brand)}
    g = defaultdict(list)
    for r in q("""SELECT COALESCE(m.category, p.product_type) AS cat, l.condition_norm AS cn, l.price_eur AS price
                  FROM matches m JOIN listings l ON l.id=m.listing_id LEFT JOIN products p ON p.id=m.product_id
                  WHERE l.brand=:b""", b=brand):
        if r["cat"]:
            g[r["cat"]].append(r)
    out, total = [], 0
    for cat, rs in g.items():
        n = len(rs)
        total += n
        tags = sum(1 for r in rs if r["cn"] == "new_with_tags")
        notags = sum(1 for r in rs if r["cn"] == "new_without_tags")
        prices = [r["price"] for r in rs if r["price"] is not None]
        avg = sum(prices) / len(prices) if prices else None
        rp = retail.get(cat)
        out.append({"category": cat, "n_listings": n, "n_new_with_tags": tags, "n_new_without_tags": notags,
                    "pct_new": round(100 * (tags + notags) / n, 1) if n else 0,
                    "avg_resale_price": round(avg, 2) if avg is not None else None,
                    "avg_retail_price": round(rp, 2) if rp is not None else None,
                    "discount_pct": round(100 * (1 - avg / rp), 1) if avg is not None and rp else None})
    out.sort(key=lambda d: -d["n_listings"])
    return out, total


@app.get("/api/categories")
def categories(brand: str = "cabaia"):
    """Types du catalogue, tries par nombre d'annonces classees puis nombre de produits (pour les boutons d'etiquetage)."""
    cat = {r["t"]: r["n"] for r in q(
        "SELECT product_type AS t, COUNT(*) AS n FROM products WHERE brand=:b AND product_type IS NOT NULL AND product_type<>'' "
        "GROUP BY product_type", b=brand)}
    lst = {r["t"]: r["n"] for r in q(
        "SELECT COALESCE(m.category, p.product_type) AS t, COUNT(*) AS n FROM matches m JOIN listings l ON l.id=m.listing_id "
        "LEFT JOIN products p ON p.id=m.product_id WHERE l.brand=:b GROUP BY COALESCE(m.category, p.product_type)", b=brand)}
    names = sorted(cat, key=lambda t: (-(lst.get(t) or 0), -cat[t], t))
    return [{"category": t, "n_products": cat[t], "n_listings": lst.get(t, 0)} for t in names]


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
        raise HTTPException(404, "run not found")
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
    if os.environ.get("AGENT_MODE") == "queue":   # appli hebergee : le worker Codex du PC (python -m app.worker) execute le run
        run_id = uuid.uuid4().hex[:12]
        log = json.dumps([{"t": now(), "phase": "queue", "msg": "Waiting for the Codex worker"}], ensure_ascii=False)
        x("INSERT INTO runs(id, brand, kind, started_at, status, stats, log) VALUES(:i,:b,'agent',:t,'queued','{}',:l)",
          i=run_id, b=brand, t=now(), l=log)
        return {"run_id": run_id}
    try:
        from app.agent import run_agent
    except ImportError:
        raise HTTPException(501, "app.agent not available yet")
    run_id = uuid.uuid4().hex[:12]
    x("INSERT INTO runs(id, brand, kind, started_at, status, stats, log) VALUES(:i,:b,'agent',:t,'running','{}','[]')",
      i=run_id, b=brand, t=now())
    threading.Thread(target=_agent_thread, args=(brand, run_id, run_agent), daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/labels/next")
def label_next(brand: str = "cabaia"):
    r = q("""SELECT l.*, m.product_id AS match_product_id, m.confidence AS match_confidence,
             p.title AS match_product_title, p.image_url AS match_product_image,
             COALESCE(m.category, p.product_type) AS match_category
             FROM listings l LEFT JOIN labels b ON b.listing_id=l.id
             LEFT JOIN matches m ON m.listing_id=l.id LEFT JOIN products p ON p.id=m.product_id
             WHERE l.brand=:b AND b.listing_id IS NULL ORDER BY (m.listing_id IS NULL), RANDOM() LIMIT 1""", b=brand)
    return r[0] if r else None


@app.post("/api/labels")
def label_post(body: dict = Body(...)):
    """product_id : id produit | 'NONE' (hors catalogue) | null (categorie seule, reference inconnue)."""
    lid = body.get("listing_id")
    if not lid:
        raise HTTPException(400, "listing_id required")
    pid = body.get("product_id") or None
    cat = (body.get("category") or "").strip() or None
    if pid and pid != "NONE":
        prod = q("SELECT product_type FROM products WHERE id=:i", i=pid)
        if not prod:
            raise HTTPException(400, f"unknown product: {pid}")
        cat = prod[0]["product_type"] or cat      # le produit choisi fait foi pour la categorie
    elif not pid and not cat:
        raise HTTPException(400, "product_id or category required")
    x("""INSERT INTO labels(listing_id, product_id, note, created_at, category) VALUES(:l,:p,:n,:t,:c)
         ON CONFLICT (listing_id) DO UPDATE SET product_id=excluded.product_id, note=excluded.note,
         created_at=excluded.created_at, category=excluded.category""",
      l=lid, p=pid, n=body.get("note"), t=now(), c=cat)
    return {"ok": True, "category": cat}


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
