import os
import time

import httpx

from app import db  # noqa: F401  (loads .env)

BASE = "https://api.keenable.ai/v1"


def _auth():
    key = (os.environ.get("KEENABLE_API_KEY") or "").strip()
    if key:
        return "", {"X-API-Key": key}
    return "/public", {"X-Keenable-Title": "Resale Radar"}


def _req(method: str, path: str, **kw):
    suffix, headers = _auth()
    url = f"{BASE}{path}{suffix}"
    for attempt in range(2):
        r = httpx.request(method, url, headers=headers, timeout=90, **kw)
        if r.status_code == 429 and attempt == 0:
            time.sleep(min(float(r.headers.get("Retry-After", "2") or 2), 10))
            continue
        r.raise_for_status()
        return r.json()


def search(query: str, site: str | None = None, max_results: int = 50, mode: str | None = None) -> list[dict]:
    body = {"query": query, "max_results": max_results}
    if site:
        body["site"] = site
    if mode:
        body["mode"] = mode
    return _req("POST", "/search", json=body).get("results", [])


def fetch(url: str, live: bool = True) -> dict:
    return _req("GET", "/fetch", params={"url": url, "live": str(live).lower()})
