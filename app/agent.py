"""Resale Radar - coeur agent (OpenAI).

Phases : COLLECT (boucle outils, modele SMART) -> IDENTIFY (cascade : reperage + pages de candidats en vision
modele FAST, arbitre modele SMART) -> LEARN (memoire alias/exemplars/requetes) -> INSIGHTS (modele SMART).
Modeles et prix lus dans .env : OPENAI_MODEL_FAST / OPENAI_MODEL_SMART, OPENAI_PRICE_{FAST,SMART}_{IN,OUT} ($/M tokens).
"""
import base64
import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import openai
from dotenv import dotenv_values

from app import db, llm_codex
from app.db import q, x

FAST, SMART = "fast", "smart"          # roles ; les noms de modeles viennent de l'environnement
FATAL = (openai.AuthenticationError, openai.PermissionDeniedError)

_DOT: dict = {}
_DOT_T = 0.0


def _env(name: str, default: str = "") -> str:
    """os.environ d'abord, puis .env relu (cache 5 s) : la cle peut etre collee pendant que le serveur tourne."""
    global _DOT, _DOT_T
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    if time.time() - _DOT_T > 5:
        try:
            _DOT = dotenv_values(db.ROOT / ".env", encoding="utf-8-sig")
        except Exception:
            _DOT = {}
        _DOT_T = time.time()
    return (_DOT.get(name) or default or "").strip()


def model_name(role: str) -> str:
    primary, other = ("OPENAI_MODEL_FAST", "OPENAI_MODEL_SMART") if role == FAST else ("OPENAI_MODEL_SMART", "OPENAI_MODEL_FAST")
    m = _env(primary) or _env(other)
    if not m:
        raise RuntimeError(f"{primary} n'est pas defini dans .env")
    return m


def _price(role: str) -> tuple[float, float]:
    r = role.upper()
    try:
        return float(_env(f"OPENAI_PRICE_{r}_IN", "0")), float(_env(f"OPENAI_PRICE_{r}_OUT", "0"))
    except ValueError:
        return 0.0, 0.0


def use_codex() -> bool:
    """LLM_BACKEND=codex (defaut) : CLI Codex = abonnement ChatGPT, zero API facturee ; LLM_BACKEND=openai : SDK OpenAI."""
    return (_env("LLM_BACKEND") or "codex").lower() != "openai"


def codex_model(role: str) -> str:
    return (_env("OPENAI_MODEL_FAST" if role == FAST else "OPENAI_MODEL_SMART")
            or ("gpt-6-luna" if role == FAST else "gpt-6.1-sol"))


SHORTLIST_K = int(_env("SHORTLIST_K", "8") or 8)   # candidats par page (par defaut 8 : top 8 par score texte)
MAX_PAGES = 3                                        # pages de candidats parcourues tant qu'aucun ne correspond
ESCALATE_BELOW = float(_env("ESCALATE_BELOW", "0.75") or 0.75)
UNKNOWN_BELOW = 0.5
LEARN_MIN_CONF = 0.85


# --------------------------------------------------------------------------- pure helpers
def estimate_cost(role: str, input_tokens: int, output_tokens: int) -> float:
    pi, po = _price(role)
    return (input_tokens * pi + output_tokens * po) / 1_000_000


def norm(s) -> str:
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


STOP = set("""de du la le les des un une et en avec pour sur au aux the and for with in of a an di da il el los las
von der die das und mit een het voor met taille size neuf neuve nieuw neu nuevo nuovo etat bon bonne tres parfait
comme very good new tags tag etiquette etiquettes etiquetas cartellino prijskaartje etikett cm ml pouces pouce inch
ou ohne sans sin senza zonder excellent tbe vendu vends vend lot occasion""".split())

COLORS = set("""noir noire black blanc blanche white rose pink bleu bleue blue vert verte green rouge red jaune yellow
orange gris grise grey gray beige marron brun brown fuchsia dore doree gold argent violet purple multicolore degrade
fleuri fleurs motif imprime kaki bordeaux marine navy creme turquoise corail camel taupe schwarz weiss blau grun rot
gelb grau zwart wit blauw groen rood geel grijs nero bianco blu verde rosso giallo grigio negro blanco azul rojo
amarillo""".split())

_SYN_RAW = {
    "rucksack": "sac dos", "rugzak": "sac dos", "zaino": "sac dos", "mochila": "sac dos", "backpack": "sac dos",
    "portemonnee": "portefeuille wallet", "geldborse": "portefeuille wallet", "geldbeutel": "portefeuille wallet",
    "portafoglio": "portefeuille wallet", "cartera": "portefeuille wallet", "monedero": "portefeuille wallet",
    "billetera": "portefeuille wallet", "monnaie": "portefeuille wallet", "portemonnaie": "portefeuille wallet",
    "tote": "cabas tote", "shopper": "cabas tote", "cabas": "tote", "totebag": "cabas tote",
    "kulturbeutel": "trousse toilette", "toilettas": "trousse toilette", "neceser": "trousse toilette",
    "laptop": "housse ordinateur", "notebook": "housse ordinateur", "computer": "housse ordinateur",
    "macbook": "housse ordinateur", "laptoptas": "housse ordinateur", "ordinateur": "housse",
    "bauchtasche": "banane", "heuptas": "banane", "marsupio": "banane", "rinonera": "banane",
    "umhangetasche": "crossbody", "bandolera": "crossbody", "tracolla": "crossbody", "schoudertas": "crossbody",
    "socken": "chaussette", "sokken": "chaussette", "calcetines": "chaussette", "calze": "chaussette",
    "clutch": "pochette", "etui": "pochette",
    "gourde": "bottle", "flasche": "gourde bottle", "fles": "gourde bottle", "borraccia": "gourde bottle",
    "botella": "gourde bottle",
    "schlusselanhanger": "keychain porte cles", "sleutelhanger": "keychain porte cles",
    "portachiavi": "keychain porte cles", "llavero": "keychain porte cles",
    "miroir": "mirror", "spiegel": "mirror", "specchio": "mirror", "espejo": "mirror",
}


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


SYN = {_stem(k): [_stem(v) for v in vs.split()] for k, vs in _SYN_RAW.items()}


def tokens(text, brand_words=(), drop_colors=False, expand=False) -> list[str]:
    out = []
    for t in norm(text).split():
        t = _stem(t)
        if len(t) < 2 or t in STOP or t in brand_words or (drop_colors and t in COLORS):
            continue
        if t.isdigit() and len(t) < 3:
            continue
        out.append(t)
    if expand:
        for t in list(out):
            out.extend(SYN.get(t, []))
    return out


def model_key(title, product_type=None) -> str:
    """Nom de modele : segment avant le premier ' - ' ; a defaut le type de produit Shopify."""
    parts = re.split(r"\s+[-–—]\s+", (title or "").strip())
    if len(parts) > 1 and parts[0].strip():
        return norm(parts[0])
    return norm(product_type) or norm(title)


def brand_words_for(brand: str) -> set:
    bw = {norm(brand)}
    try:
        from app.catalog import BRANDS
        bw |= set(norm(BRANDS[brand]["display"]).split())
    except Exception:
        pass
    return {_stem(w) for w in bw if w}


def _product_images(p) -> list[str]:
    try:
        imgs = json.loads(p.get("images") or "[]")
    except Exception:
        imgs = []
    if not imgs and p.get("image_url"):
        imgs = [p["image_url"]]
    return imgs


