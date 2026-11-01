# MaiBot-Amaidesu-Adapter

让 Amaidesu 的直播主播和常驻 QQ 的麦麦互通：主播能读 QQ 群、托 QQ 麦麦在群里说话；QQ 麦麦
知道自己在直播那边正在播什么、刚播完什么、以前每一场做了什么；主播的长期笔记（答应过的事、
老梗、长线目标、关于我自己）两边共用一份，QQ 里答应的事直播那边也知道。MaiBot 7×24 运行，
Amaidesu 用时才启动，所以由本插件在 MaiBot 插件进程里开一个服务（MCP 用 Streamable HTTP，
另有两个 JSON 接口），Amaidesu 按需连入。

## 工具

| 工具 | 作用 |
|---|---|
| `read_group` | 读群聊记录。默认只返回主播上次查看之后的新消息；第一次读回看 `qq.read_lookback_ms` |
| `ask_mai` | 托 QQ 麦麦在群里说话：交代要说的事、为什么说和必须带上的事实（如"开播了，喊大家来看，链接是……"），唤醒 QQ 麦麦按自己的群聊语气处理一轮。`must_say` 标出一定要说到的事 |
| `send_group` | 只在打开 `qq.direct_send` 时提供：以麦麦的 QQ 号发一条文字，同步写进 MaiBot 该群的上下文 |
| `recall_memory` | 回想 QQ 麦麦在这个群里记下的事（长期记忆检索，聊天范围限定为本群） |
| `recall_person` | QQ 麦麦对某个群友的印象：称呼、本群群名片、认识多久 / 来往几次，以及他在本群的相关记忆 |

只允许操作 `qq.group_ids` 里的群，第一个是默认群。这份名单也是"与直播互通的群"（下称白名单群）：
直播近况与共享笔记只注入这些群，QQ 麦麦记约定的工具也只在这些群里能用。

插件给 QQ 麦麦（MaiBot 规划器）的工具：

| 工具 | 作用 |
|---|---|
| `query_live_status` | 查直播近况与各场回顾：`n_back` 往前第几场，`keyword` 按关键词找场次（新的在前，最多三场） |
| `record_promise` | 把刚在群里明确答应、以后要在直播里兑现的事记进共享笔记；只在白名单群能用 |

两个回想工具的隐私边界（直播间是公开场合）：记忆检索把聊天范围限定为本群，私聊里形成的记忆
查不出来；按名字找人先按 MaiBot 的称呼找，但这个人在本群没有群名片时不采用，改在本群最近
`qq.person_lookup_days`（默认 30）天的发言人里按群名片 / 昵称找；不用跨全部聊天汇总的人物画像；
只给本群的群名片；输出里不出现 QQ 号。Amaidesu 里的工具全名是
`<MCP 服务名>_<工具名>`，服务名配成 `qq` 时就是 `qq_read_group` 等。

## 群里只留一张嘴

同一个 QQ 号背后有两个决策循环，但群里默认只有 QQ 麦麦说话。一句群消息要说得像同一个麦麦，
需要群里一直在聊什么、对群友的了解、在这个群的说话方式、自己之前说过什么——这些都在 MaiBot 里；
直播那边只多知道"此刻在直播、直播里刚发生了什么"，一句托付就说得清。所以分工是：

- 主播负责"说什么、为什么"：用 `ask_mai` 交代要点和直播这边的来龙去脉，必须带上的事实写进去；
- QQ 麦麦负责"怎么说、什么时候说、说不说"：托付会作为一条插件任务写进该群的上下文并强制它跑一轮规划，
  它带着完整的群聊上下文决定怎么说，之后也知道自己为什么说了那句。

托付的结果不保证：宿主让 QQ 麦麦"自行决定是否回复"，它可能看气氛不说。`must_say = true` 时插件把
优先级标高、在托付里写明"一定要说到"，只能大大提高说的概率。主播在下次读群时看得到它说了什么。
同一个群两次托付至少隔 `qq.min_ask_interval_ms`（默认 60 秒），唤醒失败不计入。

读群不受限：主播随时可以读群，在直播里聊群里的事。

## 直接发群与水群接管

需要主播亲手发群时（比如直播里正和群友连线，每句话都要和画面对上），打开 `qq.direct_send`，
主播的工具清单里才会出现 `send_group`。这时同一个群有两方以麦麦的名义说话，主播 `send_group`
成功后，插件把 MaiBot 在该群的发言频率倍率压到 `takeover.talk_adjust`，持续 `takeover.duration_ms`，
每次发群都会续期：

- `talk_adjust = 0`：QQ 麦麦只看不说，@ 也不回；
- `0 < talk_adjust < 1`：按比例少说话，@ 和提及仍按 MaiBot 的 `reply_timing` 配置强制回复。

以下情况会还原原倍率：到期（后台每 5 秒检查一次）、主播调用 `ask_mai`（先把发言权交还，
否则倍率为 0 时可能叫不动 QQ 麦麦）、关掉 `qq.direct_send`（下一次检查时全部还原）、插件卸载。
倍率只存在 MaiBot 内存里，MaiBot 重启后也会自然回到默认值。MaiBot 的专注模式会无视倍率。

