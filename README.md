<p align="center"> <img src="https://raw.githubusercontent.com/realZillionX/InspireSkill/main/assets/hero.svg" width="100%" alt="Inspire Skill: the Agent-Native cockpit for the Inspire compute platform"/> </p>

<p align="center"> <b>让 AI Agent 直接在本地 CLI 里完成启智平台的全部操作。</b><br/> </p>

<p align="center"> <a href="https://github.com/realZillionX/InspireSkill/tree/main/cli"><img src="https://img.shields.io/badge/CLI-bundled-3366FF?style=for-the-badge" alt="CLI bundled"/></a> <img src="https://img.shields.io/badge/Harness-Codex%20/%20Claude%20Code%20/%20Cursor%20/%20OpenCode%20/%20ZCode%20/%20Kimi%20Code%20/%20Kimi%20Work%20/%20Qoder%20/%20Qoder%20Work%20/%20Antigravity%20/%20OpenClaw%20/%20Pi%20/%20DeepSeek%20Harness-5566FF?style=for-the-badge" alt="Harnesses"/> <img src="https://img.shields.io/badge/status-actively%20maintained-22CCEE?style=for-the-badge" alt="Actively maintained"/> <img src="https://img.shields.io/badge/license-MIT-0f172a?style=for-the-badge" alt="License MIT"/> </p>

---

# 本项目建立的意义

