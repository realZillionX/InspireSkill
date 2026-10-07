<p align="center"> <img src="https://raw.githubusercontent.com/realZillionX/InspireSkill/main/assets/hero.svg" width="100%" alt="Inspire Skill: the Agent-Native cockpit for the Inspire compute platform"/> </p>

<p align="center"> <b>让 AI Agent 直接在本地 CLI 里完成启智平台的全部操作。</b><br/> </p>

<p align="center"> <a href="https://github.com/realZillionX/InspireSkill/tree/main/cli"><img src="https://img.shields.io/badge/CLI-bundled-3366FF?style=for-the-badge" alt="CLI bundled"/></a> <img src="https://img.shields.io/badge/Harness-Codex%20/%20Claude%20Code%20/%20Cursor%20/%20OpenCode%20/%20ZCode%20/%20Kimi%20Code%20/%20Kimi%20Work%20/%20Qoder%20/%20Qoder%20Work%20/%20Antigravity%20/%20OpenClaw%20/%20Pi-5566FF?style=for-the-badge" alt="Harnesses"/> <img src="https://img.shields.io/badge/status-actively%20maintained-22CCEE?style=for-the-badge" alt="Actively maintained"/> <img src="https://img.shields.io/badge/license-MIT-0f172a?style=for-the-badge" alt="License MIT"/> </p>

---

# 本项目建立的意义

