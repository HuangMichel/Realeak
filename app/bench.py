"""Benchmark avant/apres : python -m app.bench --brand cabaia [--limit N] [--self-learning]

Verite terrain = table labels (etiquetes humaines), evaluee a DEUX niveaux :
  - CATEGORIE (product_type du catalogue) : sur toutes les etiquettes qui ont une categorie ;
  - REFERENCE EXACTE (id produit) : seulement sur les etiquettes qui ont un vrai product_id.
Methodes comparees :
  1. workflow sans IA (regles de mots-cles)   2. agent sans memoire
  3. agent + memoire (etiquettes des AUTRES annonces + run 1, leave-one-out : aucune fuite)
  4. (option) agent + auto-apprentissage seul, sans etiquettes
"""
import argparse
import csv
import math
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

from app import agent as A
from app.db import ROOT, init_db, q, x

PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing (tarif standard, contexte <= 272k tokens, releve le 2026-10-22)"

# Synonymes titre Vinted -> mots du nom de type du catalogue (en plus de agent.SYN, deja applique via expand=True).
SYN_CAT = {
    "pochette": "poche", "poches": "poche", "pocket": "poche", "pouch": "poche",
    "portefeuille": "wallet", "cabas": "tote bag", "tote": "tote bag", "totebag": "tote bag",
    "trousse": "trousse voyage", "kit": "trousse voyage", "toilette": "trousse voyage",
    "banane": "sac banane", "belt": "sac banane", "fanny": "sac banane",
    "crossbody": "sac crossbody", "bandouliere": "bandouliere", "besace": "sac crossbody",
    "valise": "valise cabine", "trolley": "valise cabine", "suitcase": "valise cabine",
    "weekender": "sac voyage", "duffle": "sac voyage", "duffel": "sac voyage",
    "backpack": "sac dos", "rucksack": "sac dos", "gourde": "gourde", "bottle": "gourde",
    "chaussettes": "chaussette", "socks": "chaussette", "porte cles": "keychain", "keychain": "keychain",
    "lunch": "lunch bag", "cartable": "cartable", "ecole": "trousse ecole", "bonnet": "bonnet",
    "ordinateur": "housse ordinateur", "laptop": "housse ordinateur", "housse": "housse ordinateur",
    "parapluie": "parapluie", "umbrella": "parapluie", "echarpe": "echarpe", "gants": "gant", "charm": "charms",
}


def build_category_rules(products, brand_words=()):
    """Regles automatiques : pour chaque product_type, les mots du nom (hors 'sac' s'il y en a d'autres) doivent tous
    apparaitre dans le titre ; on garde le type le plus specifique (somme des IDF des mots du nom de type)."""
    types = Counter(p["product_type"] for p in products if p.get("product_type"))
    toks = {t: set(A.tokens(t, brand_words)) for t in types}
    required = {t: (tk - {"sac"} or tk) for t, tk in toks.items() if tk}
    df = Counter(w for tk in toks.values() for w in tk)   # IDF sur les noms complets (sinon 'sac' paraitrait rare)
    n = max(len(required), 1)
    idf = {w: math.log(1 + n / df[w]) for tk in required.values() for w in tk}
    return {"types": types, "required": required, "idf": idf, "bw": set(brand_words)}


def baseline_category(title, rules):
    """Categorie par mots-cles ; 'Autre' si aucun type du catalogue n'est reconnu."""
    ln = A.norm(title)
    ltoks = set(A.tokens(title, rules["bw"], expand=True))
    for w in list(ltoks) + ln.split():
        extra = SYN_CAT.get(w)
        if extra:
            ltoks |= set(A.tokens(extra, rules["bw"]))
    best, best_key = "Autre", None
    for t, req in rules["required"].items():
        if not req <= ltoks:
            continue
        phrase = 0.5 if A.norm(t) in ln else 0.0
        key = (sum(rules["idf"][w] for w in req) + phrase, rules["types"][t], t)
        if best_key is None or key > best_key:
            best, best_key = t, key
    return best


