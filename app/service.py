"""业务服务：危险区登记（全局修订号）、轨迹点批量提交、穿越审计计算。"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import Polygon

from .db import Database
from .geometry import GeometryError, build_polygon, merge_intervals, segment_inside_params
from .timeutils import TimeError, clip_intervals_halfopen, merge_windows, parse_utc, validate_window


class ConflictError(Exception):
    """内容冲突（HTTP 409）。"""


class ValidationError(Exception):
    """语义校验失败（HTTP 400）。"""


class NotFoundError(Exception):
    """资源不存在或修订号超出范围（HTTP 404）。"""


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_revision_param(raw: Any) -> int:
    """校验查询参数 revision：必须是 >=1 的整数。"""
    if isinstance(raw, bool):
        raise ValidationError(f"revision 必须是正整数，收到: {raw!r}")
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw).strip()
        try:
            value = int(text)
        except (TypeError, ValueError):
            raise ValidationError(f"revision 必须是正整数，收到: {raw!r}")
    if value < 1:
        raise ValidationError(f"revision 必须是正整数（>=1），收到: {value}")
    return value


def _row_to_zone(row: Any, *, include_revision: bool = True) -> Dict[str, Any]:
    windows_raw = json.loads(row["windows"])
    body: Dict[str, Any] = {
        "zone_id": row["zone_id"],
        "exterior": json.loads(row["exterior"]),
        "holes": json.loads(row["holes"]),
        "windows": [
            {"start": w["start_iso"], "end": w["end_iso"]} for w in windows_raw
        ],
        "created_at": row["created_at"],
        "registered_at": row["registered_at"],
    }
    if include_revision:
        body["revision"] = int(row["revision"])
    return body


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.min_duration = db.settings.min_duration

    # ------------------------------------------------------------------
    # 修订号
    # ------------------------------------------------------------------

    def current_revision(self) -> int:
        return self.db.current_revision()

    def _resolve_revision(self, raw: Any) -> int:
        """校验并解析调用方指定的修订号；超出当前范围明确报错 404。"""
        revision = _validate_revision_param(raw)
        head = self.db.current_revision()
        if revision > head:
            raise NotFoundError(
                f"修订号 {revision} 超出当前范围：当前最新修订号为 {head}"
                + ("（尚无任何区域登记）" if head == 0 else "")
            )
        return revision

    # ------------------------------------------------------------------
    # 危险区登记
    # ------------------------------------------------------------------

    def register_zone(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        zone_id = payload.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise ValidationError("zone_id 必须是非空字符串")
        zone_id = zone_id.strip()

        exterior = payload.get("exterior")
        holes = payload.get("holes") or []
        if not isinstance(holes, list):
            raise ValidationError("holes 必须是环列表")
        try:
            polygon = build_polygon(exterior, holes)
        except GeometryError as exc:
            raise ValidationError(str(exc)) from exc

        raw_windows = payload.get("windows")
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ValidationError("windows 必须是非空左闭右开时段列表 [{start, end}]")
        windows: List[Dict[str, Any]] = []
        for idx, w in enumerate(raw_windows):
            if not isinstance(w, dict):
                raise ValidationError(f"windows[{idx}] 必须是对象")
            try:
                start = parse_utc(w.get("start"), field=f"windows[{idx}].start")
                end = parse_utc(w.get("end"), field=f"windows[{idx}].end")
                validate_window(start, end)
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc
            windows.append(
                {
                    "start_epoch": start.timestamp(),
                    "end_epoch": end.timestamp(),
                    "start_iso": _iso_z(start),
                    "end_iso": _iso_z(end),
                }
            )

        now_iso = _iso_z(datetime.now(timezone.utc))
        # 单事务：读取既有首版 created_at + 追加不可变版本 + 分配全局修订号，原子提交
        with self.db.transaction() as conn:
            first = self.db.first_zone_revision(conn, zone_id)
            created = first is None
            created_iso = now_iso if created else first["created_at"]
            revision = self.db.insert_zone_revision(
                conn,
                zone_id,
                [list(c) for c in polygon.exterior.coords],
                [[list(c) for c in ring.coords] for ring in polygon.interiors],
                windows,
                created_iso,
                now_iso,
            )

        return {
            "zone_id": zone_id,
            "revision": revision,
            "status": "created" if created else "updated",
            "num_holes": len(polygon.interiors),
            "num_windows": len(windows),
        }

    def get_zone(self, zone_id: str, revision_raw: Any = None) -> Optional[Dict[str, Any]]:
        if revision_raw is None:
            row = self.db.latest_zone_row(zone_id)
            if row is None:
                return None
            return _row_to_zone(row)

        revision = self._resolve_revision(revision_raw)
        with self.db.read_view() as conn:
            row = self.db._zone_row_at(conn, zone_id, revision)
        if row is None:
            # 指定版本中该区尚未登记
            raise NotFoundError(
                f"修订号 {revision} 的配置中危险区尚未登记: {zone_id}"
            )
        return _row_to_zone(row)

    def list_zones(self, revision_raw: Any = None) -> Dict[str, Any]:
        if revision_raw is None:
            rows = self.db.list_latest_zone_rows()
            return {"zones": [_row_to_zone(r) for r in rows]}

        revision = self._resolve_revision(revision_raw)
        with self.db.read_view() as conn:
            rows = self.db._list_zone_rows_at(conn, revision)
        return {
            "revision": revision,
            "zones": [_row_to_zone(r) for r in rows],
        }

    def list_revisions(self, zone_id: Optional[str] = None, limit: int = 500) -> Dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 10000:
            raise ValidationError("limit 必须是 1..10000 的整数")
        if zone_id is None:
            rows = self.db.list_revision_rows(limit=limit)
            payload = {
                "current_revision": self.db.current_revision(),
                "revisions": [self._revision_brief(r) for r in rows],
            }
            return payload
        # 按区查看：区当前必须存在（历史曾登记即视为存在，版本不可删除）
        existing = self.db.latest_zone_row(zone_id)
        if existing is None:
            raise NotFoundError(f"危险区不存在: {zone_id}")
        rows = self.db.list_zone_revision_rows(zone_id)
        return {
            "zone_id": zone_id,
            "revisions": [self._revision_brief(r) for r in rows],
        }

    @staticmethod
    def _revision_brief(row: Any) -> Dict[str, Any]:
        return {
            "revision": int(row["revision"]),
            "zone_id": row["zone_id"],
            "created_at": row["created_at"],
            "registered_at": row["registered_at"],
        }

    # ------------------------------------------------------------------
    # 轨迹点批量提交（幂等 / 冲突）
    # ------------------------------------------------------------------

    def submit_points(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        points = payload.get("points")
        if not isinstance(points, list) or not points:
            raise ValidationError("points 必须是非空轨迹点列表")

        # ---- 批内解析与校验 ----
        parsed: List[Tuple[str, float, float, float, str]] = []
        # point_id -> (ts,x,y,ts_iso)；ts -> point_id，用于批内一致性检查
        seen_ids: Dict[str, Tuple[float, float, float, str]] = {}
        seen_ts: Dict[float, str] = {}

        for idx, item in enumerate(points):
            if not isinstance(item, dict):
                raise ValidationError(f"points[{idx}] 必须是对象")
            point_id = item.get("point_id")
            if not isinstance(point_id, str) or not point_id.strip():
                raise ValidationError(f"points[{idx}].point_id 必须是非空字符串")
            point_id = point_id.strip()

            try:
                dt = parse_utc(item.get("ts"), field=f"points[{idx}].ts")
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc

            x_raw, y_raw = item.get("x"), item.get("y")
            if isinstance(x_raw, bool) or isinstance(y_raw, bool):
                raise ValidationError(f"points[{idx}] 坐标必须是数值")
            try:
                x, y = float(x_raw), float(y_raw)
            except (TypeError, ValueError):
                raise ValidationError(
                    f"points[{idx}] 坐标必须是数值: x={x_raw!r}, y={y_raw!r}"
                )

            if not (math.isfinite(x) and math.isfinite(y)):
                raise ValidationError(
                    f"points[{idx}] 坐标必须有限，禁止 NaN/Infinity: x={x_raw!r}, y={y_raw!r}"
                )

            ts_epoch = dt.timestamp()
            sig = (ts_epoch, x, y)

            # 同 point_id 批内重复：内容一致幂等，不一致冲突
            if point_id in seen_ids:
                if seen_ids[point_id][:3] != sig:
                    raise ConflictError(
                        f"批内冲突: point_id={point_id!r} 出现了不同内容（时刻或坐标不一致）"
                    )
                continue

            # 同车次同刻不同点
            if ts_epoch in seen_ts:
                raise ConflictError(
                    f"批内冲突: 时刻 {_iso_z(dt)} 同时关联了 point_id="
                    f"{seen_ts[ts_epoch]!r} 与 {point_id!r}"
                )

            seen_ids[point_id] = (ts_epoch, x, y, _iso_z(dt))
            seen_ts[ts_epoch] = point_id
            parsed.append((point_id, ts_epoch, x, y, _iso_z(dt)))

        # ---- 单事务：与已存数据核对 + 写入 + 版本推进 ----
        # 全部读取与比对都在 BEGIN IMMEDIATE 后的同一快照/写事务内，杜绝并发下
        # “先读判断后写入”竞态；失败一律回滚，不留任何数据。
        with self.db.transaction() as conn:
            existing_rows = self.db._list_trip_points(conn, trip_id)
            stored_map: Dict[str, Tuple[float, float, float]] = {
                r["point_id"]: (r["ts_epoch"], r["x"], r["y"]) for r in existing_rows
            }
            ts_owner: Dict[float, str] = {r["ts_epoch"]: r["point_id"] for r in existing_rows}

            new_rows: List[Tuple[str, str, float, float, float]] = []
            for point_id, ts_epoch, x, y, _ in parsed:
                # 已撤销 ID 不得经上传重用（撤销不可经上传恢复）
                if self.db._revoked_row(conn, trip_id, point_id) is not None:
                    raise ConflictError(
                        f"point_id={point_id!r} 已被更正撤销，不得经批量上传重用；"
                        "如确需重新采集请使用新的 point_id"
                    )
                stored = stored_map.get(point_id)
                if stored is not None:
                    if stored != (ts_epoch, x, y):
                        raise ConflictError(
                            f"point_id={point_id!r} 已存在且内容不同："
                            f"已存 (ts_epoch={stored[0]}, x={stored[1]}, y={stored[2]})，"
                            f"新传 (ts_epoch={ts_epoch}, x={x}, y={y})"
                        )
                    continue  # 同 ID 同内容：幂等重传
                # 新点：同刻必须仍空闲
                owner = ts_owner.get(ts_epoch)
                if owner is not None:
                    raise ConflictError(
                        f"时刻冲突: point_id={point_id!r} 的时刻与已存 point_id="
                        f"{owner!r} 相同（同车次同刻只能有一个点）"
                    )
                new_rows.append((trip_id, point_id, ts_epoch, x, y))
                # 本批后续点视作已占用
                ts_owner[ts_epoch] = point_id

            if new_rows:
                self.db.insert_points(conn, new_rows)
                # 真正录入新点：版本恰好推进一次（纯幂等重传不推进）
                vrow = self.db._trip_version_row(conn, trip_id)
                next_version = 1 if vrow is None else int(vrow["version"]) + 1
                self.db.upsert_trip_version(
                    conn, trip_id, next_version, _iso_z(datetime.now(timezone.utc))
                )

        return {
            "trip_id": trip_id,
            "received": len(points),
            "accepted_new": len(new_rows),
            "idempotent_retransmit": len(points) - len(new_rows),
        }

    # ------------------------------------------------------------------
    # 采集点更正（撤销 / 改写，乐观版本，整组原子 + 不可变履历）
    # ------------------------------------------------------------------

    def correct_points(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        base_version = payload.get("base_version")
        if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 0:
            raise ValidationError(f"base_version 必须是 >=0 的整数，收到: {base_version!r}")

        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("reason 必须是非空字符串")
        reason = reason.strip()

        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError("items 必须是非空更正项列表")

        # ---- 解析整组：一组互异 point_id，每项撤销或带时区时刻+有限坐标 ----
        touched: List[str] = []
        updates: Dict[str, Tuple[float, float, float, str]] = {}
        revokes: List[str] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationError(f"items[{idx}] 必须是对象")
            action = item.get("action")
            point_id = item.get("point_id")
            if not isinstance(point_id, str) or not point_id.strip():
                raise ValidationError(f"items[{idx}].point_id 必须是非空字符串")
            point_id = point_id.strip()
            if point_id in updates or point_id in revokes:
                raise ValidationError(f"items[{idx}].point_id={point_id!r} 在本批更正中重复")
            touched.append(point_id)

            if action == "revoke":
                revokes.append(point_id)
            elif action == "update":
                try:
                    dt = parse_utc(item.get("ts"), field=f"items[{idx}].ts")
                except TimeError as exc:
                    raise ValidationError(str(exc)) from exc
                x_raw, y_raw = item.get("x"), item.get("y")
                if isinstance(x_raw, bool) or isinstance(y_raw, bool):
                    raise ValidationError(f"items[{idx}] 坐标必须是数值")
                try:
                    x, y = float(x_raw), float(y_raw)
                except (TypeError, ValueError):
                    raise ValidationError(
                        f"items[{idx}] 坐标必须是数值: x={x_raw!r}, y={y_raw!r}"
                    )
                if not (math.isfinite(x) and math.isfinite(y)):
                    raise ValidationError(
                        f"items[{idx}] 坐标必须有限，禁止 NaN/Infinity: "
                        f"x={x_raw!r}, y={y_raw!r}"
                    )
                updates[point_id] = (dt.timestamp(), x, y, _iso_z(dt))
            else:
                raise ValidationError(
                    f"items[{idx}].action 必须是 revoke 或 update，收到: {action!r}"
                )

        # ---- 单事务核对整组并写入 ----
        with self.db.transaction() as conn:
            vrow = self.db._trip_version_row(conn, trip_id)
            # 既有车次以当前点集为版本 0 基线（兼容直接通过 SQL 落库的旧数据）
            if vrow is None:
                existing = self.db._list_trip_points(conn, trip_id)
                if not existing and not self.db.list_revoked_ids(conn, trip_id):
                    raise NotFoundError(f"车次不存在（无轨迹点，无法更正）: {trip_id}")
                current_version = 0
            else:
                current_version = int(vrow["version"])

            # 过期版本：整批拒绝且不留履历
            if base_version != current_version:
                raise ConflictError(
                    f"轨迹版本过期: 提交基于版本 {base_version}，当前版本为 "
                    f"{current_version}；请读取当前点集与版本后重新提交更正"
                )

            current_rows = self.db._list_trip_points(conn, trip_id)
            current_map: Dict[str, Any] = {
                r["point_id"]: r for r in current_rows
            }

            # 点存在性：缺失点或已撤销点一律整批拒绝
            missing = [pid for pid in touched if pid not in current_map]
            if missing:
                revoked_hint = [
                    pid
                    for pid in missing
                    if self.db._revoked_row(conn, trip_id, pid) is not None
                ]
                if revoked_hint:
                    raise ConflictError(
                        "更正点已撤销，不能再次更正: "
                        + ", ".join(repr(p) for p in revoked_hint)
                    )
                raise ConflictError(
                    "更正点在当前有效点集中不存在: "
                    + ", ".join(repr(p) for p in missing)
                )

            # ---- 以整组最终有效点集核对同车次同刻唯一 ----
            # 未触及点保持原值；update 项取新值；revoke 项移除
            final_ts: Dict[float, str] = {}
            for r in current_rows:
                pid = r["point_id"]
                if pid in revokes:
                    continue
                if pid in updates:
                    continue
                final_ts[r["ts_epoch"]] = pid
            for pid, (ts_epoch, _x, _y, _iso) in updates.items():
                owner = final_ts.get(ts_epoch)
                if owner is not None and owner != pid:
                    raise ConflictError(
                        f"更正后同车次同刻冲突: point_id={pid!r} 的新时刻 "
                        f"{updates[pid][3]} 与 point_id={owner!r} 相同"
                    )
                final_ts[ts_epoch] = pid

            # ---- 写入新有效状态：先删除全部受影响点，再插入更新项 ----
            # 先删后插使同刻互换/移位不触发 (trip, ts) 唯一约束；校验已保证最终集合法
            self.db.delete_points(conn, trip_id, touched)
            self.db.insert_points(
                conn,
                [
                    (trip_id, pid, ts_epoch, x, y)
                    for pid, (ts_epoch, x, y, _iso) in updates.items()
                ],
            )
            now_iso = _iso_z(datetime.now(timezone.utc))
            new_version = current_version + 1
            self.db.upsert_trip_version(conn, trip_id, new_version, now_iso)
            if revokes:
                self.db.insert_revoked_points(
                    conn, trip_id, revokes, new_version, reason, now_iso
                )

            # ---- 不可改前后值履历（仅本批成功后写入，顺序随请求 items）----
            history_rows: List[Tuple[Any, ...]] = []
            for pid in touched:
                r = current_map[pid]
                if pid in updates:
                    ts_epoch, x, y, _iso = updates[pid]
                    history_rows.append(
                        (trip_id, new_version, reason, pid, "update",
                         r["ts_epoch"], r["x"], r["y"], ts_epoch, x, y, now_iso)
                    )
                else:
                    history_rows.append(
                        (trip_id, new_version, reason, pid, "revoke",
                         r["ts_epoch"], r["x"], r["y"], None, None, None, now_iso)
                    )
            self.db.insert_point_corrections(conn, history_rows)

        return {
            "trip_id": trip_id,
            "base_version": base_version,
            "version": new_version,
            "reason": reason,
            "affected": [self._affected_point(pid, current_map, updates) for pid in touched],
        }

    @staticmethod
    def _point_state(ts_epoch: float, x: float, y: float) -> Dict[str, Any]:
        return {
            "ts": _iso_z(datetime.fromtimestamp(ts_epoch, timezone.utc)),
            "x": x,
            "y": y,
        }

    def _affected_point(
        self,
        point_id: str,
        current_map: Dict[str, Any],
        updates: Dict[str, Tuple[float, float, float, str]],
    ) -> Dict[str, Any]:
        r = current_map[point_id]
        before = self._point_state(r["ts_epoch"], r["x"], r["y"])
        if point_id in updates:
            ts_epoch, x, y, _iso = updates[point_id]
            return {
                "point_id": point_id,
                "action": "update",
                "before": before,
                "after": self._point_state(ts_epoch, x, y),
            }
        return {"point_id": point_id, "action": "revoke", "before": before, "after": None}

    def get_trip(self, trip_id: str) -> Optional[Dict[str, Any]]:
        """车次当前轨迹（更正后有效点集）+ 当前版本；车次从未存在返回 None。"""
        with self.db.read_view() as conn:
            vrow = self.db._trip_version_row(conn, trip_id)
            rows = self.db._list_trip_points(conn, trip_id)
            if vrow is None and not rows:
                return None
            version = 0 if vrow is None else int(vrow["version"])
            return {
                "trip_id": trip_id,
                "version": version,
                "point_count": len(rows),
                "points": [
                    {
                        "point_id": r["point_id"],
                        **self._point_state(r["ts_epoch"], r["x"], r["y"]),
                    }
                    for r in rows
                ],
            }

    def list_corrections(self, trip_id: str) -> Dict[str, Any]:
        """按车次排列的更正履历（按版本升序）；车次从未存在返回 None。"""
        with self.db.read_view() as conn:
            vrow = self.db._trip_version_row(conn, trip_id)
            rows = self.db._list_trip_points(conn, trip_id)
            if vrow is None and not rows:
                return None
            current_version = 0 if vrow is None else int(vrow["version"])
            correction_rows = self.db.list_point_correction_rows(conn, trip_id)

        corrections: List[Dict[str, Any]] = []
        for r in correction_rows:
            after = None
            if r["action"] == "update":
                after = self._point_state(
                    r["after_ts_epoch"], r["after_x"], r["after_y"]
                )
            corrections.append(
                {
                    "version": int(r["version"]),
                    "reason": r["reason"],
                    "point_id": r["point_id"],
                    "action": r["action"],
                    "before": self._point_state(
                        r["before_ts_epoch"], r["before_x"], r["before_y"]
                    ),
                    "after": after,
                    "corrected_at": r["corrected_at"],
                }
            )
        return {
            "trip_id": trip_id,
            "version": current_version,
            "corrections": corrections,
        }

    # ------------------------------------------------------------------
    # 穿越审计
    # ------------------------------------------------------------------

    def query_crossings(
        self,
        trip_id: str,
        zone_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        revision_raw: Any = None,
    ) -> Dict[str, Any]:
        try:
            lo = parse_utc(since, field="since").timestamp() if since else None
            hi = parse_utc(until, field="until").timestamp() if until else None
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo is not None and hi is not None and lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        pinned = revision_raw is not None
        revision = self._resolve_revision(revision_raw) if pinned else None

        with self.db.read_view() as conn:
            points = self.db._list_trip_points(conn, trip_id)
            if len(points) < 2:
                result: Dict[str, Any] = {
                    "trip_id": trip_id,
                    "point_count": len(points),
                    "zones": [],
                }
                if pinned:
                    result["revision"] = revision
                return result

            if pinned:
                zone_rows = self.db._list_zone_rows_at(conn, revision)
            else:
                zone_rows = self.db._list_latest_zone_rows(conn)
            if zone_id is not None:
                zone_rows = [r for r in zone_rows if r["zone_id"] == zone_id]
                if not zone_rows:
                    if pinned:
                        # 指定版本中尚未登记的区域不参与核算
                        raise NotFoundError(
                            f"修订号 {revision} 的配置中危险区尚未登记: {zone_id}"
                        )
                    raise ValidationError(f"危险区不存在: {zone_id}")

            zone_results: List[Dict[str, Any]] = []
            for zrow in zone_rows:
                zone_result = self._crossing_for_zone(zrow, points, lo, hi)
                zone_results.append(zone_result)

        result = {
            "trip_id": trip_id,
            "point_count": len(points),
            "zones": zone_results,
        }
        if pinned:
            result["revision"] = revision
        return result

    def _crossing_for_zone(
        self,
        zrow: Any,
        points: List[Any],
        lo: Optional[float],
        hi: Optional[float],
    ) -> Dict[str, Any]:
        polygon = Polygon(
            json.loads(zrow["exterior"]),
            json.loads(zrow["holes"]),
        )
        windows_raw = json.loads(zrow["windows"])
        windows = merge_windows([(w["start_epoch"], w["end_epoch"]) for w in windows_raw])

        # 覆盖参数（时间）区间：遍历相邻点，乱序已在 SQL 层按时刻排序重建
        cover: List[Tuple[float, float]] = []
        for a, b in zip(points, points[1:]):
            t0, t1 = a["ts_epoch"], b["ts_epoch"]
            if t1 <= t0:
                # 同刻点不应出现（唯一约束），稳妥跳过
                continue
            params = segment_inside_params(
                polygon, (a["x"], a["y"]), (b["x"], b["y"])
            )
            for pa, pb in params:
                # 参数 0/1 直接取端点时刻，保证相邻段在公共采样点精确相接
                s = t0 if pa == 0.0 else t0 + pa * (t1 - t0)
                e = t1 if pb == 1.0 else t0 + pb * (t1 - t0)
                if e - s > self.min_duration:
                    cover.append((s, e))

        # 时间域合并留 1e-6 秒容差，吸收不同时刻解析路径的浮点误差
        cover = merge_intervals(cover, eps=1e-6)
        intervals = clip_intervals_halfopen(cover, windows)
        if lo is not None or hi is not None:
            bounds = [(-float("inf") if lo is None else lo, float("inf") if hi is None else hi)]
            intervals = clip_intervals_halfopen(intervals, bounds)
        intervals = merge_intervals(intervals, eps=1e-6)
        intervals = [(s, e) for s, e in intervals if e - s > self.min_duration]

        segments = [
            {
                "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                "duration_sec": round(e - s, 6),
            }
            for s, e in intervals
        ]
        total = round(sum(e - s for s, e in intervals), 6)

        return {
            "zone_id": zrow["zone_id"],
            "crossed": bool(intervals),
            "total_duration_sec": total,
            "segments": segments,
        }
