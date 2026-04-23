<p align="center">
  <img src="assets/hero.svg" width="100%" alt="Inspire Skill — the agent-native cockpit for the Inspire compute platform"/>
</p>

<p align="center">
  <b>让 AI Agent 直接在本地 CLI 里完成启智平台的全部操作。</b><br/>
  <sub>Notebook · Job · HPC · Serving · Model · Resources — 一条命令皆能。</sub>
</p>

<p align="center">
  <a href="cli/"><img src="https://img.shields.io/badge/CLI-bundled-3366FF?style=for-the-badge" alt="CLI bundled"/></a>
  <img src="https://img.shields.io/badge/harness-Claude%20Code%20·%20Codex%20·%20Gemini%20CLI%20·%20OpenClaw%20·%20OpenCode-5566FF?style=for-the-badge" alt="Harnesses"/>
  <img src="https://img.shields.io/badge/status-actively%20maintained-22CCEE?style=for-the-badge" alt="Actively maintained"/>
  <img src="https://img.shields.io/badge/license-MIT-0f172a?style=for-the-badge" alt="License MIT"/>
</p>

---

## 为什么要有这个 Skill？

启智平台的 Web UI `qz.sii.edu.cn` 是你日常实验链路里最慢的那一环——每次申请资源、新建 notebook、开 SSH、同步代码都要反复点点点。

**InspireSkill 把这些步骤交给你的 AI Agent。** 当 Claude Code / Codex / Gemini CLI / OpenClaw / OpenCode 识别到这个 skill，它会：

- 直接调用 `inspire` 命令查实时资源、开 notebook、提 HPC 任务、拉日志
- 提供**可选**的 Clash Verge `7897` 分流模板，让**公网与启智内网共存**一个本地端口，取代多人共用断连的 aTrust；CLI 本身不绑定 7897，任何能同时覆盖公网与 `*.sii.edu.cn` 的代理方案都行
- 把 Web UI 上所有操作都变成**可复现、可串联、可自动化**的命令链
- 从 SKILL.md 读到完整的调度约束、资源申请原则、排障 checklist，不需要你在对话里反复解释平台语义

目标是让你的 Claude Code / Codex / Gemini CLI / OpenClaw / OpenCode **成为推进科研项目的唯一入口**，不用再在浏览器里手动点。

---

## 为什么比 InspireCode / 在实例里装 Agent 更好？

启智官方的 **InspireCode** 是把 OpenCode 直接部署到某个 Inspire 实例里——要用就得打开 `qz.sii.edu.cn`、进那个实例、在它的 Web 终端里跟 OpenCode 对话。凡是"把 Agent 装在服务器上"的方案都是这个路数。**InspireSkill 走相反路径：Agent 留在你本机，Inspire 降格为被调用的工具。**

| 维度 | InspireCode（Agent 装在 Inspire 实例里） | InspireSkill（Agent 装在本机） |
| --- | --- | --- |
| **Agent 生命周期** | 绑死在某一个 notebook 实例；实例回收 / 崩溃，对话与状态一起没 | 跑在本机 harness 里，与任何一个 Inspire 实例解耦 |
| **调度范围** | 只能操作它所在那一个实例的文件系统与运行时 | 一个 Agent 横跨多 workspace / notebook / HPC job / image，**全平台统一编排** |
| **入口** | 必须打开 `qz.sii.edu.cn` 的 Web 终端 | 你本来就在用的 Claude Code / Codex / Gemini CLI / OpenClaw / OpenCode |
| **harness / 模型选择** | 锁定 OpenCode + 它支持的模型 | 任选本机已装的 5 家 harness，模型随 harness |
| **上下文来源** | 只有实例里能看到的东西；你本地代码仓库不在场 | 本机完整 repo + git 状态 + 编辑器 + 其他 MCP 工具（Figma / Preview / Playwright …）一起可用 |
| **计算占用** | Agent 进程吃 Inspire 实例的 CPU / RAM 配额；API key 必须放在实例里 | Agent 进程跑本机；Inspire 实例的 CPU / RAM 全给训练 / HPC；API key 只留本地 |
| **连接依赖** | Web UI 断 = Agent 断；aTrust 掉线对话就停 | `inspire` CLI 直打平台 API；Agent 推理甚至可以完全离 SII 内网 |
| **自动化 / 可复现** | 对话历史锁在浏览器 session 里 | 命令流可保存 / 回放；`inspire --json ...` 被其他脚本直接消费 |

