"""开播提示的业务测试：正式场开播稳定一段时间后才托 QQ 麦麦喊人，每场一次，误开不喊。"""

from pathlib import Path
from typing import Any, Optional, Tuple

import asyncio

from fake_host import GROUP_ID, STREAM_ID, FakeHost, ManualClock, load_plugin_package


load_plugin_package()
import amaidesu_adapter_under_test.go_live as go_live  # noqa: E402
import amaidesu_adapter_under_test.live_status as live_status  # noqa: E402


MINUTE_MS = 60_000
# Amaidesu 那台机器的时钟比插件快两小时：开播提示只用 Amaidesu 自己的时刻相减，不受影响
AMAIDESU_SKEW_MS = 2 * 60 * MINUTE_MS


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_announcer(
    *, state_path: Optional[Path] = None, host: Optional[FakeHost] = None, **overrides: Any
) -> Tuple[Any, FakeHost, ManualClock]:
    host = host or FakeHost()
    clock = ManualClock()
    settings = go_live.GoLiveSettings(
        group_ids=overrides.pop("group_ids", (GROUP_ID,)),
        delay_ms=overrides.pop("delay_ms", 5 * MINUTE_MS),
        stale_after_ms=overrides.pop("stale_after_ms", 3 * MINUTE_MS),
        **overrides,
    )
    announcer = go_live.GoLiveAnnouncer(host.make_context(), settings, state_path=state_path, clock_ms=clock)
    announcer.load()
    return announcer, host, clock


def snapshot(clock: ManualClock, *, live: bool = True, session_id: int = 7, live_for_ms: int = 0, **fields: Any) -> Any:
    """按 Amaidesu 的时钟造一份快照：发出时刻 = 开播时刻 + 已播时长。"""

    sent_at_ms = clock.now_ms + AMAIDESU_SKEW_MS
    body = {
        "type": "snapshot",
        "live": live,
        "live_session_id": session_id,
        "title": "周五 MC 建房",
        "platform": "bilibili",
        "room_id": "123456",
        "started_at_ms": fields.pop("started_at_ms", sent_at_ms - live_for_ms),
        "sent_at_ms": sent_at_ms,
    }
    body.update(fields)
    return live_status.parse_report(body)


def triggers(host: FakeHost) -> list:
    return host.calls_of("maisaka.proactive.trigger")


def test_announces_once_after_live_lasts_long_enough() -> None:
    announcer, host, clock = make_announcer()
    started_at_ms = clock.now_ms + AMAIDESU_SKEW_MS
    announcer.observe(snapshot(clock, started_at_ms=started_at_ms))
    run(announcer.tick())
    assert triggers(host) == []

    # 每分钟一次心跳；不到 5 分钟不喊
    for _ in range(4):
        clock.advance(MINUTE_MS)
        announcer.observe(snapshot(clock, started_at_ms=started_at_ms))
        run(announcer.tick())
    assert triggers(host) == []

    clock.advance(MINUTE_MS)
    announcer.observe(snapshot(clock, started_at_ms=started_at_ms))
    run(announcer.tick())
    (call,) = triggers(host)
    assert call["stream_id"] == STREAM_ID and call["priority"] == "高"
    assert "在B站开播了《周五 MC 建房》，直播间 https://live.bilibili.com/123456" in call["intent"]
    assert "大约 5 分钟" in call["intent"] and "一定在群里说到" in call["intent"]

    # 喊过这场就不再喊，后续心跳照常
    clock.advance(MINUTE_MS)
    announcer.observe(snapshot(clock, started_at_ms=started_at_ms))
    run(announcer.tick())
    assert len(triggers(host)) == 1


def test_live_ended_or_discarded_before_delay_is_not_announced() -> None:
    announcer, host, clock = make_announcer()
    announcer.observe(snapshot(clock))
    clock.advance(2 * MINUTE_MS)
    announcer.observe(snapshot(clock, live=False, live_for_ms=2 * MINUTE_MS))
    clock.advance(10 * MINUTE_MS)
    run(announcer.tick())

    # 开了又作废的空场同样不喊
    announcer.observe(snapshot(clock, session_id=8))
    clock.advance(MINUTE_MS)
    announcer.observe(snapshot(clock, session_id=8, live=False, discarded=True))
    clock.advance(10 * MINUTE_MS)
    run(announcer.tick())
    assert triggers(host) == []


