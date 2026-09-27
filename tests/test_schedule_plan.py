"""校准排期规划纯算法单测：禁排区间规范化合并与最少新增时点求解。"""
from __future__ import annotations

import pytest

from app.calibration import merge_intervals, plan_calibrations


# ---------- 禁排区间规范化合并 ----------

def test_merge_intervals_empty():
    assert merge_intervals([]) == []


def test_merge_intervals_drops_empty_spans():
    # 空区间（含零长度）不禁止任何时点，规范化时丢弃
    assert merge_intervals([(5.0, 5.0), (7.0, 6.0)]) == []


def test_merge_intervals_sorts_and_merges_overlaps():
    assert merge_intervals([(10.0, 20.0), (0.0, 5.0), (3.0, 12.0)]) == [(0.0, 20.0)]


def test_merge_intervals_merges_touching_spans():
    # 端点相接的区间合并为一个；相接点成为合并区间的内部
    assert merge_intervals([(0.0, 5.0), (5.0, 10.0)]) == [(0.0, 10.0)]


def test_merge_intervals_merges_nested_spans():
    assert merge_intervals([(0.0, 10.0), (2.0, 3.0)]) == [(0.0, 10.0)]


def test_merge_intervals_keeps_disjoint_spans():
    assert merge_intervals([(2.0, 3.0), (0.0, 1.0)]) == [(0.0, 1.0), (2.0, 3.0)]


# ---------- 最少新增时点求解 ----------

def test_plan_no_points_needed_when_gaps_within_max():
    assert plan_calibrations([0.0, 3000.0, 6000.0], 3000.0, []) == []


def test_plan_single_anchor_pair_needs_no_points():
    assert plan_calibrations([0.0, 10.0], 10.0, []) == []


def test_plan_fewer_than_two_anchors():
    assert plan_calibrations([], 5.0, []) == []
    assert plan_calibrations([7.0], 5.0, []) == []


def test_plan_single_point_exact_split():
    # 间隔 10、max_gap 5：一个时点，唯一可行位置 5
    assert plan_calibrations([0.0, 10.0], 5.0, []) == [5.0]


def test_plan_lexicographically_earliest_among_ties():
    # 间隔 14、max_gap 5：最少 2 点；字典序最早为 (4, 9) 而非 (5, 10) 等
    assert plan_calibrations([0.0, 14.0], 5.0, []) == [4.0, 9.0]


def test_plan_lexicographically_earliest_three_points():
    # 间隔 20、max_gap 6：最少 3 点；最早方案 (2, 8, 14)，各段 2/6/6/6
    assert plan_calibrations([0.0, 20.0], 6.0, []) == [2.0, 8.0, 14.0]


def test_plan_blackout_boundary_is_schedulable():
    # 禁排 (5, 7) 的开区间内部；边界 5 允许排期，恰为唯一可行位置
    assert plan_calibrations([0.0, 10.0], 5.0, [(5.0, 7.0)]) == [5.0]


def test_plan_blackout_can_force_extra_points():
    # 无禁排时 1 点（位置 5）即可；禁排 (4, 6) 后 1 点无解，最少 2 点且最早为 (1, 6)
    assert plan_calibrations([0.0, 10.0], 5.0, [(4.0, 6.0)]) == [1.0, 6.0]


def test_plan_points_at_both_blackout_edges():
    # 禁排 (5, 15)：两个边界都允许排期，(5, 15) 即最早最少方案
    assert plan_calibrations([0.0, 20.0], 10.0, [(5.0, 15.0)]) == [5.0, 15.0]


def test_plan_touching_blackouts_merge_and_block_interior():
    # [0,5] 与 [5,10] 先合并为 [0,10]：相接点 5 成为内部禁止排期 → 无解
    assert plan_calibrations([0.0, 10.0], 5.0, [(0.0, 5.0), (5.0, 10.0)]) is None


