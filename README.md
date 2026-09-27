# 仪器校准漂移补偿 API

基于 **FastAPI + SQLite** 的仪器校准服务：客户端在各时点提交参考值与同点多次读数，服务标记异常读数、计算校准偏差，并在相邻时点间线性插值偏差，对待校正读数施加漂移补偿。

## 快速开始（单条命令）

```bash
docker compose up --build
```

- **API 地址：http://localhost:8000**
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

SQLite 数据落在命名卷 `calibration-data`（容器内 `/data/calibration.db`），容器重启数据不丢。

## 输入边界（有限数值与时点）

`POST /observations`、`PUT /observations/{id}` 与 `POST /observation-impact-preview` 共用同一套观测输入（时点、参考值、容差、读数），其边界规则一致：

- **参考值 `reference`、容差 `tolerance`、每次读数 `readings[]`** 必须是**有限** JSON 数值；`NaN`、`Infinity`、`-Infinity`（无论出现在哪个字段、数组的哪个位置）一律返回 **422**，且不落库。容差另须为有限非负数。
- **时点 `timestamp`**（Unix 秒数字、数字字符串或 ISO 8601 字符串）必须**能转换为 UTC 日期**：`NaN`/±`Infinity` 以及有限但超出平台时间范围（如 `1e300`、`99999-01-01T00:00:00Z`）的时点返回 **422**。
- 即使各输入均为有限值，若算出的**校准偏差溢出**（如参考值 `1e308` 与有效读数中位数 `-1e308` 之差成为 ±Infinity），同样返回 **422**——无穷偏差不会入库。
- 极大但**有限**的同值读数不会被误拒：偶数个读数的组中位数中点在直接 `(a+b)/2` 会溢出时改走不溢出的求法，中位数保持有限（例如 4 个 `1e308` 的中位数仍是 `1e308`，无异常读数）。
- 被 422 拒绝的创建或修订不会留下任何观测、读数、区间或修订快照的变化（全部校验与计算在写入前/单事务内完成）；影响预演始终只读。
- 异常读数判定规则不变：读数偏离该组中位数超过 `tolerance` 即标记为异常。

### 漂移补偿入口的数值边界（/corrections、/historical-correction、/correction-receipts）

三个补偿入口共用同一套插值与叠加计算，数值边界一致：

- **插值不发生中间溢出**：区间内偏差以凸组合 `(1−w)·bias_start + w·bias_end`（`w ∈ [0,1]`）计算。即使两端偏差为 `1e308` 与 `-1e308`，旧式 `bias_start + (bias_end − bias_start)·w` 会先在差值 `2e308` 处溢出为 `Infinity`，凸组合仍给出有限结果（中点为 `0.0`）；**观测点与区间端点保持原偏差**（恰在端点时严格等于该点偏差，不做缩放），响应中的 `bias_start`/`bias_end`/端点明细也原样返回。
- **待校正读数 `reading` 必须是有限数值**：`NaN`、`Infinity`、`-Infinity`（请求体中的非标准 JSON 令牌或 GET 查询参数）一律返回 **422**。
- **最终校正值不可表示时返回 422**：读数与偏差均有限但二者之和超出 float 上界（如读数 `1e308` 在偏差 `1e308` 的观测点上校正 → `2e308 = Infinity`）时拒绝，响应中不会出现 `NaN`/`Infinity`；`POST /correction-receipts` 在该情况下同样返回 422 且**不签发回执**。
- **覆盖范围外仍返回 400**（与数值边界无关），回执的 `version=2` 签名格式（载荷顺序、最短十进制、HMAC-SHA256 小写十六进制）保持不变。

## 校准规则