def compute_metrics(pred_cat: dict, pred_pid: dict, gold_cat: dict, gold_pid: dict, cost=0.0, seconds=0.0, n_all=0) -> dict:
    """pred_cat : lid -> categorie | None ; pred_pid : lid -> product_id | None ;
    gold_cat : lid -> categorie (seulement les etiquettes qui en ont une) ; gold_pid : lid -> vrai product_id (idem)."""
    cat_ok = sum(1 for lid, g in gold_cat.items() if A.norm(pred_cat.get(lid)) == A.norm(g))
    ex_ok = sum(1 for lid, g in gold_pid.items() if pred_pid.get(lid) == g)
    pct = lambda v, n: round(100 * v / n, 1) if n else 0.0  # noqa: E731
    return {"n": n_all, "n_cat": len(gold_cat), "cat_acc": pct(cat_ok, len(gold_cat)),
            "n_exact": len(gold_pid), "exact_acc": pct(ex_ok, len(gold_pid)),
            "cost_usd": round(cost, 4), "sec_per_listing": round(seconds / n_all, 2) if n_all else 0.0}


def create_run(brand, kind, rid):
    x("INSERT INTO runs(id, brand, kind, started_at, status, stats, log) VALUES(:i, :b, :k, :t, 'running', '{}', '[]')",
      i=rid, b=brand, k=kind, t=datetime.now(timezone.utc).isoformat())


def finish_run(ctx, status="done", **extra):
    ctx.save_stats(ctx.stats(**extra))
    x("UPDATE runs SET status=:s, finished_at=:t WHERE id=:i", s=status, t=datetime.now(timezone.utc).isoformat(), i=ctx.run_id)


def _ev(l, pid):
    return {"listing_id": l["id"], "title": l["title"], "image_url": l.get("image_url"), "product_id": pid}


def run_agent_eval(brand, label, use_memory, labeled, idx, mem_for, tag):
    rid = f"bench-{tag}-{uuid.uuid4().hex[:6]}"
    create_run(brand, "bench", rid)
    ctx = A.Ctx(brand, rid, use_memory)
    ctx.log("bench", f"{label} : {len(labeled)} annonces etiquetees")
    t0 = time.time()
    done = [0]

    def prog(r):
        done[0] += 1
        if done[0] % 10 == 0:
            ctx.log("bench", f"{done[0]}/{len(labeled)}")

    try:
        res = A.identify_batch(ctx, labeled, idx, mem_for, workers=6, on_result=prog)
    except Exception as e:  # noqa: BLE001
        ctx.log("error", f"{type(e).__name__}: {e}")
        finish_run(ctx, "error", error=str(e)[:300])
        raise
    secs = time.time() - t0
    errors = sum(1 for r in res if r.get("error"))
    esc = sum(1 for r in res if r.get("escalated"))
    finish_run(ctx, listings_total=len(labeled), matched=sum(1 for r in res if r.get("product_id")), escalated=esc, errors=errors)
    return res, ctx.usage.totals(), secs, esc, errors


