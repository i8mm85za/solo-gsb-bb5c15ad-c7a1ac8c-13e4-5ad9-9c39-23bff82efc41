"""校准曲线精简纯算法单测：保留点全局最少、首尾必保留、字典序并列、逐折点误差核对。"""
from __future__ import annotations

import itertools
import random

import pytest

from app.calibration import simplify_polyline


def brute_force(points, max_error):
    """穷举全部含首尾的子序列：返回最少点数且字典序最早者，作为动态规划的对照。"""
    n = len(points)

    def feasible(kept):
        for a, b in zip(kept, kept[1:]):
            t_a, b_a = points[a]
            t_b, b_b = points[b]
            for k in range(a + 1, b):
                t_k, b_k = points[k]
                chord = b_a + (b_b - b_a) * (t_k - t_a) / (t_b - t_a)
                if abs(b_k - chord) > max_error:
                    return False
        return True

    for size in range(2, n + 1):
        for mids in itertools.combinations(range(1, n - 1), size - 2):
            kept = (0,) + mids + (n - 1,)
            if feasible(kept):
                return list(kept)


def deviations_of(points, kept):
    """逐条弦对跨越的每个原折点重算绝对偏差（保留点处为 0）。"""
    deviations = [0.0] * len(points)
    for a, b in zip(kept, kept[1:]):
        t_a, b_a = points[a]
        t_b, b_b = points[b]
        for k in range(a + 1, b):
            t_k, b_k = points[k]
            chord = b_a + (b_b - b_a) * (t_k - t_a) / (t_b - t_a)
            deviations[k] = abs(b_k - chord)
    return deviations


def assert_valid(points, kept, deviations, max_error):
    # 首尾必保留，且只从原观测中选择（严格递增子序列）
    assert kept[0] == 0
    assert kept[-1] == len(points) - 1
    assert kept == sorted(set(kept))
    # 逐折点核对偏差，且不超过 max_error
    assert deviations == pytest.approx(deviations_of(points, kept))
    assert all(d <= max_error + 1e-12 for d in deviations)


def test_two_points_are_both_kept():
    points = [(0.0, 1.0), (10.0, 2.0)]
    kept, deviations = simplify_polyline(points, 0.0)
    assert kept == [0, 1]
    assert deviations == [0.0, 0.0]


def test_zero_or_one_point():
    assert simplify_polyline([], 0.0) == ([], [])
    assert simplify_polyline([(3.0, 9.0)], 0.0) == ([0], [0.0])


def test_collinear_interior_points_are_dropped():
    # 中间点全在弦上：无论 max_error 多小都能全丢
    points = [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]
    kept, deviations = simplify_polyline(points, 0.0)
    assert kept == [0, 3]
    assert deviations == pytest.approx([0.0, 0.0, 0.0, 0.0])


def test_zero_tolerance_keeps_every_vertex():
    points = [(0.0, 0.0), (1.0, 0.0), (2.0, 10.0), (3.0, 0.0), (4.0, 0.0)]
    kept, deviations = simplify_polyline(points, 0.0)
    assert kept == [0, 1, 2, 3, 4]
    assert deviations == [0.0] * 5


def test_interior_spike_forces_three_points():
    # 折点 2 偏离任何跨过它的弦 5 个单位，max_error 1 不允许跨过：全部保留
    points = [(0.0, 0.0), (1.0, 0.0), (2.0, 10.0), (3.0, 0.0), (4.0, 0.0)]
    kept, deviations = simplify_polyline(points, 1.0)
    assert kept == [0, 1, 2, 3, 4]
    assert deviations == [0.0] * 5


def test_interior_spike_within_tolerance_can_span():
    # 放宽到 5 后，弦 0→2（折点 1 处偏差 5）与弦 2→4（折点 3 处偏差 5）都可行：
    # 最少 3 点；注意单弦 0→4 在折点 2 处偏差 10 仍不可行
    points = [(0.0, 0.0), (1.0, 0.0), (2.0, 10.0), (3.0, 0.0), (4.0, 0.0)]
    kept, deviations = simplify_polyline(points, 5.0)
    assert kept == [0, 2, 4]
    assert deviations == pytest.approx([0.0, 5.0, 0.0, 5.0, 0.0])


