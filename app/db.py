"""SQLite 持久层：危险区不可变修订版本、生效时段、轨迹点与现场点更正。

修订模型（区域）
----------------
- 每次登记（含同 ID 覆盖更新）向 ``zone_revisions`` 追加一行不可变记录，
  ``revision`` 为 SQLite AUTOINCREMENT 分配的全局递增修订号（登记事务内原子产生）。
- 截至修订号 R，每个区取其 ``revision <= R`` 的最后一行；指定版本中尚未登记的区无行、不参与核算。
- 旧库（zones / zone_windows 两张表）首次启动时一次性迁移为基线版本 revision=1..，
  迁移后删除旧表；不推断迁移前历史。

轨迹版本与更正（车次）
----------------------
- ``trip_versions`` 每车次一行记录当前轨迹版本：升级前已有点的车次首次启动时迁移为
  版本 0 基线；之后每批真正录入新点或每批成功更正各推进一次，纯幂等重传不推进。
- ``revoked_points`` 永久登记已撤销点（撤销即从 points 有效集移除），已撤销 ID 不得经上传重用。
- ``point_corrections`` 为只追加、不可改的前后值履历（action=update/revoke、前后时刻坐标、
  生效版本与更正时间），仅在整组更正成功的同一事务内写入。
- ``trip_version_log`` / ``trip_point_versions`` 原子保留每个轨迹版本的**全量有效点集快照**：
  每批真正录入新点（cause=points）或每批成功更正（cause=correction）在版本推进的同一事务内
  追加一行版本日志与该版本全部有效点；纯幂等重传不产生日志或快照。升级时已有车次只以当时当前
  版本与点集登记为最早可追溯基线（cause=baseline），不推造更早轨迹。穿越查询可按任一已保留
  版本复现当时点集。

并发
----
- 服务进程内以一把可重入锁串行化对共享连接的访问；写事务使用 ``BEGIN IMMEDIATE``，
  多进程场景下由 SQLite 写锁保证修订号严格串行。
- 查询通过只读快照（``BEGIN``/``COMMIT``）一次性读完全部版本行，不会读到半次提交。

- 所有写操作在单个事务中完成，批量失败不写入部分数据。
- 重启后数据保留（DZ_DB_PATH 指向持久文件）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import Settings

_SCHEMA = """
-- 危险区不可变修订历史：每次登记/覆盖更新追加一行，永不修改、不删除
CREATE TABLE IF NOT EXISTS zone_revisions (
    revision     INTEGER PRIMARY KEY AUTOINCREMENT,  -- 全局递增修订号
    zone_id      TEXT NOT NULL,
    exterior     TEXT NOT NULL,          -- GeoJSON-like [[x,y],...]（闭合）
    holes        TEXT NOT NULL,          -- [[[x,y],...], ...]（各环闭合）
    windows      TEXT NOT NULL,          -- [{start_epoch,end_epoch,start_iso,end_iso}, ...]
    created_at   TEXT NOT NULL,          -- 该 zone_id 首次登记时间（沿用，不随覆盖更新变化）
    registered_at TEXT NOT NULL          -- 本次登记时间
);
CREATE INDEX IF NOT EXISTS idx_zone_revisions_zone_rev
    ON zone_revisions(zone_id, revision);