两方都能说话时，接管只能缓解、去不掉这些情况：主播第一句发出前 QQ 麦麦可能已经回了同一条消息；
倍率为 0 时群友 @ 麦麦没人回（主播要主动读群才看得到）；主播只看得到读群读到的那几十条，
可能接错话题或换成直播里的口吻。

## 直播近况

Amaidesu 开启 `[maibot_sync]` 后，把开播、换环节、收播与场次回顾 POST 到同一端口的
`/live-status`（同一令牌）。每次都是完整快照，直播期间每 60 秒重发一次当心跳。插件把快照缓存在
插件数据目录的 `live_status.json`，各场回顾存在 `live_recaps.json`（默认每场都留，`live_status.keep_recaps`
可限场数），MaiBot 重启不丢。下面两种注入都只对白名单群，别的群和私聊不注入。

- **规划前注入**（`live_status.inject_planner`）：在请求末尾每轮指令之前插一段
  `<live-status>`。只在三种情况注入：正在直播、下播不到 `live_status.recent_window_ms`
  （默认 12 小时）、直播中超过 `live_status.stale_after_ms` 没收到心跳（此时只说"可能已经
  下播"，不再声称正在直播）。只写绝对时刻，不写"已播 N 分钟"，以免打破提示词前缀缓存。
- **回复前注入**（`live_status.inject_replyer`）：经 `extra_prompt` 附上同一段近况，回复里的
  环节名、开播时刻才不会编错。
- **查询工具** `query_live_status(n_back, keyword)`：0 = 当前状态和最近一场回顾；1、2… = 往前第几场的回顾；
  给了关键词就找回顾里提到它的场次。数据来自缓存，Amaidesu 没开也能答。这是 QQ 麦麦了解直播内容
  的主路线；整场记录回放（下一节）是可选的。

规划前的钩子每次都会收到整份上下文（这是 MaiBot 钩子的设计）；没有近况时立即原样放行。

回复前注入（近况与下一节的共享笔记）经 `extra_prompt` 进入回复请求。MaiBot 用复古回复模板时
（`debug.replyer_retro_prompt`，回复模型是 mai-replier 时自动打开），这段内容靠模板里的
`{extra_info_block}` 占位符放进提示词；自定义了模板（如 `data/custom_prompts/zh-CN/retro_replyer.prompt`）
要保留这个占位符，否则回复模型看不到近况与笔记，只能靠规划器写下的回复理由。MaiBot 会缓存模板，
直接改文件后要重启 MaiBot 才生效（经 WebUI 改会自动清缓存）。

## 共享笔记

主播的长期笔记里"身份"那一层——答应过的事、老梗与口头禅、长线目标、关于我自己——两边都用时
正本放在这里（插件数据目录的 `notes/MEMORY.md`，每次写入在 `notes/history.jsonl` 留底，留最近 30 条）。
Amaidesu 开播前经 `/notes`（同一端口、同一令牌）拉取，收播整理后带版本号推回；MaiBot 没开时
Amaidesu 用本地缓存照常开播，之后自动补推。只用 Amaidesu 时这份不存在。

- **注入**（`notes.inject_planner` / `notes.inject_replyer`）：白名单群的规划请求里，笔记插在系统提示词
  之后、聊天记录之前（整份不常变，吃得上前缀缓存）；回复请求经 `extra_prompt` 附上。只给上面四节与人
  自己加的节，去掉标签；"不确定""搁置"两节和答应某位直播观众的条目（标签 `对 昵称#编号`）不给。
- **记约定**（`notes.record_tool`）：`record_promise` 把一句话追加到"答应过的事"末尾，标
  `〔未兑现 · 来自 QQ群〕`。限长 `notes.max_promise_chars`（默认 80 字）、同一句不重复记、QQ 记下而
  未兑现的最多 `notes.max_open_qq_promises` 件（默认 10），正文里的标签括号会被换掉。白名单外的会话
  里，这个工具会从规划候选里拿掉，被调到也拒绝。之后的整理、淡出、标兑现都由 Amaidesu 收播时做。
- **接口** `POST /notes`：`{"action": "get"}` 取全文；`{"action": "put", "text", "expected_version", "reason",
  "changes"}` 整份写，版本对不上回 409（Amaidesu 会重读重做）。版本号 = 全文去掉首尾空白后按 UTF-8
  取 SHA-1，与 Amaidesu 同一算法。
- **名字核对**：Amaidesu 每次请求都带上主播人设里的名字，插件与 `bot.nickname`、`bot.alias_names` 比对，
  结果放进应答（`names.match`）；对不上时两边日志各告警一次，请手动对齐。人设本身不同步。

笔记格式由 Amaidesu 定义，插件只认节名、`对 …#编号`、`来自 …` 与约定状态这几样；格式变了两边要一起改。

## 直播进记忆（可选）

