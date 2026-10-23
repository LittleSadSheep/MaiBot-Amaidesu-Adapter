"""主播回想 QQ 那边的事：长期记忆检索，以及 QQ 麦麦对某个群友的印象。

直播间是公开场合，QQ 麦麦在私聊里知道的事不能经主播说出去，所以：
- 记忆检索把聊天范围限定为允许名单里的这个群：MaiBot 长期记忆（A_memorix）
  限定聊天流后只返回该群自己的记忆与全局导入的知识，私聊里形成的记忆查不出来；
- 人物只取熟悉度字段（认识多久、来往几次）与该群范围内的相关记忆，不用跨全部
  聊天汇总的人物画像，群名片也只给这个群的；
- 输出里不出现 QQ 号，并在末尾提醒别把群友的私事原样念出来。
"""

from datetime import datetime
from typing import Any, Dict, List, Protocol, Tuple

from .errors import ToolCallError


PRIVACY_NOTE = "（直播间是公开场合：这些只供你心里有数，别把群友的私事原样念出来。）"
_MEMORY_LIMIT = 5
# 按名字找人时最多扫描的群消息条数（最近的这么多条）
_LOOKUP_MESSAGE_LIMIT = 500
# MaiBot 记忆检索没查到时的固定开头
_NO_MEMORY_PREFIX = "你不太了解"


class GroupScope(Protocol):
    """回想的范围：一个允许名单里的群（由桥接层解析好再传入）。"""

    group_id: str
    stream_id: str

    @property
    def label(self) -> str: ...


def _is_failure(result: Any) -> bool:
    return isinstance(result, dict) and result.get("success") is False


async def _search_memory(ctx: Any, scope: GroupScope, query: str, *, person_id: str = "") -> str:
    """调宿主的长期记忆检索；聊天范围限定为这个群。返回检索正文，没查到返回空串。

    SDK 的 ``knowledge.search`` 封装只传查询词与条数，范围与人物过滤要经
    ``call_capability`` 直接传给宿主实现。
    """

    result = await ctx.call_capability(
        "knowledge.search",
        query=query,
        limit=_MEMORY_LIMIT,
        mode="search",
        chat_id=scope.stream_id,
        group_id=scope.group_id,
        person_id=person_id,
        respect_filter=True,
    )
    if _is_failure(result):
        raise ToolCallError(f"查 QQ 那边的记忆失败：{result.get('error') or '未知原因'}")
    content = str(result or "").strip()
    if not content or content.startswith(_NO_MEMORY_PREFIX):
        return ""
    return content


async def recall_memory(ctx: Any, scope: GroupScope, query: str) -> str:
    """回想 QQ 那边的你在这个群里记下的事。"""

    text = query.strip()
    if not text:
        raise ToolCallError("没说要回想什么")
    content = await _search_memory(ctx, scope, text)
    if not content:
        return f"QQ 那边的你在{scope.label}里没有关于「{text}」的记忆（只查了这个群）。"
    return f"（以下是 QQ 那边的你在{scope.label}里记下的，只作参考）\n{content}\n{PRIVACY_NOTE}"


async def recall_person(
    ctx: Any,
    scope: GroupScope,
    name: str,
    *,
    platform: str,
    lookup_days: int,
    now_ms: int,
) -> str:
    """QQ 那边的你对某个群友的印象：先按 MaiBot 起的称呼找，再按这个群最近的群名片 / 昵称找。"""

    target = name.strip()
    if not target:
        raise ToolCallError("没说要回想谁")
    if await _is_self(ctx, target):
        return f"「{target}」就是 QQ 那边的你自己。"

    person_id = _value_or_empty(await ctx.person.get_id_by_name(target))
    if person_id and not _card_in_group(await _field(ctx, person_id, "group_cardname_list"), scope.group_id):
        # 按称呼找到的人在这个群没有群名片：可能只在私聊里认识，按群范围不透露，改为只在本群发言人里找
        person_id = ""
    if not person_id:
        person_id = await _find_in_group(ctx, scope, target, platform=platform, lookup_days=lookup_days, now_ms=now_ms)

    if await _field(ctx, person_id, "is_known") is not True:
        return f"QQ 那边的你还不认识「{target}」。"

    person_name = str(await _field(ctx, person_id, "person_name") or "").strip()
    nickname = str(await _field(ctx, person_id, "nickname") or "").strip()
    card = _card_in_group(await _field(ctx, person_id, "group_cardname_list"), scope.group_id)
    know_since = await _field(ctx, person_id, "know_since")
    know_times = await _field(ctx, person_id, "know_times")
    last_know = await _field(ctx, person_id, "last_know")

    names: List[str] = []
    if person_name:
        names.append(f"你叫他「{person_name}」")
    if card:
        names.append(f"他在这个群的群名片是「{card}」")
    if nickname and nickname not in {person_name, card}:
        names.append(f"QQ 昵称「{nickname}」")
    familiarity: List[str] = []
    if isinstance(know_since, (int, float)) and know_since > 0:
        familiarity.append(f"认识于 {_date(know_since)}")
    if isinstance(know_times, int) and know_times > 0:
        familiarity.append(f"来往过 {know_times} 次")
    if isinstance(last_know, (int, float)) and last_know > 0:
        familiarity.append(f"上次是 {_date(last_know)}")

    memory = await _search_memory(ctx, scope, person_name or card or target, person_id=person_id)
    lines = [f"QQ 那边的你对「{target}」的印象（{scope.label}）："]
    if names:
        lines.append("- " + "，".join(names))
    if familiarity:
        lines.append("- " + "，".join(familiarity))
    lines.append("- 相关记忆：" + (memory if memory else "没有他在这个群里的记忆"))
    lines.append(PRIVACY_NOTE)
    return "\n".join(lines)


