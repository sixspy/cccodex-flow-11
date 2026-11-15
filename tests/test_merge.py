from contextlib import contextmanager

import pytest

from app.db import _analyze, get_db, run_extract


@pytest.fixture()
def conn(db):
    @contextmanager
    def _open():
        with get_db(db) as c:
            yield c
    return _open


def _row(open_db, mid=1):
    with open_db() as c:
        return dict(c.execute("SELECT * FROM merges WHERE id=?", (mid,)).fetchone())


def _analysis(open_db, mid=1):
    with open_db() as c:
        return _analyze(c, _row(open_db, mid))


def _extract(open_db, *flow_ids):
    with open_db() as c:
        for f in flow_ids:
            run_extract(c, f)


def _merge(client, headers, mid, disposition=None):
    body = {"disposition": disposition} if disposition else {}
    return client.post(f"/api/merges/{mid}/merge", json=body, headers=headers)


def test_merge_requires_both_sides_extracted(client, headers):
    _req, resp = client.get("/api/merges/1", headers=headers)
    assert resp.status == 200
    assert resp.json["ready"] is False
    assert resp.json["ready_reason"] == "一边未齐，不能合并"
    _req, resp = _merge(client, headers, 1)
    assert resp.status == 409
    assert resp.json["code"] == "source_incomplete"


def test_one_side_extracted_still_blocked(conn, client, headers):
    _extract(conn, 1)
    _req, resp = _merge(client, headers, 1, disposition="整行败绩")
    assert resp.status == 409
    assert resp.json["code"] == "source_incomplete"


def test_merge_key_missing_blocks(conn, client, headers):
    _extract(conn, 1, 2)
    with conn() as c:
        c.execute("UPDATE merges SET key_fields='' WHERE id=1")
    _req, resp = _merge(client, headers, 1, disposition="整行败绩")
    assert resp.status == 409
    assert resp.json["code"] == "merge_key_missing"


def test_policy_drift_blocks_merge(conn, client, headers):
    _extract(conn, 1, 2)
    _req, resp = _merge(client, headers, 1, disposition="保主旁记")
    assert resp.status == 409
    assert resp.json["code"] == "strategy_drift"
    assert "漂" in resp.json["detail"]


def test_policy_locked_same_day(conn, client, headers):
    _extract(conn, 1, 2)
    _req, resp = client.patch("/api/merges/1/policy", json={"conflict_policy": "保主旁记"}, headers=headers)
    assert resp.status == 409
    assert resp.json["code"] == "policy_locked"
    with conn() as c:
        c.execute("UPDATE merges SET policy_date='2026-11-14' WHERE id=1")
    _req, resp = client.patch("/api/merges/1/policy", json={"conflict_policy": "保主旁记"}, headers=headers)
    assert resp.status == 200
    assert resp.json["conflict_policy"] == "保主旁记"


def test_unknown_policy_rejected(client, headers):
    _req, resp = client.patch("/api/merges/1/policy", json={"conflict_policy": "随便"}, headers=headers)
    assert resp.status == 400
    assert resp.json["code"] == "policy_unknown"


def test_whole_row_policy_drops_conflict_rows(conn, client, headers):
    _extract(conn, 1, 2)
    analysis = _analysis(conn, 1)
    assert analysis["conflicts"], "seed 应产生抵触行"
    _req, resp = _merge(client, headers, 1, disposition="整行败绩")
    assert resp.status == 200, resp.text
    item = resp.json
    assert item["status"] == "已合并"
    assert item["counts"]["conflicts"] == len(analysis["conflicts"])
    assert item["merged_count"] == len(analysis["all_keys"]) - len(analysis["conflicts"])
    assert all(c["outcome"] == "冲突匣" for c in item["conflict_box"])
    assert len(item["conflict_box"]) == item["counts"]["conflicts"]
    with conn() as c:
        n = c.execute("SELECT COUNT(*) c FROM merge_rows WHERE merge_id=1").fetchone()["c"]
    assert n == item["merged_count"]


def test_keep_primary_policy_records_secondary_diff(conn, client, headers):
    _extract(conn, 3, 4)
    analysis = _analysis(conn, 2)
    assert analysis["conflicts"]
    _req, resp = _merge(client, headers, 2, disposition="保主旁记")
    assert resp.status == 200, resp.text
    item = resp.json
    assert item["merged_count"] == item["counts"]["merged"] == len(analysis["all_keys"])
    assert item["conflict_box"] == []
    assert all(c["outcome"] == "保留主份" and "旁记次份差" in c["note"] for c in item["conflicts"])


def test_contention_only_first_wins(conn, client, headers):
    _extract(conn, 1, 2)
    _req, first = _merge(client, headers, 1, disposition="整行败绩")
    assert first.status == 200
    _req, second = _merge(client, headers, 1, disposition="整行败绩")
    assert second.status == 409
    assert second.json["code"] == "merge_contended"
    assert "争用败绩" in second.json["detail"]
    _req, detail = client.get("/api/merges/1", headers=headers)
    assert detail.json["lost_to"] == "admin"
    assert detail.json["lost_at"]
    assert detail.json["status"] == "已合并"
    assert detail.json["loss"]["by"] == "admin"


def test_land_requires_merge_and_segment_requires_finished(conn, client, headers):
    _extract(conn, 1, 2)
    _req, resp = client.post("/api/merges/1/land", headers=headers)
    assert resp.status == 409 and resp.json["code"] == "merge_not_finished"
    _req, resp = client.post("/api/merges/1/segment", headers=headers)
    assert resp.status == 409
    _req, resp = _merge(client, headers, 1, disposition="整行败绩")
    assert resp.status == 200
    _req, resp = client.post("/api/merges/1/segment", headers=headers)
    assert resp.status == 200
    assert resp.json["segment_count"] >= 1
    _req, resp = client.post("/api/merges/1/land", headers=headers)
    assert resp.status == 200
    assert resp.json["status"] == "已入库"
    assert resp.json["landed_count"] == resp.json["merged_count"]


def test_list_shows_counts_and_policy(client, headers, conn):
    _extract(conn, 1, 2, 3, 4)
    _req, resp = client.get("/api/merges", headers=headers)
    assert resp.status == 200
    items = resp.json["items"]
    assert len(items) == 2
    for it in items:
        assert it["primary_count"] > 0
        assert it["secondary_count"] > 0
        assert it["conflict_count"] > 0
        assert it["conflict_policy"] in ("整行败绩", "保主旁记")


def test_list_always_exposes_count_fields(client, headers):
    _req, resp = client.get("/api/merges", headers=headers)
    for it in resp.json["items"]:
        for field in ("primary_count", "secondary_count", "conflict_count", "conflict_policy"):
            assert field in it


def test_detail_exposes_conflict_rows_and_roles(client, headers, conn):
    _extract(conn, 1, 2)
    _req, resp = client.get("/api/merges/1", headers=headers)
    assert resp.status == 200
    body = resp.json
    assert body["primary"]["id"] == 1 and body["secondary"]["id"] == 2
    assert body["conflicts"][0]["diff"]
    assert body["conflicts"][0]["primary_payload"]["origin"] == "flow-1"
    assert body["strategy"]["pinned_on"]
