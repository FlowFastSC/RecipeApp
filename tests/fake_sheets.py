"""In-memory nabootsing van gspread, net genoeg voor de tests.

Gedraagt zich op de punten die ertoe doen zoals Google Sheets:
- get_all_records() zet getal-achtige tekst om naar int/float (zoals gspread)
- USER_ENTERED: "'tekst" blijft tekst, "=…" is een formule
- formules blijven als formule staan; get(..., FORMULA) geeft ze terug
"""
import copy
import re


def _numericise(v):
    if isinstance(v, str):
        if re.fullmatch(r"-?\d+", v):
            return int(v)
        if re.fullmatch(r"-?\d+\.\d+", v):
            return float(v)
    return v


def _col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


class FakeWorksheet:
    def __init__(self, title, rows):
        self.title = title
        self.rows = [list(r) for r in rows]  # rij 0 = kopregel
        self.row_count = max(len(self.rows) + 20, 100)
        self.calls = []

    # lezen
    def row_values(self, n):
        return list(self.rows[n - 1])

    def col_values(self, n):
        vals = [r[n - 1] if len(r) >= n else "" for r in self.rows]
        while vals and vals[-1] in ("", None):
            vals.pop()
        return vals

    def get_all_records(self):
        hdr = self.rows[0]
        out = []
        for r in self.rows[1:]:
            r = list(r) + [""] * (len(hdr) - len(r))
            if all(v in ("", None) for v in r):
                continue
            out.append({h: _numericise(v) for h, v in zip(hdr, r)})
        return out

    def get(self, rng, value_render_option=None):
        m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", rng)
        c1, r1, c2, r2 = _col_index(m.group(1)), int(m.group(2)), _col_index(m.group(3)), int(m.group(4))
        out = []
        for r in range(r1, r2 + 1):
            row = self.rows[r - 1] if r - 1 < len(self.rows) else []
            row = list(row) + [""] * (c2 - len(row))
            out.append(row[c1 - 1:c2])
        return out

    def raw(self, row, col_name):
        return self.rows[row - 1][self.rows[0].index(col_name)]

    # schrijven
    def add_rows(self, n):
        self.calls.append(("add_rows", n))
        self.row_count += n

    def append_rows(self, rows, **kw):
        self.calls.append(("append_rows", len(rows)))
        self.rows.extend([list(r) for r in rows])

    def append_row(self, row, **kw):
        self.append_rows([row])

    def batch_update(self, data, value_input_option="RAW"):
        self.calls.append(("batch_update", len(data), value_input_option))
        for item in data:
            m = re.match(r"([A-Z]+)(\d+)", item["range"])
            col, row = _col_index(m.group(1)), int(m.group(2))
            assert row <= self.row_count, "buiten het raster"
            while len(self.rows) < row:
                self.rows.append([""] * len(self.rows[0]))
            target = self.rows[row - 1]
            for j, v in enumerate(item["values"][0]):
                if value_input_option == "USER_ENTERED" and isinstance(v, str) and v.startswith("'"):
                    v = v[1:]
                idx = col - 1 + j
                target.extend([""] * (idx + 1 - len(target)))
                target[idx] = v

    def update(self, range_name, values):
        self.batch_update([{"range": range_name, "values": values}])

    def update_cell(self, row, col, value):
        self.calls.append(("update_cell", 1))
        r = self.rows[row - 1]
        r.extend([""] * (col - len(r)))
        r[col - 1] = value

    def delete_rows(self, n):
        self.calls.append(("delete_rows", 1))
        del self.rows[n - 1]


class FakeSpreadsheet:
    def __init__(self, sheets):
        self.sheets = {name: FakeWorksheet(name, rows) for name, rows in sheets.items()}

    def worksheet(self, name):
        return self.sheets[name]

    def snapshot(self):
        return {n: copy.deepcopy(ws.rows) for n, ws in self.sheets.items()}


def _lines_row(key, rid, line, qty, uom, remark, art, sp, row):
    """Lines-rij zoals in de echte sheet, met formulekolommen."""
    return [key, rid, line, qty, uom, remark, art, sp,
            f'=IF(H{row}="SINGLE",VLOOKUP(G{row},Articles!B:C,2,FALSE),VLOOKUP(G{row},Articles!B:D,3,FALSE))',
            f"=ARRAYFORMULA(VLOOKUP(E{row},Uom!B:C,2,FALSE))",
            f"=VLOOKUP(G{row},Articles!B:G,6,false)"]


def sample_spreadsheet():
    """Structuur gelijk aan de echte sheet (backup 10-10-2026), met weinig data."""
    lines = [["Key", "RecipeId", "LineId", "Qty", "UomId", "ArticleRemark", "ArticleId", "SinglePlural",
              "ArticlePreview", "UomPreview", "Replace?"]]
    data = [
        (1, "1", 1, 200, 5005, "", "1", "PLURAL"),
        (2, "1", 2, 1, 5002, "", "2", "SINGLE"),
        (3, "2", 1, 250, 5005, "", "3", "SINGLE"),
        (4, "2", 2, 1, 5002, "gesnipperd", "4", "SINGLE"),
        (5, "1", 3, 2, 5004, "geroosterd", "5", "PLURAL"),
    ]
    for i, d in enumerate(data, start=2):
        lines.append(_lines_row(*d, row=i))
    # zoals door de oude webapp toegevoegd: zonder formules
    return FakeSpreadsheet({
        "RecipeMain": [
            ["Key", "RecipeId", "Title"],
            [1, "1", "Pasta pesto"],
            [2, "2", "Linzensoep"],
        ],
        "RecipeHeader": [
            ["Key", "RecipeId", "Link", "Content", "Remarks", "Utensils", "CookingTime", "Persons"],
            [1, "1", "https://example.com/pesto", "Kook de pasta.", "", "", "20 min", 2],
        ],
        "Lines": lines,
        "Articles": [
            ["Key", "ArticleId", "NameSingle", "NamePlural", "DefaultUomId", "PreviewUom", "Delete row"],
            [1, "1", "pasta", "pasta", 5005, "=ARRAYFORMULA(VLOOKUP(E2,Uom!B:C,2,FALSE))", ""],
            [2, "2", "potje pesto", "potjes pesto", 5002, "=ARRAYFORMULA(VLOOKUP(E3,Uom!B:C,2,FALSE))", ""],
            [3, "3", "linze", "linzen", 5005, "=ARRAYFORMULA(VLOOKUP(E4,Uom!B:C,2,FALSE))", ""],
            [4, "4", "ui", "uien", 5002, "=ARRAYFORMULA(VLOOKUP(E5,Uom!B:C,2,FALSE))", ""],
            [5, "5", "pijnboompit", "pijnboompitten", 5004, "=ARRAYFORMULA(VLOOKUP(E6,Uom!B:C,2,FALSE))", ""],
            [6, "6", "rode linze", "rode linzen", 5005, "=ARRAYFORMULA(VLOOKUP(E7,Uom!B:C,2,FALSE))", "replace by 3"],
            [7, "99999", "aa-artikel ontbreekt", "aa-artikel ontbreekt", 5001, "", ""],
        ],
        "Uom": [
            ["Key", "UomId", "UomName"],
            [1, 5001, "."],
            [2, 5002, "stuk(s)"],
            [3, 5003, "teentje(s)"],
            [4, 5004, "el"],
            [5, 5005, "g"],
            [6, 5006, "tl"],
            [7, 5012, "l"],
            [8, 9999, "UOM ontbreekt"],
        ],
    })
