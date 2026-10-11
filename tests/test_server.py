"""End-to-end: MCP over HTTP en Flask-routes via asgi.app, met een nep-spreadsheet."""
import importlib
import json
import os
import sys

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from fake_sheets import sample_spreadsheet  # noqa: E402

TOKEN = "test-token-0123456789abcdef"


class FakeClient:
    def __init__(self, ss):
        self.ss = ss

    def open(self, _name):
        return self.ss


@pytest.fixture
def env(monkeypatch):
    ss = sample_spreadsheet()
    monkeypatch.setenv("MCP_TOKEN", TOKEN)
    monkeypatch.setenv("SECRET_KEY", "test")
    monkeypatch.setenv("APP_PASSWORD", "geheim")
    import sheets
    monkeypatch.setattr(sheets, "get_client", lambda: FakeClient(ss))
    for mod in ("app", "asgi"):
        sys.modules.pop(mod, None)
    asgi = importlib.import_module("asgi")
    monkeypatch.setattr(asgi, "get_client", lambda: FakeClient(ss))
    monkeypatch.setattr(sys.modules["app"], "get_client", lambda: FakeClient(ss))
    with TestClient(asgi.app) as client:
        yield client, ss


HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(client, method, params=None, id_=1, path=f"/mcp/{TOKEN}"):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    r = client.post(path, headers=HEADERS, content=json.dumps(body))
    return r


def call(client, tool, args):
    r = rpc(client, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert not res.get("isError"), res
    return res.get("structuredContent") or json.loads(res["content"][0]["text"])


def init(client):
    r = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}})
    assert r.status_code == 200, r.text
    return r.json()


def test_mcp_requires_token(env):
    client, _ = env
    assert rpc(client, "tools/list", path="/mcp").status_code == 404
    assert rpc(client, "tools/list", path="/mcp/verkeerd-token-xxxxxxxxxxxx").status_code == 404