1. **观测登记**：每次提交参考值 `reference`（有限数值）、容差 `tolerance`（有限非负数）、同一时点 `timestamp`（须能转换为 UTC 日期）的至少 3 次**有限数值**读数。`NaN`/±`Infinity` 及无法转换为 UTC 日期的时点返回 422；有限输入算出的偏差溢出时同样 422。
2. **异常判定**：读数偏离该组**中位数**超过 `tolerance` 即标记为异常（原始读数仍完整保留）。
3. **校准偏差**：`bias = reference − 有效读数（剔除异常后）的中位数`。
4. **漂移补偿**：在相邻观测时点间对 `bias` 做**线性插值**（凸组合形式，两端偏差接近 float 上界且符号相反时中间步骤也不溢出），`校正读数 = 原始读数 + 插值偏差`。待校正读数为 `NaN`/±`Infinity` 或最终校正值溢出为非有限数时返回 422（回执入口此时不签发回执）。
5. **区间重算**：新增/修订观测只重算受影响的相邻区间（原位置缺口重新接通、新位置区间被时点拆分），其余历史区间与原始数据不变。
6. **覆盖范围**：查询时点超出 `[最早观测, 最晚观测]` 时拒绝（HTTP 400）；恰落在观测点上时直接使用该点偏差。
7. **修订与版本**：观测带单调递增的 `revision`（新建为 1）。`PUT` 修订须携带当前 `revision`，版本过期或目标时点已被占用均返回 409；成功后观测 ID 不变、版本 +1，并保存一份修订前快照（旧输入与旧偏差），可通过修订历史接口按版本倒序查询。
8. **累计漂移量**：`GET /drift-integral?from=&to=` 在覆盖范围内对当前分段线性 `bias` 做**解析积分**（精确梯形面积；段内 `bias` 过零时按解析交点拆分绝对值面积，不做固定步长采样），返回 `signed_bias_seconds`、`absolute_bias_seconds` 及按插值区间裁剪后的分段 `contributions`。窗口边界可落在区间内部；`from` 必须小于 `to`，任一边界越界返回 400。
9. **漂移超限窗口分析**：`GET /drift-exceedances?from=&to=&limit=` 在覆盖范围内**解析求解**分段线性 `bias` 与 `±limit` 的交点（不做固定步长采样），返回 `|bias| > limit` 的连续区间，每段含方向（`high`/`low`）、持续秒数与区间内峰值绝对偏差；跨观测点仍连续且同向的片段合并为一段，恰等于阈值的瞬时不计入超限（观测点恰好触及阈值时两侧不合并）。`from` 必须小于 `to`、`limit` 必须为正数，任一边界越界返回 400。
10. **漂移预算耗尽分析**：`GET /drift-budget-depletion?from=&budget=` 从 `from` 起沿当前分段线性 `bias` 累积 `|bias|·秒`，**解析求出**预算首次耗尽的精确时点：段内 `bias` 过零时先在解析零点拆分（`|bias|` 仅在拆分后的子段上才是线性函数），目标面积落入某子段时反解其累积二次式，不做固定步长采样。返回 `depleted_at`（覆盖范围内未耗尽为 `null`）、截至耗尽点（未耗尽时为覆盖终点）的 `consumed`/`remaining`，以及按插值区间裁剪的分段 `contributions`。`budget` 必须为正数，`from` 越出覆盖范围返回 400。
11. **漂移稳定窗口分析**：`GET /drift-stability-window?from=&to=&max_spread=` 在覆盖范围内**解析求出** `max(bias) − min(bias) ≤ max_spread` 的持续时间最长的连续子区间：最优窗口必是某临界带宽 `[m, m+max_spread]` 的连通分量（`m` 取遍折点取值及其下移 `max_spread` 的值），分量边界由线性段与带宽边缘的解析交点精确定位，可落在插值段内部，不做固定步长采样；时长并列时返回起点最早者。响应含起止时刻、`duration_seconds` 与窗口内实际达到的 `min_bias`/`max_bias`。`from` 必须小于 `to`、`max_spread` 必须为正数，任一边界越界返回 400。
12. **历史时点漂移补偿**：`GET /historical-correction?effective_at=&as_of=&reading=` 按 `as_of` 系统时点重建校准状态后，对目标观测时点 `effective_at` 的读数做漂移补偿。重建依据 `observations.created_at`、当前版本与 `revisions` 快照：`created_at > as_of` 的观测尚未创建、不参与；修订自 `changed_at` 起生效（恰等于 `changed_at` 时使用新版本），`changed_at > as_of` 的修订一律回滚到快照旧值。随后按重建出的观测时点排序，沿用与 `/corrections` 相同的覆盖范围与线性插值规则。查询**只读**，不改写当前观测、修订历史或区间表；响应附带 `as_of` 及所用端点的观测 ID 与版本。
13. **观测影响预演**：`POST /observation-impact-preview` 接收一条**不落库**的候选观测与正数 `impact_limit`，沿用现有异常判定算出候选偏差，预演插入对当前分段线性偏差曲线的影响。仅在候选时点两侧都已有观测时预演（候选时点已占用或无法形成双侧邻点均返回 409）：插入只会把左右最近观测之间的区间重新分段，受影响范围即 `[prev, next]`；曲线变化量 Δ 是折点 `(prev, 0) → (候选, Δc) → (next, 0)` 的分段线性函数，返回受影响时间范围、最大绝对偏差变化及其最早发生时点，以及 `|Δ| > impact_limit` 的连续区间（阈值交点解析求解，恰等于阈值的瞬时不计入，不做固定步长采样）。请求**只读**，不改写观测、修订历史或区间表。
14. **校正回执与密钥轮换**：`POST /correction-receipts` 复用 `/corrections` 的漂移补偿，并以键环当前活跃键签发 `version=2` 的 HMAC 回执：返回 `version`、`key_id`（签发密钥 ID）、归一化请求（`timestamp` 统一为 Unix 秒）、`bias`、`corrected`、`issued_at`（UTC Unix 毫秒整数）与 `receipt`。签名载荷按 `version`、`key_id`、`timestamp`、`reading`、`bias`、`corrected`、`issued_at` 顺序组成 JSON 数组，数值取 JSON 最短十进制、UTF-8 编码且无任何空白，以 `CORRECTION_RECEIPT_KEYS` 键环中 `key_id` 对应的密钥计算 HMAC-SHA256，输出小写十六进制。`POST /correction-receipts/verify` 接收完整回执，按 `key_id` **精确选键**重算签名并以**常量时间**比较：任一签名字段被改动、或 `key_id` 未知/已轮换出环均返回 `{"valid": false}`（旧键保留在环内时，轮换前签发的回执仍可验证）；结构缺失或类型错误返回 422。历史 `version=1` 回执继续按旧规则校验（密钥取自 `CORRECTION_RECEIPT_SECRET`，载荷不含 `key_id`）。键环在启动时校验：空键环、未知活跃键、非法 `key_id` 一律拒绝启动。
15. **校准排期规划**：`POST /calibration-schedules/plan` 在 `[from, to]` 窗口内规划**数量最少**的新增校准时点。固定锚点取窗口两侧能覆盖边界的现有观测（`from` 之前最近一次、`to` 之后最近一次，恰落在边界上的观测自然入选）与窗口内全部观测；在禁排时段之外插入最少的新增时点，使所有相邻锚点（含新增时点）的间隔都不超过 `max_gap`。禁排时段先**规范化合并**（丢弃空区间、排序、合并重叠或端点相接的区间），合并后区间的**开区间内部**禁止排期、**边界允许排期**；多个最少方案并列时返回**字典序最早**的一组。响应含规范化后的禁排时段、固定锚点、建议时点与逐段间隔。请求**只读**，不改写观测、修订历史或区间表；缺少覆盖边界的现有锚点或约束无解返回 409，`from ≥ to`、`max_gap ≤ 0` 或禁排区间 `from > to` 返回 400。
16. **校准曲线精简**：`POST /calibration-curves/simplify` 从当前观测（按时点排序）中**仅选择原观测的一个子序列**，首尾观测必须保留，使相邻保留点线性插值得到的精简曲线在完整覆盖范围内与原曲线的**绝对偏差逐原观测折点核对均不超过** `max_error`，并使**保留点数全局最少**（在可行弦 DAG 上做最短路）；保留点数并列时选择**时点序列字典序最早**者。每条候选弦的可行性都要逐一核对它跨越的**每个原观测折点**（不做固定步长采样，也不只查弦端点——端点偏差恒为 0）；全局实际最大误差同样由逐折点偏差取最大值得到（段内两条折线之差是线性函数，最大值必在折点），并列时取最早发生时点。响应返回保留点的 ID、时点、偏差（精简曲线经过原观测点，保留点偏差恒为 0），以及全局实际最大误差与其最早发生时点。请求**只读**，原观测、修订历史与区间表均不改写；`max_error` 必须是有限非负数（`0` 合法），否则返回 **422**；当前观测不足两条返回 **409**。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/observations` | 登记校准观测，返回偏差与异常明细（201） |
| GET | `/observations` | 列出全部观测（含原始读数与异常标记） |
| PUT | `/observations/{id}` | 修订观测（乐观锁版本校验，409 冲突；ID 不变，版本 +1） |
| GET | `/observations/{id}/revisions` | 按版本倒序列出修订前快照（旧输入与旧偏差） |
| POST | `/observation-impact-preview` | 预演插入一条观测（不落库）对偏差曲线的影响：受影响范围、最大绝对变化与超阈值区间 |
| GET | `/intervals` | 列出当前插值区间 |
| POST | `/corrections` | 对指定时点的读数做漂移补偿 |
| POST | `/correction-receipts` | 漂移补偿 + 以键环活跃键签发 version=2 HMAC-SHA256 校正回执（携带 key_id） |
| POST | `/correction-receipts/verify` | 按 key_id 精确选键、常量时间校验完整回执（兼容 version=1）：字段被改动或键已移除 valid=false，结构/类型错误 422 |
| GET | `/historical-correction` | 按 `as_of` 系统时点重建校准状态，对目标时点读数做漂移补偿（只读） |
| GET | `/drift-integral` | 对覆盖范围内分段线性偏差做精确积分（累计漂移量） |
| GET | `/drift-exceedances` | 解析求解偏差越出 ±limit 的连续区间（方向/持续秒数/峰值） |
| GET | `/drift-budget-depletion` | 解析求解漂移预算（\|bias\|·秒）首次耗尽的精确时点 |
| GET | `/drift-stability-window` | 解析求解 max(bias) − min(bias) ≤ max_spread 的最长连续稳定子区间 |
| POST | `/calibration-schedules/plan` | 规划窗口内数量最少的新增校准时点（禁排之外、相邻间隔 ≤ max_gap、字典序最早；只读） |
| POST | `/calibration-curves/simplify` | 精简当前观测曲线：全局最少保留点子序列（首尾必留）、逐折点误差 ≤ max_error、字典序并列取最早（只读） |
| GET | `/health` | 健康检查 |

时间戳支持 Unix 秒（数字）或 ISO 8601 字符串（如 `2026-09-18T08:00:00Z`），缺省为服务器当前时间；时点必须能转换为 UTC 日期，`NaN`/±`Infinity` 及超出可表达范围的值返回 422（见上文「输入边界」）。

### 登记观测

```bash
curl -X POST http://localhost:8000/observations \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T08:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [99.0, 99.2, 98.8, 150.0]}'
```

响应（`150.0` 偏离组中位数 `99.1` 超差，判为异常；`bias = 100.0 − 99.0 = 1.0`）：

```json
{
  "id": 1, "revision": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
  "reference": 100.0, "tolerance": 0.5, "group_median": 99.1, "bias": 1.0,
  "readings": [
    {"value": 99.0, "is_outlier": false}, {"value": 99.2, "is_outlier": false},
    {"value": 98.8, "is_outlier": false}, {"value": 150.0, "is_outlier": true}
  ],
  "outliers": [150.0]
}
```

### 修订观测

修订请求体与登记一致（`reference`、`tolerance`、`readings`、`timestamp`），额外携带客户端持有的当前 `revision`。服务在单个事务内重新判定异常、重算偏差，并重建**原位置与新位置**受影响的相邻插值区间；观测 ID 保持不变。版本不匹配（已被他人修订）或目标时间戳已被其它观测占用时返回 409，数据不变。

```bash
curl -X PUT http://localhost:8000/observations/1 \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T08:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [99.1, 99.0, 99.2], "revision": 1}'
```

响应与登记观测相同，`revision` 推进为 `2`：

```json
{
  "id": 1, "revision": 2, "timestamp": 1789718400.0,
  "timestamp_iso": "2026-09-18T08:00:00+00:00",
  "reference": 100.0, "tolerance": 0.5, "group_median": 99.1, "bias": 0.9,
  "readings": [
    {"value": 99.1, "is_outlier": false}, {"value": 99.0, "is_outlier": false},
    {"value": 99.2, "is_outlier": false}
  ],
  "outliers": []
}
```

版本冲突（或时间戳冲突）响应：

```json
{"detail": "observation 1 is at revision 2, not 1; fetch the latest observation and retry"}
```

修订的输入边界与登记观测完全一致（时点、参考值、容差、读数均拒绝 `NaN`/±`Infinity`，时点须能转换为 UTC 日期，有限输入偏差溢出同样 422）；被拒绝的修订不会推进版本，观测、读数、区间与修订快照均保持原样。

### 查询修订历史

每次成功修订都会先保存一份修订前快照。按版本倒序返回变更时间、旧输入（旧时点、旧参考值、旧容差、旧读数）与旧偏差：

```bash
curl http://localhost:8000/observations/1/revisions
```

```json
[
  {
    "revision": 1,
    "changed_at": 1789720200.0, "changed_at_iso": "2026-09-18T08:30:00+00:00",
    "old_timestamp": 1789718400.0, "old_timestamp_iso": "2026-09-18T08:00:00+00:00",
    "old_reference": 100.0, "old_tolerance": 0.5, "old_bias": 1.0,
    "old_readings": [99.0, 99.2, 98.8, 150.0]
  }
]
```

从未修订过的观测返回空数组 `[]`；观测 ID 不存在时两个接口均返回 404。

### 观测影响预演

`POST /observation-impact-preview` 回答“如果现在插入这条观测，偏差曲线会发生什么变化”，但**不落库**：候选观测沿用与登记观测相同的异常判定算出偏差，仅在候选时点两侧都已有观测时预演。插入只会把左右最近观测之间的区间重新分段，受影响范围即 `[prev, next]`；曲线变化量 Δ 是折点 `(prev, 0) → (候选, Δc) → (next, 0)` 的分段线性函数。响应给出受影响时间范围、最大绝对偏差变化 `max_abs_change` 及其最早发生时点 `max_change_at`，并按时间列出 `|Δ| > impact_limit` 的连续区间——阈值交点由线性段解析求解，不做固定步长采样，恰等于阈值的瞬时不计入。请求只读，不改写观测、修订历史或区间表。

沿用漂移补偿一节的场景（08:00 `bias 1.0`、10:00 `bias 3.0`），预演在 09:00 插入一条 `bias 4.5` 的观测（当前曲线在 09:00 的插值为 `2.0`，故 Δc = 2.5）：

```bash
curl -X POST http://localhost:8000/observation-impact-preview \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T09:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [95.4, 95.5, 95.6], "impact_limit": 1.0}'
```

```json
{
  "timestamp": 1789722000.0, "timestamp_iso": "2026-09-18T09:00:00+00:00",
  "impact_limit": 1.0,
  "candidate_bias": 4.5, "current_bias": 2.0,
  "affected_from": 1789718400.0, "affected_from_iso": "2026-09-18T08:00:00+00:00",
  "affected_to": 1789725600.0, "affected_to_iso": "2026-09-18T10:00:00+00:00",
  "max_abs_change": 2.5,
  "max_change_at": 1789722000.0, "max_change_at_iso": "2026-09-18T09:00:00+00:00",
  "impact_intervals": [
    {"t_start": 1789719840.0, "t_end": 1789724160.0,
     "start_iso": "2026-09-18T08:24:00+00:00", "end_iso": "2026-09-18T09:36:00+00:00",
     "duration_seconds": 4320.0}
  ]
}
```

`|Δ| > 1.0` 的区间两端（08:24、09:36）即变化量曲线与阈值 `1.0` 的解析交点；候选偏差恰等于当前插值时 `max_abs_change` 为 `0`、`impact_intervals` 为空。候选时点已被占用、或候选时点落在最早/最晚观测之外（无法形成双侧邻点）时返回 409；`impact_limit ≤ 0` 返回 400；时点/参考值/容差/读数含 `NaN`/±`Infinity`、时点无法转换为 UTC 日期，或有限输入下偏差/变化量溢出时返回 422（输入边界与登记观测完全一致）。

### 漂移补偿

```bash
curl -X POST http://localhost:8000/corrections \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T09:00:00Z", "reading": 98.0}'
```

响应包含校正值、所用区间及两端点观测的异常明细：

```json
{
  "timestamp": 1789722000.0, "timestamp_iso": "2026-09-18T09:00:00+00:00",
  "reading": 98.0, "bias": 2.0, "corrected": 100.0,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 1.0, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
     "bias": 1.0, "outliers": [150.0]},
    {"observation_id": 2, "timestamp": 1789725600.0, "timestamp_iso": "2026-09-18T10:00:00+00:00",
     "bias": 3.0, "outliers": []}
  ]
}
```

### 校正回执（HMAC 签名与密钥轮换）

`POST /correction-receipts` 的请求体与 `/corrections` 相同，内部**完全复用**漂移补偿（同样的覆盖范围判定与线性插值，越界返回 400），但只回传校正结果与一份可离线校验的签名回执，不携带区间/端点明细：

```bash
curl -X POST http://localhost:8000/correction-receipts \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T09:00:00Z", "reading": 98.0}'
```

```json
{
  "version": 2,
  "key_id": "dev",
  "request": {"timestamp": 1789722000.0, "reading": 98.0},
  "bias": 2.0,
  "corrected": 100.0,
  "issued_at": 1789844210712,
  "receipt": "3b11c61ece8baedc529a22eedc78d1f508309b5c61c7504a17afb709d982a764"
}
```

- `version`：回执格式版本，当前固定为 `2`。
- `key_id`：签发密钥 ID，即 `CORRECTION_RECEIPT_ACTIVE_KEY` 指定的键环键名（上例为开发默认键 `dev`）。
- `request`：**归一化请求**——ISO 8601/数字字符串等时间格式统一为 Unix 秒数值，`reading` 为数值。
- `issued_at`：签发时刻，UTC Unix **毫秒整数**。
- `receipt`：HMAC-SHA256 小写十六进制摘要（64 个字符）。签名载荷是把 `version`、`key_id`、`timestamp`、`reading`、`bias`、`corrected`、`issued_at` 按此顺序组成的 **JSON 数组**，数值取 JSON 最短十进制（如上例即 `[2,"dev",1789722000.0,98.0,2.0,100.0,1789844210712]`），以 UTF-8 编码、不含任何空白后，用键环中 `key_id` 对应的密钥计算 HMAC。

校验时把**完整回执原样**回传；验签按 `key_id` 从键环**精确取键**重算签名：

```bash
curl -X POST http://localhost:8000/correction-receipts/verify \
  -H 'Content-Type: application/json' \
  -d '{"version":2,"key_id":"dev","request":{"timestamp":1789722000.0,"reading":98.0},
       "bias":2.0,"corrected":100.0,"issued_at":1789844210712,
       "receipt":"3b11c61ece8baedc529a22eedc78d1f508309b5c61c7504a17afb709d982a764"}'
