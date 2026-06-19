from datetime import datetime, timezone

from data_access.daos import broadcast_dao
from data_access.models.broadcast import WaBroadcastLog, WaSuppression


def test_list_campaigns_groups_and_counts(db_session):
    for camp, status in [("c1", "sent"), ("c1", "delivered"), ("c2", "failed")]:
        db_session.add(WaBroadcastLog(campaign=camp, wa_digits=f"9100000000{status[0]}",
                                      template_name="t", language="en", status=status))
        db_session.flush()
    rows = broadcast_dao.list_campaigns(db_session)
    by = {r["campaign"]: r for r in rows}
    assert by["c1"]["total"] == 2
    assert by["c2"]["total"] == 1


def test_list_suppressions(db_session):
    broadcast_dao.suppress(db_session, wa_digits="919000000001", reason="manual", source="admin")
    rows = broadcast_dao.list_suppressions(db_session, limit=50, offset=0)
    assert len(rows) == 1 and rows[0].reason == "manual"
