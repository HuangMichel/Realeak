"""Tests purs (sans reseau, sans base) du coeur agent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent  # noqa: E402
from app.agent import (FAST, SMART, Index, aggregate_products, baseline_match, derive_memory,  # noqa: E402
                       estimate_cost, model_key, norm, shortlist, tokens)

BW = {"cabaia"}

PRODUCTS = [
    {"id": "1", "title": "Salagou  - Bottle -  500ml", "product_type": "Bottle"},
    {"id": "2", "title": "Sac à dos Adventurer Small Reykjavik", "product_type": "Sac"},
    {"id": "3", "title": "Sac à dos Adventurer Medium Surabaya", "product_type": "Sac"},
    {"id": "4", "title": "Pochette Téléphone Cody", "product_type": "Pochette téléphone"},
    {"id": "5", "title": "Portefeuille Small Surabaya", "product_type": "Wallet"},
    {"id": "6", "title": "Housse ordinateur Rio 13 pouces", "product_type": "Housse ordinateur"},
]


def test_norm_and_tokens():
    assert norm("Sac à dos Cabaïa Adventurer !") == "sac a dos cabaia adventurer"
    t = tokens("Sac à dos Cabaïa Adventurer dégradé fuchsia/rose taille Small", BW)
    assert "cabaia" not in t and "taille" not in t and "adventurer" in t and "dos" in t
    assert "fuchsia" not in tokens("dégradé fuchsia", BW, drop_colors=True)
    assert tokens("chaussettes", BW) == ["chaussette"]  # pluriel ramene au singulier


def test_model_key():
    assert model_key("Salagou  - Bottle -  500ml", "Bottle") == "salagou"
    assert model_key("Pochette Téléphone Cody", "Pochette téléphone") == "pochette telephone"
    assert model_key("", "") == ""


def test_shortlist_ranks_right_product_first():
    idx = Index(PRODUCTS, BW)
    top = shortlist(idx, "Sac à dos Cabaïa Adventurer dégradé fuchsia taille Small", None, k=3)
    assert top[0][1]["id"] == "2"  # "small" + "adventurer" + "dos"
    top = shortlist(idx, "Cabaia housse ordinateur 13 ou 14 pouces", None, k=2)
    assert top[0][1]["id"] == "6"
    top = shortlist(idx, "Cabaia gourde 500ml", None, k=1)  # synonyme gourde -> bottle
    assert top[0][1]["id"] == "1"


def test_shortlist_synonym_foreign_language():
    idx = Index(PRODUCTS, BW)
    top = shortlist(idx, "Cabaia Rucksack klein", None, k=3)
    assert {p["id"] for _, p in top[:2]} == {"2", "3"}


def test_memory_alias_boost_and_exemplars():
    idx = Index(PRODUCTS, BW)
    evidence = [
        {"listing_id": "a", "title": "Cabaia Mirror miroir poche dore", "image_url": "u1", "product_id": "5"},
        {"listing_id": "b", "title": "miroir Cabaia noir", "image_url": "u2", "product_id": "5"},
        {"listing_id": "c", "title": "Cabaia pochette JO noire et doree", "image_url": "u3", "product_id": "4"},
    ]
    mem = derive_memory(evidence, {p["id"]: p for p in PRODUCTS}, BW)
    assert "miroir" in mem.aliases  # recurrent 2 fois pour le meme produit
    assert "jo" not in mem.aliases and "noire" not in mem.aliases  # 1 occurrence / couleur
    base = shortlist(idx, "Cabaia miroir de poche", None, k=6)
    boosted = shortlist(idx, "Cabaia miroir de poche", mem, k=6)
    score = lambda lst, pid: next(s for s, p in lst if p["id"] == pid)  # noqa: E731
    assert score(boosted, "5") > score(base, "5")
    assert mem.exemplars_for([("A", "5"), ("B", "4")], skip_listing_ids={"a"}, n=2)[0][1]["listing_id"] == "b"
    assert mem.exemplars_for([("A", "5")], skip_listing_ids={"a", "b"}, n=2) == []


def test_baseline_keyword_matcher():
    # nom de modele (avant ' - ') present dans le titre -> produit ; sinon None
    assert baseline_match("Gourde Salagou Cabaia neuve", PRODUCTS, BW) == "1"
    assert baseline_match("Cabaia miroir", PRODUCTS, BW) is None
    # sans ' - ' le « modele » est le type Shopify : 'Sac' apparait, plus long gagne, depart par recouvrement
    assert baseline_match("Sac Cabaia adventurer small", PRODUCTS, BW) == "2"
    assert baseline_match("", PRODUCTS, BW) is None


def test_cost_estimate(monkeypatch):
    monkeypatch.setenv("OPENAI_PRICE_FAST_IN", "0.1")
    monkeypatch.setenv("OPENAI_PRICE_FAST_OUT", "0.5")
    monkeypatch.setenv("OPENAI_PRICE_SMART_IN", "2")
    monkeypatch.setenv("OPENAI_PRICE_SMART_OUT", "10")
    assert estimate_cost(FAST, 1_000_000, 1_000_000) == 0.10 + 0.50
    assert abs(estimate_cost(SMART, 3000, 500) - (3000 * 2 + 500 * 10) / 1e6) < 1e-12
    assert estimate_cost(FAST, 0, 0) == 0
    monkeypatch.setattr(agent, "_env", lambda n, d="": d)
    assert estimate_cost(FAST, 10**6, 10**6) == 0  # prix absents -> 0

def test_aggregate_products():
    rows = [
        {"product_id": "2", "title": "T", "retail": 80, "condition_norm": "new_with_tags", "price_eur": 40},
        {"product_id": "2", "title": "T", "retail": 80, "condition_norm": "good", "price_eur": 20},
        {"product_id": "3", "title": "U", "retail": None, "condition_norm": "new_without_tags", "price_eur": None},
    ]
    a = aggregate_products(rows)
    assert a[0]["product_id"] == "2" and a[0]["n"] == 2
    assert a[0]["pct_new_with_tags"] == 50 and a[0]["resale_vs_retail_pct"] == round(100 * 30 / 80)
    assert a[1]["avg_resale_eur"] is None and a[1]["pct_new_without_tags"] == 100


def test_sized_and_media_type():
    assert agent.sized("https://cdn.shopify.com/x.webp?v=1") .endswith("width=400")
    assert "width" not in agent.sized("https://images1.vinted.net/t/01/x.webp")
    assert agent._media_type(b"\xff\xd8\xff\xe0abc") == "image/jpeg"
    assert agent._media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert agent._media_type(b"<html>") is None


def test_metrics_in_bench():
    from app.bench import compute_metrics
    gold_pid = {"l1": "2", "l3": "5", "l4": "3"}
    gold_cat = {"l1": "Pochette", "l2": "Sac", "l3": "Pochette", "l4": "Pochette"}
    pred_pid = {"l1": "2", "l2": None, "l3": "6", "l4": "2"}
    pred_cat = {"l1": "Pochette", "l2": "Sac", "l3": "Pochette", "l4": "Wallet"}
    m = compute_metrics(pred_cat, pred_pid, gold_cat, gold_pid, cost=0.4, seconds=8.0, n_all=4)
    assert m["exact_acc"] == 33.3 and m["n_exact"] == 3 and m["cat_acc"] == 75.0 and m["n_cat"] == 4
    assert m["cost_usd"] == 0.4 and m["sec_per_listing"] == 2.0


def test_identify_cascade_paging_and_escalation(monkeypatch):
    """Client factice : page 1 = aucun candidat, page 2 = candidat peu sur -> arbitrage -> reponse finale."""
    import json
    from types import SimpleNamespace as NS
    monkeypatch.setenv("OPENAI_MODEL_FAST", "m-fast")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "m-smart")
    monkeypatch.setenv("OPENAI_API_SMART", "chat")
    prods = [{"id": str(i), "title": f"Pochette Print{i}", "product_type": "Pochette", "price_eur": 30,
              "image_url": None, "images": "[]"} for i in range(70)]
    idx = Index(prods, BW)
    calls = []

    class Completions:
        def create(self, **kw):
            name = kw["tools"][0]["function"]["name"]
            assert kw["tool_choice"]["function"]["name"] == name and "temperature" not in kw
            calls.append((kw["model"], name))
            if name == "describe_listing":
                inp = {"product_type": "Pochette", "keywords": ["print"]}
            elif kw["model"] == "m-fast":
                n_fast = sum(1 for m, t in calls if m == "m-fast" and t == "submit_match")
                inp = {"candidate": "none", "confidence": 0.9, "reason": "x"} if n_fast == 1 else \
                      {"candidate": "3", "confidence": 0.6, "plausible": ["4"], "reason": "doute"}
            else:
                inp = {"candidate": "1", "confidence": 0.9, "reason": "sur"}
            tc = NS(id="i", function=NS(name=name, arguments=json.dumps(inp)))
            return NS(choices=[NS(message=NS(content=None, tool_calls=[tc]), finish_reason="stop")],
                      usage=NS(prompt_tokens=100, completion_tokens=10))

    monkeypatch.setattr(agent, "get_client", lambda: NS(chat=NS(completions=Completions())))
    monkeypatch.setattr(agent, "image_block", lambda url, cache=True, detail="low": None)
    ctx = agent.Ctx("cabaia", None, use_memory=False)
    res = agent.identify_one(ctx, {"id": "L1", "title": "Pochette cabaia", "image_url": None}, idx, None)
    assert res["escalated"] and res["steps"] == 4 and res["confidence"] == 0.9 and res["product_id"] is not None
    assert [c[0] for c in calls] == ["m-fast", "m-fast", "m-fast", "m-smart"]
    t = ctx.usage.totals()
    assert t["llm_calls"] == 4 and t["input_tokens"] == 400 and t["output_tokens"] == 40
