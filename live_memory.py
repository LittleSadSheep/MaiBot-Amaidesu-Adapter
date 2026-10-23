"""直播内容进长期记忆：把 Amaidesu 推来的整场记录按原始时间回放进一个固定的直播间会话。

MaiBot 的聊天摘要只在麦麦自己在某个会话里发出消息之后才检查一次：新增消息够阈值，就把
最近一段总结进长期记忆。只收不发的会话永远不会被总结，所以回放分两种消息：

- 观众、联动对象的话经本插件的消息网关作为入站消息送入，带原始时间；
- 主播的话由插件以麦麦身份 ``send.text`` 发进同一会话。网关发送端不外发、只回成功；
  发送钩子把这条消息的时间和编号改成原始值（``claim_outbound``），库里的时间线与直播
  一致，摘要也按 MaiBot 原有规则在这些发言处触发。连续几句主播发言合成一条，少触发几次
  MaiBot 对每条发言做的人物事实提取。

只收不说：回放前把该会话的发言频率倍率设为 0，MaiBot 只静默接收、不进规划器；网关发送端
只认正在回放的那一条（``accept_outbound``），别的发言一律回失败，不会发出、也不会入库。

一个直播间固定一个会话（平台 + 房间号当群号）：共享记忆组只认具体会话，按房间固定才不用
每场改配置。任务与进度落在插件数据目录，插件或 MaiBot 重启后接着放；放完的场次记下编号，
重复推送直接忽略。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

import asyncio
import json
import logging
import os
import time

from .live_status import platform_name


# 一场记录最多收多少行：几个小时的弹幕也在这个量级以内，再多多半是推错了
MAX_TRANSCRIPT_LINES = 20_000
# 记住最近多少场已回放的编号（防止 Amaidesu 重试或补推时重复回放）
_DONE_KEEP = 200
# 轮询"消息已入库""会话已建立"的间隔
_POLL_INTERVAL_S = 0.1


class TranscriptLine(BaseModel):
    """一行直播记录：谁在什么时刻说了什么（付费留言等由 Amaidesu 写成可读文字）。"""

    ts_ms: int = Field(gt=0)
    role: Literal["viewer", "partner", "streamer"]
    user_id: str = ""
    user_name: str = ""
    text: str = Field(min_length=1)


class TranscriptReport(BaseModel):
    """一场直播的整场记录（收播整理完后推一次）。"""

    type: Literal["transcript"]
    live_session_id: int
    platform: str = ""
    room_id: str = ""
    title: str = ""
    started_at_ms: int = 0
    ended_at_ms: int = 0
    lines: List[TranscriptLine] = Field(default_factory=list, max_length=MAX_TRANSCRIPT_LINES)
    sent_at_ms: int = 0


def parse_transcript(body: Any) -> TranscriptReport:
    """校验 Amaidesu 推来的整场记录；不合法抛 ``pydantic.ValidationError``。"""

    return TranscriptReport.model_validate(body)


@dataclass(frozen=True)
class LiveMemorySettings:
    """回放参数（由插件配置换算而来）。"""

    default_platform: str = "bilibili"
    account_id: str = "amaidesu"
    default_room_id: str = "live"
    merge_chars: int = 500
    store_wait_ms: int = 5_000
    stream_wait_ms: int = 10_000


class ReplayStep(BaseModel):
    """回放的一步：入站（观众 / 联动对象的一句）或出站（主播连续说的几句合成一条）。"""

    ts_ms: int
    outbound: bool
    text: str
    user_id: str = ""
    user_name: str = ""


class ReplayJob(BaseModel):
    """一场的回放任务；``next_index`` 是下一步的位置，落盘后可续放。"""

    live_session_id: int
    platform: str
    group_id: str
    group_name: str
    steps: List[ReplayStep]
    next_index: int = 0
    # 会话还不存在时，先拿这一步的观众消息让 MaiBot 建会话，按顺序放到它时跳过
    early_index: int = -1


def build_job(report: TranscriptReport, settings: LiveMemorySettings) -> ReplayJob:
    """按时间排好记录，把主播连续的几句合成一条（不超过 ``merge_chars`` 字）。"""

    platform = report.platform.strip() or settings.default_platform
    steps: List[ReplayStep] = []
    for line in sorted(report.lines, key=lambda item: item.ts_ms):
        text = " ".join(line.text.split())
        if not text:
            continue
        if line.role == "streamer":
            previous = steps[-1] if steps else None
            if (
                previous is not None
                and previous.outbound
                and len(previous.text) + 1 + len(text) <= settings.merge_chars
            ):
                previous.text = f"{previous.text} {text}"
            else:
                steps.append(ReplayStep(ts_ms=line.ts_ms, outbound=True, text=text))
            continue
        name = line.user_name.strip() or ("联动嘉宾" if line.role == "partner" else "观众")
        # 平台给不出账号时按名字区分：同名的人会被 MaiBot 当成同一个人
        user_id = line.user_id.strip() or f"name:{name}"
        steps.append(ReplayStep(ts_ms=line.ts_ms, outbound=False, text=text, user_id=user_id, user_name=name))
    return ReplayJob(
        live_session_id=report.live_session_id,
        platform=platform,
        group_id=report.room_id.strip() or settings.default_room_id,
        group_name=f"{platform_name(platform)}直播间",
        steps=steps,
    )


def message_id_of(live_session_id: int, index: int) -> str:
    """回放消息的编号：同一场同一步固定，重复送入时宿主能按它去重。"""

    return f"amaidesu-live-{live_session_id}-{index}"


class _ReplayAbort(Exception):
    """这场暂时放不下去（会话建不起来等）；任务留在队列里，下次再试。"""


@dataclass(frozen=True)
class _PendingOutbound:
    """正在以麦麦身份"发出"的那条主播发言。"""

    stream_id: str
    message_id: str
    ts_ms: int
    text: str


class LiveRoomReplayer:
    """回放队列：一场一场按原始时间送进直播间会话。

    ``ctx`` 是 MaiBot SDK 注入的插件上下文（用到 gateway / chat / frequency / message / send
    五组能力）；``gateway_name`` 是插件声明的双工消息网关组件名。
    """

    def __init__(
        self,
        ctx: Any,
        data_dir: Path,
        settings: LiveMemorySettings,
        *,
        gateway_name: str,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._ctx = ctx
        self._jobs_dir = data_dir / "live_memory" / "jobs"
        self._done_path = data_dir / "live_memory" / "done.json"
        self._settings = settings
        self._gateway_name = gateway_name
        self._logger = logger or logging.getLogger("plugin.amaidesu_bridge")
        self._wakeup = asyncio.Event()
        self._worker: Optional[asyncio.Task[None]] = None
        self._pending_out: Optional[_PendingOutbound] = None
        self._declared_platform = ""

    def update_settings(self, settings: LiveMemorySettings) -> None:
        if settings.account_id != self._settings.account_id:
            # 账号变了要重新声明网关，下一场回放时生效
            self._declared_platform = ""
        self._settings = settings

    # ──── 队列 ──────────────────────────────────────────────

    def start(self) -> None:
        """起后台回放；上次没放完的任务（插件或 MaiBot 重启前留下的）接着放。"""

        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="amaidesu_bridge.live_memory")
        if self._pending_job_paths():
            self._wakeup.set()

    async def stop(self) -> None:
        """停后台回放；放到一半的任务进度已落盘，下次加载时续放。"""

        worker, self._worker = self._worker, None
        self._pending_out = None
        if worker is None:
            return
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    def submit(self, report: TranscriptReport) -> str:
        """收下一场记录排进队列；放过的直接忽略。返回给 Amaidesu 的说明。

        已在队列里的场次再推一次时不重建任务，只唤醒回放：卡住的任务（如名单没放行）
        在用户改好配置后，靠 Amaidesu 的补推就能接着放。
        """

        live_session_id = report.live_session_id
        if live_session_id in self._load_done():
            return "这场已经回放过，忽略"
        if self._job_path(live_session_id).exists():
            self.start()
            self._wakeup.set()
            return "这场已在回放队列里，重新尝试回放"
        job = build_job(report, self._settings)
        if not job.steps:
            self._mark_done(live_session_id)
            return "这场没有可回放的记录"
        self._save_job(job)
        self._logger.info(f"收到直播场次 {live_session_id} 的整场记录（{len(job.steps)} 步），排队回放进直播间会话")
        self.start()
        self._wakeup.set()
        return f"已排队回放（{len(job.steps)} 步）"

    async def _run(self) -> None:
        """有新任务就按场次先后逐场回放；某场卡住就停下，等下一场推来或插件重启时再试。"""

        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            for path in self._pending_job_paths():
                try:
                    job = ReplayJob.model_validate_json(path.read_text(encoding="utf-8"))
                except Exception as exc:
                    self._logger.error(f"回放任务文件损坏，已改名跳过（{path}）: {exc}")
                    os.replace(path, path.with_suffix(".broken"))
                    continue
                try:
                    await self._replay(job)
                except asyncio.CancelledError:
                    raise
                except _ReplayAbort as exc:
                    self._logger.error(f"直播场次 {job.live_session_id} 暂时回放不下去，留在队列里下次再试: {exc}")
                    break
                except Exception as exc:
                    self._logger.error(
                        f"回放直播场次 {job.live_session_id} 出错，留在队列里下次再试: {exc}", exc_info=True
                    )
                    break
                path.unlink(missing_ok=True)
                self._mark_done(job.live_session_id)
                self._logger.info(f"直播场次 {job.live_session_id} 已回放进直播间会话（{len(job.steps)} 步）")

    # ──── 回放 ──────────────────────────────────────────────

    async def _replay(self, job: ReplayJob) -> None:
        """声明网关 → 找到（或建立）直播间会话并静音 → 按顺序送入，每发一条主播发言前确认前面的观众消息已入库。"""

        await self._declare_gateway(job.platform)
        stream_id = await self._ensure_stream(job)
        silenced = await self._ctx.frequency.set_adjust(stream_id, 0.0)
        if not silenced:
            self._logger.warning("把直播间会话的发言频率设为 0 没成功；麦麦若想在那里说话，发送端会拒绝")

        # 上一条主播发言之后送入的观众消息时刻：发下一条主播发言前要确认它们都已入库
        unconfirmed_ms: List[int] = []
        rejected = 0
        for index in range(job.next_index, len(job.steps)):
            step = job.steps[index]
            if index == job.early_index:
                unconfirmed_ms.append(step.ts_ms)
                continue
            if not step.outbound:
                if await self._route_inbound(job, index):
                    unconfirmed_ms.append(step.ts_ms)
                else:
                    rejected += 1
                continue
            if unconfirmed_ms:
                await self._wait_stored(stream_id, unconfirmed_ms, step.ts_ms)
                unconfirmed_ms = []
            await self._send_outbound(job, index, stream_id)
            # 主播发言是摘要的检查点，在这里记进度；崩溃后最多重送上一段观众消息
            job.next_index = index + 1
            self._save_job(job)
        if rejected:
            self._logger.warning(
                f"直播场次 {job.live_session_id} 有 {rejected} 条观众消息被 MaiBot 拒收（重复或被名单拦下）"
            )

    async def _declare_gateway(self, platform: str) -> None:
        """声明网关就绪：MaiBot 据此把这个平台账号认作麦麦自己，并把直播间的出站消息交给本插件。"""

        if self._declared_platform == platform:
            return
        accepted = await self._ctx.gateway.update_state(
            self._gateway_name,
            ready=True,
            platform=platform,
            account_id=self._settings.account_id,
        )
        if not accepted:
            raise _ReplayAbort("MaiBot 没有接受直播间网关的就绪声明")
        self._declared_platform = platform

    async def _ensure_stream(self, job: ReplayJob) -> str:
        """找直播间会话；还不存在就先送一条观众消息让 MaiBot 建立，再等它出现。"""

        stream_id = await self._find_stream(job)
        if stream_id:
            return stream_id
        first = next(
            (index for index in range(job.next_index, len(job.steps)) if not job.steps[index].outbound),
            None,
        )
        if first is None:
            raise _ReplayAbort("直播间会话还不存在，这场又没有观众消息可以用来建立会话")
        if not await self._route_inbound(job, first):
            raise _ReplayAbort(
                "MaiBot 拒收了直播间消息：请在 config/adapter_policy.toml 放行本插件的网关"
                f"（平台 {job.platform}，群号 {job.group_id}）"
            )
        job.early_index = first
        self._save_job(job)
        deadline = time.monotonic() + self._settings.stream_wait_ms / 1000
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL_S)
            stream_id = await self._find_stream(job)
            if stream_id:
                return stream_id
        raise _ReplayAbort("观众消息送进去了，但等不到 MaiBot 建立直播间会话")

    async def _find_stream(self, job: ReplayJob) -> str:
        stream = await self._ctx.chat.get_stream_by_group_id(job.group_id, platform=job.platform)
        if isinstance(stream, dict) and stream.get("success") is False:
            raise _ReplayAbort(f"查询直播间会话失败：{stream.get('error') or '未知原因'}")
        if isinstance(stream, dict) and stream.get("stream_id"):
            return str(stream["stream_id"])
        return ""

    async def _route_inbound(self, job: ReplayJob, index: int) -> bool:
        step = job.steps[index]
        message_id = message_id_of(job.live_session_id, index)
        message = {
            "message_id": message_id,
            "timestamp": str(step.ts_ms / 1000),
            "platform": job.platform,
            "message_info": {
                "user_info": {"user_id": step.user_id, "user_nickname": step.user_name, "user_cardname": None},
                "group_info": {"group_id": job.group_id, "group_name": job.group_name},
                "additional_config": {},
            },
            "raw_message": [{"type": "text", "data": step.text}],
            "processed_plain_text": step.text,
            "display_message": step.text,
        }
        return bool(
            await self._ctx.gateway.route_message(
                self._gateway_name,
                message,
                external_message_id=message_id,
                dedupe_key=message_id,
            )
        )

    async def _wait_stored(self, stream_id: str, routed_ms: List[int], until_ms: int) -> None:
        """入站消息是宿主在后台入库的：等这段观众消息都进了库，摘要计数才对得上。超时只告警。"""

        start_s = str(min(routed_ms) / 1000)
        end_s = str(until_ms / 1000)
        deadline = time.monotonic() + self._settings.store_wait_ms / 1000
        while True:
            stored = await self._ctx.message.get_by_time_in_chat(stream_id, start_s, end_s, limit=0)
            if isinstance(stored, list) and len(stored) >= len(routed_ms):
                return
            if time.monotonic() >= deadline:
                count = len(stored) if isinstance(stored, list) else "?"
                self._logger.warning(f"等观众消息入库超时（应有 {len(routed_ms)} 条，查到 {count} 条），继续回放")
                return
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _send_outbound(self, job: ReplayJob, index: int, stream_id: str) -> None:
        step = job.steps[index]
        self._pending_out = _PendingOutbound(
            stream_id=stream_id,
            message_id=message_id_of(job.live_session_id, index),
            ts_ms=step.ts_ms,
            text=step.text,
        )
        try:
            result = await self._ctx.send.text(step.text, stream_id, return_details=True, storage_message=True)
        finally:
            self._pending_out = None
        sent = bool(result.get("sent")) if isinstance(result, dict) else bool(result)
        if not sent:
            self._logger.warning(f"主播发言没能写进直播间会话（场次 {job.live_session_id} 第 {index} 步），继续回放")

    # ──── 宿主回调 ──────────────────────────────────────────

    def claim_outbound(self, stream_id: str, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """发送钩子：正在回放的那条主播发言，把时间与编号改成原始值；别的消息返回 None 原样放行。"""

        pending = self._pending_out
        if pending is None or stream_id != pending.stream_id or _plain_text(message) != pending.text:
            return None
        return {**message, "message_id": pending.message_id, "timestamp": str(pending.ts_ms / 1000)}

    def accept_outbound(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """网关发送端：只认正在回放的那一条（回成功、不外发），别的发言一律回失败。"""

        pending = self._pending_out
        message_id = str(message.get("message_id") or "")
        if pending is not None and message_id == pending.message_id:
            return {"success": True, "external_message_id": message_id}
        return {"success": False, "error": "直播间会话只用来回放直播记录，不往外发消息"}

    # ──── 落盘 ──────────────────────────────────────────────

    def _job_path(self, live_session_id: int) -> Path:
        return self._jobs_dir / f"{live_session_id}.json"

    def _pending_job_paths(self) -> List[Path]:
        if not self._jobs_dir.exists():
            return []
        paths = [path for path in self._jobs_dir.glob("*.json") if path.stem.isdigit()]
        return sorted(paths, key=lambda path: int(path.stem))

    def _save_job(self, job: ReplayJob) -> None:
        self._write_atomic(self._job_path(job.live_session_id), job.model_dump_json())

    def _load_done(self) -> List[int]:
        if not self._done_path.exists():
            return []
        try:
            return [int(item) for item in json.loads(self._done_path.read_text(encoding="utf-8"))]
        except Exception as exc:
            self._logger.error(f"读取已回放场次记录失败（{self._done_path}），当作没有: {exc}")
            return []

    def _mark_done(self, live_session_id: int) -> None:
        done = [item for item in self._load_done() if item != live_session_id]
        done.append(live_session_id)
        self._write_atomic(self._done_path, json.dumps(done[-_DONE_KEEP:]))

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)


def _plain_text(message: Dict[str, Any]) -> str:
    """出站消息的正文：拼起所有文字段（``send.text`` 只有一段）。"""

    segments = message.get("raw_message")
    if not isinstance(segments, list):
        return ""
    return "".join(
        str(item.get("data") or "") for item in segments if isinstance(item, dict) and item.get("type") == "text"
    )
