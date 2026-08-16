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
| Workload 覆盖 | 少量训练 / HPC 能力 | 资源、GPU Job、HPC Submit、Logs、Dashboard、Jupyter Exec | Notebook / GPU Job / CPU HPC / Ray / Serving / Model / Image / Resources 全覆盖 |
| 观测闭环 | 有限 | Job Logs、Watch、Usage / Dashboard | Events / Logs / Metrics / Instances / Lifecycle / Status 分层诊断 |
| 资源与路径语义 | 主要是配置和命令参数 | 资源缓存、Workspace / Compute Group / Spec 解析 | Workload Profile 管调度条件，Path Alias 管远端路径，具体启智项目的 `INSPIRE.md` 管持久资产合同 |
| 多账号与项目层 | `[accounts."<user>"]` 合并层 | 以单套 `~/.qzcli/` 配置为中心 | 一账号一目录，账号级默认值和仓库级项目覆盖分层 |

一句话：这两条 CLI 各做了一段路；InspireSkill 把整个平台的操作面、文档面和观测面端到端铺平，让 Agent 不只是“能调用命令”，而是能理解应该怎么用启智平台。

---

# 快速上手

> 平台支持：macOS + Linux 一等公民。Windows 用户请用 [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install)。CLI 依赖 SSH / `rsync` / GPFS 目录约定 / POSIX 文件权限，Windows 原生不在 Roadmap。

## 安装

前置：`bash` / `curl` / `tar` / Python 3.10+ / 已装 `uv`（推荐）或 `pipx` 任一。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

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

`inspire init` 默认做账号级全局发现，写入平台 Catalog 和默认 Path Alias；`--scope project` 用于当前仓库的 Project Context 和 Path Alias 覆盖。

安装、更新和多账号操作见 [`references/setup/install-and-config.md`](references/setup/install-and-config.md)；项目初始化问询（Project / Workspace / Paths / Image）和 `INSPIRE.md` 维护见 [`references/project-context.md`](references/project-context.md)；Clash Verge 的 SII Proxy / DIRECT 分流模板见 [`references/setup/sii-proxy.md`](references/setup/sii-proxy.md)。

---

# 能力一览

