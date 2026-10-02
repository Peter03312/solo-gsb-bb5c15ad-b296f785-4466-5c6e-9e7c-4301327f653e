"""修订版本机制测试：全局递增修订号、不可变历史、按修订号复现穿越核算。"""
from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _crossing_points(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


# 10:00:00 -> 10:01:20 以 2.5m/s 横穿 x=-50..150：在 0..100 方块内为 40 秒
BASIC_POINTS = [
    ("p1", "2026-09-30T10:00:00Z", -50, 50),
    ("p2", "2026-09-30T10:00:40Z", 50, 50),
    ("p3", "2026-09-30T10:01:20Z", 150, 50),
]


def test_register_response_carries_globally_increasing_revision(client):
    r1 = register_square(client, zone_id="rev-a")
    assert r1.status_code == 201
    assert r1.json()["revision"] == 1
    r2 = register_square(client, zone_id="rev-b")
    assert r2.json()["revision"] == 2
    # 同 ID 覆盖更新也产生全局递增新号
    r3 = register_square(client, zone_id="rev-a")
    assert r3.json()["revision"] == 3
    assert r3.json()["status"] == "updated"


def test_zone_immutable_history(client):
    register_square(client, zone_id="h")
    register_square(client, zone_id="h", exterior=[[0, 0], [200, 0], [200, 200], [0, 200]])
    # 历史接口返回两版（升序）
    r = client.get("/api/v1/zones/h/revisions")
    revs = r.json()["revisions"]
    assert [x["revision"] for x in revs] == [1, 2]

    # 按修订号取不可变版本
    v1 = client.get("/api/v1/zones/h?revision=1").json()
    v2 = client.get("/api/v1/zones/h?revision=2").json()
    assert v1["revision"] == 1
    assert v1["exterior"][1] == [100, 0]
    assert v2["exterior"][1] == [200, 0]
    # 省略 revision 仍是最新
    latest = client.get("/api/v1/zones/h").json()
    assert latest["revision"] == 2


def test_repro_crossing_at_revision(client):
    # rev1：0..100 方块
    register_square(client, zone_id="geo")
    # rev2：外边界扩大到 0..200（同 ID 覆盖）
    register_square(client, zone_id="geo", exterior=[[0, 0], [200, 0], [200, 200], [0, 200]])
    client.post("/api/v1/trips/T/points", json=_crossing_points(BASIC_POINTS))

    # 最新配置：x=0..150 段计入（轨迹终点 x=150 在区内），共 60s
    latest = client.get("/api/v1/trips/T/crossings?zone_id=geo").json()
    assert "revision" not in latest  # 未指定修订号：响应语义不变
    assert latest["zones"][0]["total_duration_sec"] == 60.0

    # 按 rev1 复现：仍是 40s
    old = client.get("/api/v1/trips/T/crossings?zone_id=geo&revision=1").json()
    assert old["revision"] == 1  # 回填所用修订号
    z = old["zones"][0]
    assert z["total_duration_sec"] == 40.0
    assert z["segments"][0]["start"] == "2026-09-30T10:00:20Z"
    assert z["segments"][0]["end"] == "2026-09-30T10:01:00Z"

    # rev2 与最新一致
    at2 = client.get("/api/v1/trips/T/crossings?zone_id=geo&revision=2").json()
    assert at2["revision"] == 2
    assert at2["zones"][0]["total_duration_sec"] == 60.0


def test_window_change_reproducible(client):
    # rev1：全天生效 -> 40s
    register_square(client, zone_id="w")
    # rev2：仅 10:00:20~10:00:50 生效 -> 30s
    register_square(
        client,
        zone_id="w",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}],
    )
    client.post("/api/v1/trips/T/points", json=_crossing_points(BASIC_POINTS))

    r1 = client.get("/api/v1/trips/T/crossings?zone_id=w&revision=1").json()
    assert r1["zones"][0]["total_duration_sec"] == 40.0
    r2 = client.get("/api/v1/trips/T/crossings?zone_id=w&revision=2").json()
    assert r2["zones"][0]["total_duration_sec"] == 30.0


def test_revision_out_of_range_errors(client):
    register_square(client, zone_id="x")
    client.post("/api/v1/trips/T/points", json=_crossing_points(BASIC_POINTS))

    # 超出当前范围：明确 404
    r = client.get("/api/v1/trips/T/crossings?revision=999")
    assert r.status_code == 404
    assert "999" in r.json()["detail"] and "修订号" in r.json()["detail"]

    r = client.get("/api/v1/zones/x?revision=999")
    assert r.status_code == 404

    # 非法修订号：422（FastAPI 路径层 ge=1）或 400（服务层类型校验）
    assert client.get("/api/v1/trips/T/crossings?revision=0").status_code in (400, 422)
    assert client.get("/api/v1/trips/T/crossings?revision=abc").status_code in (400, 422)


def test_zone_not_registered_at_revision_excluded(client):
    # rev1 只有 a；rev2 才登记 b
    register_square(client, zone_id="a")
    register_square(client, zone_id="b")
    client.post("/api/v1/trips/T/points", json=_crossing_points(BASIC_POINTS))

    # 不指定 zone_id：rev1 快照只含 a，b 不参与核算
    snap = client.get("/api/v1/trips/T/crossings?revision=1").json()
    assert {z["zone_id"] for z in snap["zones"]} == {"a"}
    assert snap["revision"] == 1

    listed = client.get("/api/v1/zones?revision=1").json()
    assert {z["zone_id"] for z in listed["zones"]} == {"a"}
    assert listed["revision"] == 1

    # 指定 rev1 中尚未登记的 b：明确报错
    r = client.get("/api/v1/trips/T/crossings?zone_id=b&revision=1")
    assert r.status_code == 404
    r = client.get("/api/v1/zones/b?revision=1")
    assert r.status_code == 404

    # 最新配置两个区都在
    latest = client.get("/api/v1/trips/T/crossings").json()
    assert {z["zone_id"] for z in latest["zones"]} == {"a", "b"}


def test_revision_history_endpoint_and_current(client):
    register_square(client, zone_id="a")
    register_square(client, zone_id="b")
    register_square(client, zone_id="a")

    r = client.get("/api/v1/revisions")
    body = r.json()
    assert body["current_revision"] == 3
    # 倒序
    assert [x["revision"] for x in body["revisions"]] == [3, 2, 1]
    assert body["revisions"][0]["zone_id"] == "a"

    r = client.get("/api/v1/zones/no-such/revisions")
    assert r.status_code == 404


def test_pinned_query_empty_trip_still_reports_revision(client):
    register_square(client, zone_id="a")
    client.post(
        "/api/v1/trips/empty/points",
        json=_crossing_points([("only", "2026-09-30T10:00:00Z", 50, 50)]),
    )
    r = client.get("/api/v1/trips/empty/crossings?revision=1").json()
    assert r["revision"] == 1
    assert r["zones"] == []
