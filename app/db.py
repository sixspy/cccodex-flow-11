import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from app.config import Config

SEEDS = [
    ("目录扫描流", "scan_flow", "待抽取", "https://mock.local/scan"),
    ("列表翻页流", "page_flow", "待抽取", "https://mock.local/list"),
    ("附件下载流", "file_flow", "待抽取", "file://drops/pack"),
    ("变更监听流", "watch_flow", "待抽取", "https://mock.local/watch"),
]

# 双源合并：主份编排 + 次份编排，先各自抽取成功，再按合并键拼装并裁定冲突
MERGE_SEEDS = [
    ("目录翻页双源合并", 1, 2, "batch,row_id", "整行败绩"),
    ("附件监听双源合并", 3, 4, "batch,row_id", "保主旁记"),
]

POLICY_WHOLE_ROW = "整行败绩"   # 整条冲突行丢入冲突匣
POLICY_KEEP_PRIMARY = "保主旁记"  # 保留主份值，旁记次份差
CONFLICT_POLICIES = (POLICY_WHOLE_ROW, POLICY_KEEP_PRIMARY)
COMPARE_EXCLUDE = {"origin"}


def _err(detail, status=409, code="flow_error"):
    return {"error": True, "detail": detail, "status": status, "code": code}


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path=None):
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db(db_path=None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None):
    with get_db(db_path) as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS flows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              code TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              source_uri TEXT NOT NULL,
              extracted INTEGER NOT NULL DEFAULT 0,
              landed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS flow_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              flow_id INTEGER NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_records_flow ON flow_records(flow_id);
            CREATE TABLE IF NOT EXISTS merges (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              primary_flow_id INTEGER NOT NULL,
              secondary_flow_id INTEGER NOT NULL,
              key_fields TEXT NOT NULL,
              conflict_policy TEXT NOT NULL,
              policy_date TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT '待合并',
              primary_count INTEGER NOT NULL DEFAULT 0,
              secondary_count INTEGER NOT NULL DEFAULT 0,
              conflict_count INTEGER NOT NULL DEFAULT 0,
              merged_count INTEGER NOT NULL DEFAULT 0,
              landed_count INTEGER NOT NULL DEFAULT 0,
              lost_to TEXT,
              lost_at TEXT,
              merged_at TEXT,
              landed_at TEXT,
              created_at TEXT NOT NULL,
              UNIQUE(primary_flow_id, secondary_flow_id)
            );
            CREATE TABLE IF NOT EXISTS merge_rows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              merge_id INTEGER NOT NULL,
              row_key TEXT NOT NULL,
              payload TEXT NOT NULL,
              source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS merge_segments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              merge_id INTEGER NOT NULL,
              seg_no INTEGER NOT NULL,
              key_from TEXT NOT NULL,
              key_to TEXT NOT NULL,
              row_count INTEGER NOT NULL
            );
        ''')
        if conn.execute("SELECT COUNT(*) c FROM flows").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO flows (title, code, status, source_uri) VALUES (?,?,?,?)",
                SEEDS,
            )
        if conn.execute("SELECT COUNT(*) c FROM merges").fetchone()["c"] == 0:
            now, today = _now(), _today()
            for title, pid, sid, key_fields, policy in MERGE_SEEDS:
                conn.execute(
                    "INSERT INTO merges (title, primary_flow_id, secondary_flow_id,"
                    " key_fields, conflict_policy, policy_date, status, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (title, pid, sid, key_fields, policy, today, "待合并", now),
                )


def list_flows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, landed FROM flows ORDER BY id"
    )]


def get_flow(conn, flow_id: int):
    row = conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, landed FROM flows WHERE id=?",
        (flow_id,),
    ).fetchone()
    return dict(row) if row else None


def _flow_extracted(item) -> bool:
    return bool(item and item["extracted"] and item["status"] != "抽取失败")


def _seed_records(conn, flow_id: int, n: int):
    """确定性模拟抽取：同一合并键在双份间大部分一致，次份每 4 行错一个字段形成抵触行。"""
    conn.execute("DELETE FROM flow_records WHERE flow_id=?", (flow_id,))
    rows = []
    for i in range(1, n + 1):
        payload = {
            "batch": "B01",
            "row_id": f"{i:04d}",
            "title": f"记录{i}",
            "value": str((i * 3) % 10),
            "stamp": "ok",
            "origin": f"flow-{flow_id}",
        }
        if flow_id % 2 == 0 and i % 4 == 0:
            payload["value"] = f"{payload['value']}-次"
        rows.append((flow_id, json.dumps(payload, ensure_ascii=False)))
    conn.executemany("INSERT INTO flow_records (flow_id, payload) VALUES (?,?)", rows)


def run_extract(conn, flow_id: int):
    item = get_flow(conn, flow_id)
    if not item:
        return None
    if item["status"] in ("待抽取", "抽取失败"):
        n = 15 + (flow_id * 4) % 10
        conn.execute(
            "UPDATE flows SET status=?, extracted=?, landed=0 WHERE id=?",
            ("已抽取待落库", n, flow_id),
        )
        _seed_records(conn, flow_id, n)
    return get_flow(conn, flow_id)


def run_land(conn, flow_id: int):
    item = get_flow(conn, flow_id)
    if not item:
        return None
    if item["status"] == "已抽取待落库":
        conn.execute(
            "UPDATE flows SET status=?, landed=? WHERE id=?",
            ("已落库", item["extracted"], flow_id),
        )
    return get_flow(conn, flow_id)


# ---------------------------------------------------------------- 双源合并

def _key_fields(merge) -> list[str]:
    return [f.strip() for f in (merge.get("key_fields") or "").split(",") if f.strip()]


def _compose_key(payload, fields) -> str:
    if not fields:
        return ""
    parts = [str(payload.get(f, "")).strip() for f in fields]
    if any(not p for p in parts):
        return ""
    return "|".join(parts)


def _records(conn, flow_id: int):
    return [json.loads(r["payload"]) for r in conn.execute(
        "SELECT payload FROM flow_records WHERE flow_id=? ORDER BY id", (flow_id,)
    )]


def _diff(primary, secondary):
    out = []
    for field in sorted(set(primary) | set(secondary)):
        if field in COMPARE_EXCLUDE:
            continue
        if primary.get(field) != secondary.get(field):
            out.append({
                "field": field,
                "primary": primary.get(field),
                "secondary": secondary.get(field),
            })
    return out


def _analyze(conn, merge):
    """合并键拼装 + 冲突裁定的统一预看/执行依据。"""
    fields = _key_fields(merge)
    primary_flow = get_flow(conn, merge["primary_flow_id"])
    secondary_flow = get_flow(conn, merge["secondary_flow_id"])
    precs = _records(conn, merge["primary_flow_id"])
    srecs = _records(conn, merge["secondary_flow_id"])

    pmap, smap = {}, {}
    key_missing = 0
    for p in precs:
        key = _compose_key(p, fields)
        if not key:
            key_missing += 1
            continue
        pmap.setdefault(key, p)
    for s in srecs:
        key = _compose_key(s, fields)
        if not key:
            key_missing += 1
            continue
        smap.setdefault(key, s)

    policy = merge["conflict_policy"]
    all_keys = sorted(set(pmap) | set(smap))
    conflicts, matched = [], 0
    for key in all_keys:
        if key in pmap and key in smap:
            matched += 1
            diffs = _diff(pmap[key], smap[key])
            if diffs:
                if policy == POLICY_WHOLE_ROW:
                    outcome = "冲突匣"
                    note = "整行败绩丢入冲突匣：主份与次份整行均不进合并结果"
                else:
                    outcome = "保留主份"
                    changes = "／".join(
                        f"{d['field']} {d['primary']} → {d['secondary']}" for d in diffs
                    )
                    note = f"保留主份值，旁记次份差：{changes}"
                conflicts.append({
                    "key": key,
                    "primary_payload": pmap[key],
                    "secondary_payload": smap[key],
                    "diff": diffs,
                    "disposition": policy,
                    "outcome": outcome,
                    "note": note,
                })

    primary_only = sum(1 for k in all_keys if k in pmap and k not in smap)
    secondary_only = sum(1 for k in all_keys if k in smap and k not in pmap)
    conflict_keys = {c["key"] for c in conflicts}
    if policy == POLICY_WHOLE_ROW:
        merged_keys = [k for k in all_keys if k not in conflict_keys]
    else:
        merged_keys = list(all_keys)

    ready_reason = "双份齐件，可合并"
    if not _flow_extracted(primary_flow) or not _flow_extracted(secondary_flow):
        ready_reason = "一边未齐，不能合并"
    elif not fields or key_missing:
        ready_reason = "合并键空缺，不准开跑"
    ready = ready_reason.startswith("双份")

    key_preview = [
        {
            "key": k,
            "primary": k in pmap,
            "secondary": k in smap,
            "conflict": k in conflict_keys,
        }
        for k in all_keys[:6]
    ]
    return {
        "fields": fields,
        "key_rule": "|".join(fields),
        "key_missing": key_missing,
        "primary_flow": primary_flow,
        "secondary_flow": secondary_flow,
        "pmap": pmap,
        "smap": smap,
        "all_keys": all_keys,
        "conflicts": conflicts,
        "merged_keys": merged_keys,
        "counts": {
            "primary": len(precs),
            "secondary": len(srecs),
            "matched": matched,
            "primary_only": primary_only,
            "secondary_only": secondary_only,
            "conflicts": len(conflicts),
            "merged": len(merged_keys),
        },
        "key_preview": key_preview,
        "ready": ready,
        "ready_reason": ready_reason,
    }


def list_merges(conn):
    items = []
    for row in conn.execute("SELECT * FROM merges ORDER BY id").fetchall():
        merge = dict(row)
        analysis = _analyze(conn, merge)
        items.append({
            "id": merge["id"],
            "title": merge["title"],
            "primary_flow_id": merge["primary_flow_id"],
            "secondary_flow_id": merge["secondary_flow_id"],
            "key_fields": merge["key_fields"],
            "key_rule": analysis["key_rule"],
            "conflict_policy": merge["conflict_policy"],
            "policy_date": merge["policy_date"],
            "status": merge["status"],
            "primary_count": analysis["counts"]["primary"],
            "secondary_count": analysis["counts"]["secondary"],
            "conflict_count": analysis["counts"]["conflicts"],
            "merged_count": analysis["counts"]["merged"],
            "ready": analysis["ready"],
            "ready_reason": analysis["ready_reason"],
            "lost_to": merge["lost_to"],
            "lost_at": merge["lost_at"],
        })
    return items


def get_merge(conn, merge_id: int):
    row = conn.execute("SELECT * FROM merges WHERE id=?", (merge_id,)).fetchone()
    if not row:
        return None
    merge = dict(row)
    analysis = _analyze(conn, merge)
    segments = [dict(r) for r in conn.execute(
        "SELECT seg_no, key_from, key_to, row_count FROM merge_segments"
        " WHERE merge_id=? ORDER BY seg_no", (merge_id,)
    )]
    locked_today = merge["policy_date"] == _today()
    return {
        "id": merge["id"],
        "title": merge["title"],
        "primary_flow_id": merge["primary_flow_id"],
        "secondary_flow_id": merge["secondary_flow_id"],
        "status": merge["status"],
        "key_fields": analysis["fields"],
        "key_rule": analysis["key_rule"],
        "key_preview": analysis["key_preview"],
        "policy_date": merge["policy_date"],
        "policy_locked_today": locked_today,
        "strategy": {
            "name": merge["conflict_policy"],
            "pinned_on": merge["policy_date"],
            "locked_today": locked_today,
            "choices": list(CONFLICT_POLICIES),
        },
        "conflict_policy": merge["conflict_policy"],
        "primary": analysis["primary_flow"],
        "secondary": analysis["secondary_flow"],
        "counts": analysis["counts"],
        "conflicts": analysis["conflicts"],
        "conflict_box": [c for c in analysis["conflicts"] if c["outcome"] == "冲突匣"],
        "ready": analysis["ready"],
        "ready_reason": analysis["ready_reason"],
        "merged_count": merge["merged_count"],
        "landed_count": merge["landed_count"],
        "merged_at": merge["merged_at"],
        "landed_at": merge["landed_at"],
        "lost_to": merge["lost_to"],
        "lost_at": merge["lost_at"],
        "loss": ({"by": merge["lost_to"], "at": merge["lost_at"]}
                 if merge["lost_to"] else None),
        "segments": segments,
        "segment_count": len(segments),
    }


def run_merge(conn, merge_id: int, disposition=None, actor="admin"):
    row = conn.execute("SELECT * FROM merges WHERE id=?", (merge_id,)).fetchone()
    if not row:
        return None
    merge = dict(row)

    fields = _key_fields(merge)
    if not fields:
        return _err("合并键空缺，不准开跑", 409, "merge_key_missing")
    if disposition is not None and str(disposition).strip():
        if str(disposition).strip() != merge["conflict_policy"]:
            return _err("冲突策略漂了：本次处置与钉死策略不符", 409, "strategy_drift")

    primary = get_flow(conn, merge["primary_flow_id"])
    secondary = get_flow(conn, merge["secondary_flow_id"])
    if not _flow_extracted(primary) or not _flow_extracted(secondary):
        return _err("一边未齐，不能合并", 409, "source_incomplete")

    analysis = _analyze(conn, merge)
    if analysis["key_missing"]:
        return _err("合并键空缺，不准开跑", 409, "merge_key_missing")

    # 争抢合并：status 待合并 → 合并中 的原子抢占，只有先手能拿到
    claimed = conn.execute(
        "UPDATE merges SET status='合并中' WHERE id=? AND status='待合并'", (merge_id,)
    )
    if claimed.rowcount == 0:
        conn.execute(
            "UPDATE merges SET lost_to=COALESCE(lost_to, ?), lost_at=COALESCE(lost_at, ?)"
            " WHERE id=?",
            (actor, _now(), merge_id),
        )
        return _err("合并争用败绩：先手已夺取合并", 409, "merge_contended")

    # 按钉死策略裁定冲突并生成合并结果行
    conflict_keys = {c["key"] for c in analysis["conflicts"]}
    merged_rows = []
    for key in analysis["all_keys"]:
        if key in conflict_keys and merge["conflict_policy"] == POLICY_WHOLE_ROW:
            continue
        if key in analysis["pmap"]:
            merged_rows.append((merge_id, key, analysis["pmap"][key], "主份"))
        elif key in analysis["smap"]:
            merged_rows.append((merge_id, key, analysis["smap"][key], "次份"))

    conn.execute("DELETE FROM merge_rows WHERE merge_id=?", (merge_id,))
    conn.execute("DELETE FROM merge_segments WHERE merge_id=?", (merge_id,))
    conn.executemany(
        "INSERT INTO merge_rows (merge_id, row_key, payload, source) VALUES (?,?,?,?)",
        [(mid, key, json.dumps(payload, ensure_ascii=False), src)
         for mid, key, payload, src in merged_rows],
    )
    conn.execute(
        "UPDATE merges SET status='已合并', primary_count=?, secondary_count=?,"
        " conflict_count=?, merged_count=?, merged_at=?, landed_count=0 WHERE id=?",
        (analysis["counts"]["primary"], analysis["counts"]["secondary"],
         analysis["counts"]["conflicts"], len(merged_rows), _now(), merge_id),
    )
    return {"ok": True, "item": get_merge(conn, merge_id)}


def run_merge_segment(conn, merge_id: int, actor="admin"):
    row = conn.execute("SELECT * FROM merges WHERE id=?", (merge_id,)).fetchone()
    if not row:
        return None
    merge = dict(row)
    if merge["status"] not in ("已合并", "已入库"):
        return _err("合并未收尾，不准抽段", 409, "merge_not_finished")

    rows = [dict(r) for r in conn.execute(
        "SELECT row_key FROM merge_rows WHERE merge_id=? ORDER BY row_key", (merge_id,)
    )]
    conn.execute("DELETE FROM merge_segments WHERE merge_id=?", (merge_id,))
    segments = []
    for seg_no, start in enumerate(range(0, len(rows), 5), start=1):
        chunk = rows[start:start + 5]
        segments.append((merge_id, seg_no, chunk[0]["row_key"],
                         chunk[-1]["row_key"], len(chunk)))
    conn.executemany(
        "INSERT INTO merge_segments (merge_id, seg_no, key_from, key_to, row_count)"
        " VALUES (?,?,?,?,?)", segments
    )
    return {"ok": True, "item": get_merge(conn, merge_id)}


def run_merge_land(conn, merge_id: int):
    row = conn.execute("SELECT * FROM merges WHERE id=?", (merge_id,)).fetchone()
    if not row:
        return None
    merge = dict(row)
    if merge["status"] == "已入库":
        return {"ok": True, "item": get_merge(conn, merge_id)}
    if merge["status"] != "已合并":
        return _err("合并未收尾，不准入库", 409, "merge_not_finished")
    conn.execute(
        "UPDATE merges SET status='已入库', landed_count=?, landed_at=? WHERE id=?",
        (merge["merged_count"], _now(), merge_id),
    )
    return {"ok": True, "item": get_merge(conn, merge_id)}


def update_merge_policy(conn, merge_id: int, policy):
    row = conn.execute("SELECT * FROM merges WHERE id=?", (merge_id,)).fetchone()
    if not row:
        return None
    merge = dict(row)
    policy = str(policy or "").strip()
    if policy not in CONFLICT_POLICIES:
        return _err("未知的冲突策略", 400, "policy_unknown")
    if merge["policy_date"] == _today():
        return _err("冲突策略当日已钉死，不得更换", 409, "policy_locked")
    conn.execute(
        "UPDATE merges SET conflict_policy=?, policy_date=? WHERE id=?",
        (policy, _today(), merge_id),
    )
    return {"ok": True, "item": get_merge(conn, merge_id)}
