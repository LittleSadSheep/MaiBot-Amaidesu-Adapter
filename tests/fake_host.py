"""测试用的假 MaiBot 宿主。

插件经真实 SDK 的 ``PluginContext`` 发起能力调用，这里在 RPC 层模拟宿主的返回
形状（``{"success": ..., <结果键>: ...}``），从而连同 SDK 的参数传递与结果解包
一起验证，而不是只测插件自己的假对象。
"""

from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import PluginContext

import importlib.util
import sys


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
    """业务逻辑只依赖标准库，单独导入即可，不经过插件入口。"""

    name = "amaidesu_bridge_logic_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "bridge.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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

    def add_message(self, timestamp_s: float, text: str, stream_id: str = STREAM_ID) -> None:
        self.messages.setdefault(stream_id, []).append({"timestamp": str(timestamp_s), "text": text})

    def calls_of(self, capability: str) -> List[Dict[str, Any]]:
        return [args for name, args in self.calls if name == capability]

    def make_context(self, plugin_id: str = "amaidesu.amaidesu-bridge-plugin") -> PluginContext:
        return PluginContext(plugin_id, rpc_call=self.rpc)

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