def test_interior_spike_boundary_equal_is_allowed():
    # 容差 10：单弦 0→4 在折点 2 处恰为 10，“不超过”允许，2 点即首尾
    points = [(0.0, 0.0), (1.0, 0.0), (2.0, 10.0), (3.0, 0.0), (4.0, 0.0)]
    kept, deviations = simplify_polyline(points, 10.0)
    assert kept == [0, 4]
    assert deviations[2] == pytest.approx(10.0)


def test_endpoints_are_never_dropped_even_when_midpoint_dominates():
    points = [(0.0, 0.0), (1.0, 100.0), (2.0, 0.0), (3.0, 100.0), (4.0, 0.0)]
    kept, _ = simplify_polyline(points, 50.0)
    assert kept[0] == 0 and kept[-1] == 4


def test_deviation_boundary_equal_is_allowed():
    # 折点偏离弦恰等于 max_error：允许（“不超过”而非“严格小于”）
    points = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]
    kept, deviations = simplify_polyline(points, 1.0)
    assert kept == [0, 2]
    assert deviations[1] == pytest.approx(1.0)


def test_uneven_timestamps_use_real_time_ratio_interpolation():
    # 非均匀时点（间隔 1,1,7）：单弦 0→3 是零偏差线，折点 1/2 的真实偏差都是 1.0；
    # 保留折点 1 后，弦 1→3 在 t=2（弦上 1/8 处）的偏差是 1 − 7/8 = 0.125。
    # 误差必须按真实时点比例线性插值逐一核对——若误把折点当成等权 1/3 间隔，
    # 弦 1→3 在折点 2 处会被错算成 2/3，可行性与全局点数都会错。
    points = [(0.0, 0.0), (1.0, 1.0), (2.0, 1.0), (9.0, 0.0)]
    kept, deviations = simplify_polyline(points, 1.0)  # 边界恰等：允许 → 首尾 2 点
    assert kept == [0, 3]
    assert deviations == pytest.approx([0.0, 1.0, 1.0, 0.0])
    kept2, deviations2 = simplify_polyline(points, 0.9)  # 折点 1 偏差 1.0 超限
    assert kept2 == [0, 1, 3]  # 字典序最早的 3 点方案（弦 1→3 真实偏差仅 0.125）
    assert deviations2[2] == pytest.approx(0.125)
    assert_valid(points, kept2, deviations2, 0.9)
    kept3, deviations3 = simplify_polyline(points, 0.1)  # 0.125 也超限 → 全保留
    assert kept3 == [0, 1, 2, 3]
    assert_valid(points, kept3, deviations3, 0.1)


def test_lexicographically_earliest_among_equal_size():
    # 长度 2 的唯一弦 0→4 不可行（折点 1 偏离 0.9 > 0.8）；长度 3 中
    # (0,1,4) 与 (0,2,4) 均可行 → 取第二保留点索引更早的 (0,1,4)
    points = [(0.0, 0.0), (1.0, 0.9), (2.0, 0.2), (3.0, -0.5), (4.0, 0.0)]
    kept, deviations = simplify_polyline(points, 0.8)
    assert kept == [0, 1, 4]
    assert_valid(points, kept, deviations, 0.8)


def test_non_monotonic_feasibility_example():
    # 可行性随右端点推进并非单调：0→2 不可行（偏离 1.0），但 0→3 可行（偏离 0.5），
    # 贪心“能跨多远跨多远”会漏掉 0→3 这条弦；DP 仍应得到全局最少的 2 点方案
    points = [(0.0, 0.0), (1.0, 1.0), (2.0, 1.5), (3.0, 3.0)]
    kept, deviations = simplify_polyline(points, 0.5)
    assert kept == [0, 3]
    assert deviations[1] == pytest.approx(0.0)
    assert deviations[2] == pytest.approx(0.5)
    assert_valid(points, kept, deviations, 0.5)


@pytest.mark.parametrize("seed", range(40))
def test_matches_brute_force_randomized(seed):
    rng = random.Random(seed)
    n = rng.randint(2, 11)
    t = 0.0
    points = []
    for i in range(n):
        points.append((t, rng.uniform(-8.0, 8.0)))
        t += rng.uniform(0.5, 4.0)
    max_error = rng.choice([0.0, 0.1, 0.5, 1.0, 2.0, rng.uniform(0.0, 3.0)])
    kept, deviations = simplify_polyline(points, max_error)
    assert kept == brute_force(points, max_error)
    assert_valid(points, kept, deviations, max_error)
