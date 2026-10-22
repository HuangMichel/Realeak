import re
import unicodedata
from datetime import datetime, timezone

from app import keenable
from app.catalog import BRANDS
from app.db import q, x

COUNTRIES = {
    "fr": "www.vinted.fr", "de": "www.vinted.de", "nl": "www.vinted.nl",
    "it": "www.vinted.it", "es": "www.vinted.es", "be": "www.vinted.be",
}

IMG_RE = re.compile(r"!\[([^\]]*)\]\((https://images\d*\.vinted\.net/t/([^/\s)]+)/[^)\s]*)\)")
BRAND_LABELS = r"(?:Marque|Marke|Merk|Marca|Brand)"
COND_LABELS = r"(?:État|Etat|Zustand|Staat|Condizioni|Stato|Estado|Condición|Condition)"
TITLE_RE = re.compile(rf"^(.*?),\s*{BRAND_LABELS}\s*:", re.S)
COND_RE = re.compile(rf"{COND_LABELS}\s*:\s*(.+?),\s*(?=[€\d]|[^,:]+:)")
PRICE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*€|€\s*(\d+(?:[.,]\d+)?)")

_NORM = {
    "new_with_tags": ["neuf avec etiquette", "neu mit etikett", "nieuw met prijskaartje", "nuovo con cartellino",
                      "nuevo con etiquetas", "nuevo con etiqueta", "new with tags", "nieuw met kaartje",
                      "neu mit etiketten", "neu, mit etikett"],
    "new_without_tags": ["neuf sans etiquette", "neu ohne etikett", "nieuw zonder prijskaartje",
                         "nuovo senza cartellino", "nuevo sin etiquetas", "nuevo sin etiqueta",
                         "new without tags", "nieuw zonder kaartje", "neu ohne etiketten", "neu, ohne etikett"],
    "very_good": ["tres bon etat", "sehr gut", "heel goed", "ottime condizioni", "molto buono", "muy bueno",
                  "very good", "ottimo", "muy buen estado", "ottime"],
    "good": ["bon etat", "gut", "goed", "buone condizioni", "buono", "bueno", "good", "buen estado", "buone"],
    "satisfactory": ["satisfaisant", "zufriedenstellend", "redelijk", "soddisfacente", "satisfactorio",
                     "satisfactory", "acceptable", "befriedigend", "discrete", "soddisfacenti"],
}
_LOOKUP = {k: v for v, ks in _NORM.items() for k in ks}


def _strip(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn").strip()


def norm_condition(raw: str | None) -> str:
    if not raw:
        return "unknown"
    s = _strip(raw)
    if s in _LOOKUP:
        return _LOOKUP[s]
    for k in sorted(_LOOKUP, key=len, reverse=True):  # longest first: "tres bon etat" before "bon etat"
        if k in s:
            return _LOOKUP[k]
    return "unknown"


def _num(a: str, b: str) -> float:
    return float((a or b).replace(",", "."))


def parse_cards(markdown: str, country: str) -> list[dict]:
    out, seen = [], set()
    for m in IMG_RE.finditer(markdown or ""):
        alt, url, lid = m.group(1).strip(), m.group(2), m.group(3)
        if lid in seen or "Marque" not in alt and not re.search(BRAND_LABELS + r"\s*:", alt):
            continue
        seen.add(lid)
        t = TITLE_RE.match(alt)
        c = COND_RE.search(alt)
        prices = [_num(a, b) for a, b in PRICE_RE.findall(alt)]
        cond = c.group(1).strip() if c else None
        out.append({
            "id": lid, "country": country, "title": (t.group(1) if t else alt).strip(), "condition": cond,
            "condition_norm": norm_condition(cond),
            "price_eur": prices[0] if prices else None,
            "price_with_fees": prices[1] if len(prices) > 1 else (prices[0] if prices else None),
            "image_url": url,
        })
    return out


UPSERT = """INSERT INTO listings(id, brand, country, title, condition, condition_norm, price_eur, price_with_fees,
image_url, source_url, first_seen, last_seen, run_id)
VALUES(:id, :brand, :country, :title, :condition, :condition_norm, :price_eur, :price_with_fees,
:image_url, :source_url, :now, :now, :run_id)
ON CONFLICT (id) DO UPDATE SET last_seen=excluded.last_seen, run_id=excluded.run_id,
price_eur=excluded.price_eur, price_with_fees=excluded.price_with_fees, condition=excluded.condition,
condition_norm=excluded.condition_norm"""


def catalog_url(brand: str, country: str, page: int = 1) -> str:
    return (f"https://{COUNTRIES[country]}/catalog?brand_ids[]={BRANDS[brand]['vinted_brand_id']}"
            f"&order=newest_first&page={page}")


def collect_page(brand: str, country: str, page: int = 1, run_id: str | None = None) -> dict:
    url = catalog_url(brand, country, page)
    cards = parse_cards(keenable.fetch(url, live=True).get("content", ""), country)
    known = {r["id"] for r in q("SELECT id FROM listings WHERE brand=:b", b=brand)}
    now = datetime.now(timezone.utc).isoformat()
    for c in cards:
        x(UPSERT, brand=brand, source_url=url, now=now, run_id=run_id, **c)
    new = sum(1 for c in cards if c["id"] not in known)
    total = q("SELECT COUNT(*) AS n FROM listings WHERE brand=:b", b=brand)[0]["n"]
    return {"cards": len(cards), "new": new, "total": total}


def collect_all(brand: str, countries=("fr", "de"), max_pages: int = 2, run_id: str | None = None) -> dict:
    res = {}
    for c in countries:
        for p in range(1, max_pages + 1):
            r = collect_page(brand, c, p, run_id)
            res[f"{c}{p}"] = r
            if r["cards"] == 0:
                break
    return res
