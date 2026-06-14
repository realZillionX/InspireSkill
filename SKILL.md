---
name: inspire
description: "Inspire platform operating model for Agents: decide workspace, resources, paths, workload type, observation, cleanup, and load focused references; use CLI help for command syntax."
---

# Inspire Skill

`inspire` 是启智平台的本地命令入口。这个 Skill 不复述 CLI 使用手册；命令组、子命令、参数、默认值和示例始终以 `inspire --help` / `inspire <group> --help` / `inspire <group> <subcommand> --help` 为准。

本文件只保留超出 `--help` 的平台判断：任务应该落在哪个 workspace，资源条件和远端路径如何分离，公网和 SII 内部源怎么取舍，Notebook / Job / HPC / Ray / Serving 如何选型，以及观察、止损、清理的闭环。面向用户解释和 Agent 自己执行使用同一套原则：把用户目标翻译成可验证的平台决策，不制造一套“给人看的说法”和一套“给 Agent 跑的做法”。

## 1. 先建模，再查命令

每个任务先拆成四个平面，再去查具体 help：

| 平面 | 要回答的问题 | 不要混淆 |
| --- | --- | --- |
| 调度条件 | 跑在哪个 workspace / project / compute group，用多少 GPU / CPU / 内存 / shared memory，基于哪个镜像 | 不是远端路径，也不是对象名字 |
| 远端文件 | 代码、数据、权重、checkpoint、产物放在哪个共享盘路径 | path alias 不能代替 workspace / project / group / quota |
| 工作负载 | 需要交互调试、GPU 后台任务、CPU Slurm、Ray 弹性集群，还是模型服务 | 不要为了“能跑”把所有事都塞进 notebook |
| 观察收尾 | 如何判断排队、失败、空转、已完成，以及何时 stop / delete | `status=RUNNING` 不等于业务健康，`status=SUCCEEDED` 不等于产物完整 |

`workspace`、`project`、`group`、`quota`、`image` 是核心调度条件，没有隐式默认值。创建 workload 时显式传入，或用 workload profile 明确保存这五类条件。GPU job shared memory 属于资源细项，不等同于 `quota` 的内存字段，且不能超过 `quota` 的实例内存；需要时查 job create help 或 [references/compute-workloads.md](references/compute-workloads.md)。Path alias 只表示远端路径，服务于 `--cwd`、scp、日志路径和共享盘约定。

日常 workspace 选择通常很直接：

- `CPU资源空间`：CPU notebook、联网准备、依赖安装、HPC 数据处理、CPU Ray。
- `分布式训练空间`：GPU notebook、GPU job、多节点训练、serving、GPU 指标观察。
- 国产卡分区、`CI-情境智能` 工作空间或小组专属空间：只有任务明确要求特殊硬件、特殊权限或特殊项目环境时才切换。

## 2. 网络、内部源和镜像

联网能力属于 workspace / compute group 的实际环境，不属于命令本身。先判断要访问的是公网还是 SII 内部源：

- 公网：GitHub、Hugging Face、外部数据源、公开下载地址。目标 GPU 空间不可上网时，先在 `CPU资源空间` 的可上网 CPU notebook 准备内容，再放到共享盘或保存镜像。
- SII 内部源：PyPI / pip、Apt、Conda、PyTorch wheels、npm、Maven、Docker registry、OSS、NTP 等内部地址。即使没有公网，目标 notebook / job 所在环境也可能能访问内部源。

需要内部源地址、快速配置或发行版源选择时，加载 [references/network-and-sources.md](references/network-and-sources.md)。不要在 SKILL.md 或对话里凭记忆复写内部源清单。

依赖安装跑通后，优先把运行环境固化为镜像；后续 notebook / job / HPC / Ray / serving 复用镜像，而不是每次启动都重新联网安装。

## 3. 执行闭环

日常执行顺序：

