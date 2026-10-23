"""Amaidesu 主播使用的 QQ 群能力。

直播那边的麦麦（Amaidesu 主播）通过本插件的 MCP 服务调用三件事：
读群（看看群里在聊什么）、发群（直播时水群）、请 QQ 麦麦说话（交代意图，
由常驻 QQ 的麦麦用自己的群聊语气表达，例如开播前喊人）。

同一个 QQ 号背后有两个决策循环：主播在直播里水群时，MaiBot 自己的群聊循环
也在看同一个群。为避免两边抢话，主播发群后本模块会在一段时间内压低 MaiBot
在该群的发言频率（接管），到期、主播改请 QQ 麦麦说话或插件卸载时还原。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import asyncio
import logging
import time


# 主播发出的消息写进 MaiBot 群上下文时使用的来源标记，便于在 MaiBot 日志与上下文里辨认。
SEND_SOURCE_KIND = "amaidesu_bridge"


class ToolCallError(Exception):
    """工具调用失败，消息文本会原样返回给调用方的模型。"""


@dataclass(frozen=True)
class BridgeSettings:
    """桥接行为的运行参数（由插件配置换算而来）。"""

    platform: str = "qq"
    group_ids: Tuple[str, ...] = ()
    read_default_limit: int = 20
    read_max_limit: int = 50
    read_lookback_ms: int = 2 * 60 * 60 * 1000
    max_text_length: int = 300
    min_send_interval_ms: int = 2000
    max_intent_length: int = 1000
    takeover_enabled: bool = True
    takeover_talk_adjust: float = 0.0
    takeover_duration_ms: int = 10 * 60 * 1000


@dataclass
class _Takeover:
    """一个群的发言权接管记录。"""

    stream_id: str
    original_adjust: float
    expires_at_ms: int


@dataclass
class _GroupTarget:
    """解析后的目标群。"""

    group_id: str
    stream_id: str
    group_name: str

    @property
    def label(self) -> str:
        """给模型看的群称呼：有群名用群名，否则用群号。"""

        return f"「{self.group_name}」（{self.group_id}）" if self.group_name else f"群 {self.group_id}"


@dataclass
class _ToolDefinition:
    """一个对外暴露的工具定义（名称、说明、参数 JSON Schema）。"""

    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_failure(result: Any) -> bool:
    """SDK 能力失败时返回 ``{"success": False, "error": ...}``，成功时返回解包后的值。"""

    return isinstance(result, dict) and result.get("success") is False


def _failure_reason(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("error") or "未知原因")
    return "未知原因"


class QQBridge:
    """读群、发群、请 QQ 麦麦说话，以及水群期间的发言权接管。

    ``ctx`` 是 MaiBot SDK 注入的插件上下文（只用到 chat / message / send /
    frequency / maisaka / config 六组能力），测试中可替换为假实现。
    """

    def __init__(
        self,
        ctx: Any,
        settings: BridgeSettings,
        *,
        logger: Optional[logging.Logger] = None,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._ctx = ctx
        self._settings = settings
        self._logger = logger or logging.getLogger("plugin.amaidesu_bridge")
        self._clock_ms = clock_ms
        # 每个群聊流的已读位置（毫秒）：主播看过之后，下次默认只给新消息。
        self._read_cursors: Dict[str, int] = {}
        # 每个群聊流最近一次发群时刻（毫秒），用于限制发送间隔。
        self._last_send_ms: Dict[str, int] = {}
        self._takeovers: Dict[str, _Takeover] = {}
        self._takeover_lock = asyncio.Lock()

    @property
    def settings(self) -> BridgeSettings:
        return self._settings

    def update_settings(self, settings: BridgeSettings) -> None:
        """热更新运行参数；已读位置与进行中的接管保持不变。"""

        self._settings = settings

    # ──── 工具清单与分发 ────────────────────────────────────

    def tool_definitions(self) -> List[_ToolDefinition]:
        """按当前配置生成工具定义：可用群号写进说明，模型不用猜。"""

        settings = self._settings
        groups_text = "、".join(settings.group_ids) if settings.group_ids else "（尚未配置）"
        default_group = settings.group_ids[0] if settings.group_ids else ""
        group_property = {
            "type": "string",
            "description": f"QQ 群号，可用：{groups_text}；不填则用默认群 {default_group}",
        }
        takeover_hint = ""
        if settings.takeover_enabled:
            minutes = max(1, round(settings.takeover_duration_ms / 60000))
            takeover_hint = f"发出后约 {minutes} 分钟内，QQ 那边的你在这个群会少说话，由你来聊，避免两个你抢话。"
        return [
            _ToolDefinition(
                name="read_group",
                description=(
                    "读取 QQ 群的聊天记录，看看群友在聊什么。默认只返回你上次查看之后的新消息"
                    "（第一次查看返回最近一段时间的消息）。QQ 那边的你和你是同一个麦麦，"
                    "记录开头会说明哪个署名是你自己。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "group_id": group_property,
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": settings.read_max_limit,
                            "description": f"最多返回几条，默认 {settings.read_default_limit}",
                        },
                        "only_new": {
                            "type": "boolean",
                            "description": "是否只看上次查看之后的新消息，默认 true；false 则返回最近的消息",
                        },
                    },
                },
            ),
            _ToolDefinition(
                name="send_group",
                description=(
                    "用你自己的 QQ 号在 QQ 群里发一条文字消息，适合直播时水群、和群友聊天。"
                    f"单条不超过 {settings.max_text_length} 字。{takeover_hint}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要发的消息原文"},
                        "group_id": group_property,
                    },
                    "required": ["text"],
                },
            ),
            _ToolDefinition(
                name="ask_mai",
                description=(
                    "请常驻 QQ 的你（QQ 那边的麦麦）在群里自己处理一件事，比如开播前喊大家来看直播。"
                    "只交代目的和要点（例如直播间链接），怎么说、说不说由 QQ 那边的你按群里的气氛决定。"
                    "调用后这个群的发言权交还给 QQ 那边的你。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "intent": {"type": "string", "description": "想让 QQ 那边的你做什么、要点是什么"},
                        "group_id": group_property,
                    },
                    "required": ["intent"],
                },
            ),
        ]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Tuple[str, bool]:
        """执行一个工具，返回（给模型看的结果文本, 是否失败）。"""

        try:
            if name == "read_group":
                text = await self.read_group(
                    group_id=_as_text(arguments.get("group_id")),
                    limit=_as_optional_int(arguments.get("limit")),
                    only_new=_as_bool(arguments.get("only_new"), default=True),
                )
            elif name == "send_group":
                text = await self.send_group(
                    text=_as_text(arguments.get("text")),
                    group_id=_as_text(arguments.get("group_id")),
                )
            elif name == "ask_mai":
                text = await self.ask_mai(
                    intent=_as_text(arguments.get("intent")),
                    group_id=_as_text(arguments.get("group_id")),
                )
            else:
                raise ToolCallError(f"没有名为 {name} 的工具")
        except ToolCallError as exc:
            self._logger.info(f"Amaidesu 调用 {name} 未成功: {exc}")
            return str(exc), True
        except Exception as exc:
            self._logger.error(f"Amaidesu 调用 {name} 出错: {exc}", exc_info=True)
            return f"MaiBot 处理 {name} 时出错：{exc}", True
        return text, False

    # ──── 三个工具 ──────────────────────────────────────────

    async def read_group(self, group_id: str = "", limit: Optional[int] = None, only_new: bool = True) -> str:
        """读群：默认从上次已读位置往后读，第一次读回看配置的时间窗口。"""

        settings = self._settings
        target = await self._resolve_group(group_id)
        count = min(max(limit or settings.read_default_limit, 1), settings.read_max_limit)
        now_ms = self._clock_ms()
        cursor = self._read_cursors.get(target.stream_id) if only_new else None
        start_ms = cursor if cursor is not None else now_ms - settings.read_lookback_ms

        messages = await self._ctx.message.get_by_time_in_chat(
            target.stream_id,
            str(start_ms / 1000),
            str(now_ms / 1000),
            limit=count,
            limit_mode="latest",
        )
        if _is_failure(messages):
            raise ToolCallError(f"读取{target.label}的消息失败：{_failure_reason(messages)}")

        # 读成功就推进已读位置：主播看过了，下次默认只给这之后的新消息。
        self._read_cursors[target.stream_id] = now_ms
        if not messages:
            scope = "自上次查看以来" if cursor is not None else "最近"
            return f"{target.label}{scope}没有新消息。"

        readable = await self._ctx.message.build_readable(
            messages,
            replace_bot_name=True,
            timestamp_mode="relative",
            truncate=False,
        )
        if _is_failure(readable) or not isinstance(readable, str):
            raise ToolCallError(f"整理{target.label}的消息失败：{_failure_reason(readable)}")

        scope = "上次查看之后的新消息" if cursor is not None else "最近的消息"
        lines = [f"{target.label}{scope}（{len(messages)} 条）："]
        if self_hint := await self._self_signature_hint():
            lines.append(self_hint)
        if len(messages) >= count:
            lines.append(f"（只显示最近 {count} 条，更早的没有显示）")
        lines.append(readable.strip())
        return "\n".join(lines)

    async def _self_signature_hint(self) -> str:
        """说明记录里哪个署名是 QQ 那边的你自己。

        MaiBot 整理记录时只把正文里的机器人名换成「你」，发言署名仍是机器人昵称；
        不点明的话，主播会把自己在 QQ 上说过的话当成群友的话去接。每次读群现取配置，
        MaiBot 改了昵称或别名也不会过时；读不到昵称时不加这行。
        """

        nickname = await self._ctx.config.get("bot.nickname", "")
        if not isinstance(nickname, str) or not nickname.strip():
            return ""
        hint = f"（署名「{nickname.strip()}」的发言是 QQ 那边的你自己"
        aliases = await self._ctx.config.get("bot.alias_names", [])
        if isinstance(aliases, list):
            names = [str(item).strip() for item in aliases if str(item).strip()]
            if names:
                hint += f"；群友也可能叫你{'、'.join(names)}"
        return hint + "）"

    async def send_group(self, text: str, group_id: str = "") -> str:
        """发群：校验长度与间隔 → 发送并同步进 MaiBot 群上下文 → 接管该群发言权。"""

        settings = self._settings
        content = text.strip()
        if not content:
            raise ToolCallError("消息内容是空的")
        if len(content) > settings.max_text_length:
            raise ToolCallError(f"消息有 {len(content)} 字，超过上限 {settings.max_text_length} 字，请精简后再发")
        target = await self._resolve_group(group_id)

        now_ms = self._clock_ms()
        last_ms = self._last_send_ms.get(target.stream_id)
        if last_ms is not None and now_ms - last_ms < settings.min_send_interval_ms:
            wait_ms = settings.min_send_interval_ms - (now_ms - last_ms)
            raise ToolCallError(f"发得太快了，请 {wait_ms} 毫秒后再发")

        # 打开 sync_to_maisaka_history：这句话会写进 MaiBot 该群的上下文，
        # QQ 那边的麦麦之后接话时知道这是自己刚说的，不会前后矛盾。
        result = await self._ctx.send.text(
            content,
            target.stream_id,
            return_details=True,
            storage_message=True,
            sync_to_maisaka_history=True,
            maisaka_source_kind=SEND_SOURCE_KIND,
        )
        sent = bool(result.get("sent")) if isinstance(result, dict) else bool(result)
        if not sent:
            raise ToolCallError(f"消息没能发到{target.label}，原因见 MaiBot 日志")
        self._last_send_ms[target.stream_id] = now_ms

        reply = f"已发到{target.label}。"
        if await self._take_over(target):
            minutes = max(1, round(settings.takeover_duration_ms / 60000))
            reply += f"接下来约 {minutes} 分钟内 QQ 那边的你在这个群会少说话，由你来聊。"
        return reply

    async def ask_mai(self, intent: str, group_id: str = "") -> str:
        """请 QQ 麦麦说话：先交还发言权，再唤醒它按意图思考一轮。"""

        settings = self._settings
        content = intent.strip()
        if not content:
            raise ToolCallError("没有说明要 QQ 那边的你做什么")
        if len(content) > settings.max_intent_length:
            raise ToolCallError(f"说明有 {len(content)} 字，超过上限 {settings.max_intent_length} 字，请精简")
        target = await self._resolve_group(group_id)

        # 先交还发言权：接管期间该群频率被压低（为 0 时 QQ 麦麦只看不说），不还就可能叫不动它。
        async with self._takeover_lock:
            await self._release_locked(target.stream_id)

        result = await self._ctx.maisaka.proactive.trigger(
            target.stream_id,
            f"直播那边的你托你在这个群里：{content}",
            reason="amaidesu_request",
            metadata={"source": "amaidesu"},
        )
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ToolCallError(f"没能唤醒 QQ 那边的你：{_failure_reason(result)}")
        return f"已请 QQ 那边的你在{target.label}处理这件事。怎么说、说不说由它按群里的气氛决定。"

    # ──── 发言权接管 ────────────────────────────────────────

    async def _take_over(self, target: _GroupTarget) -> bool:
        """接管或续期一个群的发言权，返回是否处于接管中。"""

        settings = self._settings
        if not settings.takeover_enabled:
            return False
        async with self._takeover_lock:
            now_ms = self._clock_ms()
            takeover = self._takeovers.get(target.stream_id)
            if takeover is None:
                original = await self._ctx.frequency.get_adjust(target.stream_id)
                if _is_failure(original) or not isinstance(original, (int, float)):
                    # 读不到原倍率就不接管：贸然压低后无法确定该还原成多少。
                    self._logger.error(f"读取{target.label}的发言频率倍率失败，本次不接管: {_failure_reason(original)}")
                    return False
                takeover = _Takeover(target.stream_id, float(original), now_ms)
                self._takeovers[target.stream_id] = takeover
                self._logger.info(f"主播开始在{target.label}水群，接管发言权（原倍率 {float(original)}）")
            takeover.expires_at_ms = now_ms + settings.takeover_duration_ms
            # 每次续期都重新施加：倍率只存在 MaiBot 内存里的聊天运行实例上，
            # 实例尚未建立时宿主会忽略这次调整，下次发群时再补上。
            applied = await self._ctx.frequency.set_adjust(target.stream_id, settings.takeover_talk_adjust)
            if not applied:
                self._logger.warning(f"压低{target.label}的发言频率未成功，MaiBot 可能会同时接话")
            return True

    async def expire_takeovers(self) -> None:
        """还原所有已到期的接管（由插件的后台循环定时调用）。"""

        async with self._takeover_lock:
            now_ms = self._clock_ms()
            expired = [stream_id for stream_id, item in self._takeovers.items() if item.expires_at_ms <= now_ms]
            for stream_id in expired:
                await self._release_locked(stream_id)

    async def release_all(self) -> None:
        """还原全部接管（插件卸载时调用）。"""

        async with self._takeover_lock:
            for stream_id in list(self._takeovers):
                await self._release_locked(stream_id)

    def active_takeovers(self) -> Dict[str, int]:
        """返回 {聊天流 ID: 到期时刻毫秒}，供状态查看与测试。"""

        return {stream_id: item.expires_at_ms for stream_id, item in self._takeovers.items()}

    async def _release_locked(self, stream_id: str) -> bool:
        """还原一个群的原倍率；调用方须持有接管锁。"""

        takeover = self._takeovers.pop(stream_id, None)
        if takeover is None:
            return False
        restored = await self._ctx.frequency.set_adjust(stream_id, takeover.original_adjust)
        if restored:
            self._logger.info(f"交还聊天流 {stream_id} 的发言权，倍率还原为 {takeover.original_adjust}")
        else:
            self._logger.error(f"还原聊天流 {stream_id} 的发言频率倍率失败，请在 MaiBot 中手动检查")
        return True

    # ──── 群解析 ────────────────────────────────────────────

    async def _resolve_group(self, group_id: str) -> _GroupTarget:
        """把群号解析成 MaiBot 聊天流；只允许配置名单里的群。"""

        settings = self._settings
        if not settings.group_ids:
            raise ToolCallError("插件还没有配置可用的 QQ 群（qq.group_ids），请让管理员先配置")
        requested = group_id.strip() or settings.group_ids[0]
        if requested not in settings.group_ids:
            raise ToolCallError(f"群 {requested} 不在允许名单里，可用的群：{'、'.join(settings.group_ids)}")

        stream = await self._ctx.chat.get_stream_by_group_id(requested, platform=settings.platform)
        if _is_failure(stream):
            raise ToolCallError(f"查询群 {requested} 失败：{_failure_reason(stream)}")
        if not isinstance(stream, dict) or not stream.get("stream_id"):
            raise ToolCallError(f"MaiBot 还没有群 {requested} 的聊天记录，需要麦麦先在这个群里收到过消息")
        return _GroupTarget(
            group_id=requested,
            stream_id=str(stream["stream_id"]),
            group_name=str(stream.get("group_name") or ""),
        )


def _as_text(value: Any) -> str:
    """模型可能把群号写成数字，统一转成字符串。"""

    if value is None:
        return ""
    return str(value)


def _as_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ToolCallError(f"limit 需要是整数，收到的是 {value!r}") from exc


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ToolCallError(f"only_new 需要是 true 或 false，收到的是 {value!r}")
