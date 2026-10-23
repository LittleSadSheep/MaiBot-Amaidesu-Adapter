"""Amaidesu 桥接插件：直播那边的麦麦与 QQ 这边的麦麦互通。

两个方向：
- 主播 → QQ：插件在 MaiBot 插件进程里开一个 MCP 服务，Amaidesu 连入后，主播的工具
  列表里多出读群、发群、请 QQ 麦麦说话三个工具（业务逻辑在 ``bridge.py``）。
- 直播 → QQ 麦麦：Amaidesu 把开播、换环节、收播与场次回顾推到同一端口的
  ``/live-status``，插件缓存后在每次规划与回复前注入近况，并提供查询工具
  （近况逻辑在 ``live_status.py``）。
- 直播 → 长期记忆：收播后 Amaidesu 把整场记录推到同一接口，插件按原始时间回放进一个
  固定的直播间会话，由 MaiBot 自己的聊天摘要吸收进长期记忆（回放逻辑在 ``live_memory.py``）。

MCP 与 JSON 接口的服务外壳在 ``mcp_http.py``，本文件只负责配置、组件声明与生命周期。
"""

from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Field, HookHandler, MaiBotPlugin, MessageGateway, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType
from mcp import types
from pydantic import ValidationError

import asyncio

from .bridge import BridgeSettings, QQBridge
from .live_memory import LiveMemorySettings, LiveRoomReplayer, parse_transcript
from .live_status import LiveStatusSettings, LiveStatusStore, insert_status_item, parse_report
from .mcp_http import BridgeMcpServer


# Amaidesu 推送直播近况的接口路径（与 MCP 服务同一端口、同一令牌）
LIVE_STATUS_PATH = "/live-status"
# 注入条目使用的 MaiBot Context Item 协议版本；宿主协议升级后跳过注入，免得请求被判无效
_CONTEXT_ITEM_SCHEMA_VERSION = 1
# 直播间消息网关的组件名：回放整场记录时经它送入观众消息、接住主播发言
LIVE_ROOM_GATEWAY = "amaidesu_live_room"


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
    person_lookup_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="回想某个群友时，按群名片 / 昵称在这个群最近多少天的发言里找人",
    )


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


class LiveStatusConfig(PluginConfigBase):
    """直播近况：接收 Amaidesu 推来的近况，注入给麦麦并提供查询工具。"""

    __ui_label__ = "直播近况"

    enabled: bool = Field(
        default=True, description="是否接收 Amaidesu 推来的直播近况（关闭后接口拒收，注入与查询停用）"
    )
    inject_planner: bool = Field(default=True, description="是否在每次规划前把近况插进请求末尾")
    inject_replyer: bool = Field(default=True, description="是否在每次生成回复前把近况附进回复要求")
    recent_window_ms: int = Field(
        default=43_200_000,
        ge=0,
        description="下播后多久内仍把这场直播当作近况注入（毫秒，默认 12 小时）；0 = 下播即不再注入，只能用工具查",
    )
    stale_after_ms: int = Field(
        default=180_000,
        ge=30_000,
        description="直播中超过这么久没收到心跳，就视为状态不明、不再说自己正在直播（毫秒）",
    )
    recap_chars: int = Field(default=200, ge=20, le=2000, description="注入的回顾最多保留多少字")
    keep_recaps: int = Field(default=5, ge=1, le=20, description="缓存最近几场直播的回顾（查询工具最多往前查到这里）")


class LiveMemoryConfig(PluginConfigBase):
    """直播内容进长期记忆：把整场记录回放进直播间会话，由 MaiBot 的聊天摘要吸收。"""

    __ui_label__ = "直播进记忆"

    enabled: bool = Field(
        default=False,
        description=(
            "是否把 Amaidesu 推来的整场直播记录回放进直播间会话，让麦麦记住直播内容。"
            "开启前先在 adapter_policy.toml 放行本插件的网关与房间号"
        ),
    )
    default_platform: str = Field(default="bilibili", description="Amaidesu 没给出直播平台时用的平台名")
    account_id: str = Field(
        default="amaidesu",
        description="麦麦在直播平台上的账号标识，MaiBot 据此认出直播间里哪些话是麦麦自己说的；填主播账号的 UID 也可以",
    )
    default_room_id: str = Field(
        default="live",
        description="Amaidesu 没给出房间号时用的房间号（直播间会话的群号）",
    )
    merge_chars: int = Field(
        default=500,
        ge=50,
        le=4000,
        description="主播连续说的几句合成一条发言的字数上限；合并能减少 MaiBot 对每条发言做的人物事实提取",
    )
    store_wait_ms: int = Field(
        default=5_000,
        ge=0,
        description="每条主播发言写入前，最多等多久让前面的观众消息入库（毫秒）",
    )


class AmaidesuBridgeConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    takeover: TakeoverConfig = Field(default_factory=TakeoverConfig)
    live_status: LiveStatusConfig = Field(default_factory=LiveStatusConfig)
    live_memory: LiveMemoryConfig = Field(default_factory=LiveMemoryConfig)


def build_live_memory_settings(config: AmaidesuBridgeConfig) -> LiveMemorySettings:
    """把插件配置换算成回放参数。"""

    section = config.live_memory
    return LiveMemorySettings(
        default_platform=section.default_platform.strip() or "bilibili",
        account_id=section.account_id.strip() or "amaidesu",
        default_room_id=section.default_room_id.strip() or "live",
        merge_chars=section.merge_chars,
        store_wait_ms=section.store_wait_ms,
    )


def build_live_status_settings(config: AmaidesuBridgeConfig) -> LiveStatusSettings:
    """把插件配置换算成近况判断参数。"""

    section = config.live_status
    return LiveStatusSettings(
        recent_window_ms=section.recent_window_ms,
        stale_after_ms=section.stale_after_ms,
        recap_chars=section.recap_chars,
        keep_recaps=section.keep_recaps,
    )


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
        person_lookup_days=config.qq.person_lookup_days,
        takeover_enabled=config.takeover.enabled,
        takeover_talk_adjust=config.takeover.talk_adjust,
        takeover_duration_ms=config.takeover.duration_ms,
    )