def preds(res):
    return ({r["listing_id"]: r.get("category") for r in res}, {r["listing_id"]: r.get("product_id") for r in res})


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="cabaia")
    ap.add_argument("--limit", type=int, default=None, help="nombre max d'annonces etiquetees evaluees")
    ap.add_argument("--self-learning", action="store_true", help="ajoute la methode auto-apprentissage sans etiquettes")
    a = ap.parse_args()
    init_db()
    brand = a.brand

    products = q("SELECT * FROM products WHERE brand=:b", b=brand)
    labeled = q("""SELECT l.*, lb.product_id AS gold, lb.category AS gold_category FROM labels lb
                   JOIN listings l ON l.id=lb.listing_id WHERE l.brand=:b ORDER BY l.id""", b=brand)
    if a.limit:
        labeled = labeled[:a.limit]
    if not products or not labeled:
        print(f"Rien a evaluer : {len(products)} produits, {len(labeled)} annonces etiquetees pour {brand}.")
        sys.exit(2)
    pbi = {p["id"]: p for p in products}
    gold_pid = {l["id"]: l["gold"] for l in labeled if l["gold"] and l["gold"] != "NONE" and l["gold"] in pbi}
    gold_cat = {}
    for l in labeled:
        c = l.get("gold_category") or (pbi[l["gold"]].get("product_type") if l["id"] in gold_pid else None)
        if c:
            gold_cat[l["id"]] = c
    bw = A.brand_words_for(brand)
    idx = A.Index(products, bw)
    print(f"{len(labeled)} annonces etiquetees ({len(gold_cat)} avec categorie, {len(gold_pid)} avec reference exacte), "
          f"{len(products)} produits")

    rows = []  # (methode, metrics, escalade %, erreurs, totaux tokens)
    n = len(labeled)

    # 1. baseline
    t0 = time.time()
    rules = build_category_rules(products, bw)
    pc_b = {l["id"]: baseline_category(l["title"], rules) for l in labeled}
    pp_b = {l["id"]: A.baseline_match(l["title"], products, bw) for l in labeled}
    rows.append(("Workflow sans IA (mots-cles)", compute_metrics(pc_b, pp_b, gold_cat, gold_pid, 0.0, time.time() - t0, n), 0.0, 0,
                 {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0}))

    # 2. agent sans memoire
    res1, c1, s1, esc1, err1 = run_agent_eval(brand, "Agent sans memoire", False, labeled, idx, lambda l: None, "nomem")
    pc1, pp1 = preds(res1)
    rows.append(("Agent sans memoire (run 1)", compute_metrics(pc1, pp1, gold_cat, gold_pid, c1["cost_usd"], s1, n),
                 round(100 * esc1 / n, 1), err1, c1))

    # 3. agent + memoire : etiquettes des AUTRES annonces + annonces confiantes du run 1 (labels override), leave-one-out
    ev = {}
    for l, r in zip(labeled, res1):
        if r.get("product_id") and r.get("confidence", 0) >= A.LEARN_MIN_CONF:
            ev[l["id"]] = _ev(l, r["product_id"])
    for l in labeled:
        if l["gold"] == "NONE":
            ev.pop(l["id"], None)
        elif l["id"] in gold_pid:
            ev[l["id"]] = _ev(l, gold_pid[l["id"]])

    def mem_loo(l):
        return A.derive_memory([e for k, e in ev.items() if k != l["id"]], pbi, bw)

    res2, c2, s2, esc2, err2 = run_agent_eval(brand, "Agent + memoire", True, labeled, idx, mem_loo, "mem")
    pc2, pp2 = preds(res2)
    rows.append(("Agent + memoire (etiquettes + run 1)", compute_metrics(pc2, pp2, gold_cat, gold_pid, c2["cost_usd"], s2, n),
                 round(100 * esc2 / n, 1), err2, c2))

    # 4. option : auto-apprentissage seul (aucune etiquette humaine)
    if a.self_learning:
        ev_self = {l["id"]: _ev(l, r["product_id"]) for l, r in zip(labeled, res1)
                   if r.get("product_id") and r.get("confidence", 0) >= A.LEARN_MIN_CONF}
        res3, c3, s3, esc3, err3 = run_agent_eval(
            brand, "Agent + auto-apprentissage", True, labeled, idx,
            lambda l: A.derive_memory([e for k, e in ev_self.items() if k != l["id"]], pbi, bw), "self")
        pc3, pp3 = preds(res3)
        rows.append(("Agent + auto-apprentissage (sans etiquettes)",
                     compute_metrics(pc3, pp3, gold_cat, gold_pid, c3["cost_usd"], s3, n), round(100 * esc3 / n, 1), err3, c3))

    write_outputs(brand, rows, n, len(ev))


def _tpl(r):
    return round((r[4]["input_tokens"] + r[4]["output_tokens"]) / r[1]["n"]) if r[1]["n"] else 0


