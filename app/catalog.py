import json
from datetime import datetime, timezone

import httpx

from app.db import x

BRANDS = {
    "cabaia": {"display": "Cabaïa", "shop_domain": "www.cabaia.fr", "vinted_brand_id": 282959},
}

UPSERT = """INSERT INTO products(id, brand, title, product_type, handle, price_eur, image_url, images, tags, updated_at)
VALUES(:id, :brand, :title, :product_type, :handle, :price_eur, :image_url, :images, :tags, :updated_at)
ON CONFLICT (id) DO UPDATE SET brand=excluded.brand, title=excluded.title, product_type=excluded.product_type,
handle=excluded.handle, price_eur=excluded.price_eur, image_url=excluded.image_url, images=excluded.images,
tags=excluded.tags, updated_at=excluded.updated_at"""


def load_shopify_catalog(brand: str) -> int:
    cfg = BRANDS[brand]
    n, page = 0, 1
    now = datetime.now(timezone.utc).isoformat()
    while True:
        r = httpx.get(f"https://{cfg['shop_domain']}/products.json", params={"limit": 250, "page": page},
                      timeout=60, follow_redirects=True)
        r.raise_for_status()
        prods = r.json().get("products", [])
        if not prods:
            break
        for p in prods:
            prices = []
            for v in p.get("variants", []):
                try:
                    prices.append(float(v["price"]))
                except (KeyError, TypeError, ValueError):
                    pass
            imgs = [i["src"] for i in p.get("images", []) if i.get("src")][:6]
            tags = p.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            x(UPSERT, id=str(p["id"]), brand=brand, title=p.get("title"), product_type=p.get("product_type"),
              handle=p.get("handle"), price_eur=min(prices) if prices else None,
              image_url=imgs[0] if imgs else None, images=json.dumps(imgs), tags=",".join(tags), updated_at=now)
            n += 1
        page += 1
    return n
