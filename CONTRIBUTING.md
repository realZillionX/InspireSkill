# Contributing

感谢您改进 InspireSkill。这个仓库的命令和手册要适合 Agent 阅读与执行，因此每个变更都要尽量保持命令行为可复现、输出语义清晰、文档与 CLI help 一致。

## 项目定位

InspireSkill 的核心价值是让本地 CLI、Agent 手册和启智平台真实行为保持零漂移。它不是一份静态命令说明，也不是平台接口的薄包装；它要把平台调度、资源、镜像、路径、日志、事件和指标转化成可复现、可执行、适合 Agent 使用的工作流。

同一份文档只服务 Agent。适合快速判断的表达，也应该适合稳定执行。

## 开发环境

CLI 工程在 `cli/` 目录内，推荐使用 `uv`：

```bash
cd cli
uv sync --dev
uv run inspire --help
```

安装提交前检查：

```bash
uv run pre-commit install --config ../.pre-commit-config.yaml
```

如需手动运行全部已配置检查：

```bash
uv run pre-commit run --config ../.pre-commit-config.yaml --all-files
```

## 事实来源

命令表面以 CLI help 为准。新增、删除或修改命令时，`inspire --help`、`inspire <command-group> --help` 和 `inspire <command-group> <subcommand> --help` 必须反映真实 Agent 入口；日常文档引用 CLI Help，不复制完整命令表。

平台事实以 live 查询为准。`list`、`status`、`events`、`metrics`、资源规格、资源可用量、项目和账号视图都从当前平台读取。缓存只用于 Web session / auth、Name 解析、连接复用、日志传输等性能场景，不作为 Agent 可见事实来源。

Browser API 文档只收录已经闭合的合同。请求体、响应形状、Referer、权限边界和 destructive 语义需要能由当前代码、测试、live smoke、DevTools 或 SPA bundle 复现。

## Agent 合同

普通 CLI 输入输出坚持 Name-only。Agent 应该使用名称、alias、可读状态和短表格理解对象；平台 handle 只能停留在 resolver 和 API payload 中，不能成为任何 CLI 命令、错误信息、JSON 或本地 debug report 的输入输出。

默认文本输出面向 Agent，要求短、清楚、能操作。脚本接口使用 `--json`，但 `--json` 也不应默认泄露低价值平台 handle。错误、hint 和歧义列表不要把 raw ID 当作解决方案暴露给 Agent。

集合和日志输出必须有显式预算。发现类集合和 Batch 结果默认最多展示 20 项，统一提供 `--limit/-n` 和 `--all` 作为明确的上下文开销选择；Job 日志默认最多 100 行 / 条目和 16,000 个字符。截断元数据只保留 `shown`、`total`、`truncated` 等能帮助 Agent 决策的字段。

中文输出和文档要照顾宽度、标点和中英混排。表格需要中文宽度 aware；中文与 English / 数字 / 命令名相邻时保留半角空格；中文标点使用全角。

## 配置和默认值

不要把调度条件做成隐式默认值。`workspace`、`project`、`group`、`quota` 和 `image` 是创建 workload 的调度条件，只能来自显式参数、workload profile 或 batch 条目里的 `profile`。Profile 是调度条件组 alias，不是路径 alias。

Path alias 只表示远端路径。`me`、`public`、`global-me` 和存储池前缀 alias 用于 `--cwd`、`scp`、日志路径和共享盘约定，不能替代 `workspace`、`project`、`group`、`quota` 或 `image`。

账号、代理和本地环境通过账号配置呈现，不通过一次性环境变量前缀污染 live 命令示例。需要临时远端目录时用命令参数，例如 `--cwd /tmp`；需要持久语义时用 `inspire init`、`.inspire/config.toml` 和 `[path_aliases]`。

## 平台工作流

公网和 SII 内部源分开判断。公网下载、外部 Git、Hugging Face 权重和外部数据源通常放在 `CPU资源空间` 的可上网 notebook；PIP、Apt、Conda、npm、Maven、Docker 镜像仓库、OSS 和 NTP 等内部源优先在目标 notebook 中按实际可达性配置，`分布式训练空间` 等 GPU 空间也可以直接跑通依赖。

