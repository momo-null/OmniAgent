# 右侧栏（RightDock）与产物预览 · 交接文档

- 日期：2026-10-26
- 状态：**核心链路已实现、构建与测试通过、用户已手动确认可用**；收起态排列与展开/收起动画已按反馈补齐（见 §5）
- 参考稿：用户提供的 3 张截图（对话结果内产物卡片 + 右侧栏文件预览 / 折叠态快捷入口 / 折叠切换按钮）

---

## 1. 需求

1. 会话结果中如果有**产物**（agent 写出的文件），直接在结果里可见（卡片）；
2. 点击产物 → **右侧栏预览**（markdown 渲染 / 文本 / 图片）；
3. 右侧栏**默认收起**；
4. 右侧栏是完整侧栏：**文件、浏览器、git 变更、终端**，原来的 **Debug** 面板移入其中。

## 2. 实现总览（数据流）

```
agent 工具落盘（write_file / edit_file / screenshot）
  → 产物登记表 tasks/<task_id>/artifacts.json（omni_core/local/artifacts.py，按 path 去重、cap 200）
  → run_task 终态：tmp/ 清理前把登记过的 tmp 文件抢救复制到 tasks/<task_id>/artifacts/ 并回写路径
  → chat 收尾：本轮新增产物（ts 过滤）挂到最终 agent 消息 extra.artifacts（随会话 jsonl 持久化）
  → 前端：AgentTurn 渲染「产物 (N)」卡片 → 点击 openDock("files", path) → 右侧栏 FilesTab 预览
  → 右侧栏文件页与 GET /api/runtime/artifacts 同源，运行中每 3s 轮询实时刷新
```

### 后端（均已落盘，`python -m py_compile` 通过）

| 文件 | 改动 |
|---|---|
| `omni_core/local/runtime_paths.py` | 新增 `task_artifacts_dir()` / `task_artifacts_json()` |
| `omni_core/local/artifacts.py` | **新文件**：登记表 `record_artifact` / `load_artifacts` / `preserve_tmp_artifacts`（未绑定 task 静默跳过，全 try 兜底） |
| `plugins/filesystem/plugin.py` | `write_file` / `edit_file` 成功后登记产物 |
| `environments/host/backend.py` | `screenshot` 落盘后登记产物 |
| `omni_core/local/loop/graph_runner.py` | `_cleanup_task_tmp` 删 tmp 前先 `preserve_tmp_artifacts`（tmp 外产物路径原样保留） |
| `backend/api/routers/workspace_api.py` | **新文件**：`GET /api/runtime/artifacts`、`GET /files/read|raw`（允许集 = 该任务登记产物 + 任务目录树）、`GET /git/status|diff`（未跟踪文件合成新文件伪 diff）、`POST /terminal`（独立子进程，超时上限 300s） |
| `backend/api/router_runtime.py` | 挂载 workspace router + re-export |
| `backend/api/routers/chat_runtime.py` | `_dispatch_chat` 收尾把本轮新增产物挂到 `_extra["artifacts"]`（cap 12，含 path/name/bytes/tool/ts） |

### 前端（`tsc && vite build` 通过）

| 文件 | 改动 |
|---|---|
| `web/src/types/index.ts` | `ArtifactInfo` / `FilePreview` / `GitStatusResp` / `GitDiffResp` / `TerminalResp`；`ChatMsg.extra.artifacts` |
| `web/src/api/client.ts` | `workspaceApi`（artifacts / readFile / fileRawUrl / gitStatus / gitDiff / terminal） |
| `web/src/store/taskStore.tsx` | dock 状态：`dockOpen / dockTab / dockPreviewPath / openDock / closeDock / setDockTab`、`artifactsVersion / bumpArtifactsVersion`（类型 `DockTab = files|browser|git|terminal|debug`） |
| `web/src/components/RightDock.tsx` | **新文件**：右侧栏壳——收起态 30px 图标轨（5 图标），展开态 440px 图标页签头 + 页签体 |
| `web/src/components/dock/FilesTab.tsx` | 产物列表 + 预览（md→Markdown 渲染 / 文本→pre / 图片→`files/raw` / 二进制→下载）；运行中 3s 轮询 |
| `web/src/components/dock/BrowserTab.tsx` | URL 栏 + iframe + 系统浏览器打开（URL 持久化 localStorage） |
| `web/src/components/dock/GitTab.tsx` | 仓库路径（localStorage）+ 分支/变更清单 + 单文件 diff 着色 |
| `web/src/components/dock/TerminalTab.tsx` | 命令执行回显、cwd 保持、↑/↓ 历史、清屏 |
| `web/src/components/DebugPanel.tsx` | 重构为可嵌入页签（去掉固定宽/外框/关闭按钮），log+memory 两页签不变 |
| `web/src/pages/Chat/index.tsx` | 挂载 `<RightDock />`（替代原 Debug 轨）；`ArtifactCards` 产物卡片（点卡片 `bumpArtifactsVersion + openDock`） |

