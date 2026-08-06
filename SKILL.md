---
name: inspire
description: "Use for Inspire/启智平台 (qz.sii.edu.cn) through the inspire CLI: install/update, accounts, local SII proxy setup, workspace/project/resource/path selection, Notebook, GPU Job, HPC, Ray, Serving, Image, Model Registry, observation, cleanup, and Inspire CLI Browser API maintenance. Use CLI Help for syntax and load only the focused reference."
---

# Inspire Skill

`inspire` 是启智平台的本地 CLI。命令组、子命令、参数、默认值和示例以 `inspire --help`、`inspire <group> --help` 和 `inspire <group> <subcommand> --help` 为准；本文件只保留平台判断、风险边界、最短执行闭环和 Reference 路由。

## 核心操作模型

执行前先把任务拆成四个平面：

| 平面 | 绑定内容 |
| --- | --- |
| 调度条件 | `workspace`、`project`、`group`、`quota`、GPU/CPU/内存/Shared Memory 和 `image`，决定任务在哪里、以什么规格运行。 |
| 远端文件 | 代码、数据、权重、Checkpoint 和产物的共享盘路径；Path Alias 只描述文件在哪里。 |
| 工作负载 | 交互调试用 Notebook；固定 GPU 后台任务用 Job；CPU Slurm 批处理用 HPC；弹性 Worker、常驻或流式任务用 Ray；模型 HTTP 服务用 Serving。 |
| 观察收尾 | Events 看调度，Job Logs 看程序，Metrics / Instances 看实际工作单元，Status 看平台状态；其它 Workload 的应用日志按对应 Help 和共享盘约定处理。最后核验业务健康和产物，再清理资源。 |

创建 Workload 时显式绑定 `workspace`、`project`、`group`、`quota` 和 `image`，或引用保存这五项的 Workload Profile；这些调度字段没有隐式默认值。选择资源时从同一条 Live Quota Row 复制完整 `group` 和 `quota`。GPU Job Shared Memory 是实例级资源，不能超过所选 Quota 的实例内存；细节见 [`references/compute-workloads.md`](references/compute-workloads.md)。Workload Profile 保存调度条件，Path Alias 保存远端路径，两者不能互相替代。

Live 查询是账号、Workspace、Project、Compute Group、Quota、Image 和资源可用性的事实源；按账号隔离的本地缓存只用于连接、Name 解析和 Quota 目录复用，可通过 `inspire cache status|refresh|clear` 管理。CLI 对 Agent 的稳定资源身份只有 Name 和 Alias；同名对象用 Workspace、可读候选和 `--pick` 消歧，内部解析对 Agent 透明。

默认输出也有明确预算：发现类列表和 Batch 结果最多展示 20 项，使用命令自身的 `--limit/-n` 收窄或 `--all` 显式展开；Job 日志默认限制为 100 行 / 条目和 16,000 个字符。截断时只给出已展示数量和继续获取完整结果的选项。根级 `inspire --json ...` 保持单一 JSON 文档，不混入进度、请求包装、绝对本地路径或调试日志。

日常 Workspace 选择通常很直接：

- `CPU资源空间`：CPU Notebook、联网准备、依赖安装、HPC 数据处理和 CPU Ray。
- `分布式训练空间`：GPU Notebook、GPU Job、多节点训练、Serving 和 GPU 观察。
- 国产卡分区、`CI-情境智能` 或专属空间：任务明确要求特殊硬件、权限或项目环境时使用。

## 网络与合规闸门

先区分公网与 SII 内部源。公网内容在可联网的 `CPU资源空间` 准备后写入共享盘或固化到已验证镜像；内部源在目标环境验证并固化到镜像。`H100` / `H200` Compute Group 的受限 Notebook 命令通过 `exec` / `shell` 使用 JupyterTerminal，文件经 `/inspire/...` 共享路径和支持 SSH 的 Notebook 的 `scp` 或外部 `rsync` 流转。

