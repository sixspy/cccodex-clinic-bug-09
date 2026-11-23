"""死信回炉假绿回归：甲路（物化包凭证 / cutover_book）。

这些用例先于修复落地，初始应全部失败，锁定以下契约：
- 未落定时徽标不得假绿，读轨不得假装已切；
- 落定必须写入真实的「回炉落点行」，并把读轨钉到物化包且拒回刷；
- 无死信 / 重复落定必须给出可读失败原因；
- 落定与死信条数不一致时补采排队必须被挡住；
- 并发两侧落定只能有一侧成功，不得把钉点踩掉；
- 读接口（仪表盘 / 名录 / 详情）不得改写任何状态。
"""
from __future__ import annotations

import threading

import pytest

from app.db import get_db
from app.services import backfill, dead_letter, visits
from app.services.errors import ServiceError

OK = "回炉已亮"
BAD = "死信仍静"


def _add_failed_job(db_path: str, row_id: int, code: str) -> int:
    with get_db(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO backfill_jobs(job_code, row_id, status, attempt, message)
            VALUES (?, ?, '失败', 1, '测试死信')
            """,
            (code, row_id),
        )
        return int(cur.lastrowid)


def _any_gap_row_id(db_path: str) -> int:
    rows = visits.list_rows(db_path=db_path, quality="缺字段")
    rows = [r for r in rows if r["batch_code"] and r["status"] in ("待检", "待补采")]
    assert rows, "测试库缺少缺口行"
    return int(rows[0]["id"])


def _table_count(db_path: str, table: str) -> int:
    with get_db(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def test_seed_is_not_false_green(db_path):
    dash = dead_letter.dashboard(db_path)
    assert dash["chip"] == BAD
    assert dash["truth"] == BAD
    assert dash["active_read"] == "old_track"
    assert dash["aligned"] is False
    assert dash["settled_count"] == 0
    # 种子里的失败补采任务就是真实死信
    assert dash["dead_letter_count"] >= 1


def test_settle_writes_durable_landing_rows_and_pins_read_track(db_path):
    before = dead_letter.state(db_path)
    assert before["dead_letter_count"] >= 1

    result = dead_letter.settle_reflow({"note": "落定回炉"}, db_path)

    assert result["status"] == "settled"
    assert result["fate"] == "cutover_book"
    assert result["row_count"] == result["dead_count"] >= 1

    # 真实落点行必须落库，而不是只亮芯片
    with get_db(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM dead_letter_reflow_rows WHERE reflow_id=?",
            (result["id"],),
        ).fetchone()["c"]
    assert n == result["row_count"]

    st = dead_letter.state(db_path)
    assert st["active_read"] == "cutover_book"
    assert st["reject_refresh"] is True
    assert len(st["cutover_fp"]) == 64
    assert st["chip"] == OK
    assert st["aligned"] is True
    assert st["missing_count"] == 0


def test_settle_without_dead_letters_fails_with_reason(db_path):
    with get_db(db_path) as conn:
        conn.execute("UPDATE backfill_jobs SET status='排队' WHERE status='失败'")
    assert dead_letter.state(db_path)["dead_letter_count"] == 0

    with pytest.raises(ServiceError) as exc:
        dead_letter.settle_reflow({}, db_path)
    assert "死信" in str(exc.value)

    # 失败也要留下可读档案
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT status, error FROM dead_letter_reflows ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"]


def test_second_settle_is_rejected_as_refresh(db_path):
    dead_letter.settle_reflow({"note": "首次"}, db_path)
    with pytest.raises(ServiceError) as exc:
        dead_letter.settle_reflow({"note": "回刷"}, db_path)
    assert "回刷" in str(exc.value) or "钉定" in str(exc.value)

    with get_db(db_path) as conn:
        settled = conn.execute(
            "SELECT COUNT(*) AS c FROM dead_letter_reflows WHERE status='settled'"
        ).fetchone()["c"]
    assert settled == 1


def test_new_dead_letter_twists_chip_and_blocks_backfill(db_path):
    dead_letter.settle_reflow({}, db_path)
    assert dead_letter.gate_allows_pipeline(db_path) is True

    gap = _any_gap_row_id(db_path)
    _add_failed_job(db_path, gap, "BF-TEST-NEW")

    st = dead_letter.state(db_path)
    assert st["dead_letter_count"] == st["covered_count"] + st["missing_count"]
    assert st["missing_count"] >= 1
    assert st["chip"] == BAD
    assert st["aligned"] is False
    assert dead_letter.gate_allows_pipeline(db_path) is False

    with pytest.raises(ServiceError):
        backfill.enqueue_from_row(gap, db_path=db_path)


def test_concurrent_settle_has_exactly_one_winner(db_path):
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=5)
            dead_letter.settle_reflow({"note": "争用"}, db_path)
            with lock:
                outcomes.append("ok")
        except ServiceError:
            with lock:
                outcomes.append("rejected")
        except Exception as exc:  # noqa: BLE001
            with lock:
                outcomes.append(f"error:{exc}")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert outcomes.count("ok") == 1, outcomes
    assert outcomes.count("rejected") == 1, outcomes

    with get_db(db_path) as conn:
        settled = conn.execute(
            "SELECT COUNT(*) AS c FROM dead_letter_reflows WHERE status='settled'"
        ).fetchone()["c"]
    assert settled == 1


def test_read_paths_are_pure(db_path):
    dead_letter.settle_reflow({}, db_path)
    snapshots = (
        _table_count(db_path, "dead_letter_reflows"),
        _table_count(db_path, "dead_letter_reflow_rows"),
    )
    with get_db(db_path) as conn:
        meta = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'dead_letter_%'"
            ).fetchall()
        }

    for _ in range(3):
        dead_letter.dashboard(db_path)
        dead_letter.state(db_path)
        dead_letter.list_reflows(db_path)
        for item in dead_letter.list_reflows(db_path):
            dead_letter.get_reflow(item["id"], db_path)

    assert (
        _table_count(db_path, "dead_letter_reflows"),
        _table_count(db_path, "dead_letter_reflow_rows"),
    ) == snapshots
    with get_db(db_path) as conn:
        meta_after = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'dead_letter_%'"
            ).fetchall()
        }
    assert meta_after == meta


def test_enqueue_does_not_fake_settle(db_path):
    gap = _any_gap_row_id(db_path)
    backfill.enqueue_from_row(gap, db_path=db_path)
    assert _table_count(db_path, "dead_letter_reflows") == 0
    assert dead_letter.state(db_path)["chip"] == BAD


def test_catalog_and_detail_expose_landing_rows(db_path):
    result = dead_letter.settle_reflow({"note": "名录"}, db_path)
    items = dead_letter.list_reflows(db_path)
    assert any(i["id"] == result["id"] for i in items)
    detail = dead_letter.get_reflow(result["id"], db_path)
    assert len(detail["rows"]) == result["row_count"]
    assert detail["fate"] == "cutover_book"
    assert detail["status"] == "settled"
