"""Unified slice explorer — a local web app over the live database.

The static explorers embed pre-aggregated cubes, which caps how many
dimensions can be crossed. This app instead queries the DuckDB/SQLite
database on every filter change, so ANY combination of geography (state,
county, tract minority band, income quintile), lender, loan parameters
(type, purpose, occupancy, amount band), and applicant group works,
with any of those as the breakdown dimension.

Runs on localhost only (no auth, so it must never be bound to a public
interface). Stdlib HTTP server; queries are serialized through a lock
and built exclusively from the whitelists below with bound parameters.

  python run.py serve --db data/hmda.db --year 2025
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .constants import (GROUP_ORDER, GROUP_LABELS, LOAN_TYPE, LOAN_PURPOSE,
                        OCCUPANCY)
from .explorer import (DEC, SUPPRESS_N, BANDS, AMOUNT_BANDS, _amount_case,
                       _table_exists, _q)

# filter key -> SQL predicate (all values bound as parameters)
FILTERS = {
    "state":   "l.state_code = ?",
    "county":  "l.county_code = ?",
    "band":    "t.minority_band = ?",
    "incq":    "t.income_quintile_state = ?",
    "lei":     "l.lei = ?",
    "lt":      "l.loan_type = ?",
    "purpose": "l.loan_purpose = ?",
    "occ":     "l.occupancy_type = ?",
    "grp":     "l.group_re = ?",
    "amt":     None,  # handled via the amount CASE expression
}

# breakdown key -> SQL expression
DIMS = {
    "state":   "l.state_code",
    "county":  "l.county_code",
    "band":    "t.minority_band",
    "incq":    "CAST(t.income_quintile_state AS VARCHAR)",
    "lei":     "l.lei",
    "lt":      "l.loan_type",
    "purpose": "l.loan_purpose",
    "occ":     "l.occupancy_type",
    "grp":     "l.group_re",
    "amt":     None,  # amount CASE
}

METRICS = """
    COUNT(*) AS apps, SUM(l.is_denied) AS den,
    SUM(l.is_originated) AS orig, SUM(l.pricing_exempt) AS exempt_n,
    SUM(CASE WHEN l.is_originated = 1 AND l.lien_status = '1'
             AND l.rate_spread_n IS NOT NULL THEN 1 ELSE 0 END) AS sp_n,
    SUM(CASE WHEN l.is_originated = 1 AND l.lien_status = '1'
             AND l.rate_spread_n IS NOT NULL
        THEN l.rate_spread_n ELSE 0 END) AS sp_sum,
    SUM(CASE WHEN l.is_originated = 1 AND l.lien_status = '1'
             AND l.rate_spread_n >= 1.5 THEN 1 ELSE 0 END) AS hi_n,
    SUM(CASE WHEN l.is_originated = 1 AND l.lien_status = '1'
             AND l.interest_rate_n BETWEEN 0.1 AND 25
        THEN 1 ELSE 0 END) AS ir_n,
    SUM(CASE WHEN l.is_originated = 1 AND l.lien_status = '1'
             AND l.interest_rate_n BETWEEN 0.1 AND 25
        THEN l.interest_rate_n ELSE 0 END) AS ir_sum,
    SUM(CASE WHEN l.hoepa_status = '1' THEN 1 ELSE 0 END) AS hoepa_n"""

METRIC_KEYS = ["apps", "den", "orig", "exempt_n", "sp_n", "sp_sum",
               "hi_n", "ir_n", "ir_sum", "hoepa_n"]


class App:
    def __init__(self, db_path: str, year: int):
        from .db import connect
        self.conn, self.engine = connect(db_path)
        self.year = year
        self.lock = threading.Lock()
        self.amount_expr = _amount_case().replace("loan_amount_n",
                                                  "l.loan_amount_n")
        self.names = {}
        if _table_exists(self.conn, self.engine, "institutions"):
            nm = _q(self.conn, self.engine,
                    "SELECT lei, name FROM institutions "
                    "WHERE activity_year = ?", [str(year)])
            self.names = {r.lei: (r.name or "")
                          for r in nm.itertuples(index=False)}
        self.county_names = {}
        if _table_exists(self.conn, self.engine, "county_language"):
            cn = _q(self.conn, self.engine,
                    "SELECT county_fips, county_name FROM county_language")
            self.county_names = dict(zip(cn.county_fips, cn.county_name))
        self.have_tracts = _table_exists(self.conn, self.engine, "tracts")

    def meta(self):
        with self.lock:
            states = _q(self.conn, self.engine, f"""
                SELECT DISTINCT l.state_code AS s FROM lar l
                WHERE l.activity_year = ? AND {DEC}""",
                [str(self.year)])["s"].dropna().sort_values().tolist()
            counties = _q(self.conn, self.engine, f"""
                SELECT DISTINCT l.state_code AS s, l.county_code AS c
                FROM lar l WHERE l.activity_year = ? AND {DEC}
                      AND l.county_code IS NOT NULL AND l.county_code != ''
                ORDER BY c""", [str(self.year)])
        lenders = [{"lei": k, "name": v} for k, v in
                   sorted(self.names.items(), key=lambda kv: kv[1] or kv[0])]
        return {
            "year": self.year, "suppress_n": SUPPRESS_N,
            "states": [s for s in states if s],
            "counties": [{"state": r.s or "", "fips": r.c,
                          "name": self.county_names.get(r.c, r.c)}
                         for r in counties.itertuples(index=False)],
            "lenders": lenders,
            "groups": GROUP_ORDER, "group_labels": GROUP_LABELS,
            "loan_types": LOAN_TYPE, "purposes": LOAN_PURPOSE,
            "occupancy": OCCUPANCY, "bands": BANDS,
            "amt_labels": [f"${lo}k-{hi}k" if hi else f">= ${lo}k"
                           for lo, hi in AMOUNT_BANDS],
            "have_tracts": self.have_tracts,
        }

    def _where(self, params: dict):
        # DEC's columns exist only on lar, so they need no alias here
        clauses = ["l.activity_year = ?", DEC]
        binds = [str(self.year)]
        for key, pred in FILTERS.items():
            val = params.get(key, [""])[0]
            if not val:
                continue
            if key == "amt":
                clauses.append(f"({self.amount_expr}) = ?")
                binds.append(int(val))
            elif key == "incq":
                clauses.append(pred)
                binds.append(int(val))
            else:
                clauses.append(pred)
                binds.append(val)
        return " AND ".join(clauses), binds

    def _dim_name(self, by: str, code) -> str:
        code = "" if code is None else str(code)
        if by == "lei":
            return self.names.get(code) or code
        if by == "county":
            return self.county_names.get(code, code)
        if by == "grp":
            return GROUP_LABELS.get(code, code or "(unknown)")
        if by == "lt":
            return LOAN_TYPE.get(code, code)
        if by == "purpose":
            return LOAN_PURPOSE.get(code, code)
        if by == "occ":
            return OCCUPANCY.get(code, code)
        if by == "amt":
            i = int(code) if code not in ("", "-1") else -1
            if i < 0:
                return "Not reported"
            lo, hi = AMOUNT_BANDS[i]
            return f"${lo}k-{hi}k" if hi else f">= ${lo}k"
        if by == "incq":
            return f"Q{code}" if code else "(no tract data)"
        return code or "(none)"

    def agg(self, params: dict):
        by = params.get("by", ["grp"])[0]
        if by not in DIMS:
            return {"error": f"unknown breakdown '{by}'"}
        where, binds = self._where(params)
        dim_expr = self.amount_expr if by == "amt" else DIMS[by]
        sql = f"""
            SELECT {dim_expr} AS k, {METRICS}
            FROM lar l LEFT JOIN tracts t ON l.tract11 = t.tract11
            WHERE {where} GROUP BY k"""
        total_sql = f"""
            SELECT {METRICS}
            FROM lar l LEFT JOIN tracts t ON l.tract11 = t.tract11
            WHERE {where}"""
        with self.lock:
            df = _q(self.conn, self.engine, sql, binds)
            tot = _q(self.conn, self.engine, total_sql, binds)
        def num(x):
            # SUM over an empty slice is NULL -> pandas NaN; both mean 0
            if x is None or x != x:
                return 0
            return round(float(x), 3)

        rows = []
        for r in df.itertuples(index=False):
            m = {k: num(getattr(r, k)) for k in METRIC_KEYS}
            rows.append({"code": "" if r.k is None else str(r.k),
                         "name": self._dim_name(by, r.k), **m})
        rows.sort(key=lambda x: -x["apps"])
        t = tot.iloc[0]
        total = {k: num(t[k]) for k in METRIC_KEYS}
        return {"by": by, "total": total, "rows": rows[:2000],
                "row_count": len(rows)}


class Handler(BaseHTTPRequestHandler):
    app: App = None
    page: bytes = b""

    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type",
                                 "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(self.page)))
                self.end_headers()
                self.wfile.write(self.page)
            elif u.path == "/api/meta":
                self._json(self.app.meta())
            elif u.path == "/api/agg":
                self._json(self.app.agg(parse_qs(u.query)))
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # surface errors to the UI, keep serving
            self._json({"error": repr(e)}, 500)


def serve(db_path: str | Path, year: int, port: int = 8600,
          open_browser: bool = True) -> None:
    app = App(str(db_path), year)
    page = (Path(__file__).parent / "unified_template.html").read_text(
        encoding="utf-8").encode("utf-8")
    Handler.app = app
    Handler.page = page
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Unified explorer: {url}  (Ctrl+C to stop)")
    if not app.have_tracts:
        print("Note: tracts table missing — minority-band and income-"
              "quintile filters will be empty until `run.py census` runs.")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
