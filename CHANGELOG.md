# Changelog

## Unreleased

### 变更

- 新增按账号隔离、可定时刷新且支持手动管理的本地 Name 解析索引；`inspire cache
  status|refresh|clear` 用于查看、刷新或清理该加速层，平台 Live API 仍是资源事实源。
- CLI 的公共输入、Help、错误、人类输出和 JSON 输出保持严格 Name-only；平台 Handle
  只存在于内部解析器、Browser API 请求和本地调试实现中。
- 发现类列表默认限制为 20 项，Batch 结果和 Job 日志使用明确的输出预算；使用
  `--limit/-n` / `--all` 主动调整集合输出。
- 同类 workload 命令统一使用相同的 Name、Workspace、分页、确认、截断和 JSON 语义。

## v6.2.0

### 新增

- GPU Job 支持平台原生状态通知，可通过 CLI、账号配置或 Batch item 配置开关。
- 支持 Qoder Work 和 Kimi Desktop Harness。

### 变更

- 代理、登录诊断和 JSON 输出统一脱敏，不输出凭据、内部路径、请求包装或平台 Handle。
- Notebook、GPU Job、HPC、Ray、Serving、Image、Model、Project、Resources 和 User
  命令统一采用短、可读、可脚本消费的输出。

### 修复

- 修复通用 Shell proxy 与 `NO_PROXY` 的继承边界。
- 修复受限 Notebook 的内部镜像访问不应继承容器代理的问题。
- 修复 Job 通知、容错默认值和项目优先级在分层配置与 Batch 路径中的传递。

## 历史版本

请参阅 [GitHub Releases](https://github.com/realZillionX/InspireSkill/releases)。