一句话：**InspireCode 把你搬进 Inspire，InspireSkill 把 Inspire 变成 Agent 的一把工具。**

---

## 快速上手

**Requirements**: `bash` · `curl` · `tar` · Python 3.10+ · `uv` 或 `pipx`

```bash
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

**不需要把仓库克隆到本地。** 脚本会：

1. 用 `uv tool install` 或 `pipx install` 从 `git+https://github.com/realZillionX/InspireSkill.git#subdirectory=cli` **直接拉包装进隔离 venv**，`inspire` 自动挂到 `~/.local/bin/`。
2. 自动探测本机 harness（Claude Code / Codex / Gemini CLI / OpenClaw / OpenCode），把 `SKILL.md` 与 `references/` 拷贝到对应 skills 目录。
3. 为 Codex 额外生成 `agents/openai.yaml`。
4. 挂一个每日静默检查上游新版本的后台任务（可用 `--no-schedule` 关）。

想指定范围：

```bash
curl -fsSL .../install.sh | bash -s -- --harness claude             # 单个
curl -fsSL .../install.sh | bash -s -- --harness claude,codex       # 多个
curl -fsSL .../install.sh | bash -s -- --no-cli                     # 仅刷 SKILL / references
curl -fsSL .../install.sh | bash -s -- --no-schedule                # 不装后台检查任务
curl -fsSL .../install.sh | bash -s -- --ref v1.0.0                 # 钉住某个 tag / branch / SHA
```

安装后首次使用：

```bash
inspire init                                   # 账号、代理、默认工作空间
inspire config show --compact                  # 核对生效配置与来源
inspire resources list --all --include-cpu     # 看实时空余
```

之后把控制权交给 Agent，它会从 `SKILL.md` 读到所有 workflow 规则并自动执行。

---

## 能力一览

<table>
<tr>
  <td width="50%">
    <h4>📝 Notebook 统一入口</h4>
    全链路命令化：<code>create / list / status / start · stop / ssh / exec / shell / scp / refresh / forget / test / connections / ssh-config</code>。一次 <code>notebook ssh &lt;id&gt;</code> 就把 SSH 通路和本地 alias 一起记下来。
  </td>
  <td width="50%">
    <h4>🚀 HPC 任务分派</h4>
    <code>inspire hpc create -c &lt;slurm-body&gt;</code> 只写 Slurm 正文 + 显式 <code>srun</code>，平台自动补 <code>#SBATCH</code> 头；<code>resources specs --usage hpc</code> 实时查 <code>predef_quota_id</code>。
  </td>
</tr>
<tr>
  <td>
    <h4>🏃 GPU 多节点任务（不止训练）</h4>
    <code>inspire job</code> 覆盖所有 GPU 多节点场景 —— 分布式训练 / 批量推理 / 并发 worker pool 全走这里（`hpc` 对应 CPU Slurm）。<code>inspire run "&lt;cmd&gt;" --watch</code> 自动选资源组 + 跟 <code>job logs --follow</code>；精细控制优先级 / 节点数用 <code>job create</code>。
  </td>
  <td>
    <h4>📊 资源情报</h4>
    <code>resources list --all --include-cpu</code> / <code>resources nodes --all</code> / <code>resources specs</code> — 三板斧定位哪个集群有空，支持透支式申请。
  </td>
</tr>
<tr>
  <td>
    <h4>🗂 镜像管理</h4>
    <code>image save / register / list / set-default</code>，默认镜像自动写回项目 <code>.inspire/config.toml</code>；<code>hpc create --image-type</code> 明确可见性。
  </td>
  <td>
    <h4>🔌 原生 SSH 直通</h4>
    <code>inspire notebook ssh-config --install</code> 一键把所有 alias 写进 <code>~/.ssh/config</code>，之后用 <code>ssh &lt;alias&gt;</code> / <code>scp</code> / <code>rsync</code> / <code>git</code> 像本地 host 一样用。
  </td>
