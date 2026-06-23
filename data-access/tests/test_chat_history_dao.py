"""Step-4: chat_history_dao — incl. synthetic unified client_id (no clients row)."""
from data_access.daos import chat_history_dao


def test_insert_synthetic_unified_key_no_clients_row(db_session):
    cid = "__unified__abc123def456aaaa"
    chat_history_dao.insert(db_session, legacy_sqlite_id=1, client_id=cid,
                            role="user", content="hi", sources_json=None,
                            function_calls_json=None)
    rows = chat_history_dao.list_for_client(db_session, client_id=cid, limit=10)
    assert len(rows) == 1 and rows[0].content == "hi"


def test_update_content_and_set_feedback(db_session):
    chat_history_dao.insert(db_session, legacy_sqlite_id=2, client_id="c1",
                            role="assistant", content="old", sources_json=None,
                            function_calls_json=None)
    chat_history_dao.update_content(db_session, legacy_sqlite_id=2, content="new")
    chat_history_dao.set_feedback(db_session, legacy_sqlite_id=2, feedback="up")
    rows = chat_history_dao.list_for_client(db_session, client_id="c1", limit=10)
    assert rows[0].content == "new" and rows[0].feedback == "up"


def test_delete_after_removes_later_rows_only(db_session):
    for i in (10, 11, 12):
        chat_history_dao.insert(db_session, legacy_sqlite_id=i, client_id="c2",
                                role="user", content=f"m{i}", sources_json=None,
                                function_calls_json=None)
    chat_history_dao.delete_after(db_session, client_id="c2", after_legacy_id=10)
    rows = chat_history_dao.list_for_client(db_session, client_id="c2", limit=10)
    assert [r.legacy_sqlite_id for r in rows] == [10]


def test_delete_by_client_purges_all(db_session):
    chat_history_dao.insert(db_session, legacy_sqlite_id=20, client_id="c3",
                            role="user", content="x", sources_json=None,
                            function_calls_json=None)
    chat_history_dao.delete_by_client(db_session, client_id="c3")
    assert chat_history_dao.list_for_client(db_session, client_id="c3", limit=10) == []


def test_list_paginated(db_session):
    for i in range(30, 35):
        chat_history_dao.insert(db_session, legacy_sqlite_id=i, client_id="c4",
                                role="user", content=f"m{i}", sources_json=None,
                                function_calls_json=None)
    page = chat_history_dao.list_paginated(db_session, client_id="c4", offset=1, limit=2)
    assert len(page) == 2
