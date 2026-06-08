<p align="center">
  <img src="https://raw.githubusercontent.com/realZillionX/InspireSkill/main/assets/hero.svg" width="100%" alt="Inspire Skill — the agent-native cockpit for the Inspire compute platform"/>
</p>

<p align="center">
  <b>让 AI Agent 直接在本地 CLI 里完成启智平台的全部操作。</b><br/>
  <sub>Notebook · Job · HPC · Serving · Model · Resources — 一条命令皆能。</sub>
</p>

<p align="center">
  <a href="https://github.com/realZillionX/InspireSkill/tree/main/cli"><img src="https://img.shields.io/badge/CLI-bundled-3366FF?style=for-the-badge" alt="CLI bundled"/></a>
  <img src="https://img.shields.io/badge/harness-Claude%20Code%20·%20Codex%20·%20Antigravity%20·%20Cursor%20·%20OpenClaw%20·%20OpenCode%20·%20Qoder-5566FF?style=for-the-badge" alt="Harnesses"/>
  <img src="https://img.shields.io/badge/status-actively%20maintained-22CCEE?style=for-the-badge" alt="Actively maintained"/>
  <img src="https://img.shields.io/badge/license-MIT-0f172a?style=for-the-badge" alt="License MIT"/>
</p>

---

## 为什么要有这个 Skill？

启智平台网页 `qz.sii.edu.cn` 是 Agent 日常实验链路里最慢的那一环——每次申请资源、新建 notebook、开 SSH、同步代码都要反复点点点。

**InspireSkill 把这些步骤交给 AI Agent。** 当 Claude Code / Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder 识别到这个 skill，它会：

- 直接调用 `inspire` 命令查实时资源、开 notebook、提 HPC 任务、拉日志
- 提供**可选**的 Clash Verge mixed-port 分流模板，让**公网与启智内网共存**一套本地代理配置，取代多人共用断连的 aTrust；CLI 本身不绑定固定端口，任何能同时覆盖公网与 `*.sii.edu.cn` 的代理方案都行
- 把平台网页上的常用操作都变成**可复现、可串联、可自动化**的命令链
- 从 SKILL.md 按需加载对应使用手册，理解调度语义、资源申请原则和验收点，不需要 Agent 在对话里反复解释平台语义

目标是让 Claude Code / Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder **成为推进科研项目的唯一入口**，不用再在浏览器里手动点。

---

## 为什么比 InspireCode / 在实例里装 Agent 更好？

启智官方的 **InspireCode** 是把 OpenCode 直接部署到某个 Inspire 实例里——要用就得打开 `qz.sii.edu.cn`、进那个实例、在它的终端里跟 OpenCode 对话。凡是"把 Agent 装在服务器上"的方案都是这个路数。**InspireSkill 走相反路径：Agent 留在本机，Inspire 降格为被调用的工具。**

| 维度 | InspireCode（Agent 装在 Inspire 实例里） | InspireSkill（Agent 装在本机） |
| --- | --- | --- |
| **Agent 生命周期** | 绑死在某一个 notebook 实例；实例回收 / 崩溃，对话与状态一起没 | 跑在本机 harness 里，与任何一个 Inspire 实例解耦 |
| **调度范围** | 只能操作它所在那一个实例的文件系统与运行时 | 一个 Agent 横跨多 workspace / notebook / HPC job / image，**全平台统一编排** |
| **入口** | 必须打开 `qz.sii.edu.cn` 的实例终端 | Agent 本来就在用的 Claude Code / Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder |
| **harness / 模型选择** | 锁定 OpenCode + 它支持的模型 | 任选本机已装的 7 家 harness，模型随 harness |
| **上下文来源** | 只有实例里能看到的东西；本地代码仓库不在场 | 本机完整 repo + git 状态 + 编辑器 + 其他 MCP 工具（Figma / Preview / Playwright …）一起可用 |
| **计算占用** | Agent 进程吃 Inspire 实例的 CPU / RAM 配额；API key 必须放在实例里 | Agent 进程跑本机；Inspire 实例的 CPU / RAM 全给训练 / HPC；API key 只留本地 |
| **连接依赖** | 平台网页断 = Agent 断；aTrust 掉线对话就停 | `inspire` CLI 直接操作平台；Agent 推理甚至可以完全离 SII 内网 |
| **自动化 / 可复现** | 对话历史锁在浏览器页面里 | 命令流可保存 / 回放；可读格式给 Agent 决策，结构化输出留给脚本消费 |