</tr>
<tr>
  <td>
    <h4>🛰 模型部署 (Serving)</h4>
    <code>inspire serving list / status / stop / configs</code> —— 观测 + 止损分层：<code>list</code> / <code>configs</code> 走 Browser API，<code>status</code> / <code>stop</code> 走 OpenAPI，和 <code>job</code> / <code>hpc</code> 同构。
  </td>
  <td>
    <h4>📦 模型注册表 (Model)</h4>
    <code>inspire model list / status / versions</code> —— 浏览 workspace 下所有模型 + 每个模型的历史版本，带 vLLM 兼容标记 / 创建时间；之前只能在 Web UI 翻。
  </td>
</tr>
<tr>
  <td>
    <h4>👤 身份 / 配额 / 权限</h4>
    <code>inspire user whoami / permissions / api-keys</code> —— 一眼看清当前账号、在某 workspace 下实际授予的权限码（<code>job.trainingJob.create</code> 等），以及已申请的 API Key 元数据。
  </td>
  <td>
    <h4>📅 事件 & 生命周期</h4>
    <code>inspire job events</code> / <code>hpc events</code> / <code>notebook events</code> 拉 K8s / 平台事件流；<code>notebook lifecycle &lt;id&gt;</code> 看一个实例的多次启停记录 —— 原本要翻 Web UI "详情 → 事件/生命周期"两个 tab 才看得全。
  </td>
</tr>
</table>

---

## 支持的 Agent Harness

