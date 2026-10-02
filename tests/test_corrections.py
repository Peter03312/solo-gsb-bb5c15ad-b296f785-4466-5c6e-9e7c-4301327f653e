"""现场采集点更正测试：轨迹版本、撤销/改写、整组原子、不可变履历、上传约束。"""
from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _batch(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


PTS = [
    ("a", "2026-09-30T10:00:00Z", 0.0, 0.0),
    ("b", "2026-09-30T10:01:00Z", 10.0, 0.0),
    ("c", "2026-09-30T10:02:00Z", 20.0, 0.0),
]


def _upload(client, trip="T", points=PTS):
    return client.post(f"/api/v1/trips/{trip}/points", json=_batch(points))


def _correct(client, trip, base_version, reason, items):
    return client.post(
        f"/api/v1/trips/{trip}/corrections",
        json={"base_version": base_version, "reason": reason, "items": items},
    )


# ---------------------------------------------------------------------
# 版本基线与推进
# ---------------------------------------------------------------------

def test_existing_points_baseline_version_zero(client):
    _upload(client)
    body = client.get("/api/v1/trips/T").json()
    assert body["version"] == 1  # 首批新点后为 1
    # 幂等重传不推进
    _upload(client)
    assert client.get("/api/v1/trips/T").json()["version"] == 1
    # 只有真正新点才再推进一次
    client.post(
        "/api/v1/trips/T/points",
        json=_batch([("d", "2026-09-30T10:03:00Z", 30.0, 0.0)]),
    )
    assert client.get("/api/v1/trips/T").json()["version"] == 2
    # 混合幂等 + 新点也只推进一次
    client.post(
        "/api/v1/trips/T/points",
        json=_batch([
            ("a", "2026-09-30T10:00:00Z", 0.0, 0.0),  # 幂等
            ("e", "2026-09-30T10:04:00Z", 40.0, 0.0),  # 新点
        ]),
    )
    assert client.get("/api/v1/trips/T").json()["version"] == 3


def test_legacy_points_migrate_to_version_zero(tmp_path):
    """升级前已落库的点（无 trip_versions 行）以版本 0 为基线。"""
    import os

    from app.config import Settings
    from app.db import Database
    from app.service import AuditService

    path = tmp_path / "legacy-trips.db"
    settings = Settings()
    settings.db_path = path

    # 用旧结构（仅 points 表）造一个既有车次
    import sqlite3

    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE points (trip_id TEXT, point_id TEXT, ts_epoch REAL, x REAL, y REAL, "
        "PRIMARY KEY (trip_id, point_id))"
    )
    raw.execute(
        "INSERT INTO points VALUES ('old', 'p1', 100.0, 1.0, 2.0)"
    )
    raw.commit()
    raw.close()

    db = Database(settings)
    try:
        svc = AuditService(db)
        trip = svc.get_trip("old")
        assert trip["version"] == 0
        # 基于版本 0 可成功更正
        resp = svc.correct_points(
            "old",
            {
                "base_version": 0,
                "reason": "误录",
                "items": [{"action": "update", "point_id": "p1",
                           "ts": "1970-01-01T00:02:00Z", "x": 5.0, "y": 6.0}],
            },
        )
        assert resp["version"] == 1
    finally:
        db.close()


# ---------------------------------------------------------------------
# 成功更正：改写
# ---------------------------------------------------------------------

def test_successful_update_returns_new_version_and_affected(client):
    _upload(client)
    r = _correct(
        client, "T", 1, "现场复核坐标录错",
        [{"action": "update", "point_id": "b",
          "ts": "2026-09-30T10:01:30Z", "x": 11.0, "y": 1.0}],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["base_version"] == 1
    assert body["reason"] == "现场复核坐标录错"
    assert len(body["affected"]) == 1
    aff = body["affected"][0]
    assert aff["point_id"] == "b" and aff["action"] == "update"
    assert aff["before"] == {"ts": "2026-09-30T10:01:00Z", "x": 10.0, "y": 0.0}
    assert aff["after"] == {"ts": "2026-09-30T10:01:30Z", "x": 11.0, "y": 1.0}

    trip = client.get("/api/v1/trips/T").json()
    assert trip["version"] == 2
    by_id = {p["point_id"]: p for p in trip["points"]}
    assert by_id["b"]["x"] == 11.0
    assert by_id["b"]["ts"] == "2026-09-30T10:01:30Z"


def test_successful_revoke_removes_point_and_records_it(client):
    _upload(client)
    r = _correct(
        client, "T", 1, "该点为设备漂移误录",
        [{"action": "revoke", "point_id": "c"}],
    )
    assert r.status_code == 200, r.text
    aff = r.json()["affected"][0]
    assert aff["action"] == "revoke" and aff["after"] is None
    assert aff["before"]["ts"] == "2026-09-30T10:02:00Z"

    trip = client.get("/api/v1/trips/T").json()
    assert {p["point_id"] for p in trip["points"]} == {"a", "b"}
    assert trip["version"] == 2


def test_mixed_batch_revoke_and_update(client):
    _upload(client)
    r = _correct(
        client, "T", 1, "一批复核",
        [
            {"action": "revoke", "point_id": "c"},
            {"action": "update", "point_id": "a",
             "ts": "2026-09-30T10:00:00Z", "x": -1.0, "y": -1.0},
        ],
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert {a["point_id"] for a in r.json()["affected"]} == {"c", "a"}
    assert client.get("/api/v1/trips/T").json()["point_count"] == 2


# ---------------------------------------------------------------------
# 整组拒绝：过期版本 / 缺失 / 已撤销 / 非法值 / 冲突
# ---------------------------------------------------------------------

def test_stale_base_version_rejected_no_history(client):
    _upload(client)
    _correct(client, "T", 1, "第一次", [{"action": "revoke", "point_id": "c"}])
    # 重放旧请求（base_version=1）必须整批拒绝
    r = _correct(
        client, "T", 1, "重放",
        [{"action": "update", "point_id": "a",
          "ts": "2026-09-30T10:00:00Z", "x": 9.0, "y": 9.0}],
    )
    assert r.status_code == 409
    assert "版本" in r.json()["detail"]
    # 未生效、无履历追加
    trip = client.get("/api/v1/trips/T").json()
    assert trip["version"] == 2
    assert next(p for p in trip["points"] if p["point_id"] == "a")["x"] == 0.0
    hist = client.get("/api/v1/trips/T/corrections").json()["corrections"]
    assert {x["reason"] for x in hist} == {"第一次"}


def test_missing_point_rejected_whole_batch(client):
    _upload(client)
    r = _correct(
        client, "T", 1, "含不存在点",
        [
            {"action": "revoke", "point_id": "ghost"},
            {"action": "update", "point_id": "a",
             "ts": "2026-09-30T10:00:00Z", "x": 2.0, "y": 2.0},
        ],
    )
    assert r.status_code == 409 and "ghost" in r.json()["detail"]
    # 整批未生效
    trip = client.get("/api/v1/trips/T").json()
    assert trip["version"] == 1
    assert next(p for p in trip["points"] if p["point_id"] == "a")["x"] == 0.0
    assert client.get("/api/v1/trips/T/corrections").json()["corrections"] == []


def test_revoked_point_cannot_be_corrected_again(client):
    _upload(client)
    _correct(client, "T", 1, "撤销 c", [{"action": "revoke", "point_id": "c"}])
    r = _correct(
        client, "T", 2, "再改 c",
        [{"action": "update", "point_id": "c",
          "ts": "2026-09-30T10:02:00Z", "x": 1.0, "y": 1.0}],
    )
    assert r.status_code == 409
    assert "撤销" in r.json()["detail"]
    assert client.get("/api/v1/trips/T").json()["version"] == 2


def test_duplicate_point_id_in_batch_rejected(client):
    _upload(client)
    r = _correct(
        client, "T", 1, "重复",
        [
            {"action": "revoke", "point_id": "b"},
            {"action": "update", "point_id": "b",
             "ts": "2026-09-30T10:01:00Z", "x": 1.0, "y": 1.0},
        ],
    )
    assert r.status_code == 400
    assert client.get("/api/v1/trips/T").json()["version"] == 1


def test_non_finite_and_naive_ts_rejected(client):
    _upload(client)
    for bad_x in ("NaN", "Infinity", "-Infinity"):
        r = _correct(
            client, "T", 1, "坏坐标",
            [{"action": "update", "point_id": "a",
              "ts": "2026-09-30T10:00:00Z", "x": bad_x, "y": 1.0}],
        )
        assert r.status_code in (400, 422), bad_x
    # 不带时区
    r = _correct(
        client, "T", 1, "朴素时间",
        [{"action": "update", "point_id": "a",
          "ts": "2026-09-30T10:00:00", "x": 1.0, "y": 1.0}],
    )
    assert r.status_code == 400
    assert "时区" in r.json()["detail"]
    assert client.get("/api/v1/trips/T").json()["version"] == 1


def test_final_set_ts_conflict_rejected_whole_batch(client):
    _upload(client)
    # 把 b 改成与 c 同刻：最终有效集同刻冲突
    r = _correct(
        client, "T", 1, "撞时刻",
        [{"action": "update", "point_id": "b",
          "ts": "2026-09-30T10:02:00Z", "x": 20.0, "y": 0.0}],
    )
    assert r.status_code == 409
    assert client.get("/api/v1/trips/T").json()["version"] == 1


def test_ts_swap_between_two_points_is_allowed(client):
    """同刻互换：最终集仍唯一，先删后插应能成功。"""
    _upload(client)
    r = _correct(
        client, "T", 1, "两点时刻互换",
        [
            {"action": "update", "point_id": "a",
             "ts": "2026-09-30T10:01:00Z", "x": 0.0, "y": 0.0},
            {"action": "update", "point_id": "b",
             "ts": "2026-09-30T10:00:00Z", "x": 10.0, "y": 0.0},
        ],
    )
    assert r.status_code == 200, r.text
    by_id = {p["point_id"]: p for p in client.get("/api/v1/trips/T").json()["points"]}
    assert by_id["a"]["ts"] == "2026-09-30T10:01:00Z"
    assert by_id["b"]["ts"] == "2026-09-30T10:00:00Z"


def test_revoke_frees_ts_but_id_not_reusable(client):
    _upload(client)
    _correct(client, "T", 1, "撤销 c", [{"action": "revoke", "point_id": "c"}])
    # 撤销后该时刻空闲：新点可占用该时刻（用新 ID）
    r = client.post(
        "/api/v1/trips/T/points",
        json=_batch([("d", "2026-09-30T10:02:00Z", 21.0, 1.0)]),
    )
    assert r.status_code == 200, r.text
    # 已撤销 ID 不得经上传重用
    r = client.post(
        "/api/v1/trips/T/points",
        json=_batch([("c", "2026-09-30T10:05:00Z", 99.0, 99.0)]),
    )
    assert r.status_code == 409
    assert "撤销" in r.json()["detail"]


def test_correction_unknown_trip_404(client):
    r = _correct(
        client, "nope", 0, "无此车",
        [{"action": "revoke", "point_id": "x"}],
    )
    assert r.status_code == 404


def test_correction_schema_rejects_bad_shapes(client):
    _upload(client)
    base = {"base_version": 1, "reason": "r", "items": []}
    # 空 items
    assert client.post("/api/v1/trips/T/corrections", json=base).status_code == 422
    # 未知 action / 缺字段 / 顶层多余字段
    assert client.post(
        "/api/v1/trips/T/corrections",
        json={"base_version": 1, "reason": "r",
              "items": [{"action": "freeze", "point_id": "a"}]},
    ).status_code == 422
    assert client.post(
        "/api/v1/trips/T/corrections",
        json={"base_version": 1, "reason": "r",
              "items": [{"action": "update", "point_id": "a", "x": 1.0}]},
    ).status_code == 422
    assert client.post(
        "/api/v1/trips/T/corrections",
        json={"base_version": 1, "reason": "r", "extra": 1,
              "items": [{"action": "revoke", "point_id": "a"}]},
    ).status_code == 422
    # 空原因
    assert client.post(
        "/api/v1/trips/T/corrections",
        json={"base_version": 1, "reason": "   ",
              "items": [{"action": "revoke", "point_id": "a"}]},
    ).status_code in (400, 422)
    # 版本未变
    assert client.get("/api/v1/trips/T").json()["version"] == 1


# ---------------------------------------------------------------------
# 履历不可改、按车次排列
# ---------------------------------------------------------------------

def test_correction_history_immutable_and_ordered(client):
    _upload(client)
    _correct(
        client, "T", 1, "改 b",
        [{"action": "update", "point_id": "b",
          "ts": "2026-09-30T10:01:00Z", "x": 100.0, "y": 0.0}],
    )
    _correct(client, "T", 2, "撤 c", [{"action": "revoke", "point_id": "c"}])

    body = client.get("/api/v1/trips/T/corrections").json()
    assert body["version"] == 3
    hist = body["corrections"]
    assert [h["version"] for h in hist] == [2, 3]
    h1, h2 = hist
    assert h1["action"] == "update" and h1["point_id"] == "b"
    assert h1["before"]["x"] == 10.0 and h1["after"]["x"] == 100.0
    assert h2["action"] == "revoke" and h2["after"] is None
    assert "corrected_at" in h1 and h1["reason"] == "改 b"

    # 不同车次履历互不混入
    other = client.get("/api/v1/trips/other/corrections")
    assert other.status_code == 404


# ---------------------------------------------------------------------
# 查询立即使用更正后点集；区域修订/插值规则不变
# ---------------------------------------------------------------------

def test_crossings_and_trip_use_corrected_points_immediately(client):
    assert register_square(client, zone_id="z").status_code == 201
    # 横穿 0..100 方块：-50(0s) -> 50(40s) -> 150(80s)，区内 40s
    client.post(
        "/api/v1/trips/T/points",
        json=_batch([
            ("p1", "2026-09-30T10:00:00Z", -50.0, 50.0),
            ("p2", "2026-09-30T10:00:40Z", 50.0, 50.0),
            ("p3", "2026-09-30T10:01:20Z", 150.0, 50.0),
        ]),
    )
    z = client.get("/api/v1/trips/T/crossings?zone_id=z").json()["zones"][0]
    assert z["total_duration_sec"] == 40.0

    # 误录更正：p2 实际在区外（y=500），轨迹变成两条不穿区的折线
    r = _correct(
        client, "T", 1, "p2 坐标录错",
        [{"action": "update", "point_id": "p2",
          "ts": "2026-09-30T10:00:40Z", "x": 50.0, "y": 500.0}],
    )
    assert r.status_code == 200
    z = client.get("/api/v1/trips/T/crossings?zone_id=z").json()["zones"][0]
    assert z["crossed"] is False
    assert z["total_duration_sec"] == 0

    # 撤销中间点后插值规则不变：仅余首尾两点，直线仍穿区 40s
    _correct(client, "T", 2, "撤掉错误的 p2", [{"action": "revoke", "point_id": "p2"}])
    z = client.get("/api/v1/trips/T/crossings?zone_id=z").json()["zones"][0]
    assert z["total_duration_sec"] == 40.0
    # point_count 同步为更正后的有效点数
    assert client.get("/api/v1/trips/T/crossings").json()["point_count"] == 2


def test_revoke_all_points_trip_still_readable(client):
    _upload(client)
    r = _correct(
        client, "T", 1, "全部撤销",
        [{"action": "revoke", "point_id": p} for p, *_ in PTS],
    )
    assert r.status_code == 200
    trip = client.get("/api/v1/trips/T").json()
    assert trip["point_count"] == 0 and trip["points"] == [] and trip["version"] == 2
    # 履历仍在
    assert len(client.get("/api/v1/trips/T/corrections").json()["corrections"]) == 3
    # 撤销后上传新点：版本继续推进
    r = client.post(
        "/api/v1/trips/T/points",
        json=_batch([("n1", "2026-09-30T11:00:00Z", 1.0, 1.0)]),
    )
    assert r.status_code == 200
    assert client.get("/api/v1/trips/T").json()["version"] == 3


# ---------------------------------------------------------------------
# 并发：两个并发更正只有一个成功，另一个收到过期版本
# ---------------------------------------------------------------------

def test_concurrent_corrections_serialized_by_version(client):
    import threading

    _upload(client)
    results = []

    def revoke(pid):
        r = _correct(
            client, "T", 1, f"撤 {pid}",
            [{"action": "revoke", "point_id": pid}],
        )
        results.append((pid, r.status_code))

    t1 = threading.Thread(target=revoke, args=("b",))
    t2 = threading.Thread(target=revoke, args=("c",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    statuses = sorted(code for _, code in results)
    assert statuses == [200, 409]
    assert client.get("/api/v1/trips/T").json()["version"] == 2
    # 只有一个点被撤销，失败方未写履历
    hist = client.get("/api/v1/trips/T/corrections").json()["corrections"]
    assert len(hist) == 1
