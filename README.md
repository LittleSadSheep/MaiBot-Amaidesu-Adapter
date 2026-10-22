# AmaidesuBridgePlugin

让 Amaidesu 的直播主播能读写 QQ 群、请常驻 QQ 的麦麦说话。MaiBot 7×24 运行，
Amaidesu 用时才启动，所以由本插件在 MaiBot 插件进程里开一个 MCP 服务
（Streamable HTTP），Amaidesu 作为 MCP 客户端按需连入。

## 工具

| 工具 | 作用 |
|---|---|
| `read_group` | 读群聊记录。默认只返回主播上次查看之后的新消息；第一次读回看 `qq.read_lookback_ms` |
| `send_group` | 以麦麦的 QQ 号发一条文字。消息同步写进 MaiBot 该群的上下文，QQ 麦麦之后接话时知道这是自己说的 |
| `ask_mai` | 交代意图（如"开播了，喊大家来看，链接是……"），唤醒 QQ 麦麦按自己的群聊语气处理一轮 |

只允许操作 `qq.group_ids` 里的群，第一个是默认群。Amaidesu 里的工具全名是
`<MCP 服务名>_<工具名>`，服务名配成 `qq` 时就是 `qq_read_group` 等。

## 水群接管

同一个 QQ 号背后有两个决策循环。主播 `send_group` 成功后，插件把 MaiBot 在该群的
发言频率倍率压到 `takeover.talk_adjust`，持续 `takeover.duration_ms`，每次发群都会续期：

- `talk_adjust = 0`：QQ 麦麦只看不说，@ 也不回；
- `0 < talk_adjust < 1`：按比例少说话，@ 和提及仍按 MaiBot 的 `reply_timing` 配置强制回复。

以下情况会还原原倍率：到期（后台每 5 秒检查一次）、主播调用 `ask_mai`（先把发言权交还，
否则倍率为 0 时可能叫不动 QQ 麦麦）、插件卸载。倍率只存在 MaiBot 内存里，MaiBot 重启后
也会自然回到默认值。MaiBot 的专注模式会无视倍率。

## 配置

首次加载时 MaiBot 按配置模型生成 `config.toml`。至少要填：

```toml
[server]
host = "127.0.0.1"   # Amaidesu 不在同一台机器时改 0.0.0.0，且只在可信网络使用（明文 HTTP）
port = 8767
auth_token = "<随机长字符串>"  # 为空时服务不启动

[qq]
group_ids = ["123456789"]
```

Amaidesu 侧 `config/tools.toml`：

```toml
[tools.mcp.config.servers.qq]
transport = "http"
url = "http://127.0.0.1:8767/mcp"
headers = { Authorization = "Bearer <同一个令牌>" }
```

通用 MCP 工具在 Amaidesu 里默认只对主播可见。Amaidesu 启动时连不上本服务会先占位登记，
后台按 5 秒起步、60 秒封顶的间隔重试，MaiBot 上线后自动补上这组工具，不用重启。

## 开发

```bash
uv venv .venv --python 3.14
uv pip install --python .venv/Scripts/python.exe maibot-plugin-sdk==2.16.0 mcp==1.29.1 uvicorn==0.45.0 starlette==1.3.1 pytest ruff
.venv/Scripts/python.exe -m pytest -q
```

测试用真实 SDK 的 `PluginContext`，在 RPC 层模拟 MaiBot 宿主（`tests/fake_host.py`），
连同 SDK 的参数传递与结果解包一起验证；`tests/test_plugin_mcp.py` 用真实 MCP 客户端走一遍
加载、调用、改配置重启、卸载还原。
