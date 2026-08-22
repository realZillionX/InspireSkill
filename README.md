<p align="center"> <img src="https://raw.githubusercontent.com/realZillionX/InspireSkill/main/assets/hero.svg" width="100%" alt="Inspire Skill: the Agent-Native cockpit for the Inspire compute platform"/> </p>

<p align="center"> <b>让 AI Agent 直接在本地 CLI 里完成启智平台的全部操作。</b><br/> </p>

<p align="center"> <a href="https://github.com/realZillionX/InspireSkill/tree/main/cli"><img src="https://img.shields.io/badge/CLI-bundled-3366FF?style=for-the-badge" alt="CLI bundled"/></a> <img src="https://img.shields.io/badge/Harness-Claude%20Code%20/%20Codex%20/%20Antigravity%20/%20Cursor%20/%20OpenClaw%20/%20OpenCode%20/%20Qoder%20CLI%20/%20Qoder%20Work%20/%20Kimi%20Code%20/%20Kimi%20Desktop-5566FF?style=for-the-badge" alt="Harnesses"/> <img src="https://img.shields.io/badge/status-actively%20maintained-22CCEE?style=for-the-badge" alt="Actively maintained"/> <img src="https://img.shields.io/badge/license-MIT-0f172a?style=for-the-badge" alt="License MIT"/> </p>

---

# 本项目建立的意义

