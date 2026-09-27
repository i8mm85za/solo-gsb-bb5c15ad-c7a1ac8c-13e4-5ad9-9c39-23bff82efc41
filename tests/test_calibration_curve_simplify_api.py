"""校准曲线精简 API 端到端测试：每个用例使用独立临时 SQLite 库。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as c:
        yield c


def make_observation(client, ts, bias, reference=None):
    # 三次相同读数 100.0 → 有效中位数 100.0，bias = reference − 100.0
    resp = client.post(
        "/observations",
        json={
            "timestamp": ts,
            "reference": 100.0 + bias if reference is None else reference,
            "tolerance": 0.5,
            "readings": [100.0, 100.0, 100.0],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def simplify(client, max_error, **extra):
    payload = {"max_error": max_error, **extra}
    return client.post("/calibration-curves/simplify", json=payload)


# ---------- 基本精简 ----------

def test_simplify_basic_response_shape(client):
    o1 = make_observation(client, 0.0, 0.0)
    o2 = make_observation(client, 3600.0, 0.25)
    o3 = make_observation(client, 7200.0, 1.0)
    o4 = make_observation(client, 10800.0, 0.25)
    o5 = make_observation(client, 14400.0, 0.0)
    resp = simplify(client, 0.5)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_error"] == pytest.approx(0.5)
    assert body["coverage_from"] == 0.0
    assert body["coverage_to"] == pytest.approx(14400.0)
    assert body["coverage_from_iso"].startswith("1970-01-01T00:00:00")
    assert body["original_count"] == 5
    assert body["retained_count"] == 3
    assert [p["observation_id"] for p in body["retained"]] == [o1["id"], o3["id"], o5["id"]]
    assert [p["timestamp"] for p in body["retained"]] == [0.0, 7200.0, 14400.0]
    assert [p["bias"] for p in body["retained"]] == [0.0, 1.0, 0.0]
    assert body["retained"][1]["timestamp_iso"].startswith("1970-01-01T02:00:00")
    # 弦 0→2（峰值 1.0）在 09:00/11:00 的偏差恰为 0.25，并列取最早时点 09:00
    assert body["max_abs_error"] == pytest.approx(0.25)
    assert body["max_error_at"] == pytest.approx(3600.0)
    assert body["max_error_at_iso"].startswith("1970-01-01T01:00:00")


def test_simplify_two_observations_keeps_both(client):
    make_observation(client, 1000.0, 1.0)
    make_observation(client, 2000.0, 3.0)
    body = simplify(client, 0.0).json()
    assert body["original_count"] == 2
    assert body["retained_count"] == 2
    assert [p["observation_id"] for p in body["retained"]] == [1, 2]
    assert body["max_abs_error"] == 0.0
    assert body["max_error_at"] == 1000.0  # 偏差全为 0，最早时点即首观测
    assert body["coverage_from"] == 1000.0
    assert body["coverage_to"] == 2000.0


def test_simplify_zero_tolerance_keeps_all_observations(client):
    for ts, bias in [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0), (3.0, 2.0)]:
        make_observation(client, ts, bias)
    body = simplify(client, 0.0).json()
    assert body["retained_count"] == 4
    assert body["max_abs_error"] == 0.0
    assert body["max_error_at"] == 0.0


def test_simplify_collinear_intermediate_points_dropped(client):
    # bias 严格线性，中间观测全部可丢，只剩首尾
    for ts, bias in [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0)]:
        make_observation(client, ts, bias)
    body = simplify(client, 0.0).json()
    assert body["retained_count"] == 2
    assert [p["observation_id"] for p in body["retained"]] == [1, 5]
    assert body["max_abs_error"] == pytest.approx(0.0)


def test_simplify_accepts_iso8601_independent_observations(client):
    # max_error 是数值请求体；观测时点用 ISO 8601 登记，响应覆盖范围与保留点照常输出
    make_observation(client, "2026-09-18T08:00:00Z", 0.0)
    make_observation(client, "2026-09-18T09:00:00Z", 1.0)
    make_observation(client, "2026-09-18T10:00:00Z", 0.0)
    body = simplify(client, 1.0).json()
    assert body["coverage_from"] == pytest.approx(1789718400.0)
    assert body["retained_count"] == 2
    assert body["max_abs_error"] == pytest.approx(1.0)
    assert body["max_error_at_iso"] == "2026-09-18T09:00:00+00:00"


def test_simplify_reports_per_vertex_errors_not_endpoints_only(client):
    # 单弦在中间折点的偏差超过容差，但端点偏差为 0：只查端点会错误放行
    make_observation(client, 0.0, 0.0)
    make_observation(client, 1.0, 0.0)
    make_observation(client, 2.0, 10.0)
    make_observation(client, 3.0, 0.0)
    make_observation(client, 4.0, 0.0)
    # 容差 5：单弦首尾（中间偏差 10）不行，但弦 0→2、2→4 在折点 1/3 处恰为 5 → 3 点
    body = simplify(client, 5.0).json()
    assert body["retained_count"] == 3  # 必须保留折点 2，不能只留首尾
    assert body["max_abs_error"] == pytest.approx(5.0)
    assert body["max_error_at"] == pytest.approx(1.0)  # 两侧 5.0 并列，取最早时点
    # 容差 4：连 3 点方案都不允许（5 > 4）→ 全部保留，证明误差确实逐折点核对
    assert simplify(client, 4.0).json()["retained_count"] == 5


# ---------- 409：观测不足 ----------

def test_simplify_without_observations_conflicts(client):
    resp = simplify(client, 0.5)
    assert resp.status_code == 409
    assert "at least 2 observations" in resp.json()["detail"]


def test_simplify_with_single_observation_conflicts(client):
    make_observation(client, 1000.0, 1.0)
    resp = simplify(client, 0.0)
    assert resp.status_code == 409
    assert "got 1" in resp.json()["detail"]


# ---------- 422：max_error 校验 ----------

def test_simplify_rejects_negative_max_error(client):
    make_observation(client, 0.0, 0.0)
    make_observation(client, 1.0, 1.0)
    resp = simplify(client, -0.001)
    assert resp.status_code == 422


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_simplify_rejects_non_finite_max_error(client, raw):
    make_observation(client, 0.0, 0.0)
    make_observation(client, 1.0, 1.0)
    resp = client.post(
        "/calibration-curves/simplify",
        content=f'{{"max_error": {raw}}}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"][-1] == "max_error"


@pytest.mark.parametrize("value", ["0.5", True, False, None])
def test_simplify_rejects_wrong_typed_max_error(client, value):
    make_observation(client, 0.0, 0.0)
    make_observation(client, 1.0, 1.0)
    resp = client.post("/calibration-curves/simplify", json={"max_error": value})
    assert resp.status_code == 422


def test_simplify_rejects_missing_max_error(client):
    resp = client.post("/calibration-curves/simplify", json={})
    assert resp.status_code == 422


def test_simplify_rejects_malformed_json(client):
    resp = client.post(
        "/calibration-curves/simplify",
        content="{not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


# ---------- 只读保证 ----------

def test_simplify_is_read_only(client):
    make_observation(client, 1000.0, 1.0)
    make_observation(client, 2000.0, 0.0)
    make_observation(client, 3000.0, 2.0)
    before = (
        client.get("/observations").json(),
        client.get("/intervals").json(),
    )
    # 成功请求与 422 失败请求都不得改动任何现状数据；重复调用幂等
    first = simplify(client, 0.5).json()
    assert simplify(client, 0.0).status_code == 200
    assert client.post(
        "/calibration-curves/simplify", json={"max_error": -1.0}
    ).status_code == 422
    assert simplify(client, 0.5).json() == first
    after = (
        client.get("/observations").json(),
        client.get("/intervals").json(),
    )
    assert after == before


def test_simplify_is_read_only_including_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test2.db"))
    from app.main import app

    with TestClient(app) as c:
        # 空库时 409 不落任何数据
        assert c.post("/calibration-curves/simplify", json={"max_error": 0.5}).status_code == 409
        assert c.get("/observations").json() == []
        assert c.get("/intervals").json() == []
        make_observation(c, 1000.0, 1.0)
        one = (c.get("/observations").json(), c.get("/intervals").json())
        # 仅一条观测时 409 同样只读
        assert c.post("/calibration-curves/simplify", json={"max_error": 0.5}).status_code == 409
        assert (c.get("/observations").json(), c.get("/intervals").json()) == one
