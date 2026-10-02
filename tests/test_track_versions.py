"""按车次轨迹版本复现穿越的测试：版本快照原子保留、track_version 查询、越界报错。

要点：
- 每批真正录入新点 / 每批成功更正，在同一事务原子保留足以复原该版本有效点集的快照；
  纯幂等重传不产生新版本、不产生快照记录。
- 穿越查询 ``track_version=V`` 按 V 版本当时点集核算，响应回填实际使用的版本；
  可与区域 ``revision`` 同时使用；省略 ``track_version`` 时请求/响应语义不变。
- 不存在、早于基线、晚于当前的版本明确 404。
- 升级时既有车次只以当时当前点集登记版本 0 基线，不推造更早轨迹。
"""
import sqlite3

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _batch(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


# -50(0s) -> 50(40s) -> 150(80s)，横穿 0..100 方块：区内 40s
PTS = [
    ("p1", "2026-09-30T10:00:00Z", -50.0, 50.0),
    ("p2", "2026-09-30T10:00:40Z", 50.0, 50.0),
    ("p3", "2026-09-30T10:01:20Z", 150.0, 50.0),
]


def _upload(client, trip="T", points=PTS):
    return client.post(f"/api/v1/trips/{trip}/points", json=_batch(points))


def _snapshot_pids(db, trip, version):
    with db.read_view() as conn:
        rows = db.list_trip_snapshot_points(conn, trip, version)
    return [(r["point_id"], r["ts_epoch"], r["x"], r["y"]) for r in rows]


def _event_versions(db, trip):
    with db.read_view() as conn:
        rows = db.list_trip_version_event_rows(conn, trip)
    return [(int(r["version"]), r["kind"]) for r in rows]


# ---------------------------------------------------------------------
# 版本快照随“真正录入新点”原子保留；幂等重传不产生版本/快照
# ---------------------------------------------------------------------

def test_new_point_batch_records_version_snapshot(client):
    from app.main import db

    register_square(client, zone_id="z")
    _upload(client)  # v1

    assert _event_versions(db, "T") == [(1, "points")]
    snap1 = _snapshot_pids(db, "T", 1)
    assert [p[0] for p in snap1] == ["p1", "p2", "p3"]

    # 纯幂等重传：不推进版本、不登记事件
    _upload(client)
    assert client.get("/api/v1/trips/T").json()["version"] == 1
    assert _event_versions(db, "T") == [(1, "points")]

    # 混合批（部分幂等 + 真正新点）只推进一次
    client.post(
        "/api/v1/trips/T/points",
        json=_batch([
            ("p1", "2026-09-30T10:00:00Z", -50.0, 50.0),  # 幂等
            ("p4", "2026-09-30T10:02:00Z", 250.0, 50.0),   # 新点
        ]),
    )
    assert client.get("/api/v1/trips/T").json()["version"] == 2
    assert _event_versions(db, "T") == [(1, "points"), (2, "points")]
    assert [p[0] for p in _snapshot_pids(db, "T", 2)] == ["p1", "p2", "p3", "p4"]


def test_failed_batch_writes_no_version_or_snapshot(client):
    from app.main import db

    register_square(client, zone_id="z")
    _upload(client)  # v1
    # 异内容冲突：整批回滚，不留版本事件/快照
    r = client.post(
        "/api/v1/trips/T/points",
        json=_batch([("p1", "2026-09-30T10:00:00Z", -999.0, 999.0)]),
    )
    assert r.status_code == 409
    assert _event_versions(db, "T") == [(1, "points")]
    assert client.get("/api/v1/trips/T").json()["version"] == 1


# ---------------------------------------------------------------------
# 成功更正原子保留新版本快照（含全撤销空快照）
# ---------------------------------------------------------------------

def test_correction_records_snapshot_and_old_version_still_reproducible(client):
    from app.main import db

    register_square(client, zone_id="z")
    _upload(client)  # v1

    # v2：把 p2 改到区外（y=500），最新核算不再穿越
    r = client.post(
        "/api/v1/trips/T/corrections",
        json={
            "base_version": 1,
            "reason": "p2 坐标录错",
            "items": [{"action": "update", "point_id": "p2",
                       "ts": "2026-09-30T10:00:40Z", "x": 50.0, "y": 500.0}],
        },
    )
    assert r.status_code == 200, r.text
    assert _event_versions(db, "T") == [(1, "points"), (2, "correction")]
    snap2 = _snapshot_pids(db, "T", 2)
    assert [p[0] for p in snap2] == ["p1", "p2", "p3"]
    assert next(p for p in snap2 if p[0] == "p2")[3] == 500.0

    # 最新（当前点集）：不穿越
    latest = client.get("/api/v1/trips/T/crossings?zone_id=z").json()
    assert "track_version" not in latest  # 省略参数：响应语义不变
    assert latest["zones"][0]["crossed"] is False

    # 按 v1 点集复现：仍是 40s，响应标明实际版本
    old = client.get("/api/v1/trips/T/crossings?zone_id=z&track_version=1").json()
    assert old["track_version"] == 1
    assert "revision" not in old  # 未指定区域修订号：不回填 revision
    z = old["zones"][0]
    assert z["total_duration_sec"] == 40.0
    assert z["segments"][0]["start"] == "2026-09-30T10:00:20Z"
    assert z["segments"][0]["end"] == "2026-09-30T10:01:00Z"
    assert old["point_count"] == 3

    # v2 点集：不穿越
    cur = client.get("/api/v1/trips/T/crossings?zone_id=z&track_version=2").json()
    assert cur["track_version"] == 2
    assert cur["zones"][0]["crossed"] is False
    assert cur["point_count"] == 3


def test_revoke_correction_snapshot_excludes_revoked_point(client):
    from app.main import db

    register_square(client, zone_id="z")
    _upload(client)  # v1
    # v2：撤销中间点 p2，直线 -50 -> 150 仍 40s
    r = client.post(
        "/api/v1/trips/T/corrections",
        json={
            "base_version": 1,
            "reason": "p2 漂移",
            "items": [{"action": "revoke", "point_id": "p2"}],
        },
    )
    assert r.status_code == 200, r.text
    assert [p[0] for p in _snapshot_pids(db, "T", 2)] == ["p1", "p3"]

    v2 = client.get("/api/v1/trips/T/crossings?zone_id=z&track_version=2").json()
    assert v2["point_count"] == 2
    assert v2["zones"][0]["total_duration_sec"] == 40.0
    v1 = client.get("/api/v1/trips/T/crossings?zone_id=z&track_version=1").json()
    assert v1["point_count"] == 3
    assert v1["zones"][0]["total_duration_sec"] == 40.0


def test_revoke_all_records_empty_snapshot_version(client):
    from app.main import db

    _upload(client)  # v1
    r = client.post(
        "/api/v1/trips/T/corrections",
        json={
            "base_version": 1,
            "reason": "全部撤销",
            "items": [{"action": "revoke", "point_id": p} for p, *_ in PTS],
        },
    )
    assert r.status_code == 200, r.text
    # 版本仍登记、快照点为空但版本可查（point_count=0, zones=[]）
    assert _event_versions(db, "T") == [(1, "points"), (2, "correction")]
    assert _snapshot_pids(db, "T", 2) == []
    resp = client.get("/api/v1/trips/T/crossings?track_version=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["track_version"] == 2 and body["point_count"] == 0 and body["zones"] == []

    # 撤销后重新上传新点：版本继续推进，v2 空快照不受影响
    client.post(
        "/api/v1/trips/T/points",
        json=_batch([("n1", "2026-09-30T11:00:00Z", 1.0, 1.0)]),
    )
    assert client.get("/api/v1/trips/T").json()["version"] == 3
    assert [p[0] for p in _snapshot_pids(db, "T", 3)] == ["n1"]
    assert _snapshot_pids(db, "T", 2) == []


def test_failed_correction_records_no_version_or_snapshot(client):
    from app.main import db

    _upload(client)  # v1
    # 过期版本：整批拒绝，不留事件/快照
    client.post(
        "/api/v1/trips/T/corrections",
        json={"base_version": 1, "reason": "第一次",
              "items": [{"action": "revoke", "point_id": "p3"}]},
    )
    r = client.post(
        "/api/v1/trips/T/corrections",
        json={"base_version": 1, "reason": "重放",
              "items": [{"action": "update", "point_id": "p1",
                         "ts": "2026-09-30T10:00:00Z", "x": 9.0, "y": 9.0}]},
    )
    assert r.status_code == 409
    assert _event_versions(db, "T") == [(1, "points"), (2, "correction")]
    assert client.get("/api/v1/trips/T").json()["version"] == 2


# ---------------------------------------------------------------------
# track_version 与区域 revision 正交组合
# ---------------------------------------------------------------------

def test_track_version_combined_with_zone_revision(client):
    # rev1：0..100 方块；rev2：扩到 0..200
    register_square(client, zone_id="z")
    register_square(client, zone_id="z", exterior=[[0, 0], [200, 0], [200, 200], [0, 200]])
    _upload(client)  # 轨迹 v1：区内（0..100）40s；扩区后（0..150）60s

    # v2：p3 从 150 改为 120（扩区终点仍在区内=60s；原区在 x=100 提前离开=48.571s）
    client.post(
        "/api/v1/trips/T/corrections",
        json={
            "base_version": 1,
            "reason": "p3 坐标复核",
            "items": [{"action": "update", "point_id": "p3",
                       "ts": "2026-09-30T10:01:20Z", "x": 120.0, "y": 50.0}],
        },
    )

    def dur(qs):
        return client.get(f"/api/v1/trips/T/crossings?zone_id=z&{qs}").json()

    # 当前点集 + 当前区域（0..200，终点 120 仍在区内）：20+40 = 60s
    latest = dur("")
    assert "track_version" not in latest and "revision" not in latest
    assert latest["zones"][0]["total_duration_sec"] == 60.0

    # v1 点集 + 当前区域：同样 60s
    r = dur("track_version=1")
    assert r["track_version"] == 1
    assert r["zones"][0]["total_duration_sec"] == 60.0

    # 当前点集(v2) + rev1 区域（0..100）：在 x=100 提前离开 -> 48.571429s
    r = dur("revision=1")
    assert r["revision"] == 1
    assert r["zones"][0]["total_duration_sec"] == 48.571429

    # 两个版本同时指定：v1 点集 + rev1 区域 -> 40s，响应同时标明实际使用版本
    r = dur("track_version=1&revision=1")
    assert r["track_version"] == 1 and r["revision"] == 1
    assert r["zones"][0]["total_duration_sec"] == 40.0

    # v2 点集 + rev2 区域 与无参一致；v2 + rev1 与只钉 revision=1 一致
    r = dur("track_version=2&revision=2")
    assert r["zones"][0]["total_duration_sec"] == 60.0
    r = dur("track_version=2&revision=1")
    assert r["zones"][0]["total_duration_sec"] == 48.571429


def test_track_version_filters_since_until_still_apply(client):
    register_square(client, zone_id="z")
    _upload(client)
    client.post(
        "/api/v1/trips/T/corrections",
        json={
            "base_version": 1,
            "reason": "x",
            "items": [{"action": "update", "point_id": "p3",
                       "ts": "2026-09-30T10:01:20Z", "x": 120.0, "y": 50.0}],
        },
    )
    # 旧 v1 点集叠加时间半开过滤
    r = client.get(
        "/api/v1/trips/T/crossings?zone_id=z&track_version=1"
        "&since=2026-09-30T10:00:30Z&until=2026-09-30T10:01:00Z"
    ).json()
    assert r["track_version"] == 1
    assert r["zones"][0]["total_duration_sec"] == 30.0


# ---------------------------------------------------------------------
# 版本越界 / 不存在 / 非法参数
# ---------------------------------------------------------------------

def test_track_version_later_than_current_404(client):
    register_square(client, zone_id="z")
    _upload(client)  # v1
    r = client.get("/api/v1/trips/T/crossings?track_version=2")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "2" in detail and "当前" in detail


def test_track_version_before_baseline_404_for_new_trip(client):
    # 新功能上线后的车次最早可追溯版本是 1（首批新点），0 早于基线
    register_square(client, zone_id="z")
    _upload(client)
    r = client.get("/api/v1/trips/T/crossings?track_version=0")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "基线" in detail and "0" in detail


def test_track_version_unknown_trip_404(client):
    register_square(client, zone_id="z")
    r = client.get("/api/v1/trips/ghost/crossings?track_version=1")
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_track_version_bad_param_rejected(client):
    register_square(client, zone_id="z")
    _upload(client)
    assert client.get("/api/v1/trips/T/crossings?track_version=-1").status_code in (400, 422)
    assert client.get("/api/v1/trips/T/crossings?track_version=abc").status_code in (400, 422)


def test_track_version_with_both_bounds_error(client):
    # 轨迹版本越界与区域修订号越界各自独立报错
    register_square(client, zone_id="z")
    _upload(client)
    r = client.get("/api/v1/trips/T/crossings?track_version=99&revision=99")
    assert r.status_code == 404


# ---------------------------------------------------------------------
# 重启持久化：旧版本快照仍可复现
# ---------------------------------------------------------------------

def test_track_snapshots_survive_database_reopen():
    from app.config import get_settings
    from app.db import Database
    from app.service import AuditService

    settings = get_settings()
    db1 = Database(settings)
    svc1 = AuditService(db1)
    svc1.register_zone(
        {
            "zone_id": "persist-z",
            "exterior": SQUARE,
            "holes": [],
            "windows": DAY_WINDOWS,
        }
    )
    svc1.submit_points(
        "persist-track",
        {"points": [
            {"point_id": "q1", "ts": "2026-09-30T10:00:00Z", "x": -50, "y": 50},
            {"point_id": "q2", "ts": "2026-09-30T10:00:40Z", "x": 50, "y": 50},
            {"point_id": "q3", "ts": "2026-09-30T10:01:20Z", "x": 150, "y": 50},
        ]},
    )
    svc1.correct_points(
        "persist-track",
        {
            "base_version": 1,
            "reason": "q2 录错",
            "items": [{"action": "update", "point_id": "q2",
                       "ts": "2026-09-30T10:00:40Z", "x": 50, "y": 500}],
        },
    )
    db1.close()

    db2 = Database(settings)
    svc2 = AuditService(db2)
    v1 = svc2.query_crossings(
        "persist-track", zone_id="persist-z", track_version_raw=1
    )
    assert v1["track_version"] == 1
    assert v1["zones"][0]["total_duration_sec"] == 40.0
    v2 = svc2.query_crossings(
        "persist-track", zone_id="persist-z", track_version_raw=2
    )
    assert v2["zones"][0]["crossed"] is False
    # 晚于当前仍报错
    from app.service import NotFoundError
    import pytest

    with pytest.raises(NotFoundError):
        svc2.query_crossings("persist-track", track_version_raw=3)
    db2.close()


# ---------------------------------------------------------------------
# 升级迁移：既有车次只以当时当前点集登记 v0 基线，可查 v0、不可查更早
# ---------------------------------------------------------------------

def test_legacy_trip_baseline_version_zero_snapshot(tmp_path):
    from app.config import Settings
    from app.db import Database
    from app.service import AuditService, NotFoundError
    import pytest

    path = tmp_path / "legacy-track.db"
    settings = Settings()
    settings.db_path = path

    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE points (trip_id TEXT, point_id TEXT, ts_epoch REAL, x REAL, y REAL, "
        "PRIMARY KEY (trip_id, point_id))"
    )
    # 旧表点集：把时间戳设在 epoch（无窗口，穿越核算用不上；此处只验证快照可复原）
    raw.executemany(
        "INSERT INTO points VALUES (?,?,?,?,?)",
        [
            ("old", "p1", 0.0, 1.0, 2.0),
            ("old", "p2", 60.0, 3.0, 4.0),
        ],
    )
    raw.commit()
    raw.close()

    db = Database(settings)  # 构造时迁移为 v0 基线 + 快照
    try:
        svc = AuditService(db)
        assert _event_versions(db, "old") == [(0, "baseline")]
        snap0 = _snapshot_pids(db, "old", 0)
        assert [(p[0], p[2], p[3]) for p in snap0] == [("p1", 1.0, 2.0), ("p2", 3.0, 4.0)]

        # v0 可查（点 <2 个有效线段仍可；这里 2 个点，无区域则 zones 为空）
        resp = svc.query_crossings("old", track_version_raw=0)
        assert resp["track_version"] == 0 and resp["point_count"] == 2

        # 不推造更早轨迹：v0 之后尚无 v1 时，请求 v1 明确 404
        with pytest.raises(NotFoundError):
            svc.query_crossings("old", track_version_raw=1)

        # 基于 v0 更正后产生 v1，v0 快照不变
        svc.correct_points(
            "old",
            {
                "base_version": 0,
                "reason": "误录",
                "items": [{"action": "revoke", "point_id": "p2"}],
            },
        )
        assert [p[0] for p in _snapshot_pids(db, "old", 0)] == ["p1", "p2"]
        assert [p[0] for p in _snapshot_pids(db, "old", 1)] == ["p1"]
        assert _event_versions(db, "old") == [(0, "baseline"), (1, "correction")]
    finally:
        db.close()

    # 再开一次：迁移幂等，不重复登记 v0
    db2 = Database(settings)
    try:
        assert _event_versions(db2, "old") == [(0, "baseline"), (1, "correction")]
    finally:
        db2.close()
