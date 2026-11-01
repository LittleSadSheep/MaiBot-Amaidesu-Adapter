"""读群、托 QQ 麦麦说话，以及打开直接发群时的发群与发言权接管的业务测试。"""

from typing import Any, Tuple

import asyncio

from fake_host import GROUP_ID, GROUP_NAME, STREAM_ID, FakeHost, ManualClock, load_bridge_module


bridge_module = load_bridge_module()
BridgeSettings = bridge_module.BridgeSettings
QQBridge = bridge_module.QQBridge
ToolCallError = bridge_module.ToolCallError

MINUTE_MS = 60_000


def make_bridge(**overrides: Any) -> Tuple[Any, FakeHost, ManualClock]:
    host = FakeHost()
    clock = ManualClock()
    settings = BridgeSettings(group_ids=(GROUP_ID,), **overrides)
    bridge = QQBridge(host.make_context(), settings, clock_ms=clock)
    return bridge, host, clock


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_first_read_uses_lookback_then_only_new_messages() -> None:
    bridge, host, clock = make_bridge()
    now_s = clock.now_ms / 1000
    host.add_message(now_s - 3 * 60 * 60, "三小时前的旧消息")
    host.add_message(now_s - 60, "群友A: 今晚播吗")

    first = run(bridge.read_group())
    assert GROUP_NAME in first and "最近的消息（1 条）" in first
    assert "今晚播吗" in first and "旧消息" not in first
    assert "署名「麦麦」的发言是 QQ 那边的你自己" in first and "群友也可能叫你" not in first

    clock.advance(MINUTE_MS)
    assert "自上次查看以来没有新消息" in run(bridge.read_group())

    clock.advance(1000)
    host.add_message(clock.now_ms / 1000 - 0.5, "群友B: 来了来了")
    second = run(bridge.read_group())
    assert "上次查看之后的新消息（1 条）" in second
    assert "来了来了" in second and "今晚播吗" not in second


def test_read_names_aliases_and_omits_hint_without_nickname() -> None:
    bridge, host, clock = make_bridge()
    host.add_message(clock.now_ms / 1000 - 10, "群友A: @昔涟 在吗")
    host.config["bot.alias_names"] = ["昔涟", " "]
    assert "署名「麦麦」的发言是 QQ 那边的你自己；群友也可能叫你昔涟）" in run(bridge.read_group())

    host.config["bot.nickname"] = ""
    text = run(bridge.read_group(only_new=False))
    assert "署名" not in text and "在吗" in text


def test_read_without_only_new_rereads_recent_and_marks_truncation() -> None:
    bridge, host, clock = make_bridge(read_default_limit=2)
    now_s = clock.now_ms / 1000
    for index in range(3):
        host.add_message(now_s - 30 + index, f"消息{index}")
    run(bridge.read_group())

    text = run(bridge.read_group(only_new=False))
    assert "只显示最近 2 条" in text
    assert "消息1" in text and "消息2" in text and "消息0" not in text
    args = host.calls_of("message.get_by_time_in_chat")[-1]
    assert args["chat_id"] == STREAM_ID and args["limit"] == 2 and args["limit_mode"] == "latest"


def test_group_whitelist_and_missing_stream_are_reported() -> None:
    bridge, host, _ = make_bridge()
    text, failed = run(bridge.call_tool("read_group", {"group_id": "99999"}))
    assert failed and "不在允许名单" in text and GROUP_ID in text

    del host.streams[GROUP_ID]
    text, failed = run(bridge.call_tool("ask_mai", {"intent": "喊人"}))
    assert failed and "还没有群" in text
    assert host.calls_of("maisaka.proactive.trigger") == []

    empty, _, _ = make_bridge()
    empty.update_settings(BridgeSettings(group_ids=()))
    text, failed = run(empty.call_tool("read_group", {}))
    assert failed and "还没有配置" in text


def test_direct_send_off_hides_send_tool_and_rejects_send() -> None:
    bridge, host, _ = make_bridge()
    assert "send_group" not in {item.name for item in bridge.tool_definitions()}

    # 按旧工具清单发来的发群调用也挡住，并指给主播托话这条路；不发消息、不碰发言频率。
    text, failed = run(bridge.call_tool("send_group", {"text": "大家好"}))
    assert failed and "ask_mai" in text
    assert host.calls_of("send.text") == []
    assert host.calls_of("frequency.get_adjust") == [] and host.calls_of("frequency.set_adjust") == []


