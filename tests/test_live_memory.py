"""直播进记忆：整场记录的校验与合并、回放顺序与时间、只收不说、续放与去重。"""

from typing import Any, Dict, List, Optional

import asyncio
import json
import time

from fake_host import FakeHost, load_plugin_package


plugin_module = load_plugin_package()
import amaidesu_adapter_under_test.live_memory as live_memory  # noqa: E402

ROOM_ID = "778899"
LIVE_STREAM = f"stream-{ROOM_ID}"
BASE_MS = 1_792_760_700_000
GATEWAY = "amaidesu_live_room"


def line(offset_s: int, role: str, text: str, user_id: str = "", user_name: str = "") -> Dict[str, Any]:
    return {"ts_ms": BASE_MS + offset_s * 1000, "role": role, "user_id": user_id, "user_name": user_name, "text": text}


def default_lines() -> List[Dict[str, Any]]:
    return [
        line(0, "viewer", "来了来了", "1001", "小明"),
        line(5, "streamer", "欢迎小明！"),
        line(6, "streamer", "今天我们继续盖房子。"),
        line(20, "viewer", "屋顶用什么材料", "1002", "阿花"),
        line(30, "streamer", "用深色橡木。"),
        line(40, "viewer", "好看", "1001", "小明"),
    ]


def transcript(live_session_id: int = 42, lines: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "type": "transcript",
        "live_session_id": live_session_id,
        "platform": "bilibili",
        "room_id": ROOM_ID,
        "title": "周五 MC 建房",
        "started_at_ms": BASE_MS,
        "ended_at_ms": BASE_MS + 3_600_000,
        "lines": default_lines() if lines is None else lines,
    }


def make_config(enabled: bool = True) -> Dict[str, Any]:
    # 令牌留空：不起 MCP 服务，直接调接收函数
    return {
        "plugin": {"enabled": True, "config_version": "1.0.0"},
        "server": {"host": "127.0.0.1", "port": 8767, "auth_token": ""},
        "qq": {"group_ids": []},
        "live_memory": {"enabled": enabled, "store_wait_ms": 1000},
    }


def make_plugin(host: FakeHost, config: Dict[str, Any]) -> Any:
    plugin = plugin_module.create_plugin()
    plugin._set_context(host.make_context())
    plugin.set_plugin_config(config)
    return plugin


def install_send_pipeline(host: FakeHost, plugin: Any) -> None:
    """模拟宿主发送链路：构建出站消息（带当前时间）→ 发送钩子 → 网关 → 网关回成功才入库。"""

    async def pipeline(args: Dict[str, Any]) -> Dict[str, Any]:
        message = {
            "message_id": "send_api_1",
            "timestamp": "1999999999.0",
            "raw_message": [{"type": "text", "data": args["text"]}],
        }
        hooked = await plugin.backdate_live_room_speech(message=message, stream_id=args["stream_id"])
        message = hooked.get("modified_kwargs", {}).get("message", message)
        delivery = await plugin.live_room_gateway(message)
        if not delivery.get("success"):
            return {"success": False, "sent": False, "message_id": None, "error": delivery.get("error")}
        host.messages.setdefault(args["stream_id"], []).append(
            {"timestamp": message["timestamp"], "text": args["text"], "message_id": message["message_id"], "bot": True}
        )
        return {"success": True, "sent": True, "message_id": message["message_id"]}

    host.send_pipeline = pipeline


def done_ids(host: FakeHost) -> List[int]:
    path = host.data_dir / "live_memory" / "done.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


async def wait_until(predicate: Any, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.02)


def timeline(host: FakeHost) -> List[Any]:
    stored = sorted(host.messages.get(LIVE_STREAM, []), key=lambda item: float(item["timestamp"]))
    return [(round(float(item["timestamp"]) * 1000), item["text"], bool(item.get("bot"))) for item in stored]


def test_build_job_sorts_merges_and_names() -> None:
    settings = live_memory.LiveMemorySettings(merge_chars=12)
    lines = [
        line(30, "streamer", "第三句话说完了"),
        line(10, "streamer", "第一句"),
        line(11, "streamer", "第二句"),
        line(0, "viewer", "  在吗\n在吗  "),
        line(40, "partner", "我来串门"),
        line(50, "viewer", "无名氏发言", user_name="路人"),
    ]
    report = live_memory.parse_transcript({**transcript(lines=lines), "platform": "", "room_id": ""})

    job = live_memory.build_job(report, settings)

    assert (job.platform, job.group_id, job.group_name) == ("bilibili", "live", "B站直播间")
    assert [(step.outbound, step.text) for step in job.steps] == [
        (False, "在吗 在吗"),
        (True, "第一句 第二句"),
        (True, "第三句话说完了"),
        (False, "我来串门"),
        (False, "无名氏发言"),
    ]
    assert job.steps[1].ts_ms == BASE_MS + 10_000
    assert (job.steps[3].user_id, job.steps[3].user_name) == ("name:联动嘉宾", "联动嘉宾")
    assert (job.steps[4].user_id, job.steps[4].user_name) == ("name:路人", "路人")


