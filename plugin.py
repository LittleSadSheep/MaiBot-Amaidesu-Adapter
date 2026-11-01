"""Amaidesu 适配器：直播那边的麦麦与 QQ 这边的麦麦互通。

两个方向：
- 主播 → QQ：插件在 MaiBot 插件进程里开一个 MCP 服务，Amaidesu 连入后，主播的工具
  列表里多出读群、托 QQ 麦麦说话、回想 QQ 那边的记忆等工具（业务逻辑在 ``bridge.py``）。
  群里默认只有 QQ 麦麦一张嘴，主播亲手发群要另外打开 ``qq.direct_send``。
- 直播 → QQ 麦麦：Amaidesu 把开播、换环节、收播与场次回顾推到同一端口的
  ``/live-status``，插件缓存后在每次规划与回复前注入近况，并提供查询工具
  （近况逻辑在 ``live_status.py``）。
- 直播 → 长期记忆（可选）：收播后 Amaidesu 把整场记录推到同一接口，插件按原始时间回放进一个
  固定的直播间会话，由 MaiBot 自己的聊天摘要吸收进长期记忆（回放逻辑在 ``live_memory.py``）。
- 两边共用的长期笔记：主播笔记里"身份"那一层（答应过的事、老梗、长线目标、关于我自己）的
  正本存在这里，Amaidesu 经 ``/notes`` 读写；QQ 麦麦在白名单群里看得到、也能记下 QQ 里答应
  的事（正本逻辑在 ``shared_notes.py``，白名单判断在 ``scope.py``）。近况与笔记只注入白名单群。

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
from .live_status import LiveStatusSettings, LiveStatusStore, insert_notes_item, insert_status_item, parse_report
from .mcp_http import BridgeMcpServer
from .scope import WhitelistScope
from .shared_notes import NotesSettings, SharedNotesStore, names_check


# Amaidesu 推送直播近况的接口路径（与 MCP 服务同一端口、同一令牌）
LIVE_STATUS_PATH = "/live-status"
# Amaidesu 读写共享笔记正本的接口路径（同上）
NOTES_PATH = "/notes"
# QQ 麦麦记约定的工具名：白名单群外从规划候选里拿掉，调用也拒绝
RECORD_PROMISE_TOOL = "record_promise"
# 注入条目使用的 MaiBot Context Item 协议版本；宿主协议升级后跳过注入，免得请求被判无效
_CONTEXT_ITEM_SCHEMA_VERSION = 1
# 直播间消息网关的组件名：回放整场记录时经它送入观众消息、接住主播发言
LIVE_ROOM_GATEWAY = "amaidesu_live_room"


# 后台检查接管是否到期的间隔：到期后最多晚这么久把发言权还给 QQ 麦麦。
_TAKEOVER_CHECK_INTERVAL_S = 5.0


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"

    enabled: bool = Field(default=True, description="是否启用 Amaidesu 适配器")
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
    direct_send: bool = Field(
        default=False,
        description=(
            "是否给主播直接发群的工具。默认关：群里只留 QQ 麦麦一张嘴，主播想在群里说话就托它说；"
            "打开后主播能亲手发群，发群后按[水群接管]压低 QQ 麦麦在该群的发言"
        ),
    )
    max_text_length: int = Field(default=300, ge=1, le=4000, description="主播单条发群消息的字数上限（打开直接发群时）")
    min_send_interval_ms: int = Field(
        default=2000, ge=0, description="同一个群两次发群的最短间隔，毫秒（打开直接发群时）"
    )
    max_intent_length: int = Field(default=1000, ge=1, le=8000, description="托 QQ 麦麦说话时说明的字数上限")
    min_ask_interval_ms: int = Field(
        default=60_000,
        ge=0,
        description="同一个群两次托 QQ 麦麦说话的最短间隔，毫秒；免得主播每轮都托一次，把 QQ 麦麦刷成复读机",
    )
    person_lookup_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="回想某个群友时，按群名片 / 昵称在这个群最近多少天的发言里找人",
    )


class TakeoverConfig(PluginConfigBase):
    """主播水群期间对 MaiBot 发言频率的接管（只在打开 qq.direct_send 时起作用）。"""

    __ui_label__ = "水群接管"

    enabled: bool = Field(
        default=True, description="主播发群后是否压低 MaiBot 在该群的发言频率；没打开直接发群时不起作用"
    )
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
    keep_recaps: int = Field(
        default=0,
        ge=0,
        description="最多留几场直播的回顾（查询工具最多往前查到这里）；0 = 每场都留着",
    )


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


class NotesConfig(PluginConfigBase):
    """两边共用的长期笔记：正本存在这里，白名单群里的 QQ 麦麦看得到、记得进去。"""

    __ui_label__ = "共享笔记"

    enabled: bool = Field(
        default=True,
        description="是否保存 Amaidesu 主播的长期笔记正本（关闭后接口拒收，Amaidesu 退回只用本地笔记）",
    )
    inject_planner: bool = Field(default=True, description="是否在白名单群的每次规划前附上笔记")
    inject_replyer: bool = Field(default=True, description="是否在白名单群的每次生成回复前附上笔记")
    record_tool: bool = Field(default=True, description="是否允许白名单群里的麦麦把答应的事记进笔记")
    max_promise_chars: int = Field(default=80, ge=10, le=500, description="记一件约定的字数上限")
    max_open_qq_promises: int = Field(
        default=10,
        ge=1,
        le=100,
        description="QQ 这边记下、还没兑现的约定最多几件；满了先不记新的（直播那边收播整理时会处理旧的）",
    )


class AmaidesuAdapterConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    takeover: TakeoverConfig = Field(default_factory=TakeoverConfig)
    live_status: LiveStatusConfig = Field(default_factory=LiveStatusConfig)
    live_memory: LiveMemoryConfig = Field(default_factory=LiveMemoryConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)


def build_notes_settings(config: AmaidesuAdapterConfig) -> NotesSettings:
    """把插件配置换算成共享笔记参数。"""

    return NotesSettings(
        max_promise_chars=config.notes.max_promise_chars,
        max_open_qq_promises=config.notes.max_open_qq_promises,
    )


def build_live_memory_settings(config: AmaidesuAdapterConfig) -> LiveMemorySettings:
    """把插件配置换算成回放参数。"""

    section = config.live_memory
    return LiveMemorySettings(
        default_platform=section.default_platform.strip() or "bilibili",
        account_id=section.account_id.strip() or "amaidesu",
        default_room_id=section.default_room_id.strip() or "live",
        merge_chars=section.merge_chars,
        store_wait_ms=section.store_wait_ms,
    )


def build_live_status_settings(config: AmaidesuAdapterConfig) -> LiveStatusSettings:
    """把插件配置换算成近况判断参数。"""

    section = config.live_status
    return LiveStatusSettings(
        recent_window_ms=section.recent_window_ms,
        stale_after_ms=section.stale_after_ms,
        recap_chars=section.recap_chars,
        keep_recaps=section.keep_recaps,
    )


def build_bridge_settings(config: AmaidesuAdapterConfig) -> BridgeSettings:
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
        min_ask_interval_ms=config.qq.min_ask_interval_ms,
        person_lookup_days=config.qq.person_lookup_days,
        direct_send=config.qq.direct_send,
        takeover_enabled=config.takeover.enabled,
        takeover_talk_adjust=config.takeover.talk_adjust,
        takeover_duration_ms=config.takeover.duration_ms,
    )


class AmaidesuAdapterPlugin(MaiBotPlugin):
    """装配桥接逻辑、直播近况与 MCP 服务，并在卸载时把接管的发言权全部还原。"""

    config_model = AmaidesuAdapterConfig

    def __init__(self) -> None:
        super().__init__()
        self._bridge: Optional[QQBridge] = None
        self._live_status: Optional[LiveStatusStore] = None
        self._live_memory: Optional[LiveRoomReplayer] = None
        self._notes: Optional[SharedNotesStore] = None
        self._scope: Optional[WhitelistScope] = None
        self._reported_names: Optional[Tuple[str, Tuple[str, ...]]] = None
        self._server: Optional[BridgeMcpServer] = None
        self._server_key: Optional[Tuple[str, int, str]] = None
        self._takeover_task: Optional[asyncio.Task[None]] = None
        self._schema_mismatch_logged = False

    async def on_load(self) -> None:
        """加载顺序：建桥接逻辑与白名单 → 读回近况缓存与共享笔记 → 续放没放完的直播记录 → 起接管到期检查 → 开 MCP 服务。"""

        settings = build_bridge_settings(self.config)
        self._bridge = QQBridge(self.ctx, settings, logger=self.ctx.logger)
        self._scope = WhitelistScope(
            self.ctx, platform=settings.platform, group_ids=settings.group_ids, logger=self.ctx.logger
        )
        # 共享笔记正本写在插件数据目录：MaiBot 重启不丢，Amaidesu 不开时 QQ 麦麦照样读得到
        self._notes = SharedNotesStore(
            self.ctx.paths.data_dir / "notes", build_notes_settings(self.config), logger=self.ctx.logger
        )
        self._notes.load()
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
        self._takeover_task = asyncio.create_task(self._takeover_loop(), name="amaidesu_adapter.takeover")
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
        if self._scope is not None:
            settings = build_bridge_settings(self.config)
            self._scope.update(platform=settings.platform, group_ids=settings.group_ids)
        if self._notes is not None:
            self._notes.update_settings(build_notes_settings(self.config))
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
            self.ctx.logger.warning("Amaidesu 适配器未配置访问令牌（server.auth_token），MCP 服务不启动")
            return
        bridge = self._bridge
        server = BridgeMcpServer(
            host=host,
            port=port,
            token=token,
            list_tools=lambda: _to_mcp_tools(bridge),
            call_tool=bridge.call_tool,
            json_posts={LIVE_STATUS_PATH: self._receive_live_status, NOTES_PATH: self._receive_notes},
            logger=self.ctx.logger,
        )
        try:
            await server.start()
        except Exception as exc:
            self.ctx.logger.error(f"Amaidesu 适配器 MCP 服务启动失败（{host}:{port}）: {exc}", exc_info=True)
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

    def _notes_text(self, switch_on: bool) -> str:
        if not switch_on or self._notes is None or not self.config.notes.enabled:
            return ""
        return self._notes.injection_text()

    async def _in_scope(self, session_id: Any) -> bool:
        return self._scope is not None and await self._scope.contains(str(session_id or ""))

    @HookHandler(
        "maisaka.planner.before_request",
        description="白名单群的规划请求里附上共享笔记与直播近况；白名单外拿掉记约定的工具",
    )
    async def inject_context_into_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """规划前：笔记插在系统提示词之后（整份不常变，吃得上前缀缓存），近况插在末尾每轮指令之前。

        只对白名单群（qq.group_ids）注入；其他会话不注入，并把记约定的工具从候选里拿掉。
        """

        if not await self._in_scope(kwargs.get("session_id")):
            tools = kwargs.get("tool_definitions")
            if isinstance(tools, list):
                kept = [item for item in tools if _tool_name(item) != RECORD_PROMISE_TOOL]
                if len(kept) != len(tools):
                    return {"action": "continue", "modified_kwargs": {**kwargs, "tool_definitions": kept}}
            return {"action": "continue"}
        notes = self._notes_text(self.config.notes.inject_planner)
        status = self._injection_text(self.config.live_status.inject_planner)
        items = kwargs.get("items")
        if not (notes or status) or not isinstance(items, list):
            return {"action": "continue"}
        if kwargs.get("item_schema_version") != _CONTEXT_ITEM_SCHEMA_VERSION:
            if not self._schema_mismatch_logged:
                self._schema_mismatch_logged = True
                self.ctx.logger.warning(
                    f"MaiBot 上下文条目协议版本为 {kwargs.get('item_schema_version')!r}，"
                    f"本插件只认 {_CONTEXT_ITEM_SCHEMA_VERSION}，已跳过规划前的笔记与直播近况注入"
                )
            return {"action": "continue"}
        if notes:
            items = insert_notes_item(items, notes)
        if status:
            items = insert_status_item(items, status)
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": items}}

    @HookHandler(
        "maisaka.replyer.before_request",
        description="白名单群的回复要求里附上共享笔记与直播近况，让回复里的约定、环节名、开播时刻不编错",
    )
    async def inject_context_into_replyer(self, **kwargs: Any) -> Dict[str, Any]:
        """回复前：回复生成只看得到规划器写下的想法，笔记与近况细节要单独附上；保留其他插件已有的要求。"""

        if not await self._in_scope(kwargs.get("session_id")):
            return {"action": "continue"}
        added = [
            text
            for text in (
                self._notes_text(self.config.notes.inject_replyer),
                self._injection_text(self.config.live_status.inject_replyer),
            )
            if text
        ]
        if not added:
            return {"action": "continue"}
        existing = str(kwargs.get("extra_prompt") or "").strip()
        combined = "\n\n".join([existing, *added] if existing else added)
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": combined}}

    # ──── 共享笔记 ──────────────────────────────────────────

    async def _receive_notes(self, body: Any) -> Tuple[int, Dict[str, Any]]:
        """Amaidesu 读写共享笔记正本：``get`` 取全文，``put`` 带版本号整份写（版本对不上回 409）。

        每次都带上主播名，与 MaiBot 的昵称核对后放进应答，两边名字不一致时双方各告警一次。
        """

        if self._notes is None or not self.config.notes.enabled:
            return 403, {"ok": False, "error": "共享笔记已关闭（notes.enabled=false）"}
        if not isinstance(body, dict) or body.get("action") not in ("get", "put"):
            return 400, {"ok": False, "error": "action 只能是 get 或 put"}
        names = await self._check_names(str(body.get("persona_name") or ""))
        if body["action"] == "put":
            text, expected = body.get("text"), body.get("expected_version")
            changes = body.get("changes") or []
            if not isinstance(text, str) or not isinstance(expected, str) or not isinstance(changes, list):
                return 400, {"ok": False, "error": "put 需要 text、expected_version（字符串）与 changes（列表）"}
            written = self._notes.write(
                text,
                expected_version=expected,
                reason=f"Amaidesu：{str(body.get('reason') or '').strip() or '写入'}",
                changes=[str(item) for item in changes],
            )
            if not written:
                return 409, {
                    "ok": False,
                    "error": "共享笔记在读出之后被改过，请重新读取",
                    "text": self._notes.text,
                    "version": self._notes.version,
                    "names": names,
                }
        return 200, {"ok": True, "text": self._notes.text, "version": self._notes.version, "names": names}

    async def _check_names(self, persona_name: str) -> Dict[str, Any]:
        """Amaidesu 的主播名是否是 MaiBot 的昵称或别名之一；对不上时告警（同一组名字只说一次）。"""

        nickname = await self.ctx.config.get("bot.nickname", "")
        aliases = await self.ctx.config.get("bot.alias_names", [])
        maibot_names = [str(nickname or ""), *(str(item) for item in (aliases or []) if item)]
        result = names_check(persona_name, maibot_names)
        key = (persona_name.strip(), tuple(result["maibot"]))
        if result["match"] is False and key != self._reported_names:
            self._reported_names = key
            self.ctx.logger.warning(
                f"Amaidesu 的主播叫「{persona_name.strip()}」，MaiBot 叫「{'、'.join(result['maibot']) or '（没配）'}」："
                "两个身体共用一份笔记，名字要一致——请手动对齐 bot.nickname 与 Amaidesu 人设的 bot_name"
            )
        return result

    @Tool(
        RECORD_PROMISE_TOOL,
        description=(
            "把你刚在群里明确答应、以后要在直播里兑现的一件事，记进你和直播那边共用的笔记，"
            "下次开播时直播那边的你也会知道。只记你自己答应了、而且做得到的事；群友的请求、玩笑、"
            "客套不算，没答应的别记。只在和直播互通的群里能用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="答应了什么，写成一句完整的话，答应谁就写上谁，如「答应群友小明下次直播陪他玩恐怖游戏」",
                required=True,
            )
        ],
    )
    async def handle_record_promise(self, content: Any = "", **kwargs: Any) -> Dict[str, Any]:
        """记一件约定：只在白名单群里能用（按宿主给的群号判断），写进共享笔记的"答应过的事"。"""

        if self._notes is None or self._scope is None or not self.config.notes.enabled:
            return {"success": False, "error": "共享笔记已关闭"}
        if not self.config.notes.record_tool:
            return {"success": False, "error": "记约定的功能没开"}
        group_id = str(kwargs.get("group_id") or "")
        if not self._scope.allows_group(group_id, str(kwargs.get("platform") or "")):
            return {"success": False, "error": "只能在和直播互通的群里记约定"}
        group_name = self._scope.group_name(group_id)
        result = self._notes.add_promise(str(content or ""), where=f"「{group_name}」" if group_name else group_id)
        if result.ok:
            self.ctx.logger.info(f"QQ 群 {group_id} 里记下一件约定: {str(content)[:60]}")
            return {"success": True, "content": result.message}
        return {"success": False, "error": result.message}

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
            ),
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="想找哪场直播时填关键词（如游戏名、某件事），返回回顾里提到它的场次；填了就不看 n_back",
                required=False,
            ),
        ],
    )
    async def handle_query_live_status(self, n_back: Any = 0, keyword: Any = "", **kwargs: Any) -> Dict[str, Any]:
        """查询直播近况：数据来自缓存，Amaidesu 没开也能回答。"""

        del kwargs
        if self._live_status is None or not self.config.live_status.enabled:
            return {"success": False, "error": "直播近况同步已关闭"}
        try:
            steps = int(n_back or 0)
        except (TypeError, ValueError):
            return {"success": False, "error": f"n_back 需要是整数，收到的是 {n_back!r}"}
        return {"success": True, "content": self._live_status.query_text(steps, str(keyword or ""))}

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


def _tool_name(definition: Any) -> str:
    """工具定义里的名字（兼容 ``{"name": ...}`` 与 OpenAI function 结构）。"""

    if not isinstance(definition, dict):
        return ""
    function = definition.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return str(definition.get("name") or "")


def _to_mcp_tools(bridge: QQBridge) -> List[types.Tool]:
    return [
        types.Tool(name=item.name, description=item.description, inputSchema=item.input_schema)
        for item in bridge.tool_definitions()
    ]


def create_plugin() -> AmaidesuAdapterPlugin:
    """创建插件实例。"""

    return AmaidesuAdapterPlugin()
