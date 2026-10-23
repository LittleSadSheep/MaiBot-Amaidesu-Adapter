"""联调用：不连 MaiBot，用假宿主按本机 config.toml 起适配器 MCP 服务。

供 Amaidesu 侧在没有 MaiBot 运行时验证连接、鉴权与工具调用。群名单固定为假宿主里的
测试群，其余（端口、令牌、接管参数）沿用插件目录下的 config.toml。按 Ctrl+C 退出。

    .venv/Scripts/python.exe tests/serve_fake_host.py
"""

from typing import Any, Dict

import asyncio
import time
import tomllib

from fake_host import GROUP_ID, PLUGIN_DIR, FakeHost, load_plugin_package


async def main() -> None:
    plugin_module = load_plugin_package()
    config: Dict[str, Any] = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    config.setdefault("qq", {})["group_ids"] = [GROUP_ID]

    host = FakeHost()
    host.add_message(time.time() - 120, "群友A: 今晚几点开播？")
    host.add_message(time.time() - 60, "群友B: 蹲一个")
    plugin = plugin_module.create_plugin()
    plugin._set_context(host.make_context())
    plugin.set_plugin_config(config)
    await plugin.on_load()
    print(f"假宿主适配器服务已启动，测试群 {GROUP_ID}", flush=True)
    try:
        while True:
            await asyncio.sleep(1)
            # 把插件发起的能力调用逐条打印出来，联调时能看到 Amaidesu 实际触发了什么。
            while host.calls:
                capability, args = host.calls.pop(0)
                print(f"能力调用 {capability} {args}", flush=True)
    finally:
        await plugin.on_unload()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
