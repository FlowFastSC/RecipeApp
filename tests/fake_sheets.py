"""In-memory nabootsing van gspread, net genoeg voor de tests."""
import copy
import re


class FakeWorksheet:
    def __init__(self, title, rows):
        self.title = title
        self.rows = [list(r) for r in rows]  # rij 0 = kopregel
        self.calls = []

    # lezen
    def row_values(self, n):
        return list(self.rows[n - 1])

    def get_all_records(self):
        hdr = self.rows[0]
        out = []
        for r in self.rows[1:]:
            r = list(r) + [""] * (len(hdr) - len(r))
            out.append({h: v for h, v in zip(hdr, r)})
        return out

    # schrijven
    def append_rows(self, rows):
        self.calls.append(("append_rows", len(rows)))
        self.rows.extend([list(r) for r in rows])

    def append_row(self, row):
        self.append_rows([row])

    def batch_update(self, data):
        self.calls.append(("batch_update", len(data)))
        for item in data:
            row = int(re.match(r"[A-Z]+(\d+)", item["range"]).group(1))
            self.rows[row - 1] = list(item["values"][0])

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


def sample_spreadsheet():
    return FakeSpreadsheet({
        "RecipeMain": [
            ["Key", "RecipeId", "Title"],
            [1, 1, "Pasta pesto"],
            [2, 2, "Linzensoep"],
        ],
        "RecipeHeader": [
            ["Key", "RecipeId", "Link", "Content", "Remarks", "Utensils", "CookingTime", "Persons"],
            [1, 1, "https://example.com/pesto", "Kook de pasta.", "", "", "20 min", "2"],
        ],
        "Lines": [
            ["Key", "RecipeId", "LineId", "Qty", "UomId", "Article", "Remark", "ArticleId", "SinglePlural"],
            [1, 1, 1, 200, 5005, "pasta", "", 1, "PLURAL"],
            [2, 1, 2, 1, 5002, "potje pesto", "", 2, "SINGLE"],
            [3, 2, 1, 250, 5005, "linzen", "", 3, "PLURAL"],
            [4, 2, 2, 1, 5002, "ui", "gesnipperd", 4, "SINGLE"],
            [5, 1, 3, 2, 5004, "pijnboompitten", "geroosterd", 5, "PLURAL"],
        ],
        "Articles": [
            ["Key", "ArticleId", "NameSingle", "NamePlural"],
            [1, 1, "pasta", "pasta"],
            [2, 2, "potje pesto", "potjes pesto"],
            [3, 3, "linze", "linzen"],
            [4, 4, "ui", "uien"],
            [5, 5, "pijnboompit", "pijnboompitten"],
            [6, "", "olie zonder id", "olie zonder id"],
        ],
        "Uom": [
            ["Key", "UomId", "UomName"],
            [1, 5002, "stuk"],
            [2, 5004, "el"],
            [3, 5005, "g"],
            [4, 5006, "tl"],
            [5, 5003, "teentje"],
        ],
    })
