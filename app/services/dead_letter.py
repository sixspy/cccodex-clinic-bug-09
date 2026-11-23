"""死信回炉（甲路：物化包凭证 / cutover_book）。

收口契约：

* 落定（settle）把当刻**全部真实死信**（失败补采任务）抄成物化包落点行，
  写入 ``dead_letter_reflows`` / ``dead_letter_reflow_rows`` 两表；
* 落定同时把读轨钉到 ``cutover_book``、锁当刻指纹并置 ``reject_refresh``，
  此后同一批死信不得再回刷；
* 读侧（仪表盘 / 名录 / 详情 / 状态）一律只读，不得改写任何状态；
* 落定后再冒出新的死信，徽标由绿转红（面板与死信条数拧花），补采排队被挡，
  直到重新落定把缺行补进新的物化包；
* 两侧并发落定由 ``BEGIN IMMEDIATE`` 串行化，只有一侧成功，钉点不被踩掉。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import Config
from app.db import get_db
from app.services.errors import ServiceError

_SLUG = "dead_letter"
_OK = "回炉已亮"
_BAD = "死信仍静"
_TITLE = "死信回炉"
_OLD = "old_track"
_CUTOVER = "cutover_book"
_FATE = _CUTOVER


# --------------------------------------------------------------------------- #
# meta helpers
# --------------------------------------------------------------------------- #
def _meta_get(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (key, value),
    )


# --------------------------------------------------------------------------- #
# dead-letter bookkeeping
# --------------------------------------------------------------------------- #
def _dead_rows(conn) -> list[sqlite3.Row]:
    """真实死信 = 失败补采任务。"""
    return conn.execute(
        """
        SELECT j.id, j.job_code, j.row_id, j.attempt, j.message,
               r.subject_key, r.visit_code,
               b.batch_code, s.name AS site_name
        FROM backfill_jobs j
        LEFT JOIN visit_rows r ON r.id=j.row_id
        LEFT JOIN visit_batches b ON b.id=r.batch_id
        LEFT JOIN trial_sites s ON s.id=b.site_id
        WHERE j.status='失败'
        ORDER BY j.id
        """
    ).fetchall()


def _latest_settled(conn) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM dead_letter_reflows
        WHERE status='settled'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def _settled_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM dead_letter_reflows WHERE status='settled'"
    ).fetchone()["c"]


def _covered_job_ids(conn, reflow_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT job_id FROM dead_letter_reflow_rows WHERE reflow_id=?",
        (reflow_id,),
    ).fetchall()
    return {int(r["job_id"]) for r in rows}


def _landing_count(conn, reflow_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM dead_letter_reflow_rows WHERE reflow_id=?",
        (reflow_id,),
    ).fetchone()["c"]


def _fingerprint(dead: list[sqlite3.Row]) -> str:
    material = "\n".join(
        f"{int(r['id'])}|{r['job_code']}|{int(r['row_id'] or 0)}" for r in dead
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _insert_failed(conn, payload: dict, error: str, dead_count: int) -> int:
    cur = conn.execute(
        """
        INSERT INTO dead_letter_reflows(status, fate, note, dead_count, row_count, error)
        VALUES ('failed', ?, ?, ?, 0, ?)
        """,
        (_FATE, str(payload.get("note") or "")[:200], int(dead_count), error),
    )
    return int(cur.lastrowid)


# --------------------------------------------------------------------------- #
# state (pure read)
# --------------------------------------------------------------------------- #
def state(db_path: str | None = None) -> dict:
    """读侧快照：只读，不得写库。徽标由真实落点行与死信条数推导。"""
    with get_db(db_path) as conn:
        dead = _dead_rows(conn)
        dead_ids = {int(r["id"]) for r in dead}
        latest = _latest_settled(conn)
        settled_count = _settled_count(conn)

        stored_chip = _meta_get(conn, f"{_SLUG}_panel_chip", _BAD)
        ref = _meta_get(conn, f"{_SLUG}_active_read", _OLD)
        reject = _meta_get(conn, f"{_SLUG}_reject_refresh", "0") == "1"
        fp_meta = _meta_get(conn, f"{_SLUG}_cutover_fp", "")

        if latest is not None:
            covered = _covered_job_ids(conn, latest["id"])
            covered_count = len(dead_ids & covered)
            missing_count = len(dead_ids - covered)
            landing_rows = _landing_count(conn, latest["id"])
            fingerprint = latest["fingerprint"]
            last_id = int(latest["id"])
        else:
            covered_count = 0
            missing_count = len(dead_ids)
            landing_rows = 0
            fingerprint = ""
            last_id = 0

        pin_ok = (
            settled_count > 0
            and ref == _CUTOVER
            and reject
            and len(fp_meta) == 64
            and fp_meta == fingerprint
        )
        green = pin_ok and missing_count == 0 and landing_rows > 0
        chip = _OK if green else _BAD
        panel_pack_mismatch = stored_chip == _OK and not green

        return {
            "title": _TITLE,
            "label": "回炉钉定",
            "fate": _FATE,
            "chip": chip,
            "truth": _OK if green else _BAD,
            "stored_chip": stored_chip,
            "panel_pack_mismatch": panel_pack_mismatch,
            "active_read": ref,
            "reject_refresh": reject,
            "cutover_fp": fingerprint,
            "meta_cutover_fp": fp_meta,
            "settled_count": settled_count,
            "settled_id": last_id,
            "dead_letter_count": len(dead_ids),
            "covered_count": covered_count,
            "missing_count": missing_count,
            "landing_count": landing_rows,
            "aligned": green,
            "gate_open": gate_allows_pipeline(db_path),
        }


# --------------------------------------------------------------------------- #
# settle (the only writer)
# --------------------------------------------------------------------------- #
def _connect(db_path: str | None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else Config.DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def settle_reflow(payload: dict | None = None, db_path: str | None = None) -> dict:
    """落定死信回炉：写物化包落点行 + 钉死读轨。失败必须留下可读档案。"""
    payload = payload or {}
    note = str(payload.get("note") or "")[:200]
    conn = _connect(db_path)
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        dead = _dead_rows(conn)
        dead_ids = {int(r["id"]) for r in dead}
        latest = _latest_settled(conn)

        if latest is not None:
            covered = _covered_job_ids(conn, latest["id"])
            missing = dead_ids - covered
            if not missing:
                _insert_failed(conn, payload, "死信回炉已钉定，禁止回刷", len(dead_ids))
                conn.execute("COMMIT")
                committed = True
                raise ServiceError("死信回炉已钉定，禁止回刷", 409)
        else:
            missing = dead_ids

        if not dead:
            _insert_failed(conn, payload, "无死信可回炉：补采队列没有失败任务", 0)
            conn.execute("COMMIT")
            committed = True
            raise ServiceError("无死信可回炉：补采队列没有失败任务", 409)

        fp = _fingerprint(dead)
        cur = conn.execute(
            """
            INSERT INTO dead_letter_reflows(
              status, fate, note, fingerprint, dead_count, row_count, error, settled_at
            ) VALUES ('settled', ?, ?, ?, ?, ?, '', datetime('now'))
            """,
            (_FATE, note, fp, len(dead), len(dead)),
        )
        reflow_id = int(cur.lastrowid)
        for r in dead:
            conn.execute(
                """
                INSERT OR IGNORE INTO dead_letter_reflow_rows(
                  reflow_id, job_id, job_code, row_id,
                  subject_key, visit_code, batch_code, site_name, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reflow_id,
                    int(r["id"]),
                    r["job_code"] or "",
                    int(r["row_id"] or 0),
                    r["subject_key"] or "",
                    r["visit_code"] or "",
                    r["batch_code"] or "",
                    r["site_name"] or "",
                    r["message"] or "",
                ),
            )
        _meta_set(conn, f"{_SLUG}_panel_chip", _OK)
        _meta_set(conn, f"{_SLUG}_truth", _OK)
        _meta_set(conn, f"{_SLUG}_active_read", _CUTOVER)
        _meta_set(conn, f"{_SLUG}_reject_refresh", "1")
        _meta_set(conn, f"{_SLUG}_cutover_fp", fp)
        conn.execute("COMMIT")
        committed = True
    except ServiceError:
        raise
    except Exception:
        if not committed:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()

    result = state(db_path)
    return {
        "ok": True,
        "id": reflow_id,
        "status": "settled",
        "fate": _FATE,
        "note": note,
        "fingerprint": fp,
        "dead_count": len(dead),
        "row_count": len(dead),
        "chip": result["chip"],
        "truth": result["truth"],
        "active_read": result["active_read"],
        "reject_refresh": result["reject_refresh"],
        "missing_count": result["missing_count"],
        "message": f"{_TITLE}已落定：{len(dead)} 条死信入物化包",
    }


