# 工地作业车危险区穿越审计服务

用 Python (FastAPI + Shapely + SQLite) 构建的 HTTP 服务，用于：

1. **登记危险区**：平面米制多边形（支持洞）+ UTC 左闭右开 `[start, end)` 生效时段；
   每次登记（含同 ID 覆盖更新）**原子产生全局递增修订号**并随响应返回，各区保留**不可变历史版本**；
2. **批量提交轨迹点**：车次、点 ID、UTC 时刻、平面坐标；同点 ID 同内容重传幂等，异内容返回冲突；
3. **现场采集点更正**：作业人员凭车次、所见轨迹版本、非空原因与一组互异 `point_id` 整组提交，
   每项选择**撤销**或给出带时区 UTC 时刻及有限平面坐标的**改写**；整组单事务核对，
   成功后轨迹版本 +1 并写入不可改前后值履历，过期版本/缺失点/已撤销点/非法值/同刻冲突整批拒绝；
4. **穿越审计**：相邻点间按时间与坐标线性插值，仅在“轨迹覆盖且区域生效”的时间内计算穿越，按危险区返回起止区间与累计秒数；
   可按**最新配置**查询（默认，语义不变），也可指定**修订号**按当时区域配置**复现**历史核算。
   现有轨迹与穿越查询立即使用更正后的有效点集；区域修订与插值规则不变。

## 语义约定

- 坐标为**平面米制坐标**（单位统一为米），服务内部不做投影换算。
- 危险区 = 多边形内部 + **外边界**；**洞的内部与其边界均不计入**。
- 生效时段为 UTC、**左闭右开** `[start, end)`，仅当穿越时间落在生效时段内才统计。
- 相邻轨迹点按时刻排序后在相邻点间做**时间-空间线性插值**；乱序上传会自动按时刻重建。
- **零时长接触不算穿越**（轨迹仅擦过角点/边于一瞬不计）；静止点按点归属处理。
- 同一危险区的相接（端点重合）穿越区间自动合并；中间隔着未覆盖时间的区间不合并。
- 非法输入明确拒绝（HTTP 400）：自交多边形、洞越出外环、洞互相重叠、非有限坐标（NaN/Infinity）、不带时区的时刻、非正长度时段等。
- 同车次同一 UTC 时刻只能属于一个点 ID，否则视为冲突（HTTP 409）。
- 批量提交为**单事务原子写入**：任何一点校验/冲突失败，整批不写入任何数据。
- SQLite 持久化到挂载卷，**进程/容器重启后记录仍可查询**。

## 轨迹版本与现场点更正

- 车次轨迹按**每车次独立版本**管理（与区域的全局 `revision` 互不影响）：
  - **既有车次以当前点集为版本 0 基线**：升级前已有点的车次在新版本首次启动时一次性登记为 0 号（迁移幂等）；
  - 此后**每批真正录入新点**或**每批成功更正**各只推进一次版本；**纯幂等重传不推进**；
  - 混合批（部分幂等、部分新点）仍只推进一次；全撤销后重新上传新点，版本继续推进。
- 更正请求 `POST /api/v1/trips/{trip_id}/corrections` 必须携带：
  - `base_version`：作业人员所见的轨迹版本，须与库中当前版本一致，否则按**过期版本**整批拒绝（409）；
  - `reason`：**非空**更正原因；
  - `items`：**非空**且 `point_id` **一组互异**的更正项，每项二选一：
    - `{"action":"revoke","point_id":...}`：撤销该点；
    - `{"action":"update","point_id":...,"ts":...,"x":...,"y":...}`：改写为带时区 UTC 时刻与有限平面坐标。
- 整组在**同一事务**内以**整组最终有效点集**核对：
  - `base_version` 必须等于当前版本（过期版本 409）；
  - 每个 `point_id` 必须存在于当前有效点集；**缺失点**与**已撤销点**均整批拒绝（409）；
  - 未触及点保持原值、`update` 取新值、`revoke` 移除，最终集必须满足**同车次同刻唯一**（409）；
    两点时刻互换这类“最终仍唯一”的改法允许（内部先删后插，不触发中间唯一约束）；
  - 非有限坐标、不带时区时刻、空原因、重复 `point_id`、未知 `action` 等非法值拒绝（400/422）。