运行环境跑通后要保存成镜像。保存是 notebook 的生命周期事件，不是镜像目录操作：`notebook save-image` 会触发一段中等时长的镜像保存过程，过程中不可操作该 notebook；保存完毕后 notebook 不会被自动停止，仍可继续连接和使用。保存出的镜像才是后续 notebook / job / HPC / Ray / serving 应复用的稳定环境。

观察优先用平台事件、指标和实例视图。失败先看 `events`，程序输出看 `logs`，资源是否真的工作看 `metrics`，实际运行单元看 `instances`。需要进入容器看瞬时状态时用 `exec` 或 `shell`，不要为一个观察问题保留旁路命令。

## 文档边界

文档只描述当前可执行的命令、平台语义和工作流。命令语法回到 CLI Help，版本变化留在 release notes。

使用手册和开发者参考分开。`SKILL.md` 和 `references/` 的日常手册只讲黑盒用法、平台语义和工作流；`references/dev/` 只在维护 CLI 封装、排查接口合同或 Agent 明确要求看接口时加载。

上下文要节制。内部源地址等必要事实集中在专门 reference 里；Agent 文档只保留已经整理过的结论、可执行步骤、复现证据和未解决 TODO。

`INSPIRE.md` 不是仓库级必备元数据，只属于某个具体使用启智平台的科研或工程项目工作区，用于维护稳定平台拓扑、Canonical Remote Paths、永久基础设施和资产生命周期。InspireSkill 本身是 CLI、Skill 与文档的通用工具源仓库，不对应业务项目资产，因此根目录不应存在 `INSPIRE.md`。通用文档只说明该合同的边界，不复制任何具体项目的路径、资产 ID 或运行状态。

## 代码维护

优先沿用仓库已有模式。新增抽象必须降低真实复杂度、减少实际重复或匹配现有边界；不要为了局部方便引入新的风格、新依赖或平行体系。

保持变更范围小而完整。改命令行为时同步更新 resolver、formatter、help、tests 和文档；改平台 API wrapper 时同步更新测试和开发参考。不要做无关格式化或跨模块清理。

对 destructive 操作保持证据闭环。创建、停止、删除、保存、发布等操作必须有受控 live smoke、前端 bundle payload 或等价证据支撑；live smoke 创建的对象必须清理，并在结果里说明残留风险。

## 提交前检查

常规变更至少运行：

```bash
cd cli
uv run pytest -q
uv run ruff check inspire tests
uv run mypy
uv build
```

窄文档变更至少做搜索验证和 `git diff --check`；命令 help 变更跑对应 help / formatter 测试；CLI 行为变更跑目标测试、Ruff、mypy 和必要的集成 smoke；共享行为或 release 前跑全量。

当前 CI 会运行单元测试、Ruff、mypy 和构建验证。不要在无关 PR 中引入全仓格式化；如果要扩大 lint 或 typing 覆盖，请单独提交并说明迁移范围。

## 交付要求

CI 是交付信号。推送后看 GitHub Actions 的红叉 / 绿勾；失败就读日志、修原因、继续迭代，直到 required checks 通过，或剩余问题明确属于外部平台 / 权限 / 资源状态并已说明。

交付要端到端。正常维护任务不止改代码：需要同步测试、文档、生成资产、版本号、release notes、部署或 PR 状态时一并处理。工作区保持干净，不留下 scratch 文件、日志、缓存、备份副本或半成品分支。

## Pull Request

PR 描述应包含：

- 变更目的和影响范围。
- 已运行的验证命令及结果。
- 是否涉及 live Inspire 平台资源；如果涉及，说明创建对象、清理状态和残留风险。

普通贡献可以走小而清楚的 PR；大范围语义调整先用 Issue 收敛问题场景和证据。维护者处理明确任务时，可以直接完成本地编辑、验证、提交、推送、PR、release 或部署，但如果任务要求先审阅草案，就不要提前提交或推送。

版本发布按真实范围写 release notes，不能只复述 squash commit 标题。Breaking release 要覆盖 CLI 行为、配置边界、Web API、文档和测试的实际影响。

复杂任务可以拆给多个 Agent 并行，但每个 Agent 要有清晰的问题边界或文件所有权，避免重复调查和写入冲突。主线程负责整合结果、验证行为和清理工作区。
