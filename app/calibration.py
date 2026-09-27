"""纯计算逻辑：异常判定、校准偏差、线性插值、分段线性积分、超限区间求解、预算累积反解、稳定窗口求解与校准排期规划。不触碰数据库，便于单测。"""
from __future__ import annotations

import math


class CalibrationError(ValueError):
    """输入读数无法计算有效校准偏差时抛出。"""


def _mean_same_sign(a: float, b: float) -> float:
    """求两个同号（或其一为零）有限数的均值，且不发生上溢。

    a+b 有限时直接 (a+b)/2，结果与 statistics.median 的偶数组中点计算
    完全一致；a+b 会溢出（两数同号且幅值接近 float 上界）时改用
    a/2 + b/2——两数同号时除以 2 后幅值减半，加法不再溢出。极大但
    有限的同值读数（如两个 1e308）因此仍能得到 1e308，而不会被
    (a+b)/2 误算成 ±Infinity。
    """
    if math.isfinite(a + b):
        return (a + b) / 2.0
    return a / 2.0 + b / 2.0


def finite_median(values: list[float]) -> float:
    """返回有限读数列表的中位数，偶数个读数时中点均值不会上溢。

    statistics.median 对偶数个读数按 (a+b)/2 求中点；当 a、b 同为接近
    float 上界的有限数时 a+b 溢出为 Infinity，会把本应有限的中位数
    误算成 Infinity（进而把所有读数误判为异常）。此处仅在中点求和会
    溢出时走 _mean_same_sign 的不溢出分支，其余情形与 statistics.median
    完全一致。
    """
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return _mean_same_sign(values[mid - 1], values[mid])


def split_outliers(readings: list[float], tolerance: float) -> tuple[float, list[float], list[float]]:
    """按“偏离组中位数超过容差即异常”拆分读数。

    返回 (组中位数, 有效读数, 异常读数)。
    """
    group_median = finite_median(readings)
    inliers = [r for r in readings if abs(r - group_median) <= tolerance]
    outliers = [r for r in readings if abs(r - group_median) > tolerance]
    return group_median, inliers, outliers


def compute_bias(reference: float, tolerance: float, readings: list[float]) -> tuple[float, float, list[float], list[float]]:
    """校准偏差 = 参考值 - 有效读数（剔除异常后）的中位数。

    返回 (bias, 组中位数, 有效读数, 异常读数)。
    """
    if len(readings) < 3:
        raise CalibrationError(f"need at least 3 readings, got {len(readings)}")
    group_median, inliers, outliers = split_outliers(readings, tolerance)
    if not inliers:
        raise CalibrationError(
            "all readings deviate from the group median by more than the tolerance; "
            "cannot compute a calibration bias"
        )
    inlier_median = finite_median(inliers)
    bias = reference - inlier_median
    if not math.isfinite(bias):
        # reference 与有效读数中位数均为有限值，但二者之差溢出（符号相反、
        # 幅值接近 float 上界）：无穷偏差无法入库，按输入不可处理拒绝（422）。
        raise CalibrationError(
            "calibration bias overflows to a non-finite value "
            "(reference and valid readings are too far apart)"
        )
    if not math.isfinite(group_median):
        raise CalibrationError("group median is not finite")
    return bias, group_median, inliers, outliers


def interpolate(t: float, t0: float, b0: float, t1: float, b1: float) -> float:
    """在相邻时点 (t0, b0) 与 (t1, b1) 之间对 t 做线性插值（凸组合形式）。

    结果写成 (1−w)·b0 + w·b1，其中 w = (t−t0)/(t1−t0) ∈ [0, 1]，
    而不是 b0 + (b1−b0)·w：两端偏差幅值接近 float 上界且符号相反时
    （如 b0 = 1e308、b1 = −1e308），b1−b0 会先溢出为 ±Infinity，
    使本应有限的插值结果变成 Infinity。凸组合只做 [0,1] 权重的缩放与
    相加——两端同号时 |结果| ≤ max(|b0|, |b1|) 天然不溢出；两端异号时
    缩放先把两数幅值各减半以下，相加也不会溢出。恰在端点（w 为 0/1）时
    结果严格等于原端点偏差，观测点与区间端点的偏差始终保持原样。
    """
    if t1 == t0:
        return b0
    w = (t - t0) / (t1 - t0)
    return (1.0 - w) * b0 + w * b1