class Index:
    """Index de produits pour la shortlist (tokens + IDF)."""

    def __init__(self, products, brand_words=()):
        self.products = list(products)
        self.bw = set(brand_words)
        self.tok, self.mk = {}, {}
        df = Counter()
        for p in self.products:
            t = set(tokens(p.get("title"), self.bw)) | set(tokens(p.get("product_type"), self.bw))
            self.tok[p["id"]] = (set(tokens(p.get("title"), self.bw)), t)
            self.mk[p["id"]] = model_key(p.get("title"), p.get("product_type"))
            df.update(t)
        n = max(len(self.products), 1)
        self.idf = defaultdict(lambda: math.log(1 + n), {t: math.log(1 + n / (1 + d)) for t, d in df.items()})
        self.by_id = {p["id"]: p for p in self.products}
        self.type_of = {p["id"]: norm(p.get("product_type")) for p in self.products}
        tc = Counter(p.get("product_type") for p in self.products if p.get("product_type"))
        self.type_names = {norm(t): t for t in tc}
        self.type_prompt = "; ".join(f"{t} ({n})" for t, n in tc.most_common())


class Memory:
    """aliases: mot -> [(kind, value, n, rowid)] ; exemplars: product_id -> [{listing_id,image_url,title,rowid}]"""

    def __init__(self):
        self.aliases = defaultdict(list)
        self.exemplars = defaultdict(list)

    @staticmethod
    def load(brand: str) -> "Memory":
        m = Memory()
        for r in q("SELECT id, kind, key, value, hits FROM memory WHERE brand=:b AND kind IN ('alias','exemplar')", b=brand):
            if r["kind"] == "alias" and ":" in (r["value"] or ""):
                kind, val = r["value"].split(":", 1)
                m.aliases[r["key"]].append((kind, val, int(r["hits"] or 0), r["id"]))
            elif r["kind"] == "exemplar":
                try:
                    v = json.loads(r["value"])
                    m.exemplars[v["product_id"]].append({"listing_id": r["key"], "image_url": v.get("image_url"),
                                                         "title": v.get("title", ""), "rowid": r["id"]})
                except Exception:
                    pass
        return m

    def exemplars_for(self, cand_ids, skip_listing_ids=(), n=2):
        """Jusqu'a n exemplaires (1 par candidat, dans l'ordre des candidats)."""
        out = []
        for L, pid in cand_ids:
            for ex in self.exemplars.get(pid, []):
                if ex["listing_id"] not in skip_listing_ids and ex.get("image_url"):
                    out.append((L, ex))
                    break
            if len(out) >= n:
                break
        return out


def shortlist(idx: Index, title: str, mem: Memory | None = None, k: int = SHORTLIST_K, seed: str = "",
              hits: Counter | None = None, type_hint: str | None = None, offset: int = 0):
    """Produits [(score, product)] de rang offset..offset+k : tokens ponderes IDF + alias memorises + bonus de type."""
    ltoks = set(tokens(title, idx.bw, expand=True))
    scored = []
    for p in idx.products:
        title_t, all_t = idx.tok[p["id"]]
        s = 0.0
        for t in ltoks & all_t:
            s += idx.idf[t] * (1.0 if t in title_t else 0.7)
        if type_hint and idx.type_of[p["id"]] == type_hint:
            s += 4.0
        scored.append([s, p])
    if mem:
        for w in tokens(title, idx.bw):
            for kind, val, n, rowid in mem.aliases.get(w, []):
                used = False
                for sp in scored:
                    pid = sp[1]["id"]
                    if kind == "model" and idx.mk[pid] == val:
                        sp[0] += 2.0
                        used = True
                    elif kind == "token" and val in idx.tok[pid][1] and val not in ltoks:
                        sp[0] += 0.8 * idx.idf[val]
                        used = True
                if used and hits is not None:
                    hits[rowid] += 1

    # departage deterministe mais different selon l'annonce (evite que les memes produits servent de bouche-trou)
    def tie(p):
        return hashlib.md5((seed + p["id"]).encode()).hexdigest()
    scored.sort(key=lambda sp: (-sp[0], tie(sp[1])))
    return [(s, p) for s, p in scored[offset:offset + k]]


def baseline_match(listing_title: str, products, brand_words=()):
    """Workflow sans IA : un produit n'est retenu que si son nom de modele apparait dans le titre."""
    ln = norm(listing_title)
    ltoks = set(tokens(listing_title, brand_words))
    cands = []
    for p in products:
        mk = model_key(p.get("title"), p.get("product_type"))
        if mk and re.search(r"(?<![a-z0-9])" + re.escape(mk) + r"(?![a-z0-9])", ln):
            cands.append((-len(mk), -len(ltoks & set(tokens(p.get("title"), brand_words))), str(p["id"]), p["id"]))
    return sorted(cands)[0][3] if cands else None


def _mid(brand, kind, key, value) -> str:
    return hashlib.sha1(f"{brand}|{kind}|{key}|{value}".encode()).hexdigest()[:20]


def derive_memory(evidence, products_by_id, brand_words=()) -> Memory:
    """evidence : [{listing_id,title,image_url,product_id}] confirmes. Alias = mots recurrents (>=2, >=60 % du mot)."""
    m = Memory()
    counts, total = defaultdict(Counter), Counter()
    for ev in evidence:
        p = products_by_id.get(ev["product_id"])
        if not p:
            continue
        mk = model_key(p.get("title"), p.get("product_type"))
        ptoks = set(tokens(p.get("title"), brand_words)) | set(tokens(p.get("product_type"), brand_words))
        for w in set(tokens(ev.get("title"), brand_words, drop_colors=True)):
            if len(w) < 3:
                continue
            total[w] += 1
            if mk:
                counts[w][("model", mk)] += 1
            for t in ptoks:
                if t != w and len(t) >= 3:
                    counts[w][("token", t)] += 1
        m.exemplars[ev["product_id"]].append({"listing_id": ev["listing_id"], "image_url": ev.get("image_url"),
                                              "title": ev.get("title", ""),
                                              "rowid": _mid("", "exemplar", ev["listing_id"], ev["product_id"])})
    for w, c in counts.items():
        best = [(kv, n) for kv, n in c.most_common(6) if n >= 2 and n / total[w] >= 0.6][:3]
        for (kind, val), n in best:
            m.aliases[w].append((kind, val, n, _mid("", "alias", w, f"{kind}:{val}")))
    return m


def aggregate_products(rows):
    """rows : [{product_id,title,retail,condition_norm,price_eur}] -> stats par produit (tries par n decroissant)."""
    g = defaultdict(list)
    for r in rows:
        g[r["product_id"]].append(r)
    out = []
    for pid, rs in g.items():
        n = len(rs)
        tags = sum(1 for r in rs if r["condition_norm"] == "new_with_tags")
        notags = sum(1 for r in rs if r["condition_norm"] == "new_without_tags")
        prices = [r["price_eur"] for r in rs if r.get("price_eur") is not None]
        retail = rs[0].get("retail")
        avg = sum(prices) / len(prices) if prices else None
        out.append({"product_id": pid, "title": rs[0]["title"], "n": n,
                    "pct_new_with_tags": round(100 * tags / n), "pct_new_without_tags": round(100 * notags / n),
                    "avg_resale_eur": round(avg, 1) if avg is not None else None, "retail_eur": retail,
                    "resale_vs_retail_pct": round(100 * avg / retail) if avg is not None and retail else None})
    return sorted(out, key=lambda d: -d["n"])


