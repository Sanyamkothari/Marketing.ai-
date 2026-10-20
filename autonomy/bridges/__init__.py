"""Bridges from v2 to heavy v1 modules (DuckDB, AutoGluon, LightGBM, the Data Doctor's intake).

Every v1 import here sits inside a function, so importing `autonomy.bridges` stays light; only calling a
bridge pulls v1 in. Tests of a bridge carry `@pytest.mark.v1bridge` and run in the full CI job only.
"""
