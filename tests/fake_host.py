"""测试用的假 MaiBot 宿主。

插件经真实 SDK 的 ``PluginContext`` 发起能力调用，这里在 RPC 层模拟宿主的返回
形状（``{"success": ..., <结果键>: ...}``），从而连同 SDK 的参数传递与结果解包
一起验证，而不是只测插件自己的假对象。
"""

from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import PluginContext, PluginPaths

import importlib
import importlib.util
import sys
import tempfile


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "amaidesu_bridge_plugin_under_test"

GROUP_ID = "10001"
STREAM_ID = "stream-10001"
GROUP_NAME = "粉丝群"


def load_plugin_package() -> ModuleType:
    """按 MaiBot 插件加载器的方式把插件目录当作包导入，相对导入才能生效。"""

    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "plugin.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


def load_bridge_module() -> ModuleType:
    """桥接逻辑经包导入（它相对导入 errors / recall），与插件加载器一致。"""

    load_plugin_package()
    return importlib.import_module(f"{PACKAGE_NAME}.bridge")


class FakeHost:
    """记录插件发起的能力调用，并按宿主实现的形状返回结果。"""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.streams: Dict[str, Dict[str, Any]] = {
            GROUP_ID: {"stream_id": STREAM_ID, "group_id": GROUP_ID, "group_name": GROUP_NAME},
        }
        # 每条消息只放测试关心的字段：时间戳（秒，字符串，与宿主序列化一致）与正文。
        self.messages: Dict[str, List[Dict[str, Any]]] = {STREAM_ID: []}
        self.adjust: Dict[str, float] = {}
        self.send_ok = True
        self.get_adjust_fails = False
        self.trigger_result: Dict[str, Any] = {"success": True, "stream_id": STREAM_ID, "task_id": "t1", "queued": True}
        # 宿主全局配置里与读群署名提示相关的键（点分键 → 值）。
        self.config: Dict[str, Any] = {"bot.nickname": "麦麦", "bot.alias_names": []}
        self.data_dir = Path(tempfile.mkdtemp(prefix="amaidesu-bridge-test-"))
        # 人物库：person_id → 字段；称呼 → person_id；(平台, QQ 号) → person_id
        self.people: Dict[str, Dict[str, Any]] = {}
        self.person_names: Dict[str, str] = {}
        self.person_ids: Dict[Tuple[str, str], str] = {}
        # 长期记忆：检索键（有人物过滤时用 person_id，否则用查询词）→ 正文
        self.knowledge: Dict[str, str] = {}
        self.knowledge_fails = False

    def add_person(self, person_id: str, user_id: str, **fields: Any) -> None:
        """登记一个 MaiBot 认识的人；fields 为人物字段（person_name / nickname / group_cardname_list 等）。"""

        self.people[person_id] = {"is_known": True, **fields}
        if fields.get("person_name"):
            self.person_names[fields["person_name"]] = person_id
        self.person_ids[("qq", user_id)] = person_id

    def add_message(
        self,
        timestamp_s: float,
        text: str,
        stream_id: str = STREAM_ID,
        *,
        user_id: str = "",
        nickname: str = "",
        cardname: str = "",
    ) -> None:
        message: Dict[str, Any] = {"timestamp": str(timestamp_s), "text": text}
        if user_id:
            message["message_info"] = {
                "user_info": {"user_id": user_id, "user_nickname": nickname, "user_cardname": cardname}
            }
        self.messages.setdefault(stream_id, []).append(message)

    def calls_of(self, capability: str) -> List[Dict[str, Any]]:
        return [args for name, args in self.calls if name == capability]

    def make_context(self, plugin_id: str = "amaidesu.amaidesu-bridge-plugin") -> PluginContext:
        # 插件数据目录放临时目录：近况缓存等落盘文件不写进仓库
        paths = PluginPaths(data_dir=self.data_dir, runtime_dir=self.data_dir / "runtime")
        return PluginContext(plugin_id, rpc_call=self.rpc, paths=paths)

    async def rpc(
        self, method: str, plugin_id: str, payload: Optional[Dict[str, Any]], timeout_ms: Optional[int] = None
    ) -> Any:
        del plugin_id, timeout_ms
        assert method == "cap.call"
        assert payload is not None
        capability = str(payload["capability"])
        args = dict(payload["args"])
        self.calls.append((capability, args))
        handler = getattr(self, "_" + capability.replace(".", "_"))
        return handler(args)

    def _chat_get_stream_by_group_id(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True, "stream": self.streams.get(str(args["group_id"]))}

    def _message_get_by_time_in_chat(self, args: Dict[str, Any]) -> Dict[str, Any]:
        start = float(args["start_time"])
        end = float(args["end_time"])
        matched = [m for m in self.messages.get(args["chat_id"], []) if start <= float(m["timestamp"]) <= end]
        limit = int(args.get("limit", 0))
        if limit > 0:
            matched = matched[-limit:]
        return {"success": True, "messages": matched}

    def _message_build_readable(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True, "text": "\n".join(m["text"] for m in args["messages"])}

    def _send_text(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self.send_ok:
            return {"success": False, "sent": False, "message_id": None, "error": "适配器未连接"}
        return {"success": True, "sent": True, "message_id": "m1"}

    def _frequency_get_adjust(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self.get_adjust_fails:
            return {"success": False, "error": "读取失败"}
        return {"success": True, "value": self.adjust.get(args["chat_id"], 1.0)}

    def _frequency_set_adjust(self, args: Dict[str, Any]) -> Dict[str, Any]:
        self.adjust[args["chat_id"]] = float(args["value"])
        return {"success": True}

    def _knowledge_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self.knowledge_fails:
            return {"success": False, "error": "长期记忆未启用"}
        content = self.knowledge.get(args.get("person_id") or args["query"])
        return {"success": True, "content": content or f"你不太了解有关{args['query']}的知识"}

    def _person_get_id_by_name(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True, "person_id": self.person_names.get(args["person_name"], "")}

    def _person_get_id(self, args: Dict[str, Any]) -> Dict[str, Any]:
        # 宿主按平台与 QQ 号算出固定的人物 ID，不认识的人也有 ID，只是 is_known 为假
        key = (args["platform"], str(args["user_id"]))
        return {"success": True, "person_id": self.person_ids.get(key, f"unknown-{args['user_id']}")}

    def _person_get_value(self, args: Dict[str, Any]) -> Dict[str, Any]:
        person = self.people.get(args["person_id"])
        if person is None:
            if args["field_name"] == "is_known":
                return {"success": True, "value": False}
            return {"success": False, "error": "人物不存在"}
        return {"success": True, "value": person.get(args["field_name"])}

    def _config_get(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True, "value": self.config.get(args["key"], args.get("default"))}

    def _maisaka_proactive_trigger(self, args: Dict[str, Any]) -> Dict[str, Any]:
        del args
        return dict(self.trigger_result)


class ManualClock:
    """可手动拨动的毫秒时钟。"""

    def __init__(self, start_ms: int = 1_800_000_000_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, delta_ms: int) -> None:
        self.now_ms += delta_ms
