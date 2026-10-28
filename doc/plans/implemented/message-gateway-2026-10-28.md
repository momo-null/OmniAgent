# 消息通道（gateway）接入设计 · 已实现

> 状态：已实现并真机验证（2026-10-28）。本文只记机制与边界；具体账号 / 群号等运行数据不入库。
> 代码落点：`gateway/`（管道与派发）、`plugins/messaging/`（工具）、`omni_core/local/loop/*`（唯一的内核能力门）。

## 1. 定位

外部 IM 不是「第二个用户端」，而是**一条代理通道**：

- 服务对象始终是主人；外部消息是「来信 / 事件」，不是「客户请求」。
- 通道负责**收发**，派发负责**跑任务**，插件负责**工具能力**，三层互不越界。
- 因此通道适配器**不做业务加工**：只把平台事件翻译成统一的 `InboundMessage`。

## 2. 分层

| 层 | 路径 | 职责 | 是否给模型可见 |
|---|---|---|---|
| 通道适配器 | `gateway/channels/<name>.py` | 长连接收事件、调平台 API 发消息、归一/过滤 | 否（模型不该看见管道） |
| 网关进程 | `gateway/channel_gateway.py` | 入口、消息→项目/会话路由、拉起各通道 | 否 |
| 派发 | `gateway/dispatch.py` | 建任务、跑 ToolLoop、回发结果、失败直推 | 否 |
| 工具插件 | `plugins/messaging/plugin.py` | `msg_send_text` / `msg_send_card` / `msg_recent` | 是（function calling） |
| 内核 | `omni_core/local/loop/*` | 只提供一个运行级能力门 `tools_enabled` | 否 |

新增通道 = **一个适配器文件 + 一段配置**，派发 / 工具 / 调度器零改动（`wecom.py` 即该抽象的可替换性验收件）。

## 3. 通道协议（三件套）

`gateway/channels/base.py`：

- `InboundMessage`：`channel / chat_id / user_id / text / raw / reply / allow_tools`
- `ReplyChannel`：`send_text()` / `send_card()`（派发与工具只依赖这层，不感知平台 API）
- `ChannelAdapter`：`connect(on_message)` / `reply_to(chat_id)` / `broadcast(text)` / `close()`

`allow_tools` 默认 **false（fail-closed）**：新接入的通道不显式声明就不具备任何工具能力。

## 4. 通道清单

| 通道 | 协议 | 状态 |
|---|---|---|
| `feishu` | lark-oapi WebSocket 长连接 + im/v1 发送 | 实装（含消息去重 / 白名单） |
| `napcat` | OneBot 11：正向 WebSocket 收 + HTTP API 发 | 实装 |
| `wecom` | — | stub（抽象可替换性验收件） |

## 5. NapCat / OneBot 11 接入要点

- **入站**：后台线程连 `ws_url`（缺省 `ws://127.0.0.1:3001`），指数退避 1→30s 重连；只消费 `post_type == "message"`。
- **必须忽略 `post_type == "message_sent"`**：否则本机发出的消息经 WS 回流成新输入，形成自激循环。
- **出站**：`POST {http_url}/send_private_msg | /send_group_msg`，可选 `Authorization: Bearer <token>`。
- **归一双兼容**：`message` 可能是**消息段数组**（推荐 `messagePostFormat=array`）或 **CQ 码字符串**，两种都解析；非文本段转占位（`[图片]` / `[表情]` …）。
- **会话键**：`chat_id = private:<QQ号>` / `group:<群号>`，与其它通道共用 `channel:chat_id` 的会话语义。
- **触发规则**（全部配置化，不写死）：私聊缺省全回（`allow_users` 非空即白名单）；群聊缺省 `require_at: true`（只有 @ 本机才响应），`allow_groups` 非空即群白名单。
- **其它防抖**：`message_id` 去重（上限 2000）；长文本按 1200 字符分条（QQ 单条字节上限）。
- **排障要点**：NapCat 默认 `network.httpServers / websocketServers` 为空 → 端口不监听、消息只落它自己的日志。接入前必须确认 3000/3001 在听。快速登录参数不保证生效，掉登录态后需重新扫码。

### 5.1 图片（多模态附件）

QQ 图片段默认只被压成 `[图片]` 占位。要让模型"看见"，必须把图作为**多模态输入**送进去——
对外通道是纯对话（`tools_enabled=false`），工具化的看图（vision 插件 / OCR）在这条链上用不了。

```
通道下载 → InboundMessage.attachments → dispatch → TaskSpec.attachments → build_user_content
                [{"type": "image", "data_url": "data:image/jpeg;base64,..."}]
```

- **通道**：`image_urls()` 取消息段 `data.url`（`file` 为 http 地址时兜底）；`fetch_image_data_url()` 流式下载、**边读边判 ≤4MB**、非 `image/*` 丢弃、失败返回空（降级为占位）；**只取首张**（连发多图全带上会顶爆 token）。下载发生在**群聊 @ 过滤之后**——没 @ 她的群消息不下载。配置 `images: true` 可关。
- **内核**：`build_user_content(text, attachments)` 无附件返回 `str`（逐字节零回归），有附件返回内容块数组（`input_text` + `input_image`）；**未知 `type` 忽略**，不影响文本部分（fail-open）。
- **不进会话历史**：落盘仍写 `objective` 文本（含 `[图片]` 占位），base64 只活在当轮请求里。否则 jsonl 会被撑爆，且每轮重建上下文都要重传（每张图约 1k+ tokens）。
- **为什么用 dict 契约**：`attachments: List[Dict]` 而非 `images: List[str]`——以后加文件 / 音频只扩一个 `type` 分支，内核契约不再动第二次。