def settle_display_only(db_path: str | None = None) -> dict:
    """显式标记的半修：只亮面板，不写落点行。仅供对照，永远不算已落定。"""
    with get_db(db_path) as conn:
        _meta_set(conn, f"{_SLUG}_panel_chip", _OK)
        _meta_set(conn, f"{_SLUG}_display_only", "1")
    result = state(db_path)
    result["half_fixed"] = True
    result["rejected"] = True
    result["reason"] = "只改展示未写死信回炉落点行，验收驳回"
    return result


# --------------------------------------------------------------------------- #
# catalog / detail (pure read)
# --------------------------------------------------------------------------- #
def list_reflows(db_path: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(200, int(limit or 50)))
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, status, fate, note, fingerprint, dead_count, row_count,
                   error, created_at, settled_at
            FROM dead_letter_reflows
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_reflow(reflow_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM dead_letter_reflows WHERE id=?",
            (int(reflow_id),),
        ).fetchone()
        if not row:
            raise ServiceError("死信回炉记录不存在", 404)
        rows = conn.execute(
            """
            SELECT * FROM dead_letter_reflow_rows
            WHERE reflow_id=?
            ORDER BY id
            """,
            (int(reflow_id),),
        ).fetchall()
        data = dict(row)
        data["rows"] = [dict(r) for r in rows]
        data["row_count_actual"] = len(data["rows"])
        data["fate"] = data.get("fate") or _FATE
        return data