在本项目开始筹办之初，对于所有 SII 的学生，[启智平台](https://qz.sii.edu.cn)是科研实验链路里最慢的那一环：每次申请资源、新建 Notebook、新建训练任务、同步代码都要反复点点点，SSH 等更进一步的功能更是遥遥无期。

本着过渡到大 Agent 时代、将一切重复性机械工作交给 Agent 的初衷，我们创办了 InspireSkill 项目，旨在将启智平台 GUI 打平为 CLI，并建立了 CLI + Skill 的一体化系统，让 InspireSkill 成为所有 Agent 开箱即用的工具、让你的 Claude Code / Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder CLI / Qoder Work / Kimi Code / Kimi Desktop 成为进行科研工作的唯一入口。

建立和维护本项目的过程并非易事，InspireSkill 也并非只是将[启智平台](https://qz.sii.edu.cn)的网页 API 打平重构为 CLI 的简单工作，在维护本项目的过程中，设计高于平台语义的高层功能、寻找启智平台中细枝末节的 API 并将其优雅融入 CLI 系统中、尤其是维护一个易于 Agent 阅读且包含平台所有特性的文档系统都给我们带来了不小于 CLI 本身的麻烦。

在长时间的开发与维护中，以 [@realZillionX](https://github.com/realZillionX) 和 [@JingYiJun](https://github.com/JingYiJun) 为首的开发团队始终秉持着注重细节与优雅的开发者精神，最终构建出一个令人满意的项目。时至今日，我们可以自豪地说：**InspireSkill 所包含的功能，只有你想不到，没有我们做不到**。它们包括但不限于：对 HDD / SSD / QB-ILM 等项目路径的优雅维护、翻转镜像的可见范围、将平台内部源入口交给 Agent（从而使在不可上网区配置镜像成为可能）、联网 Notebook 的 SSH 板块、受限 Notebook 的 JupyterTerminal 执行路径、空闲 8 卡整节点总量的查询、低优任务占用总量的查询、将 Notebook / 训练任务的资源视图 / 事件 / 聚合日志交给 Agent。

# 对初次使用者的简单介绍

InspireSkill 将算力平台的一切入口交给 AI Agent。当 Claude Code / Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder CLI / Qoder Work / Kimi Code / Kimi Desktop 识别到本项目所提供的 `SKILL.md`，它会：

- 直接调用 `inspire` 命令查实时资源、开 Notebook、提 HPC 任务、拉日志
- 全程只用 Name：参数、帮助、错误、人类输出和 JSON 输出都使用 Name、Account Alias、Path Alias 和可读状态，不需要 Agent 记忆或搬运平台内部 ID
- 提供可选的 Clash Verge Mixed Port 分流模板，让公网与启智内网共存一套本地代理配置，取代多人共用断连的 aTrust；CLI 本身不绑定固定端口，任何能同时覆盖公网与 `*.sii.edu.cn` 的代理方案都行
- 把平台网页上的常用操作都变成可复现、可串联、可自动化的命令链
- 从 `SKILL.md` 按需加载对应使用手册，理解调度语义、资源申请原则和验收点，不需要用户在对话里反复向 Agent 解释平台语义

## 为什么比 InspireCode / 在实例里装 Agent 更好？

启智官方的 InspireCode 是把 OpenCode 直接部署到某个 Inspire 实例里，要用就得打开 `qz.sii.edu.cn`、进那个实例、在它的终端里跟 OpenCode 对话。凡是“把 Agent 装在服务器上”的方案都是这个路数。InspireSkill 走相反路径：Agent 留在本机，Inspire 降格为被调用的工具。

| 维度 | InspireCode（Agent 装在 Inspire 实例里） | InspireSkill（Agent 装在本机） |
| --- | --- | --- |
| Agent 生命周期 | 绑死在某一个 Notebook 实例；实例回收 / 崩溃，对话与状态一起没 | 跑在本机 Harness 里，与任何一个 Inspire 实例解耦 |
| 调度范围 | 只能操作它所在那一个实例的文件系统与运行时 | 一个 Agent 横跨多 Workspace / Notebook / HPC Job / Image，全平台统一编排 |
| 入口 | 必须打开 `qz.sii.edu.cn` 网页 | 大家本来就在用的 Claude Code / Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder CLI / Qoder Work / Kimi Code / Kimi Desktop |
| Harness / 模型选择 | 锁定 OpenCode + 它支持的模型 | 任选本机已装的 10 家 Harness，模型可随意配置 |
| 上下文来源 | 只有实例里能看到的东西；本地代码仓库不在场 | 本机完整 Repo + Git 状态 + 编辑器 + 其他 MCP 工具（Figma / Preview / Playwright / …）一起可用 |
| 计算占用 | Agent 进程吃 Inspire 实例的 CPU / RAM 配额；API Key 必须放在实例里 | Agent 进程跑本机；Inspire 实例的 CPU / RAM 全给训练 / HPC；API Key 只留本地 |
| 自动化 / 可复现 | 对话历史锁在浏览器页面里 | 命令流可保存 / 回放；可读格式给 Agent 决策，结构化输出留给脚本消费 |

一句话：InspireCode 把 Agent 搬进 Inspire，InspireSkill 把 Inspire 变成 Agent 的一把工具。

---

## 为什么比社区里其它启智 CLI 更值得用？

启智社区还有两条独立维护的 CLI：[EmbodiedForge/Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) 和 [tianyilt/qzcli_tool](https://github.com/tianyilt/qzcli_tool)。它们都解决了部分网页操作自动化问题，尤其 qzcli_tool 已经覆盖资源查询、GPU Job 提交、HPC Submit、Logs、Dashboard 和 Jupyter Exec，也提供 `qzcli-mcp` 给 MCP-Capable Harness 使用。

InspireSkill 的定位更往前走了一层：它不是把若干 API 包成命令，而是把启智平台整理成一套 Agent 能长期使用的操作模型。安装、命令面、`SKILL.md`、`references/`、具体启智项目的 `INSPIRE.md` 资产合同、Path Alias、Workload Profile、观测和清理闭环都在同一套设计里。

| 维度 | [Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) | [qzcli_tool](https://github.com/tianyilt/qzcli_tool) | InspireSkill |
| --- | --- | --- | --- |
| 安装与更新 | 源码渠道为主 | Clone 仓库、`pip install -e .`、手动 `mcp add` | `curl \| bash` 一键安装 CLI、`SKILL.md` 和 `references/`，`inspire update` 同步更新 |
| Agent 文档系统 | 无统一 Skill 文档 | `qzcli-mcp` 的薄 Skill，主要说明工具调用顺序 | `SKILL.md` 是平台操作模型入口，按场景路由到完整 `references/` |
| Harness 落位 | 无 | MCP 可接入 MCP-Capable Harness，但需要用户自己注册 | 安装器自动写入 Claude Code / Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder CLI / Qoder Work / Kimi Code / Kimi Desktop 的约定目录 |
| Notebook 连接 | 依赖用户预配本地组件或容器公网 | Jupyter Terminal API Exec | SSH / Shell / Exec / SCP / OpenSSH Config / Proxy URL / Connection Cache / 跨账号重建 |
| Workload 覆盖 | 少量训练 / HPC 能力 | 资源、GPU Job、HPC Submit、Logs、Dashboard、Jupyter Exec | Notebook / GPU Job / CPU HPC / Ray / Serving / TensorBoard / Model / Image / Dataset / Project / Resources 全覆盖 |
| 观测闭环 | 有限 | Job Logs、Watch、Usage / Dashboard | Events / Logs / Metrics / Instances / Lifecycle / Status 分层诊断 |
| 资源与路径语义 | 主要是配置和命令参数 | 资源缓存、Workspace / Compute Group / Spec 解析 | Workload Profile 管调度条件，Path Alias 管远端路径，具体启智项目的 `INSPIRE.md` 管持久资产合同 |
| 多账号与项目层 | `[accounts."<user>"]` 合并层 | 以单套 `~/.qzcli/` 配置为中心 | 一账号一目录，账号级默认值和仓库级项目覆盖分层 |

一句话：这两条 CLI 各做了一段路；InspireSkill 把整个平台的操作面、文档面和观测面端到端铺平，让 Agent 不只是“能调用命令”，而是能理解应该怎么用启智平台。

---

# 快速上手

> 平台支持：macOS、Linux、Windows 都是一等公民，CI 覆盖 Linux 与 Windows。Windows 走系统自带的 OpenSSH，不需要 WSL；`rsync` 是可选外部工具，Windows 上用 `inspire notebook scp` 传文件。

## 安装

### macOS / Linux

前置：`bash` / `curl` / `tar` / Python 3.10+ / 已装 `uv`（推荐）或 `pipx` 任一。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

### Windows

前置：Python 3.10+ / `uv`（推荐）或 `pipx` 任一 / OpenSSH 客户端。

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

在仓库根目录运行安装脚本（`-SkipPlaywright` 可跳过 Chromium 下载，代价是浏览器登录不可用）：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

Windows 上有两处和 POSIX 不一样，都会咬人：

- **`ssh-config` 别用 `>>` 追加。** Windows PowerShell 5.1 的 `>>` 写的是 UTF-16LE，OpenSSH 读不了。用 `inspire notebook ssh-config <notebook> | Out-File -Encoding utf8 -Append $env:USERPROFILE\.ssh\config`（PowerShell 7+ 默认已是 UTF-8，`>>` 可用）。
- **确认用的是哪个 `ssh.exe`。** `Get-Command ssh -All`：系统自带的 `C:\Windows\System32\OpenSSH\ssh.exe` 和 Git for Windows 附带的那个对 ProxyCommand 的处理方式不同。两个都能用，但混用时报错信息会互相矛盾。

安装、可选参数和安装后检查见 [`references/setup/install-and-config.md`](references/setup/install-and-config.md)。

## 更新

```bash
inspire update                # CLI 包 + SKILL.md / references/ 一起升到最新
inspire update --check        # 只检查，不动
inspire update --cli-only     # 仅升 CLI 包与运行时
inspire update --skill-only   # 仅刷 SKILL.md / references/
```

升级旧版本和 Installer 检测说明见 [`references/setup/install-and-config.md`](references/setup/install-and-config.md)。

## 卸载

```bash
inspire uninstall                # skill 目录 + 更新检查 agent + CLI 包
inspire uninstall --purge        # 连 ~/.inspire 的账号配置一起删
inspire uninstall --purge-runtime # 连共享的 Playwright 浏览器缓存一起删
```

执行前会打印完整清单并要求确认。账号配置和浏览器缓存默认保留：前者重装后还能直接用，后者是本机所有 Playwright 工具共用的；仓库自己的 `INSPIRE.md` 和 `./.inspire/` 任何一档都不碰。CLI 已经跑不起来时，用安装脚本的 `--uninstall` 兜底，分层与参数见 [`references/setup/install-and-config.md`](references/setup/install-and-config.md)。

## 完整初始化（安装后必跑）

```bash
inspire account add <name>
inspire account check
inspire init
cd /path/to/your-repo
inspire init --scope project
inspire resources availability --workspace 分布式训练空间 --include-cpu
```

`inspire init` 校验账号并清理旧版写入的派生 Catalog/Path Alias，只保留真正的账号级设置；`--scope project` 才为当前仓库写入 Project Context 和 Path Alias。Notebook 远端命令省略 `--cwd` 时不注入 `cd`，保留平台、容器或远端 Shell 给出的初始目录。

安装、更新和多账号操作见 [`references/setup/install-and-config.md`](references/setup/install-and-config.md)；项目初始化问询（Project / Workspace / Paths / Image）和 `INSPIRE.md` 维护见 [`references/project-context.md`](references/project-context.md)；Clash Verge 的 SII Proxy / DIRECT 分流模板见 [`references/setup/sii-proxy.md`](references/setup/sii-proxy.md)。

---

# 能力一览

按能力域折叠，点开你关心的那一个。命令组、子命令、参数和默认值一律以 `inspire <group> <subcommand> --help` 为准。

<details>
<summary><b>📝 Notebook 统一入口</b> —— 交互工作台、连接、文件流转、把跑通的环境固化成镜像</summary>

全链路命令化：`create / batch / list / status / start / stop / delete / ssh / ssh-config / ssh-proxy / connection / exec / shell / scp / install-deps / proxy-url / path / quota / profile / metrics / events / lifecycle / save-image / cancel-save-image`。容器里部署好的服务用 `proxy-url --port` 拿到外部地址直接请求；`ssh-proxy` 是给 OpenSSH `ProxyCommand` 用的裸流转发，`ssh-config` 生成的配置里就指向它。

把跑通的环境固化成镜像是 Notebook 自己的生命周期事件：`save-image` 会先报平台估算的快照体积（`--dry-run` 只估不存），保存期间该 Notebook 不可操作，中途要拿回来用 `cancel-save-image`——已经打出「等待推送」之后取消仍然生效。默认是在起点镜像上再堆一层，反复迭代的环境层数会一路累积，`--flatten` 把结果压成单层：实测**压平反而更小**（8 层 331.78 MB → 1 层 286.90 MB），多出来的时间落在镜像构建上而不落在 Notebook 上，两种保存都在同一时刻把容器还给你。

显卡不是 `H100` / `H200` 的 Notebook 可使用 OpenSSH / SCP / SSH Config；`H100` / `H200` 受限 Notebook 使用 JupyterTerminal 执行命令，文件流转以 `/inspire/...` 共享路径为边界，并通过支持 SSH 的 Notebook 使用 `notebook scp` 或外部 `rsync` 完成本地上传/下载。连接类命令会跨账号解析本地已缓存的 Notebook Connection，不要求先切 Active Account。

</details>

<details>
<summary><b>🏃 GPU 后台任务（平台名：分布式训练）</b> —— 一张卡到多节点，后台 GPU 任务都走这里</summary>

平台官方把 `job` 这一路叫“分布式训练” / Distributed Training；提交 Job 时只要求 GPU 计算资源和启动命令，不强制程序必须是训练。`inspire job` 可用于一张卡、多卡、单节点、多节点等后台 GPU 任务：分布式训练 / 批量推理 / 并发 Worker Pool 都走这里（`hpc` 对应 CPU Slurm）。

`inspire job create / batch / list / status / command / wait / stop / delete / events / instances / shell / logs / metrics / quota / profile`。提交统一使用 `job create`（一次提交多个用 `job batch`）；`--exclude-node` 排除坏节点，Workspace 开启指定节点能力时用可重复的 `--specified-node` 绑定节点，二者都会进入 dry-run。可用 `--enable-notification` 开启当前用户绑定飞书账号的状态通知；脚本里等任务跑完用 `job wait`，忘了提交时写的启动命令用 `job command` 原样读回；需要跟日志时用 `job logs <name> --workspace <workspace> --follow`，健康度用 `job metrics <name> --workspace <workspace>` 看 GPU、显存、CPU、内存、I/O 和多 Pod 负载是否同步。

</details>

<details>
<summary><b>🚀 HPC 任务分派</b> —— 只写 Slurm 正文，两层规格由 CLI 在提交前挡下</summary>

`inspire hpc create / batch / list / status / stop / delete / events / instances / shell / logs / metrics / quota / profile`。`hpc create -c <slurm-body>` 只写 Slurm 正文 + 显式 `srun`，平台自动补 `#SBATCH` 头。两层独立：节点资源用 `--quota gpu,cpu,mem`（CLI 自动解析到平台 Quota Row），Slurm 调度用 `--number-of-tasks / --cpus-per-task / --memory-per-cpu`。

两层之间平台和网页端都不校验，规格不匹配时要么 `FAILED` 且日志和事件里都没有原因，要么一直 `RUNNING` 却什么都没跑，所以 `hpc create` 在提交前自己挡下这些组合。`hpc status` 的 `Steps` 是判断「程序到底跑没跑」的字段——正文忘了 `srun` 的任务照样报成功，但 `Steps` 是 `0/0`。

</details>

<details>
<summary><b>🧬 弹性计算（Ray）</b> —— Head 加可伸缩 Worker Group，以及弹性到底动没动过</summary>

`inspire ray create / batch / list / status / start / stop / delete / events / instances / shell / logs / metrics / scaling / quota / profile`：一个 Head 加多个可伸缩 Worker Group。停掉的 Job 保留完整集群规格，`ray start` 原样拉回来，不需要重新指定；平台在这里会「受理但不执行」，所以命令以状态真的离开 `STOPPED` 为准，没动就报失败。

弹性是 Ray 存在的理由，而「`min` / `max` 到底动没动过」要用 `ray scaling` 才看得到：它按时间列出每个 Worker Group 的每一次副本数变更，空的历史说明这个弹性区间从来没被用到。

</details>

<details>
<summary><b>🛰 模型部署（Serving）</b> —— 部署、伸缩、回滚，以及只有请求侧才看得见的那一半</summary>

`inspire serving create / batch / list / status / start / stop / delete / scale / scale-history / versions / rollback / configs / events / instances / shell / logs / metrics / api-metrics / quota / profile`：覆盖模型部署服务的创建、列表、状态、启停与删除、副本伸缩与伸缩历史、部署历史与回滚、可用配置、事件、实例、日志和指标；创建前用 `serving quota --workspace <workspace>` 选 Quota，用 `model deploy-config` 确认规格下限。

`metrics` 看资源占用，`api-metrics` 看请求量、成功率和延迟——只有后者能把「没人调用」和「一直调用一直失败」分开。没重新部署过而延迟变了，先看 `scale-history`：掉下去的副本数、没落地的自动伸缩只出现在这里，`versions` 里一个字都没有。

</details>

<details>
<summary><b>📉 TensorBoard</b> —— 把 loss 和 eval 曲线当数字读回来，不需要有人去看一眼图</summary>

`inspire tensorboard create / list / status / start / stop / delete / tags / scalars`：TensorBoard 在平台上是一等对象——计算组单独声明 `tensorboard` 任务类型，board 既能挂在训练任务上，也能对任意一个 summary 目录单独建；规格由平台固定成 1 CPU / 2 GiB，没有 Quota 也没有镜像要选。

关键是 `tags` 和 `scalars` 直接读运行中的 board：Agent 自己建一个 board 指向训练目录，再把 loss 和 eval 曲线当数字读回来——首尾值、step 区间、最小最大值，`--points N` 给最后 N 个点——不需要浏览器，也不需要有人替它去看一眼图。`metrics` 回答「这个任务在平台侧还健康吗」，这里回答「模型训得怎么样」。

</details>

<details>
<summary><b>📈 指标、事件、日志、实例 & 远端 PTY</b> —— 「这东西为什么没起来」分几层查</summary>

`notebook metrics` / `job metrics` / `hpc metrics` / `ray metrics` / `serving metrics` 读取平台 `资源视图` 的历史时间序列，默认输出 PNG 趋势图，`--no-plot --sparkline` 适合终端快速判断。

`job events` / `hpc events` / `notebook events` / `ray events` / `serving events` 拉平台 Events——不加参数就把控制器事件和每个 Pod 的事件合成一条时间线（`--instance` 收窄到某个实例，`--workload-level` 反过来只留控制器那一半），因为「这东西为什么没起来」的答案通常在 Pod 那一半。

一批任务一起看时，`job status` / `hpc status` / `job events` 可以直接跟多个名字：平台每 20 个任务答一次，比一个个问快得多，事件会合成一条按时间排好、标着出处任务的时间线。名字答不上来的（打错、已删、或一个名字对上了好几个任务）**不会中断整条命令**——能答的照常打印，答不了的单独列在 `Unresolved:` 里，退出码同时告诉脚本这份答案是残缺的。

`job logs` / `hpc logs` / `ray logs` / `serving logs` 读程序自己的输出，四条共用同一套预算和同一份 JSON schema；`job instances` / `hpc instances` / `ray instances` / `serving instances` 看 Live Pod / Component 清单和每个 Pod 落在哪个节点，`notebook lifecycle <name>` 看一个实例的多次启停记录。

读完还要进去看的时候，`job shell` / `hpc shell` / `ray shell` / `serving shell` 把本地 stdin 接到实例里的远端 PTY（`exit` 退出、`Ctrl+]` 断开），默认进哪个实例按 Workload 定——HPC 进 `launcher`（`srun` 在那儿跑），Ray 进 head（驱动和 `ray status` 在那儿），Serving 进第一个运行中的副本，要点名用 `--instance`。

节点归属还有任务级的一层：`job` / `hpc` / `serving status` 直接列出落点节点（`job` 另给创建时的 Pin 与排除节点），`notebook status` 的 `Node` 附带该节点的健康状态。排查坏节点、复现实验、定位掉队的 Worker 都从这里开始。

</details>

<details>
<summary><b>📊 资源情报</b> —— 哪个组有空、余量去哪了、能抢回来多少、拿到手能留多久</summary>

`resources availability --workspace <name> --include-cpu` / `resources nodes --workspace <name>` / `resources usage --workspace <name>` / `resources policy --workspace <name>` / `<workload> quota --workspace <name>`：定位一个 Workspace 里哪个计算组有空，支持透支式申请。`<workload> quota` 回答「有哪些合法档位」，`availability` 回答「这些档位现在还有没有空」，`usage` 回答「余量去哪了、其中哪些能抢回来」——它的 `Reclaimable` 列是持有者手里有多少卡落在以可抢占优先级提交的任务上，`--group <关键词>` 把这个判断收窄到任务真正提交进去的那个计算组，`policy` 回答「拿到手能留多久——空闲多久被回收、有没有运行时长上限」。

`<workload> quota` 的 `Priority` 列还给出这一行接受哪些任务优先级：`分布式训练空间` 的训练区碎卡档只调度低优先级（可被抢占），整节点档才不受限，创建时 CLI 按这一列先做预检，不用等平台拒绝；`Points/h` 列给出这一行每实例每小时烧多少点券——只有 GPU 计费，CPU-only 的行一律是 `0`，而 GPU 按卡型定价（实测 H100 / H200 是 1 点券/卡/小时，4090 是 0.33）。

这些命令一律一次只看一个 Workspace——档位、余量、回收策略和占用都是按 Workspace 定义的事实，跨空间扫一遍答不出任何一个可执行的决定；还接受 `--workspace all` 的只剩「按名字找东西」那一类（`<workload> list` / `account permissions`），因为不知道东西在哪个空间时本来就给不出空间名。

机器本身发生了什么是另一层：`resources node-events <节点名>` 是平台上唯一按节点而不是按工作负载组织的事件源，内核 OOM kill、Cordon / Uncordon、重启、`NodeNotSchedulable` 都在这里，「同一台机器上反复失败」此前在 CLI 里无处可查。

余量和规格始终读 Live 数据；`inspire cache status / refresh / clear` 管的是本地加速缓存——Name 解析索引、Quota 目录和 Notebook 显卡型号，三条命令都支持 `--resource <kind>` 分类操作。正常情况下 `refresh` 根本不需要跑（Workload 名字后台一直在补，其余的解析一次就自己缓存了），所以它**不接受裸形式**，必须用 `--resource` / `--workspace` / `--name` 说明刷哪一块；`cache status` 里 Workload 那几行常态是 `partial`（后台只读最新的一头），要看的信号是 `empty`——刷过、还在有效期内、却一个名字都拿不出来。

</details>

<details>
<summary><b>🗂 镜像管理</b> —— Registry 边界沿着卡的类型走，可见性有一道单向门</summary>

`image list / detail / register / set-visibility / delete`，创建 Notebook、Job、HPC、Ray 或 Serving 时显式传 `--image`；`hpc create --image-type` 明确可见性。

镜像存在 Registry 里而不是 Workspace 里，**多个 Workspace 正常共用同一份 Registry**——这一组都要 `--workspace`，因为那是平台唯一的指定 Registry 的方式，它是路标不是分区，所以 `notebook save-image --workspace X` 存出的镜像在同一个 Registry 上的每个 Workspace 里都看得到。真正会挡住人的是 Registry 边界，而这条线基本沿着卡的类型走：国产卡空间和 NVIDIA 空间读的是两份不相交的目录。一个 Registry 动辄几千个镜像，用 `image list --keyword` 按名字搜。

把跑通的 Notebook 固化成镜像不在这一组——那是 Notebook 的生命周期事件，走 `notebook save-image`。可见性有 `private` / `project` / `public` 三档，**改成 public 是单向门**：之后既删不掉也改不回私有，只有平台管理员能清理。

</details>

<details>
<summary><b>📦 模型注册表（Model）</b> —— 模型版本、部署规格下限、删之前的占用核对</summary>

`inspire model list / register / status / versions / deploy-config / delete`：浏览或注册 Workspace 下的模型 + 每个模型的历史版本，带 vLLM 兼容标记 / 创建时间；`deploy-config` 给出某个版本装得下权重的最小节点规格，正好是 `serving create --quota` 的下限。

`status` 还会说出哪些推理服务仍占着这个版本，换版本或删模型不用再盲操作；`delete` 删整个条目连同全部版本，删之前逐版本核对占用，有服务还可能起来就点名拒绝。之前只能在平台网页里翻。

</details>

<details>
<summary><b>📚 官方数据集</b> —— 数据广场检索与 <code>--dataset</code> 只读挂载</summary>

`inspire dataset list / show / tags / validate / applications`：数据广场是和启智并列的独立平台，只共用同一套 SSO，启智那侧没有检索接口。CLI 用现有登录态走一次 CAS 握手，直接检索目录、读版本、看当前账号有没有挂载权限。

确认后在 `notebook / job / hpc create` 上用 `--dataset <数据集名>:<版本名>` 只读挂载到 `/inspire/dataset/<数据集名>/<版本名>`，创建前平台逐条校验，不会先建出一个缺数据的 Workload。数据集用名字寻址，数据广场内部的数字 ID 拿去挂载会被拒。`--tag` 认的是固定中文词，全量用 `dataset tags` 列（52 个，分属五种模态），猜不出来；没有挂载权限时申请仍然只在网页端，但 `dataset applications` 能读到申请走到哪一步。

</details>

<details>
<summary><b>🗂️ 项目（Project）</b> —— 归属、负责人、预算与平台优先级</summary>

`inspire project list / detail / owners`：项目是**全局对象，不按 Workspace 划分**，所以这一组都不接 `--workspace`。`list` 给出可见候选和显示预算，`detail <名字>` 看单个项目的预算 / 点券 / 平台优先级字段，`owners` 给出「负责人」下拉框的内容——需要权限时知道该找谁。

判断本仓库归属哪个 Project 只能由用户指认，不从列表猜；确认后写进 `./.inspire/` 与 `INSPIRE.md`，见 [`references/project-context.md`](references/project-context.md)。日常算力决策先看 `<workload> quota` 和实时余量，项目预算通常不是第一约束。

</details>

<details>
<summary><b>👤 权限</b> —— 提交前先确认自己有没有这个动作的权限</summary>

`inspire account permissions --workspace <workspace>`：看清当前账号在某 Workspace 下实际授予的权限码（`job.trainingJob.create` 等），提交前先确认自己有没有这个动作的权限。

</details>

<details>
<summary><b>🗝 多账号（一账号一目录）</b> —— 切账号 = 改一个文件</summary>

`inspire account add / list / use / rename / current / remove / check / context / permissions`：每个账号的 `config.toml`、SSH Tunnel Bridges 和登录缓存都在独立目录 `~/.inspire/accounts/<name>/`，活动账号由 `~/.inspire/current` 一行决定。`account check` 一次核对配置、登录和项目上下文，`account context` 列出当前账号能用的全部资源名。

不再有 `[accounts."<user>"]` 合并层、不再有多个环境变量的优先级链；切账号 = 改一个文件。Notebook 连接类命令的 `--account <name>` 使用本地 Account Alias，不是平台登录用户名；`all` 是跨账号扫描 Selector。

</details>

---

# 支持的 Agent Harness

不同 Harness 的后台唤醒、Skills 实现和 MCP 能力会有差异；InspireSkill 的安装器负责把同一套 `SKILL.md` / `references/` 放到各自约定目录，用户继续使用自己习惯的 Agent 入口。

| Harness | 安装后位置 | 备注 |
| --- | --- | --- |
| [Claude Code](https://claude.com/claude-code) | `~/.claude/skills/inspire/` | 用户级 Skills 层，跨项目可用 |
| [Codex CLI](https://github.com/openai/codex) | `~/.codex/skills/inspire/` | 额外生成 `agents/openai.yaml` |
| [Antigravity](https://antigravity.google/docs/skills) | `~/.gemini/config/skills/inspire/` | 用户级 Global Skills 层，跨项目可用 |
| [Cursor](https://cursor.com/docs/skills) | `~/.cursor/skills/inspire/` | 用户级 Global Skills 层，跨项目可用 |
| [OpenClaw](https://github.com/openclaw/openclaw) | `~/.openclaw/skills/inspire/` | 全局 Managed Skills 层；Workspace 层（`~/.openclaw/workspace/skills/`）可覆盖 |
| [OpenCode](https://github.com/anomalyco/opencode) | `~/.config/opencode/skills/inspire/` | 遵循 XDG；`$OPENCODE_CONFIG_DIR` 可改根 |
| [Qoder CLI](https://docs.qoder.com/en/cli/Skills) | `~/.qoder/skills/inspire/` | 用户级 Skills 层，跨项目可用 |
| [Qoder Work](https://qoder.com/product/qoderwork) | `~/.qoderwork/skills/inspire/` | 用户级 Skills 层，跨项目可用 |
| [Kimi Code](https://github.com/MoonshotAI/kimi-code) | `$KIMI_CODE_HOME/skills/inspire/`（默认 `~/.kimi-code/skills/inspire/`） | 用户级 Skills 层，跨项目可用 |
| [Kimi Desktop](https://www.kimi.com/) | `~/Library/Application Support/kimi-desktop/daimon-share/daimon/skills/inspire/` | macOS 桌面端共享 Skills 目录 |

---

# 通用 Skill 与项目资产合同

`SKILL.md` 装完是一份通用 Playbook。日常 Workspace 基本就是 `CPU资源空间` 和 `分布式训练空间`；资源条件不要写成隐式默认值，把 `workspace`、`project`、`group`、`quota` 和 `image` 组合成 Workload Profile，并在 `inspire notebook/job/hpc/... create --profile <name>` 或 Batch 文件里显式使用。

`INSPIRE.md` 不是所有仓库必备的文件。只有某个具体科研或工程项目在启智上维护稳定拓扑、Canonical Remote Paths、永久基础设施或 Image / Model / Dataset / Checkpoint 等持久资产时，才在该项目工作区根维护 `INSPIRE.md`。CLI、Skill、文档和其它通用工具源码仓库不应为了“使用了启智”而创建它；初始化问询、字段与生命周期边界见 [`references/project-context.md`](references/project-context.md)。

需要定制 Harness 级入口时，直接编辑 `~/.claude/skills/inspire/SKILL.md` 和同目录 `references/`（Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder CLI / Qoder Work / Kimi Code / Kimi Desktop 同理）。`inspire update` 默认会覆盖 `SKILL.md` 和 `references/`；维护本地改动后用 `inspire update --cli-only` 只升级 CLI 与运行时。

---

# 🔧 维护承诺

启智平台的调度语义、资源组划分、镜像可用性会频繁变化。InspireSkill 的维护目标是让 CLI 和使用手册始终贴近平台真实行为。

维护者 [@realZillionX](https://github.com/realZillionX) 会高频率、持续跟进上游变更。每次发版后，任意 `inspire <subcommand>` 都会在 stderr 提醒一行，跑 `inspire update` 即升（用法见上面[更新](#更新)段）。

发现新的平台行为差异时，在 [Issue Tracker](https://github.com/realZillionX/InspireSkill/issues) 开一条，附 `inspire --debug <cmd>` 的 Trace（CLI 会自动脱敏敏感登录凭据和代理信息）。反馈流程的更多细节见下方“开发与贡献”一节。

---

# 代理配置

不常驻 SII 的科研人员通常需要让本机代理转发 `*.sii.edu.cn` 流量；能直连 SII 校园网的人可以走 `DIRECT`。Clash Verge Mixed Port 的 SII Proxy / DIRECT 分流模板和验证步骤见 [`references/setup/sii-proxy.md`](references/setup/sii-proxy.md)；账号级 proxy、Shell proxy 与 `NO_PROXY` 诊断见 [`references/setup/install-and-config.md`](references/setup/install-and-config.md)。CLI 本身不绑定固定端口。

> 凭据（Host / User / Password）**从实验室或组织管理员获取**，不要提交到任何公开仓库或聊天记录。

---

# 开发与贡献

项目由 [@realZillionX](https://github.com/realZillionX) 维护，节奏与启智平台的行为 / 调度语义紧密绑定。为了让上游变更能被最快、最一致地消化进 CLI、`SKILL.md` 和 `references/`，贡献入口按变更风险分层：

- 欢迎小而清楚的 PR。文档修正、使用手册补丁、平台行为变化修复、可复现的小型 CLI Bugfix 都可以直接提 PR；长期协作者（如 [@JingYiJun](https://github.com/JingYiJun)）持续跟进平台变化，相关 PR 通过基础验证和 Review 后可按快速通道合入。
- 大范围语义调整先提 [Issue](https://github.com/realZillionX/InspireSkill/issues)。平台语义变化快，涉及 Workflow 重写、配置边界、调度策略或多命令联动的改动，先用 Issue 描述问题场景，附上 `inspire --debug <cmd>` 的日志最好（CLI 会自动脱敏敏感登录凭据和代理信息）。维护者会评估后纳入后续版本，通常几天内发新版。
- 新的平台行为差异同样走 Issue；不用自己附敏感本地文件，维护者会用仓库内的开发工具复现。

这么安排的权衡：这个 Skill 的价值在于与上游保持零漂移的同步。Issue 是最高效的问题信号，PR 是可落地 Patch 的通道；能小步合并的就小步合并，需要统一调度的就先收敛语义再动手。

---

# 文档索引

- [`SKILL.md`](SKILL.md)：日常使用入口，包含平台不变量、项目上下文约定、最短执行闭环和按需加载索引。
- [`references/setup/install-and-config.md`](references/setup/install-and-config.md)：安装、更新、账号配置、账号初始化和多账号操作。
- [`references/setup/sii-proxy.md`](references/setup/sii-proxy.md)：Clash Verge 的 SII Proxy / DIRECT 分流模板和验证步骤。
- [`references/project-context.md`](references/project-context.md)：项目初始化问询（Project / Workspace / Paths / Image）、`INSPIRE.md` 资产合同和项目信息持续维护。
- [`references/resources.md`](references/resources.md)：Workspace、Compute Group、规格三元组、实时资源和 Workload Profile 边界。
- [`references/paths.md`](references/paths.md)：共享盘作用域、存储池、挂载隔离、Path Alias 和远端路径边界。
- [`references/dataset.md`](references/dataset.md)：数据广场检索、官方数据集的版本与访问权限、`--dataset` 只读挂载语义。
- [`references/internal-sources.md`](references/internal-sources.md)：联网准备动线、SII 内部源入口和镜像固化策略。
- [`references/notebook.md`](references/notebook.md)：Notebook 作为交互工作台、连接方式、文件流转、Proxy 和观察边界。
- [`references/compute-workloads.md`](references/compute-workloads.md)：GPU Job、CPU HPC、Ray、Serving、TensorBoard 的适用边界、调度语义和观察闭环。
- [`references/workflows.md`](references/workflows.md)：CPU 准备、数据处理、分布式训练三阶段项目流程。
- [`references/image.md`](references/image.md)：镜像职责、保存 / 注册边界、可见性和清理原则。
- [`references/model.md`](references/model.md)：Model Registry 与 Serving 的职责边界、注册限制和版本判断。
- [`references/dev/browser-api.md`](references/dev/browser-api.md)：CLI 维护参考，唯一一份接口文档——请求契约与信封、认证与 Session、分页与 scoping、探针方法、13 条路由 115 个 Action 的逐条参数与响应表、创建面字段合同、数据广场（`aip.sii.edu.cn`）与变更验收。
- [`CONTRIBUTING.md`](CONTRIBUTING.md)：开发、测试和贡献约定。
- [`cli/`](cli/)：CLI 源码；入口 `cli/inspire/cli/main.py`。
- [`scripts/install.sh`](scripts/install.sh)：Curl Pipe Bash 安装器。
- [`scripts/scan_v2_surface.py`](scripts/scan_v2_surface.py)：CLI 维护工具，把控制台前端产物里写死的 `/api/v2` 接口面抓出来和 `discovery` 对账，`--probe` 逐个探活。

---

# License

[`LICENSE`](LICENSE)（MIT）

# Acknowledgements

- 启智平台团队提供的公开资料与协助。
- [EmbodiedForge/Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) 提供了 CLI 的初步框架。

<p align="center"><sub>Made for researchers who'd rather think than click.</sub></p>
