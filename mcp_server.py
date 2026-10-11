"""MCP-server: recepten opvragen, toevoegen en wijzigen vanuit Claude.

Schrijven gaat altijd in twee stappen:
1. aanroep ZONDER bevestigingscode  → voorstel + bevestigingscode, er wordt niets opgeslagen
2. gebruiker geeft akkoord          → zelfde aanroep MET bevestigingscode → opslaan
"""
from __future__ import annotations

import os
from typing import Callable

from pydantic import BaseModel, Field

from contextlib import contextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from recipe_db import Ingredient, RecipeDB, RecipeError

APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")

INSTRUCTIONS = """\
Receptendatabase van Francine (Google Sheets: RecipeMain, RecipeHeader, Lines, Articles, Uom).
Voertaal: Nederlands.

WERKWIJZE BIJ TOEVOEGEN OF WIJZIGEN
1. Zoek eerst of het recept al bestaat (zoek_recepten). Bij wijzigen: haal het op (recept_ophalen).
2. Zet ingrediënten om naar regels: qty / uom / article / remark.
   - Gebruik bestaande eenheden (lijst_eenheden). Geen eenheid genoemd → "stuk".
   - Gebruik bestaande artikelen (zoek_artikelen). Kleuren horen bij het artikel ("rode ui").
     Bewerkingen, maten, merken en kwaliteit gaan in remark (max 20 tekens): "gesnipperd", "middelgroot".
   - Alleen als een artikel echt niet bestaat: geef ook article_plural op (enkelvoud in article).
   - qty: max 1 decimaal, groter dan 0.
3. Roep recept_toevoegen / recept_wijzigen aan ZONDER bevestigingscode. Je krijgt een voorstel.
4. Laat het voorstel volledig aan de gebruiker zien, inclusief nieuwe artikelen/eenheden, en vraag om akkoord.
5. Pas NA expliciet akkoord van de gebruiker: dezelfde aanroep opnieuw, nu MET de bevestigingscode.
   Verander tussen stap 3 en 5 niets aan de invoer; bij een aanpassing opnieuw vanaf stap 3.
"""


class IngredientIn(BaseModel):
    qty: float | str = Field(description="Hoeveelheid, bijv. 2 of '0,5'. Max 1 decimaal.")
    uom: str = Field(description="Eenheid zoals in de Uom-tabel, bijv. 'g', 'el', 'stuk'.")
    article: str = Field(description="Artikelnaam (bestaand artikel, enkel- of meervoud; nieuw artikel: enkelvoud).")
    remark: str = Field("", description="Bewerking/opmerking, max 20 tekens, bijv. 'gesnipperd'.")
    article_plural: str | None = Field(None, description="Alleen voor een NIEUW artikel: meervoudsvorm.")

    def to_ingredient(self) -> Ingredient:
        return Ingredient(**self.model_dump())


def _header(link, bereidingswijze, opmerkingen, benodigdheden, bereidingstijd, personen) -> dict:
    mapping = {"Link": link, "Content": bereidingswijze, "Remarks": opmerkingen,
               "Utensils": benodigdheden, "CookingTime": bereidingstijd, "Persons": personen}
    return {k: v for k, v in mapping.items() if v is not None}


def _recipe_url(recipe_id) -> str | None:
    return f"{APP_BASE_URL}/select_recipe/{recipe_id}" if APP_BASE_URL else None


def _confirm_or_apply(db: RecipeDB, plan, bevestigingscode: str | None) -> dict:
    code = plan.fingerprint()
    if not bevestigingscode:
        return {
            "status": "voorstel",
            "opgeslagen": False,
            "voorstel": plan.summary,
            "bevestigingscode": code,
            "volgende_stap": "Toon dit voorstel aan de gebruiker. Roep pas na expliciet akkoord dezelfde "
                             "tool opnieuw aan met exact dezelfde invoer plus deze bevestigingscode.",
        }
    if bevestigingscode != code:
        return {
            "status": "voorstel_gewijzigd",
            "opgeslagen": False,
            "voorstel": plan.summary,
            "bevestigingscode": code,
            "volgende_stap": "De invoer of de database is gewijzigd sinds het vorige voorstel. Er is niets "
                             "opgeslagen. Toon dit nieuwe voorstel en vraag opnieuw om akkoord.",
        }
    db.apply(plan)
    recipe_id = plan.summary.get("RecipeId")
    return {"status": "opgeslagen", "opgeslagen": True, "RecipeId": recipe_id,
            "url": _recipe_url(recipe_id), "samenvatting": plan.summary}