def integrate_segment(t0: float, b0: float, t1: float, b1: float) -> tuple[float, float]:
    """对线性段 (t0, b0)-(t1, b1) 下的面积做解析积分，返回 (signed, absolute)。

    signed 为梯形面积 (b0+b1)/2·Δt。absolute 为 |bias| 的面积：端点严格异号时
    段内恰有一个零点 t* = t0 − b0·Δt/(b1−b0)，在 t* 处拆成两个三角形求和
    （面积为 (b0² + b1²)·Δt / (2·|b1−b0|)）；否则 |bias| 不变号，面积即 |signed|。
    全程解析解，不做固定步长采样。
    """
    dt = t1 - t0
    if dt <= 0:
        return 0.0, 0.0
    signed = (b0 + b1) * 0.5 * dt
    if b0 * b1 < 0:  # 严格异号：段内恰有一个零点，按解析交点拆分
        absolute = (b0 * b0 + b1 * b1) * 0.5 * dt / abs(b1 - b0)
    else:  # 同号或端点触零：不变号
        absolute = abs(signed)
    return signed, absolute


def abs_subsegments(t0: float, b0: float, t1: float, b1: float) -> list[tuple[float, float, float, float]]:
    """把线性 bias 段在解析过零点拆分，返回 |bias| 线性的子段 [(s, |b_s|, e, |b_e|)]。

    端点严格异号时段内恰有一个零点 t* = t0 − b0·Δt/(b1−b0)，在 t* 处一分为二；
    否则 |bias| 全程线性，原段直接返回。每个子段上 |bias| 都是线性函数，
    其累积面积是时长的二次函数，可解析反解（见 time_to_accumulate）。
    """
    if b0 * b1 < 0:  # 严格异号：段内恰有一个零点
        t_cross = t0 - b0 * (t1 - t0) / (b1 - b0)
        return [(t0, abs(b0), t_cross, 0.0), (t_cross, 0.0, t1, abs(b1))]
    return [(t0, abs(b0), t1, abs(b1))]


def time_to_accumulate(abs_b0: float, abs_b1: float, duration: float, area: float) -> float:
    """反解 |bias| 线性段的累积函数：从段首累积 area（|bias|·秒）所需的时长。

    |bias| 从 abs_b0 线性变化到 abs_b1（斜率 k = (abs_b1 − abs_b0)/duration），
    累积面积 A(Δt) = abs_b0·Δt + k·Δt²/2 是 Δt 的二次函数，解析求根：
    k ≠ 0 时取数值稳定形式 Δt = 2·area / (abs_b0 + √(abs_b0² + 2k·area))，
    k = 0 时退化为线性 Δt = area / abs_b0。全程解析解，不做固定步长采样。
    调用前须保证 0 < area ≤ 段内总面积（段内 |bias| 不恒为零）；
    判别式与结果做浮点误差钳制，area 恰等于段内总面积时返回 duration。
    """
    k = (abs_b1 - abs_b0) / duration
    if k == 0.0:  # |bias| 恒定：累积随时间线性增长
        return area / abs_b0
    discriminant = max(0.0, abs_b0 * abs_b0 + 2.0 * k * area)
    offset = 2.0 * area / (abs_b0 + math.sqrt(discriminant))
    return min(offset, duration)


def _above_threshold(t0: float, v0: float, t1: float, v1: float, limit: float) -> tuple[float, float] | None:
    """线性函数 v 在 [t0, t1] 上严格大于 limit 的子区间端点 (s, e)；无则 None。

    恰等于 limit 不计入；超限集本身是开/半开区间，这里返回其闭包端点
    （端点瞬时时长为零，不影响持续秒数与合并判定）。
    """
    dt = t1 - t0
    if dt <= 0:
        return None
    above0 = v0 > limit
    above1 = v1 > limit
    if above0 and above1:
        return t0, t1
    if not above0 and not above1:
        return None
    # 恰一端超限：解析交点 t* = t0 + (limit − v0)·Δt/(v1 − v0)
    t_cross = t0 + (limit - v0) * dt / (v1 - v0)
    if above0:  # v0 > limit ≥ v1：段首到交点
        return t0, t_cross
    return t_cross, t1  # v1 > limit ≥ v0：交点到段尾


def exceedance_subranges(t0: float, b0: float, t1: float, b1: float, limit: float) -> list[tuple[float, float, str]]:
    """求线性段 (t0,b0)-(t1,b1) 内 |bias| > limit 的解析子区间。

    返回 [(s, e, direction), ...]，按时间排序，至多两段：高端（bias > limit，
    direction="high"）与低端（bias < −limit，direction="low"，通过对 bias 取负
    复用同一交点求解）。恰等于 limit 不计入。全程解析解，不做固定步长采样。
    """
    out = []
    high = _above_threshold(t0, b0, t1, b1, limit)
    if high is not None:
        out.append((high[0], high[1], "high"))
    low = _above_threshold(t0, -b0, t1, -b1, limit)
    if low is not None:
        out.append((low[0], low[1], "low"))
    out.sort(key=lambda r: r[0])  # 同一段内高低两段不重叠，按起点排即时间序
    return out