在本项目开始筹办之初，对于所有 SII 的学生，[启智平台](https://qz.sii.edu.cn)是科研实验链路里最慢的那一环：每次申请资源、新建 Notebook、新建训练任务、同步代码都要反复点点点，SSH 等更进一步的功能更是遥遥无期。

本着过渡到大 Agent 时代、将一切重复性机械工作交给 Agent 的初衷，我们创办了 InspireSkill 项目，旨在将启智平台 GUI 打平为 CLI，并建立了 CLI + Skill 的一体化系统，让 InspireSkill 成为所有 Agent 开箱即用的工具、让你的 Codex / Claude Code / Cursor / OpenCode / ZCode / Kimi Code / Kimi Work / Qoder / Qoder Work / Antigravity / OpenClaw 成为进行科研工作的唯一入口。

建立和维护本项目的过程并非易事，InspireSkill 也并非只是将[启智平台](https://qz.sii.edu.cn)的网页 API 打平重构为 CLI 的简单工作，在维护本项目的过程中，设计高于平台语义的高层功能、寻找启智平台中细枝末节的 API 并将其优雅融入 CLI 系统中、尤其是维护一个易于 Agent 阅读且包含平台所有特性的文档系统都给我们带来了不小于 CLI 本身的麻烦。

在长时间的开发与维护中，以 [@realZillionX](https://github.com/realZillionX) 和 [@JingYiJun](https://github.com/JingYiJun) 为首的开发团队始终秉持着注重细节与优雅的开发者精神，最终构建出一个令人满意的项目。时至今日，我们可以自豪地说：**InspireSkill 所包含的功能，只有你想不到，没有我们做不到**。它们包括但不限于：对 HDD / SSD / QB-ILM 等项目路径的优雅维护、翻转镜像的可见范围、将平台内部源入口交给 Agent（从而使在不可上网区配置镜像成为可能）、联网 Notebook 的 SSH 板块、受限 Notebook 的 JupyterTerminal 执行路径、空闲 8 卡整节点总量的查询、低优任务占用总量的查询、将 Notebook / 训练任务的资源视图 / 事件 / 聚合日志交给 Agent。

# 对初次使用者的简单介绍

InspireSkill 是启智平台的本地 Agent 驾驶舱。你继续在熟悉的 Codex、Claude Code、Cursor 等 Harness 中写代码、看 Git 状态、调用其他工具；启智算力由 Agent 通过同一套入口调度。它由三部分组成：

- **`inspire` CLI** 把资源查询、Notebook 连接、GPU Job / HPC / Ray 提交、Serving 部署、TensorBoard 曲线读取、资产管理和清理变成可串联的命令。资源用可读 Name 和 Account Alias 寻址；需要脚本消费时使用 JSON 输出。
- **`SKILL.md` 与 `references/`** 告诉 Agent 何时选哪类工作负载、怎样从实时目录选调度条件、怎样观察任务并核验产物。安装器把它们放到受支持的 Harness 目录，用户不必在每次对话里重讲平台语义。
- **Python SDK** 与 CLI 使用同一个安装包，提供同步和异步客户端，让研究脚本、服务和 Agent runtime 直接调用启智能力。

给 Agent 一个目标，它就能先查当前可用的 Workspace、Quota 和镜像，准备共享盘上的代码与数据，提交合适的任务，再沿 Events、Logs、Metrics 和 Instances 追踪到结果，最后清理不再需要的资源。整个流程留在你的本地代码仓库和现有 Agent 中；命令可复现，稳定资产可按需记入 `INSPIRE.md`。

**从选资源到验收结果，InspireSkill 让 Agent 真正掌控启智。**

## 为什么不直接使用 QWorks？

启智推出的 [QWorks](https://qworks.tech/solutions/sii) 是下载到本机的独立 Harness，但它把用户限定在自己的 Agent 入口。已经在使用 Codex、Claude Code、Cursor 等客户端及其订阅套餐的人，无法直接把这些套餐带进 QWorks；接入其它模型仍需走 API。InspireSkill 把启智能力送进你已经选好的 Agent，无需为启智再迁移一次工作台。

| 维度 | QWorks | InspireSkill |
| --- | --- | --- |
| Agent 入口 | QWorks 自带的单一 Harness | 安装到 Codex / Claude Code / Cursor / OpenCode 等受支持的 Harness，也可直接使用 CLI |
| 模型与套餐 | 其它 AI 客户端的订阅套餐不能直接复用，外部模型按 API 接入 | 继续使用原 Harness 已有的模型配置和套餐；InspireSkill 不接管模型计费 |
| 启智操作 | 在 QWorks 内连接启智模型、工作界面与远程计算环境 | 在本地用命令覆盖资源发现、Notebook、GPU Job、HPC、Ray、Serving、TensorBoard 和资产管理 |
| 工作流 | 围绕 QWorks 会话与界面 | 本地 Repo、Git、其他工具与启智命令处于同一工作流；命令可脚本化，JSON 输出可接入自动化 |

**你已经有顺手的 Agent，就不必为了使用启智再换一个。**

---

# 快速上手

支持 macOS、Linux 和 Windows；CI 覆盖三个平台。命令组与参数以 `inspire --help` 和各子命令的 `--help` 为准。

## 安装

### macOS / Linux

需要 `bash`、`curl`、`tar`、Python 3.10+，以及 `uv`（推荐）或 `pipx`。尚未安装 `uv` 时先运行第一行：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

安装器从 PyPI 安装 CLI，并把 Skill 放入检测到的 Harness。可用 `bash -s -- --harness codex,claude` 指定目标；`--no-cli` 只装 Skill，`--no-schedule` 跳过 macOS 每日版本检查。

### Windows

需要 Python 3.10+、`uv`（推荐）或 `pipx`，以及系统 OpenSSH 客户端；不需要 WSL。在仓库根目录运行 `install.ps1`：

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

脚本自动检测 Harness；`-SkipPlaywright` 跳过浏览器运行时（浏览器登录不可用），`-SkipSkill` 只装 CLI，`-Version <x.y.z>` 安装指定版本。安装后运行 `inspire --version`、`inspire --help` 确认入口可用。

## 账号与初始化

账号配置与当前仓库无关，`<name>` 是本地 Alias。`account add` 会询问平台登录名、密码、地址及代理；登录名是平台接受的手机号、学号或邮箱，配置保存在 `~/.inspire/accounts/<name>/config.toml`。未在账号文件中保存密码时，可用 `INSPIRE_PASSWORD` 补充。

```bash
inspire account add <name>
inspire account check
inspire init
inspire resources availability --workspace 分布式训练空间 --include-cpu
```

`inspire init` 校验并规范化账号配置，不绑定仓库、Project 或资源。创建工作负载时显式选择 Workspace、Project、计算组、Quota、镜像和远端路径。需要从校园网外访问平台时，先按下文[代理配置](#代理配置)配置连接。

多账号可用 `inspire account use <name>` 设置默认账号、`inspire account current` 查看默认值、`inspire account rename <old> <new>` 修改本地 Alias。所有命令可用 `--account <name>` 临时选择账号，例如 `inspire --account <name> notebook list --workspace CPU资源空间`；这不会改变默认账号，切换也不会清除各账号的 Session、SSH 连接和资源缓存。

## 更新与卸载

```bash
inspire update                  # 升级 CLI 并刷新 Skill
inspire update --check          # 只检查
inspire update --cli-only       # 仅升级 CLI 与浏览器运行时
inspire update --skill-only     # 仅刷新 Skill 与 references/
inspire uninstall               # 卸载 CLI 与安装器管理的 Skill
```

`update` 自动识别 `uv tool` / `pipx`，并在遇到旧版遗留的本地状态时先列清单、再询问是否清理。卸载前也会列出目标并确认；账号配置和共享的 Playwright 浏览器缓存默认保留，分别用 `inspire uninstall --purge` 和 `inspire uninstall --purge-runtime` 删除，用户维护的 `INSPIRE.md` 不受影响。CLI 无法运行时，可用安装脚本的 `--uninstall` 兜底。

## Windows 原生注意事项

- `Get-Command ssh -All` 确认实际使用的 `ssh.exe`；系统 OpenSSH 与 Git for Windows 的 ProxyCommand 处理方式不同。
- PowerShell 5.1 的 `>>` 会写出 OpenSSH 无法读取的 UTF-16LE。追加 SSH 配置时用 `inspire notebook ssh-config <notebook> | Out-File -Encoding utf8 -Append $env:USERPROFILE\.ssh\config`；PowerShell 7+ 可直接用 `>>`。
- Windows OpenSSH 会拒绝权限过宽的私钥；遇到 `UNPROTECTED PRIVATE KEY FILE` 时收紧 `%USERPROFILE%\.ssh` 的 ACL。`rsync` 不是必需项，文件传输可用 `inspire notebook scp`。

---

# 能力一览

按能力域折叠，点开你关心的那一个。命令组、子命令、参数和默认值一律以 `inspire <group> <subcommand> --help` 为准。

<details>
<summary><b>📝 Notebook 统一入口</b> —— 交互工作台、连接、文件流转、把跑通的环境固化成镜像</summary>

全链路命令化：`create / batch / list / status / start / stop / delete / ssh / ssh-config / ssh-proxy / connection / exec / shell / scp / install-deps / proxy-url / path / quota / profile / metrics / events / lifecycle / save-image / cancel-save-image`。容器里部署好的服务用 `proxy-url --port` 拿到外部地址直接请求；`ssh-proxy` 是给 OpenSSH `ProxyCommand` 用的裸流转发，`ssh-config` 生成的配置里就指向它。

把跑通的环境固化成镜像是 Notebook 自己的生命周期事件：`save-image` 会先报平台估算的快照体积（`--dry-run` 只估不存），保存期间该 Notebook 不可操作，中途要拿回来用 `cancel-save-image`——已经打出「等待推送」之后取消仍然生效。默认是在起点镜像上追加增量层，反复迭代会持续累积层数；`--flatten` 把结果合并成单层，适合固化基底或在多轮迭代后收敛镜像。估算值、最终体积和构建耗时随基底与改动内容变化，以本次命令和镜像状态为准。

显卡不是 `H100` / `H200` 的 Notebook 可使用 OpenSSH / SCP / SSH Config；`H100` / `H200` 受限 Notebook 使用 JupyterTerminal 执行命令，文件流转以 `/inspire/...` 共享路径为边界，并通过支持 SSH 的 Notebook 使用 `notebook scp` 或外部 `rsync` 完成本地上传/下载。连接类命令只解析所选账号的 Notebook Connection；传 `--account <name>` 可使用其他账号，无需切换默认账号。

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

`resources availability --workspace <name> --include-cpu` / `resources usage --workspace <name>` / `resources policy --workspace <name>` / `<workload> quota --workspace <name>`：定位一个 Workspace 里哪个计算组有空，支持透支式申请。`<workload> quota` 回答「有哪些合法档位」，`availability` 同时给出「保障额度还剩多少、高优任务连可抢占卡一起能拿多少」以及 `Free Nodes` / `High Pri Nodes` 的 8 卡整节点容量；`Free Nodes` 是当前完全空闲，`High Pri Nodes` 再加上只被低优任务占用、清退后可用的整节点。GPU 保障余额仍可能为负，但整节点容量与 `Idle GPUs` 不会混入该余额。`usage` 回答「项目配额被哪些 user/task 使用」——默认按 `Project → User` 归因，`--details` 展开任务，`--project` / `--user` / `--task` 负责收窄；`--group <关键词>` 把判断收窄到任务真正提交进去的计算组。`policy` 回答「拿到手能留多久——空闲多久被回收、有没有运行时长上限」。

`<workload> quota` 的 `Priority` 列给出每一行接受的任务优先级，创建时 CLI 会据此预检，不用等平台拒绝；`Points/h` 列给出该行每实例每小时的 Live 点券成本。优先级限制和价格会随 Workspace、Compute Group、硬件与平台策略变化，不在文档里固化某个目录当时的数值。

这些命令一律一次只看一个 Workspace——档位、余量、回收策略和占用都是按 Workspace 定义的事实，跨空间扫一遍答不出任何一个可执行的决定；还接受 `--workspace all` 的只剩「按名字找东西」那一类（`<workload> list` / `account permissions`），因为不知道东西在哪个空间时本来就给不出空间名。

机器本身发生了什么是另一层：`resources node-events <节点名>` 是平台上唯一按节点而不是按工作负载组织的事件源，内核 OOM kill、Cordon / Uncordon、重启、`NodeNotSchedulable` 都在这里；命令倒序读取最新 1000 行再恢复时间顺序，不会因为分页预算只看到旧事件。「同一台机器上反复失败」此前在 CLI 里无处可查。

余量和规格始终读 Live 数据；`inspire cache status / refresh / clear` 管的是本地加速缓存——Name 解析索引、Quota 目录和 Notebook 显卡型号，三条命令都支持 `--resource <kind>` 分类操作。缓存按需读穿、写穿：命中直接在本地解析，miss 或过期才针对当前名字回源，创建/删除后立即更新；普通命令不会在后台扫描所有 Workspace。正常情况下 `refresh` 根本不需要跑，所以它**不接受裸形式**，必须用 `--resource` / `--workspace` / `--name` 说明刷哪一块；`empty` 表示刷过、还在有效期内、却一个名字都拿不出来。

</details>

<details>
<summary><b>🗂 镜像管理</b> —— Registry 边界沿着卡的类型走，可见性有一道单向门</summary>

`image list / detail / register / set-visibility / delete`，创建 Notebook、Job、HPC、Ray 或 Serving 时显式传 `--image`；`hpc create --image-type` 明确可见性。

镜像存在 Registry 里而不是 Workspace 里，**多个 Workspace 正常共用同一份 Registry**——这一组都要 `--workspace`，因为那是平台唯一的指定 Registry 的方式，它是路标不是分区，所以 `notebook save-image --workspace X` 存出的镜像在同一个 Registry 上的每个 Workspace 里都看得到。不同硬件域或专属空间也可能指向互不相交的 Registry；用目标 Workspace 的 Live `image list --keyword` 搜索，不维护静态映射。`--source all` 会并发读取四个可见性目录，再按固定页签顺序合并。

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

确认后在 `notebook / job / hpc create` 上用 `--dataset <数据集名>:<版本名>` 只读挂载到 `/inspire/dataset/<数据集名>/<版本名>`，创建前平台逐条校验，不会先建出一个缺数据的 Workload。数据集用名字寻址，数据广场内部的数字 ID 拿去挂载会被拒。`--tag` 认的是目录当前返回的中文标签，全量用 `dataset tags` 列，猜不出来；没有挂载权限时申请仍然只在网页端，但 `dataset applications` 能读到申请走到哪一步。

</details>

<details>
<summary><b>🗂️ 项目（Project）</b> —— 归属、负责人、预算与平台优先级</summary>

`inspire project list / detail / owners`：项目是**全局对象，不按 Workspace 划分**，所以这一组都不接 `--workspace`。`list` 给出可见候选和显示预算，`detail <名字>` 看单个项目的预算 / 点券 / 平台优先级字段，`owners` 给出「负责人」下拉框的内容——需要权限时知道该找谁。

仓库不绑定 Project。每次操作根据当前任务显式传入 Project；如果有跨会话复用的稳定资产，在 `INSPIRE.md` 的每个条目上单独标明所属 Project / Workspace，见 [`references/assets.md`](references/assets.md)。

</details>

<details>
<summary><b>👤 权限</b> —— 提交前先确认自己有没有这个动作的权限</summary>

`inspire account permissions --workspace <workspace>`：看清当前账号在某 Workspace 下实际授予的权限码（`job.trainingJob.create` 等），提交前先确认自己有没有这个动作的权限。

</details>

<details>
<summary><b>🗝 多账号（一账号一目录）</b> —— 切账号 = 改一个文件</summary>

`inspire account add / list / use / rename / current / remove / check / context / permissions`：每个账号的 `config.toml`、SSH Tunnel Bridges 和登录缓存都在独立目录 `~/.inspire/accounts/<name>/`，活动账号由 `~/.inspire/current` 一行决定。`account check` 核对账号配置和登录，`account context` 列出当前账号能用的全部资源名。

`account use <name>` 设置持久默认账号；所有命令的 `--account <name>` 使用本地 Account Alias，只覆盖本次命令，不改默认值。不传参数就使用默认账号，命令执行中不受其他进程切换默认账号影响。切换时保留各账号的 Session、SSH Connection、资源索引和代理缓存，切回后继续复用。

</details>

---

# Python SDK：让启智能力进入你的程序

InspireSkill 还提供稳定可用的 Python SDK，随 `inspire-skill` 一起安装。`InspireClient` 面向同步脚本，`InspireAsyncClient` 面向 asyncio 应用；两者与 CLI 共用账号、平台服务和资源模型，覆盖 Workspace、Project、Image、Dataset、Model、Notebook、GPU Job、HPC、Ray、Serving 和 TensorBoard。你可以把启智操作直接写进研究程序或服务，而不必解析终端输出。

```python
from inspire import InspireClient

with InspireClient(account="research") as client:
    client.login()
    for job in client.jobs.list("分布式训练空间", limit=10).items:
        print(job.name, job.status)
```

异步客户端使用 `async with` 和 `await`，支持并发查询、事件与日志迭代以及远程执行流；`Accounts` 提供独立的本地账号管理入口。完整的接口、资源引用和调用示例见 [Python SDK 文档](references/sdk.md)。

---

# 支持的 Agent Harness

不同 Harness 的后台唤醒、Skills 实现和 MCP 能力会有差异；InspireSkill 的安装器负责把同一套 `SKILL.md` / `references/` 放到各自约定目录，用户继续使用自己习惯的 Agent 入口。

| Harness | 安装后位置 | 备注 |
| --- | --- | --- |
| [Codex CLI](https://github.com/openai/codex) | `~/.codex/skills/inspire/` | 额外生成 `agents/openai.yaml` |
| [Claude Code](https://claude.com/claude-code) | `~/.claude/skills/inspire/` | 用户级 Skills 层，跨项目可用 |
| [Cursor](https://cursor.com/docs/skills) | `~/.cursor/skills/inspire/` | 用户级 Global Skills 层，跨项目可用 |
| [OpenCode](https://github.com/anomalyco/opencode) | `~/.config/opencode/skills/inspire/` | 遵循 XDG；`$OPENCODE_CONFIG_DIR` 可改根 |
| [ZCode](https://zcode.z.ai/) | `~/.zcode/skills/inspire/` | 用户级 Skills 层，跨项目可用 |
| [Kimi Code](https://github.com/MoonshotAI/kimi-code) | `$KIMI_CODE_HOME/skills/inspire/`（默认 `~/.kimi-code/skills/inspire/`） | 用户级 Skills 层，跨项目可用 |
| [Kimi Work](https://www.kimi.com/) | `~/Library/Application Support/kimi-desktop/daimon-share/daimon/skills/inspire/` | macOS 桌面端共享 Skills 目录 |
| [Qoder](https://qoder.com/) | `~/.qoder/skills/inspire/` | 用户级 Skills 层，跨项目可用 |
| [Qoder Work](https://qoder.com/product/qoderwork) | `~/.qoderwork/skills/inspire/` | 用户级 Skills 层，跨项目可用 |
| [Antigravity](https://antigravity.google/docs/skills) | `~/.gemini/config/skills/inspire/` | 用户级 Global Skills 层，跨项目可用 |
| [OpenClaw](https://github.com/openclaw/openclaw) | `~/.openclaw/skills/inspire/` | 全局 Managed Skills 层；Workspace 层（`~/.openclaw/workspace/skills/`）可覆盖 |
| [Pi](https://github.com/earendil-works/pi) | `~/.pi/agent/skills/inspire/` | 用户级 Skills 层，跨项目可用；Pi 另支持共享的 `~/.agents/skills/`，本安装器使用 Pi 私有目录 |

---

# 通用 Skill 与项目资产合同

`SKILL.md` 装完是一份通用 Playbook。日常 Workspace 基本就是 `CPU资源空间` 和 `分布式训练空间`；`workspace`、`project`、`group`、`quota` 和 `image` 每次创建都显式传入，Batch 中每个展开条目也同样显式提供。

`INSPIRE.md` 不是所有仓库必备的文件。只有仓库在启智上维护需要跨 Agent / 会话复用的稳定路径、永久基础设施或 Image / Model / Dataset / Checkpoint 等持久资产时才创建；每项资产可分别属于不同 Project / Workspace。边界见 [`references/assets.md`](references/assets.md)。

需要定制 Harness 级入口时，直接编辑 `~/.claude/skills/inspire/SKILL.md` 和同目录 `references/`（Codex / Cursor / OpenCode / ZCode / Kimi Code / Kimi Work / Qoder / Qoder Work / Antigravity / OpenClaw 同理）。`inspire update` 默认会覆盖 `SKILL.md` 和 `references/`；维护本地改动后用 `inspire update --cli-only` 只升级 CLI 与运行时。

---

# 🔧 维护承诺

启智平台的调度语义、资源组划分、镜像可用性会频繁变化。InspireSkill 的维护目标是让 CLI 和使用手册始终贴近平台真实行为。

维护者 [@realZillionX](https://github.com/realZillionX) 会高频率、持续跟进上游变更。每次发版后，任意 `inspire <subcommand>` 都会在 stderr 提醒一行，跑 `inspire update` 即升（用法见上面[更新](#更新)段）。

发现新的平台行为差异时，在 [Issue Tracker](https://github.com/realZillionX/InspireSkill/issues) 开一条，附 `inspire --debug <cmd>` 的 Trace（CLI 会自动脱敏敏感登录凭据和代理信息）。反馈流程的更多细节见下方“开发与贡献”一节。

---

# 代理配置

校园网外访问 `*.sii.edu.cn` 时，可在 Clash Verge 建一个 `SII Proxy` 选择组，放入组织提供的代理节点和 `DIRECT`，并把规则 `DOMAIN-SUFFIX,sii.edu.cn,SII Proxy` 放在通用规则之前；能直连校园网时选择 `DIRECT`。其他网站继续走原有规则。CLI 不绑定固定端口，把账号 proxy 配为本机实际的 Mixed Port，例如 `http://127.0.0.1:<mixed-port>`。

如果使用启智官方提供的 aTrust VPN，可以参考 [Docker-aTrust](https://github.com/realZillionX/Docker-aTrust) 将 aTrust 运行在 Docker 中，取得宿主机上的 SOCKS5 / HTTP 代理端口，再将该端口接入上述分流方案。

账号 proxy 优先于通用 Shell 代理；未设置账号 proxy 时，`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 及小写变量仍可能影响连接，需要直连时可用 `NO_PROXY=.sii.edu.cn` 绕过通用代理。用 `inspire account check --details` 查看实际代理来源、路由及 `NO_PROXY` 匹配结果。

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

- [`SKILL.md`](SKILL.md)：日常使用入口，包含平台不变量、资产合同边界、最短执行闭环和按需加载索引。
- [`references/assets.md`](references/assets.md)：`INSPIRE.md` 持久资产合同和生命周期。
- [`references/resources.md`](references/resources.md)：Workspace、Compute Group、规格三元组和实时资源。
- [`references/paths.md`](references/paths.md)：共享盘作用域、存储池、挂载隔离和远端绝对路径。
- [`references/dataset.md`](references/dataset.md)：数据广场检索、官方数据集的版本与访问权限、`--dataset` 只读挂载语义。
- [`references/internal-sources.md`](references/internal-sources.md)：联网准备动线、SII 内部源入口和镜像固化策略。
- [`references/notebook.md`](references/notebook.md)：Notebook 作为交互工作台、连接方式、文件流转、Proxy 和观察边界。
- [`references/compute-workloads.md`](references/compute-workloads.md)：GPU Job、CPU HPC、Ray、Serving、TensorBoard 的适用边界、调度语义和观察闭环。
- [`references/workflows.md`](references/workflows.md)：CPU 准备、数据处理、分布式训练三阶段项目流程。
- [`references/image.md`](references/image.md)：镜像职责、保存 / 注册边界、可见性和清理原则。
- [`references/model.md`](references/model.md)：Model Registry 与 Serving 的职责边界、注册限制和版本判断。
- [`references/sdk.md`](references/sdk.md)：Python SDK 的同步、异步客户端与资源接口。
- [`references/dev/browser-api.md`](references/dev/browser-api.md)：CLI 维护参考，唯一一份接口文档——请求契约与信封、认证与 Session、分页与 scoping、当前 CLI 使用的 Action 参数与响应表、创建面字段合同、数据广场（`aip.sii.edu.cn`）与变更验收。
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