- 校验通过后写入新有效状态，并追加**不可改的前后值履历**（`point_corrections`，含动作、原因、
  前后时刻坐标、生效版本与更正时间）；撤销点另登记到 `revoked_points`。
  **任何失败都回滚，不留半批数据、不留履历。**
- 成功响应返回**新版本** `version` 与**受影响点** `affected`（每项含 `action`、`before`/`after`，撤销时 `after=null`）。
- **现有轨迹查询与穿越查询立即使用更正后的有效点集**（撤销点不再参与，插值规则不变）；
  区域修订（`revision`）规则完全不变。
- 原批量上传的请求/响应及“同 ID 同内容幂等、异内容冲突”约定**保留**；
  **已撤销 ID 不得经上传重用**（返回 409），如需重新采集请使用新的 `point_id`；
  撤销后空出的时刻可由新 ID 的点占用。
- 可读取车次**当前轨迹版本**（`GET /api/v1/trips/{trip_id}` 响应中的 `version`）
  与**按车次排列的更正履历**（`GET /api/v1/trips/{trip_id}/corrections`，按版本升序）。

## 修订版本（历史复现）

- **修订号 `revision` 为全局正整数，单调递增**：无论是新登记还是同 ID 覆盖更新，每次成功提交都在
  同一个数据库事务内分配一个新修订号，登记响应返回该号。
- 区域历史**只追加、不可变**：覆盖更新不会改动旧版本行；每个 `(zone_id, revision)` 的几何与生效时段永久可查。
- 穿越查询带 `revision=R` 时，取**各区截至 R 的最后版本**参与核算（不是“R 时刻刚好提交的那一批”）：
  - 沿用现有的带洞边界、UTC 左闭右开窗口、相邻轨迹点插值规则，结果与当初在该版本下查询一致；
  - 响应回填所用修订号 `"revision": R`；
  - **R 中尚未登记的区不参与核算**；显式用 `zone_id` 指定这种区时返回 404；
  - 修订号不存在（`revision < 1`）或**超出当前范围**（`R > 当前最新号`）明确报错；
  - 不带 `revision` 时完全保持现有最新配置查询的路径、请求字段与响应语义（响应中**不**出现 `revision` 字段）。
- 旧库（`zones` / `zone_windows` 表）在新版本首次启动时**一次性迁移为基线版本**
  （修订号从 1 开始，按 `zone_id` 排序分配），迁移后删除旧表；**不推断迁移前历史**。迁移幂等，可安全重启。
- 并发更新由数据库写事务串行化，修订号不缺号、不重号；并发查询走只读快照，**读不到半次提交**。

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

启动后：

- 服务地址：`http://localhost:8080`
- 健康检查：`GET http://localhost:8080/health`
- 交互式 API 文档（Swagger UI）：`http://localhost:8080/docs`
- OpenAPI 描述：`http://localhost:8080/openapi.json`

