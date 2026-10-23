"""回想 QQ 那边的记忆与对群友的印象：范围只限本群、按名字找人、隐私边界。"""

from datetime import datetime
from typing import Any, Tuple

import asyncio

from fake_host import GROUP_ID, GROUP_NAME, STREAM_ID, FakeHost, ManualClock, load_bridge_module


bridge_module = load_bridge_module()
BridgeSettings = bridge_module.BridgeSettings
QQBridge = bridge_module.QQBridge

QQ_NUMBER = "2749533087"
KNOW_SINCE = datetime(2026, 9, 1, 12, 0).timestamp()
LAST_KNOW = datetime(2026, 10, 22, 20, 0).timestamp()


def make_bridge(**overrides: Any) -> Tuple[Any, FakeHost, ManualClock]:
    host = FakeHost()
    clock = ManualClock()
    bridge = QQBridge(host.make_context(), BridgeSettings(group_ids=(GROUP_ID,), **overrides), clock_ms=clock)
    return bridge, host, clock


def call(bridge: Any, tool: str, **arguments: Any) -> Tuple[str, bool]:
    return asyncio.run(bridge.call_tool(tool, arguments))


def add_sheep(host: FakeHost, **fields: Any) -> None:
    """登记群友"小羊"：MaiBot 叫他"羊羊"，本群群名片"小羊"，另一个群里叫"秘密马甲"。"""

    defaults = {
        "person_name": "羊羊",
        "nickname": "LittleSadSheep",
        "group_cardname_list": [
            {"group_id": GROUP_ID, "group_cardname": "小羊"},
            {"group_id": "777", "group_cardname": "秘密马甲"},
        ],
        "know_since": KNOW_SINCE,
        "know_times": 42,
        "last_know": LAST_KNOW,
    }
    defaults.update(fields)
    host.add_person("pid-sheep", QQ_NUMBER, **defaults)


def test_recall_memory_is_scoped_to_the_group() -> None:
    bridge, host, _ = make_bridge()
    host.knowledge["上次的约定"] = "你知道这些知识: 群里约好周五一起挖矿"

    text, failed = call(bridge, "recall_memory", query="上次的约定")
    assert not failed
    assert f"在「{GROUP_NAME}」（{GROUP_ID}）里记下的" in text and "周五一起挖矿" in text and "别把群友的私事" in text
    args = host.calls_of("knowledge.search")[0]
    # 范围限定为本群聊天流：私聊里形成的记忆不会被检出
    assert (args["chat_id"], args["group_id"], args["respect_filter"], args["person_id"]) == (
        STREAM_ID,
        GROUP_ID,
        True,
        "",
    )

    text, failed = call(bridge, "recall_memory", query="没聊过的事")
    assert not failed and "没有关于「没聊过的事」的记忆（只查了这个群）" in text


def test_recall_memory_rejects_other_groups_and_reports_failure() -> None:
    bridge, host, _ = make_bridge()
    text, failed = call(bridge, "recall_memory", query="x", group_id="999")
    assert failed and "不在允许名单" in text
    assert host.calls_of("knowledge.search") == []

    host.knowledge_fails = True
    text, failed = call(bridge, "recall_memory", query="x")
    assert failed and "长期记忆未启用" in text
    text, failed = call(bridge, "recall_memory", query="  ")
    assert failed and "没说要回想什么" in text


def test_recall_person_by_maibot_name_shows_only_this_group() -> None:
    bridge, host, _ = make_bridge()
    add_sheep(host)
    host.knowledge["pid-sheep"] = "你知道这些知识: 他喜欢在群里发猫图"

    text, failed = call(bridge, "recall_person", name="羊羊")
    assert not failed
    assert "你叫他「羊羊」，他在这个群的群名片是「小羊」，QQ 昵称「LittleSadSheep」" in text
    assert "认识于 2026年9月1日，来往过 42 次，上次是 2026年10月22日" in text
    assert "相关记忆：你知道这些知识: 他喜欢在群里发猫图" in text
    # 不出现 QQ 号，也不透露他在别的群的名片
    assert QQ_NUMBER not in text and "秘密马甲" not in text
    memory_args = host.calls_of("knowledge.search")[-1]
    assert (memory_args["person_id"], memory_args["chat_id"]) == ("pid-sheep", STREAM_ID)


def test_recall_person_by_group_card_from_recent_speakers() -> None:
    bridge, host, clock = make_bridge(person_lookup_days=30)
    add_sheep(host, person_name="")
    now_s = clock.now_ms / 1000
    host.add_message(now_s - 3600, "在吗", user_id=QQ_NUMBER, nickname="LittleSadSheep", cardname="小羊")
    host.add_message(now_s - 40 * 86400, "很久以前", user_id="111", nickname="老王", cardname="老王")

    text, failed = call(bridge, "recall_person", name="小羊")
    assert not failed and "群名片是「小羊」" in text and "没有他在这个群里的记忆" in text
    scan = host.calls_of("message.get_by_time_in_chat")[-1]
    assert float(scan["start_time"]) == now_s - 30 * 86400 and scan["limit"] == 500

    text, failed = call(bridge, "recall_person", name="老王")  # 只在 30 天之外说过话
    assert failed and "最近 30 天的聊天里没找到叫「老王」的人" in text


def test_recall_person_ambiguous_unknown_and_self() -> None:
    bridge, host, clock = make_bridge()
    now_s = clock.now_ms / 1000
    host.add_message(now_s - 10, "a", user_id="1", nickname="阿猫一号", cardname="")
    host.add_message(now_s - 9, "b", user_id="2", nickname="阿猫二号", cardname="")
    host.add_message(now_s - 8, "c", user_id="3", nickname="路人", cardname="")

    text, failed = call(bridge, "recall_person", name="阿猫")
    assert failed and "不止一个" in text and "「阿猫一号」" in text and "「阿猫二号」" in text

    text, failed = call(bridge, "recall_person", name="路人")
    assert not failed and "还不认识「路人」" in text

    host.config["bot.alias_names"] = ["昔涟"]
    for own_name in ("麦麦", "昔涟"):
        text, failed = call(bridge, "recall_person", name=own_name)
        assert not failed and "就是 QQ 那边的你自己" in text


def test_person_known_only_outside_this_group_is_not_revealed() -> None:
    bridge, host, _ = make_bridge()
    # MaiBot 认识"私聊朋友"，但他在这个群没有群名片、最近也没在这个群说过话
    host.add_person("pid-private", "555", person_name="私聊朋友", group_cardname_list=[], know_times=99)

    text, failed = call(bridge, "recall_person", name="私聊朋友")
    assert failed and "没找到叫「私聊朋友」的人" in text and "99" not in text


def test_tool_list_includes_recall_tools() -> None:
    bridge, _, _ = make_bridge(person_lookup_days=14)
    definitions = {item.name: item for item in bridge.tool_definitions()}
    assert {"recall_memory", "recall_person"} <= set(definitions)
    assert "最近 14 天" in definitions["recall_person"].description
    assert definitions["recall_memory"].input_schema["required"] == ["query"]