# --------------------------------------------------------------------------- infra LLM / images / log
class Usage:
    def __init__(self):
        self.lock = threading.Lock()
        self.by_role = defaultdict(lambda: [0, 0, 0])

    def add(self, role, in_tok, out_tok):
        with self.lock:
            b = self.by_role[role]
            b[0] += in_tok or 0
            b[1] += out_tok or 0
            b[2] += 1

    def totals(self) -> dict:
        with self.lock:
            return {"llm_calls": sum(v[2] for v in self.by_role.values()),
                    "input_tokens": sum(v[0] for v in self.by_role.values()),
                    "output_tokens": sum(v[1] for v in self.by_role.values()),
                    "cost_usd": 0.0 if use_codex() else round(
                        sum(estimate_cost(r, v[0], v[1]) for r, v in self.by_role.items()), 4),
                    "calls_fast": self.by_role[FAST][2] if FAST in self.by_role else 0,
                    "calls_smart": self.by_role[SMART][2] if SMART in self.by_role else 0}


_LOG_LOCK = threading.Lock()


class Ctx:
    def __init__(self, brand, run_id=None, use_memory=True):
        self.brand, self.run_id, self.use_memory = brand, run_id, use_memory
        self.usage = Usage()
        self.t0 = time.time()
        self.hits = Counter()
        self.lock = threading.Lock()
        self.fatal = None

    def hit(self, rowid):
        with self.lock:
            self.hits[rowid] += 1

    def log(self, phase, msg):
        try:
            print(f"[{phase}] {msg}".encode("ascii", "replace").decode(), flush=True)
        except Exception:
            pass
        if not self.run_id:
            return
        with _LOG_LOCK:
            rows = q("SELECT log FROM runs WHERE id=:i", i=self.run_id)
            if not rows:
                return
            try:
                arr = json.loads(rows[0]["log"]) if rows[0]["log"] else []
            except Exception:
                arr = []
            arr.append({"t": datetime.now(timezone.utc).isoformat(), "phase": phase, "msg": msg})
            x("UPDATE runs SET log=:l WHERE id=:i", l=json.dumps(arr, ensure_ascii=False), i=self.run_id)

    def stats(self, **extra) -> dict:
        s = self.usage.totals()
        s["seconds"] = round(time.time() - self.t0, 1)
        s["backend"] = "codex" if use_codex() else "openai"
        try:
            mn = codex_model if use_codex() else model_name
            s["models"] = {"fast": mn(FAST), "smart": mn(SMART)}
        except Exception:
            pass
        s.update(extra)
        return s

    def save_stats(self, stats: dict):
        if self.run_id:
            x("UPDATE runs SET stats=:s WHERE id=:i", s=json.dumps(stats, ensure_ascii=False), i=self.run_id)


_CLIENT = None
_CLIENT_KEY = None


def get_client():
    global _CLIENT, _CLIENT_KEY
    k = _env("OPENAI_API_KEY")
    if not k:
        raise RuntimeError("OPENAI_API_KEY est vide dans .env")
    if _CLIENT is None or k != _CLIENT_KEY:
        _CLIENT = openai.OpenAI(api_key=k, max_retries=5, timeout=120)
        _CLIENT_KEY = k
    return _CLIENT


class Out:
    """Reponse normalisee : calls = [(id, nom, arguments_json)], text, truncated."""

    def __init__(self, calls, text, truncated):
        self.calls, self.text, self.truncated = calls, text, truncated


_FORCE_RESPONSES: set = set()


def _api_for(role):
    v = _env(f"OPENAI_API_{role.upper()}").lower()
    if role in _FORCE_RESPONSES:
        return "responses"
    return v if v in ("chat", "responses") else ("responses" if role == SMART else "chat")


def _chat(ctx, role, system, content, tools, tool_choice, max_tokens):
    kw = dict(model=model_name(role), messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
              max_completion_tokens=max_tokens)
    effort = _env(f"OPENAI_REASONING_EFFORT_{role.upper()}", "none")
    if effort.lower() not in ("default", "off"):
        kw["reasoning_effort"] = effort
    if tools:
        kw["tools"] = [{"type": "function", "function": t} for t in tools]
        kw["tool_choice"] = tool_choice if tool_choice in (None, "auto", "required") else \
            {"type": "function", "function": {"name": tool_choice}}
        kw["tool_choice"] = kw["tool_choice"] or "auto"
    try:
        resp = get_client().chat.completions.create(**kw)
    except openai.BadRequestError as e:
        m = str(e)
        if "param': 'reasoning_effort'" in m and "reasoning_effort" in kw and "set reasoning_effort to" not in m:
            kw.pop("reasoning_effort")  # parametre non supporte : on l'enleve plutot que de changer de modele
            resp = get_client().chat.completions.create(**kw)
        else:
            raise
    ctx.usage.add(role, resp.usage.prompt_tokens, resp.usage.completion_tokens)
    msg, ch = resp.choices[0].message, resp.choices[0]
    return Out([(tc.id, tc.function.name, tc.function.arguments) for tc in (msg.tool_calls or [])],
               msg.content or "", ch.finish_reason == "length")


def _to_responses_parts(content):
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    out = []
    for p in content:
        if p["type"] == "text":
            out.append({"type": "input_text", "text": p["text"]})
        else:
            out.append({"type": "input_image", "image_url": p["image_url"]["url"], "detail": p["image_url"].get("detail", "low")})
    return out


def _responses(ctx, role, system, content, tools, tool_choice, max_tokens):
    kw = dict(model=model_name(role), instructions=system, max_output_tokens=max_tokens,
              input=[{"role": "user", "content": _to_responses_parts(content)}])
    effort = _env(f"OPENAI_REASONING_EFFORT_{role.upper()}", "low")
    if effort.lower() not in ("default", "off"):
        kw["reasoning"] = {"effort": effort}
    if tools:
        kw["tools"] = [{"type": "function", "strict": False, **t} for t in tools]
        kw["tool_choice"] = tool_choice if tool_choice in ("auto", "required") else \
            ({"type": "function", "name": tool_choice} if tool_choice else "auto")
    try:
        resp = get_client().responses.create(**kw)
    except openai.BadRequestError as e:
        if "reasoning" in str(e) and "reasoning" in kw:
            kw.pop("reasoning")
            resp = get_client().responses.create(**kw)
        else:
            raise
    ctx.usage.add(role, resp.usage.input_tokens, resp.usage.output_tokens)
    calls = [(o.call_id, o.name, o.arguments) for o in resp.output if o.type == "function_call"]
    return Out(calls, getattr(resp, "output_text", "") or "", resp.status == "incomplete")


def llm(ctx: Ctx, role, system, content, tools=None, tool_choice=None, max_tokens=2000) -> Out:
    """tools = [{name, description, parameters}] ; tool_choice = None|'auto'|'required'|nom d'outil force.
    FAST -> Chat Completions ; SMART -> Responses API (override : OPENAI_API_FAST / OPENAI_API_SMART = chat|responses ;
    repli automatique vers Responses si le modele refuse les function tools sur Chat Completions)."""
    if _api_for(role) == "chat":
        try:
            return _chat(ctx, role, system, content, tools, tool_choice, max_tokens)
        except openai.BadRequestError as e:
            if not (tools and ("/v1/responses" in str(e) or "Function tools with reasoning_effort" in str(e))):
                raise
            _FORCE_RESPONSES.add(role)
    return _responses(ctx, role, system, content, tools, tool_choice, max_tokens)


def tool_args(out: Out, name):
    for _id, n, args in out.calls:
        if n == name:
            try:
                return json.loads(args or "{}")
            except Exception:
                return None
    return None