def test_plan_full_blackout_infeasible():
    assert plan_calibrations([0.0, 10.0], 5.0, [(0.0, 10.0)]) is None


def test_plan_blackout_wider_than_max_gap_is_uncrossable():
    # 禁排开区间 (10, 20) 宽 10 > max_gap 7：边界 10 到边界 20 相距 10，无法跨越 → 无解
    assert plan_calibrations([0.0, 50.0], 7.0, [(10.0, 20.0)]) is None
    # 宽度恰等于 max_gap 时边界到边界可跨越
    assert plan_calibrations([0.0, 50.0], 10.0, [(10.0, 20.0)]) is not None


def test_plan_allowed_island_too_small_infeasible():
    # 允许位置仅剩 [0,2] 与 [8,10]：从 0 出发最多走到 2，永远跨不到 8 → 无解
    assert plan_calibrations([0.0, 10.0], 5.0, [(2.0, 8.0)]) is None


def test_plan_blackout_outside_gap_is_irrelevant():
    assert plan_calibrations([0.0, 10.0], 5.0, [(100.0, 200.0)]) == [5.0]


def test_plan_blackout_starting_at_anchor():
    # 禁排 (0, 5)：锚点 0 不受影响，边界 5 允许排期
    assert plan_calibrations([0.0, 10.0], 5.0, [(0.0, 5.0)]) == [5.0]


def test_plan_multiple_gaps_compose():
    # 各相邻锚点间独立求解后按时间拼接
    assert plan_calibrations([0.0, 10.0, 25.0], 5.0, []) == [5.0, 15.0, 20.0]


def test_plan_infeasible_gap_fails_whole_plan():
    # 第二段被禁排完全封死：整体无解
    assert plan_calibrations([0.0, 10.0, 20.0], 5.0, [(10.0, 20.0)]) is None


def test_plan_result_respects_max_gap_and_blackout():
    # 性质校验：相邻间隔均 ≤ max_gap，且建议时点均不在禁排开区间内
    anchors = [0.0, 50.0]
    max_gap = 12.0
    blackout = [(10.0, 20.0), (25.0, 26.0), (40.0, 45.0)]
    merged = merge_intervals(blackout)
    points = plan_calibrations(anchors, max_gap, merged)
    assert points == [8.0, 20.0, 26.0, 38.0]
    chain = [anchors[0], *points, anchors[-1]]
    for a, b in zip(chain, chain[1:]):
        assert b - a <= max_gap + 1e-9
    for p in points:
        assert anchors[0] < p < anchors[-1]
        assert all(not (s < p < e) for s, e in merged)
    # 最少性：少一个点时必存在超限间隔（逐一验证删去任一点后不可行）
    for dropped in points:
        rest = [p for p in points if p != dropped]
        chain = [anchors[0], *rest, anchors[-1]]
        assert any(b - a > max_gap + 1e-9 for a, b in zip(chain, chain[1:]))


def test_plan_minimality_holds_with_blackout():
    # 禁排 (4, 6) 时 1 点无解（唯一候选 5 被禁），故返回的 2 点确为最少
    merged = merge_intervals([(4.0, 6.0)])
    points = plan_calibrations([0.0, 10.0], 5.0, merged)
    assert points == [1.0, 6.0]
    assert len(points) == 2


@pytest.mark.parametrize("gap,max_gap,expected_count", [
    (10.0, 5.0, 1),
    (10.0, 3.0, 3),    # ceil(10/3) − 1 = 3
    (11.0, 5.0, 2),
    (100.0, 7.0, 14),  # ceil(100/7) − 1 = 14
])
def test_plan_point_count_is_minimal_without_blackout(gap, max_gap, expected_count):
    points = plan_calibrations([0.0, gap], max_gap, [])
    assert points is not None
    assert len(points) == expected_count
    chain = [0.0, *points, gap]
    assert all(b - a <= max_gap + 1e-9 for a, b in zip(chain, chain[1:]))
