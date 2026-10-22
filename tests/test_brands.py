from app import brands


def test_registrable_and_norm():
    assert brands.registrable("www.shop.cabaia.fr") == "cabaia.fr"
    assert brands.registrable("m.kbflifestyle.com.my") == "kbflifestyle.com.my"
    assert brands.norm("Cabaïa ") == "cabaia"
    assert brands.norm("Total-Energies") == "totalenergies"


def test_rank_candidates_prefers_exact_domain_and_drops_marketplaces():
    res = [{"url": "https://www.zalando.fr/cabaia/", "title": "Cabaia Zalando"},
           {"url": "https://www.instagram.com/cabaia", "title": "Cabaia"},
           {"url": "https://kbfpromo.com/x", "title": "Cabaia promo"},
           {"url": "https://cabaia.fr/pages/a", "title": "Les engagements | Cabaia"},
           {"url": "https://cabaia.fr/", "title": "CABAIA Boutique officielle"}]
    c = brands.rank_candidates(res, "Cabaïa")
    assert c[0]["domain"] == "cabaia.fr" and c[0]["exact"]
    assert {x["domain"] for x in c} == {"cabaia.fr", "kbfpromo.com"}
    assert [x["domain"] for x in brands.pick_candidates(c)] == ["cabaia.fr"]


def test_analyze_html_platform_and_signals():
    shop = ('<script src="https://cdn.shopify.com/x.js"></script><button>Ajouter au panier</button>'
            '<script type="application/ld+json">{"@type": "Product"}</script>')
    a = brands.analyze_html(shop)
    assert a["platform"] == "Shopify" and "add-to-cart button" in a["signals"] and "product structured data" in a["signals"]
    corp = "<html><body>Our company history and values. Images: image/png</body></html>"
    assert brands.analyze_html(corp) == {"platform": None, "signals": []}
    assert brands.clean_title("Les engagements | Cabaia", "cabaia") == "Cabaia"
