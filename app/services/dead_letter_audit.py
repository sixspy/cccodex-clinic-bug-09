"""死信回炉读轨审计：只汇总甲路（cutover_book）单侧引用。"""
from __future__ import annotations

from app.services.dead_letter_cutover import board as cutover_board
from app.services.dead_letter import list_reflows, state


def audit_list(db_path: str | None = None) -> list[dict]:
    return list_reflows(db_path)


def audit_summary(db_path: str | None = None) -> dict:
    return {
        "title": "死信回炉审计",
        "fate": "cutover_book",
        "reflows": audit_list(db_path),
        "cutover": cutover_board(db_path),
        "state": state(db_path),
    }
