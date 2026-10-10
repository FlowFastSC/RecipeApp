# Recepten App

Persoonlijke receptenapp: weekmenu, boodschappenlijst en receptenbeheer.
Flask, met Google Sheets als database. Er is ook een MCP-endpoint, zodat je via Claude recepten kunt toevoegen en wijzigen.

```
app.py            Flask-webapp (alle pagina's en routes)
sheets.py         Verbinding met Google Sheets (credentials via env)
recipe_db.py      Datalaag voor Claude: zoeken, voorstel maken, toevoegen, wijzigen
mcp_server.py     MCP-tools voor de Claude-connector
asgi.py           Productie-entrypoint: webapp + MCP in één proces
templates/        HTML (Jinja2)
static/css/       styles.css
tests/            Tests met een nep-spreadsheet (geen Google nodig)
docs/             Databasestructuur
```

## Lokaal draaien

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                  # en invullen; credentials.json in de map zetten
python app.py                                         # alleen de webapp, http://localhost:5000
uvicorn asgi:app --reload --port 8000                 # webapp + MCP, zoals in productie
pytest                                                # tests
```

`.env` wordt niet automatisch ingelezen. Zet de variabelen in je shell, of gebruik `GOOGLE_CREDENTIALS_FILE=credentials.json`.

## Deployen op Unraid met Coolify

1. **Coolify → New Resource → Application.** Kies de GitHub-repo (via de GitHub App of als private repo met deploy key), branch `main`.
2. **Build pack:** `Dockerfile`. **Port:** `8000`.
3. **Environment variables** (zie `.env.example`):

   | Variabele | Waarde |
   |---|---|
   | `GOOGLE_CREDENTIALS_JSON` | Volledige inhoud van `credentials.json` (op één regel). Markeer als *secret*. |
   | `SECRET_KEY` | Lange willekeurige string |
   | `MCP_TOKEN` | Lange willekeurige string (min. 24 tekens) |
   | `APP_BASE_URL` | `https://<jouw-domein>` |
   | `APP_PASSWORD` | Optioneel: wachtwoord voor de webapp |

   Willekeurige strings maak je met `python -c "import secrets;print(secrets.token_urlsafe(32))"`.
4. **Domain:** koppel het domein of de tunnel die je in Coolify beheert. HTTPS is nodig voor de Claude-connector.
5. **Automatisch deployen:** zet *Auto Deploy* aan. Elke push naar `main` rolt dan automatisch uit.
6. Controleer na deploy: `https://<jouw-domein>/health` geeft `ok`.

## Claude-connector (recepten toevoegen en wijzigen vanuit Claude)

Voeg in Claude een **custom connector** toe met deze URL:

```
https://<jouw-domein>/mcp/<MCP_TOKEN>
```

Het token in het pad is de beveiliging. Behandel de URL dus als een wachtwoord. Is hij gelekt, kies dan een nieuw `MCP_TOKEN` in Coolify en voeg de connector opnieuw toe.

Beschikbare tools:

| Tool | Wat het doet |
|---|---|
| `zoek_recepten` | Zoeken op titel en/of ingrediënt |
| `recept_ophalen` | Volledig recept met ingrediënten |
| `zoek_artikelen` / `lijst_eenheden` | Bestaande artikelen en eenheden opzoeken |
| `recept_toevoegen` | Nieuw recept, in twee stappen |
| `recept_wijzigen` | Bestaand recept aanpassen, in twee stappen |

**Twee stappen (akkoord):** zonder `bevestigingscode` geeft de tool alleen een voorstel terug. Dat voorstel bevat de ingrediënten en eventuele nieuwe artikelen of eenheden. Pas na jouw akkoord roept Claude de tool opnieuw aan met de bevestigingscode, en dan wordt er opgeslagen. Wijzigt de invoer of de database tussendoor, dan is de code ongeldig en wordt er niets geschreven.

De databaseregels worden in `recipe_db.py` afgedwongen:
- Key en RecipeId tellen op.
- LineId is uniek per recept.
- SINGLE/PLURAL wordt afgeleid uit Qty.
- Qty heeft maximaal 1 decimaal.
- Een opmerking is maximaal 20 tekens; Persons maximaal 10 tekens.
- Schrijven gebeurt in batches, om binnen de Google API-quota te blijven.