## 3. 验证状态

- `python -m pytest tests/test_artifacts.py`：**4/4 通过**（登记去重 / tmp 抢救 / files-read 允许集 / 端点形状）
- `tests/test_m0_tool_plugin.py + test_api_integration.py` 组合中 `test_tools_lists_three_classes` 失败——**stash 前后均复现，属既有用例间干扰，与本次改动无关**（单跑通过）
- 端点冒烟（TestClient + 重启后的真实后端）：`git/status`(200, branch=main, 16 entries)、`git/diff`(200)、`terminal`(200, echo 回显)、`artifacts`(200) 全通
- `npm run build` 通过（chunk >500kB 警告为既有）
- **用户已手动确认**：点击产物卡片 / 侧栏交互正常可用
- 后端已被重启加载新代码（`:8000` 常驻实例，restart 日志在 `backend.restart.log`）
- 已播种演示任务 `t_9af9c2d15545`（「右侧栏产物演示任务」，含 md+png 两个登记产物），可用于人工验收；验收后可删

## 4. 关键设计与坑（务必读）

1. **产物只认工具真实落盘**：shell 重定向等不登记（不猜）；`record_artifact` 未绑定 task 时静默返回 None，绝不影响工具主流程。
2. **tmp 抢救时序**：`run_task` 的 finally 里 `_cleanup_task_tmp` → 先 `preserve_tmp_artifacts` 再 rmtree；chat 收尾读登记表发生在其后，读到的已是持久路径。
3. **files/read 允许集**：仅「该 task_id 的登记产物 + tasks/<tid>/ 目录树」，防任意文件读；git/terminal 接受用户自给路径（与 shell_exec 同风险档，本应用即宿主机 agent 控制台）。
4. **FilesTab 点击时序**：产物卡点击 = `bumpArtifactsVersion()`（触发列表刷新）+ `openDock("files", path)`；任务切换 effect 与版本号 effect 已拆开，版本号 effect **不得**清 `dockPreviewPath`，否则刚点开的预览被打断（已修，勿回退）。
5. **IAB 自动化点击冻结是环境怪癖**：ZCode 内嵌浏览器里 dispatch 的 DOM 事件全部无法触发 React handler（连原有「定时任务」切换也无效），真实浏览器无此问题（用户手动验证通过）。勿据此误判前端代码坏了。
6. `web/dist` 由后端同源直出（桌面壳同一份），改前端后必须 `cd web && npm run build`。

## 5. 用户反馈的处理（2026-10-26 第二轮，已完成）

1. **排列对齐参考稿**：收起态从 30px 图标轨改为「带文字的快捷入口列表（查看产物 / 打开浏览器 / 查看变更 / 打开终端 / Debug）+ 底部『产物 (N)』可折叠清单」，产物行点击直达文件页预览（`RailContent`）。
2. **展开/收起动画**：容器 `width` transition（MUI enteringScreen/leavingScreen 时长）+ 内层定宽（收起 208px / 展开 440px）+ `overflow: hidden` 裁切，聊天区随宽度逐帧回流。

两项均已实现并通过 `npm run build`；截图确认收起态布局与产物清单实时数据正确。刷新 `:8000` 页面即可看到。

## 6. 建议下一步（按序）

1. 人工过一遍 5 个页签（浏览器 iframe 对 X-Frame-Options 站点会白屏，属预期，有「系统浏览器打开」兜底）；
2. 可选打磨：面板宽度可拖拽、产物卡片出现在**运行中**的实时过程流（当前只在终态消息上）、terminal 换 PTY 支持交互式程序；
3. 删除演示任务 `t_9af9c2d15545`。
