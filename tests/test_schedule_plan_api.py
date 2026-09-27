"""校准排期规划 API 端到端测试：每个用例使用独立临时 SQLite 库。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def clock(monkeypatch):
    """可控时钟：service 里的 time.time() 返回当前值，测试可随时推进。"""
    import app.service as service_module

    now = [1_000_000.0]
    monkeypatch.setattr(service_module.time, "time", lambda: now[0])
    return now


def make_observation(client, ts, reference=10.0, tolerance=0.5, readings=(9.0, 9.1, 8.9)):
    resp = client.post(
        "/observations",
        json={"timestamp": ts, "reference": reference, "tolerance": tolerance, "readings": list(readings)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def plan(client, frm, to, max_gap, blackout=None):
    payload = {"from": frm, "to": to, "max_gap": max_gap}
    if blackout is not None:
        payload["blackout"] = blackout
    return client.post("/calibration-schedules/plan", json=payload)


# ---------- 基本规划 ----------

def test_plan_basic_with_inside_anchor(client):
    o1 = make_observation(client, 1000.0)
    o2 = make_observation(client, 2000.0)
    o3 = make_observation(client, 4000.0)
    resp = plan(client, 1000.0, 4000.0, 1500.0)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from"] == pytest.approx(1000.0)
    assert body["to"] == pytest.approx(4000.0)
    assert body["max_gap"] == pytest.approx(1500.0)
    assert body["blackout"] == []
    # 固定锚点：窗口内全部观测（含边界上的观测）
    assert [a["observation_id"] for a in body["fixed_anchors"]] == [o1["id"], o2["id"], o3["id"]]
    assert [a["timestamp"] for a in body["fixed_anchors"]] == [1000.0, 2000.0, 4000.0]
    assert body["fixed_anchors"][0]["timestamp_iso"].startswith("1970-01-01T00:16:40")
    # 间隔 1000 无需补点；间隔 2000 需 1 点，最早位置 2500
    assert [p["timestamp"] for p in body["suggested"]] == [2500.0]
    assert body["suggested"][0]["timestamp_iso"].startswith("1970-01-01T00:41:40")
    segments = body["segments"]
    assert [(s["t_start"], s["t_end"]) for s in segments] == [
        (1000.0, 2000.0), (2000.0, 2500.0), (2500.0, 4000.0)
    ]
    assert [s["duration_seconds"] for s in segments] == [1000.0, 500.0, 1500.0]
    assert all(s["duration_seconds"] <= 1500.0 for s in segments)
    assert [(s["start_kind"], s["end_kind"]) for s in segments] == [
        ("fixed", "fixed"), ("fixed", "suggested"), ("suggested", "fixed")
    ]


def test_plan_boundary_anchors_cover_window_from_outside(client):
    # 窗口两侧最近的观测在窗口之外：它们作为覆盖边界的固定锚点
    make_observation(client, 1000.0)
    make_observation(client, 5000.0)
    body = plan(client, 1500.0, 4500.0, 1000.0).json()
    assert [a["timestamp"] for a in body["fixed_anchors"]] == [1000.0, 5000.0]
    assert [p["timestamp"] for p in body["suggested"]] == [2000.0, 3000.0, 4000.0]
    assert len(body["segments"]) == 4
    assert all(s["duration_seconds"] == pytest.approx(1000.0) for s in body["segments"])


def test_plan_returns_lexicographically_earliest_minimal_set(client):
    # 间隔 20、max_gap 6：最少 3 点，字典序最早为 (2, 8, 14)
    make_observation(client, 0.0)
    make_observation(client, 20.0)
    body = plan(client, 0.0, 20.0, 6.0).json()
    assert [p["timestamp"] for p in body["suggested"]] == [2.0, 8.0, 14.0]


def test_plan_no_suggested_points_when_gaps_within_max(client):
    make_observation(client, 0.0)
    make_observation(client, 3000.0)
    make_observation(client, 6000.0)
    body = plan(client, 0.0, 6000.0, 3000.0).json()
    assert body["suggested"] == []
    assert [(s["t_start"], s["t_end"]) for s in body["segments"]] == [(0.0, 3000.0), (3000.0, 6000.0)]


def test_plan_accepts_iso8601_window_and_blackout(client):
    make_observation(client, "2026-09-18T08:00:00Z")   # 1789718400
    make_observation(client, "2026-09-18T12:00:00Z")   # 1789732800
    resp = plan(client, "2026-09-18T08:00:00Z", "2026-09-18T12:00:00Z", 7200.0)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from"] == pytest.approx(1789718400.0)
    assert body["from_iso"] == "2026-09-18T08:00:00+00:00"
    assert [p["timestamp"] for p in body["suggested"]] == [1789725600.0]
    assert body["suggested"][0]["timestamp_iso"] == "2026-09-18T10:00:00+00:00"


# ---------- 禁排时段 ----------

def test_plan_blackout_boundary_is_schedulable(client):
    make_observation(client, 0.0)
    make_observation(client, 10000.0)
    body = plan(client, 0.0, 10000.0, 5000.0, blackout=[{"from": 5000.0, "to": 6000.0}]).json()
    # 边界 5000 允许排期，恰为唯一可行位置
    assert [p["timestamp"] for p in body["suggested"]] == [5000.0]
    assert [(b["from"], b["to"]) for b in body["blackout"]] == [(5000.0, 6000.0)]


def test_plan_blackout_can_force_extra_points(client):
    make_observation(client, 0.0)
    make_observation(client, 10.0)
    body = plan(client, 0.0, 10.0, 5.0, blackout=[{"from": 4.0, "to": 6.0}]).json()
    # 无禁排时 1 点（位置 5）即可；禁排 (4, 6) 后最少 2 点，最早为 (1, 6)
    assert [p["timestamp"] for p in body["suggested"]] == [1.0, 6.0]


def test_plan_blackout_is_normalized_and_merged(client):
    make_observation(client, 0.0)
    make_observation(client, 100.0)
    body = plan(
        client, 0.0, 100.0, 200.0,
        blackout=[
            {"from": 35.0, "to": 50.0},
            {"from": 30.0, "to": 40.0},   # 与上一段重叠且乱序
            {"from": 60.0, "to": 70.0},
            {"from": 70.0, "to": 80.0},   # 端点相接：合并
            {"from": 90.0, "to": 90.0},   # 零长度：丢弃
        ],
    ).json()
    assert [(b["from"], b["to"]) for b in body["blackout"]] == [(30.0, 50.0), (60.0, 80.0)]
    assert body["blackout"][0]["from_iso"].startswith("1970-01-01T00:00:30")
    assert body["suggested"] == []   # 间隔 100 ≤ max_gap 200，无需补点


def test_plan_touching_blackouts_merge_and_block_interior(client):
    make_observation(client, 0.0)
    make_observation(client, 10.0)
    # [0,5] 与 [5,10] 合并为 [0,10]：相接点 5 成为内部禁止排期 → 无解
    resp = plan(client, 0.0, 10.0, 5.0, blackout=[{"from": 0.0, "to": 5.0}, {"from": 5.0, "to": 10.0}])
    assert resp.status_code == 409


def test_plan_blackout_accepts_iso8601(client):
    make_observation(client, 0.0)
    make_observation(client, 7200.0)
    body = plan(
        client, 0.0, 7200.0, 3600.0,
        blackout=[{"from": "1970-01-01T01:00:00Z", "to": "1970-01-01T01:30:00Z"}],
    ).json()
    assert [(b["from"], b["to"]) for b in body["blackout"]] == [(3600.0, 5400.0)]
    # 禁排 (3600, 5400)：3600 恰为边界允许排期
    assert [p["timestamp"] for p in body["suggested"]] == [3600.0]


# ---------- 冲突与参数校验 ----------

def test_plan_missing_left_boundary_anchor_conflicts(client):
    make_observation(client, 2000.0)
    make_observation(client, 3000.0)
    resp = plan(client, 1000.0, 2500.0, 500.0)
    assert resp.status_code == 409
    assert "window start" in resp.json()["detail"]


def test_plan_missing_right_boundary_anchor_conflicts(client):
    make_observation(client, 2000.0)
    make_observation(client, 3000.0)
    resp = plan(client, 2500.0, 4000.0, 500.0)
    assert resp.status_code == 409
    assert "window end" in resp.json()["detail"]


def test_plan_without_any_observation_conflicts(client):
    resp = plan(client, 1000.0, 2000.0, 500.0)
    assert resp.status_code == 409


def test_plan_infeasible_blackout_conflicts(client):
    make_observation(client, 0.0)
    make_observation(client, 10000.0)
    resp = plan(client, 0.0, 10000.0, 1000.0, blackout=[{"from": 0.0, "to": 10000.0}])
    assert resp.status_code == 409
    assert "no feasible calibration schedule" in resp.json()["detail"]


def test_plan_rejects_inverted_window(client):
    make_observation(client, 1000.0)
    make_observation(client, 2000.0)
    for frm, to in ((2000.0, 1000.0), (1500.0, 1500.0)):
        resp = plan(client, frm, to, 500.0)
        assert resp.status_code == 400
        assert "must be less than" in resp.json()["detail"]


def test_plan_rejects_non_positive_max_gap(client):
    make_observation(client, 1000.0)
    make_observation(client, 2000.0)
    for max_gap in (0.0, -5.0):
        resp = plan(client, 1000.0, 2000.0, max_gap)
        assert resp.status_code == 400
        assert "must be positive" in resp.json()["detail"]


def test_plan_rejects_inverted_blackout_interval(client):
    make_observation(client, 1000.0)
    make_observation(client, 2000.0)
    resp = plan(client, 1000.0, 2000.0, 500.0, blackout=[{"from": 1600.0, "to": 1500.0}])
    assert resp.status_code == 400
    assert "blackout" in resp.json()["detail"]


def test_plan_rejects_malformed_payload(client):
    assert client.post("/calibration-schedules/plan", json={"from": 1.0, "to": 2.0}).status_code == 422
    assert client.post(
        "/calibration-schedules/plan", json={"from": 1.0, "to": 2.0, "max_gap": "wide"}
    ).status_code == 422
    assert client.post(
        "/calibration-schedules/plan",
        json={"from": 1.0, "to": 2.0, "max_gap": 1.0, "blackout": [{"from": 1.0}]},
    ).status_code == 422


# ---------- 只读保证 ----------

def test_plan_is_read_only(client, clock):
    clock[0] = 100.0
    make_observation(client, 1000.0, readings=(9.0, 9.1, 8.9, 20.0))
    clock[0] = 200.0
    make_observation(client, 2000.0)
    clock[0] = 300.0
    client.put(
        "/observations/1",
        json={"timestamp": 1000.0, "reference": 10.0, "tolerance": 0.5,
              "readings": [9.5, 9.6, 9.4], "revision": 1},
    )

    before = (
        client.get("/observations").json(),
        client.get("/intervals").json(),
        client.get("/observations/1/revisions").json(),
    )
    # 成功与失败（409/400）的规划都不得改动任何现状数据
    assert plan(client, 1000.0, 2000.0, 250.0).status_code == 200
    assert plan(client, 500.0, 2000.0, 250.0).status_code == 409      # 缺少左边界锚点
    assert plan(client, 1000.0, 2000.0, 250.0,
                blackout=[{"from": 1000.0, "to": 2000.0}]).status_code == 409   # 约束无解
    assert plan(client, 1000.0, 2000.0, 0.0).status_code == 400
    after = (
        client.get("/observations").json(),
        client.get("/intervals").json(),
        client.get("/observations/1/revisions").json(),
    )
    assert after == before
