"""共享长期笔记：Amaidesu 主播长期笔记的正本，两个身体（直播与 QQ）共用一份。

Amaidesu 与 MaiBot 都在用时，主播笔记里"身份"那一层——答应过的事、老梗与口头禅、长线目标、
关于我自己——的正本放在这里：MaiBot 7×24 常驻，QQ 麦麦随时读得到，QQ 里答应的事也能随时
记进来；Amaidesu 开播前拉取、收播整理后带版本号推回，本地只留缓存。只用 Amaidesu 时这份
不存在，与本插件无关。

笔记的格式由 Amaidesu 定义，这里只认最小的一部分（契约）：

- ``## 节名`` 分节，节下每行一条 ``- 正文``，行尾可带一组标签 ``〔标签 · 标签〕``；
- 给 QQ 麦麦看的是"答应过的事、老梗与口头禅、长线目标、关于我自己"四节与人自己加的节，
  "不确定"（还没坐实）与"搁置"（已淡出）不给；
- 带 ``对 昵称#观众编号`` 标签的条目是答应某位直播观众的事，只在直播里那位观众发言时才有用，
  不给 QQ 这边；
- QQ 里记下的约定追加到"答应过的事"末尾，标 ``未兑现 · 来自 QQ群``。Amaidesu 收播整理时
  把它当作别处写进来的条目：会被整理、会淡出、能标兑现，但不当成人手写的行。

条目的整理、淡出与标兑现都在 Amaidesu 那边做；这里只存正本（写入核对版本号并留底）、给 QQ
麦麦渲染注入文字、追加约定、核对两边名字。版本号 = 去掉首尾空白后按 UTF-8 取 SHA-1（十六进制），
与 Amaidesu 一致，两边对同一份全文算出的版本号相同。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import hashlib
import json
import logging
import os
import re
import time


SECTION_PROMISES = "答应过的事"
# 给 QQ 麦麦看的固定四节；人自己加的节（不在已知节名里）也给
INJECTED_SECTIONS = (SECTION_PROMISES, "老梗与口头禅", "长线目标", "关于我自己")
_HIDDEN_SECTIONS = ("不确定", "搁置")
QQ_SOURCE = "QQ群"
STATUS_OPEN = "未兑现"

_TAG_BLOCK = re.compile(r"〔([^〔〕]*)〕\s*$")
_VIEWER_TAG = re.compile(r"^对\s*.*#\d+$")


def content_version(text: str) -> str:
    """全文的版本号：去掉首尾空白后按 UTF-8 取 SHA-1（十六进制），与 Amaidesu 同一算法。"""

    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class NotesSettings:
    """共享笔记的参数（由插件配置换算而来）。"""

    max_promise_chars: int = 80
    max_open_qq_promises: int = 10
    history_keep: int = 30


@dataclass(frozen=True)
class PromiseResult:
    """记一件约定的结果：``ok`` 为假时 ``message`` 写明原因，回给 QQ 麦麦。"""

    ok: bool
    message: str


class SharedNotesStore:
    """共享笔记的正本：一份全文 + 留底记录，落在插件数据目录，MaiBot 重启不丢。

    文件：``<目录>/MEMORY.md`` 是正本全文；``<目录>/history.jsonl`` 每行一次写入（时刻、来由、
    改动说明、写之前的全文），只留最近 ``history_keep`` 条，写坏了能翻回去。
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
        self._text = ""

    @property
    def text(self) -> str:
        return self._text

    @property
    def version(self) -> str:
        return content_version(self._text)

    def update_settings(self, settings: NotesSettings) -> None:
        self._settings = settings

    def load(self) -> None:
        """读回正本；文件读不了只记日志、按空笔记开始（下次 Amaidesu 连上会把它那份推上来）。"""

        if self._dir is None:
            return
        path = self._dir / "MEMORY.md"
        if not path.exists():
            return
        try:
            self._text = path.read_bytes().decode("utf-8-sig").strip()
        except Exception as exc:
            self._logger.error(f"读取共享笔记失败（{path}），按空笔记开始: {exc}")
            self._text = ""

    # ──── 写入 ──────────────────────────────────────────────

    def write(self, text: str, *, expected_version: str, reason: str, changes: Sequence[str] = ()) -> bool:
        """整份覆盖写；``expected_version`` 与当前版本对不上时不写、返回 False（由写入方重读重做）。"""

        if expected_version != self.version:
            return False
        self._replace((text or "").strip(), reason=reason, changes=list(changes))
        return True

    def add_promise(self, content: str, *, where: str) -> PromiseResult:
        """把 QQ 里答应的一件事追加到"答应过的事"末尾；太长、重复、未兑现的太多时不记。"""

        text = _clean_promise(content)
        if not text:
            return PromiseResult(False, "约定内容是空的")
        if len(text) > self._settings.max_promise_chars:
            return PromiseResult(
                False, f"约定写得太长了（{len(text)} 字），精简到 {self._settings.max_promise_chars} 字以内再记"
            )
        lines = self._text.splitlines()
        promises = _section_bodies(lines, SECTION_PROMISES)
        if any(_strip_tags(body)[0] == text for body in promises):
            return PromiseResult(True, "这件事已经记着了")
        open_from_qq = sum(1 for body in promises if {STATUS_OPEN, f"来自 {QQ_SOURCE}"} <= set(_strip_tags(body)[1]))
        if open_from_qq >= self._settings.max_open_qq_promises:
            return PromiseResult(
                False,
                f"QQ 这边记下、还没兑现的约定已经有 {open_from_qq} 件了，先兑现或等直播那边整理后再记新的",
            )
        line = f"- {text}〔{STATUS_OPEN} · 来自 {QQ_SOURCE}〕"
        self._replace(
            "\n".join(_append_to_section(lines, SECTION_PROMISES, line)).strip(),
            reason=f"QQ 群{where}里记下的约定",
            changes=[f"新增〔{SECTION_PROMISES}〕{text}"],
        )
        return PromiseResult(True, "记下了，直播那边下次开播也会知道")

    def _replace(self, text: str, *, reason: str, changes: List[str]) -> None:
        if text == self._text:
            return
        previous, self._text = self._text, text
        if self._dir is None:
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._append_history(previous, reason, changes)
            _atomic_write(self._dir / "MEMORY.md", text + "\n" if text else "")
        except Exception as exc:
            self._logger.error(f"写入共享笔记失败（{self._dir}）: {exc}")

    def _append_history(self, previous: str, reason: str, changes: List[str]) -> None:
        assert self._dir is not None
        path = self._dir / "history.jsonl"
        lines = path.read_bytes().decode("utf-8").splitlines() if path.exists() else []
        entry = {"at_ms": self._clock_ms(), "reason": reason, "changes": changes, "previous": previous}
        lines.append(json.dumps(entry, ensure_ascii=False))
        kept = [line for line in lines if line.strip()][-self._settings.history_keep :]
        _atomic_write(path, "".join(line + "\n" for line in kept))

    # ──── 给 QQ 麦麦看的文字 ────────────────────────────────

    def injection_text(self) -> str:
        """每轮规划 / 回复前注入的笔记；没有能给 QQ 这边看的条目时返回空串。"""

        body = render_for_qq(self._text)
        if not body:
            return ""
        return (
            "<shared-notes>\n"
            "你一直记着的事（直播那边和 QQ 这边共用这一份笔记，你自己就是那个主播）：\n"
            f"{body}\n"
            "有人提起相关的事时照这个说，不用主动念叨；还没兑现的约定别说成已经做了。\n"
            "</shared-notes>"
        )


