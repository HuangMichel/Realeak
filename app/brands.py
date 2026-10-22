"""Live brand search: find a brand's official site on the web (Keenable), then decide whether it is an online shop."""
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import httpx

from app import keenable

TTL = 3600
_cache: dict = {}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")

# Marketplaces, social networks, press, directories: never the brand's own site.
BLOCKED = {
    "amazon", "zalando", "ebay", "vinted", "etsy", "aliexpress", "alibaba", "instagram", "facebook", "tiktok",
    "youtube", "linkedin", "pinterest", "twitter", "x", "wikipedia", "wikidata", "trustpilot", "societe",
    "pappers", "reddit", "quora", "medium", "github", "google", "bing", "yahoo", "poshmark", "thredup", "depop",
    "vestiairecollective", "leboncoin", "rakuten", "cdiscount", "kaufland", "otto", "asos", "farfetch",
    "lemonde", "lefigaro", "liberation", "bfmtv", "lesechos", "leparisien", "20minutes", "ouest-france",
    "lexpress", "forbes", "bloomberg", "reuters", "cnn", "bbc", "nytimes", "theguardian", "wwd", "vogue",
    "elle", "fashionnetwork", "businessoffashion", "glassdoor", "indeed", "crunchbase", "pagesjaunes",
    "kompass", "tripadvisor", "yelp", "manualslib", "fashionmodeldirectory", "fashion-collect", "lyst",
    "shopstyle", "wanboo", "mercari", "grailed", "vinteduk", "justia", "everything",
}
SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}


