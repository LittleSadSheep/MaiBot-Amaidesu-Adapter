"""插件生命周期与 MCP 服务的端到端测试：真实 SDK 上下文 + 真实 MCP 客户端。"""

from typing import Any, Dict, List

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import asyncio
import time
import socket

import httpx

from fake_host import GROUP_ID, STREAM_ID, FakeHost, load_plugin_package


plugin_module = load_plugin_package()
TOKEN = "test-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_config(port: int, token: str = TOKEN, **qq: Any) -> Dict[str, Any]:
    return {
        "plugin": {"enabled": True, "config_version": "1.0.0"},
        "server": {"host": "127.0.0.1", "port": port, "auth_token": token},
        "qq": {"group_ids": [GROUP_ID], **qq},
        "takeover": {"enabled": True, "talk_adjust": 0.0, "duration_ms": 600_000},
    }


def make_plugin(host: FakeHost, config: Dict[str, Any]) -> Any:
    plugin = plugin_module.create_plugin()
    plugin._set_context(host.make_context())
    plugin.set_plugin_config(config)
    return plugin


async def call_tools(url: str, token: str, calls: List[Any]) -> Any:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                results = [await session.call_tool(name, args) for name, args in calls]
                return tools, results


def test_plugin_serves_tools_over_mcp_and_restores_on_unload() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.adjust[STREAM_ID] = 0.7
        port = free_port()
        plugin = make_plugin(host, make_config(port, direct_send=True))
        await plugin.on_load()
        url = f"http://127.0.0.1:{port}/mcp"
        try:
            tools, results = await call_tools(
                url,
                TOKEN,
                [
                    ("send_group", {"text": "开播啦"}),
                    ("read_group", {"group_id": "424242"}),
                ],
            )
            assert {tool.name for tool in tools.tools} == {
                "read_group",
                "send_group",
                "ask_mai",
                "recall_memory",
                "recall_person",
            }
            sent, rejected = results
            assert not sent.isError and "已发到" in sent.content[0].text
            assert rejected.isError and "不在允许名单" in rejected.content[0].text
            assert host.adjust[STREAM_ID] == 0.0

            async with httpx.AsyncClient() as client:
                missing = await client.post(url, json={})
                wrong = await client.post(url, json={}, headers={"Authorization": "Bearer nope"})
            assert missing.status_code == 401 and wrong.status_code == 401
        finally:
            await plugin.on_unload()

        assert host.adjust[STREAM_ID] == 0.7
        # 卸载后端口释放，可以重新绑定。
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))

    asyncio.run(scenario())


def test_config_update_restarts_server_only_when_endpoint_changes() -> None:
    async def scenario() -> None:
        host = FakeHost()
        port = free_port()
        plugin = make_plugin(host, make_config(port))
        await plugin.on_load()
        url = f"http://127.0.0.1:{port}/mcp"
        try:
            first_server = plugin._server
            # 默认群里只留 QQ 麦麦一张嘴：工具清单不给发群，发来也拒绝。
            tools, results = await call_tools(url, TOKEN, [("send_group", {"text": "开播啦"})])
            assert "send_group" not in {tool.name for tool in tools.tools}
            assert results[0].isError and "ask_mai" in results[0].content[0].text
            assert host.calls_of("send.text") == []

            # 打开直接发群属于热更新：服务不重启，工具清单与字数上限立即生效。
            plugin.set_plugin_config(make_config(port, direct_send=True, max_text_length=2))
            await plugin.on_config_update("self", {}, "1.0.0")
            assert plugin._server is first_server
            tools, results = await call_tools(url, TOKEN, [("send_group", {"text": "三个字"})])
            assert "send_group" in {tool.name for tool in tools.tools}
            assert results[0].isError and "超过上限 2 字" in results[0].content[0].text

            plugin.set_plugin_config(make_config(port, token="rotated", direct_send=True))
            await plugin.on_config_update("self", {}, "1.0.0")
            assert plugin._server is not first_server
            tools, _ = await call_tools(url, "rotated", [])
            assert len(tools.tools) == 5
            async with httpx.AsyncClient() as client:
                old = await client.post(url, json={}, headers={"Authorization": f"Bearer {TOKEN}"})
            assert old.status_code == 401
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_empty_token_keeps_server_closed() -> None:
    async def scenario() -> None:
        host = FakeHost()
        plugin = make_plugin(host, make_config(free_port(), token=""))
        await plugin.on_load()
        try:
            assert plugin._server is None
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def _live_snapshot(**fields: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "type": "snapshot",
        "live": True,
        "live_session_id": 7,
        "title": "周五 MC 建房",
        "platform": "bilibili",
        "started_at_ms": int(time.time() * 1000) - 60_000,
        "segment": {"title": "搭屋顶", "index": 1, "total": 3},
    }
    body.update(fields)
    return body


