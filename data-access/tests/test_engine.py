import os
from data_access.engine import engine, get_session

def test_engine_uses_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://x:y@localhost:5432/test")
    from importlib import reload
    import data_access.engine as e
    reload(e)
    assert "postgresql" in str(e.engine.url)

def test_get_session_yields_session():
    with get_session() as s:
        assert s.is_active