# {"valid":true}
```

任一签名字段（含 `version`、`key_id`、`request.timestamp`、`request.reading`、`bias`、`corrected`、`issued_at`、`receipt`）被改动，或 `key_id` 不在当前键环中（未知或已轮换移除），均返回 `{"valid": false}`；校验只重算签名做常量时间比较，不会因数据库中的校准状态变化而失效。字段缺失或类型错误（例如时间戳传字符串、`issued_at` 传小数、`receipt` 传数字、`version=2` 缺 `key_id`、整体不是 JSON 对象）一律返回 **422**。未签名的额外字段不影响校验结果。

#### 密钥轮换

签名密钥由两个环境变量管理（详见下文「环境变量」）：`CORRECTION_RECEIPT_KEYS`（JSON 对象，`key_id → 密钥`）构成签名/验签键环，`CORRECTION_RECEIPT_ACTIVE_KEY` 指定当前签发键。轮换分两步，无需任何数据迁移：

```bash
# 1) 加入新键并切换活跃键，旧键保留在环内：
#    新回执改用 2026-09 签发，此前 2026-08 签发的回执仍可验证
CORRECTION_RECEIPT_KEYS='{"2026-08":"old-secret","2026-09":"new-secret"}' \
CORRECTION_RECEIPT_ACTIVE_KEY=2026-09 \
docker compose up -d

