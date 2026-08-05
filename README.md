<p align="center">
  <img src="https://raw.githubusercontent.com/realZillionX/InspireSkill/main/assets/hero.svg" width="100%" alt="InspireSkill — Agent-native CLI for the Inspire compute platform"/>
</p>

<p align="center">
  <b>让本地 AI Agent 通过精简、可复现的 CLI 操作启智平台。</b>
</p>

# InspireSkill

InspireSkill 由两部分组成：

- `inspire`：管理启智账号、资源、Notebook、Job、HPC、Ray、Serving、Image 和 Model Registry 的本地 CLI。
- `SKILL.md` + `references/`：供 Agent 按需加载的平台操作模型与长期使用手册。

Agent 留在本机，代码仓库、Git 状态和其它开发工具继续作为完整上下文；启智平台只承担资源调度、远端执行和产物存储。

## 安装

支持 macOS、Linux 和 WSL2。需要 `bash`、`curl`、`tar`、Python 3.10+，以及 `uv` 或 `pipx` 任一。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

安装器会安装 `inspire-skill`，并把 Skill 文档写入已选择的 Agent Harness。完整参数与目录约定见 [`references/setup/install-and-config.md`](references/setup/install-and-config.md)。

首次配置：

```bash
inspire account add <account-name>
inspire config check
inspire init

cd /path/to/your/repository
inspire init --scope project
inspire resources availability --workspace all --include-cpu
```

更新：

```bash
inspire update
inspire update --check
inspire update --cli-only
inspire update --skill-only
```

## 稳定 CLI 合同

- **Name-only**：资源参数、帮助、错误、人类输出和 JSON 输出都使用 Name、Account Alias、Path Alias 与可读状态。同名候选通过 Workspace、过滤条件和 `--pick` 消歧。
- **显式调度条件**：创建 Workload 时显式指定 `workspace`、`project`、`group`、`quota` 和 `image`，或显式引用包含这些字段的 Workload Profile。
- **Live 数据优先**：Workspace、Project、Compute Group、Quota、Image、状态和资源余量以当前平台查询为准。本地名称索引只加速解析，可用 `inspire cache status|refresh|clear` 管理。
- **输出有预算**：列表和 Batch 默认只展示有限结果；同类命令统一使用 `--limit/-n` 收窄、`--all` 展开。结构化输出使用根级 `inspire --json ...`。
- **诊断不污染结果**：默认输出只保留 Agent 决策所需字段；工程诊断写入 `--debug` 日志，不混入普通输出或 JSON。
- **清理有确认**：运行中的对象先 `stop`，再 `delete`；破坏性命令按 Help 要求确认，自动化时显式传 `--yes`。

命令、参数、默认值和示例始终以当前安装版本的 Help 为准：

```bash
inspire --help
inspire <group> --help
inspire <group> <command> --help
```

## 命令索引

下表与当前 Click 命令树一致；括号中列出嵌套子命令。

