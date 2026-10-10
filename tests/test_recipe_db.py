import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from fake_sheets import sample_spreadsheet  # noqa: E402
from recipe_db import Ingredient, RecipeDB, RecipeError, parse_qty  # noqa: E402


def db_for(ss):
    return RecipeDB(ss)


# ── lezen ─────────────────────────────────────────────────────────────────────
def test_search_and_get():
    ss = sample_spreadsheet()
    db = db_for(ss)
    assert [r["Title"] for r in db.search_recipes("soep")] == ["Linzensoep"]
    assert [r["Title"] for r in db.search_recipes(article="uien")] == ["Linzensoep"]
    r = db.get_recipe(1)
    assert r["Title"] == "Pasta pesto"
    assert [l["LineId"] for l in r["Ingredients"]] == [1, 2, 3]
    assert r["Ingredients"][1]["Article"] == "potje pesto"       # SINGLE
    assert r["Ingredients"][2]["Remark"] == "geroosterd"           # kolom 'Remark'
    with pytest.raises(RecipeError):
        db.get_recipe(99)


def test_parse_qty():
    assert parse_qty("1,5") == 1.5
    assert parse_qty("2") == 2
    for bad in ("0", "-1", "abc", "0.25"):
        with pytest.raises(RecipeError):
            parse_qty(bad)


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
    # voorstel schrijft niets
    assert ss.snapshot() == before
    s = plan.summary
    assert s["RecipeId"] == 3
    assert s["Nieuwe artikelen"] == ["olijfolie / olijfolie", "peterselie / peterselie"]
    assert s["Nieuwe eenheden"] == ["bosje"]
    assert s["Ingrediënten"][0] == "2 stuk uien (gesnipperd)"

    db = db_for(ss)
    plan2 = new_plan(db)
    assert plan2.fingerprint() == plan.fingerprint()   # deterministisch
    db.apply(plan2)

    main = ss.worksheet("RecipeMain").get_all_records()
    assert main[-1] == {"Key": 3, "RecipeId": 3, "Title": "Uiensoep"}
    hdr = ss.worksheet("RecipeHeader").get_all_records()[-1]
    assert hdr["RecipeId"] == 3 and hdr["Persons"] == "4" and hdr["Key"] == 2

    lines = [l for l in ss.worksheet("Lines").get_all_records() if l["RecipeId"] == 3]
    assert [l["LineId"] for l in lines] == [1, 2, 3]
    assert [l["Key"] for l in lines] == [6, 7, 8]
    assert lines[0]["ArticleId"] == 4 and lines[0]["SinglePlural"] == "PLURAL" and lines[0]["Remark"] == "gesnipperd"
    assert lines[1]["UomId"] == 5004 and lines[1]["SinglePlural"] == "SINGLE"
    assert lines[2]["Qty"] == 0.5

    arts = ss.worksheet("Articles").get_all_records()
    assert arts[-2]["ArticleId"] == 6 and arts[-1]["ArticleId"] == 7 and arts[-1]["Key"] == 8
    uoms = ss.worksheet("Uom").get_all_records()
    assert uoms[-1] == {"Key": 6, "UomId": 5007, "UomName": "bosje"}

    # het recept is direct weer op te halen
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
    ss.worksheet("RecipeMain").append_row([3, 3, "Iets anders"])
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
    assert ss.worksheet("RecipeMain").get_all_records()[0]["Title"] == "Pasta met pesto"
    hdr = ss.worksheet("RecipeHeader").get_all_records()[0]
    assert hdr["CookingTime"] == "25 min" and hdr["Link"] == "https://example.com/pesto"
    assert ss.snapshot()["Lines"] == lines_before


def test_update_ingredients_shrink_and_grow():
    ss = sample_spreadsheet()
    db = db_for(ss)
    # recept 1 had 3 regels (rij 2, 3 en 6); nu 2 regels
    plan = db.plan_update_recipe(1, None, {}, [
        Ingredient(qty=250, uom="g", article="pasta"),
        Ingredient(qty=1, uom="stuk", article="potje pesto", remark="groen"),
    ])
    assert plan.line_deletes == [6]
    db.apply(plan)
    lines = ss.worksheet("Lines").get_all_records()
    r1 = [l for l in lines if l["RecipeId"] == 1]
    assert [(l["LineId"], l["Qty"], l["Remark"]) for l in r1] == [(1, 250, ""), (2, 1, "groen")]
    assert [l["Key"] for l in r1] == [1, 2]                      # bestaande keys behouden
    assert len([l for l in lines if l["RecipeId"] == 2]) == 2      # ander recept onaangeroerd

    # groeien: 4 regels
    db = db_for(ss)
    plan = db.plan_update_recipe(1, None, {}, [
        Ingredient(qty=250, uom="g", article="pasta"),
        Ingredient(qty=1, uom="stuk", article="potje pesto", remark="groen"),
        Ingredient(qty=2, uom="el", article="pijnboompitten"),
        Ingredient(qty=1, uom="teentje", article="knoflook", article_plural="knoflook"),
    ])
    db.apply(plan)
    r1 = [l for l in ss.worksheet("Lines").get_all_records() if l["RecipeId"] == 1]
    assert [l["LineId"] for l in r1] == [1, 2, 3, 4]
    assert len({l["Key"] for l in ss.worksheet("Lines").get_all_records()}) == len(ss.worksheet("Lines").rows) - 1


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
    assert hdr and hdr[0]["Persons"] == "6" and hdr[0]["Key"] == 2


def test_writes_are_batched():
    ss = sample_spreadsheet()
    db = db_for(ss)
    db.apply(new_plan(db))
    calls = ss.worksheet("Lines").calls
    assert calls == [("append_rows", 3)]