# 2) 确认不再有需要验证的旧回执后，把旧键移出键环：
#    此后携带 key_id=2026-08 的回执验签返回 {"valid": false}
CORRECTION_RECEIPT_KEYS='{"2026-09":"new-secret"}' \
CORRECTION_RECEIPT_ACTIVE_KEY=2026-09 \
docker compose up -d
```

键环在**启动时**校验，以下配置一律拒绝启动：空键环（`""`/`{}`）、非 JSON 对象、非法 `key_id`（须为 1–64 字符：字母或数字开头，仅含字母、数字、`.`、`_`、`-`）、空密钥、活跃键不在键环中。

#### version=1 历史回执兼容

升级前签发的 `version=1` 回执继续有效：验签沿用旧规则——载荷为 `[1,timestamp,reading,bias,corrected,issued_at]`（不含 `key_id`），密钥取自 `CORRECTION_RECEIPT_SECRET`，与键环无关。例如下面这份以开发默认密钥签发的 v1 回执：

```bash
curl -X POST http://localhost:8000/correction-receipts/verify \
  -H 'Content-Type: application/json' \
  -d '{"version":1,"request":{"timestamp":1789722000.0,"reading":98.0},
       "bias":2.0,"corrected":100.0,"issued_at":1789844210712,
       "receipt":"656aa4129125843e2fc1a62bbc8a6bda8f55486f4794160ea228d4572724322e"}'
