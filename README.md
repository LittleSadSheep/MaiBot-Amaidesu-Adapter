# AmaidesuBridgePlugin

让 Amaidesu 的直播主播和常驻 QQ 的麦麦互通：主播能读写 QQ 群、请 QQ 麦麦说话；QQ 麦麦
知道自己在直播那边正在播什么、刚播完什么。MaiBot 7×24 运行，Amaidesu 用时才启动，所以由
本插件在 MaiBot 插件进程里开一个服务（MCP 用 Streamable HTTP，另有一个 JSON 接口），
Amaidesu 按需连入。

## 工具

| 工具 | 作用 |
|---|---|
| `read_group` | 读群聊记录。默认只返回主播上次查看之后的新消息；第一次读回看 `qq.read_lookback_ms` |
| `send_group` | 以麦麦的 QQ 号发一条文字。消息同步写进 MaiBot 该群的上下文，QQ 麦麦之后接话时知道这是自己说的 |
| `ask_mai` | 交代意图（如"开播了，喊大家来看，链接是……"），唤醒 QQ 麦麦按自己的群聊语气处理一轮 |
| `recall_memory` | 回想 QQ 麦麦在这个群里记下的事（长期记忆检索，聊天范围限定为本群） |
| `recall_person` | QQ 麦麦对某个群友的印象：称呼、本群群名片、认识多久 / 来往几次，以及他在本群的相关记忆 |

只允许操作 `qq.group_ids` 里的群，第一个是默认群。

两个回想工具的隐私边界（直播间是公开场合）：记忆检索把聊天范围限定为本群，私聊里形成的记忆
查不出来；按名字找人先按 MaiBot 的称呼找，但这个人在本群没有群名片时不采用，改在本群最近
`qq.person_lookup_days`（默认 30）天的发言人里按群名片 / 昵称找；不用跨全部聊天汇总的人物画像；
只给本群的群名片；输出里不出现 QQ 号。Amaidesu 里的工具全名是
`<MCP 服务名>_<工具名>`，服务名配成 `qq` 时就是 `qq_read_group` 等。

## 水群接管

同一个 QQ 号背后有两个决策循环。主播 `send_group` 成功后，插件把 MaiBot 在该群的
发言频率倍率压到 `takeover.talk_adjust`，持续 `takeover.duration_ms`，每次发群都会续期：

- `talk_adjust = 0`：QQ 麦麦只看不说，@ 也不回；
- `0 < talk_adjust < 1`：按比例少说话，@ 和提及仍按 MaiBot 的 `reply_timing` 配置强制回复。

以下情况会还原原倍率：到期（后台每 5 秒检查一次）、主播调用 `ask_mai`（先把发言权交还，
否则倍率为 0 时可能叫不动 QQ 麦麦）、插件卸载。倍率只存在 MaiBot 内存里，MaiBot 重启后
也会自然回到默认值。MaiBot 的专注模式会无视倍率。

## 直播近况

Amaidesu 开启 `[maibot_sync]` 后，把开播、换环节、收播与场次回顾 POST 到同一端口的
`/live-status`（同一令牌）。每次都是完整快照，直播期间每 60 秒重发一次当心跳。插件缓存在
插件数据目录的 `live_status.json`，MaiBot 重启不丢。

- **规划前注入**（`live_status.inject_planner`）：在请求末尾每轮指令之前插一段
  `<live-status>`。只在三种情况注入：正在直播、下播不到 `live_status.recent_window_ms`
  （默认 12 小时）、直播中超过 `live_status.stale_after_ms` 没收到心跳（此时只说"可能已经
  下播"，不再声称正在直播）。只写绝对时刻，不写"已播 N 分钟"，以免打破提示词前缀缓存。
- **回复前注入**（`live_status.inject_replyer`）：经 `extra_prompt` 附上同一段近况，回复里的
  环节名、开播时刻才不会编错。
- **查询工具** `query_live_status(n_back)`：0 = 当前状态和最近一场回顾；1、2… = 往前第几场的回顾。
  数据来自缓存，Amaidesu 没开也能答。

规划前的钩子每次都会收到整份上下文（这是 MaiBot 钩子的设计）；没有近况时立即原样放行。

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

同步直播近况还要在 Amaidesu 的 `config/infra.toml` 打开：

```toml
[maibot_sync]
enabled = true
url = "http://127.0.0.1:8767/live-status"
token = "<同一个令牌>"
```

## 开发

```bash
uv venv .venv --python 3.14
uv pip install --python .venv/Scripts/python.exe maibot-plugin-sdk==2.16.0 mcp==1.29.1 uvicorn==0.45.0 starlette==1.3.1 pytest ruff
.venv/Scripts/python.exe -m pytest -q
```

测试用真实 SDK 的 `PluginContext`，在 RPC 层模拟 MaiBot 宿主（`tests/fake_host.py`），
连同 SDK 的参数传递与结果解包一起验证；`tests/test_plugin_mcp.py` 用真实 MCP 客户端走一遍
加载、调用、改配置重启、卸载还原。
