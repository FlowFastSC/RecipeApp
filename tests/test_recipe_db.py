import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from fake_sheets import sample_spreadsheet  # noqa: E402
from recipe_db import Ingredient, RecipeDB, RecipeError, _shift_rows, parse_qty, single_plural_for  # noqa: E402


def db_for(ss):
    return RecipeDB(ss)


def lines_of(ss, recipe_id):
    return [l for l in ss.worksheet("Lines").get_all_records() if l["RecipeId"] == recipe_id]


# ── lezen ─────────────────────────────────────────────────────────────────────
def test_search_and_get():
    db = db_for(sample_spreadsheet())
    assert [r["Title"] for r in db.search_recipes("soep")] == ["Linzensoep"]
    assert [r["Title"] for r in db.search_recipes(article="uien")] == ["Linzensoep"]
    r = db.get_recipe(1)
    assert r["Title"] == "Pasta pesto"
    assert [l["LineId"] for l in r["Ingredients"]] == [1, 2, 3]
    assert r["Ingredients"][1]["Article"] == "potje pesto"       # SINGLE
    assert r["Ingredients"][1]["Uom"] == "stuk(s)"
    assert r["Ingredients"][2]["Remark"] == "geroosterd"           # kolom ArticleRemark
    with pytest.raises(RecipeError):
        db.get_recipe(99)


def test_parse_qty():
    assert parse_qty("1,5") == 1.5
    assert parse_qty("2") == 2
    for bad in ("0", "-1", "abc", "0.25"):
        with pytest.raises(RecipeError):
            parse_qty(bad)


def test_shift_rows():
    f = '=IF(H5="SINGLE",VLOOKUP(G5,Articles!B:C,2,FALSE),VLOOKUP(G5,Articles!B:D,3,FALSE))'
    assert _shift_rows(f, 5, 12) == f.replace("H5", "H12").replace("G5", "G12")
    assert _shift_rows("=VLOOKUP(G5,Articles!B:G,6,false)", 5, 7) == "=VLOOKUP(G7,Articles!B:G,6,false)"
    assert _shift_rows("=$A$5+A5+A50", 5, 9) == "=$A$5+A9+A50"


# ── eenheden en artikelen koppelen ────────────────────────────────────────────
def test_uom_matching_real_names():
    db = db_for(sample_spreadsheet())
    plan = db.plan_new_recipe("Test", {}, [
        Ingredient(qty=2, uom="stuks", article="uien"),
        Ingredient(qty=1, uom="stuk", article="ui"),
        Ingredient(qty=2, uom="tenen", article="ui"),
        Ingredient(qty=1, uom="", article="ui"),
        Ingredient(qty=1, uom="eetlepel", article="ui"),
        Ingredient(qty=1, uom="liter", article="ui"),
        Ingredient(qty=1, uom="stuk(s)", article="ui"),
    ])
    assert [l["UomId"] for l in plan.line_appends] == [5002, 5002, 5003, 5002, 5004, 5012, 5002]
    assert plan.new_uoms == []


def test_new_uom_id_ignores_9999():
    db = db_for(sample_spreadsheet())
    plan = db.plan_new_recipe("Test", {}, [Ingredient(qty=1, uom="bosje", article="ui")])
    assert plan.new_uoms == [{"Key": 9, "UomId": 5013, "UomName": "bosje"}]


def test_article_markers():
    db = db_for(sample_spreadsheet())
    # "rode linze" is samengevoegd: "replace by 3" → ArticleId 3
    plan = db.plan_new_recipe("Test", {}, [Ingredient(qty=100, uom="g", article="rode linzen")])
    assert plan.line_appends[0]["ArticleId"] == 3 and plan.new_articles == []
    # 99999 "aa-artikel ontbreekt" mag nooit als match dienen
    with pytest.raises(RecipeError, match="article_plural"):
        db.plan_new_recipe("Test", {}, [Ingredient(qty=1, uom="g", article="aa-artikel ontbreekt")])


# ── nieuw recept ──────────────────────────────────────────────────────────────
def new_plan(db, **kw):
    ingredients = kw.pop("ingredients", [
        Ingredient(qty=2, uom="stuks", article="uien", remark="gesnipperd"),
        Ingredient(qty=1, uom="eetlepel", article="olijfolie", article_plural="olijfolie"),
        Ingredient(qty="0,5", uom="bosje", article="peterselie", article_plural="peterselie"),
    ])
    return db.plan_new_recipe(kw.pop("title", "Uiensoep"), {"Persons": "4", "Content": "Bak de uien."}, ingredients)


