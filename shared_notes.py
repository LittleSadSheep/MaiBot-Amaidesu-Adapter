"""共享长期笔记在 MaiBot 这边的两样东西：Amaidesu 发布来的笔记文字，与 QQ 麦麦记下、直播那边还没收走的约定。

主播的长期笔记（答应过的事、老梗与口头禅、长线目标、关于我自己）只有一份正本，在 Amaidesu
本地，格式也归 Amaidesu 管。两边连接只交换"发布出来的东西"，不为了对接去改任何一边的内部：

- Amaidesu 每次写完正式场的笔记，把它自己渲染好的"给 QQ 麦麦看的笔记"推过来；这里原样存下、
  原样注入，不解析、不改写。
- QQ 麦麦在群里答应的事记在插件自己的约定清单里（正文、在哪个群记的、什么时候），注入时和
  笔记一起给它看，记下当场就看得到。
- Amaidesu 读笔记时来取清单，按它自己的规则并进笔记、发布新版本后告诉这里"已收"，这里再从
  清单里删掉。

只用 Amaidesu 时这里什么都没有，与本插件无关。
"""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import json
import logging
import os
import re
import time
import uuid


# QQ 里记下的约定告诉 Amaidesu 的来处（Amaidesu 并进笔记时标成"来自 QQ群"）
QQ_SOURCE = "QQ群"

# 旧版本把 Amaidesu 格式的整份笔记当正本存在这里；读到时只把 QQ 记下、还没兑现的约定搬进清单
_LEGACY_FILE = "MEMORY.md"
_LEGACY_PROMISE = re.compile(r"^[-*]\s*(.+?)〔([^〔〕]*)〕\s*$")


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class NotesSettings:
    """共享笔记的参数（由插件配置换算而来）。"""

    max_promise_chars: int = 80
    max_pending_promises: int = 10


@dataclass(frozen=True)
class PromiseResult:
    """记一件约定的结果：``ok`` 为假时 ``message`` 写明原因，回给 QQ 麦麦。"""

    ok: bool
    message: str


@dataclass(frozen=True)
class PendingPromise:
    """QQ 里记下、直播那边还没收走的一件约定。"""

    id: str
    text: str
    source: str
    where: str
    created_at_ms: int


