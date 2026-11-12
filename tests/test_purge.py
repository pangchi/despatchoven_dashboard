"""Unit tests for the purge policy (no database needed): pytest -q"""
from datetime import datetime, timedelta, timezone

from app.purge import run_purge


class FakeCfg:
    RETENTION_DAYS = 0
    PURGE_THRESHOLD_PCT = 80.0
    PURGE_TARGET_PCT = 75.0
    DISK_CHECK_PATH = "/"


class FakeDB:
    def __init__(self, days_back):
        today = datetime.now(timezone.utc).date()
        self.parts = [(f"telemetry_{today - timedelta(days=d):%Y%m%d}", today - timedelta(days=d), 1000)
                      for d in sorted(days_back, reverse=True)]
        self.events = []

    def list_partitions(self):
        return list(self.parts)

    def drop_partition(self, name):
        self.parts = [p for p in self.parts if p[0] != name]

    def purge_events_before(self, ts):
        return 0

    def log_event(self, *a, **kw):
        self.events.append(a)


def test_no_purge_below_threshold():
    db = FakeDB([3, 2, 1, 0])
    assert run_purge(db, FakeCfg(), usage_fn=lambda: 50)["dropped"] == []
    assert len(db.parts) == 4


def test_purges_oldest_until_target():
    db = FakeDB([4, 3, 2, 1, 0])
    seq = iter([85, 78, 74])
    res = run_purge(db, FakeCfg(), usage_fn=lambda: next(seq))
    assert [d["partition"] for d in res["dropped"]] == [p[0] for p in FakeDB([4, 3]).parts]
    assert len(db.parts) == 3


def test_never_drops_today():
    db = FakeDB([1, 0, -1])   # yesterday, today, tomorrow
    res = run_purge(db, FakeCfg(), usage_fn=lambda: 95)
    assert len(res["dropped"]) == 1
    assert len(db.parts) == 2


def test_retention_days():
    cfg = FakeCfg()
    cfg.RETENTION_DAYS = 7
    db = FakeDB([30, 10, 8, 7, 1, 0])
    res = run_purge(db, cfg, usage_fn=lambda: 10)
    assert len(res["dropped"]) == 3