def test_new_recipe_plan_and_apply():
    ss = sample_spreadsheet()
    before = ss.snapshot()
    plan = new_plan(db_for(ss))
    assert ss.snapshot() == before                    # voorstel schrijft niets
    s = plan.summary
    assert s["RecipeId"] == 3
    assert s["Nieuwe artikelen"] == ["olijfolie / olijfolie", "peterselie / peterselie"]
    assert s["Nieuwe eenheden"] == ["bosje"]
    assert s["Ingrediënten"][0] == "2 stuk(s) uien (gesnipperd)"

    db = db_for(ss)
    plan2 = new_plan(db)
    assert plan2.fingerprint() == plan.fingerprint()  # deterministisch
    db.apply(plan2)

    main = ss.worksheet("RecipeMain")
    assert main.get_all_records()[-1] == {"Key": 3, "RecipeId": 3, "Title": "Uiensoep"}
    assert main.raw(4, "RecipeId") == "3"            # als TEKST, net als de rest

    hdr = ss.worksheet("RecipeHeader").get_all_records()[-1]
    assert hdr["RecipeId"] == 3 and hdr["Persons"] == 4 and hdr["Key"] == 2

    lines = lines_of(ss, 3)
    assert [l["LineId"] for l in lines] == [1, 2, 3]
    assert [l["Key"] for l in lines] == [6, 7, 8]
    assert lines[0]["ArticleId"] == 4 and lines[0]["SinglePlural"] == "PLURAL"
    assert lines[0]["ArticleRemark"] == "gesnipperd"
    assert lines[1]["UomId"] == 5004 and lines[1]["SinglePlural"] == "SINGLE"
    assert lines[2]["Qty"] == 0.5

    ws = ss.worksheet("Lines")
    first_new = 7                                     # rij 1 kop + 5 data → rij 7
    assert ws.raw(first_new, "RecipeId") == "3" and ws.raw(first_new, "ArticleId") == "4"
    assert ws.raw(first_new, "UomId") == 5002          # UomId blijft een getal
    # formules overgenomen van de rij erboven, met aangepaste rijnummers
    assert ws.raw(first_new, "ArticlePreview").startswith('=IF(H7="SINGLE",VLOOKUP(G7,')
    assert ws.raw(first_new + 2, "Replace?") == "=VLOOKUP(G9,Articles!B:G,6,false)"

    arts = ss.worksheet("Articles")
    new_arts = arts.get_all_records()[-2:]
    assert [a["ArticleId"] for a in new_arts] == [100000, 100001] or [a["ArticleId"] for a in new_arts] == [7, 8]
    assert new_arts[0]["DefaultUomId"] == 5004        # eenheid van de regel
    assert arts.raw(9, "ArticleId") == "7" and arts.raw(9, "PreviewUom").startswith("=ARRAYFORMULA(VLOOKUP(E9,")
    uoms = ss.worksheet("Uom").get_all_records()
    assert uoms[-1] == {"Key": 9, "UomId": 5013, "UomName": "bosje"}

    r = db_for(ss).get_recipe(3)
    assert [l["Article"] for l in r["Ingredients"]] == ["uien", "olijfolie", "peterselie"]


def test_new_recipe_validation():
    db = db_for(sample_spreadsheet())
    with pytest.raises(RecipeError, match="bestaat al"):
        new_plan(db, title="pasta PESTO")
    with pytest.raises(RecipeError, match="article_plural"):
        new_plan(db, ingredients=[Ingredient(qty=1, uom="g", article="tofu")])
    with pytest.raises(RecipeError, match="20 tekens"):
        new_plan(db, ingredients=[Ingredient(qty=1, uom="g", article="ui", remark="x" * 21)])
    with pytest.raises(RecipeError, match="minimaal één"):
        new_plan(db, ingredients=[])


def test_fingerprint_changes_when_db_changes():
    ss = sample_spreadsheet()
    code = new_plan(db_for(ss)).fingerprint()
    ss.worksheet("RecipeMain").rows.append([3, "3", "Iets anders"])
    assert new_plan(db_for(ss)).fingerprint() != code


# ── wijzigen ──────────────────────────────────────────────────────────────────
def test_update_header_and_title_only():
    ss = sample_spreadsheet()
    lines_before = ss.snapshot()["Lines"]
    db = db_for(ss)
    plan = db.plan_update_recipe(1, "Pasta met pesto", {"CookingTime": "25 min", "Link": None}, None)
    w = plan.summary["wijzigingen"]
    assert w["Titel"] == "Pasta pesto → Pasta met pesto"
    assert w["CookingTime"] == "20 min → 25 min"
    assert "Link" not in w
    db.apply(plan)
    assert ss.worksheet("RecipeMain").raw(2, "Title") == "Pasta met pesto"
    assert ss.worksheet("RecipeMain").raw(2, "RecipeId") == "1"          # onaangeroerd
    hdr = ss.worksheet("RecipeHeader").get_all_records()[0]
    assert hdr["CookingTime"] == "25 min" and hdr["Link"] == "https://example.com/pesto"
    assert ss.snapshot()["Lines"] == lines_before