def norm(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", (s or "").lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s)


def registrable(host: str) -> str:
    host = (host or "").lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def label_of(domain: str) -> str:
    return domain.split(".")[0]


def is_blocked(domain: str) -> bool:
    lab = label_of(domain)
    return lab in BLOCKED or norm(lab) in BLOCKED


def clean_title(title: str, q: str) -> str:
    """Shortest title segment that contains the brand; falls back to the query as typed."""
    nq = norm(q)
    segs = [s.strip() for s in re.split(r"\s+[|\-–—·:]\s+|[|]", title or "") if s.strip()]
    ok = [s for s in segs if nq and nq in norm(s) and len(s.split()) <= 4]
    if ok:
        return min(ok, key=len)
    return q.strip().title() if q.strip().islower() else q.strip()


def rank_candidates(results: list[dict], q: str) -> list[dict]:
    """Pure: score the search hits by domain. Higher = more likely the brand's own site."""
    nq = norm(q)
    agg: dict = {}
    for rank, r in enumerate(results):
        try:
            u = urlparse(r.get("url") or "")
        except ValueError:
            continue
        if u.scheme not in ("http", "https") or not u.hostname:
            continue
        d = registrable(u.hostname)
        if is_blocked(d):
            continue
        a = agg.setdefault(d, {"domain": d, "score": 0.0, "hits": 0, "title": "", "depth": 99,
                               "url": f"https://{d}/"})
        a["hits"] += 1
        a["score"] += 1.0 + max(0, 10 - rank) * 0.1
        depth = len([p for p in u.path.split("/") if p])
        if depth < a["depth"] and r.get("title"):
            a["depth"], a["title"] = depth, r["title"]
        if re.search(r"officiel|official|boutique|shop|store", (r.get("title") or ""), re.I) and nq in norm(r.get("title")):
            a["score"] += 0.5
    for d, a in agg.items():
        lab = norm(label_of(d))
        if nq and lab == nq:
            a["score"] += 6
        elif nq and len(nq) >= 3 and lab.startswith(nq):
            a["score"] += 3
        elif nq and len(nq) >= 3 and nq in lab:
            a["score"] += 2
        a["label_match"] = bool(nq) and (lab == nq or (len(nq) >= 3 and nq in lab))
        a["exact"] = bool(nq) and lab == nq
    return sorted(agg.values(), key=lambda a: -a["score"])


def pick_candidates(cands: list[dict]) -> list[dict]:
    """Pure: keep the name-matching domains (one per brand label) when there are any, else the best-scored ones."""
    matching = [c for c in cands if c["label_match"]]
    pool = matching or cands
    seen, out = set(), []
    for c in pool:
        lab = label_of(c["domain"])
        if lab not in seen:
            seen.add(lab)
            out.append(c)
    return out


def _favicon(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=64"


def _cached(key):
    v = _cache.get(key)
    return v[1] if v and time.time() - v[0] < TTL else None


def search_brand(q: str) -> dict:
    q = (q or "").strip()
    key = ("s", norm(q))
    if (hit := _cached(key)) is not None:
        return hit
    def _s(query):
        try:
            return keenable.search(query, max_results=10)
        except Exception:  # noqa: BLE001
            return []
    queries = [f"{q} site officiel boutique en ligne", f"{q} official website", f"{q} fashion brand official online store"]
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = [r for part in ex.map(_s, queries) for r in part]
    cands = pick_candidates(rank_candidates(results, q))
    if not cands:
        out = {"query": q, "found": False}
    else:
        top = cands[:3]
        cl = [{"domain": c["domain"], "url": c["url"], "name": clean_title(c["title"], q) if c["label_match"] else
               label_of(c["domain"]).title(), "favicon": _favicon(c["domain"]), "label_match": c["label_match"], "exact": c["exact"],
               "score": round(c["score"], 2)} for c in top]
        best = cl[0]
        out = {"query": q, "found": True, "name": best["name"], "domain": best["domain"], "url": best["url"],
               "favicon": best["favicon"], "candidates": cl}
    _cache[key] = (time.time(), out)
    return out


# ---------------- e-commerce detection ----------------
PLATFORMS = [
    ("Shopify", ("cdn.shopify.com", "shopify.theme", "myshopify")),
    ("WooCommerce", ("woocommerce", "wc-ajax")),
    ("PrestaShop", ("prestashop",)),
    ("Magento", ("magento_", "mage.cookies", "mage-cache-storage", "magento-")),
    ("Salesforce Commerce", ("demandware",)),
    ("BigCommerce", ("bigcommerce",)),
    ("Wix Stores", ("wixstores", "wix-stores", "wixapps.net/api/stores")),
    ("Shopware", ("shopware",)),
]
CART_BTN = re.compile(r"add[- _]to[- _](?:cart|bag|basket)|ajouter au (?:panier|sac)|in den warenkorb|"
                      r"aggiungi al carrello|a[ñn]adir al (?:carrito|cesta)|toevoegen aan winkelwagen", re.I)
CART_LINK = re.compile(r"""href=["'][^"']*/(?:cart|panier|basket|warenkorb|checkout|carrello|carrito)\b""", re.I)
JSONLD = re.compile(r'"@type"\s*:\s*\[?\s*"(?:Product|Offer|ProductGroup)"', re.I)
PRICE = re.compile(r"\d+(?:[.,]\d{2})?\s*€|€\s*\d+(?:[.,]\d{2})?|\$\s*\d+(?:\.\d{2})?|£\s*\d+(?:\.\d{2})?")


def analyze_html(html: str) -> dict:
    """Pure: platform + generic shop signals from a page."""
    low = (html or "").lower()
    platform = next((name for name, marks in PLATFORMS if any(m in low for m in marks)), None)
    signals = []
    if JSONLD.search(html or ""):
        signals.append("product structured data")
    if CART_BTN.search(html or ""):
        signals.append("add-to-cart button")
    if CART_LINK.search(html or ""):
        signals.append("cart link")
    if len(PRICE.findall(html or "")) >= 3:
        signals.append("prices")
    return {"platform": platform, "signals": signals}


def _shopify_count(host: str, timeout: int = 12) -> tuple[int | None, bool]:
    n, capped = 0, False
    for page in (1, 2, 3):
        try:
            r = httpx.get(f"https://{host}/products.json", params={"limit": 250, "page": page}, timeout=timeout,
                          follow_redirects=True, headers={"User-Agent": UA})
            prods = r.json().get("products") if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            prods = None
        if prods is None:
            return (n or None), False
        n += len(prods)
        if len(prods) < 250:
            return n, False
        capped = page == 3
    return n, capped


def _reason(is_ecom: bool, platform, count, capped, signals) -> str:
    if not is_ecom:
        return "No shop found: this looks like a corporate/information site"
    if platform == "Shopify" and count:
        return f"Online shop on Shopify with {count:,}{'+' if capped else ''} products"
    if platform:
        return f"Online shop on {platform}"
    if "product pages indexed" in signals:
        return "Online shop (site blocks scanners, but product pages with prices are indexed)"
    return "Online shop (" + ", ".join(signals[:3]) + ")"


PRODUCT_PATH = re.compile(r"/(?:products?|produits?|p|collections?|e-shop|shop|boutique|catalog)/", re.I)


def search_signals(domain: str) -> list[str]:
    """Fallback when the site blocks bots: look at what the web index knows about its pages."""
    try:
        res = keenable.search("buy shop product price", site=domain, max_results=10)
    except Exception:  # noqa: BLE001
        return []
    paths = sum(1 for r in res if PRODUCT_PATH.search(urlparse(r.get("url") or "").path + "/"))
    prices = sum(1 for r in res if PRICE.search(f"{r.get('title') or ''} {r.get('snippet') or r.get('description') or ''}"))
    sig = []
    if paths >= 3:
        sig.append("product pages indexed")
    if prices >= 3:
        sig.append("prices in indexed pages")
    return sig


def detect_ecommerce(domain: str) -> dict:
    key = ("d", domain)
    if (hit := _cached(key)) is not None:
        return hit
    html, host = "", domain
    try:
        r = httpx.get(f"https://{domain}/", timeout=15, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept-Language": "fr,en;q=0.8"})
        if r.status_code < 400:
            html, host = r.text, r.url.host or domain
    except Exception:  # noqa: BLE001
        pass
    if not html:
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            html = ex.submit(lambda: keenable.fetch(f"https://{domain}/", live=True).get("content", "") or "").result(timeout=8)
        except Exception:  # noqa: BLE001
            html = ""
        ex.shutdown(wait=False)
    a = analyze_html(html)
    platform, signals, count, capped = a["platform"], a["signals"], None, False
    blocked = len(html) < 1500 and not signals and not platform   # empty or bot-challenge page
    if blocked:
        signals = search_signals(domain)
    if platform == "Shopify" or not html:
        n, capped = _shopify_count(host, 12 if html else 5)
        if n:
            platform, count = "Shopify", n
            signals = ["products.json catalog"] + signals
        elif platform == "Shopify" and not signals:
            platform = None   # theme markers only, no public catalog: not conclusive
    strong = {"add-to-cart button", "product structured data", "product pages indexed", "products.json catalog"} & set(signals)
    is_ecom = (bool(platform) and bool(signals)) or (len(signals) >= 2 and bool(strong)) or (blocked and bool(signals))
    out = {"is_ecommerce": is_ecom, "platform": platform, "product_count": count, "product_count_capped": capped,
           "signals": signals, "reason": _reason(is_ecom, platform, count, capped, signals)}
    _cache[key] = (time.time(), out)
    return out


def detect_many(domains: list[str]) -> list[dict]:
    with ThreadPoolExecutor(max_workers=max(1, len(domains))) as ex:
        return list(ex.map(detect_ecommerce, domains))