数据保存在 Docker 命名卷 `audit-data`（容器内 `/data/dangerzones.db`），重启不丢。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据卷
```

仅构建镜像：`docker build -t danger-zone-audit:1.0.0 .`，容器入口脚本会先执行数据库初始化（建表）再启动 uvicorn。

## 本地直接运行（无 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
export DZ_DB_PATH=./data/dangerzones.db
python -m app.init_db                     # 可选，首次启动会自动建表
uvicorn app.main:app --host 0.0.0.0 --port 8080
# 或：python -m app.main
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DZ_HOST` | `0.0.0.0` | 监听地址 |
| `DZ_PORT` | `8080` | 监听端口 |
| `DZ_DB_PATH` | `./data/dangerzones.db`（Compose 内为 `/data/dangerzones.db`） | SQLite 数据文件路径 |
| `DZ_MIN_DURATION_SEC` | `1e-9` | 小于该秒数的覆盖片段视为零时长接触，不计入穿越 |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/zones` | 登记（或同 ID 覆盖更新）危险区，响应返回全局修订号 |
| GET | `/api/v1/zones` | 列出全部危险区（可加 `revision` 列历史快照） |
| GET | `/api/v1/zones/{zone_id}` | 查询单个危险区（可加 `revision` 取历史版本） |
| GET | `/api/v1/revisions` | 全局修订流水（按修订号倒序，含当前最新号） |
| GET | `/api/v1/zones/{zone_id}/revisions` | 单个危险区的不可变修订历史（升序） |
| POST | `/api/v1/trips/{trip_id}/points` | 批量提交/重传某车次轨迹点（原子、幂等） |
| POST | `/api/v1/trips/{trip_id}/corrections` | 整组提交采集点更正（撤销/改写，乐观版本，原子+履历） |
| GET | `/api/v1/trips/{trip_id}` | 查询车次当前轨迹版本与全部有效点（按时刻排序） |
| GET | `/api/v1/trips/{trip_id}/corrections` | 按车次排列的点更正履历（按版本升序，含前后值） |
| GET | `/api/v1/trips/{trip_id}/crossings` | 穿越审计结果（可加 `revision` 按历史配置复现） |

穿越查询参数（均可选）：`zone_id`（只看某危险区）、`since` / `until`（UTC ISO 8601，半开 `[since, until)` 时间过滤）、
`revision`（区域配置修订号正整数；省略 = 最新配置，响应无 `revision` 字段；指定后响应回填该号）。
区域列表/详情的 `revision` 参数语义相同：省略返回最新版本，指定返回截至该号的最后版本。

时间字符串使用 ISO 8601 且**必须带时区**，推荐以 `Z` 结尾表示 UTC，例如 `2026-09-30T12:00:00Z`。

## 调用示例

### 1) 登记带洞危险区

外边界 100m×100m，中央 20m×20m 为洞（洞及其边界不计入），2026-09-30 全天生效：

```bash
curl -s -X POST http://localhost:8080/api/v1/zones \
  -H 'Content-Type: application/json' \
  -d '{
    "zone_id": "Z-A",
    "exterior": [[0,0],[100,0],[100,100],[0,100]],
    "holes": [[[40,40],[60,40],[60,60],[40,60]]],
    "windows": [
      {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
    ]
  }'
# {"zone_id":"Z-A","revision":1,"status":"created","num_holes":1,"num_windows":1}
```

登记响应的 `revision` 即本次提交原子产生的**全局递增修订号**；同 ID 再次提交（几何或时段有调整）
会得到 `status:"updated"` 与新的修订号，旧版本保持不变：

```bash
curl -s -X POST http://localhost:8080/api/v1/zones \
  -H 'Content-Type: application/json' \
  -d '{
    "zone_id": "Z-A",
    "exterior": [[0,0],[120,0],[120,120],[0,120]],
    "holes": [],
    "windows": [
      {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
    ]
  }'
# {"zone_id":"Z-A","revision":2,"status":"updated","num_holes":0,"num_windows":1}
```

环的首尾点无需重复给出；可登记多个生效时段（重叠/相接时段会合并处理）。

### 2) 批量提交轨迹点（允许乱序）

```bash
curl -s -X POST http://localhost:8080/api/v1/trips/truck-7/points \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"point_id":"a", "ts":"2026-09-30T12:00:00Z", "x":-20, "y":50},
      {"point_id":"c", "ts":"2026-09-30T12:02:00Z", "x":120, "y":50},
      {"point_id":"b", "ts":"2026-09-30T12:01:00Z", "x":50,  "y":50}
    ]
  }'
# {"trip_id":"truck-7","received":3,"accepted_new":3,"idempotent_retransmit":0}
```

同点 ID 同内容重传返回 `accepted_new=0, idempotent_retransmit=N`（HTTP 200）；
异内容返回 HTTP 409，例如：

```json
{"error":"conflict","detail":"point_id='a' 已存在且内容不同：已存 (ts_epoch=..., x=-20.0, y=50.0)，新传 (...)"}
```

整批中任何一点失败都会回滚，不写入部分数据。

### 2.5) 现场采集点更正（撤销 / 改写）

先读取车次当前轨迹版本（`GET /api/v1/trips/{trip_id}` 的 `version`，首批上传后为 1）：

```bash
curl -s http://localhost:8080/api/v1/trips/truck-7
# {"trip_id":"truck-7","version":1,"point_count":3,"points":[...]}
```

作业人员凭车次、所见版本、非空原因和一组互异 `point_id` 整组提交；每项撤销或给出
带时区 UTC 时刻与有限平面坐标的改写：

```bash
curl -s -X POST http://localhost:8080/api/v1/trips/truck-7/corrections \
  -H 'Content-Type: application/json' \
  -d '{
    "base_version": 1,
    "reason": "2026-09-30 现场复核：b 点坐标录错，c 点为设备漂移误录",
    "items": [
      {"action":"update", "point_id":"b",
       "ts":"2026-09-30T12:01:05Z", "x":50.2, "y":49.8},
      {"action":"revoke", "point_id":"c"}
    ]
  }'
