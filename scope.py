"""白名单群：哪些 QQ 会话与直播互通（``qq.group_ids``）。

直播那边的近况、共享笔记只注入白名单群，记约定的工具也只在这些群里能用：直播是公开场合，
QQ 号还在别的群和私聊里，那些地方的人不该被动地看到直播的事，也不该能往主播的笔记里写东西。

MaiBot 的钩子只给会话 ID，白名单配的是群号，所以按群号向宿主查会话 ID 并缓存。群里还没人
说过话时宿主查不到会话，隔一会儿再查（每个群最多每 ``retry_ms`` 查一次）。
"""

from typing import Any, Callable, Dict, Optional, Sequence

import logging
import time


def _now_ms() -> int:
    return int(time.time() * 1000)


class WhitelistScope:
    """会话 ID → 是不是白名单群；顺带记下群名，记约定时写进留底。"""

    def __init__(
        self,
        ctx: Any,
        *,
        platform: str,
        group_ids: Sequence[str],
        logger: Optional[logging.Logger] = None,
        clock_ms: Callable[[], int] = _now_ms,
        retry_ms: int = 60_000,
    ) -> None:
        self._ctx = ctx
        self._logger = logger or logging.getLogger("plugin.amaidesu_adapter")
        self._clock_ms = clock_ms
        self._retry_ms = retry_ms
        self._platform = platform
        self._group_ids: tuple = ()
        self._sessions: Dict[str, str] = {}
        self._group_names: Dict[str, str] = {}
        self._last_try_ms: Dict[str, int] = {}
        self.update(platform=platform, group_ids=group_ids)

    def update(self, *, platform: str, group_ids: Sequence[str]) -> None:
        """白名单或平台变了：清掉缓存，下次用到时重新查。"""

        self._platform = platform
        self._group_ids = tuple(dict.fromkeys(item.strip() for item in group_ids if item.strip()))
        self._sessions.clear()
        self._group_names.clear()
        self._last_try_ms.clear()

    def allows_group(self, group_id: str, platform: str = "") -> bool:
        """按群号判断（工具调用时宿主直接给了群号与平台）。"""

        if platform and platform.strip().lower() != self._platform.strip().lower():
            return False
        return str(group_id or "").strip() in self._group_ids

    def group_name(self, group_id: str) -> str:
        return self._group_names.get(str(group_id or "").strip(), "")

    async def contains(self, session_id: str) -> bool:
        """这个会话是不是白名单群；没查过会话 ID 的群先查一遍。"""

        session_id = str(session_id or "").strip()
        if not session_id or not self._group_ids:
            return False
        if session_id in self._sessions:
            return True
        await self._resolve_missing()
        return session_id in self._sessions

    async def _resolve_missing(self) -> None:
        now_ms = self._clock_ms()
        resolved = set(self._sessions.values())
        for group_id in self._group_ids:
            if group_id in resolved or now_ms - self._last_try_ms.get(group_id, -self._retry_ms) < self._retry_ms:
                continue
            self._last_try_ms[group_id] = now_ms
            try:
                stream = await self._ctx.chat.get_stream_by_group_id(group_id, platform=self._platform)
            except Exception as exc:
                self._logger.warning(f"查白名单群 {group_id} 的会话失败，稍后再试: {exc}")
                continue
            if isinstance(stream, dict) and stream.get("stream_id"):
                self._sessions[str(stream["stream_id"])] = group_id
                self._group_names[group_id] = str(stream.get("group_name") or "")
