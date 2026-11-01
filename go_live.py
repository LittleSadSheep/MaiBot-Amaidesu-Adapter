"""开播提示：正式场开播并稳定一段时间后，托 QQ 麦麦去通知群喊大家来看直播。

账号运营与粉丝群归常驻的 MaiBot，开播喊人也由 QQ 麦麦来说，主播不用自己托话。
难点是别喊错：误触开播、在正式场里测试，开了马上又关，这时喊出去就是打扰粉丝。
所以不是一收到开播就喊，而是"延迟确认"：

- 只认 Amaidesu 推来的正式场（彩排场 Amaidesu 根本不推）；
- 直播已经持续 ``delay_ms``（默认 5 分钟）、最近一次心跳没有超时、也没有收播，才喊；
  这段时间里收播或心跳断了，这场就不喊；
- 每场最多喊一次，喊过的场次落盘，MaiBot 重启、Amaidesu 重推近况都不会重复；
- 通知群单独配置，留空就完全不喊。

直播已经播了多久按 Amaidesu 自己的时钟算（快照里的发出时刻减开播时刻，再加上插件收到之后
过去的时间），两台机器的时钟对不齐也不影响判断。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import json
import logging
import os
import time

from .live_status import SnapshotReport, platform_name


# 喊过的场次最多记多少条：只用来防重复，留最近一批就够
_KEEP_ANNOUNCED = 50


@dataclass(frozen=True)
class GoLiveSettings:
    """开播提示参数（由插件配置换算而来）。"""

    group_ids: Tuple[str, ...] = ()
    platform: str = "qq"
    delay_ms: int = 5 * 60 * 1000
    stale_after_ms: int = 3 * 60 * 1000
    room_url: str = ""


@dataclass
class _PendingLive:
    """一场已经开播、还没喊过的正式场。"""

    key: str
    snapshot: SnapshotReport
    first_seen_ms: int
    received_ms: int


def _now_ms() -> int:
    return int(time.time() * 1000)


def _session_key(snapshot: SnapshotReport) -> str:
    """场次身份：编号加开播时刻。Amaidesu 换了数据库、编号从头数时也不会把新场次当成喊过的。"""

    return f"{snapshot.live_session_id}:{snapshot.started_at_ms}"


def room_url(snapshot: SnapshotReport, override: str = "") -> str:
    """直播间链接：配置里写了就用配置的；B 站按房间号拼；其他平台不拼。"""

    if override.strip():
        return override.strip()
    if snapshot.platform.lower() == "bilibili" and snapshot.room_id.strip():
        return f"https://live.bilibili.com/{snapshot.room_id.strip()}"
    return ""


class GoLiveAnnouncer:
    """跟踪正在进行的正式场，到点托 QQ 麦麦去通知群喊人。

    ``observe`` 在每次收到快照时调用（只记账、不发请求）；``tick`` 由插件后台循环定时调用，
    条件满足就发起托话。``ctx`` 是 MaiBot SDK 注入的插件上下文（只用到 chat 与 maisaka）。
    """

    def __init__(
        self,
        ctx: Any,
        settings: GoLiveSettings,
        *,
        state_path: Optional[Path],
        logger: Optional[logging.Logger] = None,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._ctx = ctx
        self._settings = settings
        self._state_path = state_path
        self._logger = logger or logging.getLogger("plugin.amaidesu_adapter")
        self._clock_ms = clock_ms
        self._pending: Optional[_PendingLive] = None
        self._announced: List[str] = []

    def update_settings(self, settings: GoLiveSettings) -> None:
        self._settings = settings

    def load(self) -> None:
        """读回喊过的场次；文件损坏只记日志、从空列表开始（最坏是同一场再喊一次）。"""

        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._announced = [str(item) for item in data.get("announced") or []]
        except Exception as exc:
            self._logger.error(f"读取开播提示记录失败（{self._state_path}），从空记录开始: {exc}")
            self._announced = []

    def observe(self, snapshot: SnapshotReport) -> None:
        """收到一份快照：开播或心跳就记下（或刷新）待喊的场次，收播就作废。"""

        key = _session_key(snapshot)
        now_ms = self._clock_ms()
        if not snapshot.live:
            # 收播（含开了又作废的空场）：还没到点的这场不喊了
            if self._pending is not None and self._pending.snapshot.live_session_id == snapshot.live_session_id:
                self._logger.info(f"场次 {snapshot.live_session_id} 没等到开播提示就收播了，不喊")
                self._pending = None
            return
        if key in self._announced:
            return
        if self._pending is None or self._pending.key != key:
            self._pending = _PendingLive(key=key, snapshot=snapshot, first_seen_ms=now_ms, received_ms=now_ms)
            return
        self._pending.snapshot = snapshot
        self._pending.received_ms = now_ms

    def elapsed_ms(self) -> int:
        """待喊的这场已经播了多久；没有待喊的场次时为 0。"""

        pending = self._pending
        if pending is None:
            return 0
        now_ms = self._clock_ms()
        snapshot = pending.snapshot
        if snapshot.sent_at_ms > 0 and snapshot.started_at_ms > 0:
            # 两个时刻都出自 Amaidesu 的时钟，相减不受两台机器时钟差的影响
            return max(0, snapshot.sent_at_ms - snapshot.started_at_ms) + (now_ms - pending.received_ms)
        return now_ms - pending.first_seen_ms

    async def tick(self) -> None:
        """后台定时检查：通知群配好了、直播持续够久、心跳没断，就喊一次并记下这场。"""

        pending = self._pending
        settings = self._settings
        if pending is None or not settings.group_ids:
            return
        now_ms = self._clock_ms()
        if now_ms - pending.received_ms > settings.stale_after_ms:
            # 心跳断了：可能已经关了或断线，宁可不喊；心跳恢复后会接着等
            return
        elapsed_ms = self.elapsed_ms()
        if elapsed_ms < settings.delay_ms:
            return

        # 先记下这场再去喊：喊的过程中失败也不重试，宁可漏喊一次也不在群里刷屏
        self._pending = None
        self._remember(pending.key)
        request = self._request_text(pending.snapshot, elapsed_ms)
        for group_id in settings.group_ids:
            try:
                await self._announce_to(group_id, request, pending.snapshot.live_session_id)
            except Exception as exc:
                # 一个群出错不影响其他通知群
                self._logger.error(f"开播提示在群 {group_id} 出错: {exc}", exc_info=True)

    def _request_text(self, snapshot: SnapshotReport, elapsed_ms: int) -> str:
        """托 QQ 麦麦喊人的话：只给要点（标题、平台、链接、播了多久），怎么说由它决定。"""

        title = f"《{snapshot.title.strip()}》" if snapshot.title.strip() else ""
        platform = platform_name(snapshot.platform)
        where = f"在{platform}" if platform else ""
        url = room_url(snapshot, self._settings.room_url)
        link = f"，直播间 {url}" if url else ""
        minutes = max(1, round(elapsed_ms / 60000))
        return (
            f"直播那边的你{where}开播了{title}{link}，已经播了大约 {minutes} 分钟。"
            "在群里喊大家来看直播，怎么说由你按群里的气氛决定。\n"
            "这件事请一定在群里说到。"
        )

    async def _announce_to(self, group_id: str, request: str, live_session_id: int) -> None:
        """在一个通知群里托 QQ 麦麦喊人；失败只记日志。"""

        stream = await self._ctx.chat.get_stream_by_group_id(group_id, platform=self._settings.platform)
        if not isinstance(stream, dict) or not stream.get("stream_id"):
            self._logger.error(f"开播提示没能发到群 {group_id}：MaiBot 里找不到这个群的聊天记录（{stream}）")
            return
        result = await self._ctx.maisaka.proactive.trigger(
            str(stream["stream_id"]),
            request,
            reason="amaidesu_go_live",
            priority="高",
            metadata={"source": "amaidesu", "live_session_id": live_session_id},
        )
        if isinstance(result, dict) and result.get("success") is True:
            self._logger.info(f"已托 QQ 麦麦在群 {group_id} 喊人看直播（场次 {live_session_id}）")
        else:
            error = result.get("error") if isinstance(result, dict) else result
            self._logger.error(f"开播提示没能唤醒群 {group_id} 的 QQ 麦麦：{error}")

    def _remember(self, key: str) -> None:
        self._announced.append(key)
        del self._announced[:-_KEEP_ANNOUNCED]
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._state_path.with_suffix(".tmp")
            temp.write_text(json.dumps({"announced": self._announced}, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self._state_path)
        except Exception as exc:
            self._logger.error(f"写入开播提示记录失败（{self._state_path}），重启后这场可能再喊一次: {exc}")