def test_stale_heartbeat_holds_off_until_heartbeat_returns() -> None:
    announcer, host, clock = make_announcer()
    started_at_ms = clock.now_ms + AMAIDESU_SKEW_MS
    announcer.observe(snapshot(clock, started_at_ms=started_at_ms))

    # 6 分钟没有心跳：可能已经关了或断线，宁可不喊
    clock.advance(6 * MINUTE_MS)
    run(announcer.tick())
    assert triggers(host) == []

    announcer.observe(snapshot(clock, started_at_ms=started_at_ms))
    run(announcer.tick())
    assert len(triggers(host)) == 1


def test_mid_live_restart_announces_by_amaidesu_clock() -> None:
    # 插件或 MaiBot 在直播中途才起来：第一次收到的心跳已经播了 20 分钟，不用再等 5 分钟
    announcer, host, clock = make_announcer()
    announcer.observe(snapshot(clock, live_for_ms=20 * MINUTE_MS))
    run(announcer.tick())
    (call,) = triggers(host)
    assert "大约 20 分钟" in call["intent"]


def test_announced_sessions_survive_restart(tmp_path: Path) -> None:
    state = tmp_path / "go_live_announced.json"
    announcer, host, clock = make_announcer(state_path=state, delay_ms=0)
    first = snapshot(clock, live_for_ms=MINUTE_MS)
    announcer.observe(first)
    run(announcer.tick())
    assert len(triggers(host)) == 1

    # 重启后同一场的心跳不会再喊；新的一场照喊
    restarted, restarted_host, restarted_clock = make_announcer(state_path=state, delay_ms=0)
    restarted_clock.advance(2 * MINUTE_MS)
    restarted.observe(snapshot(restarted_clock, started_at_ms=first.started_at_ms))
    run(restarted.tick())
    assert triggers(restarted_host) == []

    restarted.observe(snapshot(restarted_clock, session_id=8, live_for_ms=MINUTE_MS))
    run(restarted.tick())
    assert len(triggers(restarted_host)) == 1


def test_no_groups_configured_never_announces() -> None:
    announcer, host, clock = make_announcer(group_ids=(), delay_ms=0)
    announcer.observe(snapshot(clock, live_for_ms=30 * MINUTE_MS))
    run(announcer.tick())
    assert triggers(host) == [] and host.calls_of("chat.get_stream_by_group_id") == []


def test_one_failing_group_does_not_block_others_and_is_not_retried() -> None:
    host = FakeHost()
    host.streams["20002"] = {"stream_id": "stream-20002", "group_id": "20002", "group_name": "二群"}
    announcer, host, clock = make_announcer(host=host, group_ids=("99999", "20002"), delay_ms=0)
    announcer.observe(snapshot(clock, live_for_ms=MINUTE_MS))
    run(announcer.tick())
    (call,) = triggers(host)
    assert call["stream_id"] == "stream-20002"

    # 宁可漏喊一次也不刷屏：失败的群不重试
    clock.advance(MINUTE_MS)
    announcer.observe(snapshot(clock, live_for_ms=2 * MINUTE_MS))
    run(announcer.tick())
    assert len(triggers(host)) == 1


def test_room_link_override_and_unknown_platform() -> None:
    announcer, host, clock = make_announcer(delay_ms=0)
    announcer.observe(snapshot(clock, live_for_ms=MINUTE_MS, platform="twitch", room_id="abc"))
    run(announcer.tick())
    assert "直播间" not in triggers(host)[0]["intent"]

    custom, custom_host, custom_clock = make_announcer(delay_ms=0, room_url="https://b23.tv/live")
    custom.observe(snapshot(custom_clock, live_for_ms=MINUTE_MS))
    run(custom.tick())
    assert "直播间 https://b23.tv/live" in triggers(custom_host)[0]["intent"]
