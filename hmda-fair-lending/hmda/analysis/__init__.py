"""Analysis modules. Each exposes run(conn, engine, year) -> dict of
{sheet_name: DataFrame} consumed by the report generator."""
from __future__ import annotations

import pandas as pd


def q(conn, engine: str, sql: str, params: list | None = None) -> pd.DataFrame:
    """Run SQL, return a DataFrame. Portable across duckdb/sqlite."""
    params = params or []
    if engine == "duckdb":
        return conn.execute(sql, params).df()
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)
