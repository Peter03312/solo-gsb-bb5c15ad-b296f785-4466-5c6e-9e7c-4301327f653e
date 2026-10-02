"""按车次轨迹版本复现穿越测试。

每批真正录入新点 / 每批成功更正都原子保留该版本全量有效点集快照；
纯幂等重传不产生新版本或快照；升级时已有车次以当时当前版本为最早基线，
不推造更早轨迹。穿越查询可指定 track_version，并可与区域 revision 同时使用。
"""
import sqlite3

from tests.conftest import register_square


def _batch(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


# 以 2.5m/s 横穿：x=-50(10:00:00) -> 50(10:00:40) -> 150(10:01:20)
PTS = [
    ("p1", "2026-09-30T10:00:00Z", -50, 50),
    ("p2", "2026-09-30T10:00:40Z", 50, 50),
    ("p3", "2026-09-30T10:01:20Z", 150, 50),
]


def _upload(client, trip="T", points=PTS):
    return client.post(f"/api/v1/trips/{trip}/points", json=_batch(points))


def _correct(client, trip, base_version, reason, items):
    return client.post(
        f"/api/v1/trips/{trip}/corrections",
        json={"base_version": base_version, "reason": reason, "items": items},
    )


# ---------------------------------------------------------------------
# 版本快照原子保留
# ---------------------------------------------------------------------

def test_version_log_records_only_real_changes(client):
    _upload(client)  # 版本 1（新点）
    _upload(client)  # 纯幂等重传：不产生新版本
    body = client.get("/api/v1/trips/T/versions").json()
    assert body["version"] == 1
    assert body["baseline_version"] == 1
    assert [v["version"] for v in body["versions"]] == [1]
    assert body["versions"][0]["cause"] == "points"
    assert body["versions"][0]["point_count"] == 3

    # 再传一批真正新点 -> 版本 2
    client.post(
        "/api/v1/trips/T/points",
        json=_batch([("p4", "2026-09-30T10:02:00Z", 250, 50)]),
    )
    body = client.get("/api/v1/trips/T/versions").json()
    assert [v["version"] for v in body["versions"]] == [1, 2]
    assert body["versions"][1]["point_count"] == 4

    # 混合批（部分幂等 + 部分新点）只产生一个版本
    client.post(
        "/api/v1/trips/T/points",
        json=_batch([
            ("p1", "2026-09-30T10:00:00Z", -50, 50),   # 幂等
            ("p5", "2026-09-30T10:02:40Z", 350, 50),   # 新点
        ]),
    )
    body = client.get("/api/v1/trips/T/versions").json()
    assert [v["version"] for v in body["versions"]] == [1, 2, 3]
    assert body["versions"][2]["point_count"] == 5


def test_correction_version_snapshot_is_full_point_set(client):
    register_square(client, zone_id="z")
    _upload(client)
    r = _correct(
        client, "T", 1, "撤 p3",
        [{"action": "revoke", "point_id": "p3"}],
    )
    assert r.status_code == 200
    body = client.get("/api/v1/trips/T/versions").json()
    causes = {v["version"]: (v["cause"], v["point_count"]) for v in body["versions"]}
    assert causes == {1: ("points", 3), 2: ("correction", 2)}


# ---------------------------------------------------------------------
# 按轨迹版本复现穿越
# ---------------------------------------------------------------------

def test_crossings_pinned_to_track_version_reproduces_old_point_set(client):
    register_square(client, zone_id="geo")
    _upload(client)

    # 版本 1 点集：p2 在区内 -> 40s
    latest = client.get("/api/v1/trips/T/crossings?zone_id=geo").json()
    assert "track_version" not in latest
    assert latest["zones"][0]["total_duration_sec"] == 40.0

    # 更正：p2 实际在区外（y=500），两段折线均不再进入方块 -> 当前点集不穿越
    assert _correct(
        client, "T", 1, "p2 坐标录错",
        [{"action": "update", "point_id": "p2",
          "ts": "2026-09-30T10:00:40Z", "x": 50, "y": 500}],
    ).status_code == 200
    current = client.get("/api/v1/trips/T/crossings?zone_id=geo").json()
    assert current["zones"][0]["crossed"] is False

    # 按 track_version=1 复现：仍是旧点集，40s
    pinned = client.get(
        "/api/v1/trips/T/crossings?zone_id=geo&track_version=1"
    ).json()
    assert pinned["track_version"] == 1          # 回填实际使用的轨迹版本
    assert "revision" not in pinned             # 未指定区域修订号：不出现 revision
    assert pinned["point_count"] == 3
    assert pinned["zones"][0]["total_duration_sec"] == 40.0

    # 版本 2（更正后）与当前一致：不穿越
    v2 = client.get("/api/v1/trips/T/crossings?zone_id=geo&track_version=2").json()
    assert v2["track_version"] == 2
    assert v2["point_count"] == 3
    assert v2["zones"][0]["crossed"] is False


def test_track_version_and_zone_revision_can_combine(client):
    # rev1 方块 0..100；rev2 扩到 0..200
    register_square(client, zone_id="geo")
    _upload(client)
    register_square(client, zone_id="geo", exterior=[[0, 0], [200, 0], [200, 200], [0, 200]])
    # 更正 p2：拉远到 x=120（仍在 rev2 的 0..200 内，不在 rev1 的 0..100 内）
    _correct(
        client, "T", 1, "p2 x 偏",
        [{"action": "update", "point_id": "p2",
          "ts": "2026-09-30T10:00:40Z", "x": 120, "y": 50}],
    )

    def ask(track, rev):
        return client.get(
            f"/api/v1/trips/T/crossings?zone_id=geo"
            f"&track_version={track}&revision={rev}"
        ).json()

    # 旧点集(版本1) × 旧区域(rev1)：40s
    r = ask(1, 1)
    assert r["track_version"] == 1 and r["revision"] == 1
    assert r["zones"][0]["total_duration_sec"] == 40.0
    # 旧点集 × 新区域(0..200)：p3 在 x=150 区内，共 60s
    assert ask(1, 2)["zones"][0]["total_duration_sec"] == 60.0
    # 新点集(版本2) × 旧区域 rev1：仅 p1->p2 段切过方块约 23.53s，p2->p3 全程区外
    r = ask(2, 1)
    assert r["track_version"] == 2 and r["revision"] == 1
    assert round(r["zones"][0]["total_duration_sec"], 6) == round(800 / 34, 6)
    # 新点集 × 新区域：p1->p2 在 0..200 内约 48.24s，p2(120)->p3(150) 全在区内 20s
    r = ask(2, 2)
    assert round(r["zones"][0]["total_duration_sec"], 6) == round(20 + 820 / 17, 6)


def test_pinned_track_version_reflects_revoked_points(client):
    register_square(client, zone_id="z")
    _upload(client)
    # 撤销 p2：版本 2 仅剩 p1,p3 首尾直线（-50->150，80s 走 200m），0..100 段为 40s
    _correct(client, "T", 1, "撤 p2", [{"action": "revoke", "point_id": "p2"}])

    v1 = client.get("/api/v1/trips/T/crossings?zone_id=z&track_version=1").json()
    v2 = client.get("/api/v1/trips/T/crossings?zone_id=z&track_version=2").json()
    assert v1["point_count"] == 3 and v2["point_count"] == 2
    assert v1["zones"][0]["total_duration_sec"] == 40.0
    assert v2["zones"][0]["total_duration_sec"] == 40.0

    # 继续上传新点 -> 版本 3；旧版本快照不受影响
    client.post(
        "/api/v1/trips/T/points",
        json=_batch([("n1", "2026-09-30T10:03:00Z", 50, 500)]),
    )
    v1_again = client.get("/api/v1/trips/T/crossings?zone_id=z&track_version=1").json()
    assert v1_again["point_count"] == 3
    assert v1_again["zones"][0]["total_duration_sec"] == 40.0


def test_pinned_query_since_until_still_applies(client):
    register_square(client, zone_id="z")
    _upload(client)
    r = client.get(
        "/api/v1/trips/T/crossings?zone_id=z&track_version=1"
        "&since=2026-09-30T10:00:30Z&until=2026-09-30T10:01:00Z"
    ).json()
    assert r["track_version"] == 1
    assert r["zones"][0]["total_duration_sec"] == 30.0


def test_failed_correction_leaves_no_version_or_snapshot(client):
    register_square(client, zone_id="z")
    _upload(client)
    # 过期版本：整批拒绝
    r = _correct(
        client, "T", 9, "过期",
        [{"action": "revoke", "point_id": "p2"}],
    )
    assert r.status_code == 409
    body = client.get("/api/v1/trips/T/versions").json()
    assert [v["version"] for v in body["versions"]] == [1]
    assert body["version"] == 1
    # 不存在的点：整批拒绝，同样不产生版本 2
    r = _correct(
        client, "T", 1, "含幽灵点",
        [{"action": "revoke", "point_id": "ghost"}],
    )
    assert r.status_code == 409
    assert [v["version"] for v in client.get("/api/v1/trips/T/versions").json()["versions"]] == [1]
    # 版本 2 不存在 -> 明确报错
    r = client.get("/api/v1/trips/T/crossings?track_version=2")
    assert r.status_code == 404


def test_pinned_query_with_less_than_two_points(client):
    register_square(client, zone_id="z")
    # 版本 1：仅一个点；版本 2：全部撤销后为空
    client.post(
        "/api/v1/trips/T/points",
        json=_batch([("solo", "2026-09-30T10:00:00Z", 50, 50)]),
    )
    _correct(
        client, "T", 1, "唯一一点误录，撤销",
        [{"action": "revoke", "point_id": "solo"}],
    )
    r = client.get("/api/v1/trips/T/crossings?zone_id=z&track_version=1").json()
    assert r["track_version"] == 1 and r["point_count"] == 1 and r["zones"] == []
    r = client.get("/api/v1/trips/T/crossings?track_version=2").json()
    assert r["track_version"] == 2 and r["point_count"] == 0 and r["zones"] == []


def test_versions_endpoint_unknown_trip_404(client):
    assert client.get("/api/v1/trips/nope/versions").status_code == 404
    _upload(client)
    body = client.get("/api/v1/trips/T/versions").json()
    assert body["baseline_version"] == 1 and body["version"] == 1


# ---------------------------------------------------------------------
# 错误：版本不存在 / 早于基线 / 晚于当前 / 非法
# ---------------------------------------------------------------------

def test_track_version_not_found(client):
    _upload(client)
    # 全新车次最早可追溯版本为 1：请求版本 0 = 早于基线
    r = client.get("/api/v1/trips/T/crossings?track_version=0")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "基线" in detail and "0" in detail

    # 晚于当前
    r = client.get("/api/v1/trips/T/crossings?track_version=5")
    assert r.status_code == 404
    assert "5" in r.json()["detail"] and "当前版本" in r.json()["detail"]

    # 车次不存在
    r = client.get("/api/v1/trips/ghost/crossings?track_version=1")
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]

    # 非法值
    assert client.get("/api/v1/trips/T/crossings?track_version=-1").status_code == 422
    assert client.get("/api/v1/trips/T/crossings?track_version=abc").status_code in (400, 422)