# ---------- 观测插入影响预演 ----------

def insertion_impact(
    t_prev: float, t_candidate: float, t_next: float, delta_candidate: float, limit: float
) -> tuple[float, float, list[tuple[float, float]]]:
    """解析比较插入候选观测前后的分段线性偏差曲线。

    插入只会把 (t_prev, t_next) 一段重新分段，曲线变化量 Δ 是折点为
    (t_prev, 0) → (t_candidate, delta_candidate) → (t_next, 0) 的分段线性函数，
    该范围之外 Δ 恒为 0。返回 (max_abs_change, max_change_at, exceedances)：

    - max_abs_change = |delta_candidate|：两段上 |Δ| 分别由 0 单调升至峰值再降回 0，
      最大值必在候选点取得；
    - max_change_at 为该最大值的最早发生时点：delta_candidate ≠ 0 时唯一（候选点
      本身）；delta_candidate = 0 时 Δ 恒为 0，取受影响范围起点 t_prev；
    - exceedances 为 |Δ| > limit 的连续区间 [(s, e)]（至多一段）：在两段上分别复用
      exceedance_subranges 解析求解与 limit 的交点，跨候选点仍超限的片段合并；
      恰等于 limit 的瞬时不计入。全程解析解，不做固定步长采样。
    """
    max_abs_change = abs(delta_candidate)
    max_change_at = t_candidate if delta_candidate != 0.0 else t_prev
    exceedances: list[list[float]] = []
    for s, e, _direction in exceedance_subranges(t_prev, 0.0, t_candidate, delta_candidate, limit):
        exceedances.append([s, e])
    for s, e, _direction in exceedance_subranges(t_candidate, delta_candidate, t_next, 0.0, limit):
        if exceedances and exceedances[-1][1] == s and max_abs_change > limit:
            exceedances[-1][1] = e  # 候选点两侧同属一段超限：跨候选点合并
        else:
            exceedances.append([s, e])
    return max_abs_change, max_change_at, [(s, e) for s, e in exceedances]


# ---------- 最长稳定窗口（max−min ≤ spread） ----------

def _clip_to_band(t0: float, b0: float, t1: float, b1: float, lo: float, hi: float) -> tuple[float, float] | None:
    """线性段 (t0,b0)-(t1,b1) 上 lo ≤ b ≤ hi 的子区间 (u, v)；完全在带外则 None。

    端点落在带内时 u/v 直接取精确的端点时刻；仅当边界严格穿过带宽时才计算解析
    交点 t* = t0 + (level − b0)·Δt/(b1 − b0)。这样相邻段在带内折点处的拼接是
    精确的（不受除法舍入误差影响），全程解析解，不做固定步长采样。
    """
    in0 = lo <= b0 <= hi
    in1 = lo <= b1 <= hi
    if in0 and in1:
        return t0, t1
    if b0 == b1:  # 恒定且在带外
        return None
    k = (b1 - b0) / (t1 - t0)
    t_lo = t0 + (lo - b0) / k   # b = lo 的解析交点
    t_hi = t0 + (hi - b0) / k   # b = hi 的解析交点
    enter, leave = (t_lo, t_hi) if k > 0 else (t_hi, t_lo)
    u = t0 if in0 else max(t0, enter)
    v = t1 if in1 else min(t1, leave)
    if u > v:
        return None
    return u, v


def _band_components(points: list[tuple[float, float]], lo: float, hi: float):
    """产出 {t : lo ≤ b(t) ≤ hi} 的各连通分量 (s, e)，按时间排序。

    相邻线性段在共享折点处同属带宽时合并为同一分量；仅在折点处相触也算相连
    （该瞬时时长为零，窗口经过该点不影响 max−min ≤ spread 的成立）。
    """
    cur_s = cur_e = None
    for (t0, b0), (t1, b1) in zip(points, points[1:]):
        clip = _clip_to_band(t0, b0, t1, b1, lo, hi)
        if clip is None:
            if cur_s is not None:
                yield cur_s, cur_e
                cur_s = cur_e = None
            continue
        u, v = clip
        if cur_e is not None and u == cur_e:
            cur_e = v                      # 与上一段在折点处相接：延伸当前分量
        else:
            if cur_s is not None:
                yield cur_s, cur_e
            cur_s, cur_e = u, v
    if cur_s is not None:
        yield cur_s, cur_e


