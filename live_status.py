"""直播那边的近况：接收 Amaidesu 推来的快照与场次回顾，缓存、落盘，并生成给麦麦看的文字。

Amaidesu 在开播、换环节、收播时推完整快照，直播期间按心跳重发；回顾写好后单独推。
本模块只认这份契约（``SnapshotReport`` / ``RecapReport``），不关心 Amaidesu 内部结构。

给麦麦看的文字只写绝对时刻（"20:05 开播"），不写"已播 35 分钟"这类每分钟都变的
描述：注入段放在每次请求里，内容不变才不会白白打破提示词前缀缓存。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter

import json
import logging
import os
import time


_PLATFORM_NAMES = {"bilibili": "B站", "douyin": "抖音", "youtube": "YouTube", "twitch": "Twitch"}
_WEEKDAY_NAMES = "一二三四五六日"


class SegmentInfo(BaseModel):
    """当前环节；``finished`` 表示流程单已走完。"""

    title: str = ""
    index: int = -1
    total: int = 0
    finished: bool = False


class SnapshotReport(BaseModel):
    """一份完整近况快照（开播 / 换环节 / 心跳 / 收播都发这一种）。"""

    type: Literal["snapshot"]
    live: bool
    live_session_id: int
    title: str = ""
    platform: str = ""
    room_id: str = ""
    started_at_ms: int = 0
    ended_at_ms: int = 0
    discarded: bool = False
    segment: Optional[SegmentInfo] = None
    sent_at_ms: int = 0


class RecapReport(BaseModel):
    """一场直播的回顾。"""

    type: Literal["recap"]
    live_session_id: int
    started_at_ms: int = 0
    ended_at_ms: int = 0
    text: str = Field(min_length=1)
    sent_at_ms: int = 0


LiveReport = Annotated[Union[SnapshotReport, RecapReport], Field(discriminator="type")]
_REPORT_ADAPTER: TypeAdapter[Any] = TypeAdapter(LiveReport)


def parse_report(body: Any) -> Union[SnapshotReport, RecapReport]:
    """校验 Amaidesu 推来的 JSON；不合法抛 ``pydantic.ValidationError``。"""

    return _REPORT_ADAPTER.validate_python(body)


@dataclass(frozen=True)
class LiveStatusSettings:
    """近况判断与文字生成参数（由插件配置换算而来）。"""

    recent_window_ms: int = 12 * 60 * 60 * 1000
    stale_after_ms: int = 3 * 60 * 1000
    recap_chars: int = 200
    keep_recaps: int = 5


def _now_ms() -> int:
    return int(time.time() * 1000)


class LiveStatusStore:
    """近况缓存：最近一份快照（及收到时刻）+ 最近几场回顾，写在插件数据目录，重启不丢。"""

    def __init__(
        self,
        path: Optional[Path],
        settings: LiveStatusSettings,
        *,
        logger: Optional[logging.Logger] = None,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._path = path
        self._settings = settings
        self._logger = logger or logging.getLogger("plugin.amaidesu_bridge")
        self._clock_ms = clock_ms
        self._snapshot: Optional[SnapshotReport] = None
        self._snapshot_received_ms = 0
        self._recaps: List[RecapReport] = []

    def update_settings(self, settings: LiveStatusSettings) -> None:
        self._settings = settings
        del self._recaps[settings.keep_recaps :]

    # ──── 写入 ──────────────────────────────────────────────

    def apply(self, report: Union[SnapshotReport, RecapReport]) -> None:
        """收下一份上报并落盘。快照整份覆盖；回顾按场次去重、新的在前。"""

        if isinstance(report, SnapshotReport):
            self._snapshot = report
            self._snapshot_received_ms = self._clock_ms()
        else:
            self._recaps = [item for item in self._recaps if item.live_session_id != report.live_session_id]
            self._recaps.append(report)
            self._recaps.sort(key=lambda item: item.ended_at_ms, reverse=True)
            del self._recaps[self._settings.keep_recaps :]
        self._save()

    def load(self) -> None:
        """读回上次落盘的近况；文件损坏只记日志、从空状态开始。"""

        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            snapshot = data.get("snapshot")
            self._snapshot = SnapshotReport.model_validate(snapshot) if snapshot else None
            self._snapshot_received_ms = int(data.get("snapshot_received_ms") or 0)
            self._recaps = [RecapReport.model_validate(item) for item in data.get("recaps", [])]
            del self._recaps[self._settings.keep_recaps :]
        except Exception as exc:
            self._logger.error(f"读取直播近况缓存失败（{self._path}），从空状态开始: {exc}")
            self._snapshot, self._snapshot_received_ms, self._recaps = None, 0, []

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            "snapshot": self._snapshot.model_dump() if self._snapshot else None,
            "snapshot_received_ms": self._snapshot_received_ms,
            "recaps": [item.model_dump() for item in self._recaps],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self._path)
        except Exception as exc:
            self._logger.error(f"写入直播近况缓存失败（{self._path}）: {exc}")

    # ──── 给麦麦看的文字 ────────────────────────────────────

    def injection_text(self) -> str:
        """每轮规划 / 回复前注入的近况；不在"直播中 / 近期刚播完 / 状态不明"时返回空串。"""

        status = self._status_sentence(with_recap=True)
        if not status:
            return ""
        # 实机发现：写成"可以聊，不用硬提"时，模型把它当可有可无的参考，被问"在干嘛"
        # 仍按人设答"在上网"。这里明确它是关于自己的事实、被问到就照此回答；
        # 只在无关话题里不主动提，避免逢人就说自己在直播。
        return (
            "<live-status>\n"
            f"关于你自己的事实：{status}\n"
            "别人问你在干嘛、忙不忙、在不在直播、最近播了什么时，照这个如实回答，不要另编；"
            "聊别的话题时不用主动提。\n"
            "</live-status>"
        )

    def query_text(self, n_back: int = 0) -> str:
        """查询工具的回答：0 = 当前状态 + 最近一场回顾；n ≥ 1 = 往前第 n 场的回顾。"""

        if n_back <= 0:
            status = self._status_sentence(with_recap=False, ignore_window=True)
            lines = [status or "还没有收到过直播那边的近况。"]
            if self._recaps:
                lines.append("最近一场的回顾：" + self._describe_recap(self._recaps[0]))
            return "\n".join(lines)
        if n_back >= len(self._recaps):
            return f"只记得最近 {len(self._recaps)} 场直播的回顾，没有往前第 {n_back} 场的。"
        return f"往前第 {n_back} 场：" + self._describe_recap(self._recaps[n_back])

    def _status_sentence(self, *, with_recap: bool, ignore_window: bool = False) -> str:
        # 句式都写"开直播（你自己是主播）"：实机里"正在 B站直播"被回复模型读成"在看别人的
        # 直播"，答出"那个人已经下播了"。点明主播是自己，才不会理解成观看。
        now_ms = self._clock_ms()
        snapshot = self._snapshot
        if snapshot is not None and snapshot.live:
            platform = _platform_name(snapshot.platform)
            if now_ms - self._snapshot_received_ms > self._settings.stale_after_ms:
                return (
                    f"你{_clock(snapshot.started_at_ms, now_ms)} 在{platform}开了一场直播（你自己是主播）"
                    f"{_title(snapshot.title)}，但最后一次收到直播那边的消息是 "
                    f"{_clock(self._snapshot_received_ms, now_ms)}，之后再没有消息，可能已经下播或断开了；"
                    "不确定时别说自己正在直播。"
                )
            sentence = (
                f"你此刻正在{platform}开直播（你自己是主播）{_title(snapshot.title)}"
                f"（{_clock(snapshot.started_at_ms, now_ms)} 开播）"
            )
            segment = snapshot.segment
            if segment is not None and segment.finished:
                sentence += "，流程单已经走完"
            elif segment is not None and segment.index >= 0 and segment.title:
                sentence += f"，进行到第 {segment.index + 1}/{segment.total} 个环节「{segment.title}」"
            return sentence + "。"

        ended = self._last_ended(now_ms, ignore_window=ignore_window)
        if ended is None:
            return ""
        session_id, title, started_ms, ended_ms, platform = ended
        span = f"{_clock(started_ms, now_ms)}–{_clock(ended_ms, now_ms, short_same_day_as=started_ms)}"
        where = f" 在{platform}" if platform else " "
        sentence = f"你{span}{where}开了一场直播（你自己是主播）{_title(title)}，已经下播。"
        recap = next((item for item in self._recaps if item.live_session_id == session_id), None)
        if with_recap and recap is not None:
            sentence += "这场的回顾：" + _clip(recap.text, self._settings.recap_chars)
        return sentence

    def _last_ended(self, now_ms: int, *, ignore_window: bool) -> Optional[tuple]:
        """最近一场已下播的直播（快照优先，没有快照时用最近一场回顾的起止）。"""

        snapshot = self._snapshot
        if snapshot is not None and not snapshot.live and not snapshot.discarded and snapshot.ended_at_ms > 0:
            candidate = (
                snapshot.live_session_id,
                snapshot.title,
                snapshot.started_at_ms,
                snapshot.ended_at_ms,
                _platform_name(snapshot.platform),
            )
        elif self._recaps:
            recap = self._recaps[0]
            candidate = (recap.live_session_id, "", recap.started_at_ms, recap.ended_at_ms, "")
        else:
            return None
        if not ignore_window and now_ms - candidate[3] > self._settings.recent_window_ms:
            return None
        return candidate

    def _describe_recap(self, recap: RecapReport) -> str:
        now_ms = self._clock_ms()
        when = f"{_clock(recap.started_at_ms, now_ms)}–{_clock(recap.ended_at_ms, now_ms, short_same_day_as=recap.started_at_ms)}"
        return f"（{when}）{recap.text}"


def _platform_name(platform: str) -> str:
    """平台标识换成口语名（bilibili → B站）；未知平台原样，空串表示不提平台。"""

    return _PLATFORM_NAMES.get(platform.lower(), platform)


def _title(title: str) -> str:
    return f"《{title}》" if title.strip() else ""


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "……"


def _clock(ts_ms: int, now_ms: int, *, short_same_day_as: Optional[int] = None) -> str:
    """绝对时刻：今天只写时分；昨天写"昨天"；更早写月日与星期。

    ``short_same_day_as`` 给结束时刻用：与开始同一天时只写时分，免得"今天 20:05–今天 21:30"。
    """

    if ts_ms <= 0:
        return "某个时间"
    moment = datetime.fromtimestamp(ts_ms / 1000)
    clock = moment.strftime("%H:%M")
    if short_same_day_as is not None and short_same_day_as > 0:
        if datetime.fromtimestamp(short_same_day_as / 1000).date() == moment.date():
            return clock
    today = datetime.fromtimestamp(now_ms / 1000).date()
    days = (today - moment.date()).days
    if days == 0:
        return f"今天 {clock}"
    if days == 1:
        return f"昨天 {clock}"
    return f"{moment.month}月{moment.day}日（周{_WEEKDAY_NAMES[moment.weekday()]}）{clock}"


def build_status_item(text: str) -> Dict[str, Any]:
    """把近况包装成一条 MaiBot Context Item（schema 版本 1 的用户消息条目）。"""

    return {
        "item_type": "UserMessageItem",
        "meta": {
            "item_id": f"amaidesu-live-status-{_now_ms()}",
            "logical_turn_id": None,
            "timestamp": datetime.now().isoformat(),
        },
        "parts": [{"type": "text", "text": text}],
    }


def insert_status_item(items: List[Any], text: str) -> List[Any]:
    """插在请求末尾的每轮指令之前（MaiBot 自己的工具提示、"时间："也放在这一段）。"""

    new_items = list(items)
    position = len(new_items) - 1 if new_items else 0
    new_items.insert(max(position, 0), build_status_item(text))
    return new_items