def render_for_qq(text: str) -> str:
    """笔记全文 → 给 QQ 麦麦看的正文：只留注入的节，去掉标签，跳过只对某位直播观众的条目。"""

    preamble: List[str] = []
    blocks: List[Tuple[str, List[str]]] = []
    section = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            section = line[3:].strip()
            blocks.append((section, []))
            continue
        if not section:
            preamble.append(line)
            continue
        if section in _HIDDEN_SECTIONS:
            continue
        body = line[2:].strip() if line[:2] in ("- ", "* ") else line
        item_text, tags = _strip_tags(body)
        if not item_text or any(_VIEWER_TAG.match(tag) for tag in tags):
            continue
        blocks[-1][1].append(f"- {item_text}" + _describe(tags))
    parts = ["\n".join(preamble)] if preamble else []
    parts.extend("\n".join([f"## {name}", *lines]) for name, lines in blocks if lines)
    return "\n\n".join(parts)


def _describe(tags: List[str]) -> str:
    notes: List[str] = []
    for tag in tags:
        if tag == STATUS_OPEN:
            notes.append("还没兑现")
        elif tag in ("已兑现", "放弃"):
            notes.append(tag)
        elif tag.startswith("游戏 "):
            notes.append(tag[3:].strip())
        elif tag.startswith("来自 "):
            notes.append(tag)
    return f"（{'，'.join(notes)}）" if notes else ""


def _strip_tags(body: str) -> Tuple[str, List[str]]:
    """一行正文拆成（去掉标签的正文, 标签列表）。"""

    match = _TAG_BLOCK.search(body)
    if match is None:
        return body.strip(), []
    tags = [part.strip() for part in match.group(1).split("·") if part.strip()]
    return body[: match.start()].strip(), tags


def _section_bodies(lines: List[str], section: str) -> List[str]:
    bodies: List[str] = []
    current = ""
    for raw in lines:
        line = raw.strip()
        if line.startswith("## "):
            current = line[3:].strip()
        elif current == section and line:
            bodies.append(line[2:].strip() if line[:2] in ("- ", "* ") else line)
    return bodies


def _append_to_section(lines: List[str], section: str, new_line: str) -> List[str]:
    """把一行加到某节末尾；没有这一节时作为第一节插在说明文字之后（与 Amaidesu 的节序一致）。"""

    header = f"## {section}"
    stripped = [line.strip() for line in lines]
    if header in stripped:
        end = stripped.index(header) + 1
        while end < len(lines) and not stripped[end].startswith("## "):
            end += 1
        while end > 0 and not stripped[end - 1]:
            end -= 1  # 插在这一节最后一条之后，不插到节间空行后面
        return [*lines[:end], new_line, *lines[end:]]
    first = next((index for index, line in enumerate(stripped) if line.startswith("## ")), len(lines))
    before = lines[:first]
    while before and not before[-1].strip():
        before.pop()
    block = [header, new_line]
    rest = lines[first:]
    return [*before, *([""] if before else []), *block, *([""] if rest else []), *rest]


def _clean_promise(content: str) -> str:
    """约定正文压成一行：去掉换行、行首的列表符号与标签括号（免得伪造标签）。"""

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