# {"valid":true}
```

### 历史时点漂移补偿（as_of 状态重建）

`GET /historical-correction?effective_at=&as_of=&reading=` 回答“以 `as_of` 系统时点所能看到的校准状态，如何校正 `effective_at` 时点的读数”。服务用 `observations.created_at`、当前版本与 `revisions` 快照重建 `as_of` 状态：尚未创建的观测不参与；修订自 `changed_at` 起生效（恰等于变更时刻用新版本），晚于 `as_of` 的修订回滚到快照旧值。随后按重建出的观测时点排序，套用与 `/corrections` 完全相同的覆盖范围与线性插值规则。查询只读，不改写当前观测、修订历史或区间表。

沿用上文场景：两条观测（08:00 `bias 1.0`、10:00 `bias 3.0`）均已在 08:15 前登记，观测 1 的修订（`bias 1.0 → 0.9`，版本 1 → 2）于 08:30 生效。查询 09:00 的读数 `98.0`：

```bash
# as_of 在修订之前：看到旧版本（revision 1，bias 1.0）
curl "http://localhost:8000/historical-correction?effective_at=2026-09-18T09:00:00Z&as_of=2026-09-18T08:15:00Z&reading=98.0"
```

```json
{
  "effective_at": 1789722000.0, "effective_at_iso": "2026-09-18T09:00:00+00:00",
  "as_of": 1789719300.0, "as_of_iso": "2026-09-18T08:15:00+00:00",
  "reading": 98.0, "bias": 2.0, "corrected": 100.0,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 1.0, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "revision": 1, "timestamp": 1789718400.0,
     "timestamp_iso": "2026-09-18T08:00:00+00:00", "bias": 1.0},
    {"observation_id": 2, "revision": 1, "timestamp": 1789725600.0,
     "timestamp_iso": "2026-09-18T10:00:00+00:00", "bias": 3.0}
  ]
}
```

```bash
# as_of 在修订之后：看到新版本（revision 2，bias 0.9），中点插值变为 1.95
curl "http://localhost:8000/historical-correction?effective_at=2026-09-18T09:00:00Z&as_of=2026-09-18T08:45:00Z&reading=98.0"
```

```json
{
  "effective_at": 1789722000.0, "effective_at_iso": "2026-09-18T09:00:00+00:00",
  "as_of": 1789721100.0, "as_of_iso": "2026-09-18T08:45:00+00:00",
  "reading": 98.0, "bias": 1.95, "corrected": 99.95,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 0.9, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "revision": 2, "timestamp": 1789718400.0,
     "timestamp_iso": "2026-09-18T08:00:00+00:00", "bias": 0.9},
    {"observation_id": 2, "revision": 1, "timestamp": 1789725600.0,
     "timestamp_iso": "2026-09-18T10:00:00+00:00", "bias": 3.0}
  ]
}
```

`effective_at` 越出重建后的覆盖范围（含 `as_of` 时无任何观测）时返回 400；恰落在重建后的观测点上时直接使用该点偏差（`interval` 为 `null`）。

### 累计漂移量积分

对 `[from, to]` 窗口内的分段线性 `bias` 做解析积分。窗口边界可落在区间内部（按插值裁剪）；段内 `bias` 过零时，绝对值面积在解析零点处拆成两个三角形求和，全程无固定步长采样。各分段贡献按时间排序，其 `signed`/`absolute` 面积之和分别等于汇总值。

```bash
curl "http://localhost:8000/drift-integral?from=2026-09-18T08:00:00Z&to=2026-09-18T10:00:00Z"
```

响应（沿用上文 08:00 `bias 1.0` → 10:00 `bias 3.0` 的区间，梯形面积 `(1+3)/2 × 7200s`）：

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "to": 1789725600.0, "to_iso": "2026-09-18T10:00:00+00:00",
  "signed_bias_seconds": 14400.0,
  "absolute_bias_seconds": 14400.0,
  "contributions": [
    {"t_start": 1789718400.0, "t_end": 1789725600.0,
     "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
     "bias_start": 1.0, "bias_end": 3.0,
     "signed_bias_seconds": 14400.0, "absolute_bias_seconds": 14400.0}
  ]
}
```