<table> <tr> <td width="50%"> <h4>📝 Notebook 统一入口</h4> 全链路命令化：<code>create / list / status / start / stop / ssh / connection / ssh-config / exec / shell / scp / install-deps / proxy-url / path / metrics / events / lifecycle</code>。容器里部署好的服务用 <code>proxy-url --port</code> 拿到外部地址直接请求。显卡不是 <code>H100</code> / <code>H200</code> 的 Notebook 可使用 OpenSSH / SCP / SSH Config；<code>H100</code> / <code>H200</code> 受限 Notebook 使用 JupyterTerminal 执行命令，文件流转以 <code>/inspire/...</code> 共享路径为边界，并通过支持 SSH 的 Notebook 使用 <code>notebook scp</code> 或外部 <code>rsync</code> 完成本地上传/下载。连接类命令会跨账号解析本地已缓存的 Notebook Connection，不要求先切 Active Account。</td> <td width="50%"> <h4>🚀 HPC 任务分派</h4> <code>inspire hpc create -c &lt;slurm-body&gt;</code> 只写 Slurm 正文 + 显式 <code>srun</code>，平台自动补 <code>#SBATCH</code> 头。两层独立：节点资源用 <code>--quota gpu,cpu,mem</code>（CLI 自动解析到平台 Quota Row），Slurm 调度用 <code>--number-of-tasks / --cpus-per-task / --memory-per-cpu</code>。两层之间平台和网页端都不校验，规格不匹配时要么 <code>FAILED</code> 且日志和事件里都没有原因，要么一直 <code>RUNNING</code> 却什么都没跑，所以 <code>hpc create</code> 在提交前自己挡下这些组合。<code>hpc status</code> 的 <code>Steps</code> 是判断「程序到底跑没跑」的字段——正文忘了 <code>srun</code> 的任务照样报成功，但 <code>Steps</code> 是 <code>0/0</code>。</td> </tr> <tr> <td> <h4>🏃 GPU 后台任务（平台名：分布式训练）</h4> 平台官方把 <code>job</code> 这一路叫“分布式训练” / Distributed Training；提交 Job 时只要求 GPU 计算资源和启动命令，不强制程序必须是训练。<code>inspire job</code> 可用于一张卡、多卡、单节点、多节点等后台 GPU 任务：分布式训练 / 批量推理 / 并发 Worker Pool 都走这里（<code>hpc</code> 对应 CPU Slurm）。提交统一使用 <code>job create</code>，可用 <code>--enable-notification</code> 开启当前用户绑定飞书账号的状态通知；需要跟日志时用 <code>job logs &lt;name&gt; --workspace &lt;workspace&gt; --follow</code>，健康度用 <code>job metrics &lt;name&gt; --workspace &lt;workspace&gt;</code> 看 GPU、显存、CPU、内存、I/O 和多 Pod 负载是否同步。</td> <td> <h4>📊 资源情报</h4> <code>resources availability --workspace &lt;name&gt; --include-cpu</code> / <code>resources nodes --workspace &lt;name&gt;</code> / <code>resources usage --workspace &lt;name&gt;</code> / <code>resources policy --workspace &lt;name&gt;</code> / <code>&lt;workload&gt; quota --workspace &lt;name&gt;</code>：定位一个 Workspace 里哪个计算组有空，支持透支式申请。<code>&lt;workload&gt; quota</code> 回答「有哪些合法档位」，<code>availability</code> 回答「这些档位现在还有没有空」，<code>usage</code> 回答「余量去哪了、有多少是停着没用的」，<code>policy</code> 回答「拿到手能留多久——空闲多久被回收、有没有运行时长上限」。<code>&lt;workload&gt; quota</code> 的 <code>Priority</code> 列还给出这一行接受哪些任务优先级：<code>分布式训练空间</code> 的训练区碎卡档只调度低优先级（可被抢占），整节点档才不受限，创建时 CLI 按这一列先做预检，不用等平台拒绝。这些命令一律一次只看一个 Workspace——档位、余量、回收策略和占用都是按 Workspace 定义的事实，跨空间扫一遍答不出任何一个可执行的决定；还接受 <code>--workspace all</code> 的只剩「按名字找东西」那一类（<code>&lt;workload&gt; list</code> / <code>account permissions</code>），因为不知道东西在哪个空间时本来就给不出空间名。余量和规格始终读 Live 数据；<code>inspire cache status / refresh / clear</code> 管的是本地加速缓存——Name 解析索引、Quota 目录和 Notebook 显卡型号，三条命令都支持 <code>--resource &lt;kind&gt;</code> 分类操作。</td> </tr> <tr> <td> <h4>🗂 镜像管理</h4> <code>image list / detail / save / register / set-visibility / delete</code>，创建 Notebook、Job、HPC、Ray 或 Serving 时显式传 <code>--image</code>；<code>hpc create --image-type</code> 明确可见性。</td> <td> <h4>🛰 模型部署（Serving）</h4> <code>inspire serving create / list / status / start / stop / scale / versions / rollback / configs / events / instances / metrics / api-metrics</code>：覆盖模型部署服务的创建、列表、状态、启停、副本伸缩、部署历史与回滚、可用配置、事件、实例和指标；创建前用 <code>serving quota --workspace &lt;workspace&gt;</code> 选 Quota，用 <code>model deploy-config</code> 确认规格下限。<code>metrics</code> 看资源占用，<code>api-metrics</code> 看请求量、成功率和延迟——只有后者能把「没人调用」和「一直调用一直失败」分开。</td> </tr> <tr> <td> <h4>📦 模型注册表（Model）</h4> <code>inspire model list / register / status / versions / deploy-config</code>：浏览或注册 Workspace 下的模型 + 每个模型的历史版本，带 vLLM 兼容标记 / 创建时间；<code>deploy-config</code> 给出某个版本装得下权重的最小节点规格，正好是 <code>serving create --quota</code> 的下限。之前只能在平台网页里翻。</td> <td> <h4>👤 权限</h4> <code>inspire account permissions --workspace &lt;workspace&gt;</code>：看清当前账号在某 Workspace 下实际授予的权限码（<code>job.trainingJob.create</code> 等），提交前先确认自己有没有这个动作的权限。</td> </tr> <tr> <td> <h4>📚 官方数据集</h4> <code>inspire dataset list / show / validate</code>：数据广场是和启智并列的独立平台，只共用同一套 SSO，启智那侧没有检索接口。CLI 用现有登录态走一次 CAS 握手，直接检索目录、读版本、看当前账号有没有挂载权限。确认后在 <code>notebook / job / hpc create</code> 上用 <code>--dataset &lt;数据集名&gt;:&lt;版本名&gt;</code> 只读挂载到 <code>/inspire/dataset/&lt;数据集名&gt;/&lt;版本名&gt;</code>，创建前平台逐条校验，不会先建出一个缺数据的 Workload。数据集用名字寻址，数据广场内部的数字 ID 拿去挂载会被拒。</td> <td> <h4>🧬 弹性计算（Ray）</h4> <code>inspire ray create / list / status / start / stop / delete / events / instances / metrics</code>：一个 Head 加多个可伸缩 Worker Group。停掉的 Job 保留完整集群规格，<code>ray start</code> 原样拉回来，不需要重新指定；平台在这里会「受理但不执行」，所以命令以状态真的离开 <code>STOPPED</code> 为准，没动就报失败。</td> </tr> <tr> <td> <h4>📉 TensorBoard</h4> <code>inspire tensorboard create / list / status / start / stop / delete / tags / scalars</code>：TensorBoard 在平台上是一等对象——计算组单独声明 <code>tensorboard</code> 任务类型，board 既能挂在训练任务上，也能对任意一个 summary 目录单独建；规格由平台固定成 1 CPU / 2 GiB，没有 Quota 也没有镜像要选。关键是 <code>tags</code> 和 <code>scalars</code> 直接读运行中的 board：Agent 自己建一个 board 指向训练目录，再把 loss 和 eval 曲线当数字读回来——首尾值、step 区间、最小最大值，<code>--points N</code> 给最后 N 个点——不需要浏览器，也不需要有人替它去看一眼图。<code>metrics</code> 回答「这个任务在平台侧还健康吗」，这里回答「模型训得怎么样」。</td>  <td width="50%"> <h4>📈 指标、事件 & 生命周期</h4> <code>notebook metrics</code> / <code>job metrics</code> / <code>hpc metrics</code> / <code>ray metrics</code> / <code>serving metrics</code> 读取平台 <code>资源视图</code> 的历史时间序列，默认输出 PNG 趋势图，<code>--no-plot --sparkline</code> 适合终端快速判断；<code>job events</code> / <code>hpc events</code> / <code>notebook events</code> / <code>ray events</code> / <code>serving events</code> 拉平台 Events，<code>job instances</code> / <code>hpc instances</code> / <code>ray instances</code> / <code>serving instances</code> 看 Live Pod / Component 清单和每个 Pod 落在哪个节点，<code>notebook lifecycle &lt;name&gt;</code> 看一个实例的多次启停记录。节点归属还有任务级的一层：<code>job</code> / <code>hpc</code> / <code>serving status</code> 直接列出落点节点（<code>job</code> 另给创建时的 Pin 与排除节点），<code>notebook status</code> 的 <code>Node</code> 附带该节点的健康状态。排查坏节点、复现实验、定位掉队的 Worker 都从这里开始。</td> </tr> <tr> <td colspan="2"> <h4>🗝 多账号（一账号一目录）</h4> <code>inspire account add / list / use / rename / current / remove</code>：每个账号的 <code>config.toml</code>、SSH Tunnel Bridges 和登录缓存都在独立目录 <code>~/.inspire/accounts/&lt;name&gt;/</code>，活动账号由 <code>~/.inspire/current</code> 一行决定。不再有 <code>[accounts."&lt;user&gt;"]</code> 合并层、不再有多个环境变量的优先级链；切账号 = 改一个文件。Notebook 连接类命令的 <code>--account &lt;name&gt;</code> 使用本地 Account Alias，不是平台登录用户名；<code>all</code> 是跨账号扫描 Selector。</td> </tr> </table>

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
- [`references/setup/install-and-config.md`](references/setup/install-and-config.md)：安装、更新、账号配置、全局发现和多账号操作。
- [`references/setup/sii-proxy.md`](references/setup/sii-proxy.md)：Clash Verge 的 SII Proxy / DIRECT 分流模板和验证步骤。
- [`references/project-context.md`](references/project-context.md)：项目初始化问询（Project / Workspace / Paths / Image）、`INSPIRE.md` 资产合同和项目信息持续维护。
- [`references/resources.md`](references/resources.md)：Workspace、Compute Group、规格三元组、实时资源和 Workload Profile 边界。
- [`references/paths.md`](references/paths.md)：共享盘作用域、存储池、挂载隔离、Path Alias 和远端路径边界。
- [`references/internal-sources.md`](references/internal-sources.md)：联网准备动线、SII 内部源入口和镜像固化策略。
- [`references/notebook.md`](references/notebook.md)：Notebook 作为交互工作台、连接方式、文件流转、Proxy 和观察边界。
- [`references/compute-workloads.md`](references/compute-workloads.md)：GPU Job、CPU HPC、Ray、Serving 的适用边界、调度语义和观察闭环。
- [`references/workflows.md`](references/workflows.md)：CPU 准备、数据处理、分布式训练三阶段项目流程。
- [`references/image.md`](references/image.md)：镜像职责、保存 / 注册边界、可见性和清理原则。
- [`references/model.md`](references/model.md)：Model Registry 与 Serving 的职责边界、注册限制和版本判断。
- [`references/dev/browser-api.md`](references/dev/browser-api.md)：CLI 维护参考，覆盖请求契约、响应信封、认证与 Session、分页与 scoping、探针方法、仍在用的 v1 端点和变更验收。
- [`references/dev/browser-api-actions.md`](references/dev/browser-api-actions.md)：CLI 维护参考，12 条路由 93 个 Action 的请求体、响应、参数语义、CLI 映射与限制。
- [`references/dev/data-plaza-api.md`](references/dev/data-plaza-api.md)：CLI 维护参考，数据广场（`aip.sii.edu.cn`）的 CAS 握手、信封与目录端点。
- [`CONTRIBUTING.md`](CONTRIBUTING.md)：开发、测试和贡献约定。
- [`cli/`](cli/)：CLI 源码；入口 `cli/inspire/cli/main.py`。
- [`scripts/install.sh`](scripts/install.sh)：Curl Pipe Bash 安装器。

---

# License

[`LICENSE`](LICENSE)（MIT）

# Acknowledgements

- 启智平台团队提供的公开资料与协助。
- [EmbodiedForge/Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) 提供了 CLI 的初步框架。

<p align="center"><sub>Made for researchers who'd rather think than click.</sub></p>
