"""Verbinding met de Google Sheets database ("Recepten").

Credentials komen uit omgevingsvariabelen, nooit uit de repository:

- GOOGLE_CREDENTIALS_JSON : volledige inhoud van het service-account JSON-bestand
- GOOGLE_CREDENTIALS_FILE : pad naar het JSON-bestand (alternatief, default credentials.json)
- SHEET_NAME              : naam van de spreadsheet (default "Recepten")
"""
import json
import os
from functools import lru_cache

import gspread

SHEET_NAME = os.environ.get("SHEET_NAME", "Recepten")


@lru_cache(maxsize=1)
def get_client() -> gspread.Client:
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return gspread.service_account_from_dict(json.loads(raw))
    path = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    return gspread.service_account(filename=path)


def get_spreadsheet():
    return get_client().open(SHEET_NAME)