网络可达性不等于服务授权；分别核验接入端点、服务地域与条款、项目政策。不可上网区保持平台提供的 JupyterTerminal 和共享路径闭环，不通过反向隧道、代理、VPN 或中继穿透；未获授权或仅限海外使用的模型 API、AI 编程服务不在启智远端启动。信息不足时让 Agent 留在本地或已批准的联网环境。完整判断见 [`references/network-and-sources.md`](references/network-and-sources.md) 和 [`references/notebook.md`](references/notebook.md)。

## 最短执行闭环

1. 根据用户目标加载一份最匹配的 Reference；跨边界时再加载第二份。
2. 用 CLI Help 确认当前版本的真实命令表面。
3. 用 Live 查询确认账号、Workspace、Project、Compute Group、Quota、Image 和资源可用性。
4. 准备共享路径、代码和环境；复杂条件先 `dry-run` 或运行短 Probe。
5. 提交匹配的 Workload。
6. 按当前 Workload 的 Help 依次观察可用的 Events、Logs、Metrics、Instances 和 Status。
7. 核验业务健康与产物完整性。
8. 运行中的对象先 `stop`，再 `delete`；终态且不再需要的对象直接 `delete`，仍有当前或声明的未来消费者时保留。

## 按需加载索引

命令语法和参数始终回到 CLI Help。每次先加载最匹配的一份 Reference：

### 平台操作

| 用户问题或判断点 | 先加载 |
| --- | --- |
| 安装、更新、账号、多账号切换、首次发现、项目初始化 | [`references/setup/install-and-config.md`](references/setup/install-and-config.md) |
| 本机 Clash Verge 的 `*.sii.edu.cn`、`SII Proxy` / `DIRECT` 分流 | [`references/setup/sii-proxy.md`](references/setup/sii-proxy.md) |
| Workspace、Compute Group、Quota、实时资源、优先级、Workload Profile | [`references/resources-and-paths.md`](references/resources-and-paths.md) |
| 公网、SII 内部源、离线 GPU 空间、远端服务合规、镜像固化 | [`references/network-and-sources.md`](references/network-and-sources.md) |
| 共享盘、存储池、挂载隔离、Path Alias | [`references/paths.md`](references/paths.md) |
| Notebook 创建、连接、跨账号解析、`exec` / `shell` / `scp`、IDE URL、文件流转 | [`references/notebook.md`](references/notebook.md) |
| GPU Job、CPU HPC、Ray、Serving，及提交后的观察、优先级与异常 | [`references/compute-workloads.md`](references/compute-workloads.md) |
| CPU 准备、数据处理、GPU 训练、部署或交付的阶段化计划 | [`references/workflows.md`](references/workflows.md) |
| Image 选择、保存、注册、可见性和清理 | [`references/image-management.md`](references/image-management.md) |
| Model Registry、Model Version 与 Serving 的关系 | [`references/model.md`](references/model.md) |
| 具体项目的稳定平台拓扑、远端路径、永久基础设施、资产身份与生命周期 | [`references/project-assets.md`](references/project-assets.md) |

### CLI 开发

仅在维护 CLI Browser API 封装、核对前端请求合同，或用户明确要求接口细节时加载 [`references/dev/browser-api.md`](references/dev/browser-api.md)。

## 具体项目的资产合同

`INSPIRE.md` 只属于某个具体使用启智平台的科研或工程项目工作区，用于让不同 Agent 共享该项目的稳定平台拓扑、Canonical Remote Paths、永久基础设施，以及 Image、Model、Dataset、Checkpoint 等资产的精确身份、消费者、来源和生命周期。它不是 InspireSkill 安装产物，也不是所有仓库必备的元数据；CLI、Skill 或通用工具源码仓库不因自身涉及启智而创建 `INSPIRE.md`。

账号、凭据、代理、平台 Session、`.inspire` 配置、实时资源余量、当前 Workload 状态、日志、Smoke 结果和短期计划不进入 `INSPIRE.md`。需要创建或维护具体项目的资产合同时，加载 [`references/project-assets.md`](references/project-assets.md)。