## 6. 能力门 `tools_enabled`（唯一的内核改动）

语义：`False` = 本次运行为纯对话。此时

- 能力插件（registry）、MCP、内核元工具（`task_done` / `verify` / `escalate` / `record` / `dispatch`）**全部不装配**；
- 模型请求里连 `tools` 字段都不存在 → tool_call **物理不可达**（不是「调用后被拒」，后者仍会给模型可乘之机并白烧轮次）；
- 纯文本收尾由 `plain_finish_judge` 路径正常收口（`task_mode=oneshot`）。

改动点（净 +20 行，其余为注释）：

| 文件 | 内容 |
|---|---|
| `omni_core/local/loop/core.py` | `ToolLoop(..., tools_enabled=True)` → `self.tools_enabled`；False 时**不构建 MCP 客户端** |
| `omni_core/local/loop/sdk_bridge.py` | 按开关传 `tools` / `mcp_servers`，并透传 |
| `omni_core/brain/sdk_loop.py` | `_assemble_agent_stack` 的装配分支：False 时 `tools=[]`、不调 `build_meta_tools`、不挂 `StopAtTools` |

为什么必须在内核：**「不给工具」发生在 Agent 装配层**，通道 / 插件都可以决定「要不要」，但无法替内核装配。它与 `max_steps` / `task_mode` 同级，是通用维度（语音模式、roleplay 禁工具可复用），不含任何平台判断。

边界判定口径：内核出现**具体平台名 / 业务词** = 污染；出现**布尔开关 + 通用语义** = 能力。自查：`rg -i "napcat|qq|feishu|wechat" omni_core/` 应为空。

## 7. 场合自述（通道注入给模型的事实）

通道在 user 文本首行写四要素，**用自然叙述而非机器标记**：

```
（这是你的 QQ。你在以主人的身份跟 「昵称」(号码) 说话，用第一人称「我」，
回复会直接发给对方，不用请谁转达。你代表他，但不替他做决定——
钱、账号、承诺、隐私、评价他人这类问题，一律回「我确认下再回你」。）
<原文>
```

四要素缺一即出事故（均为实测）：

1. **这是你的 QQ**：缺了会被理解成「别人转给我看的信息」。
2. **以主人身份 / 第一人称**：模型否则会把自己放在「帮主人起草回复」的位置。
3. **回复直接发出**：缺了会输出「我帮你想好了，就这么回他…」。
4. **代答边界**：可以说「话」，不能做「决定」。

群聊额外带说话人（一个群共用一个会话，群里多人在说话）。

## 8. 路由与项目归属

`channel_gateway._route_chat` 两级匹配，精确优先：

```
chat_routes:
  "napcat:private:<QQ号>": { project_id: xxx }   # 单会话
  "napcat:*":              { project_id: qq  }   # 整条通道
```

未配 `project_id` 的消息会成为**临时任务**：跑完即从任务索引删除（`TaskStore.remove`），任务列表里看不到，只剩会话 jsonl。配了则持久归属项目、界面可见。

会话 id 规则：`s_<channel>_<md5(channel:chat_id)[:12]>`，同一聊天窗口恒定同一会话。`ProjectStore.list_sessions()` 直接扫 `*.jsonl`，**重命名/归档会话文件会让它从列表消失**。

## 9. 跨进程发送

`msg_send_*` 取通道默认查本进程注册表（`gateway.channels.context`），而注册表只在**网关进程**里有实例；在 Web 后端进程里调用会返回「无可用通道」，模型于是绕去 shell 调平台 API（实测绕了 9 步）。

解法：`gateway/channel_gateway.build_adapter(name)` —— 按 `config.channels.<name>` 构造**只发送、不连接**的句柄，插件在注册表未命中时回退调用它。QQ 走 HTTP，因此无需长连接；飞书认证在 `connect` 里，跨进程发送仍不可用（应在网关侧发）。

## 10. 跨会话往来查询 `msg_recent`

动机：一个会话里做过的事，换个会话（Web ↔ 外部 IM）就失忆，只能 shell 翻文件。

实现（`plugins/messaging`）：扫 `projects/*/s_*.jsonl`（按 mtime 取最近 24 个），把 `user`+`assistant` 配成「对方说 → 我回」，按时间倒序返回 `when / channel / project / who / said / my_reply`；`_strip_ctx()` 剥掉通道写的场合前缀并解析对方身份。

边界：纯对话运行（`tools_enabled=false`）下模型**调不到**它 —— 对外不提供任何可查内部数据的工具。

## 11. 观测与常见故障

| 现象 | 原因 |
|---|---|
| 通道「已连接」但收不到消息 | 平台侧未开启对应的上报/服务端（如 OneBot 的 `httpServers` / `websocketServers`） |
| 同一条消息回复多遍 | 多个网关实例并存（桌面壳会自动拉一个 + 手动又起一个），看 3001 上有几个客户端 |
| 消息处理了但列表里看不到 | 临时任务被删除；或会话文件被改名 |
| 模型反复 shell 翻文件找消息 | 缺 `msg_recent` 或该进程未加载新插件（插件注册是进程级、装载幂等，改完必须重启） |

## 12. 未做（有意留白）

- 入站**推送**（外部消息主动通知主人）：当前由主人侧主动查询。
- **接管/纠偏**：运行中介入外部对话（M8 软注入对通道会话生效待验证）。
- 通道**自动发现**：目前新增通道需在 `_register_factories` 手动加一行。