开启 `live_memory.enabled` 后，Amaidesu 每场回顾写好时把整场记录（观众弹幕与付费留言、联动对象
发言、主播发言）推到同一个 `/live-status`（`type="transcript"`）。插件把它按原始时间回放进一个
固定的直播间会话（平台 + 房间号当群号，群名为"B站直播间"这类），由 MaiBot 自己的聊天摘要吸收
进长期记忆。

MaiBot 只在麦麦自己在某个会话里发出消息后才检查要不要写聊天摘要，所以回放时：

- 观众、联动对象的话经插件的消息网关 `amaidesu_live_room` 作为入站消息送入，带原始时间；
- 主播的话由插件以麦麦身份发进同一会话（连续几句合成一条，不超过 `live_memory.merge_chars` 字）。
  网关发送端不外发、只回成功，`send_service.after_build_message` 钩子把这条消息的时间改回原始时刻；
- 回放前把该会话的发言频率倍率设为 0：MaiBot 只静默接收、不进规划器。网关发送端只认正在回放
  的那一条，麦麦自己想在直播间说的话一律回失败，不会发出、也不会入库。

回放任务与进度存在插件数据目录的 `live_memory/` 下，插件或 MaiBot 重启后接着放；放完的场次记下
编号，Amaidesu 重复推送时直接忽略。卡住的场次（如名单没放行）留在队列里，Amaidesu 再推同一场或
下一场推来时重试。

要让 QQ 那边查得到直播记忆，MaiBot 侧还要改两处配置（不改源码）：

```toml
# config/adapter_policy.toml：放行本插件的直播间网关（群号 = 直播间房间号）
[[adapters]]
plugin_id = "amaidesu.amaidesu-adapter"
platform = "bilibili"

[adapters.group]
allow_ids = ["<房间号>"]
```

```toml
# config/bot_config.toml：把直播间和要互通的 QQ 群、私聊编进同一个共享记忆组
[[a_memorix.shared_memory_groups]]
targets = [
    { platform = "bilibili", item_id = "<房间号>", rule_type = "group" },
    { platform = "qq", item_id = "<群号>", rule_type = "group" },
]
```

共享记忆组只认已经存在的会话：直播间会话在第一次回放后才出现。另外几点：

- 直播间会话第一次建立（或闲置被 MaiBot 回收后重建）时，第一条观众消息可能赶在倍率设为 0
  之前触发一次规划；想彻底避免，可在 `chat.reply_timing.talk_value_rules` 里把直播间的发言频率设为 0。
- 聊天摘要每次只总结最近 `a_memorix.integration.chat_summary_writeback_context_length` 条（默认 36）。
  主播长时间不说话、其间弹幕超过这个数时，多出的部分不进摘要；调大它能缓解，对 QQ 群同样生效。
- 麦麦每发一条消息，MaiBot 都会对最近说话的人做一次人物事实提取，一场回放会有几十到上百次模型调用。

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
share_notes = true       # 共享长期笔记（默认开），接口地址由 url 推出：/notes
send_transcript = false  # 整场记录回放（默认关），要开还得打开插件的 live_memory.enabled
```

## 插件类型与热更新

manifest 里 `plugin_type = "adapter"`：本插件和 SnowLuma 等适配器一起跑在 MaiBot 的内置插件进程里
（普通插件在另一个第三方进程）。所以：

- 本插件里的崩溃、长时间阻塞可能连累同进程的 QQ 适配器；
- WebUI 的"适配器下线"会把本插件一起卸载；
- WebUI 聊天页管理适配器放行策略的入口只认 adapter 类型，直播间会话可以在那里按本插件管理。

热更新规则（与插件类型无关）：

- 改 `config.toml` 只触发 `on_config_update`，立即生效，不重启插件进程；服务地址或令牌变了才重启
  本插件的 MCP 服务。
- 改插件目录里任何 `.py` 或 `_manifest.json`（包括 `tests/` 里的）都会让 MaiBot 重启**全部**插件运行
  进程，QQ 适配器也会跟着重连。MaiBot 常驻运行时，改代码最好挑没人聊天的时候。
- Amaidesu 侧的 MCP 连接在插件重启期间调用会失败一次，下一次调用时自动重连；整场记录与近况推送
  失败会按 5 秒、30 秒、2 分钟重试（插件明确拒收时不重试）；共享笔记推不上去时先存在 Amaidesu 本地，
  按 30 秒、2 分钟、10 分钟补推。

## 开发

```bash
uv venv .venv --python 3.14
uv pip install --python .venv/Scripts/python.exe maibot-plugin-sdk==2.16.0 mcp==1.29.1 uvicorn==0.45.0 starlette==1.3.1 pytest ruff
.venv/Scripts/python.exe -m pytest -q
```

测试用真实 SDK 的 `PluginContext`，在 RPC 层模拟 MaiBot 宿主（`tests/fake_host.py`），
连同 SDK 的参数传递与结果解包一起验证；`tests/test_plugin_mcp.py` 用真实 MCP 客户端走一遍
加载、调用、改配置重启、卸载还原。
