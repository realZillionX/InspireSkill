---
name: inspire
description: "Use for Inspire/启智平台 (qz.sii.edu.cn) through the inspire CLI: install/update/uninstall, accounts, local SII proxy setup, INSPIRE.md project onboarding and upkeep, workspace/project/resource/path selection, Notebook, GPU Job, HPC, Ray, Serving, TensorBoard, Image, Model Registry, observation, cleanup, and Inspire CLI Browser API maintenance. Use CLI Help for syntax and load only the focused reference."
---

# Inspire Skill

`inspire` 是启智平台的本地 CLI。命令组、子命令、参数、默认值和示例以 `inspire --help`、`inspire <group> --help` 和 `inspire <group> <subcommand> --help` 为准；本文件只保留平台判断、项目上下文约定、最短执行闭环和 Reference 路由。

## 核心操作模型

执行前先把任务拆成四个平面：

| 平面 | 绑定内容 |
| --- | --- |
| 调度条件 | `workspace`、`project`、`group`、`quota`、GPU / CPU / 内存 / Shared Memory 和 `image`，决定任务在哪里、以什么规格运行。 |
| 远端文件 | 代码、数据、权重、Checkpoint 和产物的共享盘路径；Path Alias 只描述文件在哪里。数据广场的官方数据集是另一条来源，由创建时的 `--dataset` 只读挂载，不归 Path Alias 管。 |
| 工作负载 | 交互调试用 Notebook；固定 GPU 后台任务用 Job；CPU Slurm 批处理用 HPC；弹性 Worker、常驻或流式任务用 Ray；模型 HTTP 服务用 Serving。 |
| 观察收尾 | Events 看调度，Logs 看程序（Job / HPC / Ray / Serving 都有，Notebook 用 `exec` / `shell` 读），Metrics / Instances 看实际工作单元，Status 看平台状态和落点；读完还要进实例里看的时候 `shell` 在 Job / HPC / Ray / Serving 下都有，默认进哪个实例按 Workload 定；训练曲线本身用 TensorBoard（`inspire tensorboard`）读。最后核验业务健康和产物，再清理资源。 |

创建 Workload 时显式绑定 `workspace`、`project`、`group`、`quota` 和 `image`，或引用保存这五项的 Workload Profile；这些调度字段没有隐式默认值。选择资源时从同一条 Live Quota Row 复制完整 `group` 和 `quota`。GPU Job Shared Memory 是实例级资源，不能超过所选 Quota 的实例内存；细节见 [`references/compute-workloads.md`](references/compute-workloads.md)。Workload Profile 保存调度条件，Path Alias 保存远端路径，两者不能互相替代。

Live 查询是账号、Workspace、Project、Compute Group、Quota、Image 和资源可用性的事实源；本地缓存只是加速层，可用 `inspire cache status|refresh|clear` 管理，三条命令都接受 `--resource <kind>` 只针对一类，不能当作资源事实；`refresh` 不接受裸形式，必须用 `--resource` / `--workspace` / `--name` 说明刷哪一块，而且正常情况下不需要跑它。CLI 对 Agent 的稳定资源身份只有 Name 和 Alias；同名对象用 Workspace、可读候选和 `--pick` 消歧。

发现类列表和 Batch 结果默认最多展示 20 项，用命令自身的 `--limit/-n` 收窄或 `--all` 显式展开；Job 日志默认有行数和字符预算，截断时会给出已展示数量和继续获取完整结果的选项。需要结构化输出时使用根级 `inspire --json ...`，输出始终是单一 JSON 文档。

Workspace 判断：

- `CPU资源空间` 和 `分布式训练空间` 是所有用户默认可用的公共 Workspace：前者承担 CPU Notebook、联网准备、依赖安装、HPC 数据处理和 CPU Ray；后者承担 GPU Notebook、GPU Job、多节点训练、Serving 和 GPU 观察。
- 其余 Workspace（项目专属空间、国产卡分区等）必须由用户亲自指认，并记录到项目上下文；不要因为列表可见就自行启用。
- `分布式训练空间` 通常没有公网：公网内容先在 `CPU资源空间` 准备，写入共享盘或固化成镜像，再供 GPU Workload 使用；判断细节见 [`references/internal-sources.md`](references/internal-sources.md)。

## 项目上下文

