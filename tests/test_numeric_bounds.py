"""数值边界测试：NaN/±Infinity 拒绝、时点 UTC 可转换性、偏差溢出与事务原子性。

覆盖 POST /observations、PUT /observations/{id} 与
POST /observation-impact-preview 三个共用观测输入的入口。
"""
from __future__ import annotations

import json
import math

import pytest
from fastapi.testclient import TestClient

NON_FINITE = (float("nan"), float("inf"), float("-inf"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as c:
        yield c


def make_observation(client, ts=1000.0, reference=10.0, tolerance=0.5,
                     readings=(9.0, 9.1, 8.9)):
    resp = client.post(
        "/observations",
        json={"timestamp": ts, "reference": reference, "tolerance": tolerance,
              "readings": list(readings)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def table_counts(client):
    from app.db import connect

    with connect() as conn:
        return (
            conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"],
            conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"],
            conn.execute("SELECT COUNT(*) c FROM intervals").fetchone()["c"],
            conn.execute("SELECT COUNT(*) c FROM revisions").fetchone()["c"],
        )


def post_json(client, path, body):
    """以 NaN/Infinity 字面量发送 JSON（模拟真实客户端的非标准 JSON 令牌）。"""
    return client.post(
        path,
        content=json.dumps(body, allow_nan=True),
        headers={"Content-Type": "application/json"},
    )


def put_json(client, path, body):
    return client.put(
        path,
        content=json.dumps(body, allow_nan=True),
        headers={"Content-Type": "application/json"},
    )


# ---------- POST /observations：NaN/±Infinity 一律 422 ----------

@pytest.mark.parametrize("bad", NON_FINITE)
def test_create_rejects_non_finite_reference(client, bad):
    resp = post_json(
        client, "/observations",
        {"timestamp": 1000.0, "reference": bad, "tolerance": 0.5,
         "readings": [9.0, 9.1, 8.9]},
    )
    assert resp.status_code == 422
    assert table_counts(client) == (0, 0, 0, 0)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_create_rejects_non_finite_tolerance(client, bad):
    resp = post_json(
        client, "/observations",
        {"timestamp": 1000.0, "reference": 10.0, "tolerance": bad,
         "readings": [9.0, 9.1, 8.9]},
    )
    assert resp.status_code == 422
    assert table_counts(client) == (0, 0, 0, 0)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_create_rejects_non_finite_reading(client, bad):
    resp = post_json(
        client, "/observations",
        {"timestamp": 1000.0, "reference": 10.0, "tolerance": 0.5,
         "readings": [9.0, bad, 8.9]},
    )
    assert resp.status_code == 422
    assert table_counts(client) == (0, 0, 0, 0)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_create_rejects_non_finite_timestamp(client, bad):
    resp = post_json(
        client, "/observations",
        {"timestamp": bad, "reference": 10.0, "tolerance": 0.5,
         "readings": [9.0, 9.1, 8.9]},
    )
    assert resp.status_code == 422
    assert table_counts(client) == (0, 0, 0, 0)


def test_create_rejects_timestamp_unconvertible_to_utc_date(client):
    # 有限但超出平台 time_t 范围、无法转换为 UTC 日期的时点
    resp = post_json(
        client, "/observations",
        {"timestamp": 1e300, "reference": 10.0, "tolerance": 0.5,
         "readings": [9.0, 9.1, 8.9]},
    )
    assert resp.status_code == 422
    assert "UTC date" in resp.text
    assert table_counts(client) == (0, 0, 0, 0)


def test_create_rejects_iso_timestamp_unconvertible_to_utc_date(client):
    # 远超 datetime 可表达范围的 ISO 时点同样 422
    resp = client.post(
        "/observations",
        json={"timestamp": "99999-01-01T00:00:00Z", "reference": 10.0,
              "tolerance": 0.5, "readings": [9.0, 9.1, 8.9]},
    )
    assert resp.status_code == 422
    assert table_counts(client) == (0, 0, 0, 0)


def test_create_error_response_with_nan_reading_is_valid_json(client):
    # NaN 回显嵌在 readings 数组中时，422 响应体仍须是合法 JSON（不得 500）
    resp = post_json(
        client, "/observations",
        {"timestamp": 1000.0, "reference": 10.0, "tolerance": 0.5,
         "readings": [9.0, float("nan"), 8.9]},
    )
    assert resp.status_code == 422
    parsed = json.loads(resp.text)
    assert isinstance(parsed["detail"], list)


# ---------- 有限但极端的输入 ----------

def test_create_accepts_huge_finite_equal_readings_even_count(client):
    # 4 个同为 1e308 的读数：偶数个读数中点 (1e308+1e308)/2 会溢出，
    # 不得仅因中位数求法溢出而误拒；组中位数仍为 1e308，无异常读数
    big = 1e308
    body = make_observation(client, ts=1000.0, reference=big, tolerance=0.0,
                            readings=[big, big, big, big])
    assert body["group_median"] == pytest.approx(big)
    assert body["bias"] == pytest.approx(0.0)
    assert body["outliers"] == []
    assert table_counts(client) == (1, 4, 0, 0)


def test_create_accepts_huge_finite_equal_readings_odd_count(client):
    big = 1e308
    body = make_observation(client, ts=1000.0, reference=big, tolerance=0.0,
                            readings=[big, big, big])
    assert body["group_median"] == pytest.approx(big)
    assert body["bias"] == pytest.approx(0.0)
    assert body["outliers"] == []


def test_create_rejects_finite_input_whose_bias_overflows(client):
    # reference 与有效读数符号相反、均接近 float 上界：reference - median 溢出
    resp = post_json(
        client, "/observations",
        {"timestamp": 1000.0, "reference": 1e308, "tolerance": 0.0,
         "readings": [-1e308, -1e308, -1e308]},
    )
    assert resp.status_code == 422
    assert "overflows" in resp.json()["detail"]
    assert table_counts(client) == (0, 0, 0, 0)


def test_create_bias_overflow_after_outliers_removed(client):
    # 20.0 按现有规则先判为异常；剔除后有效中位数 -1e308 与参考值之差溢出
    resp = post_json(
        client, "/observations",
        {"timestamp": 1000.0, "reference": 1e308, "tolerance": 0.5,
         "readings": [-1e308, -1e308, -1e308, 20.0]},
    )
    assert resp.status_code == 422
    assert "overflows" in resp.json()["detail"]
    assert table_counts(client) == (0, 0, 0, 0)


def test_outlier_rule_unchanged_for_extreme_values(client):
    # 沿用“偏离组中位数超过容差即异常”：-1e308 相对组中位数 1e308 判为异常
    body = make_observation(client, ts=1000.0, reference=1e308, tolerance=0.0,
                            readings=[1e308, 1e308, 1e308, -1e308])
    assert body["group_median"] == pytest.approx(1e308)
    assert body["outliers"] == [-1e308]
    assert [r["is_outlier"] for r in body["readings"]] == [False, False, False, True]


# ---------- 拒绝后无任何落库变化 ----------

def test_rejected_create_leaves_no_rows(client):
    make_observation(client, ts=1000.0)
    make_observation(client, ts=2000.0)
    assert table_counts(client) == (2, 6, 1, 0)

    for body in (
        {"timestamp": 3000.0, "reference": float("nan"), "tolerance": 0.5,
         "readings": [9.0, 9.1, 8.9]},
        {"timestamp": 3000.0, "reference": 10.0, "tolerance": float("inf"),
         "readings": [9.0, 9.1, 8.9]},
        {"timestamp": 3000.0, "reference": 10.0, "tolerance": 0.5,
         "readings": [9.0, 9.1, float("nan")]},
        {"timestamp": float("inf"), "reference": 10.0, "tolerance": 0.5,
         "readings": [9.0, 9.1, 8.9]},
        {"timestamp": 1e300, "reference": 10.0, "tolerance": 0.5,
         "readings": [9.0, 9.1, 8.9]},
        {"timestamp": 3000.0, "reference": 1e308, "tolerance": 0.0,
         "readings": [-1e308, -1e308, -1e308]},
    ):
        assert post_json(client, "/observations", body).status_code == 422

    assert table_counts(client) == (2, 6, 1, 0)


# ---------- PUT /observations/{id} ----------

@pytest.fixture()
def two_points(client):
    make_observation(client, ts=1000.0)
    make_observation(client, ts=2000.0)
    return client


def revise(client, obs_id, payload_part):
    body = {"timestamp": 1000.0, "reference": 10.0, "tolerance": 0.5,
            "readings": [9.0, 9.1, 8.9], "revision": 1, **payload_part}
    return put_json(client, f"/observations/{obs_id}", body)


@pytest.mark.parametrize("field,bad", [
    ("reference", float("nan")),
    ("reference", float("inf")),
    ("reference", float("-inf")),
    ("tolerance", float("nan")),
    ("tolerance", float("inf")),
    ("timestamp", float("nan")),
    ("timestamp", float("inf")),
])
def test_revise_rejects_non_finite_fields(two_points, field, bad):
    resp = revise(two_points, 1, {field: bad})
    assert resp.status_code == 422
    obs = next(o for o in two_points.get("/observations").json() if o["id"] == 1)
    assert obs["revision"] == 1  # 版本未推进
    assert table_counts(two_points) == (2, 6, 1, 0)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_revise_rejects_non_finite_reading(two_points, bad):
    resp = revise(two_points, 1, {"readings": [9.0, 9.1, bad]})
    assert resp.status_code == 422
    assert table_counts(two_points) == (2, 6, 1, 0)


def test_revise_rejects_unconvertible_timestamp(two_points):
    resp = revise(two_points, 1, {"timestamp": 1e300})
    assert resp.status_code == 422
    assert table_counts(two_points) == (2, 6, 1, 0)


def test_revise_rejects_bias_overflow_without_any_change(two_points):
    counts_before = table_counts(two_points)
    obs_before = two_points.get("/observations").json()
    resp = revise(two_points, 1, {"reference": 1e308, "tolerance": 0.0,
                                  "readings": [-1e308, -1e308, -1e308]})
    assert resp.status_code == 422
    assert "overflows" in resp.json()["detail"]
    # 观测、读数、区间、修订快照均无变化
    assert table_counts(two_points) == counts_before
    assert two_points.get("/observations").json() == obs_before
    assert two_points.get("/observations/1/revisions").json() == []


def test_revise_accepts_huge_finite_equal_readings(two_points):
    big = 1e308
    resp = revise(two_points, 1, {"reference": big, "tolerance": 0.0,
                                  "readings": [big, big, big, big]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["group_median"] == pytest.approx(big)
    assert body["bias"] == pytest.approx(0.0)
    assert body["revision"] == 2


# ---------- POST /observation-impact-preview ----------

def preview(client, **overrides):
    body = {"timestamp": 1500.0, "reference": 10.0, "tolerance": 0.5,
            "readings": [8.0, 8.1, 7.9], "impact_limit": 1.0, **overrides}
    return post_json(client, "/observation-impact-preview", body)


@pytest.mark.parametrize("field,bad", [
    ("reference", float("nan")),
    ("reference", float("inf")),
    ("reference", float("-inf")),
    ("tolerance", float("nan")),
    ("tolerance", float("inf")),
    ("timestamp", float("nan")),
    ("timestamp", float("inf")),
])
def test_preview_rejects_non_finite_fields(two_points, field, bad):
    resp = preview(two_points, **{field: bad})
    assert resp.status_code == 422
    assert table_counts(two_points) == (2, 6, 1, 0)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_preview_rejects_non_finite_reading(two_points, bad):
    resp = preview(two_points, readings=[8.0, 8.1, bad])
    assert resp.status_code == 422


def test_preview_rejects_unconvertible_timestamp(two_points):
    assert preview(two_points, timestamp=1e300).status_code == 422


def test_preview_rejects_bias_overflow(two_points):
    resp = preview(two_points, reference=1e308, tolerance=0.0,
                   readings=[-1e308, -1e308, -1e308])
    assert resp.status_code == 422
    assert "overflows" in resp.json()["detail"]


def test_preview_rejects_when_threshold_crossing_overflows(client):
    # 所有输入均有限，但候选变化量与阈值的解析交点时刻溢出为 Infinity
    # （变化量 1e308、阈值 1 秒级，求根超出 float 时间表达范围）：422 且只读。
    big = 1e308
    make_observation(client, ts=1000.0, reference=-big, tolerance=0.0,
                     readings=[0.0, 0.0, 0.0])          # bias -1e308
    make_observation(client, ts=2000.0, reference=0.0, tolerance=0.0,
                     readings=[big, big, big])           # bias -1e308
    resp = post_json(
        client, "/observation-impact-preview",
        {"timestamp": 1500.0, "reference": 0.0, "tolerance": 0.0,
         "readings": [0.0, 0.0, 0.0], "impact_limit": 1.0},
    )
    assert resp.status_code == 422
    assert "overflows" in resp.json()["detail"]
    assert table_counts(client) == (2, 6, 1, 0)


def test_preview_accepts_huge_finite_equal_readings(two_points):
    big = 1e308
    # 先把两侧观测放到极端量级，候选同量级读数算出的偏差为 0
    assert two_points.put(
        "/observations/1",
        json={"timestamp": 1000.0, "reference": big, "tolerance": 0.0,
              "readings": [big, big, big], "revision": 1},
    ).status_code == 200
    assert two_points.put(
        "/observations/2",
        json={"timestamp": 2000.0, "reference": big, "tolerance": 0.0,
              "readings": [big, big, big], "revision": 1},
    ).status_code == 200
    resp = preview(two_points, reference=big, tolerance=0.0,
                   readings=[big, big, big, big])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["candidate_bias"] == pytest.approx(0.0)
    assert math.isfinite(body["max_abs_change"])


def test_preview_with_invalid_numerics_is_read_only(two_points):
    before = (
        two_points.get("/observations").json(),
        two_points.get("/intervals").json(),
        two_points.get("/observations/1/revisions").json(),
    )
    for bad in (
        {"reference": float("nan")},
        {"tolerance": float("inf")},
        {"readings": [1.0, 2.0, float("nan")]},
        {"timestamp": float("-inf")},
        {"timestamp": 1e300},
        {"reference": 1e308, "tolerance": 0.0, "readings": [-1e308, -1e308, -1e308]},
    ):
        assert preview(two_points, **bad).status_code == 422
    after = (
        two_points.get("/observations").json(),
        two_points.get("/intervals").json(),
        two_points.get("/observations/1/revisions").json(),
    )
    assert after == before


# ---------- 纯函数单测：溢出安全的中位数 ----------

def test_finite_median_matches_statistics_median_for_ordinary_values():
    from statistics import median

    from app.calibration import finite_median

    for values in (
        [1.0],
        [3.0, 1.0, 2.0],
        [1.0, 2.0, 3.0, 4.0],
        [9.0, 9.1, 8.9, 20.0],
        [-1.0, 1.0],
    ):
        assert finite_median(list(values)) == median(values)


def test_finite_median_does_not_overflow_for_equal_extremes():
    from app.calibration import finite_median

    assert finite_median([1e308, 1e308, 1e308, 1e308]) == 1e308
    assert finite_median([-1e308, -1e308, -1e308, -1e308]) == -1e308
    assert math.isfinite(finite_median([1e308, 1e308]))