class AmaidesuBridgePlugin(MaiBotPlugin):
    """装配桥接逻辑、直播近况与 MCP 服务，并在卸载时把接管的发言权全部还原。"""

    config_model = AmaidesuBridgeConfig

    def __init__(self) -> None:
        super().__init__()
        self._bridge: Optional[QQBridge] = None
        self._live_status: Optional[LiveStatusStore] = None
        self._live_memory: Optional[LiveRoomReplayer] = None
        self._server: Optional[BridgeMcpServer] = None
        self._server_key: Optional[Tuple[str, int, str]] = None
        self._takeover_task: Optional[asyncio.Task[None]] = None
        self._schema_mismatch_logged = False

    async def on_load(self) -> None:
        """加载顺序：建桥接逻辑 → 读回近况缓存 → 续放没放完的直播记录 → 起接管到期检查 → 开 MCP 服务。"""

        self._bridge = QQBridge(self.ctx, build_bridge_settings(self.config), logger=self.ctx.logger)
        # 近况写在插件数据目录：MaiBot 重启后仍记得上一场直播
        self._live_status = LiveStatusStore(
            self.ctx.paths.data_dir / "live_status.json",
            build_live_status_settings(self.config),
            logger=self.ctx.logger,
        )
        self._live_status.load()
        self._live_memory = LiveRoomReplayer(
            self.ctx,
            self.ctx.paths.data_dir,
            build_live_memory_settings(self.config),
            gateway_name=LIVE_ROOM_GATEWAY,
            logger=self.ctx.logger,
        )
        if self.config.live_memory.enabled:
            self._live_memory.start()
        self._takeover_task = asyncio.create_task(self._takeover_loop(), name="amaidesu_bridge.takeover")
        await self._start_server()

    async def on_unload(self) -> None:
        """卸载顺序：先关服务不再接新调用 → 停回放（进度已落盘）→ 停到期检查 → 还原所有接管。"""

        await self._stop_server()
        if self._live_memory is not None:
            await self._live_memory.stop()
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
        if self._live_status is not None:
            self._live_status.update_settings(build_live_status_settings(self.config))
        if self._live_memory is not None:
            self._live_memory.update_settings(build_live_memory_settings(self.config))
            if self.config.live_memory.enabled:
                self._live_memory.start()
            else:
                await self._live_memory.stop()
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
            json_posts={LIVE_STATUS_PATH: self._receive_live_status},
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

    # ──── 直播近况 ──────────────────────────────────────────

    async def _receive_live_status(self, body: Any) -> Tuple[int, Dict[str, Any]]:
        """Amaidesu 推来一份近况：校验后收进缓存（开播、换环节、心跳、收播、回顾都走这里）。

        整场记录也推到这里（``type="transcript"``），转给直播间回放。
        """

        if isinstance(body, dict) and body.get("type") == "transcript":
            return self._receive_transcript(body)
        if self._live_status is None or not self.config.live_status.enabled:
            return 403, {"ok": False, "error": "直播近况同步已关闭（live_status.enabled=false）"}
        try:
            report = parse_report(body)
        except ValidationError as exc:
            self.ctx.logger.warning(f"Amaidesu 推来的直播近况格式不对，已拒收: {exc}")
            return 400, {"ok": False, "error": f"近况格式不对: {exc.error_count()} 处错误"}
        self._live_status.apply(report)
        self.ctx.logger.debug(f"收到直播近况: type={report.type} 场次={report.live_session_id}")
        return 200, {"ok": True}

    def _receive_transcript(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if self._live_memory is None or not self.config.live_memory.enabled:
            return 403, {"ok": False, "error": "直播记录回放已关闭（live_memory.enabled=false）"}
        try:
            report = parse_transcript(body)
        except ValidationError as exc:
            self.ctx.logger.warning(f"Amaidesu 推来的整场记录格式不对，已拒收: {exc.error_count()} 处错误")
            return 400, {"ok": False, "error": f"整场记录格式不对: {exc.error_count()} 处错误"}
        return 200, {"ok": True, "detail": self._live_memory.submit(report)}

    def _injection_text(self, switch_on: bool) -> str:
        if not switch_on or self._live_status is None or not self.config.live_status.enabled:
            return ""
        return self._live_status.injection_text()

    @HookHandler(
        "maisaka.planner.before_request",
        description="在规划请求末尾插入直播那边的近况（正在直播 / 近期刚播完 / 状态不明时）",
    )
    async def inject_live_status_into_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """规划前：近况插在末尾每轮指令之前，让麦麦决定说什么时知道自己在不在直播。"""

        text = self._injection_text(self.config.live_status.inject_planner)
        items = kwargs.get("items")
        if not text or not isinstance(items, list):
            return {"action": "continue"}
        if kwargs.get("item_schema_version") != _CONTEXT_ITEM_SCHEMA_VERSION:
            if not self._schema_mismatch_logged:
                self._schema_mismatch_logged = True
                self.ctx.logger.warning(
                    f"MaiBot 上下文条目协议版本为 {kwargs.get('item_schema_version')!r}，"
                    f"本插件只认 {_CONTEXT_ITEM_SCHEMA_VERSION}，已跳过规划前的直播近况注入"
                )
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": insert_status_item(items, text)}}

    @HookHandler(
        "maisaka.replyer.before_request",
        description="在回复要求里附上直播那边的近况，让回复里的环节名、开播时刻不编错",
    )
    async def inject_live_status_into_replyer(self, **kwargs: Any) -> Dict[str, Any]:
        """回复前：回复生成只看得到规划器写下的想法，近况细节要单独附上；保留其他插件已有的要求。"""

        text = self._injection_text(self.config.live_status.inject_replyer)
        if not text:
            return {"action": "continue"}
        existing = str(kwargs.get("extra_prompt") or "").strip()
        combined = f"{existing}\n\n{text}" if existing else text
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": combined}}

    @Tool(
        "query_live_status",
        description=(
            "查看你在直播那边（虚拟主播）的近况：现在是否在直播、进行到哪个环节、最近几场直播做了什么。"
            "有人问你直播的事、或你想提起直播时先查一下，别凭印象说。"
        ),
        parameters=[
            ToolParameterInfo(
                name="n_back",
                param_type=ToolParamType.INTEGER,
                description="0 或不填 = 当前状态和最近一场的回顾；1、2… = 往前第几场直播的回顾",
                required=False,
            )
        ],
    )
    async def handle_query_live_status(self, n_back: Any = 0, **kwargs: Any) -> Dict[str, Any]:
        """查询直播近况：数据来自缓存，Amaidesu 没开也能回答。"""

        del kwargs
        if self._live_status is None or not self.config.live_status.enabled:
            return {"success": False, "error": "直播近况同步已关闭"}
        try:
            steps = int(n_back or 0)
        except (TypeError, ValueError):
            return {"success": False, "error": f"n_back 需要是整数，收到的是 {n_back!r}"}
        return {"success": True, "content": self._live_status.query_text(steps)}

    # ──── 直播间回放 ────────────────────────────────────────

    @MessageGateway(
        "duplex",
        name=LIVE_ROOM_GATEWAY,
        description="直播间：回放 Amaidesu 推来的整场直播记录，只收不发",
    )
    async def live_room_gateway(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """网关发送端：只接住正在回放的主播发言（不外发），麦麦自己想在直播间说的话一律回失败。"""

        del route, metadata, kwargs
        if self._live_memory is None:
            return {"success": False, "error": "直播间回放未初始化"}
        return self._live_memory.accept_outbound(message)

    @HookHandler(
        "send_service.after_build_message",
        description="回放直播记录时，把麦麦在直播间会话的发言时间改回直播里的原始时刻",
    )
    async def backdate_live_room_speech(self, **kwargs: Any) -> Dict[str, Any]:
        """每次发送都会经过这里；不是正在回放的那条主播发言就立即放行。"""

        message = kwargs.get("message")
        if self._live_memory is None or not isinstance(message, dict):
            return {"action": "continue"}
        claimed = self._live_memory.claim_outbound(str(kwargs.get("stream_id") or ""), message)
        if claimed is None:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": claimed}}

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