若段内 `bias` 变号（如 `2.0 → −2.0`），`signed_bias_seconds` 中正负面积相互抵消（此处为 0），`absolute_bias_seconds` 则为零点两侧两个三角形面积之和（此处为 `7200.0`）。

### 漂移超限窗口分析

对 `[from, to]` 窗口内的分段线性 `bias` 解析求解与 `±limit` 的交点，返回 `|bias| > limit` 的连续区间。每段给出方向（`high`：`bias > limit`；`low`：`bias < −limit`）、持续秒数与区间内峰值绝对偏差；跨观测点仍连续且同向的片段合并为一段，恰等于阈值的瞬时不计入超限（观测点恰好触及阈值时，两侧超限不合并）。全程解析解，不做固定步长采样。

沿用上文观测，再补一条 12:00 的观测（`bias = −3.0`）：

```bash
curl -X POST http://localhost:8000/observations \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T12:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [103.0, 103.1, 102.9]}'

curl "http://localhost:8000/drift-exceedances?from=2026-09-18T08:00:00Z&to=2026-09-18T12:00:00Z&limit=2.0"
```

响应（`bias` 1.0 → 3.0 → −3.0：09:00 越上阈、10:20 回落，两段跨 10:00 观测点合并；11:40 跌破下阈）：

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "to": 1789732800.0, "to_iso": "2026-09-18T12:00:00+00:00",
  "limit": 2.0,
  "exceedances": [
    {"t_start": 1789722000.0, "t_end": 1789726800.0,
     "start_iso": "2026-09-18T09:00:00+00:00", "end_iso": "2026-09-18T10:20:00+00:00",
     "direction": "high", "duration_seconds": 4800.0, "peak_abs_deviation": 3.0},
    {"t_start": 1789731600.0, "t_end": 1789732800.0,
     "start_iso": "2026-09-18T11:40:00+00:00", "end_iso": "2026-09-18T12:00:00+00:00",
     "direction": "low", "duration_seconds": 1200.0, "peak_abs_deviation": 3.0}
  ]
}
```

窗口内从未超限时 `exceedances` 为空数组；`from ≥ to`、`limit ≤ 0` 或窗口边界越出覆盖范围均返回 400。

### 漂移预算耗尽分析

`GET /drift-budget-depletion?from=&budget=` 从 `from` 起沿当前分段线性 `bias` 累积 `|bias|·秒`，解析求出预算首次耗尽的精确时点。逐插值区间推进：段内 `bias` 过零时先在解析零点拆分（`|bias|` 仅在拆分后的子段上才是线性函数，其累积面积是时长的二次函数），目标面积落入某子段时反解该二次式得到精确耗尽时点，全程无固定步长采样。

沿用上文 08:00 `bias 1.0` → 10:00 `bias 3.0` 的区间（`|bias|` 从 1.0 线性升到 3.0，预算 5400 恰在 09:00 耗尽：`(1+2)/2 × 3600s`）：

```bash
curl "http://localhost:8000/drift-budget-depletion?from=2026-09-18T08:00:00Z&budget=5400"
```

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "budget": 5400.0,
  "depleted_at": 1789722000.0, "depleted_at_iso": "2026-09-18T09:00:00+00:00",
  "consumed": 5400.0, "remaining": 0.0,
  "coverage_end": 1789725600.0, "coverage_end_iso": "2026-09-18T10:00:00+00:00",
  "contributions": [
    {"t_start": 1789718400.0, "t_end": 1789722000.0,
     "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T09:00:00+00:00",
     "bias_start": 1.0, "bias_end": 2.0, "absolute_bias_seconds": 5400.0}
  ]
}
```

预算未在覆盖范围内耗尽时 `depleted_at` 为 `null`，`consumed` 为到覆盖终点的全部累计量、`remaining` 为剩余预算；`contributions` 按插值区间裁剪到 `[from, 耗尽点或覆盖终点]`，各段 `absolute_bias_seconds` 之和即 `consumed`。`budget ≤ 0` 或 `from` 越出覆盖范围返回 400。

### 漂移稳定窗口分析

`GET /drift-stability-window?from=&to=&max_spread=` 在 `[from, to]` 内解析求出 `max(bias) − min(bias) ≤ max_spread` 的持续时间最长的连续子区间。最优窗口必是某临界带宽 `[m, m+max_spread]` 的连通分量——`m` 只需取遍各折点取值 `b_i` 与 `b_i − max_spread`（带宽边缘未触及折点取值时，微调 `m` 可让分量至少一端继续外伸、时长严格增大）；分量边界由线性段与带宽边缘的**解析交点**精确定位，可落在插值段内部，全程无固定步长采样。时长并列时返回起点最早者。

沿用上文观测（08:00 `bias 1.0` → 10:00 `bias 3.0` → 12:00 `bias −3.0`），允许波动 `max_spread=2.0` 时，最长稳定窗口为 08:00–10:40（`bias` 1.0 → 3.0 → 1.0，波动恰为 2.0，右端点是 `bias` 回落至 1.0 的解析交点）：

