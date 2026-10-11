"""Verbinding met de Google Sheets database ("Recepten").

Credentials komen uit omgevingsvariabelen, nooit uit de repository:

- GOOGLE_CREDENTIALS_JSON : volledige inhoud van het service-account JSON-bestand
- GOOGLE_CREDENTIALS_FILE : pad naar het JSON-bestand (alternatief, default credentials.json)
- SHEET_NAME              : naam van de spreadsheet (default "Recepten")
- CACHE_SECONDS           : hoe lang gelezen tabbladen bewaard blijven (default 300)

Snelheid
--------
Elke aanroep naar Google kost een paar honderd milliseconden. Daarom:
- de spreadsheet wordt één keer per proces geopend (openen op naam is een Drive-zoekactie);
- alle tabbladen worden in één aanroep opgehaald (values_batch_get) en CACHE_SECONDS bewaard;
- elke schrijfactie via de app of Claude leegt de cache direct, zodat je je eigen
  wijzigingen meteen ziet. Alleen wijzigingen die je rechtstreeks in Google Sheets
  doet, verschijnen pas na maximaal CACHE_SECONDS.
"""
import base64
import json
import os
import threading
import time
from functools import lru_cache

import gspread
from gspread.utils import fill_gaps, numericise_all, to_records

SHEET_NAME = os.environ.get("SHEET_NAME", "Recepten")
CACHE_SECONDS = float(os.environ.get("CACHE_SECONDS", "300"))

# Tabbladen die samen in één aanroep worden opgehaald
PREFETCH = ("RecipeMain", "RecipeHeader", "Lines", "Articles", "Uom", "Jaar", "Week_status")

# Worksheet-methodes die iets veranderen: daarna is de cache ongeldig
WRITE_METHODS = {
    "update", "update_cell", "update_cells", "batch_update", "append_row", "append_rows",
    "insert_row", "insert_rows", "delete_rows", "delete_row", "add_rows", "add_cols",
    "resize", "clear", "batch_clear", "format", "batch_format",
}


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


def _to_records(values: list[list]) -> list[dict]:
    """Zelfde uitkomst als gspread's Worksheet.get_all_records() met standaardopties."""
    values = fill_gaps(values or [])
    if not values:
        return []
    keys, rows = values[0], values[1:]
    rows = [numericise_all(r, False, "", False, []) for r in rows]
    return to_records(keys, rows)


class SheetStore:
    """Procesbrede cache rond één gspread-spreadsheet (thread-safe)."""

    def __init__(self, open_spreadsheet, ttl: float = CACHE_SECONDS):
        self._open = open_spreadsheet
        self._ss = None
        self.ttl = ttl
        self._lock = threading.RLock()
        self._gen = 0                # verhoogd bij elke invalidatie
        self._records: dict[str, list[dict]] = {}
        self._worksheets: dict[str, object] = {}
        self._loaded_at = 0.0

    # -- basis ------------------------------------------------------------------
    @property
    def spreadsheet(self):
        with self._lock:
            if self._ss is None:
                self._ss = self._open()
            return self._ss

    def invalidate(self):
        with self._lock:
            self._gen += 1
            self._records = {}
            self._worksheets = {}   # o.a. row_count kan veranderd zijn
            self._loaded_at = 0.0

    def _fresh(self) -> bool:
        return self._records and (time.monotonic() - self._loaded_at) < self.ttl

    # -- lezen ------------------------------------------------------------------
    def worksheet(self, name):
        with self._lock:
            if name not in self._worksheets:
                self._worksheets[name] = self.spreadsheet.worksheet(name)
            return self._worksheets[name]

    def records(self, name) -> list[dict]:
        with self._lock:
            if not self._fresh():
                self._load_all()
            if name not in self._records:   # tabblad buiten PREFETCH
                self._records[name] = self.worksheet(name).get_all_records()
            return [dict(r) for r in self._records[name]]

    def _load_all(self):
        gen = self._gen
        ss = self.spreadsheet
        data: dict[str, list[dict]] = {}
        try:
            resp = ss.values_batch_get([f"'{n}'" for n in PREFETCH])
            for name, vr in zip(PREFETCH, resp.get("valueRanges", [])):
                data[name] = _to_records(vr.get("values", []))
        except Exception:
            # Bijvoorbeeld een ontbrekend tabblad: dan per tabblad ophalen
            data = {}
        if gen == self._gen:
            self._records = data
            self._loaded_at = time.monotonic()


class CachedWorksheet:
    """Worksheet waarvan get_all_records() uit de cache komt; schrijven leegt de cache."""

    def __init__(self, store: SheetStore, name: str):
        self._store = store
        self.title = name

    def get_all_records(self, *args, **kwargs):
        if args or kwargs:
            return self._store.worksheet(self.title).get_all_records(*args, **kwargs)
        return self._store.records(self.title)

    def __getattr__(self, attr):
        real = getattr(self._store.worksheet(self.title), attr)
        if attr in WRITE_METHODS and callable(real):
            def write(*args, **kwargs):
                try:
                    return real(*args, **kwargs)
                finally:
                    self._store.invalidate()
            return write
        return real


class CachedSpreadsheet:
    def __init__(self, store: SheetStore):
        self._store = store

    def worksheet(self, name):
        return CachedWorksheet(self._store, name)

    def invalidate(self):
        self._store.invalidate()

    def __getattr__(self, attr):
        return getattr(self._store.spreadsheet, attr)


@lru_cache(maxsize=1)
def get_store() -> SheetStore:
    return SheetStore(lambda: get_client().open(SHEET_NAME))


def get_spreadsheet() -> CachedSpreadsheet:
    return CachedSpreadsheet(get_store())