def test_send_syncs_to_maibot_history_and_takes_over_group() -> None:
    bridge, host, _ = make_bridge(direct_send=True)
    host.adjust[STREAM_ID] = 0.8

    reply = run(bridge.send_group("大家晚上好，我开播啦"))
    assert "已发到" in reply and "少说话" in reply
    send_args = host.calls_of("send.text")[0]
    assert send_args["stream_id"] == STREAM_ID
    assert send_args["sync_to_maisaka_history"] is True
    assert send_args["maisaka_source_kind"] == bridge_module.SEND_SOURCE_KIND
    assert host.adjust[STREAM_ID] == 0.0
    assert STREAM_ID in bridge.active_takeovers()


def test_takeover_renews_on_each_send_and_restores_after_expiry() -> None:
    bridge, host, clock = make_bridge(direct_send=True, takeover_duration_ms=10 * MINUTE_MS, min_send_interval_ms=0)
    host.adjust[STREAM_ID] = 0.8
    run(bridge.send_group("第一句"))
    clock.advance(8 * MINUTE_MS)
    run(bridge.send_group("第二句"))
    # 续期时重新施加倍率，并且只在第一次接管时读取原倍率。
    assert len(host.calls_of("frequency.set_adjust")) == 2
    assert len(host.calls_of("frequency.get_adjust")) == 1

    clock.advance(9 * MINUTE_MS)
    run(bridge.expire_takeovers())
    assert host.adjust[STREAM_ID] == 0.0

    clock.advance(2 * MINUTE_MS)
    run(bridge.expire_takeovers())
    assert host.adjust[STREAM_ID] == 0.8
    assert bridge.active_takeovers() == {}


def test_send_guards_length_interval_and_failure() -> None:
    bridge, host, clock = make_bridge(direct_send=True, max_text_length=5, min_send_interval_ms=2000)
    text, failed = run(bridge.call_tool("send_group", {"text": "这句话超过五个字"}))
    assert failed and "超过上限 5 字" in text

    run(bridge.send_group("短句"))
    clock.advance(500)
    text, failed = run(bridge.call_tool("send_group", {"text": "再来"}))
    assert failed and "1500 毫秒后再发" in text
    assert len(host.calls_of("send.text")) == 1

    other, other_host, _ = make_bridge(direct_send=True)
    other_host.send_ok = False
    text, failed = run(other.call_tool("send_group", {"text": "发不出去"}))
    assert failed and "没能发到" in text
    assert other.active_takeovers() == {}
    assert other_host.calls_of("frequency.set_adjust") == []


def test_unreadable_adjust_skips_takeover_but_message_still_sent() -> None:
    bridge, host, _ = make_bridge(direct_send=True)
    host.get_adjust_fails = True
    reply = run(bridge.send_group("你好"))
    assert reply.startswith("已发到") and "少说话" not in reply
    assert host.calls_of("frequency.set_adjust") == []


def test_takeover_disabled_never_touches_frequency() -> None:
    bridge, host, _ = make_bridge(direct_send=True, takeover_enabled=False)
    run(bridge.send_group("你好"))
    assert host.calls_of("frequency.get_adjust") == [] and host.calls_of("frequency.set_adjust") == []


def test_ask_mai_returns_voice_before_waking_qq_mai() -> None:
    bridge, host, _ = make_bridge(direct_send=True)
    host.adjust[STREAM_ID] = 0.6
    run(bridge.send_group("我先聊两句"))
    assert host.adjust[STREAM_ID] == 0.0

    reply = run(bridge.ask_mai("开播了，喊大家来看，直播间 https://live.bilibili.com/1"))
    assert "已托 QQ 那边的你" in reply and "下次读群" in reply
    assert host.adjust[STREAM_ID] == 0.6
    # 还原发言权必须发生在唤醒之前，否则倍率为 0 时 QQ 麦麦可能不出声。
    names = [name for name, _ in host.calls]
    assert names.index("maisaka.proactive.trigger") > max(
        index for index, name in enumerate(names) if name == "frequency.set_adjust"
    )
    trigger_args = host.calls_of("maisaka.proactive.trigger")[0]
    assert trigger_args["stream_id"] == STREAM_ID
    assert "live.bilibili.com/1" in trigger_args["intent"]


def test_ask_mai_reports_trigger_refusal() -> None:
    bridge, host, _ = make_bridge()
    host.trigger_result = {"success": False, "error": "适配器策略拒绝", "error_code": "adapter_policy_denied"}
    text, failed = run(bridge.call_tool("ask_mai", {"intent": "喊人"}))
    assert failed and "适配器策略拒绝" in text

    # 唤醒失败不算托过，主播可以马上重试。
    host.trigger_result = {"success": True, "stream_id": STREAM_ID, "task_id": "t2", "queued": True}
    text, failed = run(bridge.call_tool("ask_mai", {"intent": "喊人"}))
    assert not failed