def call_for_tool(ctx, role, system, messages, tool, max_tokens):
    """Appel structure : function calling avec outil force ; repli 'required' si le forcage est refuse,
    relance avec plus de tokens si la sortie est tronquee (modeles a raisonnement)."""
    content = messages[-1]["content"]
    choice = tool["name"]
    for _ in range(3):
        try:
            out = llm(ctx, role, system, content, [tool], choice, max_tokens)
        except openai.BadRequestError as e:
            if "tool_choice" in str(e) and choice != "required":
                choice = "required"
                continue
            raise
        inp = tool_args(out, tool["name"])
        if inp is not None:
            return inp
        if out.truncated:
            max_tokens *= 2
            continue
        break
    return None


_IMG_CACHE: dict = {}
_IMG_LOCK = threading.Lock()


def _media_type(b: bytes):
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


def sized(url, width=400):
    """Les images Shopify acceptent ?width=400 (moins de poids)."""
    if url and ("cdn.shopify.com" in url or "/cdn/shop/" in url):
        p = urlparse(url)
        qs = dict(parse_qsl(p.query))
        qs["width"] = str(width)
        return urlunparse(p._replace(query=urlencode(qs)))
    return url


def data_url(url, cache=True):
    """Telecharge l'image -> data URL base64 ; None si echec (l'image est simplement ignoree)."""
    if not url:
        return None
    if cache:
        with _IMG_LOCK:
            if url in _IMG_CACHE:
                return _IMG_CACHE[url]
    out = None
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 ResaleRadar"})
        r.raise_for_status()
        mt = _media_type(r.content)
        if mt and len(r.content) < 15_000_000:
            out = f"data:{mt};base64,{base64.standard_b64encode(r.content).decode()}"
    except Exception:
        out = None
    if cache:
        with _IMG_LOCK:
            _IMG_CACHE[url] = out
    return out


def image_block(url, cache=True, detail="low"):
    """Partie de message image_url (detail 'low' = 85 tokens fixes, suffisant pour des vignettes)."""
    d = data_url(url, cache)
    return {"type": "image_url", "image_url": {"url": d, "detail": detail}} if d else None


# --------------------------------------------------------------------------- backend Codex (LLM_BACKEND=codex)
CODEX_COLLECT_TURNS = 6