# --------------------------------------------------------------------------- #
# gate
# --------------------------------------------------------------------------- #
def gate_allows_pipeline(db_path: str | None = None) -> bool:
    """落定后若死信条数又拧花，挡住补采/抓取/质控，逼重新落定。"""
    with get_db(db_path) as conn:
        dead_ids = {int(r["id"]) for r in _dead_rows(conn)}
        latest = _latest_settled(conn)
        if latest is None:
            return True
        covered = _covered_job_ids(conn, latest["id"])
        return not (dead_ids - covered)


def require_pack_or_raise(db_path: str | None = None) -> None:
    if not gate_allows_pipeline(db_path):
        raise ServiceError(f"{_TITLE}读轨未对齐：存在未入物化包的死信，禁止放行", 409)


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #
def dashboard(db_path: str | None = None) -> dict:
    st = state(db_path)
    latest = st["settled_id"]
    rows: list[dict] = []
    if latest:
        detail = get_reflow(latest, db_path)
        rows = detail["rows"]
    return {
        **st,
        "ok_chip": _OK,
        "bad_chip": _BAD,
        "rows": rows,
        "reflows": list_reflows(db_path),
        "read_tracks": {"old_track": _OLD, "cutover_book": _CUTOVER},
        "gate_allows": st["gate_open"],
    }


# --------------------------------------------------------------------------- #
# seed / legacy normalization
# --------------------------------------------------------------------------- #
def seed_skew(db_path: str | None = None) -> None:
    """初始态：面板必须为死信仍静，读轨停在旧轨，未落定。"""
    with get_db(db_path) as conn:
        _meta_set(conn, f"{_SLUG}_panel_chip", _BAD)
        _meta_set(conn, f"{_SLUG}_truth", _BAD)
        _meta_set(conn, f"{_SLUG}_active_read", _OLD)
        _meta_set(conn, f"{_SLUG}_cutover_fp", "")
        _meta_set(conn, f"{_SLUG}_reject_refresh", "0")
        _meta_set(conn, f"{_SLUG}_mode", "cutover_book")


def normalize_legacy(db_path: str | None = None) -> None:
    """旧库若只有假绿面板而没有真实落点行，启动时纠正为红。"""
    with get_db(db_path) as conn:
        latest = _latest_settled(conn)
        if latest is not None:
            return
        if _meta_get(conn, f"{_SLUG}_panel_chip", _BAD) == _OK:
            _meta_set(conn, f"{_SLUG}_panel_chip", _BAD)
        if _meta_get(conn, f"{_SLUG}_truth", _BAD) == _OK:
            _meta_set(conn, f"{_SLUG}_truth", _BAD)
        if _meta_get(conn, f"{_SLUG}_active_read", _OLD) != _OLD:
            _meta_set(conn, f"{_SLUG}_active_read", _OLD)
        _meta_set(conn, f"{_SLUG}_reject_refresh", "0")
        _meta_set(conn, f"{_SLUG}_cutover_fp", "")