一句话：**InspireCode 把 Agent 搬进 Inspire，InspireSkill 把 Inspire 变成 Agent 的一把工具。**

---

## 为什么比社区里其它启智 CLI 更值得用？

启智社区还有两条独立维护的 CLI：[EmbodiedForge/Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) 和 [tianyilt/qzcli_tool](https://github.com/tianyilt/qzcli_tool)。差异主要在能力覆盖广度——能不能在 `分布式训练空间` 这类离线 GPU 空间零配置 SSH、能不能拿到事件 / 生命周期 / GPU 利用率这些观测信号、能不能端到端编排 notebook + HPC + Ray + serving。

| 维度 | [Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) | [qzcli_tool](https://github.com/tianyilt/qzcli_tool) | **InspireSkill** |
| --- | --- | --- | --- |
| **License / 分发** | Proprietary, members-only · 源码渠道 | 无 LICENSE · 源码渠道 | **MIT · PyPI + `curl \| install.sh`** |
| **零配置 SSH / 文件流转** | 需要 Agent 预配本地组件或容器公网 | 无统一远程执行抽象 | **零配置**，`ssh / exec / shell / scp` 都直接按 notebook name 使用 |
| **平台能力覆盖** | 少量训练 / HPC 能力 | 部分 HPC + job 能力 | notebook / job / HPC / Ray / Serving / Model / resources 全覆盖 |
| **事件 / 生命周期 / GPU 利用率查询** | 无 | 无 | **5 类 events + notebook/job/hpc/serving metrics + run_index 生命周期** |
| **HPC / Ray / Serving / Model** | 仅有部分 job + notebook | 仅 HPC（单层）+ job | **HPC 两层模型 + Ray 弹性 + Serving + Model** 全覆盖 |
| **多账号** | `[accounts."<user>"]` 合并层 | 单账号 | 一账号一独立目录，`~/.inspire/current` 切换 |
| **Agent 接入** | 无 | `qzcli-mcp`（1 家 harness） | Skill 格式覆盖 7 家 harness |
| **测试** | 多文件回归套件 | 3 文件 | 持续扩展的单元测试套件 |

一句话：**这两条 CLI 各做了一段路；InspireSkill 把整个平台的操作面和观测面端到端铺平了，并且在离线场景下零配置可用。**

---

## 快速上手

> **平台支持**：macOS + Linux 一等公民。**Windows Agent 请用 [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install)**——CLI 依赖 SSH / rsync / GPFS 目录约定 / POSIX 文件权限，Windows 原生不在 roadmap。

### 安装

**前置**：`bash` · `curl` · `tar` · Python 3.10+ · 已装 `uv`（推荐）或 `pipx` 任一。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

安装、可选参数和安装后检查见 [references/setup/install-and-config.md](references/setup/install-and-config.md)。

### 更新

```bash
inspire update                # CLI 包 + SKILL/references 一起升到最新
inspire update --check        # 只检查，不动
inspire update --cli-only     # 仅升 CLI 包与运行时
inspire update --skill-only   # 仅刷 SKILL.md / references/
```

升级旧版本和 installer 检测说明见 [references/setup/install-and-config.md](references/setup/install-and-config.md)。
成功升级 CLI 后，`inspire update` 会显示旧版本到新版本之间的 GitHub Release 更新摘要。

### 完整初始化（安装后必跑）

```bash
inspire account add <name>
inspire config show --compact
inspire init
cd /path/to/your-repo
inspire init --scope project
inspire resources availability --workspace all --include-cpu
```

`inspire init` 默认做账号级全局发现，写入平台 catalog 和默认 path alias；`--scope project` 用于当前仓库的 project context 和 path alias 覆盖。

账号级 / 项目级配置分层、多账号和代理 setup 见 [references/setup/install-and-config.md](references/setup/install-and-config.md)。

---

## 能力一览

<table>
<tr>
  <td width="50%">
    <h4>📝 Notebook 统一入口</h4>
    全链路命令化：<code>create / list / status / start · stop / ssh / connection / ssh-config / exec / shell / scp / install-deps / metrics / events / lifecycle</code>。<code>notebook ssh &lt;name&gt;</code> 像 SSH 一样打开交互终端，<code>notebook connection refresh &lt;name&gt;</code> 可显式刷新连接缓存，<code>notebook ssh-config &lt;name&gt;</code> 可接入原生 OpenSSH / scp / rsync。<b>任何镜像、任何计算组、有无公网</b>都能直接使用远程执行和文件流转命令。
  </td>
  <td width="50%">
    <h4>🚀 HPC 任务分派</h4>
    <code>inspire hpc create -c &lt;slurm-body&gt;</code> 只写 Slurm 正文 + 显式 <code>srun</code>，平台自动补 <code>#SBATCH</code> 头。两层独立：节点资源用 <code>--quota gpu,cpu,mem</code>（CLI 自动解析到平台 quota row），slurm 调度用 <code>--number-of-tasks / --cpus-per-task / --memory-per-cpu</code>。
  </td>
</tr>
<tr>
  <td>
    <h4>🏃 GPU 后台任务（平台名：分布式训练）</h4>
    平台官方把 <code>job</code> 这一路叫"分布式训练" / distributed training；提交 job 时只要求 GPU 计算资源和启动命令，不强制程序必须是训练。<code>inspire job</code> 可用于一张卡、多卡、单节点、多节点等后台 GPU 任务 —— 分布式训练 / 批量推理 / 并发 worker pool 都走这里（<code>hpc</code> 对应 CPU Slurm）。提交统一使用 <code>job create</code>；需要跟日志时用 <code>job logs &lt;name&gt; --workspace &lt;workspace&gt; --follow</code>，健康度用 <code>job metrics &lt;name&gt; --workspace &lt;workspace&gt;</code> 看 GPU、显存、CPU、内存、I/O 和多 pod 负载是否同步。
  </td>
  <td>
    <h4>📊 资源情报</h4>
    <code>resources availability --workspace all --include-cpu</code> / <code>resources nodes --workspace all</code> / <code>&lt;workload&gt; quota --workspace &lt;name&gt;</code> — 三板斧定位哪个集群有空，支持透支式申请。
  </td>
</tr>
<tr>
  <td>
    <h4>🗂 镜像管理</h4>
    <code>image list / detail / save / register / set-visibility / delete</code>，创建 notebook、job、HPC、Ray 或 serving 时显式传 <code>--image</code>；<code>hpc create --image-type</code> 明确可见性。
  </td>
  <td>
    <h4>🛰 模型部署 （Serving）</h4>
    <code>inspire serving create / list / status / stop / configs / metrics</code> —— 覆盖模型部署服务的创建、列表、状态、可用配置、资源指标和停止操作；创建前用 <code>serving quota --workspace &lt;workspace&gt;</code> 选 quota。
  </td>
</tr>
<tr>
  <td>
    <h4>📦 模型注册表 （Model）</h4>
    <code>inspire model list / status / versions</code> —— 浏览 workspace 下所有模型 + 每个模型的历史版本，带 vLLM 兼容标记 / 创建时间；之前只能在平台网页里翻。
  </td>
  <td>
    <h4>👤 身份 / 配额 / 权限</h4>
    <code>inspire user whoami / permissions / api-keys</code> —— 一眼看清当前账号、在某 workspace 下实际授予的权限码（<code>job.trainingJob.create</code> 等），以及已申请的 API Key 元数据。
  </td>
</tr>
<tr>
  <td width="50%">
    <h4>📈 指标、事件 & 生命周期</h4>
    <code>notebook metrics</code> / <code>job metrics</code> / <code>hpc metrics</code> / <code>serving metrics</code> 读取平台 <code>资源视图</code> 的历史时间序列，默认输出 PNG 趋势图，<code>--no-plot --sparkline</code> 适合终端快速判断；<code>job events</code> / <code>hpc events</code> / <code>notebook events</code> / <code>ray events</code> 拉平台事件流，<code>job instances</code> / <code>hpc instances</code> / <code>ray instances</code> 看 live pod / component 清单，<code>notebook lifecycle &lt;name&gt;</code> 看一个实例的多次启停记录。
  </td>
  <td width="50%">
    <h4>🗝 多账号（一账号一目录）</h4>
    <code>inspire account add / list / use / current / remove</code> —— 每个账号的 <code>config.toml</code>、SSH tunnel bridges 和登录缓存都在独立目录 <code>~/.inspire/accounts/&lt;name&gt;/</code>，活动账号由 <code>~/.inspire/current</code> 一行决定。不再有 <code>[accounts."&lt;user&gt;"]</code> 合并层、不再有多个环境变量的优先级链；切账号 = 改一个文件。
  </td>
</tr>
</table>

---

## 支持的 Agent Harness

| Harness | 安装后位置 | 备注 |
| --- | --- | --- |
| [Claude Code](https://claude.com/claude-code) | `~/.claude/skills/inspire/` | **默认推荐** —— Agent 可被**后台命令完成事件**自动唤醒 |
| [Codex CLI](https://github.com/openai/codex) | `~/.codex/skills/inspire/` | 额外生成 `agents/openai.yaml` |
| [Antigravity](https://antigravity.google/docs/skills) | `~/.gemini/config/skills/inspire/` | 用户级 global Skills 层，跨项目可用 |
| [Cursor](https://cursor.com/docs/skills) | `~/.cursor/skills/inspire/` | 用户级 global Skills 层，跨项目可用 |
| [OpenClaw](https://github.com/openclaw/openclaw) | `~/.openclaw/skills/inspire/` | 全局 "managed skills" 层；workspace 层 （`~/.openclaw/workspace/skills/`） 可覆盖 |
| [OpenCode](https://github.com/anomalyco/opencode) | `~/.config/opencode/skills/inspire/` | 遵循 XDG；`$OPENCODE_CONFIG_DIR` 可改根 |
| [Qoder CLI](https://docs.qoder.com/en/cli/Skills) | `~/.qoder/skills/inspire/` | 用户级 Skills 层，跨项目可用 |

**为什么默认推 Claude Code**：它的 scheduler 支持在**后台 Bash 命令结束时自动唤醒 Agent**。把 `inspire job logs <name> --workspace <workspace> --follow` / 长轮询 checkpoint / `inspire hpc status <name> --workspace <workspace>` 监视之类长 watch 挂到后台，训练或 HPC 任务跑完 Agent 自己醒过来接下一步。Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder 目前没有这个能力，做长流水的自动化会弱一档。

---

## 自定义 SKILL.md / INSPIRE.md

SKILL.md 装完是一份**通用 playbook**。日常 workspace 基本就是 `CPU资源空间` 和 `分布式训练空间`；资源条件不要写成隐式默认值，把 `workspace`、`project`、`group`、`quota` 和 `image` 组合成 workload profile，并在 `inspire notebook/job/hpc/... create --profile <name>` 或 batch 文件里显式使用。如果 Agent 的主战场是启智的国产卡分区、`CI-情境智能` 工作空间，或小组自己划走的专属资源开发空间，两条口子做定制：

1. **项目级（推荐）**：改仓库根的 `INSPIRE.md`，并用 `inspire <workload> profile set <name> ...` 保存条件组；`Path Conventions` 只写 remote path alias。`INSPIRE.md` 属于当前 repo，不会被 `inspire update` 覆写，也方便跟组内协作。
2. **Harness 级**：直接编辑 `~/.claude/skills/inspire/SKILL.md` 和同目录 `references/`（Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder 同理），改按需加载入口或对应使用手册。注意：`inspire update` **默认会覆盖 SKILL.md 和 references/**；维护了本地改动后用 `inspire update --cli-only` 只升级 CLI 与运行时、不动 skill 文件，想合并上游变更时再手动 diff。

---

## 🔧 维护承诺

**启智平台的调度语义、资源组划分、镜像可用性会频繁变化。** InspireSkill 的维护目标是让 CLI 和使用手册始终贴近平台真实行为。

维护者 [@realZillionX](https://github.com/realZillionX) 会**高频率、持续**跟进上游变更。每次发版后，任意 `inspire <subcommand>` 都会在 stderr 提醒一行，跑 `inspire update` 即升（用法见上面 [更新](#更新) 段）。

发现新的平台行为差异时，在 [issue tracker](https://github.com/realZillionX/InspireSkill/issues) 开一条，附 `inspire --debug <cmd>` 的 trace（CLI 会自动脱敏敏感登录凭据和代理信息）。**反馈流程的更多细节见下方"开发与贡献"一节。**

---

## 代理配置

不常驻 SII 的科研人员通常需要让本机代理转发 `*.sii.edu.cn` 流量；能直连 SII 校园网的人可以走 `DIRECT`。Clash Verge mixed-port 的 SII proxy / DIRECT 分流模板见 [references/setup/install-and-config.md](references/setup/install-and-config.md)；CLI 本身不绑定固定端口。代理地址通过 `inspire account add` 写入账号配置，并可用 `inspire config show --compact` 核对。

> 凭据（host / user / password）**从实验室或组织管理员获取**，不要提交到任何公开仓库或聊天记录。

---

## 开发与贡献

项目由 [@realZillionX](https://github.com/realZillionX) 维护，节奏与启智平台的行为 / 调度语义紧密绑定。为了让上游变更能被**最快、最一致地**消化进 CLI + SKILL.md + `references/`，贡献入口按变更风险分层：

- **欢迎小而清楚的 PR。** 文档修正、使用手册补丁、平台行为变化修复、可复现的小型 CLI bugfix 都可以直接提 PR；长期协作者（如 [@JingYiJun](https://github.com/JingYiJun)）持续跟进平台变化，相关 PR 通过测试和基础 review 后可按快速通道合入。
- **大范围语义调整先提 [Issue](https://github.com/realZillionX/InspireSkill/issues)。** 平台语义变化快，涉及工作流重写、配置边界、调度策略或多命令联动的改动，先用 Issue 描述问题场景，**附上 `inspire --debug <cmd>` 的日志最好**（CLI 会自动脱敏敏感登录凭据和代理信息）。维护者会评估后纳入后续版本，通常几天内发新版。
- **新的平台行为差异**同样走 Issue；不用自己附敏感本地文件，维护者会用仓库内的开发工具复现。

这么安排的权衡：**这个 skill 的价值在于与上游保持零漂移的同步**。Issue 是最高效的问题信号，PR 是可落地 patch 的通道；能小步合并的就小步合并，需要统一调度的就先收敛语义再动手。

---

## 文档索引

- [**SKILL.md**](SKILL.md) — 日常使用入口：CLI help 查询方式、按需加载索引和项目上下文字段。
- [references/setup/install-and-config.md](references/setup/install-and-config.md) — 安装、更新、账号初始化、项目初始化和 SII proxy setup。
- [references/dev/browser-api.md](references/dev/browser-api.md) — CLI 维护参考：网页会话接口和当前前端请求合约。
- [references/resources-and-paths.md](references/resources-and-paths.md) — 实时资源、规格三元组、共享盘作用域、存储池和项目路径。
- [references/notebook.md](references/notebook.md) — Notebook 创建、连接、远程执行、传文件、基底环境准备和容器内 HTTP 服务暴露。
- [references/compute-workloads.md](references/compute-workloads.md) — GPU job、CPU HPC、Ray、serving 的适用边界、调度语义和示例。
- [references/workflows.md](references/workflows.md) — CPU 准备、数据处理、分布式训练三阶段项目流程。
- [`cli/`](cli/) — CLI 源码；入口 `cli/inspire/cli/main.py`。
- [`scripts/install.sh`](scripts/install.sh) — curl-pipe-bash 安装器。

---

## License

[MIT](LICENSE)

## Acknowledgements

- 启智平台团队提供的公开资料与协助。
- [EmbodiedForge/Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) 提供了 CLI 的初步框架。

<p align="center"><sub>Made for researchers who'd rather think than click.</sub></p>