| Harness | 安装后位置 | 备注 |
| --- | --- | --- |
| [Claude Code](https://claude.com/claude-code) | `~/.claude/skills/inspire/` | **默认推荐** —— Agent 可被**后台命令完成事件**自动唤醒 |
| [Codex CLI](https://github.com/openai/codex) | `~/.codex/skills/inspire/` | 额外生成 `agents/openai.yaml` |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `~/.gemini/skills/inspire/` | |
| [OpenClaw](https://github.com/openclaw/openclaw) | `~/.openclaw/skills/inspire/` | 全局 "managed skills" 层；workspace 层 (`~/.openclaw/workspace/skills/`) 可覆盖 |
| [OpenCode](https://github.com/anomalyco/opencode) | `~/.config/opencode/skills/inspire/` | 遵循 XDG；`$OPENCODE_CONFIG_DIR` 可改根 |

**为什么默认推 Claude Code**：它的 scheduler 支持在**后台 Bash 命令结束时自动唤醒 Agent**。把 `inspire job logs --follow <id>` / 长轮询 checkpoint / `inspire hpc status <id>` 监视之类长 watch 挂到后台，训练或 HPC 任务跑完 Agent 自己醒过来接下一步 —— 不用你守在终端。Codex / Gemini CLI / OpenClaw / OpenCode 目前没有这个能力，做长流水的自动化会弱一档。

---

## 自定义 SKILL.md / INSPIRE.md

SKILL.md 装完是一份**通用默认 playbook**，默认口径是主力跑 `分布式训练空间` 下的 H100 / H200。如果你的主战场不在这儿（比如启智的国产卡 workspace `CI-情境智能-国产卡` / `CI-情境智能-国产卡-ssd3`，或小组自己划走的专属资源开发空间），两条口子做定制：

1. **项目级（推荐）**：改仓库根的 `INSPIRE.md` —— `Path Conventions` 换 remote workspace 路径，`Existing Notebooks` / `Ongoing Jobs` 里显式写国产卡机型和任务。`INSPIRE.md` 属于你的 repo，不会被 `inspire update` 覆写，也方便跟组内协作。SKILL.md §1 "项目叙述上下文" 一行详述约定。
2. **Harness 级**：直接编辑 `~/.claude/skills/inspire/SKILL.md`（Codex / Gemini / OpenClaw / OpenCode 同理），改资源申请段落、默认镜像、常用 workspace 名。注意：`inspire update` **默认会覆盖 SKILL.md**；维护了本地改动后用 `inspire update --cli-only` 只升级 CLI 不动 skill 文件，想合并上游变更时再手动 diff。

---

## 🔧 维护承诺与自动更新

**启智平台的调度语义、资源组划分、镜像可用性会频繁变化。** 一旦平台侧改了而 Skill / CLI 没跟进，Agent 就会按失效的路径执行，产生错误结果。

维护者 [@realZillionX](https://github.com/realZillionX) 会**高频率、持续**跟进上游变更。你只要：

```bash
inspire update            # 拉新版 CLI + 刷 SKILL.md / references
inspire update --check    # 只检查不升级
inspire update --cli-only # 仅升级 Python 包
inspire update --skill-only # 仅刷 SKILL / references
```

有新版本时 CLI 会在任意 `inspire <subcommand>` 的 stderr 提醒一行。直接跑 `inspire update` 就行。

平台侧行为突变又未及时 patch 时，在 [issue tracker](https://github.com/realZillionX/InspireSkill/issues) 开一条，附 `inspire --debug <cmd>` 的 trace（CLI 会自动脱敏 token / cookie / proxy secrets）。**反馈流程的更多细节见下方"开发与贡献"一节。**

---

## 代理配置

不常驻 SII 的科研人员通常需要让本机代理同时转发公网和 `*.sii.edu.cn` 流量。仓库提供一份**可选**的 Clash Verge `7897` mixed-port 分流模板，见 [references/proxy-setup.md](references/proxy-setup.md)；但 CLI 本身不绑定 7897，你也可以换成任意代理方案，只要 `config.toml` / 环境变量里的 proxy 字段指向一个能同时访问公网与 `*.sii.edu.cn` 的本地端口。

> 凭据（host / user / password）**从实验室或组织管理员获取**，不要提交到任何公开仓库或聊天记录。

---

## 开发与贡献

项目由 [@realZillionX](https://github.com/realZillionX) 维护，节奏与启智平台的接口 / 调度语义紧密绑定。为了让上游变更能被**最快、最一致地**消化进 CLI + SKILL.md + `references/`，合作方式有意简化：

- **不建议提 PR。** 平台语义变化快（端点下线 / body schema 改名 / 权限矩阵重排），外部 PR 从提交到 review 完成的窗口里，仓库这边的 CLI / SKILL.md 往往已经跟着上游动了——合并时容易语义错位。维护者更倾向于亲自 implement 并控制上游同步节奏。
- **请提 [Issue](https://github.com/realZillionX/InspireSkill/issues)。** Bug / 端点失效 / SKILL.md 里某条规则不再适用 / CLI UX 想改进 / 新观察到的 Browser API 端点——任何想反馈的都用 Issue 描述问题场景，**附上 `inspire --debug <cmd>` 的日志最好**（CLI 自动脱敏 token / cookie）。维护者会评估后纳入后续版本，通常几天内发新版。
- **反向抓包的新发现**（某端点 404 了、某字段改名了、冒出一个前端在打但 CLI 没封装的路径）同样走 Issue；不用自己附 Playwright `storage_state` / JSONL，维护者会用 [`cli/scripts/reverse_capture/`](cli/scripts/reverse_capture/) 自己复现。

这么安排的底层权衡：**这个 skill 的价值在于与上游保持零漂移的同步**，比起零散 PR 稳步合入，维护者按批次自己写更容易做到这点。你提的 Issue 是最高效的信号。

---

## 文档索引

- [**SKILL.md**](SKILL.md) — Agent 看的主规约：认证链路、命令速查、HPC Slurm 语义、开发主流程三阶段。
- [references/openapi.md](references/openapi.md) — `/openapi/v1` 公开合约（10 条端点：train_job / hpc_jobs / inference_servings 的 `create/detail/stop` + `cluster_nodes/list`；CLI 都已封装，`inference_servings` 由 `inspire serving` 暴露）。
- [references/browser-api.md](references/browser-api.md) — `/api/v1` 前端 SSO API（列表 / 事件 / 模型注册表 / 部署管理 / 用户权限 / 配额 …… 观测性几乎全在这里，OpenAPI 上没有）。
- [references/proxy-setup.md](references/proxy-setup.md) — Clash Verge 7897 分流配置。
- [references/troubleshooting.md](references/troubleshooting.md) — SSH bootstrap / rtunnel / HPC 异常状态对照 / 镜像保存等排障 checklist。
- [references/less-used-commands.md](references/less-used-commands.md) — 不在主速查表里但 CLI 已封装的命令：`serving` / `model` / `project detail|owners` / `user quota|api-keys`。
- [`cli/`](cli/) — CLI 源码；入口 `cli/inspire/cli/main.py`。
- [`scripts/install.sh`](scripts/install.sh) — 面向用户的 curl-pipe-bash 安装器。

---

## License

[MIT](LICENSE)

## Acknowledgements

- 启智平台团队提供的 OpenAPI / Web 接口。
- [EmbodiedForge/Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) 提供了 CLI 的初步框架。

<p align="center"><sub>Made for researchers who'd rather think than click.</sub></p>