```

```json
{
  "trip_id": "truck-7",
  "base_version": 1,
  "version": 2,
  "reason": "2026-09-30 现场复核：b 点坐标录错，c 点为设备漂移误录",
  "affected": [
    {
      "point_id": "b",
      "action": "update",
      "before": {"ts": "2026-09-30T12:01:00Z", "x": 50.0, "y": 50.0},
      "after":  {"ts": "2026-09-30T12:01:05Z", "x": 50.2, "y": 49.8}
    },
    {
      "point_id": "c",
      "action": "revoke",
      "before": {"ts": "2026-09-30T12:02:00Z", "x": 120.0, "y": 50.0},
      "after": null
    }
  ]
}
```

成功后版本恰好推进到 2；此后轨迹查询与穿越审计立即使用更正后的有效点集（`c` 不再参与插值）。

继续更正必须携带新的 `base_version: 2`；仍用 1 提交会得到 **409 过期版本**，整批不生效、不留履历：

```json
{"error":"conflict","detail":"轨迹版本过期: 提交基于版本 1，当前版本为 2；请读取当前点集与版本后重新提交更正"}
```

其他整批拒绝情形（均不写入、不留履历）：

- **缺失点 / 已撤销点**（409）：`更正点在当前有效点集中不存在: 'ghost'`、`更正点已撤销，不能再次更正: 'c'`；
- **最终集同刻冲突**（409）：改写后的时刻与同车次另一个点相同；
- **非法值**（400/422）：非有限坐标（NaN/Infinity）、不带时区时刻、空 `reason`、批内重复 `point_id`、未知 `action`。

按车次查看不可改的更正履历（按版本升序，含前后值）：

```bash
curl -s http://localhost:8080/api/v1/trips/truck-7/corrections
```

```json
{
  "trip_id": "truck-7",
  "version": 2,
  "corrections": [
    {
      "version": 2,
      "reason": "2026-09-30 现场复核：b 点坐标录错，c 点为设备漂移误录",
      "point_id": "b",
      "action": "update",
      "before": {"ts": "2026-09-30T12:01:00Z", "x": 50.0, "y": 50.0},
      "after":  {"ts": "2026-09-30T12:01:05Z", "x": 50.2, "y": 49.8},
      "corrected_at": "2026-10-01T03:10:00Z"
    },
    {
      "version": 2,
      "reason": "2026-09-30 现场复核：b 点坐标录错，c 点为设备漂移误录",
      "point_id": "c",
      "action": "revoke",
      "before": {"ts": "2026-09-30T12:02:00Z", "x": 120.0, "y": 50.0},
      "after": null,
      "corrected_at": "2026-10-01T03:10:00Z"
    }
  ]
}
```

注意：**已撤销的 `c` 不得再经批量上传重用**（409）；如需重新采集请使用新的 `point_id`。

### 3) 查询穿越审计

```bash
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings"
```

```json
{
  "trip_id": "truck-7",
  "point_count": 3,
  "zones": [
    {
      "zone_id": "Z-A",
      "crossed": true,
      "total_duration_sec": 68.571429,
      "segments": [
        {"start": "2026-09-30T12:00:17.142857Z", "end": "2026-09-30T12:00:51.428571Z", "duration_sec": 34.285714},
        {"start": "2026-09-30T12:01:08.571429Z", "end": "2026-09-30T12:01:42.857143Z", "duration_sec": 34.285714}
      ]
    }
  ]
}
```

车辆以 140m/120s 匀速横穿，进入外边界到进入洞前沿途计入；穿越洞（含其边界）的
约 17.14 秒被剔除，出洞后再次计入，因此得到两段、累计 68.571429 秒。两段之间被
洞隔开，不合并；若轨迹在区域边界处首尾相接，则自动合并为一段。

带时间过滤的查询：

```bash
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings?zone_id=Z-A&since=2026-09-30T12:00:30Z&until=2026-09-30T12:01:30Z"
```

### 4) 按修订号复现历史核算

现场会修订危险区（如上面 `Z-A` 已从“100m×100m 带洞”更新为“120m×120m 无洞”，修订号 2）。
已出具的核算可用当时的修订号复现——穿越查询加 `revision=1` 即取各区截至 1 号的最后版本，
计算规则与最新查询完全一致，响应回填所用修订号：

```bash
# 最新配置（不带 revision，响应不含 revision 字段；语义与旧版本完全一致）
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings?zone_id=Z-A"