进入一个具体科研或工程项目的工作区时，先核对项目上下文：`INSPIRE.md` 和 `./.inspire/` 项目配置。缺失或过期时不要直接开工，先主动向用户问清四件事——本仓库归属的 Project、需要指认的专属 Workspace、Paths（默认存储池与规范远端路径）、Image（项目基底镜像）——再执行 `inspire init --scope project`，并在符合条件时创建 `INSPIRE.md`。归属判断只能由用户确认，不从列表猜测。

`INSPIRE.md` 是具体项目工作区的资产合同，让不同 Agent、成员和会话共享稳定平台拓扑、Canonical Remote Paths、永久基础设施和资产身份；它不是 InspireSkill 安装产物，CLI、Skill 或通用工具源码仓库不创建。账号凭据、实时状态、日志和短期计划不进入该文件。

项目信息需要持续维护：保存新基底镜像、变更规范路径、指认新 Workspace、新增永久基础设施后，当场同步 `INSPIRE.md` 和项目配置；发现记录与 Live 查询漂移时以 Live 为准并修正。完整问询清单、初始化步骤和维护触发点见 [`references/project-context.md`](references/project-context.md)。

## 最短执行闭环

1. 项目工作区先核对项目上下文；缺失时按 [`references/project-context.md`](references/project-context.md) 问清并初始化。
2. 根据用户目标加载一份最匹配的 Reference；跨边界时再加载第二份。
3. 用 CLI Help 确认当前版本的真实命令表面。
4. 用 Live 查询确认账号、Workspace、Project、Compute Group、Quota、Image 和资源可用性。
5. 准备共享路径、代码和环境；复杂条件先 `dry-run` 或运行短 Probe。
6. 提交匹配的 Workload。
7. 按当前 Workload 的 Help 依次观察可用的 Events、Logs、Metrics、Instances 和 Status。
8. 核验业务健康与产物完整性；本次产生的持久资产写回 `INSPIRE.md`。
9. 运行中的对象先 `stop`，再 `delete`；终态且不再需要的对象直接 `delete`，仍有当前或声明的未来消费者时保留。

## 按需加载索引

命令语法和参数始终回到 CLI Help。每次先加载最匹配的一份 Reference：

### 平台操作

| 用户问题或判断点 | 先加载 |
| --- | --- |
| 安装、更新、卸载、账号、多账号切换、全局发现 | [`references/setup/install-and-config.md`](references/setup/install-and-config.md) |
| 本机 Clash Verge 的 `*.sii.edu.cn`、`SII Proxy` / `DIRECT` 分流 | [`references/setup/sii-proxy.md`](references/setup/sii-proxy.md) |
| 项目初始化、`INSPIRE.md`、Project / Workspace / Paths / Image 问询、项目信息持续维护 | [`references/project-context.md`](references/project-context.md) |
| Workspace、Compute Group、Quota、实时资源、优先级、Workload Profile | [`references/resources.md`](references/resources.md) |
| 项目归属、负责人、预算与平台优先级（`inspire project`，全局对象、不按 Workspace 划分） | [`references/project-context.md`](references/project-context.md) |
| 共享盘、存储池、挂载隔离、Path Alias | [`references/paths.md`](references/paths.md) |
| 数据广场检索、官方数据集挂载、版本与访问权限 | [`references/dataset.md`](references/dataset.md) |
| 联网准备、SII 内部源、依赖安装、镜像固化 | [`references/internal-sources.md`](references/internal-sources.md) |
| Notebook 创建、连接、跨账号解析、`exec` / `shell` / `scp`、IDE URL、文件流转 | [`references/notebook.md`](references/notebook.md) |
| GPU Job、CPU HPC、Ray、Serving、TensorBoard，及提交后的观察、优先级与异常 | [`references/compute-workloads.md`](references/compute-workloads.md) |
| CPU 准备、数据处理、GPU 训练、部署或交付的阶段化计划 | [`references/workflows.md`](references/workflows.md) |
| Image 选择、保存、注册、可见性和清理 | [`references/image.md`](references/image.md) |
| Model Registry、Model Version 与 Serving 的关系 | [`references/model.md`](references/model.md) |

### CLI 开发

仅在维护 CLI Browser API 封装、核对前端请求合同，或用户明确要求接口细节时加载：

| 需要什么 | 加载 |
| --- | --- |
| 请求契约、响应信封、认证与 Session、分页、Workspace scoping、错误码、探针方法、变更验收；13 条路由 114 个 Action 的请求体 / 响应 / 参数语义 / CLI 映射 / 限制；创建面字段合同；数据广场（`aip.sii.edu.cn`）的握手、信封与目录端点 | [`references/dev/browser-api.md`](references/dev/browser-api.md) |