def test_update_ingredients_keeps_formulas():
    ss = sample_spreadsheet()
    ws = ss.worksheet("Lines")
    formula_before = ws.raw(2, "ArticlePreview")
    db = db_for(ss)
    # recept 1 had 3 regels (rij 2, 3 en 6); nu 2 regels
    plan = db.plan_update_recipe(1, None, {}, [
        Ingredient(qty=250, uom="g", article="pasta"),
        Ingredient(qty=1, uom="stuk", article="potje pesto", remark="groen"),
    ])
    assert plan.line_deletes == [6]
    db.apply(plan)
    r1 = lines_of(ss, 1)
    assert [(l["LineId"], l["Qty"], l["ArticleRemark"]) for l in r1] == [(1, 250, ""), (2, 1, "groen")]
    assert [l["Key"] for l in r1] == [1, 2]                      # bestaande keys behouden
    assert ws.raw(2, "ArticlePreview") == formula_before         # formule niet overschreven
    assert ws.raw(2, "RecipeId") == "1"
    assert len(lines_of(ss, 2)) == 2                             # ander recept onaangeroerd
    # alleen eigen kolommen geschreven, met RAW
    assert ("batch_update", 16, "RAW") in ws.calls

    # groeien: 4 regels
    db = db_for(ss)
    db.apply(db.plan_update_recipe(1, None, {}, [
        Ingredient(qty=250, uom="g", article="pasta"),
        Ingredient(qty=1, uom="stuk", article="potje pesto", remark="groen"),
        Ingredient(qty=2, uom="el", article="pijnboompitten"),
        Ingredient(qty=1, uom="teentje", article="knoflook", article_plural="knoflook"),
    ]))
    r1 = lines_of(ss, 1)
    assert [l["LineId"] for l in r1] == [1, 2, 3, 4]
    keys = [l["Key"] for l in ws.get_all_records()]
    assert len(keys) == len(set(keys))
    last = len(ws.rows)
    assert ws.raw(last, "UomPreview") == f"=ARRAYFORMULA(VLOOKUP(E{last},Uom!B:C,2,FALSE))"


def test_update_without_changes_is_error():
    db = db_for(sample_spreadsheet())
    current = db.get_recipe(2)
    same = [Ingredient(qty=l["Qty"], uom=l["Uom"], article=l["Article"], remark=l["Remark"])
            for l in current["Ingredients"]]
    with pytest.raises(RecipeError, match="Geen wijzigingen"):
        db.plan_update_recipe(2, "Linzensoep", {}, same)


def test_header_created_when_missing():
    ss = sample_spreadsheet()
    db = db_for(ss)
    db.apply(db.plan_update_recipe(2, None, {"Persons": "6"}, None))
    hdr = [h for h in ss.worksheet("RecipeHeader").get_all_records() if h["RecipeId"] == 2]
    assert hdr and hdr[0]["Persons"] == 6 and hdr[0]["Key"] == 2
    assert ss.worksheet("RecipeHeader").raw(3, "RecipeId") == "2"


def test_sheet_grows_when_full():
    ss = sample_spreadsheet()
    ws = ss.worksheet("Lines")
    ws.row_count = len(ws.rows)                       # raster precies vol
    db = db_for(ss)
    db.apply(new_plan(db))
    assert ("add_rows", 3) in ws.calls


@pytest.mark.parametrize("qty, uom, expected", [
    (1, "stuk(s)", "SINGLE"),
    (2, "stuk(s)", "PLURAL"),
    ("1,5", "stuks", "PLURAL"),
    (0.5, "stuk", "SINGLE"),
    (3, ".", "PLURAL"),
    (3, "", "PLURAL"),
    (250, "g", "SINGLE"),
    (3, "el", "SINGLE"),
    (2, "blik(ken)", "SINGLE"),
    (3, "teentje(s)", "SINGLE"),
])
def test_single_plural_follows_unit(qty, uom, expected):
    assert single_plural_for(qty, uom) == expected


def test_plan_derives_single_plural_from_unit():
    db = db_for(sample_spreadsheet())
    plan = db.plan_new_recipe("Roerbak", {}, [
        Ingredient(qty=250, uom="g", article="spitskool", article_plural="spitskolen"),
        Ingredient(qty=2, uom="stuk", article="ui"),
        Ingredient(qty=30, uom="g", article="pijnboompit"),
    ])
    assert plan.summary["Ingrediënten"] == [
        "250 g spitskool  [NIEUW ARTIKEL]",
        "2 stuk(s) uien",
        "30 g pijnboompit",
    ]