def test_replay_writes_timeline_with_original_times_and_silences_room() -> None:
    async def scenario() -> None:
        host = FakeHost()
        plugin = make_plugin(host, make_config())
        install_send_pipeline(host, plugin)
        await plugin.on_load()
        try:
            status, body = await plugin._receive_live_status(transcript())
            assert status == 200 and "已排队" in body["detail"]
            await wait_until(lambda: 42 in done_ids(host))

            state = host.gateway_states[0]
            assert (state["gateway_name"], state["ready"], state["platform"], state["account_id"]) == (
                GATEWAY,
                True,
                "bilibili",
                "amaidesu",
            )
            assert host.adjust[LIVE_STREAM] == 0.0
            assert host.streams[ROOM_ID]["group_name"] == "B站直播间"
            assert timeline(host) == [
                (BASE_MS, "来了来了", False),
                (BASE_MS + 5_000, "欢迎小明！ 今天我们继续盖房子。", True),
                (BASE_MS + 20_000, "屋顶用什么材料", False),
                (BASE_MS + 30_000, "用深色橡木。", True),
                (BASE_MS + 40_000, "好看", False),
            ]
            bot_ids = [item["message_id"] for item in host.messages[LIVE_STREAM] if item.get("bot")]
            assert bot_ids == ["amaidesu-live-42-1", "amaidesu-live-42-3"]

            status, body = await plugin._receive_live_status(transcript())
            assert status == 200 and "已经回放过" in body["detail"]
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_each_speech_waits_until_earlier_viewer_messages_are_stored() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.defer_store = True
        plugin = make_plugin(host, make_config())
        install_send_pipeline(host, plugin)
        await plugin.on_load()
        try:
            await plugin._receive_live_status(transcript())
            await wait_until(lambda: 42 in done_ids(host))
            # 第一句前应有 1 条观众消息入库；第二句前有 观众 2 + 主播 1
            assert host.stored_count_at_send == [1, 3]
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_room_refuses_speech_that_is_not_being_replayed() -> None:
    async def scenario() -> None:
        host = FakeHost()
        plugin = make_plugin(host, make_config())
        await plugin.on_load()
        try:
            planner_reply = {"message_id": "send_api_9", "raw_message": [{"type": "text", "data": "大家好"}]}
            delivery = await plugin.live_room_gateway(planner_reply)
            assert delivery["success"] is False

            hooked = await plugin.backdate_live_room_speech(message=planner_reply, stream_id="stream-10001")
            assert hooked == {"action": "continue"}
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_blocked_room_stays_queued_and_retries_on_repush() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.route_accept = False
        plugin = make_plugin(host, make_config())
        install_send_pipeline(host, plugin)
        await plugin.on_load()
        try:
            await plugin._receive_live_status(transcript())
            await wait_until(lambda: bool(host.calls_of("host.route_message")))
            await asyncio.sleep(0.1)
            assert done_ids(host) == []
            assert (host.data_dir / "live_memory" / "jobs" / "42.json").exists()

            # 用户放行后，Amaidesu 补推同一场：不重建任务，唤醒后接着放
            host.route_accept = True
            status, body = await plugin._receive_live_status(transcript())
            assert status == 200 and "重新尝试" in body["detail"]
            await wait_until(lambda: 42 in done_ids(host))
            assert [text for _, text, _ in timeline(host)][0] == "来了来了"
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_unfinished_job_resumes_after_reload() -> None:
    async def scenario() -> None:
        host = FakeHost()
        settings = live_memory.LiveMemorySettings()
        job = live_memory.build_job(live_memory.parse_transcript(transcript()), settings)
        # 上次放到第 2 步（屋顶那句）时插件被重启
        job.next_index = 2
        jobs_dir = host.data_dir / "live_memory" / "jobs"
        jobs_dir.mkdir(parents=True)
        (jobs_dir / "42.json").write_text(job.model_dump_json(), encoding="utf-8")

        plugin = make_plugin(host, make_config())
        install_send_pipeline(host, plugin)
        await plugin.on_load()
        try:
            await wait_until(lambda: 42 in done_ids(host))
            assert [text for _, text, _ in timeline(host)] == ["屋顶用什么材料", "用深色橡木。", "好看"]
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_transcript_rejected_when_disabled_or_malformed() -> None:
    async def scenario() -> None:
        host = FakeHost()
        disabled = make_plugin(host, make_config(enabled=False))
        await disabled.on_load()
        try:
            status, _ = await disabled._receive_live_status(transcript())
            assert status == 403
        finally:
            await disabled.on_unload()

        enabled = make_plugin(FakeHost(), make_config())
        await enabled.on_load()
        try:
            status, _ = await enabled._receive_live_status(transcript(lines=[line(0, "viewer", "")]))
            assert status == 400
        finally:
            await enabled.on_unload()

    asyncio.run(scenario())
