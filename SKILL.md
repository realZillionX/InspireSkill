---
name: inspire
description: "Use for Inspire/启智平台 (qz.sii.edu.cn) through the inspire CLI: install/update, account or SII proxy setup, workspace/project/resource/path selection, Notebook, GPU Job, HPC, Ray, Serving, Image, or Model Registry lifecycle, observation, and cleanup. Also use for Inspire CLI Browser API maintenance; use CLI help for syntax and load only the matching reference."
---

# Inspire Skill

`inspire` 是启智平台的本地 CLI。命令组、子命令、参数、默认值和示例以
`inspire --help`、`inspire <group> --help` 和 `inspire <group> <subcommand> --help`
为准；本文件只补充平台判断、风险边界和 Reference 路由。

## 核心不变量

先把任务拆成四个平面，再查 CLI Help：

| 平面 | 绑定内容 |
| --- | --- |
| 调度条件 | `workspace`、`project`、`group`、`quota`、GPU/CPU/内存/Shared Memory 和 `image`，决定任务在哪里、以什么规格运行。 |
| 远端文件 | 代码、数据、权重、Checkpoint 和产物的共享盘路径；Path Alias 只描述远端路径。 |
| 工作负载 | 交互调试用 Notebook；固定 GPU 后台任务用 Job；CPU Slurm 批处理用 HPC；弹性 Worker、长守护或流式任务用 Ray；模型 HTTP 服务用 Serving。 |
| 观察收尾 | Events 看调度，Logs 看程序，Metrics/Instances 看实际工作单元，Status 看平台状态；最后核验业务健康和产物，再清理资源。 |

创建 Workload 时显式绑定 `workspace/project/group/quota/image`；这些调度字段没有隐式默认值，
或引用保存这五项的 Workload Profile。从同一条 Live Quota Row 复制完整 `group` 和 `quota`。
Shared Memory 是实例级资源，细节见 [`references/compute-workloads.md`](references/compute-workloads.md)。
Workload Profile 保存调度条件，Path Alias 保存远端路径，两者职责分开。

Live 查询是平台事实源；缓存用于连接复用。日常选择通常是：

- `CPU资源空间`：CPU Notebook、联网准备、依赖安装、HPC 数据处理和 CPU Ray。
- `分布式训练空间`：GPU Notebook、GPU Job、多节点训练、Serving 和 GPU 观察。
- 国产卡分区、`CI-情境智能` 或专属空间：任务明确要求特殊硬件、权限或项目环境时使用。

Name、Alias 和可读状态是用户侧接口；平台 Handle 只经专门 `id` 命令或 Resolver 处理。
Notebook 连接的 `--account` 使用本地 Account Alias；重建 SSH Tunnel 时沿用目标 Notebook
所属账号的 Session 和配置。

## 网络与合规闸门

先区分公网与 SII 内部源：公网内容在可联网 `CPU资源空间` 准备后写入共享盘或已验证镜像；
内部源在目标环境验证并固化到镜像。受限 Notebook 的命令走 `exec` / `shell`（JupyterTerminal），
文件经 `/inspire/...` 共享路径和可联网 Notebook 的 `scp` / `rsync` 流转。

分别核验网络可达性、服务地域与条款、项目授权。受限区保持平台提供的 JupyterTerminal
和共享路径闭环，不做反向隧道、代理、VPN 或中继穿透；未获准或仅限海外的模型 API、AI
编程服务不在启智远端启动。信息不足时让 Agent 留在本地或已批准的联网环境。细节见
[`references/network-and-sources.md`](references/network-and-sources.md) 和
[`references/notebook.md`](references/notebook.md)。

## 最短执行闭环

1. 选一个 focused Reference；跨边界时再加载第二份。
2. 用 CLI Help 确认当前命令表面。
3. Live 查询账号、Workspace、Project、Group、Quota、Image 和资源可用性。
4. 准备共享路径、代码和环境；复杂条件先 `dry-run` 或短 Probe。
5. 提交 Workload。
6. 依次观察 Events、Logs、Metrics、Instances、Status。
7. 核验业务健康与产物完整性。
8. 运行中的对象先 `stop`，再 `delete`；终态且不再需要的对象直接 `delete`，仍可能被使用的对象保留。

## 按需加载索引

命令语法回到 CLI Help；每次先加载最匹配的一份 Reference：

### 平台操作

| 用户问题或判断点 | 先加载 |
| --- | --- |
| 安装、更新、账号、首次发现、项目初始化 | [`references/setup/install-and-config.md`](references/setup/install-and-config.md) |
| 本机 Clash Verge 的 `*.sii.edu.cn`、`SII Proxy` / `DIRECT` 分流 | [`references/setup/sii-proxy.md`](references/setup/sii-proxy.md) |
| Workspace、Compute Group、Quota、实时资源、Workload Profile | [`references/resources-and-paths.md`](references/resources-and-paths.md) |
| 公网、SII 内部源、离线 GPU 空间、镜像固化 | [`references/network-and-sources.md`](references/network-and-sources.md) |
| 共享盘、存储池、挂载隔离、Path Alias | [`references/paths.md`](references/paths.md) |
| Notebook 创建、连接、`exec` / `shell` / `scp`、IDE URL、文件流转 | [`references/notebook.md`](references/notebook.md) |
| GPU Job、CPU HPC、Ray、Serving，及提交后的观察与异常 | [`references/compute-workloads.md`](references/compute-workloads.md) |
| CPU 准备、数据处理、GPU 训练、部署或交付的阶段化计划 | [`references/workflows.md`](references/workflows.md) |
| Image 选择、保存、注册、可见性和清理 | [`references/image-management.md`](references/image-management.md) |
| Model Registry、Model Version 与 Serving 关系 | [`references/model.md`](references/model.md) |

### CLI 开发

仅在维护 CLI Browser API 封装、核对前端接口合同、执行 Reverse Capture，或用户明确要求接口细节时加载：

[`references/dev/browser-api.md`](references/dev/browser-api.md)

## 项目上下文所有权

每个启智项目仓库在根目录维护 `INSPIRE.md`，保存非配置性的项目事实，例如 Default Image、
Path Conventions、Public Directory Layout、Existing Notebooks 和 Ongoing Jobs。账号、凭据、
平台 Session、`.inspire` 配置和本地执行计划分别由 CLI 配置层与 `AGENTS.md` / `CLAUDE.md`
管理；不同 Harness 共享同一份 `INSPIRE.md` 项目事实。