def test_unknown_trip_without_track_version_keeps_legacy_200(client):
    # 不指定轨迹版本时，既有穿越查询语义不变：未知车次返回 200 空结果
    register_square(client, zone_id="z")
    r = client.get("/api/v1/trips/nope/crossings")
    assert r.status_code == 200
    body = r.json()
    assert body["point_count"] == 0 and body["zones"] == []
    assert "track_version" not in body


# ---------------------------------------------------------------------
# 升级基线：已有车次只以当时当前版本与点集为基线，不推造更早轨迹
# ---------------------------------------------------------------------

def test_upgraded_trip_baselines_at_current_version(tmp_path):
    import os

    from app.config import Settings
    from app.db import Database
    from app.service import AuditService

    path = tmp_path / "upgraded.db"
    settings = Settings()
    settings.db_path = path

    # 模拟“上一版本”库：已有 trip_versions(version=4) + points，但无版本快照表数据
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE points (
            trip_id TEXT NOT NULL, point_id TEXT NOT NULL,
            ts_epoch REAL NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
            PRIMARY KEY (trip_id, point_id)
        );
        CREATE UNIQUE INDEX idx_points_trip_ts ON points(trip_id, ts_epoch);
        CREATE TABLE trip_versions (
            trip_id TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE revoked_points (
            trip_id TEXT NOT NULL, point_id TEXT NOT NULL, version INTEGER NOT NULL,
            reason TEXT NOT NULL, revoked_at TEXT NOT NULL,
            PRIMARY KEY (trip_id, point_id)
        );
        CREATE TABLE point_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id TEXT NOT NULL,
            version INTEGER NOT NULL, reason TEXT NOT NULL, point_id TEXT NOT NULL,
            action TEXT NOT NULL, before_ts_epoch REAL NOT NULL, before_x REAL NOT NULL,
            before_y REAL NOT NULL, after_ts_epoch REAL, after_x REAL, after_y REAL,
            corrected_at TEXT NOT NULL
        );
        """
    )
    raw.execute("INSERT INTO trip_versions VALUES ('old', 4, '2026-09-29T00:00:00Z')")
    raw.executemany(
        "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
        [
            ("old", "a", 0.0, -50.0, 50.0),
            ("old", "b", 40.0, 50.0, 50.0),
            ("old", "c", 80.0, 150.0, 50.0),
        ],
    )
    raw.commit()
    raw.close()

    db = Database(settings)  # 升级启动：建表 + 基线回填
    try:
        svc = AuditService(db)
        trip = svc.get_trip("old")
        assert trip["version"] == 4
        assert trip["baseline_version"] == 4  # 最早可追溯基线就是当时当前版本

        with db.read_view() as conn:
            log = conn.execute(
                "SELECT version, cause FROM trip_version_log WHERE trip_id='old' ORDER BY version"
            ).fetchall()
        assert [(r["version"], r["cause"]) for r in log] == [(4, "baseline")]

        # 早于基线（1/2/3）明确报错：不推造更早轨迹
        # 直接以服务层断言
        from app.service import NotFoundError
        import pytest

        for v in (1, 2, 3):
            with pytest.raises(NotFoundError):
                svc.query_crossings("old", track_version_raw=v)
        # 版本 4 可复现，且与当时点集一致
        with pytest.raises(NotFoundError):
            svc.query_crossings("old", track_version_raw=5)
        snap = svc.query_crossings("old", track_version_raw=4)
        assert snap["track_version"] == 4 and snap["point_count"] == 3

        # 升级后继续推进：版本 5
        svc.submit_points(
            "old",
            {"points": [{"point_id": "d", "ts": "1970-01-01T00:02:00Z", "x": 250, "y": 50}]},
        )
        assert svc.get_trip("old")["version"] == 5
        with db.read_view() as conn:
            log = conn.execute(
                "SELECT version, cause FROM trip_version_log WHERE trip_id='old' ORDER BY version"
            ).fetchall()
        assert [(r["version"], r["cause"]) for r in log] == [
            (4, "baseline"),
            (5, "points"),
        ]
    finally:
        db.close()

    # 再开一次：基线回填幂等，不重复、不改动
    db2 = Database(settings)
    try:
        with db2.read_view() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM trip_version_log WHERE trip_id='old'"
            ).fetchone()["c"]
        assert n == 2
    finally:
        db2.close()


def test_legacy_trip_idempotent_upload_then_correct_single_v0_baseline(tmp_path):
    """旧库车次（无 trip_versions）：幂等上传路径先补齐 v0 基线后，
    随后更正不得再插入第二个 v0 流水（主键冲突回归）。"""
    from app.config import Settings
    from app.db import Database
    from app.service import AuditService

    path = tmp_path / "legacy-idem.db"
    settings = Settings()
    settings.db_path = path

    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE points (trip_id TEXT, point_id TEXT, ts_epoch REAL, x REAL, y REAL, "
        "PRIMARY KEY (trip_id, point_id))"
    )
    raw.execute("INSERT INTO points VALUES ('L', 'p1', 0.0, 1.0, 2.0)")
    raw.commit()
    raw.close()

    db = Database(settings)
    try:
        svc = AuditService(db)
        # 纯幂等重传：不推进版本，但应已补齐 v0 基线
        r = svc.submit_points(
            "L",
            {"points": [{"point_id": "p1", "ts": "1970-01-01T00:00:00Z", "x": 1, "y": 2}]},
        )
        assert r["accepted_new"] == 0
        assert svc.get_trip("L")["version"] == 0
        # 随后基于 v0 更正：不得因重复 v0 流水而失败，推进到 v1
        resp = svc.correct_points(
            "L",
            {
                "base_version": 0,
                "reason": "改写",
                "items": [{"action": "update", "point_id": "p1",
                           "ts": "1970-01-01T00:01:00Z", "x": 3.0, "y": 4.0}],
            },
        )
        assert resp["version"] == 1
        with db.read_view() as conn:
            log = [
                (row["version"], row["cause"])
                for row in conn.execute(
                    "SELECT version, cause FROM trip_version_log WHERE trip_id='L' ORDER BY version"
                )
            ]
        assert log == [(0, "baseline"), (1, "correction")]
    finally:
        db.close()


def test_legacy_points_only_baseline_version_zero(tmp_path):
    """更老的库（仅 points 表，无 trip_versions）：基线仍为版本 0。"""
    from app.config import Settings
    from app.db import Database
    from app.service import AuditService, NotFoundError
    import pytest

    path = tmp_path / "ancient.db"
    settings = Settings()
    settings.db_path = path

    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE points (trip_id TEXT, point_id TEXT, ts_epoch REAL, x REAL, y REAL, "
        "PRIMARY KEY (trip_id, point_id))"
    )
    raw.execute("INSERT INTO points VALUES ('anc', 'p1', 100.0, 1.0, 2.0)")
    raw.commit()
    raw.close()

    db = Database(settings)
    try:
        svc = AuditService(db)
        assert svc.get_trip("anc")["baseline_version"] == 0
        r = svc.query_crossings("anc", track_version_raw=0)
        assert r["track_version"] == 0 and r["point_count"] == 1
        with pytest.raises(NotFoundError):
            svc.query_crossings("anc", track_version_raw=1)
    finally:
        db.close()


# ---------------------------------------------------------------------
# 快照重启后仍可复现
# ---------------------------------------------------------------------

def test_version_snapshots_survive_reopen(tmp_path):
    from app.config import Settings
    from app.db import Database
    from app.service import AuditService

    settings = Settings()
    settings.db_path = tmp_path / "snap.db"

    db1 = Database(settings)
    svc1 = AuditService(db1)
    svc1.register_zone(
        {
            "zone_id": "z",
            "exterior": [[0, 0], [100, 0], [100, 100], [0, 100]],
            "holes": [],
            "windows": [{"start": "1970-01-01T00:00:00Z", "end": "1970-01-02T00:00:00Z"}],
        }
    )
    svc1.submit_points(
        "rt",
        {"points": [
            {"point_id": "a", "ts": "1970-01-01T00:00:00Z", "x": -50, "y": 50},
            {"point_id": "b", "ts": "1970-01-01T00:00:40Z", "x": 50, "y": 50},
            {"point_id": "c", "ts": "1970-01-01T00:01:20Z", "x": 150, "y": 50},
        ]},
    )
    svc1.correct_points(
        "rt",
        {
            "base_version": 1,
            "reason": "撤 c",
            "items": [{"action": "revoke", "point_id": "c"}],
        },
    )
    db1.close()

    db2 = Database(settings)
    svc2 = AuditService(db2)
    r1 = svc2.query_crossings("rt", zone_id="z", track_version_raw=1)
    r2 = svc2.query_crossings("rt", zone_id="z", track_version_raw=2)
    assert r1["point_count"] == 3 and r1["zones"][0]["total_duration_sec"] == 40.0
    # 撤销 c 后仅剩 a(-50)->b(50)，0..100 内为 20s
    assert r2["point_count"] == 2 and r2["zones"][0]["total_duration_sec"] == 20.0
    db2.close()
