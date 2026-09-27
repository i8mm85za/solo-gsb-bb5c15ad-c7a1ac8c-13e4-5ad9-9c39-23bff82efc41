"""Pydantic 请求/响应模型。时间戳对外同时提供 Unix 秒与 ISO 8601 两种形式。"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, field_validator


def _must_be_finite(value: float) -> float:
    """数值字段必须有限：NaN/±Infinity 一律拒绝（422）。

    待校正读数为 NaN/±Infinity 时无法参与漂移补偿（与偏差相加只会得到
    NaN/Infinity，响应与回执都无法表示），在入口模型层直接拒绝。
    """
    if not math.isfinite(value):
        raise ValueError("must be a finite number (NaN and Infinity are not allowed)")
    return value


# 待校正读数：先经 float 强转，再拒绝 NaN/±Infinity
FiniteFloat = Annotated[float, AfterValidator(_must_be_finite)]


def epoch_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _ensure_utc_date(ts: float) -> None:
    """时点必须能转换为 UTC 日期：拒绝 NaN/±Infinity 及超出平台时间范围的值。"""
    if not math.isfinite(ts):
        raise ValueError("timestamp must be a finite number")
    try:
        datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        # 极大（如 1e300）的有限时间戳虽为数字，但无法转换为任何 UTC 日期
        raise ValueError("timestamp cannot be converted to a UTC date") from exc


def parse_timestamp(value: Any) -> float:
    """接受 Unix 秒（数字或数字字符串）或 ISO 8601 字符串，统一为 Unix 秒。"""
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    if isinstance(value, (int, float)):
        ts = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                ts = float(text)
            except ValueError:
                raise ValueError(f"unparseable timestamp: {value!r}") from None
        else:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
    else:
        raise ValueError(f"unsupported timestamp type: {type(value).__name__}")
    _ensure_utc_date(ts)
    return ts


Timestamp = Annotated[float, BeforeValidator(parse_timestamp)]


# ---------- 请求 ----------

class ObservationIn(BaseModel):
    reference: float = Field(description="参考值（真值），必须为有限数值")
    tolerance: float = Field(ge=0, description="容差：读数偏离组中位数超过该值即判为异常；必须为有限非负数")
    readings: list[float] = Field(min_length=3, description="同一时点的读数，至少 3 次，均须为有限数值")
    timestamp: Timestamp = Field(
        default_factory=lambda: datetime.now(timezone.utc).timestamp(),
        description="观测时点：Unix 秒或 ISO 8601，缺省为当前时间；须能转换为 UTC 日期",
    )

    @field_validator("reference", "tolerance", mode="after")
    @classmethod
    def _scalar_must_be_finite(cls, value: float) -> float:
        # 标量在 Pydantic 数值强转之后再校验：NaN 通过 ge=0 比较（结果恒为 False
        # 之外的情形），±Infinity 则能越过 ge=0，必须在此显式拒绝。
        if not math.isfinite(value):
            raise ValueError("must be a finite number (NaN and Infinity are not allowed)")
        return value

    @field_validator("readings", mode="after")
    @classmethod
    def _readings_must_be_finite(cls, values: list[float]) -> list[float]:
        for value in values:
            if not math.isfinite(value):
                raise ValueError("readings must all be finite numbers (NaN and Infinity are not allowed)")
        return values


class CorrectionIn(BaseModel):
    timestamp: Timestamp = Field(description="待校正读数的时点：Unix 秒或 ISO 8601")
    reading: FiniteFloat = Field(description="待校正的原始读数（必须为有限数值）")


class ObservationReviseIn(ObservationIn):
    revision: int = Field(ge=1, description="客户端持有的当前版本号；与服务端不一致时拒绝修订（409）")


class ObservationImpactPreviewIn(ObservationIn):
    impact_limit: float = Field(description="影响阈值（正数）：|偏差变化| 严格大于该值才计入影响区间")


class BlackoutIntervalIn(BaseModel):
    from_: Timestamp = Field(alias="from", description="禁排时段起点：Unix 秒或 ISO 8601")
    to: Timestamp = Field(description="禁排时段终点：Unix 秒或 ISO 8601")


class CalibrationSchedulePlanIn(BaseModel):
    from_: Timestamp = Field(alias="from", description="规划窗口下界：Unix 秒或 ISO 8601")
    to: Timestamp = Field(description="规划窗口上界：Unix 秒或 ISO 8601")
    max_gap: float = Field(description="相邻锚点允许的最大间隔（正数，秒）")
    blackout: list[BlackoutIntervalIn] = Field(
        default_factory=list,
        description="禁排时段（可选）：先规范化合并，其开区间内部禁止排期，边界允许",
    )


class CalibrationCurveSimplifyIn(BaseModel):
    max_error: float = Field(
        description="允许的最大绝对偏差（有限且非负）：精简曲线逐原折点的偏差均不得超过该值"
    )

    @field_validator("max_error", mode="before")
    @classmethod
    def _validate_non_negative_finite(cls, value: Any) -> Any:
        # 只接受 JSON 数值（int/float）；布尔、字符串等冒充类型拒绝（422）。
        # NaN/±Infinity 也要 422，但它们经 float() 后无法被标准 JSON 响应回显，
        # 故在 float 转换之前先按原始文本拒绝，避免默认 422 处理器序列化崩溃。
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("max_error must be a number")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("max_error must be a finite non-negative number")
        return value  # max_error = 0 合法（通常要求保留全部折点）


# ---------- 响应 ----------

class ReadingOut(BaseModel):
    value: float
    is_outlier: bool


class ObservationOut(BaseModel):
    id: int
    revision: int
    timestamp: float
    timestamp_iso: str
    reference: float
    tolerance: float
    group_median: float
    bias: float
    readings: list[ReadingOut]
    outliers: list[float]


class RevisionOut(BaseModel):
    revision: int = Field(description="该快照对应的版本（修订前版本）")
    changed_at: float
    changed_at_iso: str
    old_timestamp: float
    old_timestamp_iso: str
    old_reference: float
    old_tolerance: float
    old_bias: float
    old_readings: list[float] = Field(description="修订前原始读数（保持提交顺序）")


class IntervalOut(BaseModel):
    t_start: float
    t_end: float
    start_iso: str
    end_iso: str
    bias_start: float
    bias_end: float


class EndpointOut(BaseModel):
    observation_id: int
    timestamp: float
    timestamp_iso: str
    bias: float
    outliers: list[float]


class CorrectionOut(BaseModel):
    timestamp: float
    timestamp_iso: str
    reading: float
    bias: float
    corrected: float
    interval: IntervalOut | None = Field(description="所用插值区间；恰落在观测点上时为 null")
    endpoints: list[EndpointOut] = Field(description="区间端点观测及其异常读数明细")


class HistoricalEndpointOut(BaseModel):
    observation_id: int
    revision: int = Field(description="as_of 系统时点该观测所处的版本号")
    timestamp: float = Field(description="as_of 状态下重建出的观测时点（Unix 秒）")
    timestamp_iso: str
    bias: float = Field(description="as_of 状态下该观测的校准偏差")


class HistoricalCorrectionOut(BaseModel):
    effective_at: float = Field(description="目标观测时点（Unix 秒）")
    effective_at_iso: str
    as_of: float = Field(description="系统时点（Unix 秒）：校准状态按该时刻重建")
    as_of_iso: str
    reading: float
    bias: float
    corrected: float
    interval: IntervalOut | None = Field(description="重建状态下所用的插值区间；恰落在观测点上时为 null")
    endpoints: list[HistoricalEndpointOut] = Field(description="所用端点的观测 ID 与 as_of 时点版本")


class DriftContributionOut(BaseModel):
    t_start: float = Field(description="裁剪后子段起点（Unix 秒）")
    t_end: float = Field(description="裁剪后子段终点（Unix 秒）")
    start_iso: str
    end_iso: str
    bias_start: float = Field(description="子段起点处的插值偏差")
    bias_end: float = Field(description="子段终点处的插值偏差")
    signed_bias_seconds: float = Field(description="子段有符号面积（偏差·秒）")
    absolute_bias_seconds: float = Field(description="子段绝对值面积（偏差·秒），段内过零按解析交点拆分")


class DriftIntegralOut(BaseModel):
    from_: float = Field(alias="from", description="积分窗口下界（Unix 秒）")
    from_iso: str
    to: float = Field(description="积分窗口上界（Unix 秒）")
    to_iso: str
    signed_bias_seconds: float = Field(description="窗口内偏差的有符号积分（偏差·秒）")
    absolute_bias_seconds: float = Field(description="窗口内偏差绝对值的积分（偏差·秒）")
    contributions: list[DriftContributionOut] = Field(
        description="按插值区间裁剪后的分段贡献，按时间排序；各分段面积之和等于汇总值"
    )


class ExceedanceOut(BaseModel):
    t_start: float = Field(description="超限区间起点（Unix 秒）：解析交点或窗口边界")
    t_end: float = Field(description="超限区间终点（Unix 秒）")
    start_iso: str
    end_iso: str
    direction: str = Field(description="超限方向：high = bias > limit；low = bias < −limit")
    duration_seconds: float = Field(description="超限持续秒数（t_end − t_start）")
    peak_abs_deviation: float = Field(description="区间内 |bias| 的峰值")


class DriftExceedancesOut(BaseModel):
    from_: float = Field(alias="from", description="分析窗口下界（Unix 秒）")
    from_iso: str
    to: float = Field(description="分析窗口上界（Unix 秒）")
    to_iso: str
    limit: float = Field(description="超限阈值（正数）：|bias| 严格大于该值才计为超限")
    exceedances: list[ExceedanceOut] = Field(
        description="|bias| > limit 的连续区间，按时间排序；跨观测点仍连续且同向的片段已合并"
    )


class BudgetContributionOut(BaseModel):
    t_start: float = Field(description="裁剪后子段起点（Unix 秒）")
    t_end: float = Field(description="裁剪后子段终点（Unix 秒）：耗尽所在段截止于 depleted_at")
    start_iso: str
    end_iso: str
    bias_start: float = Field(description="子段起点处的（有符号）插值偏差")
    bias_end: float = Field(description="子段终点处的（有符号）插值偏差")
    absolute_bias_seconds: float = Field(description="子段消耗的预算（|bias|·秒）")


class DriftBudgetDepletionOut(BaseModel):
    from_: float = Field(alias="from", description="累积起点（Unix 秒）")
    from_iso: str
    budget: float = Field(description="漂移预算（正数）：可承受的 |bias|·秒 总量")
    depleted_at: float | None = Field(
        description="预算首次耗尽的精确时点（Unix 秒）；覆盖范围内未耗尽为 null"
    )
    depleted_at_iso: str | None
    consumed: float = Field(description="截至耗尽点（未耗尽时为覆盖终点）已消耗的预算（|bias|·秒）")
    remaining: float = Field(description="截至耗尽点（未耗尽时为覆盖终点）的剩余预算")
    coverage_end: float = Field(description="覆盖范围上界（Unix 秒）：累积推进的终点")
    coverage_end_iso: str
    contributions: list[BudgetContributionOut] = Field(
        description="按插值区间裁剪到 [from, 耗尽点或覆盖终点] 的分段消耗，按时间排序；"
        "各段 absolute_bias_seconds 之和等于 consumed"
    )


class DriftStabilityWindowOut(BaseModel):
    from_: float = Field(alias="from", description="分析窗口下界（Unix 秒）")
    from_iso: str
    to: float = Field(description="分析窗口上界（Unix 秒）")
    to_iso: str
    max_spread: float = Field(description="允许的最大波动幅度（正数）：窗口内 max(bias) − min(bias) ≤ max_spread")
    t_start: float = Field(description="最长稳定子区间起点（Unix 秒）：解析交点或窗口边界")
    t_end: float = Field(description="最长稳定子区间终点（Unix 秒）：解析交点或窗口边界")
    start_iso: str
    end_iso: str
    duration_seconds: float = Field(description="稳定持续秒数（t_end − t_start）")
    min_bias: float = Field(description="子区间内 bias 实际达到的最小值")
    max_bias: float = Field(description="子区间内 bias 实际达到的最大值")


class ImpactIntervalOut(BaseModel):
    t_start: float = Field(description="影响区间起点（Unix 秒）：|偏差变化| 与 impact_limit 的解析交点")
    t_end: float = Field(description="影响区间终点（Unix 秒）")
    start_iso: str
    end_iso: str
    duration_seconds: float = Field(description="超限持续秒数（t_end − t_start）")


class ObservationImpactPreviewOut(BaseModel):
    timestamp: float = Field(description="候选观测时点（Unix 秒）")
    timestamp_iso: str
    impact_limit: float = Field(description="影响阈值（正数）：|偏差变化| 严格大于该值才计入影响区间")
    candidate_bias: float = Field(description="按现有异常判定算出的候选观测偏差（不落库）")
    current_bias: float = Field(description="当前分段线性曲线在候选时点的插值偏差")
    affected_from: float = Field(description="受影响时间范围下界（Unix 秒）：候选点左侧最近观测时点")
    affected_from_iso: str
    affected_to: float = Field(description="受影响时间范围上界（Unix 秒）：候选点右侧最近观测时点")
    affected_to_iso: str
    max_abs_change: float = Field(description="受影响范围内 |新曲线 − 当前曲线| 的最大值")
    max_change_at: float = Field(description="最大绝对偏差变化的最早发生时点（Unix 秒）")
    max_change_at_iso: str
    impact_intervals: list[ImpactIntervalOut] = Field(
        description="|偏差变化| > impact_limit 的连续区间，按时间排序；阈值交点解析求解"
    )


class BlackoutIntervalOut(BaseModel):
    from_: float = Field(alias="from", description="规范化合并后的禁排时段起点（Unix 秒）")
    from_iso: str
    to: float = Field(description="规范化合并后的禁排时段终点（Unix 秒）")
    to_iso: str


class ScheduleAnchorOut(BaseModel):
    observation_id: int = Field(description="锚点对应的观测 ID")
    timestamp: float = Field(description="锚点时点（Unix 秒）")
    timestamp_iso: str


class SuggestedPointOut(BaseModel):
    timestamp: float = Field(description="建议新增校准时点（Unix 秒）")
    timestamp_iso: str


class ScheduleSegmentOut(BaseModel):
    t_start: float = Field(description="分段起点（Unix 秒）")
    t_end: float = Field(description="分段终点（Unix 秒）")
    start_iso: str
    end_iso: str
    duration_seconds: float = Field(description="分段间隔秒数（t_end − t_start），不超过 max_gap")
    start_kind: str = Field(description="起点类型：fixed = 固定锚点；suggested = 建议时点")
    end_kind: str = Field(description="终点类型：fixed = 固定锚点；suggested = 建议时点")


class CalibrationSchedulePlanOut(BaseModel):
    from_: float = Field(alias="from", description="规划窗口下界（Unix 秒）")
    from_iso: str
    to: float = Field(description="规划窗口上界（Unix 秒）")
    to_iso: str
    max_gap: float = Field(description="相邻锚点允许的最大间隔（正数，秒）")
    blackout: list[BlackoutIntervalOut] = Field(
        description="规范化合并后的禁排时段：其开区间内部禁止排期，边界允许"
    )
    fixed_anchors: list[ScheduleAnchorOut] = Field(
        description="固定锚点：窗口两侧能覆盖边界的现有观测及窗口内观测，按时间排序"
    )
    suggested: list[SuggestedPointOut] = Field(
        description="建议新增校准时点：数量最少，多个最少方案并列时取字典序最早的一组"
    )
    segments: list[ScheduleSegmentOut] = Field(
        description="固定锚点与建议时点合并排序后的逐段间隔，按时间排序"
    )


class SimplifiedPointOut(BaseModel):
    observation_id: int = Field(description="保留观测的 ID（原样引用，不新建/不改写）")
    timestamp: float = Field(description="保留观测时点（Unix 秒）")
    timestamp_iso: str
    bias: float = Field(description="该保留点的校准偏差（精简曲线经过原观测点，该点偏差恒为 0）")


class CalibrationCurveSimplifyOut(BaseModel):
    max_error: float = Field(description="请求允许的最大绝对偏差（有限非负数）")
    coverage_from: float = Field(description="精简曲线完整覆盖范围下界 = 最早观测时点（Unix 秒）")
    coverage_from_iso: str
    coverage_to: float = Field(description="精简曲线完整覆盖范围上界 = 最晚观测时点（Unix 秒）")
    coverage_to_iso: str
    original_count: int = Field(description="原观测条数（按时点排序）")
    retained_count: int = Field(description="保留点数：全局最少（首尾观测必保留）")
    retained: list[SimplifiedPointOut] = Field(
        description="保留点子序列（原观测的子集），按时点排序；同点数方案并列时取时点序列字典序最早者"
    )
    max_abs_error: float = Field(
        description="精简曲线与原曲线的全局实际最大绝对偏差（逐一核对全部原观测折点得到，不采样）"
    )
    max_error_at: float = Field(description="全局实际最大误差的最早发生时点（Unix 秒，必为某原观测折点）")
    max_error_at_iso: str
