# Databasestructuur (Google Sheets "Recepten")

| Tabblad | Kolommen |
|---|---|
| RecipeMain | Key · RecipeId · Title |
| RecipeHeader | Key · RecipeId · Link · Content · Remarks · Utensils · CookingTime · Persons |
| Lines | Key · RecipeId · LineId · Qty · UomId · Article · Remark/ArticleRemark · ArticleId · SinglePlural |
| Articles | Key · ArticleId · NameSingle · NamePlural |
| Uom | Key · UomId · UomName |
| Jaar | Key · Week · Dag · RecipeId |
| Week_status | Key · Week · Status |

```
RecipeMain (RecipeId)
 ├── RecipeHeader (RecipeId)  1-op-1
 ├── Lines        (RecipeId)  1-op-veel → Articles (ArticleId), Uom (UomId)
 └── Jaar         (RecipeId)  1-op-veel
```

## Regels

- **Key** is per tabblad een oplopend getal (max + 1).
- **RecipeId** loopt op in RecipeMain.
- **LineId** is uniek *per recept*, niet globaal. Gebruik altijd de combinatie RecipeId + LineId.
- **Qty** is numeriek, met max 1 decimaal.
- **Opmerking** bij een regel is max 20 tekens. De kolom heet in de documentatie `Remark` en in de code/templates `ArticleRemark`. De code leest en schrijft beide; alleen de kolom die in het tabblad bestaat wordt gevuld.
- **SinglePlural**: `SINGLE` als Qty ≤ 1, anders `PLURAL`. Dit bepaalt of `NameSingle` of `NamePlural` getoond wordt.
- **Uom** heeft één naamveld: `UomName`.
- **Persons** is max 10 tekens.
- **Jaar** heeft 7 vaste rijen per week. Een lege RecipeId betekent een lege dag. Bij verwijderen uit het weekmenu wordt de cel leeggemaakt; de rij blijft staan.
- **Articles** zonder ArticleId worden overal genegeerd (dropdowns, zoeken, koppelen).