在本项目开始筹办之初，对于所有 SII 的学生，[启智平台](https://qz.sii.edu.cn)是科研实验链路里最慢的那一环：每次申请资源、新建 Notebook、新建训练任务、同步代码都要反复点点点，SSH 等更进一步的功能更是遥遥无期。

本着过渡到大 Agent 时代、将一切重复性机械工作交给 Agent 的初衷，我们创办了 InspireSkill 项目，旨在将启智平台 GUI 打平为 CLI，并建立了 CLI + Skill 的一体化系统，让 InspireSkill 成为所有 Agent 开箱即用的工具、让你的 Codex / Claude Code / Cursor / OpenCode / ZCode / Kimi Code / Kimi Work / Qoder / Qoder Work / Antigravity / OpenClaw / Pi / DeepSeek Harness 成为进行科研工作的唯一入口。

建立和维护本项目的过程并非易事，InspireSkill 也并非只是将[启智平台](https://qz.sii.edu.cn)的网页 API 打平重构为 CLI 的简单工作，在维护本项目的过程中，设计高于平台语义的高层功能、寻找启智平台中细枝末节的 API 并将其优雅融入 CLI 系统中、尤其是维护一个易于 Agent 阅读且包含平台所有特性的文档系统都给我们带来了不小于 CLI 本身的麻烦。

在长时间的开发与维护中，以 [@realZillionX](https://github.com/realZillionX) 和 [@JingYiJun](https://github.com/JingYiJun) 为首的开发团队始终秉持着注重细节与优雅的开发者精神，最终构建出一个令人满意的项目。时至今日，我们可以自豪地说：**InspireSkill 所包含的功能，只有你想不到，没有我们做不到**。它们包括但不限于：对 HDD / SSD / QB-ILM 等项目路径的优雅维护、翻转镜像的可见范围、将平台内部源入口交给 Agent（从而使在不可上网区配置镜像成为可能）、联网 Notebook 的 SSH 板块、受限 Notebook 的 JupyterTerminal 执行路径、空闲 8 卡整节点总量的查询、低优任务占用总量的查询、将 Notebook / 训练任务的资源视图 / 事件 / 聚合日志交给 Agent。

# 对初次使用者的简单介绍

InspireSkill 是启智平台的本地 Agent 工具套件。你可以继续在熟悉的 Codex、Claude Code、Cursor 等客户端中写代码、管 Git，同时让 Agent 直接通过命令行调度启智算力。项目包含三部分：

- **`inspire` CLI**：把资源查询、Notebook 连接、GPU Job / HPC / Ray 提交、Serving 部署、TensorBoard 曲线读取、镜像与模型管理整合为命令行工具。所有资源直接按名称和本地账号别名寻址，支持 `--json` 结构化输出。
- **`SKILL.md` 与 `references/`**：配套的 Agent 技能手册，涵盖工作负载选型、资源与配额查询、任务观察和结果验收流程。安装脚本会自动将其部署到本机已安装的 Agent 客户端中。
- **Python SDK**：随 `inspire-skill` 包一同安装的同步（`InspireClient`）与异步（`InspireAsyncClient`）Python 客户端，方便在脚本、评测流水线或后端服务中直接调用平台接口。

在对话中告诉 Agent 你的实验目标，它就可以自动查询可用 Workspace、配额与镜像，同步共享盘代码与数据，提交训练或处理任务，并通过事件、日志、监控指标和 TensorBoard 追踪训练进展，最后清理闲置资源。

## 为什么不直接使用 QWorks？

启智推出的 [QWorks](https://qworks.tech/solutions/sii) 是独立的桌面客户端，要求用户迁移到它自带的对话界面中，也无法直接复用 Codex、Claude Code、Cursor 等工具已有的订阅套餐。InspireSkill 则以 CLI + Skill 的形式接入你现有的 Agent 工具链：

| 维度 | QWorks | InspireSkill |
| --- | --- | --- |
| Agent 入口 | 绑定 QWorks 自带客户端 | 支持 Codex / Claude Code / Cursor / OpenCode / Antigravity 等主流客户端，也可在终端直接使用 CLI |
| 模型与套餐 | 无法直接使用其他客户端的订阅套餐，外部模型需按 API 接入 | 直接沿用现有 Agent 客户端的模型配置与订阅套餐 |
| 启智操作 | 在 QWorks 界面内连接平台环境 | 通过本地 CLI 与 Python SDK 覆盖资源查询、Notebook、GPU Job、HPC、Ray、Serving、TensorBoard、镜像与数据集管理 |
| 工作流 | 围绕 QWorks 内部会话 | 与本地代码仓库、Git 及其他开发工具无缝配合，支持脚本编排与 `--json` 自动化 |

---

# 快速上手

支持 macOS、Linux 和 Windows（无需 WSL）。具体命令参数请以 `inspire --help` 和各子命令的 `--help` 为准。

## 安装

### macOS / Linux

需要 `bash`、`curl`、`tar`、Python 3.10+，以及 `uv`（推荐）或 `pipx`。如未安装 `uv`，请先执行第一行：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

安装脚本会从 PyPI 安装 `inspire-skill`，并将 `SKILL.md` 与 `references/` 写入自动检测到的 Agent 客户端目录。可通过 `bash -s -- --harness codex,claude` 指定目标客户端；`--no-cli` 仅安装 Skill 文档，`--no-schedule` 跳过 macOS 每日更新检查。

### Windows

需要 Python 3.10+、`uv`（推荐）或 `pipx`，以及系统自带的 OpenSSH 客户端。在仓库根目录运行 `install.ps1`：

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

脚本会自动检测本机 Agent 客户端；`-SkipPlaywright` 跳过浏览器运行时安装，`-SkipSkill` 仅安装 CLI，`-Version <x.y.z>` 安装指定版本。安装完成后可运行 `inspire --version` 和 `inspire --help` 验证。

## 账号与初始化

账号配置按本地别名（`<name>`）保存在 `~/.inspire/accounts/<name>/config.toml`，与具体代码仓库无关。`account add` 会引导填写平台登录名（手机号、学号或邮箱）、密码、平台地址及代理；未在配置文件中保存密码时，也可通过环境变量 `INSPIRE_PASSWORD` 传入。

```bash
inspire account add <name>
inspire account check
inspire init
inspire resources availability --workspace 分布式训练空间 --include-cpu
```

`inspire init` 用于校验并补全账号配置。在校园网外访问平台时，请先参考下文[代理配置](#代理配置)完成网络设置。

多账号管理可使用 `inspire account use <name>` 切换默认账号、`inspire account current` 查看当前默认账号、`inspire account rename <old> <new>` 重命名别名。所有命令均支持通过 `--account <name>` 临时指定单次命令使用的账号（如 `inspire --account <name> notebook list --workspace CPU资源空间`），切换账号不会清理已有的登录 Session、SSH 连接或本地缓存。

## 更新与卸载

```bash
inspire update                  # 升级 CLI 并同步最新 Skill 文档
inspire update --check          # 仅检查是否有新版本
inspire update --cli-only       # 仅升级 CLI 与浏览器运行时
inspire update --skill-only     # 仅更新 SKILL.md 与 references/
inspire uninstall               # 卸载 CLI 与已安装的 Skill 文档
```

`inspire uninstall` 默认保留 `~/.inspire` 下的账号配置和 Playwright 浏览器缓存；如需一并清除，可分别加上 `--purge` 和 `--purge-runtime`（仓库中的 `INSPIRE.md` 不会被删除）。若 CLI 环境损坏无法运行，也可使用安装脚本的 `--uninstall` 参数卸载。

## Windows 原生环境提示

- 运行 `Get-Command ssh -All` 确认实际调用的 `ssh.exe` 路径；系统 OpenSSH 与 Git for Windows 自带 OpenSSH 对 `ProxyCommand` 的处理存在差异。
- Windows PowerShell 5.1 的 `>>` 默认输出 UTF-16LE 编码，会导致 OpenSSH 无法解析配置文件。写入 SSH 配置时请使用：`inspire notebook ssh-config <notebook> | Out-File -Encoding utf8 -Append $env:USERPROFILE\.ssh\config`（PowerShell 7+ 可直接使用 `>>`）。
- 若遇到 `UNPROTECTED PRIVATE KEY FILE` 报错，请收紧 `%USERPROFILE%\.ssh` 目录的 ACL 权限。文件传输可直接使用 `inspire notebook scp`，无需额外安装 `rsync`。

---

# 能力一览

点击展开各模块说明。完整子命令、参数与示例请查看 `inspire <group> <subcommand> --help`。

<details>
<summary><b>📝 Notebook</b> —— 交互式开发、远程执行、文件传输与镜像固化</summary>

支持命令：`create / batch / list / status / start / stop / delete / ssh / ssh-config / ssh-proxy / connection / exec / shell / scp / install-deps / proxy-url / quota / metrics / events / lifecycle / save-image / cancel-save-image`。

- **远程连接与文件传输**：非 `H100` / `H200` 节点的 Notebook 支持完整的 OpenSSH、`scp` 与 `ssh-config`（可直接接入 VS Code Remote SSH）；`H100` / `H200` 受限节点会自动切换为基于 `JupyterTerminal` 的 `exec` 和 `shell`，文件上传下载可通过同项目下支持 SSH 的 CPU Notebook 经 `/inspire/...` 共享盘中转。
- **HTTP 服务代理**：容器内启动的 Web 服务（如 Gradio、FastAPI、推理服务）可通过 `notebook proxy-url <name> --port <port>` 获取外部访问地址。
- **保存为自定义镜像**：环境配置完成后，使用 `notebook save-image` 将当前运行中的容器直接固化为平台镜像（`--dry-run` 可预先估算可写层增量体积，`cancel-save-image` 可中途取消并恢复 Notebook）。默认按增量层追加；使用 `--flatten` 可将镜像压平为单层，适合清理旧层残留文件或收敛项目基底镜像。

</details>

<details>
<summary><b>🏃 GPU 后台任务（Job）</b> —— 单卡、多卡与多节点分布式训练 / 批量推理</summary>

支持命令：`inspire job create / batch / list / status / command / wait / stop / delete / events / instances / shell / logs / metrics / quota`。

- 覆盖单卡后台任务、单机多卡与多机分布式训练（多节点任务按每节点 8 卡满节点规格提交）。批量实验可使用 `job batch <file>` 提交，支持 `--dry-run` 预览完整生效配置。
- 支持 `--exclude-node` 排除故障节点、`--specified-node` 绑定指定节点（需目标 Workspace 开启对应能力）、`--enable-notification` 开启飞书状态通知，以及设置成功/失败后的容器保留时长以便事后用 `job shell` 进入容器排查。
- 使用 `job command` 可随时查回任务提交时的完整启动命令，`job wait` 可在脚本中阻塞等待任务到达终态。

</details>

<details>
<summary><b>🚀 CPU 批处理（HPC）</b> —— 自动构建 Slurm 脚本并在提交前校验两层资源规格</summary>

支持命令：`inspire hpc create / batch / list / status / stop / delete / events / instances / shell / logs / metrics / quota`。

- `hpc create -c <slurm-body>` 只需传入 Slurm 执行正文（使用显式 `srun` 启动程序），CLI 会自动封装 `#SBATCH` 头部参数。
- **双层规格预检**：HPC 同时包含节点物理规格（`--quota gpu,cpu,mem` 与 `--instance-count`）和 Slurm 任务划分（`--number-of-tasks`、`--cpus-per-task`、`--memory-per-cpu`）。平台后端与网页端均不校验两层是否匹配，配置越界会导致任务静默失败或 Slurm step 永久排队；CLI 会在提交前自动拦截冲突组合，并在省略单任务参数时按节点配额自动推导安全值。
- **执行确认**：`hpc status` 中的 `Steps` 字段（如 `1/1` vs `0/0`）直观反映 `srun` 步骤是否真正执行，避免把漏写 `srun` 的空跑任务误判为成功。

</details>

<details>
<summary><b>🧬 弹性计算（Ray）</b> —— Head + 可伸缩 Worker Group 集群管理</summary>

支持命令：`inspire ray create / batch / list / status / start / stop / delete / events / instances / shell / logs / metrics / scaling / quota`。

- 支持配置单个 Head 节点与多个可弹性伸缩的 Worker Group（指定 `min` / `max` 副本数）。
- 停止后的 Ray 任务保留完整集群配置，可通过 `ray start` 直接拉起，并由 CLI 轮询确认状态真正离开 `STOPPED`。
- `ray scaling` 可查看各 Worker Group 的历史扩缩容记录（`initialized`、`scale_up`、`scale_down`），方便核对弹性策略是否实际触发。

</details>

<details>
<summary><b>🛰 模型在线部署（Serving）</b> —— 服务部署、扩缩容、版本回滚与 API Key 管理</summary>

支持命令：`inspire serving create / batch / list / status / api / start / stop / delete / scale / scale-history / versions / rollback / configs / events / instances / shell / logs / metrics / api-metrics / quota`。

- **全生命周期管理**：配合 `model deploy-config` 确认模型权重的最小硬件规格后，通过 `serving create` 创建在线推理服务；支持 `serving scale` 调整副本数、`serving versions` 与 `serving rollback` 管理历史部署版本、`serving scale-history` 查看副本扩缩容时间线。
- **双维度监控**：`serving metrics` 查看容器 GPU / CPU / 内存占用，`serving api-metrics` 查看请求 QPS、成功率、延迟与 Token 吞吐。
- **调用地址与密钥管理**：`serving api` 可输出完整服务 Endpoint 与 `curl` 调用模板；配套的 `inspire account api-key list / create / export / run / delete` 支持创建推理密钥、以 `0600` 权限安全导出 `.env` 文件，或通过 `api-key run` 直接将 `INF_API_KEY` 注入子进程环境。

</details>

<details>
<summary><b>📉 TensorBoard</b> —— 无需浏览器，直接在终端与 Agent 中读取训练曲线</summary>

支持命令：`inspire tensorboard create / list / status / start / stop / delete / tags / scalars`。

- TensorBoard 在平台中是独立的一等资源（固定 1 CPU / 2 GiB 规格，无需选择配额或镜像）。创建时需指定 `--name`、`--workspace`、`--project`、`--group`，以及指向共享盘日志目录的 `--summary-path`。
- **`tags` 与 `scalars` 直读曲线**：无需打开浏览器，`tensorboard tags` 即可列出所有 run 与指标名称，`tensorboard scalars` 直接返回各条曲线的起止值、最大最小值、step 范围以及最近 `N` 个采样点（`--points N`），让 Agent 能够直接读取并判断 loss 收敛与评测指标走势。

</details>

<details>
<summary><b>📈 监控指标、事件、日志、实例与远端 Shell</b> —— 从调度器到容器内部的全链路可观测性</summary>

- **监控指标（`metrics`）**：`notebook` / `job` / `hpc` / `ray` / `serving metrics` 支持导出 PNG 趋势图或在终端渲染字符趋势线（`--no-plot --sparkline`），Notebook 还可通过 `metrics --now` 查看实时负载快照。
- **平台事件（`events`）**：默认合并控制器级事件与各实例 Pod 级事件（如 `FailedScheduling`、`Pulling`、`Unhealthy`），支持 `--instance` 或 `--workload-level` 过滤。
- **批量状态与事件查询**：`job status`、`hpc status` 和 `job events` 支持一次传入多个任务名批量查询（按每批 20 个并发请求），方便快速巡检矩阵实验。
- **聚合日志（`logs`）与实例列表（`instances`）**：自动汇总各实例日志并标注实例角色或 Rank，在 `instances` 与 `status` 中直接展示各 Pod 所在的物理节点名称。
- **交互式容器终端（`shell`）**：`job` / `hpc` / `ray` / `serving shell` 可直接将本地终端连接到运行中的容器 PTY（HPC 默认进入 `launcher`，Ray 默认进入 `head`，Serving 默认进入首个运行副本，也可通过 `--instance` 指定具体实例）。

</details>

<details>
<summary><b>📊 资源与配额情报</b> —— 实时空余容量、8 卡整节点统计、用量归因与节点事件</summary>

- **规格与容量查询**：`<workload> quota` 列出各计算组支持的 `gpu,cpu,mem` 规格、允许的优先级（`Priority`）及每小时点券价格（`Points/h`）；`resources availability` 同时展示计算组保障额度余量、可抢占低优 GPU 数量，以及物理 8 卡整节点的完全空闲数（`Free Nodes`）和清退低优任务后可得的整节点数（`High Pri Nodes`）。
- **占用归因与回收策略**：`resources usage` 按 `Project → User`（或 `--details` 展开到具体任务）统计当前资源占用及可抢占卡数（`Reclaimable`），支持按 `--group`、`--project`、`--user` 过滤；`resources policy` 展示当前 Workspace 对各类任务的空闲自动回收阈值与运行时长上限。
- **物理节点事件（`resources node-events`）**：按节点名称查询宿主机内核 OOM kill、`TaskHung`、Cordon/Uncordon、重启与 `NodeNotSchedulable` 事件，快速定位单机硬件或内核故障。
- **按需本地缓存（`inspire cache`）**：`inspire cache status / refresh / clear` 管理本地名称与配额索引缓存，日常查询按需自动读写，无需全量预热。

</details>

<details>
<summary><b>🗂 镜像、模型注册表、官方数据集与项目管理</b></summary>

- **镜像管理（`inspire image`）**：`image list / detail / register / sync / set-visibility / delete`，支持 `--keyword` 模糊搜索、`--source all` 并发检索四个可见性目录（`official` / `public` / `project` / `private`）、注册外部 Docker 镜像、push 后同步镜像以及调整自定义镜像可见范围。
- **模型注册表（`inspire model`）**：`model list / register / status / versions / deploy-config / delete`，支持将共享盘模型目录注册到平台、查询实时 vLLM 兼容性与最小部署硬件规格（`deploy-config`），并在删除模型前自动检查是否仍有推理服务占用。
- **数据广场官方数据集（`inspire dataset`）**：`dataset list / show / tags / validate / applications` 直接通过 CAS SSO 握手检索数据广场（`aip.sii.edu.cn`）目录、版本、权限与申请进度，并在创建 Notebook、Job、HPC 时通过 `--dataset <数据集名>:<版本名>` 只读挂载至 `/inspire/dataset/<数据集名>/<版本名>`。
- **项目与预算（`inspire project`）**：`project list / detail / owners` 查看全局项目列表、个人可用点券额度（`My Budget`）与项目总余额（`Project Budget`）、各项支出明细以及项目负责人列表。

</details>

---

# Python SDK：让启智能力进入你的程序

`inspire-skill` 内置了完整的 Python SDK。`InspireClient` 提供同步接口，`InspireAsyncClient` 提供基于 `asyncio` 的原生异步接口；两者与 CLI 共享账号配置与资源模型，覆盖 Workspace、Project、Image、Dataset、Model、Notebook、GPU Job、HPC、Ray、Serving 和 TensorBoard，无需在代码中拼接命令或解析终端文本。

```python
from inspire import InspireClient

with InspireClient(account="research") as client:
    client.login()
    for job in client.jobs.list("分布式训练空间", limit=10).items:
        print(job.name, job.status)
```

异步客户端支持 `async with`、`asyncio.gather` 并发查询、事件与日志流式迭代、远程命令执行（`exec` / `exec_stream`）以及 Notebook 文件上传下载（`upload` / `download`）。完整 API 说明与示例请参考 [Python SDK 文档](references/sdk.md)。

---

# 支持的 Agent Harness

安装脚本会自动把 `SKILL.md` 与 `references/` 部署到本机已安装的 Agent 客户端目录中：

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
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | `~/.dsh/skills/inspire/` | 用户级 Skills 层，跨项目可用；设置 `DSH_HOME` 时使用该目录下的 `skills/inspire/`，可用 `--harness deepseek` 显式安装 |
| [Pi](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md) | `~/.pi/agent/skills/inspire/` | 用户级 Skills 层，跨项目可用；设置 `PI_CODING_AGENT_DIR` 时使用该目录下的 `skills/inspire/` |

---

# 通用 Skill 与项目资产约定（`INSPIRE.md`）

- 安装后的 `SKILL.md` 是通用操作手册。日常任务主要使用 `CPU资源空间` 和 `分布式训练空间`；创建任务时显式指定 `workspace`、`project`、`group`、`quota` 和 `image`。
- 如果某个代码仓库有需要跨会话、跨成员复用的远端共享盘路径、基底镜像、模型或数据集版本，可以在仓库根目录维护一份可选的 `INSPIRE.md`（详见 [`references/assets.md`](references/assets.md)）。
- 如需在本地自定义已安装的 `SKILL.md` 或 `references/`，升级时可使用 `inspire update --cli-only` 避免覆盖本地文档修改。

---

# 🔧 维护承诺

启智平台的调度策略、计算组划分与接口细节会随平台迭代变化。维护者 [@realZillionX](https://github.com/realZillionX) 会持续跟进上游变更并保持 CLI 与文档同步。每次发布新版本后，运行 `inspire` 命令会在 stderr 提示更新，执行 `inspire update` 即可一键升级（详见上方[更新与卸载](#更新与卸载)）。

如遇到平台行为变更或命令异常，欢迎在 [Issue Tracker](https://github.com/realZillionX/InspireSkill/issues) 提交反馈并附上 `inspire --debug <cmd>` 的输出（CLI 会自动脱敏密码、Cookie、Token 与代理凭据）。

---

# 代理配置

在校园网外访问 `*.sii.edu.cn` 时，可在 Clash Verge 中新建一个 `SII Proxy` 策略组，加入组织提供的代理节点与 `DIRECT`，并将规则 `DOMAIN-SUFFIX,sii.edu.cn,SII Proxy` 置于通用规则之前（在校园网内直连时切换为 `DIRECT`）。然后将账号配置中的 `proxy` 设为本机实际的 Mixed Port 地址，例如 `http://127.0.0.1:<mixed-port>`。

如果使用启智官方提供的 aTrust VPN，可参考 [Docker-aTrust](https://github.com/realZillionX/Docker-aTrust) 将 aTrust 运行在 Docker 容器中，在宿主机暴露本地 SOCKS5 / HTTP 代理端口后接入上述分流规则。

账号 proxy 也可以直接写 `socks5://127.0.0.1:<port>`。CLI 的 HTTP 请求会自动改用 `socks5h://`，由代理端解析域名，因此只能在 VPN 内解析的平台域名也能访问；浏览器仍使用原地址。远程 Shell / exec 的 WebSocket 目前只支持 HTTP(S) 代理，需要这些功能时请配置 HTTP 代理端口。

账号配置中的 `proxy` 优先于 Shell 环境变量；未设置账号 `proxy` 时，`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 仍会生效，直连环境下可通过 `NO_PROXY=.sii.edu.cn` 绕过系统代理。可随时运行 `inspire account check --details` 检查当前生效的代理配置与路由状态。

> 代理凭据（Host / User / Password）**请向所在实验室或组织管理员获取**，切勿提交到公开仓库或对话记录中。

---

# 开发与贡献

项目由 [@realZillionX](https://github.com/realZillionX) 维护：

- **直接提交 PR**：文档修正、手册补充、已知平台字段适配或小型 Bugfix 欢迎直接提交 PR；长期协作者（如 [@JingYiJun](https://github.com/JingYiJun)）跟进的平台适配改动在通过测试与 Review 后可快速合入。
- **先提交 Issue**：涉及工作流调整、跨模块重构或多命令行为变更时，建议先在 [Issues](https://github.com/realZillionX/InspireSkill/issues) 描述场景并附上脱敏后的 `inspire --debug <cmd>` 日志，对齐方案后再编码。

开发环境与提交检查说明见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

---

# 文档索引

- [`SKILL.md`](SKILL.md)：Agent 技能主入口，包含核心操作模型、执行闭环与参考文档路由。
- [`references/assets.md`](references/assets.md)：`INSPIRE.md` 持久资产约定与生命周期管理。
- [`references/resources.md`](references/resources.md)：Workspace、计算组、配额规格、实时余量、用量归因与本地缓存。
- [`references/paths.md`](references/paths.md)：共享盘作用域、存储池、挂载隔离与远端路径规范。
- [`references/dataset.md`](references/dataset.md)：数据广场检索、版本与权限核验、`--dataset` 只读挂载。
- [`references/internal-sources.md`](references/internal-sources.md)：联网准备流程、内部软件源配置与镜像固化策略。
- [`references/notebook.md`](references/notebook.md)：Notebook 创建、SSH / JupyterTerminal 连接、文件流转、HTTP 代理与保存镜像。
- [`references/compute-workloads.md`](references/compute-workloads.md)：GPU Job、CPU HPC、Ray、Serving、API Key、TensorBoard、批量提交与任务观察。
- [`references/workflows.md`](references/workflows.md)：跨 CPU 准备、数据处理、GPU 训练与部署的多阶段项目工作流。
- [`references/image.md`](references/image.md)：镜像目录、外部镜像注册、可见性级别与清理规则。
- [`references/model.md`](references/model.md)：Model Registry 模型注册、版本检查、最小部署规格与删除预检。
- [`references/sdk.md`](references/sdk.md)：Python SDK 同步与异步客户端、资源模型与完整方法参考。
- [`references/dev/browser-api.md`](references/dev/browser-api.md)：CLI 维护者参考，记录 `/api/v2` 接口契约、认证与登录保护、分页与 scoping、各路由 Action 表、创建字段合同及数据广场协议。
- [`CONTRIBUTING.md`](CONTRIBUTING.md)：开发环境、测试要求与贡献指南。
- [`cli/`](cli/)：CLI 与 Python SDK 源码（入口为 `cli/inspire/cli/main.py`）。
- [`scripts/install.sh`](scripts/install.sh) / [`scripts/install.ps1`](scripts/install.ps1)：跨平台安装脚本。
- [`scripts/scan_v2_surface.py`](scripts/scan_v2_surface.py)：前端产物 `/api/v2` 接口扫描与 `discovery` 对账工具。

---

# License

[`LICENSE`](LICENSE)（MIT）

# Acknowledgements

- 启智平台团队提供的公开资料与协助。
- [EmbodiedForge/Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) 提供了 CLI 的初步框架。

<p align="center"><sub>Made for researchers who'd rather think than click.</sub></p>