# 按修订号 1 复现：仍是带洞版本，两段、累计 68.571429 秒
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings?zone_id=Z-A&revision=1"
```

```json
{
  "trip_id": "truck-7",
  "point_count": 3,
  "zones": [
    {
      "zone_id": "Z-A",
      "crossed": true,
      "total_duration_sec": 68.571429,
      "segments": [
        {"start": "2026-09-30T12:00:17.142857Z", "end": "2026-09-30T12:00:51.428571Z", "duration_sec": 34.285714},
        {"start": "2026-09-30T12:01:08.571429Z", "end": "2026-09-30T12:01:42.857143Z", "duration_sec": 34.285714}
      ]
    }
  ],
  "revision": 1
}
```

不带 `zone_id` 时，仅修订号 1 快照中已登记的区参与核算；当时尚未登记的区自动排除。

修订历史可随时核对：

```bash
curl -s "http://localhost:8080/api/v1/revisions"                 # 全局流水（倒序）+ 当前最新号
curl -s "http://localhost:8080/api/v1/zones/Z-A/revisions"       # Z-A 的各版本号（升序）
curl -s "http://localhost:8080/api/v1/zones/Z-A?revision=1"      # Z-A 在 1 号时的几何与窗口
curl -s "http://localhost:8080/api/v1/zones?revision=1"          # 1 号时的全部区域快照
```

修订号超出当前范围明确报错（404），例如当前最新号为 2：

```json
{"error":"not_found","detail":"修订号 999 超出当前范围：当前最新修订号为 2"}
```

`revision=0`、非整数等非法值在请求层即被拒绝（422）。

## 错误响应

- `400 validation_error`：几何/时间/坐标语义非法（自交多边形、洞越界、非有限坐标、时区缺失、更正项重复等）。
- `409 conflict`：同点 ID 异内容，或同车次同刻关联了不同点 ID；
  更正时的过期版本、缺失点/已撤销点、最终点集同刻冲突；已撤销 ID 经上传重用。
- `404 not_found`：危险区/车次不存在；指定修订号超出当前范围；指定版本中该区尚未登记。
- `422`：请求体/查询参数不符合模式（类型错误、多余字段、缺字段、revision<1、更正 action 非法等）。

## 运行测试

```bash
pip install pytest httpx
python -m pytest -q
```

测试覆盖：带洞多边形裁剪、外边界计入、洞与洞边界剔除、零时长接触、自交/洞越界拒绝、
幂等与冲突、批量原子性、乱序重建、生效时段半开裁剪、区间合并、重启持久化，以及
修订号全局递增与同 ID 覆盖、不可变历史、按修订号复现核算（几何/窗口变更）、
超范围修订号报错、指定版本未登记区域排除、旧库基线迁移、并发更新不重号/查询不读半提交；
以及轨迹版本基线（既有车次版本 0）、真正新点/成功更正才推进版本而幂等重传不推进、
撤销与改写、过期版本/缺失点/已撤销点/非法值/同刻冲突整批拒绝且不留履历、
同刻互换、撤销 ID 禁止上传重用、前后值履历按版本排列、更正后点集立即用于轨迹与穿越查询、
更正数据重启持久化、并发更正由版本串行化等。

## 目录结构

```
app/
  config.py       # 环境变量配置
  geometry.py     # 带洞多边形校验与线段裁剪（Shapely）
  timeutils.py    # UTC 解析与半开时间窗运算
  db.py           # SQLite 持久层（区域不可变修订、车次轨迹版本、撤销点与更正履历、快照/写事务、唯一约束、旧库迁移）
  service.py      # 业务逻辑：登记/幂等批量/穿越计算
  schemas.py      # Pydantic 请求模型
  main.py         # FastAPI 入口
  init_db.py      # 数据库初始化（建表）
tests/            # pytest 测试套件
Dockerfile
docker-compose.yml
docker-entrypoint.sh
requirements.txt
```
