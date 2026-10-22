"""插件生命周期与 MCP 服务的端到端测试：真实 SDK 上下文 + 真实 MCP 客户端。"""

from typing import Any, Dict, List

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import asyncio
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