def _value_at(points: list[tuple[float, float]], t: float) -> float:
    """分段线性函数在 t 处的取值（t 须在 points 覆盖范围内）。"""
    for (t0, b0), (t1, b1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            return interpolate(t, t0, b0, t1, b1)
    return points[-1][1]


def _is_better(duration: float, s: float, best_duration: float, best_s: float) -> bool:
    """候选窗口是否优于当前最优：时长更长；时长并列（含浮点噪声）时取起点更早者。"""
    tol = 1e-9 * max(1.0, abs(duration), abs(best_duration))
    if duration > best_duration + tol:
        return True
    return abs(duration - best_duration) <= tol and s < best_s


def stability_window(points: list[tuple[float, float]], spread: float) -> tuple[float, float, float, float]:
    """求分段线性 bias 上 max(bias) − min(bias) ≤ spread 的最长连续子区间。

    points 为按时间升序的折点 [(t, b)]（含裁剪后的查询窗口端点）。最优窗口必为
    某临界带宽 [m, m+spread] 的连通分量：带宽边缘未触及任何折点取值时，微调 m
    可让分量至少一端继续外伸、时长严格增大（或平移到更早的起点），故只需枚举
    m 取遍折点取值 b_i 与 b_i − spread。分量边界由线性段与 m / m+spread 的解析
    交点定位，不做固定步长采样。返回 (s, e, min_bias, max_bias)，其中
    min_bias/max_bias 为窗口内实际达到的极值；时长并列时取起点最早者。
    """
    values = [b for _, b in points]
    candidates = sorted({*values, *(v - spread for v in values)})
    best_duration = best_s = best_e = None
    for m in candidates:
        for s, e in _band_components(points, m, m + spread):
            duration = e - s
            if best_duration is None or _is_better(duration, s, best_duration, best_s):
                best_duration, best_s, best_e = duration, s, e
    s, e = best_s, best_e
    extrema = [_value_at(points, s), _value_at(points, e)]
    extrema += [b for t, b in points if s < t < e]
    return s, e, min(extrema), max(extrema)


# ---------- 校准排期规划（最少新增时点） ----------

def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """把禁排区间规范化为互不重叠、互不相接的升序区间列表。

    丢弃空区间（s >= e），按起点排序后合并一切重叠或端点相接（s 恰等于前一区间
    终点）的区间。相接区间合并后原相接点成为合并区间的内部——排期时只有合并后
    区间的端点允许排期。
    """
    spans = sorted((s, e) for s, e in intervals if s < e)
    merged: list[list[float]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _min_allowed_at_or_after(x: float, end: float, blackout: list[tuple[float, float]]) -> float | None:
    """返回 [x, end) 内最小允许排期的位置；不存在返回 None。

    blackout 为已规范化（merge_intervals）的禁排区间：禁排的是开区间 (s, e)，
    边界 s/e 允许排期。
    """
    p = x
    for s, e in blackout:
        if s >= end:
            break            # 区间已排序，之后的区间更不可能落在 end 内
        if e <= p:
            continue         # 该区间整体在 p 左侧
        if p <= s:
            return p         # p 在区间左边界上或其外：允许（边界允许排期）
        if p < e:
            p = e            # p 落在 (s, e) 内：跳到右边界（边界允许排期）
    return p if p < end else None


def _plan_gap_points(
    start: float, end: float, max_gap: float, blackout: list[tuple[float, float]]
) -> list[float] | None:
    """在 (start, end) 内生成数量最少、字典序最早的新增时点，使相邻间隔 ≤ max_gap。

    新增时点不得落在任何（已合并）禁排区间的开区间内部，边界允许；无解返回 None。

    两阶段构造：
    1. 自右向左推阈值 th[j] = 至多再用 j 个时点即可到达 end 的最小当前位置：
       th[0] = end − max_gap，th[j+1] = min(th[j], q − max_gap)，其中 q 为
       [th[j], end) 内最小可排期位置。阈值不再下降时若仍大于 start 则无解；
       首次 th[k] ≤ start 时的 k 即最少新增点数。
    2. 自左向右逐点取“仍能用剩余点数完成”的最早位置：第 i 个点取
       [th[k−i], 当前位置 + max_gap] 内最小可排期位置，即字典序最早方案。
    """
    if end - start <= max_gap:
        return []
    th = [end - max_gap]
    while th[-1] > start:
        q = _min_allowed_at_or_after(th[-1], end, blackout)
        if q is None:
            return None
        nxt = min(th[-1], q - max_gap)
        if nxt == th[-1]:
            return None    # 阈值不再下降：start 左侧无可继续推进的排期位置
        th.append(nxt)
    points = []
    cur = start
    for j in range(len(th) - 1, 0, -1):
        p = _min_allowed_at_or_after(th[j - 1], end, blackout)
        if p is None or p > cur + max_gap:
            return None    # 阈值阶段已保证可行，此处仅为防御
        points.append(p)
        cur = p
    return points


def plan_calibrations(
    anchors: list[float], max_gap: float, blackout: list[tuple[float, float]]
) -> list[float] | None:
    """在固定锚点之间插入数量最少的新增校准时点，使所有相邻时点间隔 ≤ max_gap。

    anchors 为升序固定锚点；blackout 为禁排区间（内部先经 merge_intervals 规范化
    合并），新增时点不得落在合并后区间的开区间内部（边界允许排期）。各相邻锚点间
    相互独立，分别求解后按时间拼接即全局最少且字典序最早的方案；任一段无解时
    整体返回 None。
    """
    merged = merge_intervals(blackout)
    suggested: list[float] = []
    for a, b in zip(anchors, anchors[1:]):
        points = _plan_gap_points(a, b, max_gap, merged)
        if points is None:
            return None
        suggested.extend(points)
    return suggested


# ---------- 校准曲线精简（保留点全局最少） ----------

def chord_value(t_k: float, t_i: float, b_i: float, t_j: float, b_j: float) -> float:
    """弦 (t_i, b_i)→(t_j, b_j) 在时点 t_k 处的线性插值。"""
    return b_i + (b_j - b_i) * (t_k - t_i) / (t_j - t_i)


def simplify_polyline(
    points: list[tuple[float, float]], max_error: float
) -> tuple[list[int], list[float]]:
    """从按时点升序的折点 points 中仅选择原折点构成子序列，首尾必须保留，
    使其弦折线（相邻保留点间线性插值）在完整覆盖范围内对原折线的逐折点绝对
    偏差均不超过 max_error，并使保留点数全局最少；保留点数并列时选择时点序列
    字典序最早者（时点严格递增，等价于索引序列字典序最早）。

    可行性核对：弦 i→j 是否可用，要逐一核对它跨越的每个原观测折点
    k（i < k < j）：|b_k − chord_i→j(t_k)| ≤ max_error——不做固定步长采样，
    也不只查弦端点（端点偏差恒为 0）。在“可行弦”构成的 DAG 上做边权 1 的
    最短路即得全局最少保留点；同长度路径并列时保留字典序最早的索引元组。

    返回 (保留点在 points 中的索引列表, 每个原折点相对精简曲线的绝对偏差列表)。
    原观测点为 0/1 个时原样返回（调用方负责在不足两条时拒绝请求）。
    """
    n = len(points)
    if n <= 1:
        return list(range(n)), [0.0] * n
    # feasible[i][j]：弦 i→j 跨越的每个原折点逐一核对后偏差是否都不超过 max_error
    feasible = [bytearray(n) for _ in range(n)]
    for i in range(n - 1):
        t_i, b_i = points[i]
        for j in range(i + 1, n):
            t_j, b_j = points[j]
            dt = t_j - t_i
            ok = True
            for k in range(i + 1, j):  # 逐一核对跨越的每个原折点，绝不只查端点
                t_k, b_k = points[k]
                chord = b_i + (b_j - b_i) * (t_k - t_i) / dt
                if abs(b_k - chord) > max_error:
                    ok = False
                    break
            if ok:
                feasible[i][j] = 1
    # DAG 最短路：dp[j] = 到达 j 的最少点数；并列保留字典序最早的索引序列
    inf = n + 1
    dp = [inf] * n
    best: list[tuple[int, ...] | None] = [None] * n
    dp[0] = 1
    best[0] = (0,)
    for j in range(1, n):
        for i in range(j):
            if not feasible[i][j]:
                continue
            cand_len = dp[i] + 1
            if cand_len > dp[j]:
                continue
            cand = best[i] + (j,)  # 邻边恒可行，best[i] 必已到达
            if cand_len < dp[j] or cand < best[j]:
                dp[j] = cand_len
                best[j] = cand
    kept = list(best[n - 1])
    # 精简曲线在每个原折点处的绝对偏差：逐保留段对跨越折点重算（保留点处为 0）
    deviations = [0.0] * n
    for a, b in zip(kept, kept[1:]):
        t_a, b_a = points[a]
        t_b, b_b = points[b]
        for k in range(a + 1, b):
            t_k, b_k = points[k]
            deviations[k] = abs(b_k - chord_value(t_k, t_a, b_a, t_b, b_b))
    return kept, deviations
