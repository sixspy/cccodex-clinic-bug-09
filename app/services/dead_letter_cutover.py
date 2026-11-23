"""死信回炉切流账册（甲路收口面）。

本模块只做甲路（``cutover_book``）的读写入口，全部落到
:mod:`app.services.dead_letter` 的物化包落点行与钉定状态，不再另开一侧。
"""
from __future__ import annotations

from app.services.dead_letter import dashboard, get_reflow, list_reflows, settle_reflow

_TITLE = "死信回炉"


def append_cutover(ref_id: int, note: str, db_path: str | None = None) -> dict:
    """落台切流账册：写物化包落点行并钉死读轨。"""
    del ref_id  # 甲路以当刻真实死信集为准，不再依赖外部 ref
    return settle_reflow({"note": note or _TITLE}, db_path)


def cutover_rows(db_path: str | None = None) -> list[dict]:
    """切流账册名录：最近若干次落定记录。"""
    return list_reflows(db_path)


def landing_rows(reflow_id: int, db_path: str | None = None) -> list[dict]:
    return get_reflow(reflow_id, db_path)["rows"]


def board(db_path: str | None = None) -> dict:
    dash = dashboard(db_path)
    dash["fate"] = "cutover_book"
    dash["cutover"] = cutover_rows(db_path)
    return dash