```bash
curl "http://localhost:8000/drift-stability-window?from=2026-09-18T08:00:00Z&to=2026-09-18T12:00:00Z&max_spread=2.0"
```

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "to": 1789732800.0, "to_iso": "2026-09-18T12:00:00+00:00",
  "max_spread": 2.0,
  "t_start": 1789718400.0, "t_end": 1789728000.0,
  "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:40:00+00:00",
  "duration_seconds": 9600.0,
  "min_bias": 1.0, "max_bias": 3.0
}
```

`min_bias`/`max_bias` 为窗口内实际达到的极值（可能小于 `max_spread` 所允许的带宽）；`from ≥ to`、`max_spread ≤ 0` 或窗口边界越出覆盖范围均返回 400。

### 校准排期规划

`POST /calibration-schedules/plan` 回答“在 `[from, to]` 窗口内至少还要安排几次校准、各在什么时刻”，请求**只读**，不改写观测、修订历史或区间表。请求体含 `from`、`to`、`max_gap`（正数，秒）与可选 `blackout` 禁排时段数组：

- **固定锚点**：`from` 之前最近一次观测、`to` 之后最近一次观测（覆盖窗口边界，恰落在边界上的观测自然入选）与窗口内全部观测。
- **禁排规范化**：禁排时段先丢弃空区间（`from == to`）、排序并合并一切重叠或端点相接的区间；合并后区间的**开区间内部**禁止排期，**边界允许排期**（相接两段合并后，原相接点成为内部、同样禁止）。
- **最少时点**：在禁排之外插入数量最少的新增时点，使所有相邻锚点（含新增时点）的间隔都不超过 `max_gap`；多个最少方案并列时返回**字典序最早**的一组（先让第一个时点尽量早，依次类推）。

响应含规范化后的禁排时段、固定锚点（观测 ID 与时点）、建议时点，以及固定锚点与建议时点合并排序后的逐段间隔（`duration_seconds` 均不超过 `max_gap`，端点以 `start_kind`/`end_kind` 区分 `fixed`/`suggested`）。

沿用 08:00、12:00 两条观测的场景，规划 08:00–12:00 窗口（`max_gap` 2 小时，09:30–10:30 禁排）：

```bash
curl -X POST http://localhost:8000/calibration-schedules/plan \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-09-18T08:00:00Z", "to": "2026-09-18T12:00:00Z",
       "max_gap": 7200,
       "blackout": [{"from": "2026-09-18T09:30:00Z", "to": "2026-09-18T10:30:00Z"}]}'
```

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "to": 1789732800.0, "to_iso": "2026-09-18T12:00:00+00:00",
  "max_gap": 7200.0,
  "blackout": [
    {"from": 1789723800.0, "from_iso": "2026-09-18T09:30:00+00:00",
     "to": 1789727400.0, "to_iso": "2026-09-18T10:30:00+00:00"}
  ],
  "fixed_anchors": [
    {"observation_id": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00"},
    {"observation_id": 2, "timestamp": 1789732800.0, "timestamp_iso": "2026-09-18T12:00:00+00:00"}
  ],
  "suggested": [
    {"timestamp": 1789720200.0, "timestamp_iso": "2026-09-18T08:30:00+00:00"},
    {"timestamp": 1789727400.0, "timestamp_iso": "2026-09-18T10:30:00+00:00"}
  ],
  "segments": [
    {"t_start": 1789718400.0, "t_end": 1789720200.0, "duration_seconds": 1800.0,
     "start_kind": "fixed", "end_kind": "suggested", "...": "..."},
    {"t_start": 1789720200.0, "t_end": 1789727400.0, "duration_seconds": 7200.0,
     "start_kind": "suggested", "end_kind": "suggested", "...": "..."},
    {"t_start": 1789727400.0, "t_end": 1789732800.0, "duration_seconds": 5400.0,
     "start_kind": "suggested", "end_kind": "fixed", "...": "..."}
  ]
}
```

无禁排时本例只需 1 个时点（10:00），但它落在禁排开区间 (09:30, 10:30) 内，故最少方案为 2 个时点（08:30 与禁排边界 10:30）。缺少覆盖边界的现有锚点（`from` 之前或 `to` 之后无任何观测）或约束无解（禁排封死所有可行位置，例如禁排段比 `max_gap` 还宽）返回 **409**；`from ≥ to`、`max_gap ≤ 0` 或禁排区间 `from > to` 返回 **400**。

### 校准曲线精简

`POST /calibration-curves/simplify` 回答“保留哪些观测点，就能用一条更短的分段线性曲线在容差内代表当前偏差曲线”，请求**只读**，不改写观测、修订历史或区间表。请求体只有一个参数 `max_error`（允许的最大绝对偏差，必须是**有限非负数**，`0` 合法）。

规则：当前观测按时点排序，**首尾观测必须保留**；只允许从**原观测**中选择子序列（不引入任何新时点）；每条候选弦（相邻两个保留点的连线）都要**逐一核对它跨越的每个原观测折点**——`|b_k − 弦在 t_k 的线性插值| ≤ max_error`，不做固定步长采样，也不只查弦端点（端点偏差恒为 0）。在所有可行弦构成的 DAG 上求边权 1 的最短路，即得**全局最少**保留点数；保留点数并列时选择**时点序列字典序最早**的一组。

沿用 08:00–12:00 每小时一条观测的场景，偏差为 `0.0、0.25、1.0、0.25、0.0`，容差 `0.5`：

```bash
curl -X POST http://localhost:8000/calibration-curves/simplify \
  -H 'Content-Type: application/json' \
  -d '{"max_error": 0.5}'
```

```json
{
  "max_error": 0.5,
  "coverage_from": 1789718400.0, "coverage_from_iso": "2026-09-18T08:00:00+00:00",
  "coverage_to": 1789732800.0, "coverage_to_iso": "2026-09-18T12:00:00+00:00",
  "original_count": 5,
  "retained_count": 3,
  "retained": [
    {"observation_id": 1, "timestamp": 1789718400.0,
     "timestamp_iso": "2026-09-18T08:00:00+00:00", "bias": 0.0},
    {"observation_id": 3, "timestamp": 1789725600.0,
     "timestamp_iso": "2026-09-18T10:00:00+00:00", "bias": 1.0},
    {"observation_id": 5, "timestamp": 1789732800.0,
     "timestamp_iso": "2026-09-18T12:00:00+00:00", "bias": 0.0}
  ],
  "max_abs_error": 0.25,
  "max_error_at": 1789722000.0,
  "max_error_at_iso": "2026-09-18T09:00:00+00:00"
}
```

精简后只剩 08:00、10:00、12:00 三条观测：弦 08:00→10:00（偏差 0→1）在 09:00 的插值为 0.5，相对原偏差 0.25 的绝对偏差恰为 `0.25`；11:00 一侧对称，偏差同为 `0.25`。全局实际最大误差 `0.25` 由**逐原观测折点核对**得到（段内两折线之差是线性函数，最大值必落在折点），09:00 与 11:00 并列时报告**最早**发生时点 09:00。保留点处精简曲线与原曲线重合，故保留点自身偏差恒为 0（响应中的 `bias` 是该观测点的校准偏差）。

