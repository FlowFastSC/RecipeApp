"""Datalaag voor recepten: zoeken, ophalen, toevoegen en wijzigen.

Wordt gebruikt door de MCP-server (mcp_server.py). Alle databaseregels uit
DB_structuur staan hier op één plek:

- Key           : per tabblad oplopend (max + 1)
- RecipeId      : oplopend in RecipeMain
- LineId        : uniek PER recept (1..n), niet globaal
- SinglePlural  : afgeleid uit eenheid + Qty (zie single_plural_for)
- Qty           : numeriek, max 1 decimaal
- Remark        : max 20 tekens
- Persons       : max 10 tekens
- Uom           : één naamveld "UomName"

Schrijven gaat altijd in twee stappen: eerst een voorstel (plan) met een
bevestigingscode, daarna pas uitvoeren met diezelfde code. Is de database of
het voorstel tussendoor gewijzigd, dan klopt de code niet meer en wordt er
niets geschreven.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

MAIN, HEADER, LINES, ARTICLES, UOM = "RecipeMain", "RecipeHeader", "Lines", "Articles", "Uom"

# Velden in RecipeHeader die via de MCP-tools gezet kunnen worden
HEADER_FIELDS = ["Link", "Content", "Remarks", "Utensils", "CookingTime", "Persons"]

# Veelvoorkomende schrijfwijzen → naam in Uom-tabblad (case-insensitive)
UOM_ALIASES = {
    "gram": "g", "gr": "g", "kilogram": "kg", "kilo": "kg",
    "milliliter": "ml", "liter": "l",
    "eetlepel": "el", "eetlepels": "el", "eetl.": "el", "eetl": "el",
    "theelepel": "tl", "theelepels": "tl", "theel.": "tl", "theel": "tl",
    "st": "stuk", "st.": "stuk",
    "tenen": "teentje", "teen": "teentje",
}

# Id's die geen echte eenheid/artikel zijn maar een markering (bijv. 9999 "UOM ontbreekt")
SENTINEL_IDS = {9999, 99999}

# Kolommen waarin id's als TEKST staan (VLOOKUP-formules in de sheet rekenen daarop)
TEXT_ID_COLS = {
    "RecipeMain": {"RecipeId"},
    "RecipeHeader": {"RecipeId"},
    "Lines": {"RecipeId", "ArticleId"},
    "Articles": {"ArticleId"},
}


def _uom_keys(name: str) -> set[str]:
    """'stuk(s)' → {'stuk(s)', 'stuk', 'stuks'}; 'blik(ken)' → {'blik(ken)', 'blik', 'blikken'}."""
    n = _norm(name)
    keys = {n}
    m = re.fullmatch(r"(.+?)\((\w+)\)", n)
    if m:
        keys |= {m.group(1).strip(), (m.group(1) + m.group(2)).strip()}
    return keys


# Eenheden waarbij je het artikel zelf telt: "1 ui" / "2 uien".
# Bij alle andere eenheden (g, el, blik(ken), teentje(s) …) is het artikel een
# stofnaam en blijft het enkelvoud: "250 g spitskool", "2 blik(ken) tomatenblokjes".
COUNT_UOM_KEYS = {"", ".", "stuk", "stuks", "stuk(s)"}


def single_plural_for(qty, uom_name) -> str:
    """Bepaal SINGLE/PLURAL automatisch.

    - telbare eenheid (stuk(s), ".", geen eenheid): SINGLE bij Qty <= 1, anders PLURAL
    - elke andere eenheid: altijd SINGLE
    """
    if not (_uom_keys(uom_name or "") & COUNT_UOM_KEYS):
        return "SINGLE"
    try:
        q = float(str(qty).strip().replace(",", "."))
    except (TypeError, ValueError):
        q = 1
    return "PLURAL" if q > 1 else "SINGLE"


class RecipeError(ValueError):
    """Fout die aan de gebruiker getoond kan worden."""


# ─────────────────────────────── hulpfuncties ────────────────────────────────

def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _to_int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _next_id(records: list[dict], col: str) -> int:
    ids = [_to_int(r.get(col)) or 0 for r in records]
    return max((i for i in ids if i not in SENTINEL_IDS), default=0) + 1


def parse_qty(value: Any) -> float | int:
    """'1,5' → 1.5 ; '2' → 2. Max 1 decimaal, > 0."""
    if isinstance(value, bool):
        raise RecipeError(f"Ongeldige hoeveelheid: {value!r}")
    s = str(value).strip().replace(",", ".")
    try:
        q = float(s)
    except ValueError:
        raise RecipeError(f"Hoeveelheid '{value}' is geen getal")
    if q <= 0:
        raise RecipeError(f"Hoeveelheid moet groter dan 0 zijn (was {value})")
    if round(q, 1) != q:
        raise RecipeError(
            f"Hoeveelheid {value} heeft meer dan 1 decimaal; kies een andere eenheid (bijv. g i.p.v. kg)"
        )
    return int(q) if q == int(q) else q


@dataclass
class Ingredient:
    qty: Any
    uom: str
    article: str
    remark: str = ""
    article_plural: str | None = None   # alleen nodig voor een NIEUW artikel


@dataclass
class Plan:
    """Beschrijving van wat er geschreven gaat worden."""
    kind: str                       # "nieuw" | "wijziging"
    summary: dict                   # leesbaar voorstel voor de gebruiker
    new_articles: list[dict] = field(default_factory=list)
    new_uoms: list[dict] = field(default_factory=list)
    main_row: dict | None = None            # nieuw: volledige rij; wijziging: {"row": n, "values": {...}}
    header_row: dict | None = None          # idem
    line_updates: list[dict] = field(default_factory=list)   # [{"row": n, "values": {...}}]
    line_appends: list[dict] = field(default_factory=list)   # [{...}]
    line_deletes: list[int] = field(default_factory=list)    # sheet-rijnummers

    def fingerprint(self) -> str:
        payload = json.dumps(
            {k: v for k, v in self.__dict__.items()}, sort_keys=True, default=str, ensure_ascii=False
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:10]


# ─────────────────────────────── database ────────────────────────────────────

class RecipeDB:
    def __init__(self, spreadsheet):
        self.ss = spreadsheet
        self._cache: dict[str, Any] = {}

    # -- lezen ----------------------------------------------------------------
    def ws(self, name):
        if ("ws", name) not in self._cache:
            self._cache[("ws", name)] = self.ss.worksheet(name)
        return self._cache[("ws", name)]

    def records(self, name) -> list[dict]:
        # Eén RecipeDB-instantie per tool-aanroep: binnen die aanroep cachen we
        # de tabbladen zodat we binnen de leesquota van Google blijven.
        if ("rec", name) not in self._cache:
            self._cache[("rec", name)] = self.ws(name).get_all_records()
        return [dict(r) for r in self._cache[("rec", name)]]

    def headers(self, name) -> list[str]:
        if ("hdr", name) not in self._cache:
            self._cache[("hdr", name)] = self.ws(name).row_values(1)
        return self._cache[("hdr", name)]

    def _lookups(self):
        articles = [a for a in self.records(ARTICLES) if _to_int(a.get("ArticleId"))]
        uoms = [u for u in self.records(UOM) if _to_int(u.get("UomId"))]
        return articles, uoms

    def search_recipes(self, query: str = "", article: str = "", limit: int = 25) -> list[dict]:
        main = self.records(MAIN)
        q = _norm(query)
        hits = [r for r in main if r.get("Title") and (not q or q in _norm(r["Title"]))]
        if article:
            a = _norm(article)
            art_ids = {
                str(x["ArticleId"]) for x in self.records(ARTICLES)
                if x.get("ArticleId") and (a in _norm(x.get("NameSingle")) or a in _norm(x.get("NamePlural")))
            }
            rec_ids = {str(l.get("RecipeId")) for l in self.records(LINES) if str(l.get("ArticleId")) in art_ids}
            hits = [r for r in hits if str(r.get("RecipeId")) in rec_ids]
        hits.sort(key=lambda r: _norm(r["Title"]))
        return [{"RecipeId": r["RecipeId"], "Title": r["Title"]} for r in hits[:limit]]

    def get_recipe(self, recipe_id: int) -> dict:
        main = next((r for r in self.records(MAIN) if _to_int(r.get("RecipeId")) == recipe_id), None)
        if not main:
            raise RecipeError(f"Recept {recipe_id} bestaat niet")
        header = next((r for r in self.records(HEADER) if _to_int(r.get("RecipeId")) == recipe_id), {})
        articles, uoms = self._lookups()
        art = {str(a["ArticleId"]): a for a in articles}
        uom = {str(u["UomId"]): u for u in uoms}
        lines = []
        for l in self.records(LINES):
            if _to_int(l.get("RecipeId")) != recipe_id:
                continue
            a = art.get(str(l.get("ArticleId")), {})
            sp = l.get("SinglePlural") or "PLURAL"
            lines.append({
                "LineId": l.get("LineId"),
                "Qty": l.get("Qty"),
                "Uom": uom.get(str(l.get("UomId")), {}).get("UomName", ""),
                "Article": a.get("NameSingle" if sp == "SINGLE" else "NamePlural", "") or l.get("Article", ""),
                "NameSingle": a.get("NameSingle", ""),
                "NamePlural": a.get("NamePlural", ""),
                "SinglePlural": sp,
                "Remark": l.get("ArticleRemark", l.get("Remark", "")),
            })
        lines.sort(key=lambda x: _to_int(x["LineId"]) or 0)
        return {
            "RecipeId": recipe_id,
            "Title": main.get("Title", ""),
            **{f: header.get(f, "") for f in HEADER_FIELDS},
            "Ingredients": lines,
        }

    def search_articles(self, query: str, limit: int = 30) -> list[dict]:
        q = _norm(query)
        res = [
            {"ArticleId": a["ArticleId"], "NameSingle": a.get("NameSingle", ""), "NamePlural": a.get("NamePlural", "")}
            for a in self.records(ARTICLES)
            if a.get("ArticleId") and (q in _norm(a.get("NameSingle")) or q in _norm(a.get("NamePlural")))
        ]
        res.sort(key=lambda a: (_norm(a["NameSingle"]) != q, len(a["NameSingle"]), _norm(a["NameSingle"])))
        return res[:limit]

    def list_uoms(self) -> list[dict]:
        return [{"UomId": u["UomId"], "UomName": u.get("UomName", "")}
                for u in self.records(UOM) if u.get("UomId")]

    # -- plannen --------------------------------------------------------------
    def _resolve_ingredients(self, ingredients: list[Ingredient], plan: Plan) -> list[dict]:
        """Koppelt ingrediënten aan ArticleId/UomId; plant nieuwe artikelen/eenheden."""
        if not ingredients:
            raise RecipeError("Een recept heeft minimaal één ingrediënt nodig")
        articles, uoms = self._lookups()
        by_id = {_to_int(a.get("ArticleId")): a for a in articles}
        art_by_name: dict[str, dict] = {}
        for a in articles:
            if _to_int(a.get("ArticleId")) in SENTINEL_IDS:
                continue                      # 99999 "aa-artikel ontbreekt"
            note = str(a.get("Delete row") or "").strip()
            if note:
                # Samengevoegd artikel: "replace by 10362" → verwijs naar dat artikel
                m = re.search(r"(\d+)", note)
                target = by_id.get(int(m.group(1))) if m else None
                if not target:
                    continue
                a_match = target
            else:
                a_match = a
            for col in ("NameSingle", "NamePlural"):
                if a.get(col):
                    art_by_name.setdefault(_norm(a[col]), a_match)
        uom_by_name: dict[str, dict] = {}
        for u in uoms:
            if u.get("UomName") and _to_int(u.get("UomId")) not in SENTINEL_IDS:
                for k in _uom_keys(u["UomName"]):
                    uom_by_name.setdefault(k, u)
        next_art_id = _next_id(articles, "ArticleId")
        next_art_key = _next_id(self.records(ARTICLES), "Key")
        next_uom_id = _next_id(uoms, "UomId")
        next_uom_key = _next_id(self.records(UOM), "Key")

        errors, resolved = [], []
        for n, ing in enumerate(ingredients, start=1):
            try:
                qty = parse_qty(ing.qty)
                remark = (ing.remark or "").strip()
                if len(remark) > 20:
                    raise RecipeError(f"opmerking '{remark}' is langer dan 20 tekens")

                # Eenheid
                u_key = _norm(ing.uom) or "stuk"
                candidates = [u_key, UOM_ALIASES.get(u_key, "")]
                for suffix in ("s", "en"):          # meervoud → enkelvoud: 'stuks' → 'stuk'
                    if u_key.endswith(suffix) and len(u_key) > len(suffix) + 1:
                        candidates.append(u_key[: -len(suffix)])
                u = next((uom_by_name[c] for c in candidates if c and c in uom_by_name), None)
                new_uom = False
                if not u:
                    name = ing.uom.strip()
                    u = {"Key": next_uom_key, "UomId": next_uom_id, "UomName": name}
                    plan.new_uoms.append(u)
                    for k in _uom_keys(name):
                        uom_by_name.setdefault(k, u)
                    next_uom_id += 1
                    next_uom_key += 1
                    new_uom = True

                # Artikel
                a = art_by_name.get(_norm(ing.article))
                new_art = False
                if not a:
                    if not (ing.article_plural or "").strip():
                        raise RecipeError(
                            f"artikel '{ing.article}' bestaat nog niet; geef 'article_plural' op om het als "
                            f"nieuw artikel aan te maken, of kies een bestaand artikel (zoek_artikelen)"
                        )
                    a = {"Key": next_art_key, "ArticleId": next_art_id,
                         "NameSingle": ing.article.strip(), "NamePlural": ing.article_plural.strip(),
                         "DefaultUomId": _to_int(u["UomId"])}
                    plan.new_articles.append(a)
                    art_by_name[_norm(a["NameSingle"])] = a
                    art_by_name[_norm(a["NamePlural"])] = a
                    next_art_id += 1
                    next_art_key += 1
                    new_art = True

                sp = single_plural_for(qty, u["UomName"])
                display = a["NameSingle"] if sp == "SINGLE" else a["NamePlural"]
                resolved.append({
                    "Qty": qty, "UomId": _to_int(u["UomId"]), "UomName": u["UomName"],
                    "ArticleId": _to_int(a["ArticleId"]), "ArticleName": display,
                    "SinglePlural": sp, "Remark": remark,
                    "_new_article": new_art, "_new_uom": new_uom,
                })
            except RecipeError as e:
                errors.append(f"Regel {n} ({ing.qty} {ing.uom} {ing.article}): {e}")
        if errors:
            raise RecipeError("Het voorstel bevat fouten:\n- " + "\n- ".join(errors))
        return resolved

    @staticmethod
    def _line_values(recipe_id: int, line_id: int, key: int, r: dict) -> dict:
        # Zowel 'Remark' als 'ArticleRemark' en 'Article' vullen: alleen kolommen
        # die echt in het tabblad bestaan worden geschreven.
        return {
            "Key": key, "RecipeId": recipe_id, "LineId": line_id, "Qty": r["Qty"],
            "UomId": r["UomId"], "Article": r["ArticleName"], "Remark": r["Remark"],
            "ArticleRemark": r["Remark"], "ArticleId": r["ArticleId"], "SinglePlural": r["SinglePlural"],
        }

    @staticmethod
    def _summary_lines(resolved: list[dict]) -> list[str]:
        out = []
        for r in resolved:
            tags = [t for t, on in (("NIEUW ARTIKEL", r["_new_article"]), ("NIEUWE EENHEID", r["_new_uom"])) if on]
            s = f"{r['Qty']} {r['UomName']} {r['ArticleName']}"
            if r["Remark"]:
                s += f" ({r['Remark']})"
            if tags:
                s += "  [" + ", ".join(tags) + "]"
            out.append(s)
        return out

    @staticmethod
    def _check_header(fields: dict):
        if len(str(fields.get("Persons") or "")) > 10:
            raise RecipeError("Persons mag maximaal 10 tekens zijn")

    def plan_new_recipe(self, title: str, header: dict, ingredients: list[Ingredient]) -> Plan:
        title = (title or "").strip()
        if not title:
            raise RecipeError("Titel is verplicht")
        main = self.records(MAIN)
        dup = next((r for r in main if _norm(r.get("Title")) == _norm(title)), None)
        if dup:
            raise RecipeError(
                f"Er bestaat al een recept met de titel '{dup['Title']}' (RecipeId {dup['RecipeId']}). "
                f"Gebruik recept_wijzigen of kies een andere titel."
            )
        header = {k: (v if v is not None else "") for k, v in header.items() if k in HEADER_FIELDS}
        self._check_header(header)

        plan = Plan(kind="nieuw", summary={})
        resolved = self._resolve_ingredients(ingredients, plan)
        recipe_id = _next_id(main, "RecipeId")
        plan.main_row = {"Key": _next_id(main, "Key"), "RecipeId": recipe_id, "Title": title}
        plan.header_row = {"Key": _next_id(self.records(HEADER), "Key"), "RecipeId": recipe_id,
                           **{f: header.get(f, "") for f in HEADER_FIELDS}}
        next_key = _next_id(self.records(LINES), "Key")
        plan.line_appends = [self._line_values(recipe_id, i, next_key + i - 1, r)
                             for i, r in enumerate(resolved, start=1)]
        plan.summary = {
            "actie": "Nieuw recept toevoegen",
            "RecipeId": recipe_id,
            "Titel": title,
            **{f: header.get(f, "") for f in HEADER_FIELDS if header.get(f)},
            "Ingrediënten": self._summary_lines(resolved),
            "Nieuwe artikelen": [f"{a['NameSingle']} / {a['NamePlural']}" for a in plan.new_articles],
            "Nieuwe eenheden": [u["UomName"] for u in plan.new_uoms],
        }
        return plan

    def plan_update_recipe(self, recipe_id: int, title: str | None, header: dict,
                           ingredients: list[Ingredient] | None) -> Plan:
        current = self.get_recipe(recipe_id)  # gooit fout als recept niet bestaat
        plan = Plan(kind="wijziging", summary={"actie": "Recept wijzigen", "RecipeId": recipe_id,
                                               "Titel": current["Title"]})
        changes: dict[str, Any] = {}

        if title is not None and title.strip() and title.strip() != current["Title"]:
            new_title = title.strip()
            dup = next((r for r in self.records(MAIN)
                        if _norm(r.get("Title")) == _norm(new_title) and _to_int(r.get("RecipeId")) != recipe_id), None)
            if dup:
                raise RecipeError(f"Titel '{new_title}' is al in gebruik door RecipeId {dup['RecipeId']}")
            main = self.records(MAIN)
            idx = next(i for i, r in enumerate(main) if _to_int(r.get("RecipeId")) == recipe_id)
            plan.main_row = {"row": idx + 2, "values": {"Title": new_title}}
            changes["Titel"] = f"{current['Title']} → {new_title}"

        # None = veld niet wijzigen; "" = veld leegmaken
        header = {k: v for k, v in header.items() if k in HEADER_FIELDS and v is not None}
        header = {k: v for k, v in header.items() if str(v) != str(current.get(k, ""))}
        if header:
            self._check_header(header)
            hdr = self.records(HEADER)
            idx = next((i for i, r in enumerate(hdr) if _to_int(r.get("RecipeId")) == recipe_id), None)
            if idx is None:
                plan.header_row = {"new": True, "values": {
                    "Key": _next_id(hdr, "Key"), "RecipeId": recipe_id,
                    **{f: header.get(f, "") for f in HEADER_FIELDS}}}
            else:
                plan.header_row = {"row": idx + 2, "values": dict(header)}
            for k, v in header.items():
                old = str(current.get(k, ""))
                if k == "Content":
                    changes[k] = "bereidingswijze wordt vervangen"
                else:
                    changes[k] = f"{old or '(leeg)'} → {v or '(leeg)'}"

        if ingredients is not None:
            resolved = self._resolve_ingredients(ingredients, plan)
            lines = self.records(LINES)
            existing = sorted(
                [(i + 2, l) for i, l in enumerate(lines) if _to_int(l.get("RecipeId")) == recipe_id],
                key=lambda t: _to_int(t[1].get("LineId")) or 0,
            )
            next_key = _next_id(lines, "Key")
            for n, r in enumerate(resolved, start=1):
                if n <= len(existing):
                    row, old = existing[n - 1]
                    key = old.get("Key") or next_key
                    if not old.get("Key"):
                        next_key += 1
                    plan.line_updates.append({"row": row, "values": self._line_values(recipe_id, n, key, r)})
                else:
                    plan.line_appends.append(self._line_values(recipe_id, n, next_key, r))
                    next_key += 1
            plan.line_deletes = sorted((row for row, _ in existing[len(resolved):]), reverse=True)
            old_txt = [f"{l['Qty']} {l['Uom']} {l['Article']}" + (f" ({l['Remark']})" if l["Remark"] else "")
                       for l in current["Ingredients"]]
            new_txt = self._summary_lines(resolved)
            if old_txt != [t.split("  [")[0] for t in new_txt] or plan.new_articles or plan.new_uoms:
                changes["Ingrediënten (oud)"] = old_txt
                changes["Ingrediënten (nieuw)"] = new_txt
            else:
                plan.line_updates, plan.line_appends, plan.line_deletes = [], [], []

        if plan.new_articles:
            changes["Nieuwe artikelen"] = [f"{a['NameSingle']} / {a['NamePlural']}" for a in plan.new_articles]
        if plan.new_uoms:
            changes["Nieuwe eenheden"] = [u["UomName"] for u in plan.new_uoms]
        if not changes:
            raise RecipeError("Geen wijzigingen ten opzichte van het huidige recept")
        plan.summary["wijzigingen"] = changes
        return plan

    # -- schrijven ------------------------------------------------------------
    # Uitgangspunten (zie docs/DB_structuur.md):
    # - We schrijven ALLEEN de kolommen die we zelf vullen. Andere kolommen
    #   (formules zoals ArticlePreview, UomPreview, Replace?, PreviewUom) raken
    #   we bij een wijziging niet aan.
    # - Bij nieuwe rijen nemen we de formules over van de laatste rij erboven
    #   die een formule in die kolom heeft (rijnummers worden aangepast).
    # - RecipeId/ArticleId staan in de sheet als TEKST; die schrijven we ook als
    #   tekst, anders werken de VLOOKUP-formules niet.

    @staticmethod
    def _cast(sheet: str, col: str, value):
        if value in (None, ""):
            return ""
        if col in TEXT_ID_COLS.get(sheet, set()):
            return str(_to_int(value) if _to_int(value) is not None else value)
        if col == "Persons" and _to_int(value) is not None:
            return _to_int(value)                      # staat in de sheet als getal
        return value

    def _update_rows(self, sheet: str, updates: list[dict]):
        if not updates:
            return
        headers = self.headers(sheet)
        data = []
        for u in updates:
            for col, value in u["values"].items():
                if col in headers:
                    cell = f"{_col_letter(headers.index(col) + 1)}{u['row']}"
                    data.append({"range": cell, "values": [[self._cast(sheet, col, value)]]})
        if data:
            self.ws(sheet).batch_update(data, value_input_option="RAW")

    def _formula_templates(self, sheet: str, last_row: int) -> dict[int, tuple[str, int]]:
        """{kolomindex: (formule, rijnummer)} van de laatste rij met een formule per kolom."""
        if last_row < 2:
            return {}
        headers = self.headers(sheet)
        start = 2                                       # hele kolom: onderste rijen missen soms formules
        rng = f"A{start}:{_col_letter(len(headers))}{last_row}"
        rows = self.ws(sheet).get(rng, value_render_option="FORMULA")
        templates: dict[int, tuple[str, int]] = {}
        for offset, row in enumerate(rows):
            for i, v in enumerate(row):
                if isinstance(v, str) and v.startswith("="):
                    templates[i] = (v, start + offset)
        return templates

    def _append_rows(self, sheet: str, rows: list[dict]):
        if not rows:
            return
        ws, headers = self.ws(sheet), self.headers(sheet)
        last_row = len(ws.col_values(1))                  # kolom A (Key) is altijd gevuld
        templates = self._formula_templates(sheet, last_row)
        needed = last_row + len(rows) - ws.row_count
        if needed > 0:
            ws.add_rows(needed)
        data = []
        for k, row in enumerate(rows):
            target = last_row + 1 + k
            values = []
            for i, h in enumerate(headers):
                if h in row:
                    v = self._cast(sheet, h, row[h])
                    # USER_ENTERED: tekst met ' ervoor blijft tekst (ook '1001' en '1/2')
                    values.append("'" + v if isinstance(v, str) and v else v)
                elif i in templates:
                    formula, src = templates[i]
                    values.append(_shift_rows(formula, src, target))
                else:
                    values.append("")
            end_col = _col_letter(len(headers))
            data.append({"range": f"A{target}:{end_col}{target}", "values": [values]})
        ws.batch_update(data, value_input_option="USER_ENTERED")

    # Publieke namen, ook gebruikt door de webapp (app.py)
    def append_rows(self, sheet: str, rows: list[dict]):
        self._append_rows(sheet, rows)
        self._cache = {k: v for k, v in self._cache.items() if k[0] == "ws"}

    def update_cells(self, sheet: str, updates: list[dict]):
        self._update_rows(sheet, updates)

    def apply(self, plan: Plan) -> None:
        # Volgorde: eerst stamgegevens, dan recept, dan regels. Verwijderen van
        # onderaf zodat rijnummers kloppen.
        self._append_rows(ARTICLES, plan.new_articles)
        self._append_rows(UOM, plan.new_uoms)
        if plan.kind == "nieuw":
            self._append_rows(MAIN, [plan.main_row])
            self._append_rows(HEADER, [plan.header_row])
        else:
            if plan.main_row:
                self._update_rows(MAIN, [plan.main_row])
            if plan.header_row:
                if plan.header_row.get("new"):
                    self._append_rows(HEADER, [plan.header_row["values"]])
                else:
                    self._update_rows(HEADER, [plan.header_row])
        self._update_rows(LINES, plan.line_updates)
        for row in plan.line_deletes:
            self.ws(LINES).delete_rows(row)
        self._append_rows(LINES, plan.line_appends)
        self._cache = {k: v for k, v in self._cache.items() if k[0] == "ws"}


_REL_REF = re.compile(r"(?<![A-Za-z0-9_$!\]])(\$?[A-Z]{1,3})(\d+)(?![\d(])")


def _shift_rows(formula: str, src_row: int, dst_row: int) -> str:
    """Celverwijzingen van de voorbeeldrij omzetten naar de nieuwe rij: '=VLOOKUP(G2,…)' → '=VLOOKUP(G7,…)'.

    De 'eigen rij' van de formule wordt uit de formule zelf afgeleid (alle relatieve
    verwijzingen naar dezelfde rij); valt dat niet eenduidig af te leiden, dan geldt src_row.
    Absolute rijen ($5) en verwijzingen naar andere tabbladen (Articles!B:G) blijven staan.
    """
    rows = {int(m.group(2)) for m in _REL_REF.finditer(formula) if not m.group(0).endswith("$")}
    own = rows.pop() if len(rows) == 1 else src_row
    return _REL_REF.sub(lambda m: f"{m.group(1)}{dst_row}" if int(m.group(2)) == own else m.group(0), formula)


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
