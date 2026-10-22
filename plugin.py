"""Amaidesu 桥接插件：让直播那边的麦麦能读写 QQ 群、请 QQ 麦麦说话。

插件在 MaiBot 插件进程里开一个 MCP 服务，Amaidesu 作为 MCP 客户端连入后，
主播的工具列表里就多出读群、发群、请 QQ 麦麦说话三个工具。工具的业务逻辑在
``bridge.py``，MCP 服务外壳在 ``mcp_http.py``，本文件只负责配置与生命周期。
"""

from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase
from mcp import types

import asyncio

from .bridge import BridgeSettings, QQBridge
from .mcp_http import BridgeMcpServer


# 后台检查接管是否到期的间隔：到期后最多晚这么久把发言权还给 QQ 麦麦。
_TAKEOVER_CHECK_INTERVAL_S = 5.0


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"

    enabled: bool = Field(default=True, description="是否启用 Amaidesu 桥接插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ServerConfig(PluginConfigBase):
    """MCP 服务监听配置。"""

    __ui_label__ = "MCP 服务"

    host: str = Field(
        default="127.0.0.1",
        description="监听地址；Amaidesu 与 MaiBot 不在同一台机器时改成 0.0.0.0，并只在可信网络里使用",
    )
    port: int = Field(default=8767, ge=1, le=65535, description="监听端口，Amaidesu 侧 MCP 地址要与之一致")
    auth_token: str = Field(
        default="",
        description="访问令牌（Amaidesu 侧以 Authorization: Bearer <令牌> 携带）；为空时不启动服务",
        json_schema_extra={"input_type": "password", "label": "访问令牌"},
    )


class QQConfig(PluginConfigBase):
    """主播可操作的 QQ 群与读写限制。"""

    __ui_label__ = "QQ 群"

    platform: str = Field(default="qq", description="群聊所在平台名（与 MaiBot 适配器一致）")
    group_ids: List[str] = Field(
        default_factory=list,
        description="允许主播读写的群号；第一个是默认群。名单外的群一律拒绝",
    )
    read_default_limit: int = Field(default=20, ge=1, le=200, description="读群默认返回条数")
    read_max_limit: int = Field(default=50, ge=1, le=200, description="读群单次最多返回条数")
    read_lookback_ms: int = Field(
        default=7_200_000,
        ge=60_000,
        description="首次读群（没有已读位置时）回看多久的消息，毫秒",
    )
    max_text_length: int = Field(default=300, ge=1, le=4000, description="主播单条发群消息的字数上限")
    min_send_interval_ms: int = Field(default=2000, ge=0, description="同一个群两次发群的最短间隔，毫秒")
    max_intent_length: int = Field(default=1000, ge=1, le=8000, description="请 QQ 麦麦说话时意图说明的字数上限")


class TakeoverConfig(PluginConfigBase):
    """主播水群期间对 MaiBot 发言频率的接管。"""

    __ui_label__ = "水群接管"

    enabled: bool = Field(default=True, description="主播发群后是否压低 MaiBot 在该群的发言频率")
    talk_adjust: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="接管期间的发言频率倍率：0 = 只看不说（@ 也不回）；0~1 = 按比例少说，@ 与提及仍按 MaiBot 配置回复",
    )
    duration_ms: int = Field(
        default=600_000,
        ge=10_000,
        description="主播最后一次发群后接管持续多久，毫秒；到期自动还原",
    )


class AmaidesuBridgeConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    takeover: TakeoverConfig = Field(default_factory=TakeoverConfig)