- `max_error = 0` 时只有严格共线的中间折点可丢弃；一般情况下全部观测都会保留。
- 当前观测不足两条（0 或 1 条）时无法构成有覆盖范围的曲线，返回 **409**。
- `max_error` 为负数、`NaN`/`Infinity`、字符串、布尔值或缺失时返回 **422**。

### 错误码

| 状态码 | 场景 |
| --- | --- |
| 400 | 查询时点超出校准覆盖范围（响应含可用区间；历史补偿按重建后的覆盖范围判定）；积分/超限/稳定窗口 `from ≥ to` 或任一边界越界；超限阈值 `limit ≤ 0`；稳定窗口 `max_spread ≤ 0`；预算耗尽起点 `from` 越界或 `budget ≤ 0`；影响预演 `impact_limit ≤ 0`；排期规划 `from ≥ to`、`max_gap ≤ 0` 或禁排区间 `from > to` |
| 404 | 修订/查询修订历史时观测 ID 不存在 |
| 409 | 同一时点重复登记观测；修订时版本过期或目标时点已被占用；影响预演候选时点已占用或两侧无法形成邻点；排期规划缺少覆盖边界的现有锚点或禁排约束下无解；曲线精简时当前观测不足两条 |
| 422 | 读数不足 3 次、全部读数被判异常、参数非法；观测输入（时点/参考值/容差/读数）出现 `NaN`/±`Infinity`、时点无法转换为 UTC 日期，或有限输入算出的偏差/影响变化溢出为非有限值；漂移补偿（`/corrections`、`/historical-correction`、`/correction-receipts`）的待校正读数为 `NaN`/±`Infinity` 或最终校正值溢出为非有限值（此时不签发回执）；曲线精简 `max_error` 非有限非负数/类型错误/缺失；回执校验请求结构缺失或字段类型错误（含 version=2 缺 `key_id`） |

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 运行测试
uvicorn app.main:app --reload       # 本地启动（默认 ./calibration.db）
```

环境变量：

- `CALIB_DB_PATH`：SQLite 文件路径（容器内默认 `/data/calibration.db`，本地开发默认 `./calibration.db`）。
- `CORRECTION_RECEIPT_KEYS`：校正回执 v2 签名/验签键环，JSON 对象（`key_id → 密钥`）。`key_id` 须为 1–64 个字符：字母或数字开头，仅含字母、数字、`.`、`_`、`-`；密钥为非空字符串。启动时校验：空键环（`""`/`{}`）、非 JSON 对象、非法 `key_id`、空密钥均拒绝启动。未设置时回退为单键开发环 `{"dev": <CORRECTION_RECEIPT_SECRET 或开发默认值>}`，仅供本地调试。**生产部署必须覆盖**，例如：

  ```bash
  # 单条命令启动：注入随机密钥键环并指定签发键，前台运行，监听 8000 端口
  CORRECTION_RECEIPT_KEYS="{\"2026-09\":\"$(openssl rand -hex 32)\"}" \
  CORRECTION_RECEIPT_ACTIVE_KEY=2026-09 \
  docker compose up --build
  ```

- `CORRECTION_RECEIPT_ACTIVE_KEY`：当前签发键的 `key_id`（缺省 `dev`），必须存在于键环中，否则拒绝启动。轮换流程见上文「密钥轮换」：新键加入环内并切换活跃键后，保留旧键即可继续验证轮换前签发的回执；旧键移出键环后，携带该 `key_id` 的回执返回 `valid=false`。
- `CORRECTION_RECEIPT_SECRET`：`version=1` 历史回执的验签密钥（也是未配置键环时开发环的密钥来源）。未设置时 Compose 与应用会回退到开发默认值 `dev-only-insecure-correction-receipt-secret`（仅供本地调试，任何持有源码者都能伪造回执，切勿用于生产）。更换该密钥后，旧密钥签发的 v1 回执将校验失败（`valid=false`）。

## 项目结构

```
app/
  main.py         # FastAPI 路由
  service.py      # 观测入库、相邻区间重算、补偿查询、as_of 历史状态重建、漂移积分、超限窗口分析、预算耗尽分析、稳定窗口分析、观测影响预演、校准排期规划、校准曲线精简（只读）
  receipts.py     # 校正回执：复用漂移补偿、版本化 HMAC-SHA256 签名与常量时间校验（兼容 v1）
  receipt_keys.py # 回执密钥轮换：键环解析、key_id 校验、活跃键选择、启动时校验
  calibration.py  # 纯计算：异常判定、偏差、线性插值、分段积分、超限交点求解、预算累积反解、稳定窗口求解、插入影响解析比较、排期规划（禁排合并与最少时点求解）、曲线精简（可行弦 DAG 最短路与逐折点误差）
  models.py       # Pydantic 请求/响应模型
  db.py           # SQLite 连接与建表
tests/test_api.py                            # 端到端 API 测试
tests/test_numeric_bounds.py                 # 数值边界测试：NaN/±Infinity 拒绝、时点 UTC 可转换性、偏差溢出与事务原子性
tests/test_correction_receipts.py            # 校正回执签发/校验/密钥轮换/v1 兼容端到端测试
tests/test_receipt_keys.py                   # 键环加载与启动校验测试
tests/test_schedule_plan.py                  # 排期规划纯算法单测（禁排合并、最少时点、字典序最早、无解判定）
tests/test_schedule_plan_api.py              # 排期规划 API 端到端测试（锚点选择、禁排边界、409/400、只读）
tests/test_calibration_curve_simplify.py     # 曲线精简纯算法单测（全局最少、首尾必留、字典序并列、逐折点误差、穷举对照）
tests/test_calibration_curve_simplify_api.py # 曲线精简 API 端到端测试（保留点/全局误差、422/409、只读）
Dockerfile / docker-compose.yml
```
