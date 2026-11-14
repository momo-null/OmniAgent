# 插件配置单一事实源（2026-11-14）

状态：已实施，待验收  
分支：`fix/plugin-config-single-source`

## 问题

插件/通道搬迁完成后，运行时仍保留旧 `~/.omniagent/channels/<name>.yaml` 回落，并有四处配置读取：

- `harness/events/discover.py` 读 `plugins/<name>.yaml`，但 `configured` 仍把旧 `channels/` 算进来；
- `harness/channel_gateway.py::channel_config` 新旧双读，并用 `discover` 的 `enabled` 覆盖旧文件值；
- `omni_core/tools/loader.py::plugin_config` 只读新路径；
- `plugins/bilibili/plugin.py::_plugin_cfg` 又自己读一遍，且回落旧 `channels/`。

最严重的行为差异：旧 `channels/<name>.yaml` 写 `enabled:false` 时，`discover._enabled()` 只读新文件 → 取 manifest 的 `true` → `channel_config` 再覆盖 → 通道被静默启用。`examples/channels` 模板默认正是 `enabled:false`，按旧文档操作会触发。

## 决策

1. 运行时唯一事实源：`~/.omniagent/plugins/<name>.yaml`；旧 `channels/<name>.yaml` 只由离线迁移脚本处理。
2. `scripts/migrate_plugin_configs.py` 保留旧 `enabled`（旧文件明确关闭的通道迁移后仍关闭）；新路径同名字段优先。
3. `channels/routes.yaml` 继续保留，它是全局聊天路由表，不是插件配置。
4. 通道模板迁到 `examples/plugins/<name>.yaml`；`examples/channels/` 只留 `routes.yaml`。

## 改动

- `harness/events/discover.py`：`configured` 只认 `plugins/<name>.yaml`；配置解析复用 `loader.plugin_config`。
- `harness/channel_gateway.py`：`channel_config` 只读插件配置，`enabled` 仍由 `discover` 合并；启动时对残留旧文件给出迁移提示。
- `scripts/migrate_plugin_configs.py`：移除 `DROP_KEYS={"enabled"}`，旧值保留、新值优先。
- `plugins/bilibili/plugin.py`：改用 `loader.plugin_config`，删除私有旧路径回落。
- `plugins/host/plugin.py`、`plugins/emulator/plugin.py`：删除 `environments/` 回落。
- `plugins/bilibili/channel.py`、`plugins/xiaohongshu/channel.py`：CLI 改用 `loader.plugin_config`，不再 `from ..channel_gateway`。
- 模板：五个通道模板从 `examples/channels/` 迁到 `examples/plugins/`；更新头注释、`examples/README`、根 README 中英文、`examples/config.yaml`、`harness/outbound.py`、后端报错与 Web 入口注释。
- 守卫：`tests/test_plugin_events_guards.py` 增加相对导入检测、运行时旧路径读取检测；`tests/test_channel_examples.py` 校验模板存在且不含真实密钥/本机路径。

## 兼容与回退

- 已有旧配置的用户先运行 `python scripts/migrate_plugin_configs.py`；脚本保留 `enabled`，备份目标文件，并把旧文件重命名为 `.bak-<日期>`。
- 运行时不再回落旧路径，也不扫描/提示旧 `channels/*.yaml`；迁移由脚本一次性离线完成。
- 回退方式：恢复对应 `.bak-<日期>` 文件后回到旧版本运行；或从分支提交回退。

## 验收

- `pytest -q` 全量通过；
- `python scripts/review_lint.py --strict` 通过；
- 隔离配置验证：旧路径单独存在时 `channel_config("napcat") == {}`、`configured is False`；新路径 `enabled:false` 时 `channel_config` 返回 `enabled: False`；
- 迁移脚本验证：旧 `enabled:false` 保留；新路径已有 `enabled:true` 时新值优先。