async def _is_self(ctx: Any, name: str) -> bool:
    """名字是不是 QQ 那边的麦麦自己（机器人昵称或别名）。"""

    nickname = await ctx.config.get("bot.nickname", "")
    aliases = await ctx.config.get("bot.alias_names", [])
    names = {str(nickname).strip()} if isinstance(nickname, str) else set()
    if isinstance(aliases, list):
        names.update(str(item).strip() for item in aliases)
    names.discard("")
    return name in names


async def _find_in_group(
    ctx: Any, scope: GroupScope, name: str, *, platform: str, lookup_days: int, now_ms: int
) -> str:
    """按这个群最近的发言人群名片 / 昵称找人：精确匹配优先，没有再按包含匹配；多人同名时列出候选。"""

    start_s = (now_ms - lookup_days * 24 * 60 * 60 * 1000) / 1000
    messages = await ctx.message.get_by_time_in_chat(
        scope.stream_id,
        str(start_s),
        str(now_ms / 1000),
        limit=_LOOKUP_MESSAGE_LIMIT,
        limit_mode="latest",
    )
    if _is_failure(messages):
        raise ToolCallError(f"查{scope.label}的聊天记录失败：{messages.get('error') or '未知原因'}")

    speakers: Dict[str, Tuple[str, str]] = {}
    for message in messages if isinstance(messages, list) else []:
        user = ((message or {}).get("message_info") or {}).get("user_info") or {}
        user_id = str(user.get("user_id") or "").strip()
        if user_id:
            speakers[user_id] = (
                str(user.get("user_cardname") or "").strip(),
                str(user.get("user_nickname") or "").strip(),
            )

    exact = [uid for uid, (card, nick) in speakers.items() if name in {card, nick}]
    matched = exact or [uid for uid, (card, nick) in speakers.items() if name in card or name in nick]
    if not matched:
        raise ToolCallError(f"在{scope.label}最近 {lookup_days} 天的聊天里没找到叫「{name}」的人")
    if len(matched) > 1:
        candidates = "、".join(f"「{speakers[uid][0] or speakers[uid][1]}」" for uid in matched[:8])
        raise ToolCallError(f"{scope.label}里叫「{name}」的不止一个：{candidates}，请说清楚是哪一个")
    person_id = _value_or_empty(await ctx.person.get_id(platform, matched[0]))
    if not person_id:
        raise ToolCallError(f"QQ 那边的你还不认识「{name}」")
    return person_id


async def _field(ctx: Any, person_id: str, field_name: str) -> Any:
    """读人物字段；读不到（未知人物或字段缺失）返回 None。"""

    if not person_id:
        return None
    value = await ctx.person.get_value(person_id, field_name)
    return None if _is_failure(value) else value


def _value_or_empty(value: Any) -> str:
    return "" if _is_failure(value) or value is None else str(value).strip()


def _card_in_group(card_list: Any, group_id: str) -> str:
    """只取这个群的群名片：别的群的名片可能透露他还在哪些群。"""

    if not isinstance(card_list, list):
        return ""
    for item in card_list:
        if isinstance(item, dict) and str(item.get("group_id") or "") == group_id:
            return str(item.get("group_cardname") or "").strip()
    return ""


def _date(ts_s: float) -> str:
    moment = datetime.fromtimestamp(ts_s)
    return f"{moment.year}年{moment.month}月{moment.day}日"