| 命令组 | 当前公开命令 | 用途 |
| --- | --- | --- |
| `account` | `add`, `current`, `list`, `remove`, `rename`, `use` | 管理本地 Account Alias 和活动账号 |
| `cache` | `clear`, `refresh`, `status` | 管理可刷新的本地名称解析索引 |
| `config` | `check`, `context`, `env`, `show` | 校验、查看和生成配置；JSON 使用根级 `--json` |
| `config env` | `use` | 生成 Dotenv 模板，或登记仓库级 Dotenv 文件 |
| `init` | `init` | 发现账号级 Catalog、项目上下文和 Path Alias |
| `notebook` | `batch`, `connection`, `create`, `delete`, `events`, `exec`, `install-deps`, `lifecycle`, `list`, `metrics`, `net-test`, `path`, `profile`, `proxy-url`, `quota`, `scp`, `shell`, `ssh`, `ssh-config`, `ssh-proxy`, `start`, `status`, `stop`, `url`, `vscode` | Notebook 生命周期、连接、执行、传输和观测 |
| `notebook connection` | `forget`, `list`, `prune`, `refresh`, `status`, `target` | 管理联网 Notebook 的本地连接和跨账号目标选择 |
| `notebook connection target` | `forget`, `list` | 管理跨账号 Remembered Target |
| `notebook path` | `delete`, `list`, `set`, `show` | 管理项目级远端 Path Alias |
| `notebook profile` | `delete`, `list`, `set`, `show` | 管理 Notebook Workload Profile |
| `job` | `batch`, `command`, `create`, `delete`, `events`, `instances`, `list`, `logs`, `metrics`, `profile`, `quota`, `shell`, `status`, `stop`, `wait` | 固定规模 GPU 后台任务与分布式训练 |
| `job profile` | `delete`, `list`, `set`, `show` | 管理 Job Workload Profile |
| `hpc` | `batch`, `create`, `delete`, `events`, `instances`, `list`, `metrics`, `profile`, `quota`, `status`, `stop` | CPU Slurm / HPC 批处理 |
| `hpc profile` | `delete`, `list`, `set`, `show` | 管理 HPC Workload Profile |
| `ray` | `batch`, `create`, `delete`, `events`, `instances`, `list`, `metrics`, `profile`, `quota`, `status`, `stop` | Head + 弹性 Worker Group 的 Ray Workload |
| `ray profile` | `delete`, `list`, `set`, `show` | 管理 Ray Workload Profile |
| `serving` | `batch`, `configs`, `create`, `delete`, `events`, `instances`, `list`, `metrics`, `profile`, `quota`, `start`, `status`, `stop` | 自定义模型服务的配置、生命周期和观测 |
| `serving profile` | `delete`, `list`, `set`, `show` | 管理 Serving Workload Profile |
| `image` | `delete`, `detail`, `list`, `register`, `save`, `set-visibility` | 选择、保存、注册、共享和清理镜像 |
| `model` | `list`, `register`, `status`, `versions` | 浏览或注册平台可见模型目录，并检查版本 |
| `project` | `detail`, `list`, `owners` | 查看项目元数据、负责人、预算和优先级 |
| `resources` | `availability`, `nodes` | 查询实时 Compute Group 余量和整节点空闲 |
| `user` | `api-keys`, `permissions`, `quota`, `ssh-keys`, `whoami` | 查看当前用户与管理 Notebook SSH 公钥 |
| `user ssh-keys` | `add`, `delete`, `list` | 按名称管理平台用户中心的 SSH 公钥 |
| `update` | `update` | 检查或安装 CLI 与 Skill 更新 |

`quota` 和 `profile` 只存在于支持创建 Workload 的命令组；`batch` 当前覆盖 Notebook、Job、HPC、Ray 和 Serving。只有 Job 公开聚合日志命令；其它 Workload 的公共观测入口以各自 Help 中的 `status`、`events`、`instances` 和 `metrics` 为准。

## 最短工作流

```bash
# 1. 发现可用名称
inspire config context

# 2. 查询合法规格与实时余量
inspire job quota --workspace 分布式训练空间
inspire resources availability --workspace 分布式训练空间

# 3. 创建并观察
inspire job create --name train-a --workspace 分布式训练空间 \
  --project CI-情境智能 --group H200-2号机房 \
  --quota 4,80,800 --image train-base:v1 \
  --command "bash train.sh"
inspire job events train-a --workspace 分布式训练空间
inspire job logs train-a --workspace 分布式训练空间 --follow
inspire job metrics train-a --workspace 分布式训练空间
```

Notebook、HPC、Ray 和 Serving 的选择边界、提交前检查与收尾方式见对应 Reference；不要把示例中的 Workspace、Project、Group、Quota 或 Image 当作默认值。

## 文档索引

- [`SKILL.md`](SKILL.md) — Agent 的平台操作模型、风险边界和按需加载入口。
- [`references/setup/install-and-config.md`](references/setup/install-and-config.md) — 安装、更新、账号、配置和项目初始化。
- [`references/setup/sii-proxy.md`](references/setup/sii-proxy.md) — Clash Verge 的 SII Proxy / `DIRECT` 分流。
- [`references/resources-and-paths.md`](references/resources-and-paths.md) — Workspace、Compute Group、Quota、实时资源、名称缓存和 Workload Profile。
- [`references/paths.md`](references/paths.md) — 共享盘作用域、存储池和 Path Alias。
- [`references/network-and-sources.md`](references/network-and-sources.md) — 公网、SII 内部源、受限环境和镜像固化。
- [`references/notebook.md`](references/notebook.md) — Notebook 创建、连接、执行、传输、Proxy 和清理。
- [`references/compute-workloads.md`](references/compute-workloads.md) — Job、HPC、Ray 与 Serving 的选型和观察闭环。
- [`references/image-management.md`](references/image-management.md) — Image 保存、注册、可见性和清理。
- [`references/model.md`](references/model.md) — Model Registry、版本与 Serving 的边界。
- [`references/workflows.md`](references/workflows.md) — CPU 准备、数据处理、GPU 训练和部署流程。
- [`references/project-assets.md`](references/project-assets.md) — 具体项目的持久 `INSPIRE.md` 资产合同。
- [`references/dev/browser-api.md`](references/dev/browser-api.md) — 仅供 CLI 维护者使用的 Browser API 实现地图。
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — 开发、测试和贡献约定。

## License

[MIT](LICENSE)