def build_bridge_settings(config: AmaidesuBridgeConfig) -> BridgeSettings:
    """把插件配置换算成桥接运行参数（群号去空白、去重，保留先后顺序）。"""

    group_ids = tuple(dict.fromkeys(item.strip() for item in config.qq.group_ids if item.strip()))
    return BridgeSettings(
        platform=config.qq.platform.strip(),
        group_ids=group_ids,
        read_default_limit=min(config.qq.read_default_limit, config.qq.read_max_limit),
        read_max_limit=config.qq.read_max_limit,
        read_lookback_ms=config.qq.read_lookback_ms,
        max_text_length=config.qq.max_text_length,
        min_send_interval_ms=config.qq.min_send_interval_ms,
        max_intent_length=config.qq.max_intent_length,
        takeover_enabled=config.takeover.enabled,
        takeover_talk_adjust=config.takeover.talk_adjust,
        takeover_duration_ms=config.takeover.duration_ms,
    )


class AmaidesuBridgePlugin(MaiBotPlugin):
    """装配桥接逻辑与 MCP 服务，并在卸载时把接管的发言权全部还原。"""

    config_model = AmaidesuBridgeConfig

    def __init__(self) -> None:
        super().__init__()
        self._bridge: Optional[QQBridge] = None
        self._server: Optional[BridgeMcpServer] = None
        self._server_key: Optional[Tuple[str, int, str]] = None
        self._takeover_task: Optional[asyncio.Task[None]] = None

    async def on_load(self) -> None:
        """加载顺序：建桥接逻辑 → 起接管到期检查 → 开 MCP 服务。"""

        self._bridge = QQBridge(self.ctx, build_bridge_settings(self.config), logger=self.ctx.logger)
        self._takeover_task = asyncio.create_task(self._takeover_loop(), name="amaidesu_bridge.takeover")
        await self._start_server()

    async def on_unload(self) -> None:
        """卸载顺序：先关服务不再接新调用 → 停到期检查 → 还原所有接管。"""

        await self._stop_server()
        if self._takeover_task is not None:
            self._takeover_task.cancel()
            try:
                await self._takeover_task
            except asyncio.CancelledError:
                pass
            self._takeover_task = None
        if self._bridge is not None:
            await self._bridge.release_all()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """自身配置变更：群名单等立即生效；监听地址或令牌变了才重启服务。"""

        del config_data, version
        if scope != "self" or self._bridge is None:
            return
        self._bridge.update_settings(build_bridge_settings(self.config))
        if self._current_server_key() != self._server_key:
            await self._stop_server()
            await self._start_server()

    def _current_server_key(self) -> Tuple[str, int, str]:
        server = self.config.server
        return server.host.strip(), server.port, server.auth_token.strip()

    async def _start_server(self) -> None:
        assert self._bridge is not None
        host, port, token = self._current_server_key()
        self._server_key = (host, port, token)
        if not token:
            self.ctx.logger.warning("Amaidesu 桥接未配置访问令牌（server.auth_token），MCP 服务不启动")
            return
        bridge = self._bridge
        server = BridgeMcpServer(
            host=host,
            port=port,
            token=token,
            list_tools=lambda: _to_mcp_tools(bridge),
            call_tool=bridge.call_tool,
            logger=self.ctx.logger,
        )
        try:
            await server.start()
        except Exception as exc:
            self.ctx.logger.error(f"Amaidesu 桥接 MCP 服务启动失败（{host}:{port}）: {exc}", exc_info=True)
            return
        self._server = server

    async def _stop_server(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def _takeover_loop(self) -> None:
        """定时还原到期的接管；单次失败只记日志，不中断循环。"""

        while True:
            await asyncio.sleep(_TAKEOVER_CHECK_INTERVAL_S)
            if self._bridge is None:
                continue
            try:
                await self._bridge.expire_takeovers()
            except Exception as exc:
                self.ctx.logger.error(f"检查水群接管到期时出错: {exc}", exc_info=True)


def _to_mcp_tools(bridge: QQBridge) -> List[types.Tool]:
    return [
        types.Tool(name=item.name, description=item.description, inputSchema=item.input_schema)
        for item in bridge.tool_definitions()
    ]


def create_plugin() -> AmaidesuBridgePlugin:
    """创建插件实例。"""

    return AmaidesuBridgePlugin()