def write_outputs(brand, rows, n, n_ev):
    out = ROOT / "bench"
    out.mkdir(exist_ok=True)
    head = ["methode", "n_annonces", "n_categorie", "exactitude_categorie_pct", "n_reference", "exactitude_reference_pct",
            "cout_usd", "cout_par_annonce_usd", "secondes_par_annonce", "escalade_pct", "erreurs", "tokens_entree",
            "tokens_sortie", "appels_llm", "tokens_par_annonce"]
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(head)
        for r in rows:
            name, m, esc, err, tk = r
            w.writerow([name, m["n"], m["n_cat"], m["cat_acc"], m["n_exact"], m["exact_acc"], m["cost_usd"],
                        round(m["cost_usd"] / m["n"], 5) if m["n"] else 0, m["sec_per_listing"], esc, err,
                        tk["input_tokens"], tk["output_tokens"], tk["llm_calls"], _tpl(r)])

    base, a1, a2 = rows[0][1], rows[1][1], rows[2][1]
    cpl = lambda m: m["cost_usd"] / m["n"] if m["n"] else 0  # noqa: E731
    dc, de = round(a2["cat_acc"] - a1["cat_acc"], 1), round(a2["exact_acc"] - a1["exact_acc"], 1)
    lines = [
        f"- Categorie : les mots-cles trouvent {base['cat_acc']} % des categories, l'agent sans memoire {a1['cat_acc']} % "
        f"({round(a1['cat_acc'] - base['cat_acc'], 1):+} pts) pour {cpl(a1):.4f} $ et {_tpl(rows[1])} tokens par annonce.",
        f"- Reference exacte : {base['exact_acc']} % pour les mots-cles contre {a1['exact_acc']} % pour l'agent sans memoire "
        f"({round(a1['exact_acc'] - base['exact_acc'], 1):+} pts) ; beaucoup de produits Cabaia sont quasi identiques "
        f"(memes imprimes, pieces detachees), d'ou l'ecart avec le niveau categorie.",
        f"- Memoire : categorie {a1['cat_acc']} % -> {a2['cat_acc']} % ({dc:+} pts), reference exacte {a1['exact_acc']} % -> "
        f"{a2['exact_acc']} % ({de:+} pts) ; {_tpl(rows[2])} tokens par annonce contre {_tpl(rows[1])} sans memoire "
        f"(cout {cpl(a2):.4f} $ contre {cpl(a1):.4f} $).",
        f"- Prudence : {n} annonces etiquetees dont {a1['n_cat']} avec categorie et {a1['n_exact']} avec reference exacte "
        f"(petits echantillons) ; evaluation « leave-one-out » (l'annonce testee et sa propre etiquette sont exclues de la "
        f"memoire) ; {n_ev} cas confirmes alimentent la memoire ; temps mesures avec 6 appels en parallele.",
    ]
    md = [f"# Benchmark Resale Radar - {brand}", "",
          f"Evalue le {datetime.now().strftime('%Y-%m-%d %H:%M')} sur {n} annonces etiquetees par l'equipe "
          "(verite terrain = table `labels`), a deux niveaux : categorie et reference exacte.", "",
          "| Methode | Exactitude categorie | Exactitude reference exacte | Cout total | Tokens / annonce | Sec. / annonce | Escalade modele expert |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        name, m, esc, err, tk = r
        md.append(f"| {name} | {m['cat_acc']} % (n={m['n_cat']}) | {m['exact_acc']} % (n={m['n_exact']}) | "
                  f"{m['cost_usd']:.4f} $ | {_tpl(r)} | {m['sec_per_listing']} s | {esc} % |")
    pf, po = A._price(A.FAST), A._price(A.SMART)
    md += ["", "Categorie = product_type du catalogue (famille de produit), evaluee sur toutes les etiquettes qui ont une "
           "categorie. Reference exacte = id produit, evaluee uniquement sur les etiquettes qui ont un vrai produit "
           "(les etiquettes « categorie seule » et « hors catalogue » en sont exclues). Cout = tokens x prix du .env.", "",
           f"Prix utilises (USD par million de tokens, entree / sortie) : modele rapide {pf[0]} / {pf[1]} ; "
           f"modele expert {po[0]} / {po[1]}. Source : {PRICE_SOURCE}.", "",
           "## Lecture", ""] + lines + [""]
    (out / "results.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
