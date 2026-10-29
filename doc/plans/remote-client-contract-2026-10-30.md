# 远程客户端契约（Remote Client Contract）· 2026-10-30

> 状态：**契约冻结**（`memory/sync` 端点待实现，见 §3）。
> 本文件描述**任意外部 HTTP 客户端**如何使用运行时。内核不感知客户端的实现、
> 平台与形态——Web UI 与通道网关只是已知客户端中的两个，本文对所有客户端一视同仁。
> 语义上游：`unified-chain-and-voice-2026-10-28.md` §3.1（mode 收窄）、§4（lane）。

## 0. 一句话

任意外部客户端在局域网/远程与同一运行时对话：同一 persona、同一记忆、同一
工具面；客户端只依赖本契约，不感知内核实现。内核也不知道客户端是谁。

## 1. 契约（冻结）

| 用途 | 接口 | 备注 |
|---|---|---|
| 健康/探活 | `GET /health` | 无需鉴权，1s 超时 |
| 发起对话 | `POST /api/runtime/chat` | body: `{messages, mode:"chat", persona?}`；返回 `task_id` |
| 收回复 | `GET /api/runtime/stream?task_id=…&sub=<client-id>` | SSE；`sub` 旁路订阅（多播，与其他订阅者互不抢事件）；关注 `message` 通道 |
| 打断 | `POST /api/runtime/stop` | body: `{task_id}`；对齐语音 barge-in |
| 会话回放 | `POST /api/runtime/memory/sync` | body: `{session_id, messages:[{role,content,seq}]}`；幂等；**待实现** |

鉴权：非回环来源必须 `Authorization: Bearer <server.auth_token>`；loopback 豁免
（桌面同源 UI 无感）、`/health` 豁免（外部探活）、静态 UI 豁免（页面可加载，API
另行拦截）。网关定位：公网面统一鉴权在 nginx，服务侧 token 即 nginx 转发时携带
的内部凭证（`proxy_set_header Authorization`），不是用户体系。逃生门
`server.allow_tokenless_lan: true`（默认关，启动大字警告）。

`mode` 语义：对话取向纪律（chat=对话纪律，task=干活进工作 lane），字段与取值不变。

客户端故障隔离：任一客户端崩溃/失联不影响运行时与其他订阅者（SSE 多播语义保证）。

## 2. 契约演进纪律

- **冻结 + 向后兼容**：新增可选字段允许；改变既有语义必须版本化（新路径或版本参数）。
- **契约测试**：内核侧为以上端点维护 fixture，任何表面/客户端以同一 fixture 验收；
  漂移检测点在契约，不在客户端行为。
- 客户端离线期间产生的会话，回联网后经 §1 会话回放进入记忆管线（幂等）。

## 3. 内核待办

- `POST /api/runtime/memory/sync`：外部客户端的会话回放端点——`session_id` +
  消息序号幂等去重，喂入 capture 缓冲后走既有提炼/写链管线。语义即「离线会话
  回放」，不为任何特定客户端定制，不碰写链纪律。
- §2 契约测试 fixture。