def test_live_status_endpoint_feeds_hooks_and_query_tool() -> None:
    async def scenario() -> None:
        host = FakeHost()
        port = free_port()
        plugin = make_plugin(host, make_config(port))
        await plugin.on_load()
        url = f"http://127.0.0.1:{port}/live-status"
        auth = {"Authorization": f"Bearer {TOKEN}"}
        try:
            # 没有近况时两个钩子都原样放行，不改请求
            items = [{"item_type": "SystemMessageItem"}, {"item_type": "UserMessageItem"}]
            idle = await plugin.inject_context_into_planner(items=items, item_schema_version=1, session_id=STREAM_ID)
            assert idle == {"action": "continue"}
            assert await plugin.inject_context_into_replyer(extra_prompt="", session_id=STREAM_ID) == {
                "action": "continue"
            }

            async with httpx.AsyncClient() as client:
                assert (await client.post(url, json=_live_snapshot())).status_code == 401
                assert (await client.post(url, json={"type": "gossip"}, headers=auth)).status_code == 400
                bad_json = await client.post(url, content=b"{not json", headers=auth)
                assert bad_json.status_code == 400
                accepted = await client.post(url, json=_live_snapshot(), headers=auth)
                assert accepted.status_code == 200 and accepted.json() == {"ok": True}

            planner = await plugin.inject_context_into_planner(items=items, item_schema_version=1, session_id=STREAM_ID)
            new_items = planner["modified_kwargs"]["items"]
            assert len(new_items) == 3 and new_items[-1] == items[-1]
            assert "你此刻正在B站开直播（你自己是主播）《周五 MC 建房》" in new_items[1]["parts"][0]["text"]
            assert planner["modified_kwargs"]["session_id"] == STREAM_ID  # 其余参数原样带回
            # 白名单外的会话（别的群、私聊）不注入直播近况
            outside = await plugin.inject_context_into_planner(items=items, item_schema_version=1, session_id="私聊")
            assert outside == {"action": "continue"}
            assert await plugin.inject_context_into_replyer(extra_prompt="", session_id="私聊") == {
                "action": "continue"
            }

            # 宿主协议版本不认识时不冒险改请求
            other = await plugin.inject_context_into_planner(items=items, item_schema_version=2, session_id=STREAM_ID)
            assert other == {"action": "continue"}

            replyer = await plugin.inject_context_into_replyer(extra_prompt="别用表情", session_id=STREAM_ID)
            extra = replyer["modified_kwargs"]["extra_prompt"]
            assert extra.startswith("别用表情\n\n<live-status>") and "第 2/3 个环节「搭屋顶」" in extra

            answer = await plugin.handle_query_live_status(n_back=0)
            assert answer["success"] and "你此刻正在B站开直播" in answer["content"]
            assert (await plugin.handle_query_live_status(n_back="x"))["success"] is False

            # 关掉近况：接口拒收、注入与查询停用
            plugin.set_plugin_config(make_config(port) | {"live_status": {"enabled": False}})
            await plugin.on_config_update("self", {}, "1.0.0")
            async with httpx.AsyncClient() as client:
                assert (await client.post(url, json=_live_snapshot(), headers=auth)).status_code == 403
            assert await plugin.inject_context_into_planner(items=items, item_schema_version=1) == {
                "action": "continue"
            }
            assert (await plugin.handle_query_live_status())["success"] is False
        finally:
            await plugin.on_unload()

        # 近况落在插件数据目录，重新加载后还在
        reloaded = make_plugin(host, make_config(free_port()))
        await reloaded.on_load()
        try:
            assert "你此刻正在B站开直播" in (await reloaded.handle_query_live_status())["content"]
        finally:
            await reloaded.on_unload()

    asyncio.run(scenario())


NOTES = "## 答应过的事\n- 答应挖钻石〔未兑现 · 始于 #3〕\n- 答应老王玩恐怖游戏〔未兑现 · 对 老王#12〕\n\n## 不确定\n- 好像怕黑"