1. 根据用户目标选择最相关 reference，只读需要的上下文。
2. 用 CLI help 确认当前版本的真实命令表面。
3. 用 live 查询确认账号、workspace、project、compute group、quota、镜像和资源可用性；不要把本地缓存、旧截图或历史记忆当事实。
4. 准备远端文件和环境。公网准备、内部源配置、镜像保存分别按实际环境选择位置。
5. 提交 workload。复杂调度条件先 dry-run 或先用小规模 notebook 验证。
6. 观察 events / logs / metrics / instances / status。先看调度事件，再看程序日志，再看资源曲线是否真的在工作。
7. 终态且不再需要的 notebook、job、HPC、Ray、serving 和临时镜像要清理；运行中的对象先 stop，再 delete。

Name-only 边界始终有效：普通输入输出使用名称、alias、可读状态和短表格，不让用户理解或传递平台 handle。需要平台 handle 只走专门 `id` 命令或内部 resolver。

Notebook 连接类命令可跨账号解析已有连接；`--account <name>` 指本地 account alias，SSH tunnel rebuild 应使用目标 notebook 所属账号的 session 和配置。

## 4. 按需加载索引

先按问题类型选一份 reference；跨边界时再读第二份。不要把 reference 当命令大全；命令语法回到 CLI help。

| 用户问题或 Agent 判断点 | 先加载 |
| --- | --- |
| 安装、更新、账号添加 / 切换、首次初始化、`inspire init` 全局发现与项目初始化、SII proxy setup | [references/setup/install-and-config.md](references/setup/install-and-config.md) |
| 选择 `CPU资源空间` / `分布式训练空间` / 特殊 workspace，理解 compute group、`--quota gpu,cpu,mem`、资源实时查询和 workload profile | [references/resources-and-paths.md](references/resources-and-paths.md) |
| 需要区分公网、SII 内部源、离线 GPU 空间，或查 PyPI / pip、Apt、Conda、PyTorch wheels、npm、Maven、Docker registry、OSS、NTP 等内部源地址 | [references/network-and-sources.md](references/network-and-sources.md) |
| 理解共享盘作用域、存储池、挂载隔离、path alias、远端路径不可见和仓库 `INSPIRE.md` 维护边界 | [references/paths.md](references/paths.md) |
| 创建交互环境、连接 notebook、SSH / exec / shell / scp、IDE URL、容器 HTTP 服务暴露、基底环境准备、大文件流转 | [references/notebook.md](references/notebook.md) |
| 在 GPU job、CPU HPC、Ray、serving 之间选型，或提交后观察 events / logs / metrics / instances / status，分析排队、失败、空转、优先级和异常状态 | [references/compute-workloads.md](references/compute-workloads.md) |
| 一个项目跨越 CPU 准备、数据处理、GPU 训练、部署或交付，需要阶段化计划 | [references/workflows.md](references/workflows.md) |
| 选择已有镜像、从 notebook 保存镜像、注册外部镜像、调整可见性、清理临时镜像 | [references/image-management.md](references/image-management.md) |
| 浏览 / 注册模型仓库条目，判断 model registry、model version 和 serving 的关系 | [references/model.md](references/model.md) |
| 维护 CLI Browser API 封装、排查前端接口合同、reverse-capture 平台请求，或用户明确要求看接口 | [references/dev/browser-api.md](references/dev/browser-api.md) |

## 5. 项目上下文

每个启智项目仓库都必须维护仓库根 `INSPIRE.md`。这是开发原则，不是建议项：启智相关上下文要和 `AGENTS.md` / `CLAUDE.md` 等本地 Agent 计划文件分开，避免 harness 私有计划、平台配置和项目协作约定互相污染。

`INSPIRE.md` 只记录非配置性上下文，例如：

- `Default Image`
- `Path Conventions`
- `Public Directory Layout`
- `Existing Notebooks`
- `Ongoing Jobs`

不要把账号配置、密码、代理密钥、平台 session、`.inspire/config.toml` 或 `.inspire/accounts/<account>/config.toml` 内容复制进 `INSPIRE.md`。配置事实由 CLI 合并和展示；项目说明只记录人类可读的协作约定。
