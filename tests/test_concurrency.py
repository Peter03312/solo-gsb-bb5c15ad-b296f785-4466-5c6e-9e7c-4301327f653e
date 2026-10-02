"""并发测试：并发登记的修订号全局唯一且连续；查询读不到半次提交。"""
import threading

from tests.conftest import SQUARE


def test_concurrent_registrations_get_unique_monotonic_revisions():
    from app.main import db, service

    n_per_zone = 20
    errors = []

    def register(zone_id: str, start: int) -> None:
        try:
            for i in range(start, start + n_per_zone):
                # 同 ID 反复覆盖，逐步扩大外边界，保证每次内容合法且不同
                size = 100 + i
                service.register_zone(
                    {
                        "zone_id": zone_id,
                        "exterior": [[0, 0], [size, 0], [size, size], [0, size]],
                        "holes": [],
                        "windows": [
                            {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
                        ],
                    }
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=register, args=("zone-x", 0)),
        threading.Thread(target=register, args=("zone-y", 1000)),
        threading.Thread(target=register, args=("zone-z", 2000)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    head = service.current_revision()
    assert head == 3 * n_per_zone

    # 修订号连续无缺号；每个修订号恰好一条不可变记录
    with db.read_view() as conn:
        rows = conn.execute(
            "SELECT revision FROM zone_revisions ORDER BY revision"
        ).fetchall()
    revisions = [r["revision"] for r in rows]
    assert revisions == list(range(1, head + 1))


def test_concurrent_queries_never_see_half_commit(client):
    # 先登记一个基础区并上传轨迹
    r = client.post(
        "/api/v1/zones",
        json={
            "zone_id": "live",
            "exterior": SQUARE,
            "holes": [],
            "windows": [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-02T00:00:00Z"}],
        },
    )
    assert r.status_code == 201
    client.post(
        "/api/v1/trips/live-trip/points",
        json={
            "points": [
                {"point_id": "a", "ts": "2026-09-30T10:00:00Z", "x": -50, "y": 50},
                {"point_id": "b", "ts": "2026-09-30T10:01:20Z", "x": 150, "y": 50},
            ]
        },
    )

    stop = threading.Event()
    failures = []

    def writer() -> None:
        # 反复同 ID 覆盖更新（窗口/几何交替变化）
        i = 0
        while not stop.is_set():
            size = 100 + (i % 5) * 20
            client.post(
                "/api/v1/zones",
                json={
                    "zone_id": "live",
                    "exterior": [[0, 0], [size, 0], [size, size], [0, size]],
                    "holes": [],
                    "windows": [
                        {"start": "2026-09-30T00:00:00Z", "end": "2026-10-02T00:00:00Z"}
                    ],
                },
            )
            i += 1

    def reader() -> None:
        import random

        while not stop.is_set():
            # 最新读：必须始终返回结构完整的结果
            r = client.get("/api/v1/trips/live-trip/crossings?zone_id=live")
            if r.status_code != 200:
                failures.append(("latest", r.status_code, r.text))
                return
            zones = r.json().get("zones")
            if not zones or "total_duration_sec" not in zones[0]:
                failures.append(("latest-payload", r.text))
                return
            # 指定修订号读：任何已存在的修订号都必须可读、结果可复现
            head = client.get("/api/v1/revisions").json()["current_revision"]
            rev = random.randint(1, head)
            r1 = client.get(f"/api/v1/trips/live-trip/crossings?zone_id=live&revision={rev}")
            r2 = client.get(f"/api/v1/trips/live-trip/crossings?zone_id=live&revision={rev}")
            if r1.status_code != 200 or r2.status_code != 200:
                failures.append(("pinned-status", rev, r1.status_code, r2.status_code))
                return
            if r1.json() != r2.json():
                failures.append(("pinned-nondeterministic", rev))
                return

    threads = [threading.Thread(target=writer), threading.Thread(target=reader), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    # 让并发混合跑约 1 秒
    stop.wait(timeout=1.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not failures
