"""数值边界测试：漂移补偿三端（/corrections、/historical-correction、
/correction-receipts）在极大有限偏差/读数下的行为。

- 两端偏差异号且均接近 float 上界（1e308 与 -1e308）时，中点插值的直接式
  会在 b1−b0 处先溢出；改为凸组合后结果可表示时必须返回有限值，观测点与
  区间端点保持原偏差。
- 读数为 NaN/±Infinity（请求体令牌或查询参数）统一 422。
- 读数与偏差均有限但最终校正值溢出为 ±Infinity 时返回 422，且不签发回执。
- 覆盖范围外仍为 400；既有回执签名格式（version=2、HMAC-SHA256）不变。
"""
from __future__ import annotations

import json
import math

import pytest
from fastapi.testclient import TestClient

NON_FINITE = (float("nan"), float("inf"), float("-inf"))
BIG = 1e308


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CORRECTION_RECEIPT_SECRET", "test-correction-receipt-secret")
    monkeypatch.delenv("CORRECTION_RECEIPT_KEYS", raising=False)
    monkeypatch.delenv("CORRECTION_RECEIPT_ACTIVE_KEY", raising=False)
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def extreme_points(client):
    """两条极端观测：1000 处 bias +1e308（reference 1e308、读数全 0），
    2000 处 bias -1e308（reference 0、读数全 1e308）。"""
    def make(ts, reference, readings):
        resp = client.post(
            "/observations",
            json={"timestamp": ts, "reference": reference, "tolerance": 0.0, "readings": readings},
        )
        assert resp.status_code == 201, resp.text

    make(1000.0, BIG, [0.0, 0.0, 0.0])
    make(2000.0, 0.0, [BIG, BIG, BIG])
    return client


def post_json(client, path, body):
    """以 NaN/Infinity 字面量发送 JSON（模拟真实客户端的非标准 JSON 令牌）。"""
    return client.post(
        path,
        content=json.dumps(body, allow_nan=True),
        headers={"Content-Type": "application/json"},
    )


# ---------- 插值：中间溢出规避，结果可表示时保持有限 ----------

