"""统一本地日折算（plan personal-agent-memory-2026-11-05 D-1：统一切本地日）。

背景：日记/月记此前按 UTC 取日（``datetime.now(timezone.utc).strftime``），
跨 UTC 日界的本地一天（UTC+8 的白天）会被拆进两篇日记。本模块是**唯一**的
本地日口径：存储时间戳继续 UTC ISO（存量会话 JSONL 零改写），只在「取日」
这一步折算成用户所在时区的日历日。

必须走这里的 7 处（plan §13.2.1）：``maintain_diary`` 的 day 计算 / 素材筛选 /
完成任务筛选 / 日记文件名（自动跟随）/ ``_catchup_diary_days()`` /
``maintain_month_diary`` 的 ``current_month`` / 事实源 §2.7 待办关闭。
**不改** ``_now_iso()`` 的 UTC 写入、``_l0_cutoff(days)`` 滚动窗口、META 时间戳。

自检三问（plan §2）：① 解决的是语境（用户感知的"一天"），不是隔离；② 去掉它
日记按 UTC 日界断开，主人傍晚的经历会被记进"昨天"；③ 不在存储层做任何事——
只折算，不改写。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

__all__ = ["local_tz", "local_today", "local_days_ago", "local_date"]


def local_tz():
    """本机时区（惰性求值，跟随系统设置）。"""
    return datetime.now().astimezone().tzinfo


def local_today() -> str:
    """今天（本地日历日，``YYYY-MM-DD``）。"""
    return datetime.now().strftime("%Y-%m-%d")


def local_days_ago(n: int) -> str:
    """N 天前（本地日历日）。``n=1`` 即"昨日"（日记 D+1 语义的取日口径）。"""
    return (datetime.now() - timedelta(days=max(0, int(n)))).strftime("%Y-%m-%d")


def local_date(ts: str) -> str:
    """存储时间戳（UTC ISO）→ 本地日历日；解析失败返回空串（调用方按降级处理）。

    naive 时间戳按存储口径（UTC）处理——会话 JSONL 的 ``ts`` 由 ``_now_iso()``
    写入，恒为带时区的 UTC ISO；naive 属历史脏数据，按 UTC 折算不猜本地。
    """
    t = str(ts or "").strip()
    if not t:
        return ""
    try:
        dt = datetime.fromisoformat(t)
    except Exception:
        # 宽容「YYYY-MM-DD …」形态（前 10 位是合法日历日则原样返回）
        head = t[:10]
        try:
            datetime.strptime(head, "%Y-%m-%d")
            return head
        except Exception:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz()).strftime("%Y-%m-%d")


def local_month_bounds(month: str) -> tuple:
    """``YYYY-MM`` → (首日, 末日) 本地日历日字符串（月记索引的 date_from/date_to）。"""
    from calendar import monthrange

    m = str(month or "")[:7]
    try:
        year, mon = int(m[:4]), int(m[5:7])
        last = monthrange(year, mon)[1]
        return (f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}")
    except Exception:
        return ("", "")
