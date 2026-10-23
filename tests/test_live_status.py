"""直播近况：上报校验、缓存与落盘、注入与查询文字、注入位置。"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from fake_host import load_plugin_package


load_plugin_package()
import amaidesu_adapter_under_test.live_status as live_status  # noqa: E402

ValidationError = pytest.importorskip("pydantic").ValidationError

MINUTE_MS = 60_000
# 固定在某天 21:00，避免跨天让"今天 / 昨天"的说法随测试运行时刻变化
NOW_MS = int(datetime(2026, 10, 23, 21, 0).timestamp() * 1000)


class Clock:
    def __init__(self) -> None:
        self.now_ms = NOW_MS

    def __call__(self) -> int:
        return self.now_ms


def make_store(path: Any = None, **settings: Any) -> Tuple[Any, Clock]:
    clock = Clock()
    store = live_status.LiveStatusStore(path, live_status.LiveStatusSettings(**settings), clock_ms=clock)
    return store, clock


def snapshot(live: bool = True, **fields: Any) -> Any:
    body: Dict[str, Any] = {
        "type": "snapshot",
        "live": live,
        "live_session_id": 7,
        "title": "周五 MC 建房",
        "platform": "bilibili",
        "started_at_ms": NOW_MS - 55 * MINUTE_MS,
    }
    body.update(fields)
    return live_status.parse_report(body)


def recap(
    session_id: int = 7, ended_ms: int = NOW_MS - 5 * MINUTE_MS, text: str = "这场搭好了屋顶，答应下次挖矿"
) -> Any:
    return live_status.parse_report(
        {
            "type": "recap",
            "live_session_id": session_id,
            "started_at_ms": ended_ms - 60 * MINUTE_MS,
            "ended_at_ms": ended_ms,
            "text": text,
        }
    )


def test_parse_report_validates_contract() -> None:
    assert isinstance(snapshot(), live_status.SnapshotReport)
    assert isinstance(recap(), live_status.RecapReport)
    for bad in (
        {"type": "gossip", "live_session_id": 1},
        {"type": "recap", "live_session_id": 1, "text": ""},
        {"live": True},
    ):
        with pytest.raises(ValidationError):
            live_status.parse_report(bad)


def test_live_injection_uses_absolute_times_and_segment() -> None:
    store, _ = make_store()
    assert store.injection_text() == ""  # 什么都没收到：不注入

    store.apply(snapshot(segment={"title": "搭屋顶", "index": 2, "total": 7}))
    text = store.injection_text()
    assert text.startswith("<live-status>") and text.endswith("</live-status>")
    assert (
        "你此刻正在B站开直播（你自己是主播）《周五 MC 建房》（今天 20:05 开播），进行到第 3/7 个环节「搭屋顶」。"
        in text
    )
    # 写明是关于自己的事实、被问到要照此回答（实机里写成"可以聊"时模型会忽略它）
    assert "关于你自己的事实" in text and "照这个如实回答" in text
    assert "分钟" not in text  # 不写会每分钟变化的相对时长

    store.apply(snapshot(segment={"title": "", "index": 7, "total": 7, "finished": True}))
    assert "流程单已经走完" in store.injection_text()


def test_live_without_heartbeat_becomes_uncertain() -> None:
    store, clock = make_store(stale_after_ms=3 * MINUTE_MS)
    store.apply(snapshot())
    clock.now_ms += 4 * MINUTE_MS
    text = store.injection_text()
    assert text.count("你今天 20:05 在B站开了一场直播（你自己是主播）《周五 MC 建房》") == 1
    assert "此刻正在" not in text and "最后一次收到直播那边的消息是 今天 21:00" in text and "可能已经下播或断开" in text


def test_recent_end_window_is_configurable() -> None:
    store, clock = make_store(recent_window_ms=12 * 60 * MINUTE_MS, recap_chars=6)
    store.apply(snapshot(live=False, ended_at_ms=NOW_MS - 5 * MINUTE_MS))
    store.apply(recap())
    text = store.injection_text()
    assert (
        "你今天 20:05–20:55 在B站开了一场直播（你自己是主播）《周五 MC 建房》，已经下播。这场的回顾：这场搭好了屋……"
        in text
    )

    clock.now_ms += 13 * 60 * MINUTE_MS  # 超出 12 小时窗口：不再主动注入，工具仍能查
    assert store.injection_text() == ""
    assert "已经下播" in store.query_text()

    instant, _ = make_store(recent_window_ms=0)
    instant.apply(snapshot(live=False, ended_at_ms=NOW_MS - 1))
    assert instant.injection_text() == ""


def test_discarded_session_falls_back_to_last_recap() -> None:
    store, _ = make_store()
    store.apply(recap(session_id=6, ended_ms=NOW_MS - 3 * 60 * MINUTE_MS, text="上一场的回顾"))
    store.apply(snapshot(live=False, live_session_id=8, ended_at_ms=NOW_MS - MINUTE_MS, discarded=True))
    text = store.injection_text()
    assert "周五 MC 建房" not in text and "上一场的回顾" in text


def test_recaps_dedupe_order_limit_and_query() -> None:
    store, _ = make_store(keep_recaps=2)
    store.apply(recap(session_id=1, ended_ms=NOW_MS - 3 * 24 * 60 * MINUTE_MS, text="第一场"))
    store.apply(recap(session_id=2, ended_ms=NOW_MS - 2 * 24 * 60 * MINUTE_MS, text="第二场"))
    store.apply(recap(session_id=2, ended_ms=NOW_MS - 2 * 24 * 60 * MINUTE_MS, text="第二场改写"))
    store.apply(recap(session_id=3, ended_ms=NOW_MS - 24 * 60 * MINUTE_MS, text="第三场"))

    assert "还没有收到过直播那边的近况" not in store.query_text(0)
    assert "最近一场的回顾" in store.query_text(0) and "第三场" in store.query_text(0)
    assert "往前第 1 场" in store.query_text(1) and "第二场改写" in store.query_text(1)
    assert "只记得最近 2 场" in store.query_text(2)


def test_persists_across_restart_and_survives_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "live_status.json"
    store, _ = make_store(path)
    store.apply(snapshot(segment={"title": "开场", "index": 0, "total": 3}))
    store.apply(recap(session_id=6))

    reloaded, _ = make_store(path)
    reloaded.load()
    assert reloaded.injection_text() == store.injection_text()
    assert "最近一场的回顾" in reloaded.query_text(0)

    path.write_text("{坏掉的 JSON", encoding="utf-8")
    broken, _ = make_store(path)
    broken.load()
    assert broken.injection_text() == ""


def test_status_item_goes_before_the_final_instruction() -> None:
    items = [
        {"item_type": "SystemMessageItem"},
        {"item_type": "UserMessageItem"},
        {"item_type": "UserMessageItem", "x": "指令"},
    ]
    merged = live_status.insert_status_item(items, "<live-status>x</live-status>")
    assert (
        len(merged) == 4
        and merged[-1]["x"] == "指令"
        and merged[2]["parts"][0]["text"] == "<live-status>x</live-status>"
    )
    assert merged[2]["item_type"] == "UserMessageItem" and merged[2]["meta"]["logical_turn_id"] is None
    assert items[2]["x"] == "指令" and len(items) == 3  # 不改原列表
    assert len(live_status.insert_status_item([], "t")) == 1