def test_shared_notes_endpoint_hooks_and_record_tool() -> None:
    """共享笔记：Amaidesu 经 /notes 读写正本（版本冲突回 409、带名字核对）；白名单群注入笔记、能记约定，
    白名单外不注入、记约定的工具从候选里拿掉且调用被拒；正本落盘，重新加载后还在。"""

    async def scenario() -> None:
        host = FakeHost()
        port = free_port()
        plugin = make_plugin(host, make_config(port))
        await plugin.on_load()
        url = f"http://127.0.0.1:{port}/notes"
        auth = {"Authorization": f"Bearer {TOKEN}"}
        items = [{"item_type": "SystemMessageItem"}, {"item_type": "UserMessageItem"}]
        try:
            async with httpx.AsyncClient(headers=auth) as client:
                got = (await client.post(url, json={"action": "get", "persona_name": "麦麦"})).json()
                assert got["text"] == "" and got["names"] == {"maibot": ["麦麦"], "match": True}
                stale = await client.post(
                    url, json={"action": "put", "text": NOTES, "expected_version": "旧的", "changes": []}
                )
                assert stale.status_code == 409 and stale.json()["text"] == ""
                put = await client.post(
                    url,
                    json={
                        "action": "put",
                        "text": NOTES,
                        "expected_version": got["version"],
                        "reason": "收播整理（场次 #5）",
                        "changes": ["新增"],
                        "persona_name": "小麦",
                    },
                )
                assert put.status_code == 200 and put.json()["text"] == NOTES
                assert put.json()["names"]["match"] is False  # 两边名字不一致
                assert (await client.post(url, json={"action": "delete"})).status_code == 400

            planner = await plugin.inject_context_into_planner(items=items, item_schema_version=1, session_id=STREAM_ID)
            new_items = planner["modified_kwargs"]["items"]
            notes_text = new_items[1]["parts"][0]["text"]  # 紧跟系统提示词
            assert len(new_items) == 3 and "- 答应挖钻石（还没兑现）" in notes_text
            assert "老王" not in notes_text and "好像怕黑" not in notes_text
            replyer = await plugin.inject_context_into_replyer(extra_prompt="", session_id=STREAM_ID)
            assert replyer["modified_kwargs"]["extra_prompt"].startswith("<shared-notes>")

            tools = [{"name": "reply"}, {"type": "function", "function": {"name": "record_promise"}}]
            outside = await plugin.inject_context_into_planner(
                items=items, item_schema_version=1, session_id="别的群", tool_definitions=tools
            )
            assert outside["modified_kwargs"]["tool_definitions"] == [{"name": "reply"}]
            assert "items" not in outside["modified_kwargs"] or outside["modified_kwargs"]["items"] == items

            recorded = await plugin.handle_record_promise(
                content="答应群友小明下次直播唱歌", group_id=GROUP_ID, platform="qq", stream_id=STREAM_ID
            )
            assert recorded["success"]
            refused = await plugin.handle_record_promise(content="答应别的群", group_id="99999", platform="qq")
            assert refused["success"] is False and "互通" in refused["error"]
            async with httpx.AsyncClient(headers=auth) as client:
                text = (await client.post(url, json={"action": "get"})).json()["text"]
            assert text.splitlines()[3] == "- 答应群友小明下次直播唱歌〔未兑现 · 来自 QQ群〕"
        finally:
            await plugin.on_unload()

        reloaded = make_plugin(host, make_config(free_port()))
        await reloaded.on_load()
        try:
            planner = await reloaded.inject_context_into_planner(
                items=items, item_schema_version=1, session_id=STREAM_ID
            )
            assert (
                "答应群友小明下次直播唱歌（还没兑现，来自 QQ群）"
                in planner["modified_kwargs"]["items"][1]["parts"][0]["text"]
            )
        finally:
            await reloaded.on_unload()

    asyncio.run(scenario())


def test_go_live_announcement_follows_live_status_endpoint() -> None:
    async def scenario() -> None:
        host = FakeHost()
        auth = {"Authorization": f"Bearer {TOKEN}"}
        config = {"go_live": {"group_ids": [GROUP_ID], "delay_ms": 0}}
        # 同一场的快照开播时刻固定：三次都推这一份，重启前后才是"同一场"
        live = _live_snapshot()

        # 默认不配通知群：收到开播也不喊
        port = free_port()
        quiet = make_plugin(host, make_config(port))
        await quiet.on_load()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(f"http://127.0.0.1:{port}/live-status", json=live, headers=auth)
            assert response.status_code == 200
            await quiet._go_live.tick()
            assert host.calls_of("maisaka.proactive.trigger") == []
        finally:
            await quiet.on_unload()

        # 配了通知群：经近况接口收到开播，到点后托 QQ 麦麦喊一次
        port = free_port()
        plugin = make_plugin(host, make_config(port) | config)
        await plugin.on_load()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(f"http://127.0.0.1:{port}/live-status", json=live, headers=auth)
            assert response.status_code == 200
            await plugin._go_live.tick()
            (call,) = host.calls_of("maisaka.proactive.trigger")
            assert call["stream_id"] == STREAM_ID and "开播了《周五 MC 建房》" in call["intent"]
        finally:
            await plugin.on_unload()

        # 喊过的场次落在插件数据目录：重启后同一场不再喊
        port = free_port()
        reloaded = make_plugin(host, make_config(port) | config)
        await reloaded.on_load()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(f"http://127.0.0.1:{port}/live-status", json=live, headers=auth)
            assert response.status_code == 200
            await reloaded._go_live.tick()
            assert len(host.calls_of("maisaka.proactive.trigger")) == 1
        finally:
            await reloaded.on_unload()

    asyncio.run(scenario())