@contextmanager
def _user_errors():
    """RecipeError → ToolError, zodat Claude de foutmelding te zien krijgt."""
    try:
        yield
    except RecipeError as e:
        raise ToolError(str(e)) from e


def build_server(db_factory: Callable[[], RecipeDB]) -> MCPServer:
    mcp = MCPServer(name="Recepten", instructions=INSTRUCTIONS)
    ro = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    rw = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

    @mcp.tool(annotations=ro)
    def zoek_recepten(zoekterm: str = "", artikel: str = "", max_resultaten: int = 25) -> dict:
        """Zoek recepten op (deel van) de titel en/of op een ingrediënt/artikel."""
        with _user_errors():
            return {"recepten": db_factory().search_recipes(zoekterm, artikel, max_resultaten)}

    @mcp.tool(annotations=ro)
    def recept_ophalen(recipe_id: int) -> dict:
        """Haal een volledig recept op: titel, link, bereidingswijze, opmerkingen en ingrediënten."""
        with _user_errors():
            r = db_factory().get_recipe(recipe_id)
            r["url"] = _recipe_url(recipe_id)
            return r

    @mcp.tool(annotations=ro)
    def zoek_artikelen(zoekterm: str) -> dict:
        """Zoek bestaande artikelen (ingrediënten) in de Articles-tabel, op enkel- of meervoud."""
        with _user_errors():
            return {"artikelen": db_factory().search_articles(zoekterm)}

    @mcp.tool(annotations=ro)
    def lijst_eenheden() -> dict:
        """Alle eenheden uit de Uom-tabel."""
        with _user_errors():
            return {"eenheden": db_factory().list_uoms()}

    @mcp.tool(annotations=rw)
    def recept_toevoegen(
        titel: str,
        ingredienten: list[IngredientIn],
        link: str = "",
        bereidingswijze: str = "",
        opmerkingen: str = "",
        benodigdheden: str = "",
        bereidingstijd: str = "",
        personen: str = "",
        bevestigingscode: str | None = None,
    ) -> dict:
        """Voeg een nieuw recept toe. Zonder bevestigingscode: alleen een voorstel (niets opgeslagen).
        Met de bevestigingscode uit het voorstel (na akkoord van de gebruiker): opslaan."""
        with _user_errors():
            db = db_factory()
            plan = db.plan_new_recipe(
                titel,
                _header(link, bereidingswijze, opmerkingen, benodigdheden, bereidingstijd, personen),
                [i.to_ingredient() for i in ingredienten],
            )
            return _confirm_or_apply(db, plan, bevestigingscode)

    @mcp.tool(annotations=rw)
    def recept_wijzigen(
        recipe_id: int,
        titel: str | None = None,
        link: str | None = None,
        bereidingswijze: str | None = None,
        opmerkingen: str | None = None,
        benodigdheden: str | None = None,
        bereidingstijd: str | None = None,
        personen: str | None = None,
        ingredienten: list[IngredientIn] | None = None,
        bevestigingscode: str | None = None,
    ) -> dict:
        """Wijzig een bestaand recept. Geef alleen velden mee die veranderen.
        'ingredienten' vervangt de VOLLEDIGE ingrediëntenlijst (geef dus ook ongewijzigde regels mee).
        Zonder bevestigingscode: alleen een voorstel met oud → nieuw. Met code (na akkoord): opslaan."""
        with _user_errors():
            db = db_factory()
            plan = db.plan_update_recipe(
                recipe_id,
                titel,
                _header(link, bereidingswijze, opmerkingen, benodigdheden, bereidingstijd, personen),
                [i.to_ingredient() for i in ingredienten] if ingredienten is not None else None,
            )
            return _confirm_or_apply(db, plan, bevestigingscode)

    return mcp


__all__ = ["build_server", "RecipeError"]
