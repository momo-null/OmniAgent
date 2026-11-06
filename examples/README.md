# 配置示例（examples/）

这里的目录树**镜像 `~/.omniagent/`**：把 `examples/` 下面的东西按同名路径放进
`~/.omniagent/`，就得到一份「每个键都有注释说明」的初始配置。

```
examples/                                  →  ~/.omniagent/
├── models.json            模型端点唯一真源
├── config.yaml            引擎旋钮（brain / runtime / security / knowledge / skill …）
├── mcp.json               MCP server 清单
├── voice_service.yaml     语音引擎服务（独立进程，不是插件配置）
├── channels/
│   ├── napcat.yaml        QQ 通道（NapCat / OneBot）
│   ├── feishu.yaml        飞书通道
│   ├── wecom.yaml         企微通道（骨架，尚未实现）
│   └── routes.yaml        聊天路由表：哪个群/会话属于哪个项目
├── plugins/
│   └── voice.yaml         语音插件的调用端（只描述「请求发到哪」）
└── cron/
    └── jobs.json          定时任务清单
```

Windows 一把梭（在仓库根执行）：

```bat
if not exist "%USERPROFILE%\.omniagent\channels" mkdir "%USERPROFILE%\.omniagent\channels"
if not exist "%USERPROFILE%\.omniagent\plugins"  mkdir "%USERPROFILE%\.omniagent\plugins"
if not exist "%USERPROFILE%\.omniagent\cron"     mkdir "%USERPROFILE%\.omniagent\cron"
copy examples\*.json          %USERPROFILE%\.omniagent\
copy examples\config.yaml     %USERPROFILE%\.omniagent\
copy examples\voice_service.yaml %USERPROFILE%\.omniagent\
copy examples\channels\*.yaml %USERPROFILE%\.omniagent\channels\
copy examples\plugins\*.yaml  %USERPROFILE%\.omniagent\plugins\
copy examples\cron\jobs.json  %USERPROFILE%\.omniagent\cron\
```

*nix 系（macOS / Linux）：

```sh
mkdir -p ~/.omniagent/{channels,plugins,cron}
cp examples/{models.json,config.yaml,mcp.json,voice_service.yaml} ~/.omniagent/
cp examples/channels/*.yaml ~/.omniagent/channels/
cp examples/plugins/*.yaml  ~/.omniagent/plugins/
cp examples/cron/jobs.json  ~/.omniagent/cron/
```

**一份都不用建也能跑**：每个文件对每个键都有代码默认值，缺文件只是「没得改」而不是「起不来」。
真正必须自己填的只有 `models.json` 里的端点和 key——其余全是调优项。

## 谁读它、改完怎么生效

| 文件 | 拷到 | 谁来读 | 生效时机 | Web 入口 |
| --- | --- | --- | --- | --- |
| `models.json` | `~/.omniagent/models.json` | 大脑模型路由、网关派发、记忆链路取端点 | Web 保存即时；**手改要重启**（进程内缓存） | 设置 → 模型 |
| `config.yaml` | `~/.omniagent/config.yaml` | core 引擎旋钮 | 下一次运行生效（运行级快照，当前这轮不受影响） | 设置 → 通用 / 安全 / 模型 / 编排 / 伙伴（记忆开关） |
| `mcp.json` | `~/.omniagent/mcp.json` | 工具运行时（MCP server 启停） | Web 保存即时；**手改要重启** | 技能与工具 |
| `voice_service.yaml` | `~/.omniagent/voice_service.yaml` | 语音引擎服务（**独立进程**） | 监听/令牌/空闲秒数在**服务启动**时读，音色与路径在调用时读 | 设置 → 模型 → 语音引擎 |
| `channels/napcat.yaml`<br>`channels/feishu.yaml`<br>`channels/wecom.yaml` | `~/.omniagent/channels/<同名>.yaml` | 网关通道适配器 | 重启网关（适配器在启动时按文件构造） | 无，纯手改 |
| `channels/routes.yaml` | `~/.omniagent/channels/routes.yaml` | 网关聊天路由 | 每条消息重读，**改完即生效** | 无，纯手改 |
| `plugins/voice.yaml` | `~/.omniagent/plugins/voice.yaml` | 语音插件调用端 | 端点类键调用时读；`enabled` 开关重启生效 | 技能与工具（开关） |
| `cron/jobs.json` | `~/.omniagent/cron/jobs.json` | 调度器 | 每 tick 重读，**改文件即生效** | 定时任务 |

## 三条约定

**`models.json` / `mcp.json` / `cron/jobs.json` 容忍 `//` 注释**（整行与行尾都支持，字符串里的
`//` 原样保留，所以 `"base_url": "http://127.0.0.1:8080/v1"` 不会被切坏）。但 Web 面板保存时会
整体重写这三份文件，注释必然被抹掉——**所以注释的家在这里，不在运行文件里**。长期解释写在示例，
临时改值写在真身。

**示例里不含任何真实密钥与本机绝对路径**（`api_key`、`service_token` 一律留空，路径全走
`~/.omniagent/` 下的自动探测）。语音那两份例子由 `tests/test_voice_examples.py` 拿插件 schema
做双向校验：加了新键忘了写例子、例子写了 schema 没有的键、或把本机 key 抄进去，测试都会红。
其余例子目前靠改动时人工对照代码——注释里点名的键都能在源码里找到读它的地方。

**优先级**：`~/.omniagent/config.yaml` > 仓库根 `config.yaml`（已废弃，仍作底但会 WARN 一次）>
代码默认值。仓库根那份只保留「Web 无入口」的少量项，不要往它堆可调项。
