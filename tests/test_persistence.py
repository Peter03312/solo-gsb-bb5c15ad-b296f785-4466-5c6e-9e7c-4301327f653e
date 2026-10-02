"""持久化测试：重新打开同一数据库文件后，历史记录仍可查询。"""
import os

from app.config import get_settings
from app.db import Database
from app.service import AuditService
from tests.conftest import SQUARE


def test_records_survive_database_reopen():
    settings = get_settings()
    path = settings.db_path
    assert os.path.exists(path)

    db1 = Database(settings)
    svc1 = AuditService(db1)
    svc1.register_zone(
        {
            "zone_id": "persist-z",
            "exterior": SQUARE,
            "holes": [],
            "windows": [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}],
        }
    )
    svc1.submit_points(
        "persist-trip",
        {
            "points": [
                {"point_id": "q1", "ts": "2026-09-30T10:00:00Z", "x": -50, "y": 50},
                {"point_id": "q2", "ts": "2026-09-30T10:00:40Z", "x": 50, "y": 50},
                {"point_id": "q3", "ts": "2026-09-30T10:01:20Z", "x": 150, "y": 50},
            ]
        },
    )
    db1.close()

    # 模拟进程重启：用同一文件新建连接
    db2 = Database(settings)
    svc2 = AuditService(db2)
    zone = svc2.get_zone("persist-z")
    assert zone is not None
    result = svc2.query_crossings("persist-trip", zone_id="persist-z")
    z = result["zones"][0]
    assert z["crossed"] is True
    assert z["total_duration_sec"] == 40.0
    db2.close()


def test_corrections_survive_database_reopen():
    settings = get_settings()
    db1 = Database(settings)
    svc1 = AuditService(db1)
    svc1.submit_points(
        "persist-fix",
        {
            "points": [
                {"point_id": "q1", "ts": "2026-09-30T10:00:00Z", "x": -50, "y": 50},
                {"point_id": "q2", "ts": "2026-09-30T10:00:40Z", "x": 50, "y": 50},
                {"point_id": "q3", "ts": "2026-09-30T10:01:20Z", "x": 150, "y": 50},
            ]
        },
    )
    svc1.correct_points(
        "persist-fix",
        {
            "base_version": 1,
            "reason": "q2 误录",
            "items": [
                {"action": "update", "point_id": "q2",
                 "ts": "2026-09-30T10:00:40Z", "x": 50, "y": 500},
                {"action": "revoke", "point_id": "q3"},
            ],
        },
    )
    db1.close()

    db2 = Database(settings)
    svc2 = AuditService(db2)
    trip = svc2.get_trip("persist-fix")
    assert trip["version"] == 2
    assert {p["point_id"] for p in trip["points"]} == {"q1", "q2"}
    assert next(p for p in trip["points"] if p["point_id"] == "q2")["y"] == 500

    hist = svc2.list_corrections("persist-fix")
    assert [c["action"] for c in hist["corrections"]] == ["update", "revoke"]
    # 已撤销 ID 重开后仍禁止上传重用
    import pytest
    from app.service import ConflictError

    with pytest.raises(ConflictError):
        svc2.submit_points(
            "persist-fix",
            {"points": [{"point_id": "q3", "ts": "2026-09-30T12:00:00Z", "x": 1, "y": 1}]},
        )
    db2.close()