def test_ask_mai_waits_between_requests_to_same_group() -> None:
    bridge, host, clock = make_bridge(min_ask_interval_ms=MINUTE_MS)
    run(bridge.ask_mai("开播了，喊大家来看"))

    clock.advance(20_000)
    text, failed = run(bridge.call_tool("ask_mai", {"intent": "再喊一遍"}))
    assert failed and "40 秒后再托" in text
    assert len(host.calls_of("maisaka.proactive.trigger")) == 1

    clock.advance(40_000)
    text, failed = run(bridge.call_tool("ask_mai", {"intent": "再喊一遍"}))
    assert not failed
    assert len(host.calls_of("maisaka.proactive.trigger")) == 2


def test_ask_mai_must_say_raises_priority() -> None:
    bridge, host, _ = make_bridge(min_ask_interval_ms=0)
    run(bridge.call_tool("ask_mai", {"intent": "开播了，直播间 https://live.bilibili.com/1", "must_say": "true"}))
    run(bridge.call_tool("ask_mai", {"intent": "随便聊聊今天的直播"}))

    urgent, casual = host.calls_of("maisaka.proactive.trigger")
    assert urgent["priority"] == "高" and "一定在群里说到" in urgent["intent"]
    assert casual["priority"] == "" and "一定" not in casual["intent"]

    text, failed = run(bridge.call_tool("ask_mai", {"intent": "喊人", "must_say": "maybe"}))
    assert failed and "must_say" in text


def test_turning_direct_send_off_returns_voice_at_once() -> None:
    bridge, host, _ = make_bridge(direct_send=True)
    host.adjust[STREAM_ID] = 0.8
    run(bridge.send_group("我来聊两句"))
    assert host.adjust[STREAM_ID] == 0.0

    # 接管还没到期，但直接发群被关掉了：下一次到期检查就把发言权还给 QQ 麦麦。
    bridge.update_settings(BridgeSettings(group_ids=(GROUP_ID,), direct_send=False))
    run(bridge.expire_takeovers())
    assert host.adjust[STREAM_ID] == 0.8
    assert bridge.active_takeovers() == {}


def test_release_all_restores_every_group() -> None:
    bridge, host, _ = make_bridge(direct_send=True)
    host.adjust[STREAM_ID] = 0.9
    run(bridge.send_group("在吗"))
    run(bridge.release_all())
    assert host.adjust[STREAM_ID] == 0.9
    assert bridge.active_takeovers() == {}


def test_call_tool_coerces_model_arguments() -> None:
    bridge, host, clock = make_bridge()
    host.add_message(clock.now_ms / 1000 - 10, "消息")
    text, failed = run(bridge.call_tool("read_group", {"group_id": int(GROUP_ID), "limit": "5", "only_new": "false"}))
    assert not failed and "消息" in text
    assert host.calls_of("message.get_by_time_in_chat")[-1]["limit"] == 5

    text, failed = run(bridge.call_tool("read_group", {"only_new": "maybe"}))
    assert failed and "only_new" in text
    text, failed = run(bridge.call_tool("no_such_tool", {}))
    assert failed and "no_such_tool" in text


def test_tool_definitions_list_configured_groups() -> None:
    bridge, _, _ = make_bridge(min_ask_interval_ms=90_000)
    definitions = {item.name: item for item in bridge.tool_definitions()}
    assert set(definitions) == {"read_group", "ask_mai", "recall_memory", "recall_person"}
    ask = definitions["ask_mai"]
    assert GROUP_ID in ask.input_schema["properties"]["group_id"]["description"]
    assert ask.input_schema["required"] == ["intent"] and "must_say" in ask.input_schema["properties"]
    assert "隔 90 秒" in ask.description and "别在直播里说已经发到群里了" in ask.description
    assert "发言权交还" not in ask.description


def test_tool_definitions_with_direct_send_add_send_tool() -> None:
    bridge, _, _ = make_bridge(direct_send=True, takeover_duration_ms=5 * MINUTE_MS)
    definitions = {item.name: item for item in bridge.tool_definitions()}
    assert set(definitions) == {"read_group", "send_group", "ask_mai", "recall_memory", "recall_person"}
    assert GROUP_ID in definitions["send_group"].input_schema["properties"]["group_id"]["description"]
    assert "约 5 分钟" in definitions["send_group"].description
    assert "发言权交还" in definitions["ask_mai"].description
