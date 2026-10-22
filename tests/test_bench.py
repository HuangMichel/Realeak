from app import bench as B

PRODS = [{"product_type": t} for t in
         ["Sac", "Sac", "Sac à dos", "Sac à dos Voyager", "Housse ordinateur", "Wallet", "Poche", "Poche", "Sac crossbody"]]


def test_baseline_category_rules():
    rules = B.build_category_rules(PRODS, {"cabaia"})
    f = lambda t: B.baseline_category(t, rules)  # noqa: E731
    assert f("Sac à dos Cabaïa noir") == "Sac à dos"
    assert f("Cabaia sac a dos voyager") == "Sac à dos Voyager"
    assert f("Pochette cabaia") == "Poche"
    assert f("housse macbook 13 pouces") == "Housse ordinateur"
    assert f("Cabaia portefeuille") == "Wallet"
    assert f("Sac cabaia neuf") == "Sac"
    assert f("Mug licorne") == "Autre"


def test_compute_metrics_two_levels():
    m = B.compute_metrics({"a": "Sac", "b": "sac", "c": None}, {"a": "1", "b": "9", "c": None},
                          {"a": "Sac", "b": "Sac", "c": "Wallet"}, {"a": "1", "b": "2"}, 0.5, 10, 4)
    assert (m["n"], m["n_cat"], m["cat_acc"]) == (4, 3, 66.7)
    assert (m["n_exact"], m["exact_acc"], m["sec_per_listing"]) == (2, 50.0, 2.5)
    assert B.compute_metrics({}, {}, {}, {})["cat_acc"] == 0.0