def _strict(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


COLLECT_SCHEMA = _strict({"action": {"type": "string", "enum": ["fetch", "finish"]},
                          "country": {"type": ["string", "null"]}, "page": {"type": ["integer", "null"]},
                          "reason": {"type": "string"}})
def _ident_schema(idx) -> dict:
    """category = enum des familles du catalogue + 'Autre' (le modele ne peut pas inventer une famille)."""
    return _strict({"category": {"type": "string", "enum": list(idx.type_names.values()) + ["Autre"]},
                    "candidate": {"type": ["integer", "null"]}, "confidence": {"type": "number"},
                    "reason": {"type": "string"}})


# champs fixes plutot qu'un tableau : mesure du 22/10, le modele renvoyait `{"insights": []}` avec un tableau
INSIGHTS_SCHEMA = _strict({f"insight_{i}": {"type": "string"} for i in range(1, 6)})


def _cx(ctx, role, prompt, images, schema, effort="low"):
    """Un appel Codex (2 essais) ; comptabilise les tokens dans ctx.usage ; None si echec."""
    for _ in range(2):
        r = llm_codex.codex_json(prompt, images, schema, codex_model(role), effort=effort,
                                 on_usage=lambda i, o: ctx.usage.add(role, i, o))
        if r is not None:
            return r
    return None


def _cx_pick(ctx, role, listing, cands, idx, mem, per_cand, skip_ids, effort="low"):
    """Un appel Codex d'identification : annonce + images candidats -> dict(category, product_id, confidence, reason)|None."""
    urls = [[sized(u, 400) for u in _product_images(c)[:per_cand]] for c in cands]
    lurl = listing.get("image_url")
    flat = sorted({u for us in urls for u in us} | ({lurl} if lurl else set()))
    got = dict(zip(flat, _PREFETCH.map(llm_codex.fetch_image, flat)))
    images, lines = [], []
    if got.get(lurl):
        images.append(got[lurl])
    head = "Image 1 = photo de l'annonce Vinted." if images else "(photo de l'annonce indisponible : base-toi sur le titre)"
    for i, (c, us) in enumerate(zip(cands, urls), 1):
        nums = []
        for u in us:
            if got.get(u):
                images.append(got[u])
                nums.append(str(len(images)))
        lines.append(f"Candidat {i} | product_id={c['id']} | {c['title']} | famille : {c.get('product_type') or '?'} | "
                     f"prix neuf : {_fmt_price(c.get('price_eur'))} EUR | " +
                     (f"image(s) {', '.join(nums)}" if nums else "photo indisponible"))
    mem_lines = []
    if ctx.use_memory and mem is not None:
        for L, ex in mem.exemplars_for([(i, c["id"]) for i, c in enumerate(cands, 1)], skip_ids, n=2):
            b = llm_codex.fetch_image(ex["image_url"])
            if b:
                images.append(b)
                mem_lines.append(f"MEMOIRE : image {len(images)} = une annonce deja confirmee comme etant le candidat {L}.")
                ctx.hit(ex["rowid"])
    prompt = (f"Tu identifies un article de seconde main revendu sur Vinted. {head}\n"
              f"ANNONCE : titre : {listing['title']} | etat : {listing.get('condition') or listing.get('condition_norm') or '?'}"
              f" | prix : {_fmt_price(listing.get('price_eur'))} EUR (pays {listing.get('country')})\n"
              "CANDIDATS DU CATALOGUE (photos produit officielles, images numerotees dans l'ordre des fichiers joints) :\n" +
              "\n".join(lines + mem_lines) + "\nFamilles du catalogue (nombre de produits) : " + idx.type_prompt +
              "\nTACHE : category = famille du catalogue de l'article, copiee exactement dans la liste, ou 'Autre' ; "
              "candidate = le NUMERO (1 a " + str(len(cands)) + ") du candidat qui est le meme produit : compare "
              "l'imprime/coloris et la forme de l'annonce a CHAQUE candidat ; si l'un d'eux a le meme imprime et la meme forme, "
              "renvoie son numero (les marques sur la photo ne sont pas necessaires) ; null seulement si AUCUN candidat n'a "
              "le meme imprime et la meme forme ; confidence = 0 a 1 (0.9+ certain, <0.5 incertain) ; "
              "reason = une phrase courte en francais citant le candidat retenu. Reponds uniquement en JSON.")
    r = _cx(ctx, role, prompt, images, _ident_schema(idx), effort)
    if r is None:
        return None
    k = r.get("candidate")   # numero du candidat (1..N) : plus fiable qu'un identifiant recopie par le modele
    pid = cands[k - 1]["id"] if isinstance(k, int) and not isinstance(k, bool) and 1 <= k <= len(cands) else None
    try:
        conf = max(0.0, min(1.0, float(r.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    return {"category": idx.type_names.get(norm(r.get("category"))) or "Autre", "product_id": pid,
            "confidence": conf, "reason": str(r.get("reason", ""))[:400]}


def identify_one_codex(ctx: Ctx, listing: dict, idx: Index, use_mem, skip) -> dict:
    """UN appel combine (annonce + 8 candidats) ; si confiance < ESCALATE_BELOW : un appel SMART (effort moyen) sur 4 x 2 images.
    CODEX_IDENT_ROLE=smart fait passer aussi l'appel combine sur le modele SMART (plus lent, pas meilleur en test)."""
    role1 = SMART if (_env("CODEX_IDENT_ROLE", "fast") or "fast").lower() == "smart" else FAST
    cands = [p for _, p in shortlist(idx, listing["title"], use_mem, SHORTLIST_K, seed=listing["id"],
                                     hits=ctx.hits if use_mem else None)]
    if not cands:
        return {"listing_id": listing["id"], "steps": 0, "escalated": False, "category": "Autre", "product_id": None,
                "confidence": 0.0, "reasoning": "Catalogue vide"}
    r = _cx_pick(ctx, role1, listing, cands, idx, use_mem, 1, skip)
    if r is None:
        raise RuntimeError("appel Codex en echec ou timeout")
    steps = 1
    if r["product_id"] is None and r["category"] != "Autre":   # aucun des 8 : 8 autres candidats de la famille reperee
        seen = {c["id"] for c in cands}
        more = [p for _, p in shortlist(idx, listing["title"], use_mem, SHORTLIST_K + len(seen), seed=listing["id"],
                                        hits=ctx.hits if use_mem else None, type_hint=norm(r["category"]))
                if p["id"] not in seen][:SHORTLIST_K]
        r_b = _cx_pick(ctx, role1, listing, more, idx, use_mem, 1, skip) if more else None
        if r_b is not None:
            cands, r, steps = more, r_b, 2
    res = {"listing_id": listing["id"], "steps": steps, "escalated": False, "category": r["category"],
           "product_id": r["product_id"], "confidence": r["confidence"], "reasoning": r["reason"]}
    if r["confidence"] < ESCALATE_BELOW:
        top = ([idx.by_id[r["product_id"]]] if r["product_id"] else []) + [c for c in cands if c["id"] != r["product_id"]]
        r2 = _cx_pick(ctx, SMART, listing, top[:4], idx, use_mem, 2, skip, effort="medium")
        if r2 is None:
            res.update(steps=steps + 1, escalated=True, reasoning=res["reasoning"] + " (escalade Codex echouee)")
        else:
            res.update(steps=steps + 1, escalated=True, category=r2["category"], product_id=r2["product_id"],
                       confidence=r2["confidence"], reasoning=f"[arbitrage] {r2['reason']}")
    if res["confidence"] < UNKNOWN_BELOW:
        res["product_id"] = None
    if res["product_id"]:   # produit identifie : sa categorie fait foi
        res["category"] = idx.by_id[res["product_id"]].get("product_type") or res["category"]
    return res


# --------------------------------------------------------------------------- phase 1 : COLLECT
COLLECT_TOOLS = [
    {"name": "fetch_vinted_catalog",
     "description": "Recupere une page du catalogue Vinted de la marque dans un pays (~155 annonces par page) et les enregistre.",
     "parameters": {"type": "object", "properties": {
         "country": {"type": "string", "enum": ["fr", "de", "nl", "it", "es", "be"]},
         "page": {"type": "integer", "description": "numero de page, a partir de 1"},
         "why": {"type": "string", "description": "une phrase courte en francais qui justifie ce choix"}},
         "required": ["country", "page", "why"]}},
    {"name": "finish_collection",
     "description": "Termine la collecte quand la couverture est suffisante ou que le gain marginal est faible.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]

COLLECT_SYSTEM = """Tu es l'agent de collecte de Resale Radar. Objectif : maximiser le nombre d'annonces Vinted UNIQUES
de la marque avec le minimum d'appels. Chaque appel coute du temps et de l'argent : budget maximum 12 appels.
Commence par fr page 1. Ensuite, decide d'apres le rendement : une page est rentable si le nombre d'annonces NOUVELLES
depasse ~10 % des cartes. Arrete (finish_collection) quand le rendement marginal tombe sous ~10 % ou que le budget est
atteint. Explique chaque choix en une phrase courte en francais dans le champ `why`. Appelle toujours un outil, un seul
a la fois."""


def phase_collect(ctx: Ctx, max_listings=None, max_calls=12) -> dict:
    from app.catalog import load_shopify_catalog
    from app.collect import COUNTRIES, collect_page

    if not q("SELECT 1 AS n FROM products WHERE brand=:b LIMIT 1", b=ctx.brand):
        ctx.log("collect", "Catalogue produits vide : chargement du catalogue Shopify")
        n = load_shopify_catalog(ctx.brand)
        ctx.log("collect", f"Catalogue Shopify charge : {n} produits")
    have = q("SELECT COUNT(*) AS n FROM listings WHERE brand=:b", b=ctx.brand)[0]["n"]
    if max_listings and have >= max_listings:
        ctx.log("collect", f"Collecte sautee : {have} annonces deja en base (objectif {max_listings})")
        return {"calls": 0, "collected_new": 0}

    mem_txt = ""
    if ctx.use_memory:
        qm = q("SELECT key, value FROM memory WHERE brand=:b AND kind='query' ORDER BY created_at DESC", b=ctx.brand)
        if qm:
            mem_txt = "\nMemoire des collectes precedentes (pays:page -> rendement) : " + "; ".join(
                f"{r['key']} -> {r['value']}" for r in qm[:15])
    goal = f" Objectif : environ {max_listings} annonces au total." if max_listings else ""
    base = (f"Marque : {ctx.brand}. Annonces deja en base : {have}. Pays disponibles : {', '.join(COUNTRIES)}."
            f"{goal}{mem_txt}")
    calls, new_total, seen, notebook = 0, 0, set(), []
    for _ in range(CODEX_COLLECT_TURNS if use_codex() else 16):
        prompt = (base + "\nCarnet des appels deja faits : " + (" | ".join(notebook) or "(aucun)") +
                  f"\nAppels restants : {max_calls - calls}. Choisis la prochaine action.")
        if use_codex():
            inp = _cx(ctx, SMART, COLLECT_SYSTEM + "\n\n" + prompt + "\nReponds en JSON : action ('fetch' pour recuperer "
                      "une page, 'finish' pour terminer), country (code pays ou null), page (entier ou null), reason (une "
                      "phrase courte en francais).", [], COLLECT_SCHEMA)
            if not inp:
                break
            name = "finish_collection" if inp.get("action") == "finish" else "fetch_vinted_catalog"
            inp["why"] = inp.get("reason", "")
        else:
            out = llm(ctx, SMART, COLLECT_SYSTEM, prompt, COLLECT_TOOLS, "required", max_tokens=3000)
            if not out.calls:
                break
            _id, name, args = out.calls[0]  # un seul outil a la fois
            try:
                inp = json.loads(args or "{}")
            except Exception:
                inp = {}
        if name == "finish_collection":
            ctx.log("collect", f"Fin de collecte : {inp.get('reason', '')}")
            break
        c = inp.get("country")
        try:
            p = int(inp.get("page") or 1)
        except (TypeError, ValueError):
            p = 1
        if calls >= max_calls:
            ctx.log("collect", "Budget d'appels epuise : collecte arretee")
            break
        if (c, p) in seen or c not in COUNTRIES:
            notebook.append(f"ERREUR {c} p{p} : deja recuperee ou pays invalide")
            continue
        seen.add((c, p))
        calls += 1
        ctx.log("collect", f"Appel {calls}/{max_calls} : {c} page {p} - {inp.get('why', '')}")
        try:
            r = collect_page(ctx.brand, c, p, ctx.run_id)
        except Exception as e:  # noqa: BLE001
            ctx.log("collect", f"Echec {c} p{p} : {str(e)[:150]}")
            notebook.append(f"ERREUR {c} p{p} : {str(e)[:100]}")
            continue
        new_total += r["new"]
        pct = round(100 * r["new"] / r["cards"]) if r["cards"] else 0
        ctx.log("collect", f"{c} page {p} : {r['cards']} cartes, {r['new']} nouvelles ({pct} %), total {r['total']}")
        _remember_query(ctx, c, p, r)
        notebook.append(f"{c} p{p} : {r['cards']} cartes, {r['new']} nouvelles ({pct} %), total {r['total']}")
        if max_listings and r["total"] >= max_listings:
            ctx.log("collect", "Objectif d'annonces atteint : collecte arretee")
            break
    return {"calls": calls, "collected_new": new_total}


def _remember_query(ctx, country, page, r):
    key = f"{country}:{page}"
    val = f"{r['cards']} cartes, {r['new']} nouvelles"
    x("""INSERT INTO memory(id, brand, kind, key, value, hits, created_at) VALUES(:id, :b, 'query', :k, :v, 1, :t)
         ON CONFLICT (id) DO UPDATE SET value=excluded.value, hits=memory.hits+1""",
      id=_mid(ctx.brand, "query", key, ""), b=ctx.brand, k=key, v=val, t=datetime.now(timezone.utc).isoformat())


# --------------------------------------------------------------------------- phase 2 : IDENTIFY
SUBMIT_TOOL = {
    "name": "submit_match",
    "description": "Soumet l'identification de l'annonce.",
    "parameters": {"type": "object", "properties": {
        "candidate": {"type": "string", "description": "Numero du candidat identique a l'annonce (ex: '7') ou 'none' si aucun ne correspond."},
        "confidence": {"type": "number", "description": "Confiance de 0 a 1 (0.9+ = certain, <0.5 = incertain)."},
        "plausible": {"type": "array", "items": {"type": "string"},
                      "description": "Autres numeros plausibles (3 max), a verifier si tu doutes."},
        "reason": {"type": "string", "description": "Une phrase courte en francais."}},
        "required": ["candidate", "confidence", "plausible", "reason"]},
}

DESCRIBE_TOOL = {
    "name": "describe_listing",
    "description": "Decrit l'article de l'annonce pour guider la recherche dans le catalogue.",
    "parameters": {"type": "object", "properties": {
        "product_type": {"type": "string", "description": "Categorie de l'article : un type de la liste du catalogue, copie EXACTEMENT, ou 'Autre' si aucun ne convient."},
        "keywords": {"type": "array", "items": {"type": "string"},
                     "description": "3 a 6 mots francais utiles : coloris, motif/imprime, taille, forme."},
        "reason": {"type": "string"}},
        "required": ["product_type", "keywords", "reason"]},
}

ID_SYSTEM = """Tu identifies des articles de seconde main revendus sur Vinted : pour une annonce (photo + titre) tu dois
dire lequel des candidats du catalogue de la marque est EXACTEMENT le meme produit (meme modele, meme forme, meme
imprime/coloris, meme taille si visible). Si aucun candidat de CETTE LISTE ne correspond, reponds 'none' (le produit est
peut-etre dans la page suivante de candidats, ou hors catalogue). Sois honnete sur ta confiance. Reponds uniquement en
appelant l'outil submit_match."""

DESC_SYSTEM = """Tu es l'etape de reperage d'un agent d'identification de produits de seconde main. A partir de la photo et
du titre d'une annonce Vinted, indique le type de produit du catalogue le plus proche (liste fournie) et quelques mots-cles
visuels en francais. Reponds uniquement en appelant l'outil describe_listing."""


def _fmt_price(v):
    return f"{v:.0f}" if isinstance(v, (int, float)) else "?"


_PREFETCH = ThreadPoolExecutor(max_workers=8)


def _images_for(cands, per_cand, width, detail):
    """Telecharge en parallele les images candidats -> liste de listes de parties image_url."""
    jobs = [[sized(u, width) for u in _product_images(c)[:per_cand]] for c in cands]
    flat = sorted({u for urls in jobs for u in urls})
    got = dict(zip(flat, _PREFETCH.map(data_url, flat)))
    return [[{"type": "image_url", "image_url": {"url": got[u], "detail": detail}} for u in urls if got.get(u)]
            for urls in jobs]


def _describe(ctx, listing, idx):
    content = [{"type": "text", "text": f"ANNONCE VINTED\nTitre : {listing['title']}\nEtat : "
                f"{listing.get('condition') or '?'}\nPrix : {_fmt_price(listing.get('price_eur'))} EUR"}]
    lb = image_block(listing.get("image_url"), cache=False)
    content.append(lb or {"type": "text", "text": "(photo indisponible)"})
    content.append({"type": "text", "text": "Types de produits du catalogue (nombre de produits) : " + idx.type_prompt +
                    "\nChoisis le product_type parmi cette liste (ou 'Autre'). Appelle describe_listing."})
    inp = call_for_tool(ctx, FAST, DESC_SYSTEM, [{"role": "user", "content": content}], DESCRIBE_TOOL, 3000)
    if not inp:
        return None, []
    t = norm(inp.get("product_type"))
    kws = [str(k) for k in (inp.get("keywords") or [])][:8]
    return (t if t in idx.type_names else "autre"), kws   # "autre" = le modele a repondu, hors liste


def _pick(ctx, role, listing, cands, mem, per_cand, width, detail, max_tokens, skip_ids, page_note=""):
    """Un appel vision : dict(product_id, confidence, reason, plausible_ids)."""
    labels = {str(i + 1): c for i, c in enumerate(cands)}
    content = [{"type": "text", "text": f"ANNONCE VINTED A IDENTIFIER\nTitre : {listing['title']}\n"
                f"Etat : {listing.get('condition') or listing.get('condition_norm') or '?'}\n"
                f"Prix : {_fmt_price(listing.get('price_eur'))} EUR (pays {listing.get('country')})"}]
    lb = image_block(listing.get("image_url"), cache=False, detail=detail)
    content.append(lb or {"type": "text", "text": "(photo de l'annonce indisponible : base-toi sur le titre)"})
    content.append({"type": "text", "text": f"CANDIDATS DU CATALOGUE{page_note} (photos produit officielles) :"})
    for (L, c), blocks in zip(labels.items(), _images_for(cands, per_cand, width, detail)):
        content.append({"type": "text", "text": f"Candidat {L} : {c['title']} | type : {c.get('product_type') or '?'} | "
                        f"prix neuf : {_fmt_price(c.get('price_eur'))} EUR" + ("" if blocks else " (photo indisponible)")})
        content.extend(blocks)
    if ctx.use_memory and mem is not None:
        for L, ex in mem.exemplars_for([(L, c["id"]) for L, c in labels.items()], skip_ids, n=2):
            blk = image_block(ex["image_url"], detail=detail)
            if blk:
                content.append({"type": "text", "text": f"MEMOIRE : une annonce Vinted deja confirmee comme etant le "
                                f"candidat {L} ({ex.get('title', '')[:80]}) ressemblait a ceci :"})
                content.append(blk)
                ctx.hit(ex["rowid"])
    content.append({"type": "text", "text": "Quel candidat est le meme produit que l'annonce ? Appelle submit_match."})
    inp = call_for_tool(ctx, role, ID_SYSTEM, [{"role": "user", "content": content}], SUBMIT_TOOL, max_tokens)
    if not inp:
        return {"product_id": None, "confidence": 0.0, "reason": "pas de reponse structuree", "plausible_ids": []}
    cand = str(inp.get("candidate", "none")).strip().lower().lstrip("#")
    try:
        conf = max(0.0, min(1.0, float(inp.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    pid = labels[cand]["id"] if cand in labels else None
    plaus = [labels[str(l).strip()]["id"] for l in (inp.get("plausible") or []) if str(l).strip() in labels]
    return {"product_id": pid, "confidence": conf, "reason": str(inp.get("reason", ""))[:400], "plausible_ids": plaus}


def identify_one(ctx: Ctx, listing: dict, idx: Index, mem: Memory | None, skip_ids=()) -> dict:
    """Cascade : reperage du type (FAST) -> pages de candidats (FAST vision) -> arbitre (SMART) si doute."""
    skip = set(skip_ids) | {listing["id"]}
    use_mem = mem if ctx.use_memory else None
    hits = ctx.hits if use_mem else None
    if use_codex():
        return identify_one_codex(ctx, listing, idx, use_mem, skip)
    steps, trace = 0, []
    try:
        dtype, kws = _describe(ctx, listing, idx)
    except FATAL:
        raise
    except Exception:  # noqa: BLE001  (le reperage est facultatif)
        dtype, kws = None, []
    steps += 1
    query = listing["title"] + " " + " ".join(kws)
    described_cat = idx.type_names.get(dtype) or ("Autre" if dtype == "autre" else None)   # None = reperage en echec
    trace.append(f"type={described_cat or '?'}")

    best, none_conf = None, 0.0
    for page in range(MAX_PAGES):
        sl = shortlist(idx, query, use_mem, SHORTLIST_K, seed=listing["id"], hits=hits, type_hint=dtype,
                       offset=page * SHORTLIST_K)
        cands = [p for _, p in sl]
        if not cands:
            break
        r = _pick(ctx, FAST, listing, cands, use_mem, 1, 400, "low", 3000, skip,
                  page_note=f" (page {page + 1}/{MAX_PAGES})" if page else "")
        steps += 1
        if r["product_id"]:
            best = {**r, "cands": cands}
            break
        none_conf = max(none_conf, r["confidence"])
        trace.append(f"page {page + 1}: aucun")
    res = {"listing_id": listing["id"], "steps": steps, "escalated": False, "category": described_cat}
    if best is None:
        res.update(product_id=None, confidence=none_conf or 0.0,
                   reasoning=f"Aucun candidat ne correspond ({'; '.join(trace)})")
        return res
    res.update(product_id=best["product_id"], confidence=best["confidence"], reasoning=best["reason"])
    if best["confidence"] < ESCALATE_BELOW:
        order = [best["product_id"]] + best["plausible_ids"] + [p["id"] for p in best["cands"]]
        top = []
        for pid in order:
            if pid not in top:
                top.append(pid)
        top = [idx.by_id[pid] for pid in top[:4]]
        try:
            r2 = _pick(ctx, SMART, listing, top, use_mem, 3, 600, _env("OPENAI_DETAIL_SMART", "low"), 6000, skip)
            res.update(product_id=r2["product_id"], confidence=r2["confidence"],
                       reasoning=f"[arbitrage] {r2['reason']}", steps=steps + 1, escalated=True)
        except FATAL:
            raise
        except Exception as e:  # noqa: BLE001  (on garde la lecture du modele rapide)
            res.update(steps=steps + 1, escalated=True, reasoning=res["reasoning"] + f" (escalade echouee : {str(e)[:80]})")
    if res["confidence"] < UNKNOWN_BELOW:
        res["product_id"] = None
    if res["product_id"]:   # produit identifie : sa categorie fait foi
        res["category"] = idx.by_id[res["product_id"]].get("product_type") or described_cat
    return res


def identify_batch(ctx: Ctx, listings, idx: Index, mem_for, workers=6, on_result=None) -> list[dict]:
    """mem_for(listing) -> Memory|None. Retourne les resultats (meme ordre) ; erreur -> {'error': ...}."""
    if use_codex():
        workers = int(_env("CODEX_CONCURRENCY", "6") or 6)

    def work(l):
        if ctx.fatal:
            return {"listing_id": l["id"], "error": "abandon"}
        try:
            return identify_one(ctx, l, idx, mem_for(l))
        except FATAL as e:
            ctx.fatal = e
            return {"listing_id": l["id"], "error": f"auth: {e}"}
        except Exception as e:  # noqa: BLE001
            return {"listing_id": l["id"], "error": f"{type(e).__name__}: {str(e)[:150]}"}

    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, l): l["id"] for l in listings}
        for f in as_completed(futs):
            r = f.result()
            out[futs[f]] = r
            if on_result:
                on_result(r)
    if ctx.fatal:
        raise ctx.fatal
    return [out[l["id"]] for l in listings]


UPSERT_MATCH = """INSERT INTO matches(listing_id, product_id, confidence, method, reasoning, steps, run_id, created_at, category)
VALUES(:lid, :pid, :conf, 'agent', :why, :steps, :run, :t, :cat)
ON CONFLICT (listing_id) DO UPDATE SET product_id=excluded.product_id, confidence=excluded.confidence,
method=excluded.method, reasoning=excluded.reasoning, steps=excluded.steps, run_id=excluded.run_id,
created_at=excluded.created_at, category=excluded.category"""


def phase_identify(ctx: Ctx, max_listings=None) -> dict:
    products = q("SELECT * FROM products WHERE brand=:b", b=ctx.brand)
    if not products:
        raise RuntimeError("Catalogue produits vide : rien a identifier")
    idx = Index(products, brand_words_for(ctx.brand))
    todo = q("""SELECT l.* FROM listings l LEFT JOIN matches m ON m.listing_id=l.id
                WHERE l.brand=:b AND m.listing_id IS NULL ORDER BY l.last_seen DESC, l.id""", b=ctx.brand)
    if max_listings:
        todo = todo[:max_listings]
    ctx.log("identify", f"{len(todo)} annonces a identifier ({len(products)} produits au catalogue, memoire "
            f"{'activee' if ctx.use_memory else 'desactivee'})")
    mem = Memory.load(ctx.brand) if ctx.use_memory else None
    if mem:
        ctx.log("identify", f"Memoire chargee : {sum(len(v) for v in mem.aliases.values())} alias, "
                f"{sum(len(v) for v in mem.exemplars.values())} exemplaires")
    counters = Counter()
    titles = {l["id"]: l["title"] or "" for l in todo}

    def on_result(r):
        counters["done"] += 1
        if r.get("error"):
            counters["errors"] += 1
            if counters["errors"] <= 3:
                ctx.log("identify", f"Erreur annonce {r['listing_id']} : {r['error']}")
        else:
            x(UPSERT_MATCH, lid=r["listing_id"], pid=r["product_id"], conf=r["confidence"], why=r["reasoning"],
              steps=r["steps"], run=ctx.run_id, t=datetime.now(timezone.utc).isoformat(), cat=r.get("category"))
            counters["matched" if r["product_id"] else "unknown"] += 1
            counters["escalated"] += int(r["escalated"])
            if r["escalated"] and counters["escalated"] <= 12:
                ctx.log("identify", f"Doute sur « {titles.get(r['listing_id'], '')[:50]} » : arbitrage -> "
                        f"{'produit ' + str(r['product_id']) if r['product_id'] else 'inconnu'} "
                        f"(confiance {r['confidence']:.2f}) {r['reasoning'][:90]}")
        if counters["done"] % 10 == 0 or counters["done"] == len(todo):
            ctx.log("identify", f"{counters['done']}/{len(todo)} : {counters['matched']} identifiees, "
                    f"{counters['unknown']} inconnues, {counters['escalated']} arbitrees par le modele expert")
            ctx.save_stats(ctx.stats(listings_total=len(todo), matched=counters["matched"],
                                     unknown=counters["unknown"], escalated=counters["escalated"]))

    if todo:
        identify_batch(ctx, todo, idx, lambda l: mem, workers=6, on_result=on_result)
    if ctx.use_memory and ctx.hits:
        for rowid, n in ctx.hits.items():
            x("UPDATE memory SET hits=hits+:n WHERE id=:i", n=n, i=rowid)
    return {"listings": len(todo), "matched": counters["matched"], "unknown": counters["unknown"],
            "escalated": counters["escalated"], "errors": counters["errors"]}


# --------------------------------------------------------------------------- phase 3 : LEARN
def load_evidence(brand: str, min_conf=LEARN_MIN_CONF) -> list[dict]:
    ev = {}
    for r in q("""SELECT m.listing_id, m.product_id, l.title, l.image_url FROM matches m
                  JOIN listings l ON l.id=m.listing_id
                  WHERE l.brand=:b AND m.product_id IS NOT NULL AND m.confidence>=:c""", b=brand, c=min_conf):
        ev[r["listing_id"]] = {**r, "src": "agent"}
    for r in q("""SELECT lb.listing_id, lb.product_id, l.title, l.image_url FROM labels lb
                  JOIN listings l ON l.id=lb.listing_id WHERE l.brand=:b""", b=brand):
        if r["product_id"] == "NONE":
            ev.pop(r["listing_id"], None)  # l'humain dit « hors catalogue » : il override l'agent
        elif r["product_id"]:
            ev[r["listing_id"]] = {**r, "src": "label"}
    return list(ev.values())


def persist_memory(brand: str, mem: Memory) -> int:
    existing = {r["id"] for r in q("SELECT id FROM memory WHERE brand=:b", b=brand)}
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    sql = """INSERT INTO memory(id, brand, kind, key, value, hits, created_at) VALUES(:id, :b, :k, :key, :v, :h, :t)
             ON CONFLICT (id) DO UPDATE SET value=excluded.value"""
    for w, lst in mem.aliases.items():
        for kind, val, n, rowid in lst:
            rid = _mid(brand, "alias", w, f"{kind}:{val}")
            x(sql, id=rid, b=brand, k="alias", key=w, v=f"{kind}:{val}", h=0, t=now)
            added += rid not in existing
    for pid, lst in mem.exemplars.items():
        for ex in lst:
            rid = _mid(brand, "exemplar", ex["listing_id"], pid)
            x(sql, id=rid, b=brand, k="exemplar", key=ex["listing_id"],
              v=json.dumps({"product_id": pid, "image_url": ex.get("image_url"), "title": ex.get("title", "")},
                           ensure_ascii=False), h=0, t=now)
            added += rid not in existing
    return added


def phase_learn(ctx: Ctx) -> int:
    products = q("SELECT * FROM products WHERE brand=:b", b=ctx.brand)
    ev = load_evidence(ctx.brand)
    n_lab = sum(1 for e in ev if e["src"] == "label")
    mem = derive_memory(ev, {p["id"]: p for p in products}, brand_words_for(ctx.brand))
    added = persist_memory(ctx.brand, mem)
    ctx.log("learn", f"Apprentissage : {len(ev)} cas confirmes ({n_lab} par etiquettes humaines) -> "
            f"{added} nouvelles memoires ({sum(len(v) for v in mem.aliases.values())} alias, "
            f"{sum(len(v) for v in mem.exemplars.values())} exemplaires au total)")
    return added


# --------------------------------------------------------------------------- phase 4 : INSIGHTS
def phase_insights(ctx: Ctx) -> list[str]:
    rows = q("""SELECT m.product_id, p.title, p.price_eur AS retail, l.condition_norm, l.price_eur
                FROM matches m JOIN listings l ON l.id=m.listing_id JOIN products p ON p.id=m.product_id
                WHERE l.brand=:b AND m.product_id IS NOT NULL""", b=ctx.brand)
    if not rows:
        ctx.log("insights", "Aucune annonce identifiee : pas d'insights")
        return []
    agg = aggregate_products(rows)
    n_all = len(rows)
    new_all = sum(1 for r in rows if r["condition_norm"] in ("new_with_tags", "new_without_tags"))
    payload = {"annonces_identifiees": n_all, "pct_neuf_global": round(100 * new_all / n_all),
               "produits_distincts": len(agg), "top_produits": agg[:25]}
    prompt = ("Donnees de revente Vinted de la marque " + ctx.brand + " (par produit : n annonces, % neuf avec etiquette, "
              "% neuf sans etiquette, prix moyen de revente vs prix neuf catalogue) :\n" +
              json.dumps(payload, ensure_ascii=False) +
              "\n\nRedige 3 a 5 insights en francais pour l'equipe produit de la marque : produits indesirables "
              "(beaucoup de neufs revendus = surstock, cadeaux non voulus, mauvais coloris), produits qui gardent leur "
              "valeur, points d'attention. Chiffres a l'appui, prudence si n est petit. Reponds uniquement par des "
              "puces, une par ligne, commencant par '- '.")
    if use_codex():
        r = _cx(ctx, SMART, "Tu es analyste e-commerce pour une marque de maroquinerie/bagagerie.\n\n" + prompt.replace(
            "Reponds uniquement par des puces, une par ligne, commencant par '- '.",
            "Reponds en JSON : insight_1 a insight_5 = une phrase d'analyse chiffree en francais chacune "
            "(insight_4 et insight_5 peuvent etre vides)."), [], INSIGHTS_SCHEMA)
        bullets = [str(v).replace("**", "").strip() for k, v in sorted((r or {}).items()) if str(v).strip()][:5]
    else:
        resp = llm(ctx, SMART, "Tu es analyste e-commerce pour une marque de maroquinerie/bagagerie.",
                   prompt, max_tokens=8000)
        text = resp.text
        bullets = [re.sub(r"^[-*•]\s*", "", l).strip() for l in text.splitlines() if re.match(r"^\s*[-*•]\s+", l)]
        bullets = [b.replace("**", "") for b in bullets][:5]
    ctx.log("insights", f"{len(bullets)} insights generes")
    return bullets


# --------------------------------------------------------------------------- orchestration
def run_agent(brand: str, run_id: str, use_memory: bool = True, max_listings: int | None = None) -> dict:
    ctx = Ctx(brand, run_id, use_memory)
    stats: dict = {}
    try:
        ctx.log("start", f"Demarrage de l'agent pour {brand} (memoire {'ON' if use_memory else 'OFF'}"
                f"{f', max {max_listings} annonces' if max_listings else ''})")
        col = phase_collect(ctx, max_listings)
        ident = phase_identify(ctx, max_listings)
        added = phase_learn(ctx) if use_memory else 0
        if not use_memory:
            ctx.log("learn", "Memoire desactivee : etape d'apprentissage sautee")
        insights = phase_insights(ctx)
        stats = ctx.stats(listings_total=q("SELECT COUNT(*) AS n FROM listings WHERE brand=:b", b=brand)[0]["n"],
                          matched=ident["matched"], unknown=ident["unknown"], escalated=ident["escalated"],
                          identified_this_run=ident["listings"], errors=ident["errors"], memory_added=added,
                          collect_calls=col["calls"], collected_new=col["collected_new"], insights=insights)
        ctx.save_stats(stats)
        x("UPDATE runs SET status='done', finished_at=:t WHERE id=:i", t=datetime.now(timezone.utc).isoformat(), i=run_id)
        ctx.log("done", f"Termine en {stats['seconds']} s : {ident['matched']} identifiees, {ident['unknown']} inconnues, "
                f"{stats['input_tokens']} tokens entree / {stats['output_tokens']} sortie, cout estime {stats['cost_usd']} $")
    except Exception as e:  # noqa: BLE001
        ctx.log("error", f"Erreur : {type(e).__name__}: {str(e)[:300]}")
        stats = ctx.stats(error=f"{type(e).__name__}: {str(e)[:300]}")
        ctx.save_stats(stats)
        x("UPDATE runs SET status='error', finished_at=:t WHERE id=:i", t=datetime.now(timezone.utc).isoformat(), i=run_id)
    return stats
