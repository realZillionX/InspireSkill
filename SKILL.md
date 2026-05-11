---
name: inspire
description: "Execution-first Inspire platform CLI usage manual, with on-demand references for platform workflows."
---

# Inspire Skill

`inspire` 是启智平台的本地命令入口。本文档只描述可观察、可执行的黑盒用法：命令是否存在、参数叫什么、默认值是什么，以 CLI help 为准；资源、项目、事件、日志和指标，以实时查询为准；不把内部接口、实现结构或历史行为当作使用事实。

同一份手册同时服务人类和模型。适合人类快速判断的表达，也应该适合模型稳定执行；不区分两套原则。

## 1. 平台使用模型

启智上的一次任务可以拆成四个黑盒层面：

| 层面 | 决策问题 | 主要入口 |
| --- | --- | --- |
| 调度条件 | 在哪个 workspace、project、compute group 上，用多少 GPU / CPU / 内存，跑哪个镜像 | `config context`、`resources specs`、`<workload> profile` |
| 远端文件 | 代码、数据、权重、产物放在哪个项目共享盘路径 | `init`、`notebook path`、`notebook exec --cwd`、`notebook scp` |
| 工作负载 | 交互调试、GPU job、CPU HPC、Ray、serving 选哪一个入口 | `notebook`、`job`、`hpc`、`ray`、`serving` |
| 观察与收尾 | 为什么排队 / 失败、是否真的在工作、日志在哪里、何时清理 | `events`、`logs`、`metrics`、`status`、`instances`、`stop`、`delete` |

`workspace`、`project`、`group`、`quota` 和 `image` 是调度条件。它们没有隐式默认值；必须在 create 命令里显式传入，或用 workload profile 显式填入。Path alias 只表示远端路径，不能替代调度条件。

日常主 workspace 基本只有两类：`CPU资源空间` 用于 CPU notebook、HPC、联网下载、依赖安装和镜像准备；`分布式训练空间` 用于 GPU notebook、GPU job、模型 serving 和训练调试。国产卡分区、`CI-情境智能` 工作空间或其它小组专属空间是特殊项目 / 特殊硬件路径，只有任务明确需要时才切换。

联网能力属于 workspace / compute group 的实际环境，而不是命令本身。`分布式训练空间` 常见为不可上网；拉 Git、取 Hugging Face 权重、访问外部数据源时，优先在 `CPU资源空间` 的可上网 CPU notebook 中完成，然后把结果留在 `me` / `public` 等 path alias 指向的共享路径，或保存成镜像，再去 `分布式训练空间` 创建 GPU notebook / job。SII 内部源和公网不是同一个能力：内部源覆盖 PIP、Apt、Conda、npm、Maven、Docker Harbor、OSS、DNS 和 NTP；即使 compute group 没有公网，也应先判断内部源是否可用，再决定是否回到 `CPU资源空间` 准备镜像或共享盘内容。

## 2. 执行闭环

日常任务按这个顺序推进：

1. 用 help 确认可用命令和参数。
2. 用 `inspire config context` 和 `inspire resources specs --usage <kind> --workspace CPU资源空间` 或 `--workspace 分布式训练空间` 确认名字和可用规格。
3. 如果 `分布式训练空间` 或目标 compute group 不可上网，外部下载走 `CPU资源空间`；Python / Linux 包、Conda、npm、Maven、Docker 镜像、OSS、DNS / NTP 这类平台内部可达资源先按 SII 内部源处理。
4. 用 notebook / job / hpc / ray / serving 的 create 命令提交，必要时先 `--dry-run`。
5. 用 events 看调度和启动原因，用 logs 看程序输出，用 metrics 看资源是否真的在工作，用 status / instances 看对象和实例状态。
6. 终态且不再需要的 notebook、job、HPC、Ray、serving 和临时镜像要清理；运行中的对象先 stop，再 delete。

## 3. CLI Help 是命令事实来源

先查 help，再执行命令：

```bash
inspire --help
inspire <command-group> --help
inspire <command-group> <subcommand> --help
```

在本仓库源码 checkout 内验证 CLI 行为时，用：

```bash
cd cli
uv run inspire --help
uv run inspire notebook --help
uv run inspire hpc create --help
```

`inspire --help` 给出当前版本真实命令组；`inspire <command-group> --help` 给出该组子命令和工作流说明；`inspire <command-group> <subcommand> --help` 给出参数、默认值、必填项和示例。

需要复用调度条件时，用对应 workload 的 `profile set/list/show/delete` 管理条件组；具体参数以 `inspire <workload> profile --help` 为准。`profile set` 保存 `workspace`、`project`、`group`、`quota`、`image` 五个条件；create 命令显式传 `--profile <name>`；batch 条目写 `profile = "<name>"`。

## 4. 按需加载索引

每次优先只读一份最相关手册；跨边界时再读第二份。日常手册不维护完整命令清单，命令表面始终回到 CLI help。

| 场景 | 手册 |
| --- | --- |
| 选择 `CPU资源空间` / `分布式训练空间`、compute group、`--quota`、存储池、path alias，或解释路径不可见 | [references/resources-and-paths.md](references/resources-and-paths.md) |
| 创建、连接、执行、传文件，或准备 notebook 基底环境 | [references/notebook.md](references/notebook.md) |
| 把 notebook 容器内 HTTP 服务暴露给浏览器、SDK 或小组成员 | [references/notebook-service-proxy.md](references/notebook-service-proxy.md) |
| 提交 GPU job、CPU HPC、Ray、serving，或观察事件、日志和指标 | [references/compute-workloads.md](references/compute-workloads.md) |
| 一个项目从环境准备、数据处理推进到训练 | [references/workflows.md](references/workflows.md) |
| 浏览、注册、保存、调整可见性或清理镜像 | [references/image-management.md](references/image-management.md) |
| 浏览或注册模型仓库条目，判断 model registry 和 serving 的关系 | [references/model.md](references/model.md) |
| 安装、更新、账号、项目初始化、代理 setup | [references/setup/install-and-config.md](references/setup/install-and-config.md)、[references/setup/proxy-setup.md](references/setup/proxy-setup.md) |

开发者手册只在维护 CLI 封装、排查平台接口合约，或用户明确要求看接口时读取：

| 场景 | 手册 |
| --- | --- |
| 对照平台接口 | [references/dev/openapi.md](references/dev/openapi.md)、[references/dev/browser-api.md](references/dev/browser-api.md) |

## 5. 项目上下文

仓库根可用 `INSPIRE.md` 记录非配置性上下文，建议包含：

- `Default Image`
- `Path Conventions`
- `Public Directory Layout`
- `Existing Notebooks`
- `Ongoing Jobs`

不要把账号配置、密码、代理密钥或 `.inspire/config.toml` 内容复制进 `INSPIRE.md`。配置由 CLI 合并和展示。