def test_mcp_tools_and_flow(env):
    client, ss = env
    info = init(client)
    assert "Recepten" in json.dumps(info)
    names = {t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert names == {"zoek_recepten", "recept_ophalen", "zoek_artikelen", "lijst_eenheden",
                     "recept_toevoegen", "recept_wijzigen"}

    assert call(client, "zoek_recepten", {"zoekterm": "pesto"})["recepten"][0]["RecipeId"] == 1

    args = {"titel": "Uiensoep", "personen": "4", "ingredienten": [
        {"qty": 4, "uom": "stuk", "article": "uien", "remark": "in ringen"},
        {"qty": "1,5", "uom": "el", "article": "boter", "article_plural": "boter"},
    ]}
    voorstel = call(client, "recept_toevoegen", args)
    assert voorstel["status"] == "voorstel" and voorstel["opgeslagen"] is False
    assert len(ss.worksheet("RecipeMain").rows) == 3          # niets geschreven

    fout = call(client, "recept_toevoegen", {**args, "bevestigingscode": "verkeerd"})
    assert fout["status"] == "voorstel_gewijzigd"
    assert len(ss.worksheet("RecipeMain").rows) == 3

    ok = call(client, "recept_toevoegen", {**args, "bevestigingscode": voorstel["bevestigingscode"]})
    assert ok["status"] == "opgeslagen" and ok["RecipeId"] == 3
    r = call(client, "recept_ophalen", {"recipe_id": 3})
    assert [i["Article"] for i in r["Ingredients"]] == ["uien", "boter"]

    # wijzigen
    w = call(client, "recept_wijzigen", {"recipe_id": 3, "bereidingstijd": "45 min"})
    assert w["voorstel"]["wijzigingen"] == {"CookingTime": "(leeg) → 45 min"}
    call(client, "recept_wijzigen", {"recipe_id": 3, "bereidingstijd": "45 min",
                                     "bevestigingscode": w["bevestigingscode"]})
    assert call(client, "recept_ophalen", {"recipe_id": 3})["CookingTime"] == "45 min"


def test_mcp_error_is_reported(env):
    client, _ = env
    init(client)
    r = rpc(client, "tools/call", {"name": "recept_toevoegen", "arguments": {
        "titel": "Linzensoep", "ingredienten": [{"qty": 1, "uom": "g", "article": "ui"}]}})
    res = r.json()["result"]
    assert res["isError"] and "bestaat al" in res["content"][0]["text"]


def test_flask_pages(env):
    client, ss = env
    assert client.get("/").status_code == 401
    assert client.get("/health").status_code == 200
    auth = ("recepten", "geheim")
    assert client.get("/", auth=auth).status_code == 200
    page = client.get("/select_recipe/1", auth=auth)
    assert page.status_code == 200 and "Pasta pesto" in page.text and "geroosterd" in page.text
    page = client.get("/select_recipe/2", auth=auth)                 # recept zonder header
    assert page.status_code == 200 and "Linzensoep" in page.text
    assert client.get("/edit_recipe/1", auth=auth).status_code == 200
    assert client.get("/recipe_list", auth=auth).status_code == 200
    assert client.get("/add_ingredients/1", auth=auth).status_code == 200
    assert client.get("/add_recipe_header/1", auth=auth).status_code == 200
    assert client.get("/selected_recipe", auth=auth, follow_redirects=False).status_code in (302, 400)

    # nieuwe regel via bewerkscherm krijgt Key + LineId per recept
    r = client.post("/save_recipe_lines/2", auth=auth, json={
        "existing": [{"LineId": 2, "ArticleId": "4", "Qty": "2", "UomId": "5002",
                      "ArticleRemark": "fijn", "SinglePlural": "PLURAL"}],
        "new": [{"ArticleId": "1", "Qty": "100", "UomId": "5005", "ArticleRemark": "", "SinglePlural": "PLURAL"}]})
    assert r.json() == {"success": True}
    r2 = [l for l in ss.worksheet("Lines").get_all_records() if l["RecipeId"] == 2]
    assert r2[1]["Qty"] == 2 and r2[1]["ArticleRemark"] == "fijn"
    assert r2[2]["LineId"] == 3 and r2[2]["Key"] == 6 and r2[2]["Qty"] == 100


def test_webapp_writes_keep_sheet_structure(env):
    client, ss = env
    auth = ("recepten", "geheim")
    lines = ss.worksheet("Lines")
    formula_row2 = lines.raw(2, "ArticlePreview")

    # bestaande regel bewerken + nieuwe regel via het bewerkscherm
    r = client.post("/save_recipe_lines/2", auth=auth, json={
        "existing": [{"LineId": 1, "ArticleId": "3", "Qty": "300", "UomId": "5005",
                      "ArticleRemark": "", "SinglePlural": "PLURAL"}],
        "new": [{"ArticleId": "4", "Qty": "1", "UomId": "5002", "ArticleRemark": "", "SinglePlural": "SINGLE"}]})
    assert r.json() == {"success": True}
    assert lines.raw(2, "ArticlePreview") == formula_row2          # formule van ander recept intact
    row4 = 4                                                        # recept 2, LineId 1
    assert lines.raw(row4, "Qty") == 300 and lines.raw(row4, "RecipeId") == "2"
    assert lines.raw(row4, "ArticlePreview").startswith("=IF(H4=")  # formule niet overschreven
    last = len(lines.rows)
    assert lines.raw(last, "RecipeId") == "2" and lines.raw(last, "ArticleId") == "4"
    assert lines.raw(last, "Replace?") == f"=VLOOKUP(G{last},Articles!B:G,6,false)"

    # titel wijzigen raakt RecipeId niet
    client.post("/edit_recipe_main/1", auth=auth, data={"Title": "Pasta pesto!", "Link": "x", "Content": "y"})
    main = ss.worksheet("RecipeMain")
    assert main.raw(2, "Title") == "Pasta pesto!" and main.raw(2, "RecipeId") == "1"

    # nieuw recept via de webapp: RecipeId als tekst
    client.post("/add", auth=auth, data={"title": "Nieuw gerecht"})
    assert main.raw(len(main.rows), "RecipeId") == "3"


def test_edit_screen_ignores_client_single_plural(env):
    """SinglePlural volgt uit eenheid + aantal, ook als de browser iets anders meestuurt."""
    client, ss = env
    auth = ("recepten", "geheim")
    r = client.post("/save_recipe_lines/2", auth=auth, json={
        "existing": [{"LineId": 1, "ArticleId": "3", "Qty": "300", "UomId": "5005",
                      "ArticleRemark": "", "SinglePlural": "PLURAL"}],
        "new": [{"ArticleId": "4", "Qty": "3", "UomId": "5002", "ArticleRemark": ""}]})
    assert r.json() == {"success": True}
    rows = [l for l in ss.worksheet("Lines").get_all_records() if l["RecipeId"] == 2]
    assert rows[0]["SinglePlural"] == "SINGLE"      # 300 g → enkelvoud
    assert rows[-1]["SinglePlural"] == "PLURAL"     # 3 stuk(s) → meervoud


def test_add_ingredients_form_derives_single_plural(env):
    client, ss = env
    auth = ("recepten", "geheim")
    page = client.get("/add_ingredients/2", auth=auth)
    assert "singleplural" not in page.text and "Enkv/Meerv" not in page.text
    r = client.post("/add_ingredients/2", auth=auth, follow_redirects=False, data={
        "qty_1": "2", "uom_1": "stuk(s)", "article_1": "ui", "remark_1": "",
        "qty_2": "200", "uom_2": "g", "article_2": "linze", "remark_2": "",
    })
    assert r.status_code in (302, 303), r.text
    rows = [l for l in ss.worksheet("Lines").get_all_records() if l["RecipeId"] == 2]
    assert [x["SinglePlural"] for x in rows[-2:]] == ["PLURAL", "SINGLE"]