class SharedNotesStore:
    """发布来的笔记文字 + 待收的约定清单，落在插件数据目录，MaiBot 重启不丢。

    文件：``<目录>/published.md`` 是 Amaidesu 最近一次发布的文字；``<目录>/promises.json`` 是
    待收的约定清单。
    """

    def __init__(
        self,
        directory: Optional[Path],
        settings: NotesSettings,
        *,
        logger: Optional[logging.Logger] = None,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._dir = directory
        self._settings = settings
        self._logger = logger or logging.getLogger("plugin.amaidesu_adapter")
        self._clock_ms = clock_ms
        self._published = ""
        self._promises: List[PendingPromise] = []

    @property
    def published(self) -> str:
        return self._published

    def pending(self) -> List[PendingPromise]:
        """待收的约定（先记的在前）。"""

        return list(self._promises)

    def update_settings(self, settings: NotesSettings) -> None:
        self._settings = settings

    def load(self) -> None:
        """读回发布的文字与约定清单；文件读不了只记日志、按空的开始（下次 Amaidesu 开播会再发布）。"""

        if self._dir is None:
            return
        published = self._dir / "published.md"
        if published.exists():
            try:
                self._published = published.read_bytes().decode("utf-8-sig").strip()
            except Exception as exc:
                self._logger.error(f"读取 Amaidesu 发布的笔记失败（{published}），按空笔记开始: {exc}")
                self._published = ""
        promises = self._dir / "promises.json"
        if promises.exists():
            try:
                raw = json.loads(promises.read_bytes().decode("utf-8"))
                self._promises = [PendingPromise(**item) for item in raw]
            except Exception as exc:
                self._logger.error(f"读取待收的约定清单失败（{promises}），按空清单开始: {exc}")
                self._promises = []
        self._migrate_legacy()

    # ──── Amaidesu 那边的读写 ────────────────────────────────

    def publish(self, text: str) -> None:
        """收下 Amaidesu 发布的笔记文字（整份替换）。"""

        text = (text or "").strip()
        if text == self._published:
            return
        self._published = text
        self._write("published.md", text + "\n" if text else "")

    def acknowledge(self, ids: Sequence[str]) -> int:
        """Amaidesu 收走了这些约定（已并进它的笔记并发布）：从清单里删掉，返回删了几件。"""

        wanted = {str(item) for item in ids}
        kept = [item for item in self._promises if item.id not in wanted]
        removed = len(self._promises) - len(kept)
        if removed:
            self._promises = kept
            self._save_promises()
        return removed

    # ──── QQ 麦麦记约定 ──────────────────────────────────────

    def add_promise(self, content: str, *, where: str) -> PromiseResult:
        """把 QQ 里答应的一件事放进待收清单；太长、清单里已有、待收的太多时不记。"""

        text = _clean_promise(content)
        if not text:
            return PromiseResult(False, "约定内容是空的")
        if len(text) > self._settings.max_promise_chars:
            return PromiseResult(
                False, f"约定写得太长了（{len(text)} 字），精简到 {self._settings.max_promise_chars} 字以内再记"
            )
        if any(item.text == text for item in self._promises):
            return PromiseResult(True, "这件事已经记着了")
        if len(self._promises) >= self._settings.max_pending_promises:
            return PromiseResult(
                False,
                f"QQ 这边记下、直播那边还没收走的约定已经有 {len(self._promises)} 件了，等直播那边开播收走后再记新的",
            )
        self._append(text, source=QQ_SOURCE, where=where)
        return PromiseResult(True, "记下了，直播那边下次开播也会知道")

    def _append(self, text: str, *, source: str, where: str) -> None:
        # 编号随机生成：只用来让 Amaidesu 回报"收走了哪几件"，重启后也不会和旧编号撞上
        self._promises.append(
            PendingPromise(
                id=uuid.uuid4().hex[:12], text=text, source=source, where=where, created_at_ms=self._clock_ms()
            )
        )
        self._save_promises()

    # ──── 给 QQ 麦麦看的文字 ────────────────────────────────

    def injection_text(self) -> str:
        """每轮规划 / 回复前注入的笔记；发布的文字与待收的约定都没有时返回空串。"""

        parts: List[str] = []
        if self._published:
            parts.append(self._published)
        if self._promises:
            lines = "\n".join(f"- {item.text}（还没兑现，来自 {item.source}）" for item in self._promises)
            parts.append(f"QQ 这边刚答应、直播那边还没收进笔记的约定：\n{lines}")
        if not parts:
            return ""
        body = "\n\n".join(parts)
        return (
            "<shared-notes>\n"
            "你一直记着的事（直播那边和 QQ 这边共用这份笔记，你自己就是那个主播）：\n"
            f"{body}\n"
            "有人提起相关的事时照这个说，不用主动念叨；还没兑现的约定别说成已经做了。\n"
            "</shared-notes>"
        )

    # ──── 落盘 ──────────────────────────────────────────────

    def _save_promises(self) -> None:
        payload = json.dumps([asdict(item) for item in self._promises], ensure_ascii=False)
        self._write("promises.json", payload)

    def _write(self, name: str, content: str) -> None:
        if self._dir is None:
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self._dir / name, content)
        except Exception as exc:
            self._logger.error(f"写入共享笔记文件失败（{self._dir / name}）: {exc}")

    def _migrate_legacy(self) -> None:
        """旧版本在这里存过 Amaidesu 格式的整份笔记：QQ 记下、还没兑现的约定搬进清单，旧文件改名留底。

        其余条目本来就在 Amaidesu 本地笔记里（旧版本每次读写都同步到本地），不用搬。
        """

        assert self._dir is not None
        legacy = self._dir / _LEGACY_FILE
        if not legacy.exists():
            return
        try:
            lines = legacy.read_bytes().decode("utf-8-sig").splitlines()
            moved = 0
            for line in lines:
                match = _LEGACY_PROMISE.match(line.strip())
                if match is None:
                    continue
                tags = [part.strip() for part in match.group(2).split("·")]
                source = next((tag[3:].strip() for tag in tags if tag.startswith("来自 ")), "")
                text = _clean_promise(match.group(1))
                if source and "未兑现" in tags and text and all(item.text != text for item in self._promises):
                    self._append(text, source=source, where="")
                    moved += 1
            os.replace(legacy, legacy.with_name("MEMORY.legacy.md"))
            self._logger.info(f"旧版共享笔记已改名为 MEMORY.legacy.md，搬了 {moved} 件 QQ 记下的约定进待收清单")
        except Exception as exc:
            self._logger.error(f"处理旧版共享笔记失败（{legacy}），原文件保留: {exc}")


def _clean_promise(content: str) -> str:
    """约定正文压成一行：去掉换行、行首的列表符号，标签括号换成普通括号（免得混进 Amaidesu 的标签）。"""

    text = re.sub(r"\s+", " ", str(content or "")).strip()
    text = text.replace("〔", "（").replace("〕", "）")
    return text[2:].strip() if text[:2] in ("- ", "* ") else text


def _atomic_write(path: Path, content: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(content.encode("utf-8"))
    os.replace(temp, path)


def names_check(persona_name: str, maibot_names: Sequence[str]) -> Dict[str, object]:
    """两边名字核对：Amaidesu 的主播名是否是 MaiBot 的昵称或别名之一；主播名没给时不判断。"""

    names = [name for name in (str(item).strip() for item in maibot_names) if name]
    persona = persona_name.strip()
    return {"maibot": names, "match": (persona in names) if persona else None}