def test_correction_midpoint_between_extreme_biases_is_finite_zero(extreme_points):
    # 直接式在 b1−b0 = -2e308 处先溢出为 -Infinity；中点真值 0.0 可表示
    resp = post_json(extreme_points, "/corrections", {"timestamp": 1500.0, "reading": 0.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bias"] == pytest.approx(0.0)
    assert math.isfinite(body["bias"])
    assert body["corrected"] == pytest.approx(0.0)
    # 区间端点偏差原样保留，不因中间计算而被改写
    assert body["interval"]["bias_start"] == pytest.approx(BIG)
    assert body["interval"]["bias_end"] == pytest.approx(-BIG)
    assert body["endpoints"][0]["bias"] == pytest.approx(BIG)
    assert body["endpoints"][1]["bias"] == pytest.approx(-BIG)


@pytest.mark.parametrize("ts,expected", [
    (1250.0, 5e307),    # 1/4 处
    (1750.0, -5e307),   # 3/4 处
])
def test_correction_interior_points_between_extreme_biases(extreme_points, ts, expected):
    resp = post_json(extreme_points, "/corrections", {"timestamp": ts, "reading": 0.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert math.isfinite(body["bias"])
    assert body["bias"] == pytest.approx(expected)
    assert body["corrected"] == pytest.approx(expected)


def test_correction_at_observation_point_keeps_original_extreme_bias(extreme_points):
    # 恰落在观测点上：直接使用该点原偏差，绝不经过插值
    resp = post_json(extreme_points, "/corrections", {"timestamp": 1000.0, "reading": 0.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bias"] == pytest.approx(BIG)
    assert body["interval"] is None
    assert len(body["endpoints"]) == 1
    assert body["endpoints"][0]["bias"] == pytest.approx(BIG)


def test_historical_correction_midpoint_between_extreme_biases_is_finite(extreme_points):
    resp = extreme_points.get(
        "/historical-correction",
        params={"effective_at": 1500.0, "as_of": 4_000_000_000.0, "reading": 0.0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert math.isfinite(body["bias"])
    assert body["bias"] == pytest.approx(0.0)
    assert body["corrected"] == pytest.approx(0.0)
    assert body["interval"]["bias_start"] == pytest.approx(BIG)
    assert body["interval"]["bias_end"] == pytest.approx(-BIG)


def test_receipt_at_extreme_midpoint_is_signed_and_verifies(extreme_points):
    resp = post_json(extreme_points, "/correction-receipts",
                     {"timestamp": 1500.0, "reading": 0.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 2
    assert body["key_id"] == "dev"
    assert body["bias"] == pytest.approx(0.0)
    assert body["corrected"] == pytest.approx(0.0)
    assert isinstance(body["issued_at"], int)
    assert len(body["receipt"]) == 64  # 既有签名格式：HMAC-SHA256 小写十六进制
    verify = extreme_points.post("/correction-receipts/verify", json=body)
    assert verify.status_code == 200
    assert verify.json() == {"valid": True}


# ---------- 读数 NaN/±Infinity：三端入口统一 422 ----------

@pytest.mark.parametrize("bad", NON_FINITE)
def test_correction_rejects_non_finite_reading(extreme_points, bad):
    resp = post_json(extreme_points, "/corrections", {"timestamp": 1500.0, "reading": bad})
    assert resp.status_code == 422


@pytest.mark.parametrize("bad", NON_FINITE)
def test_receipt_rejects_non_finite_reading_without_signing(extreme_points, bad):
    resp = post_json(extreme_points, "/correction-receipts",
                     {"timestamp": 1500.0, "reading": bad})
    assert resp.status_code == 422
    assert "receipt" not in resp.json()  # 非有限读数绝不签发回执


@pytest.mark.parametrize("token", ["nan", "NaN", "inf", "Infinity", "-inf", "-Infinity"])
def test_historical_correction_rejects_non_finite_reading_query(extreme_points, token):
    resp = extreme_points.get(
        "/historical-correction",
        params={"effective_at": 1500.0, "as_of": 4_000_000_000.0, "reading": token},
    )
    assert resp.status_code == 422


def test_correction_rejects_non_numeric_reading(extreme_points):
    for payload in (
        {"timestamp": 1500.0, "reading": "not-a-number"},
        {"timestamp": 1500.0, "reading": None},
        {"timestamp": 1500.0},
    ):
        assert post_json(extreme_points, "/corrections", payload).status_code == 422


# ---------- 最终校正值溢出：422 且不签发回执 ----------

def test_correction_rejects_unrepresentable_corrected_value(extreme_points):
    # 读数 1e308 叠加观测点偏差 1e308 → Infinity：偏差本身有限且原样返回无意义，
    # 最终校正值不可表示，拒绝为 422
    resp = post_json(extreme_points, "/corrections",
                     {"timestamp": 1000.0, "reading": BIG})
    assert resp.status_code == 422
    assert "overflows" in resp.json()["detail"]


def test_receipt_does_not_sign_when_corrected_value_overflows(extreme_points):
    resp = post_json(extreme_points, "/correction-receipts",
                     {"timestamp": 1000.0, "reading": BIG})
    assert resp.status_code == 422
    body = resp.json()
    assert "receipt" not in body
    assert "overflows" in body["detail"]


def test_historical_correction_rejects_unrepresentable_corrected_value(extreme_points):
    resp = extreme_points.get(
        "/historical-correction",
        params={"effective_at": 1000.0, "as_of": 4_000_000_000.0, "reading": BIG},
    )
    assert resp.status_code == 422
    assert "overflows" in resp.json()["detail"]


def test_correction_accepts_large_but_representable_sum(extreme_points):
    # 读数 -5e307 + 偏差 1e308 = 5e307：同量级极端值只要和可表示即正常返回
    resp = post_json(extreme_points, "/corrections",
                     {"timestamp": 1000.0, "reading": -5e307})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["corrected"] == pytest.approx(5e307)


# ---------- 覆盖范围外保持 400 ----------

def test_out_of_coverage_remains_400_with_finite_reading(extreme_points):
    assert post_json(extreme_points, "/corrections",
                     {"timestamp": 500.0, "reading": 1.0}).status_code == 400
    assert post_json(extreme_points, "/correction-receipts",
                     {"timestamp": 2500.0, "reading": 1.0}).status_code == 400
    resp = extreme_points.get(
        "/historical-correction",
        params={"effective_at": 500.0, "as_of": 4_000_000_000.0, "reading": 1.0},
    )
    assert resp.status_code == 400


# ---------- 纯函数单测 ----------

def test_interpolate_matches_direct_formula_for_ordinary_values():
    from app.calibration import interpolate

    # 与直接式 b0 + (b1-b0)·w 一致（b1-b0 有限时逐位走原分支）
    assert interpolate(1500.0, 1000.0, 1.0, 2000.0, 3.0) == pytest.approx(2.0)
    assert interpolate(1000.0, 1000.0, 1.0, 2000.0, 3.0) == pytest.approx(1.0)
    assert interpolate(2000.0, 1000.0, 1.0, 2000.0, 3.0) == pytest.approx(3.0)
    assert interpolate(1250.0, 1000.0, 1.0, 2000.0, 3.0) == pytest.approx(1.5)
    # 退化区间（t0 == t1）返回 b0
    assert interpolate(1000.0, 1000.0, 7.0, 1000.0, 9.0) == pytest.approx(7.0)


def test_interpolate_does_not_overflow_for_opposite_sign_extremes():
    from app.calibration import interpolate

    # 中点：直接式在 b1-b0 溢出，凸组合给出可表示的 0.0
    assert math.isfinite(interpolate(1500.0, 1000.0, BIG, 2000.0, -BIG))
    assert interpolate(1500.0, 1000.0, BIG, 2000.0, -BIG) == pytest.approx(0.0)
    # 两端权重精确还原端点原偏差（含 t 恰为端点、另一端为极端值的情形）
    assert interpolate(1000.0, 1000.0, BIG, 2000.0, -BIG) == pytest.approx(BIG)
    assert interpolate(2000.0, 1000.0, BIG, 2000.0, -BIG) == pytest.approx(-BIG)
    assert interpolate(2000.0, 1000.0, -1e100, 2000.0, 1.0) == pytest.approx(1.0)
    # 内部权重结果有限且为两端的凸组合
    for ts, expected in ((1250.0, 5e307), (1750.0, -5e307)):
        value = interpolate(ts, 1000.0, BIG, 2000.0, -BIG)
        assert math.isfinite(value)
        assert value == pytest.approx(expected)


def test_interpolate_finite_when_product_but_not_difference_overflows():
    # b1-b0 有限，但 (b1-b0)·w 溢出（差约 2.4e307、w≈0.68）：直接式为 -inf，
    # 回退分支必须给出落在两端之间的有限值
    from app.calibration import interpolate

    b0, b1, t = 2.4155049975983286e307, 4.729424283280249, 1676.6994874229113
    assert not math.isfinite(b0 + (b1 - b0) * (t - 1000.0) / 1000.0)  # 直接式确实溢出
    value = interpolate(t, 1000.0, b0, 2000.0, b1)
    assert math.isfinite(value)
    assert min(b0, b1) <= value <= max(b0, b1)
    # 真解约 7.81e306（凸组合）
    assert value == pytest.approx(7.80934003856059e306, rel=1e-12)


def test_correct_reading_returns_sum_when_representable():
    from app.calibration import correct_reading

    assert correct_reading(5.0, 2.0) == pytest.approx(7.0)
    assert correct_reading(1e308, -5e307) == pytest.approx(5e307)
    # 同号但和不超过 float 上界：0.6e308 + 0.6e308 = 1.2e308 仍可表示
    assert correct_reading(6e307, 6e307) == pytest.approx(1.2e308)


def test_correct_reading_raises_when_sum_overflows():
    from app.calibration import CalibrationError, correct_reading

    with pytest.raises(CalibrationError, match="overflows"):
        correct_reading(1e308, 1e308)
    with pytest.raises(CalibrationError, match="overflows"):
        correct_reading(-1e308, -1e308)
    with pytest.raises(CalibrationError):
        correct_reading(float("inf"), 1.0)
    with pytest.raises(CalibrationError):
        correct_reading(1.0, float("nan"))
