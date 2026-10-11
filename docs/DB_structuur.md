# Databasestructuur – Google Sheets "Recepten"

Bijgewerkt: 10-10-2026, op basis van de backup van die dag.

## Tabbladen

| Tabblad | Kolommen (in volgorde) |
|---|---|
| RecipeMain | Key · RecipeId · Title |
| RecipeHeader | Key · RecipeId · Link · Content · Remarks · Utensils · CookingTime · Persons |
| Lines | Key · RecipeId · LineId · Qty · UomId · ArticleRemark · ArticleId · SinglePlural · *ArticlePreview* · *UomPreview* · *Replace?* |
| Articles | Key · ArticleId · NameSingle · NamePlural · DefaultUomId · *PreviewUom* · Delete row |
| Uom | Key · UomId · UomName |
| Jaar | Key · Week · Dag · RecipeId |
| Week_status | Key · Week · Status |
| Header, Detail | Oude structuur, niet meer in gebruik |

*Cursief* = formulekolom (VLOOKUP/XLOOKUP). Deze kolommen worden nooit door de app of Claude overschreven. Nieuwe rijen krijgen de formule van de rij erboven, met aangepast rijnummer.

```
RecipeMain (RecipeId)
 ├── RecipeHeader (RecipeId)  1-op-1
 ├── Lines        (RecipeId)  1-op-veel → Articles (ArticleId), Uom (UomId)
 └── Jaar         (RecipeId)  1-op-veel
Articles.DefaultUomId → Uom.UomId
```

## Datatypes – belangrijk

| Kolom | Type in de sheet |
|---|---|
| RecipeId in RecipeMain, RecipeHeader, Lines | **tekst** (`'1001`) |
| ArticleId in Lines, Articles | **tekst** (`'10002`) |
| UomId (overal), DefaultUomId | getal |
| Key, LineId, Qty, Persons | getal |
| RecipeId in Jaar | getal |

De VLOOKUP-formules rekenen op deze types. Een ArticleId die als getal wordt geschreven, levert "#N/A" in de previewkolommen op.

## Nummering

- **Key**: per tabblad het hoogste getal plus 1. De volgorde in de sheet maakt niet uit.
- **RecipeId**: begint bij 1001.
- **ArticleId**: begint bij 10001. `99999` = "aa-artikel ontbreekt" (markering, geen echt artikel).
- **UomId**: begint bij 5001. `9999` = "UOM ontbreekt" (markering). `5001` = "." (geen eenheid).
- **LineId**: uniek *per recept*, niet over de hele sheet. Gebruik altijd RecipeId + LineId. Na een wijziging via Claude is de nummering per recept weer 1, 2, 3 …

## Regels

- **Eenheden** heten in enkelvoud plus meervoud: `stuk(s)`, `teentje(s)`, `blik(ken)`. Claude mag `stuk`, `stuks`, `stuk(s)` of `tenen` schrijven; de server koppelt dat aan het juiste UomId.
- **SinglePlural**: wordt automatisch bepaald, je kiest het niet meer zelf. Bij een telbare eenheid (`stuk(s)`, `.` of geen eenheid): `SINGLE` als Qty ≤ 1, anders `PLURAL` ("1 ui", "2 uien"). Bij alle andere eenheden altijd `SINGLE` ("250 g spitskool", "2 blik(ken) reuzenbonen"). Dit bepaalt of NameSingle of NamePlural getoond wordt. De regel staat in `single_plural_for` in `recipe_db.py`.
- **Qty**: getal groter dan 0, maximaal 1 decimaal.
- **ArticleRemark**: maximaal 20 tekens.
- **Persons**: maximaal 10 tekens.
- **Samengevoegde artikelen**: staat er in `Articles.Delete row` "replace by 10362", dan gebruikt Claude artikel 10362.
- **Jaar** heeft 7 vaste rijen per week. Bij verwijderen uit het weekmenu wordt alleen RecipeId leeggemaakt; de rij blijft staan.
