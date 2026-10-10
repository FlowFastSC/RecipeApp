"""Verbinding met de Google Sheets database ("Recepten").

Credentials komen uit omgevingsvariabelen, nooit uit de repository:

- GOOGLE_CREDENTIALS_JSON : volledige inhoud van het service-account JSON-bestand
- GOOGLE_CREDENTIALS_FILE : pad naar het JSON-bestand (alternatief, default credentials.json)
- SHEET_NAME              : naam van de spreadsheet (default "Recepten")
"""
import base64
import json
import os
from functools import lru_cache

import gspread

SHEET_NAME = os.environ.get("SHEET_NAME", "Recepten")


@lru_cache(maxsize=1)
def get_client() -> gspread.Client:
    raw = (os.environ.get("GOOGLE_CREDENTIALS_JSON") or "").strip()
    if raw:
        if not raw.startswith("{"):
            # Ook base64 toegestaan (handig als een paneel quotes/regeleinden verminkt)
            raw = base64.b64decode(raw).decode("utf-8")
        # strict=False: accepteert echte regeleinden in private_key, die sommige
        # panelen (zoals Coolify) maken van de \n-escapes in het bestand
        return gspread.service_account_from_dict(json.loads(raw, strict=False))
    path = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    return gspread.service_account(filename=path)


def get_spreadsheet():
    return get_client().open(SHEET_NAME)