CREATE TABLE IF NOT EXISTS points (
    trip_id    TEXT NOT NULL,
    point_id   TEXT NOT NULL,
    ts_epoch   REAL NOT NULL,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    PRIMARY KEY (trip_id, point_id)
);
-- 同车次同刻必须是同一个点：部分索引防止 (trip, ts) 重复
CREATE UNIQUE INDEX IF NOT EXISTS idx_points_trip_ts
    ON points(trip_id, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_points_trip ON points(trip_id, ts_epoch);

-- 车次轨迹版本：每车次一行。既有车次（本表功能上线前已有点）首次启动时迁移为 version=0 基线；
-- 此后每批真正录入新点或每批成功更正各推进一次，纯幂等重传不推进。
CREATE TABLE IF NOT EXISTS trip_versions (
    trip_id    TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,          -- 当前轨迹版本号（基线为 0）
    updated_at TEXT NOT NULL
);

-- 已撤销点：撤销即从 points 有效集移除并在此永久登记；已撤销 ID 不得经上传重用。
CREATE TABLE IF NOT EXISTS revoked_points (
    trip_id    TEXT NOT NULL,
    point_id   TEXT NOT NULL,
    version    INTEGER NOT NULL,          -- 撤销所在批次成功后的轨迹版本
    reason     TEXT NOT NULL,
    revoked_at TEXT NOT NULL,
    PRIMARY KEY (trip_id, point_id)
);

-- 点更正履历：只追加、不可改。每次成功更正对每个受影响点追加一行（带前后值）。
CREATE TABLE IF NOT EXISTS point_corrections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id        TEXT NOT NULL,
    version        INTEGER NOT NULL,       -- 该批更正成功后的新版本号
    reason         TEXT NOT NULL,
    point_id       TEXT NOT NULL,
    action         TEXT NOT NULL,          -- update | revoke
    before_ts_epoch REAL NOT NULL,
    before_x       REAL NOT NULL,
    before_y       REAL NOT NULL,
    after_ts_epoch REAL,                   -- revoke 时为 NULL
    after_x        REAL,
    after_y        REAL,
    corrected_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_point_corrections_trip
    ON point_corrections(trip_id, version, id);

-- 车次轨迹版本流水：每个真实存在的轨迹版本一行（基线 / 新点批次 / 更正批次）。
-- 只追加、不可改；纯幂等重传不产生行。升级前车次的最早一行为其当时当前版本（基线）。
CREATE TABLE IF NOT EXISTS trip_version_log (
    trip_id    TEXT NOT NULL,
    version    INTEGER NOT NULL,          -- 该版本号（基线车次为其当时当前版本）
    cause      TEXT NOT NULL,             -- baseline | points | correction
    created_at TEXT NOT NULL,
    PRIMARY KEY (trip_id, version)
);

-- 各轨迹版本的全量有效点集快照：版本产生的同一事务内写入，足以原样复原该版本。
-- 与 points 当前有效集独立，不可改、不随后续更正删除。
CREATE TABLE IF NOT EXISTS trip_point_versions (
    trip_id    TEXT NOT NULL,
    version    INTEGER NOT NULL,
    point_id   TEXT NOT NULL,
    ts_epoch   REAL NOT NULL,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    PRIMARY KEY (trip_id, version, point_id)
);
CREATE INDEX IF NOT EXISTS idx_trip_point_versions_lookup
    ON trip_point_versions(trip_id, version, ts_epoch, point_id);
"""

_LEGACY_TABLES = ("zones", "zone_windows")


def _connect(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=30,
        isolation_level=None,  # 显式事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 可重入锁：服务进程内串行化共享连接上的所有访问，杜绝读到半次提交
        self._lock = threading.RLock()
        self._conn = _connect(settings)
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate_legacy_zones(self._conn)
            self._baseline_existing_trips(self._conn)

    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return cur.fetchone() is not None

    def _migrate_legacy_zones(self, conn: sqlite3.Connection) -> None:
        """旧库现存区域一次性迁移为基线版本（revision 1..N，按 zone_id 排序）。

        幂等：仅当旧 zones 表存在时执行；迁移完成后删除旧表。不推断迁移前历史。
        """
        if not self._table_exists(conn, "zones"):
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            legacy_rows = list(
                conn.execute("SELECT zone_id, exterior, holes, created_at FROM zones ORDER BY zone_id")
            )
            for row in legacy_rows:
                legacy_windows = list(
                    conn.execute(
                        "SELECT start_epoch, end_epoch, start_iso, end_iso "
                        "FROM zone_windows WHERE zone_id = ? ORDER BY start_epoch",
                        (row["zone_id"],),
                    )
                )
                windows = [
                    {
                        "start_epoch": w["start_epoch"],
                        "end_epoch": w["end_epoch"],
                        "start_iso": w["start_iso"],
                        "end_iso": w["end_iso"],
                    }
                    for w in legacy_windows
                ]
                # 基线版本：created_at / registered_at 均沿用旧 created_at
                conn.execute(
                    "INSERT INTO zone_revisions"
                    "(zone_id, exterior, holes, windows, created_at, registered_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        row["zone_id"],
                        row["exterior"],
                        row["holes"],
                        json.dumps(windows),
                        row["created_at"],
                        row["created_at"],
                    ),
                )
            for name in _LEGACY_TABLES:
                conn.execute(f"DROP TABLE {name}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _baseline_existing_trips(self, conn: sqlite3.Connection) -> None:
        """升级时把已有车次登记为**最早可追溯基线**，并原子保留该版本全量点集快照。

        两类既有车次（均不推造更早轨迹）：

        1. 无 ``trip_versions`` 行的旧库车次（仅有 points）：以**版本 0**与当时点集为基线；
        2. 已有 ``trip_versions`` 但在版本快照功能上线前产生的车次：以其**当时当前版本**
           与点集为基线（版本号保持不变）。

        幂等：已在 ``trip_version_log`` 中登记的车次版本不重复写入；新功能上线后首次上传
        的车次在上传事务内直接建立版本 1 与快照，不经过此路径。
        """
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 1) 旧库车次（无版本行）登记为版本 0
            conn.execute(
                "INSERT OR IGNORE INTO trip_versions(trip_id, version, updated_at) "
                "SELECT DISTINCT p.trip_id, 0, ? FROM points p "
                "WHERE NOT EXISTS (SELECT 1 FROM trip_versions tv WHERE tv.trip_id = p.trip_id)",
                (now_iso,),
            )

            # 2) 尚无版本流水的车次：以当时当前版本与有效点集作为基线快照
            missing = list(
                conn.execute(
                    "SELECT tv.trip_id, tv.version FROM trip_versions tv "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM trip_version_log vl "
                    "  WHERE vl.trip_id = tv.trip_id AND vl.version = tv.version)"
                )
            )
            for trip_id, version in missing:
                rows = self._list_trip_points(conn, trip_id)
                self._insert_version_snapshot(
                    conn, trip_id, int(version), "baseline", now_iso, list(rows)
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _insert_version_snapshot(
        conn: sqlite3.Connection,
        trip_id: str,
        version: int,
        cause: str,
        created_iso: str,
        point_rows: List["sqlite3.Row"] | List[Tuple[str, float, float, float]],
    ) -> None:
        """在给定写事务内追加版本流水与该版本**全量有效点集**快照（原子、不可改）。

        point_rows 可为 sqlite3.Row 序列（含 point_id/ts_epoch/x/y）或
        (point_id, ts_epoch, x, y) 元组序列。
        """
        conn.execute(
            "INSERT INTO trip_version_log(trip_id, version, cause, created_at) "
            "VALUES (?,?,?,?)",
            (trip_id, version, cause, created_iso),
        )
        normalized = [
            (r["point_id"], r["ts_epoch"], r["x"], r["y"])
            if isinstance(r, sqlite3.Row)
            else (r[0], r[1], r[2], r[3])
            for r in point_rows
        ]
        conn.executemany(
            "INSERT INTO trip_point_versions"
            "(trip_id, version, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?,?)",
            [(trip_id, version, pid, ts, x, y) for pid, ts, x, y in normalized],
        )

    @staticmethod
    def ensure_legacy_baseline(
        conn: sqlite3.Connection, trip_id: str, created_iso: str
    ) -> bool:
        """兼容直接通过 SQL 落库的旧数据：无版本流水但有点时，幂等补齐版本 0 基线。

        必须在写事务内调用。已存在版本流水（例如同进程内刚补齐过）则不重复写入。
        返回是否为本次新建基线（调用方可据此把当前版本视作 0）。
        """
        if Database._version_exists(conn, trip_id, 0):
            return False
        rows = Database._list_trip_points(conn, trip_id)
        has_revoked = conn.execute(
            "SELECT 1 FROM revoked_points WHERE trip_id = ? LIMIT 1", (trip_id,)
        ).fetchone() is not None
        if not rows and not has_revoked:
            return False
        Database._insert_version_snapshot(conn, trip_id, 0, "baseline", created_iso, rows)
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：进程锁 + BEGIN IMMEDIATE（多进程时由 SQLite 写锁串行化）。"""
        conn = self._conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def read_view(self) -> Iterator[sqlite3.Connection]:
        """只读快照事务：块内所有读取自同一快照，并发提交在块内不可见。"""
        conn = self._conn
        with self._lock:
            conn.execute("BEGIN")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ----- 修订号 ----------------------------------------------------------

    def current_revision(self) -> int:
        """当前全局修订号（尚无任何登记时为 0）。"""
        with self._lock:
            cur = self._conn.execute("SELECT COALESCE(MAX(revision), 0) AS r FROM zone_revisions")
            return int(cur.fetchone()["r"])

    # ----- 危险区修订版本 --------------------------------------------------

    def insert_zone_revision(
        self,
        conn: sqlite3.Connection,
        zone_id: str,
        exterior: List[List[float]],
        holes: List[List[List[float]]],
        windows: List[Dict[str, Any]],
        created_iso: str,
        registered_iso: str,
    ) -> int:
        """在给定写事务内追加一条不可变修订记录，返回由 SQLite 分配的全局修订号。"""
        cur = conn.execute(
            "INSERT INTO zone_revisions"
            "(zone_id, exterior, holes, windows, created_at, registered_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                zone_id,
                json.dumps(exterior),
                json.dumps(holes),
                json.dumps(windows),
                created_iso,
                registered_iso,
            ),
        )
        return int(cur.lastrowid)

    def first_zone_revision(self, conn: sqlite3.Connection, zone_id: str) -> Optional[sqlite3.Row]:
        cur = conn.execute(
            "SELECT created_at FROM zone_revisions WHERE zone_id = ? ORDER BY revision LIMIT 1",
            (zone_id,),
        )
        return cur.fetchone()

    def latest_zone_row(self, zone_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._latest_zone_row(self._conn, zone_id)

    @staticmethod
    def _latest_zone_row(conn: sqlite3.Connection, zone_id: str) -> Optional[sqlite3.Row]:
        cur = conn.execute(
            "SELECT * FROM zone_revisions WHERE zone_id = ? ORDER BY revision DESC LIMIT 1",
            (zone_id,),
        )
        return cur.fetchone()

    @staticmethod
    def _zone_row_at(conn: sqlite3.Connection, zone_id: str, revision: int) -> Optional[sqlite3.Row]:
        cur = conn.execute(
            "SELECT * FROM zone_revisions WHERE zone_id = ? AND revision <= ? "
            "ORDER BY revision DESC LIMIT 1",
            (zone_id, revision),
        )
        return cur.fetchone()

    def list_latest_zone_rows(self) -> List[sqlite3.Row]:
        with self._lock:
            return self._list_latest_zone_rows(self._conn)

    @staticmethod
    def _list_latest_zone_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT zr.* FROM zone_revisions zr "
                "JOIN (SELECT zone_id, MAX(revision) AS mr FROM zone_revisions GROUP BY zone_id) m "
                "ON zr.zone_id = m.zone_id AND zr.revision = m.mr "
                "ORDER BY zr.zone_id"
            )
        )

    @staticmethod
    def _list_zone_rows_at(conn: sqlite3.Connection, revision: int) -> List[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT zr.* FROM zone_revisions zr "
                "JOIN (SELECT zone_id, MAX(revision) AS mr FROM zone_revisions "
                "      WHERE revision <= ? GROUP BY zone_id) m "
                "ON zr.zone_id = m.zone_id AND zr.revision = m.mr "
                "ORDER BY zr.zone_id",
                (revision,),
            )
        )

    def list_revision_rows(self, limit: int = 500) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT revision, zone_id, created_at, registered_at FROM zone_revisions "
                    "ORDER BY revision DESC LIMIT ?",
                    (int(limit),),
                )
            )

    def list_zone_revision_rows(self, zone_id: str) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT revision, zone_id, created_at, registered_at FROM zone_revisions "
                    "WHERE zone_id = ? ORDER BY revision",
                    (zone_id,),
                )
            )

    # ----- 轨迹点 ----------------------------------------------------------

    def get_point(self, trip_id: str, point_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM points WHERE trip_id = ? AND point_id = ?",
                (trip_id, point_id),
            )
            return cur.fetchone()

    def find_point_at_ts(self, trip_id: str, ts_epoch: float) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM points WHERE trip_id = ? AND ts_epoch = ?",
                (trip_id, ts_epoch),
            )
            return cur.fetchone()

    def list_trip_points(self, trip_id: str) -> List[sqlite3.Row]:
        with self._lock:
            return self._list_trip_points(self._conn, trip_id)

    @staticmethod
    def _list_trip_points(conn: sqlite3.Connection, trip_id: str) -> List[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM points WHERE trip_id = ? ORDER BY ts_epoch, point_id",
                (trip_id,),
            )
        )

    def count_trip_points(self, trip_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS c FROM points WHERE trip_id = ?", (trip_id,)
            )
            return int(cur.fetchone()["c"])

    def insert_points(
        self, conn: sqlite3.Connection, rows: List[Tuple[str, str, float, float, float]]
    ) -> None:
        conn.executemany(
            "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
            rows,
        )

    def delete_points(self, conn: sqlite3.Connection, trip_id: str, point_ids: List[str]) -> None:
        conn.executemany(
            "DELETE FROM points WHERE trip_id = ? AND point_id = ?",
            [(trip_id, pid) for pid in point_ids],
        )

    # ----- 车次轨迹版本 ------------------------------------------------------

    @staticmethod
    def _trip_version_row(conn: sqlite3.Connection, trip_id: str) -> Optional[sqlite3.Row]:
        cur = conn.execute(
            "SELECT * FROM trip_versions WHERE trip_id = ?", (trip_id,)
        )
        return cur.fetchone()

    def get_trip_version(self, trip_id: str) -> Optional[int]:
        """当前轨迹版本号；车次从未存在时为 None。"""
        with self._lock:
            row = self._trip_version_row(self._conn, trip_id)
            return None if row is None else int(row["version"])

    @staticmethod
    def upsert_trip_version(
        conn: sqlite3.Connection, trip_id: str, version: int, updated_iso: str
    ) -> None:
        conn.execute(
            "INSERT INTO trip_versions(trip_id, version, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(trip_id) DO UPDATE SET version=excluded.version, "
            "updated_at=excluded.updated_at",
            (trip_id, version, updated_iso),
        )

    # ----- 已撤销点 ----------------------------------------------------------

    @staticmethod
    def _revoked_row(conn: sqlite3.Connection, trip_id: str, point_id: str) -> Optional[sqlite3.Row]:
        cur = conn.execute(
            "SELECT * FROM revoked_points WHERE trip_id = ? AND point_id = ?",
            (trip_id, point_id),
        )
        return cur.fetchone()

    def is_point_revoked(self, trip_id: str, point_id: str) -> bool:
        with self._lock:
            return self._revoked_row(self._conn, trip_id, point_id) is not None

    def list_revoked_ids(self, conn: sqlite3.Connection, trip_id: str) -> List[str]:
        return [
            r["point_id"]
            for r in conn.execute(
                "SELECT point_id FROM revoked_points WHERE trip_id = ? ORDER BY point_id",
                (trip_id,),
            )
        ]

    @staticmethod
    def insert_revoked_points(
        conn: sqlite3.Connection,
        trip_id: str,
        point_ids: List[str],
        version: int,
        reason: str,
        revoked_iso: str,
    ) -> None:
        conn.executemany(
            "INSERT INTO revoked_points(trip_id, point_id, version, reason, revoked_at) "
            "VALUES (?,?,?,?,?)",
            [(trip_id, pid, version, reason, revoked_iso) for pid in point_ids],
        )

    # ----- 点更正履历 --------------------------------------------------------

    @staticmethod
    def insert_point_corrections(
        conn: sqlite3.Connection, rows: List[Tuple[Any, ...]]
    ) -> None:
        conn.executemany(
            "INSERT INTO point_corrections"
            "(trip_id, version, reason, point_id, action, "
            " before_ts_epoch, before_x, before_y, after_ts_epoch, after_x, after_y, corrected_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    @staticmethod
    def list_point_correction_rows(
        conn: sqlite3.Connection, trip_id: str
    ) -> List[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM point_corrections WHERE trip_id = ? "
                "ORDER BY version, id",
                (trip_id,),
            )
        )

    # ----- 轨迹版本快照 ------------------------------------------------------

    @staticmethod
    def _trip_version_info(
        conn: sqlite3.Connection, trip_id: str
    ) -> Tuple[Optional[int], Optional[int]]:
        """返回 (最早可追溯基线版本, 当前版本)；无该车次（无当前版本且无任何版本流水）为 (None, None)。"""
        cur = conn.execute(
            "SELECT MIN(version) AS baseline, MAX(version) AS head "
            "FROM trip_version_log WHERE trip_id = ?",
            (trip_id,),
        )
        row = cur.fetchone()
        if row is None or row["baseline"] is None:
            return None, None
        return int(row["baseline"]), int(row["head"])

    @staticmethod
    def _version_exists(conn: sqlite3.Connection, trip_id: str, version: int) -> bool:
        cur = conn.execute(
            "SELECT 1 FROM trip_version_log WHERE trip_id = ? AND version = ?",
            (trip_id, version),
        )
        return cur.fetchone() is not None

    @staticmethod
    def _list_trip_point_version_rows(
        conn: sqlite3.Connection, trip_id: str, version: int
    ) -> List[sqlite3.Row]:
        """取某轨迹版本保留下来的全量有效点集（按时刻排序，足以复原该版本）。"""
        return list(
            conn.execute(
                "SELECT point_id, ts_epoch, x, y FROM trip_point_versions "
                "WHERE trip_id = ? AND version = ? ORDER BY ts_epoch, point_id",
                (trip_id, version),
            )
        )

    @staticmethod
    def list_trip_version_log_rows(
        conn: sqlite3.Connection, trip_id: str
    ) -> List[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM trip_version_log WHERE trip_id = ? ORDER BY version",
                (trip_id,),
            )
        )

    def clear_all_for_tests(self) -> None:
        """测试专用：清空业务数据并重置修订号序列。"""
        with self.transaction() as conn:
            conn.execute("DELETE FROM points")
            conn.execute("DELETE FROM zone_revisions")
            conn.execute("DELETE FROM trip_versions")
            conn.execute("DELETE FROM revoked_points")
            conn.execute("DELETE FROM point_corrections")
            conn.execute("DELETE FROM trip_version_log")
            conn.execute("DELETE FROM trip_point_versions")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='zone_revisions'")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='point_corrections'")
