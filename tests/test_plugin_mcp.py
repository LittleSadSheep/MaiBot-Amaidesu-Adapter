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
        plugin = make_plugin(host, make_config(port))
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
            assert {tool.name for tool in tools.tools} == {"read_group", "send_group", "ask_mai"}
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
            plugin.set_plugin_config(make_config(port, max_text_length=2))
            await plugin.on_config_update("self", {}, "1.0.0")
            assert plugin._server is first_server
            _, results = await call_tools(url, TOKEN, [("send_group", {"text": "三个字"})])
            assert results[0].isError and "超过上限 2 字" in results[0].content[0].text

            plugin.set_plugin_config(make_config(port, token="rotated"))
            await plugin.on_config_update("self", {}, "1.0.0")
            assert plugin._server is not first_server
            tools, _ = await call_tools(url, "rotated", [])
            assert len(tools.tools) == 3
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
            idle = await plugin.inject_live_status_into_planner(items=items, item_schema_version=1, session_id="s")
            assert idle == {"action": "continue"}
            assert await plugin.inject_live_status_into_replyer(extra_prompt="", session_id="s") == {
                "action": "continue"
            }

            async with httpx.AsyncClient() as client:
                assert (await client.post(url, json=_live_snapshot())).status_code == 401
                assert (await client.post(url, json={"type": "gossip"}, headers=auth)).status_code == 400
                bad_json = await client.post(url, content=b"{not json", headers=auth)
                assert bad_json.status_code == 400
                accepted = await client.post(url, json=_live_snapshot(), headers=auth)
                assert accepted.status_code == 200 and accepted.json() == {"ok": True}

            planner = await plugin.inject_live_status_into_planner(items=items, item_schema_version=1, session_id="s")
            new_items = planner["modified_kwargs"]["items"]
            assert len(new_items) == 3 and new_items[-1] == items[-1]
            assert "你此刻正在B站开直播（你自己是主播）《周五 MC 建房》" in new_items[1]["parts"][0]["text"]
            assert planner["modified_kwargs"]["session_id"] == "s"  # 其余参数原样带回

            # 宿主协议版本不认识时不冒险改请求
            other = await plugin.inject_live_status_into_planner(items=items, item_schema_version=2, session_id="s")
            assert other == {"action": "continue"}

            replyer = await plugin.inject_live_status_into_replyer(extra_prompt="别用表情", session_id="s")
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
            assert await plugin.inject_live_status_into_planner(items=items, item_schema_version=1) == {
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
