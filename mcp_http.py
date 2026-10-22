"""插件进程内的 MCP 服务（Streamable HTTP，无状态 JSON 响应）。

MaiBot 常驻、Amaidesu 用时才启动，所以由本插件开端口，Amaidesu 作为 MCP 客户端
按需连入。本模块只负责"开端口、验令牌、转发工具调用"，不认识任何具体工具。
"""

from contextlib import asynccontextmanager, contextmanager
from ipaddress import ip_address
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterator, List, Optional, Tuple

from mcp import types
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

import asyncio
import logging
import secrets
import socket
import uvicorn


MCP_PATH = "/mcp"
_START_TIMEOUT_S = 5.0
_STOP_TIMEOUT_S = 3.0

ListToolsFn = Callable[[], List[types.Tool]]
CallToolFn = Callable[[str, Dict[str, Any]], Awaitable[Tuple[str, bool]]]


class _EmbeddedUvicornServer(uvicorn.Server):
    """插件子进程里的内嵌服务：信号归 MaiBot 插件进程处理，不让 uvicorn 接管 Ctrl+C。"""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class BridgeMcpServer:
    """一个带 Bearer 令牌校验的本机/局域网 MCP 服务。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        list_tools: ListToolsFn,
        call_tool: CallToolFn,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not token:
            raise ValueError("访问令牌为空，拒绝启动 MCP 服务")
        self._host = host
        self._port = port
        self._token = token
        self._list_tools = list_tools
        self._call_tool = call_tool
        self._logger = logger or logging.getLogger("plugin.amaidesu_bridge")
        self._server: Optional[_EmbeddedUvicornServer] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._socket: Optional[socket.socket] = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}{MCP_PATH}"

    @property
    def port(self) -> int:
        """实际监听端口（配置为 0 时由系统分配，测试用）。"""

        if self._socket is not None:
            return int(self._socket.getsockname()[1])
        return self._port

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _build_app(self) -> Starlette:
        """每次启动都新建会话管理器：它的 run() 一个实例只能进入一次。"""

        server: Server = Server("amaidesu-bridge")
        list_tools_fn = self._list_tools
        call_tool_fn = self._call_tool

        @server.list_tools()
        async def handle_list_tools() -> List[types.Tool]:
            return list_tools_fn()

        @server.call_tool()
        async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> types.CallToolResult:
            text, is_error = await call_tool_fn(name, arguments or {})
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=is_error)

        if _is_loopback(self._host):
            # 只在本机监听时开启 DNS 重绑定防护；对外监听时由令牌校验把关。
            security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"],
                allowed_origins=[],
            )
        else:
            security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True, security_settings=security)
        endpoint = StreamableHTTPASGIApp(manager)
        expected = f"Bearer {self._token}".encode()

        class AuthorizedEndpoint:
            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                headers = dict(scope["headers"])
                if not secrets.compare_digest(headers.get(b"authorization", b""), expected):
                    await Response(status_code=401)(scope, receive, send)
                    return
                if scope["method"] == "GET":
                    # 无状态工具入口只做请求/响应，不建立服务端推送流。
                    await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
                    return
                await endpoint(scope, receive, send)

        @asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            async with manager.run():
                yield

        return Starlette(
            routes=[Route(MCP_PATH, AuthorizedEndpoint(), methods=["GET", "POST", "DELETE"])],
            lifespan=lifespan,
        )

    async def start(self) -> None:
        """绑定端口并等服务就绪；端口被占用等错误直接抛出。"""

        if self.running:
            return
        family = socket.AF_INET6 if ":" in self._host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.bind((self._host, self._port))
        except OSError:
            sock.close()
            raise
        self._socket = sock
        app = self._build_app()
        config = uvicorn.Config(app, log_config=None, access_log=False, log_level="warning", ws="none", lifespan="on")
        self._server = _EmbeddedUvicornServer(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]), name="amaidesu_bridge.mcp")
        try:
            await asyncio.wait_for(self._wait_started(), _START_TIMEOUT_S)
        except BaseException:
            await self.stop()
            raise
        self._logger.info(f"Amaidesu 桥接 MCP 服务已启动: {self.url}")

    async def _wait_started(self) -> None:
        assert self._server is not None and self._task is not None
        while not self._server.started:
            if self._task.done():
                await self._task
                raise RuntimeError("Amaidesu 桥接 MCP 服务未能启动")
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        """请求服务退出，超时则取消任务，最后释放端口。"""

        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), _STOP_TIMEOUT_S)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            except Exception as exc:
                self._logger.error(f"Amaidesu 桥接 MCP 服务退出时出错: {exc}", exc_info=True)
        if self._socket is not None:
            self._socket.close()
        self._server = None
        self._task = None
        self._socket = None
