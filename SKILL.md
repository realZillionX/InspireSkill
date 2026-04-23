---
name: inspire
description: "Execution-first Inspire platform playbook for agents driving the inspire CLI as a black-box tool, covering notebook lifecycle, remote-exec/SSH workflows, image and resource ops, job and HPC submission, proxy routing, and failure recovery."
---

# Inspire Skill

> **定位**：用 `inspire` 命令完成启智平台全流程操作。Agent 把 CLI 当黑盒直接用，不要读源码。命令失败先看 [references/troubleshooting.md](references/troubleshooting.md)；代理配置见 [references/proxy-setup.md](references/proxy-setup.md)。

## 0. 平台侧硬约束

只列 **CLI 之外**、Agent 不可能从命令自己看出来的平台特性。CLI 使用坑放到 §2 各命令行的注释列里。

| 主题 | 约束 |
| --- | --- |
| **资源申请** | 切勿保守。先 `resources list --all --include-cpu` / `resources nodes` / `resources specs` 查实时空余，按真实需求申请（需要 20 张还是 500 张 GPU 都行，不要惯性缩量）；只有调度语义 / 项目配额 / 实时空余明确要求时才降档。 |
| **代理** | 公网与 `*.sii.edu.cn` 需**同时可达**。任意覆盖这两段的代理方案都行（仓库提供可选的 Clash Verge `7897` 分流模板，见 `references/proxy-setup.md`）。 |
| **低优抢占** | `priority_level: LOW` 会被 `HIGH` **强制抢占**，必须高频 checkpoint。（优先级 flag 语义见 `job create` / `hpc create` 行。） |
| **HPC 资源余量** | 平台自身额外占 `0.3` 核 CPU + `384 MB` 内存；应用层并发压到 **`cpus-per-task - 4`** 或更低。 |
| **`CPU资源空间` 下能跑 hpc 的计算组** | **只有 `HPC-可上网区资源-2`** 支持 `inspire hpc create`。其它计算组（`CPU资源-1` / `CPU资源-2` / ……）只能建 notebook，不能提 hpc。 |
| **HPC-可上网区资源-2 的 500GB 规格实际不可用（运维 bug）** | 2026-04 实测：`resources specs --usage hpc` 和 Web UI 都列着 500GB 档，但实际提交**静默排队不被调度**（节点侧没配）。且由于上一行约束，**目前 CPU 空间没有任何计算组能跑 500GB 的 HPC 任务**——真需要 500GB 时只能退化成在 `CPU资源-2` 上起 500GB notebook 跑交互式 / 脚本处理。 |
| **项目-实例绑定的挂载可见性** | 一个 notebook / job / hpc 实例只挂**自身所在项目**的 fileset，其它项目的 `/inspire/hdd\|ssd\|qb-ilm\|qb-ilm2/project/<others>/` 路径在该实例里**根本不存在**（`ls` 报 `No such file or directory`）——不是权限问题，是没挂。访问项目 `<X>` 的存储**必须**在 `project=<X>` 的实例里操作：`inspire --json notebook list -A` 按 `project.name` 找 running；没有就 `inspire notebook create --project <X-alias>` 新起。 |
| **跨项目文件传输** | 不同 project 复制共享盘文件**需要 root 权限**，`notebook scp` / `exec cp` / 单账号 CLI 都做不到。找**飞书项目群**里的管理员做 `cp` / `chmod`，不要反复试。 |

## 1. 通用规则

| 主题 | 规则 |
| --- | --- |
| 账号管理 | 多账号 = 独立目录。每个账号的 `config.toml` / `bridges.json` / `web_session.json` 都在 `~/.inspire/accounts/<name>/` 里，活动账号写在一行 `~/.inspire/current`。用 `inspire account add/list/use/current/remove` 管理。**没有活动账号时 CLI 直接报错**指向 `inspire account add`——不存在全局 fallback 路径。 |
| 配置查询 | **不要直接读** `~/.inspire/accounts/<active>/config.toml` 或 `./.inspire/config.toml`；合并由 CLI 负责。扁平字段 `inspire config show [--json] [--compact]`；活动账号 / 项目 / workspace alias / compute_groups 用 `inspire config context [--json]`。 |
| 项目叙述上下文 | 项目仓库根下用 **`INSPIRE.md`** 写非配置性上下文。建议五节：`Default Image`（config.toml 未托管的镜像，如 base / HPC 专用）· `Path Conventions`（本地与远端路径派生规则）· `Public Directory Layout`（`public/` 下的共享结构）· `Existing Notebooks`（角色 → ID）· `Ongoing Jobs`（当前长期在跑的任务）。**不**把 config.toml 内容复制进来。`AGENTS.md` / `CLAUDE.md` / `GEMINI.md` 只放通用工程事项。 |
| `--json` 位置 | 全局 `--json` **必须放子命令前**：`inspire --json hpc status <id>`。 |
| Debug | `inspire --debug` 把脱敏日志写进 `~/.cache/inspire-skill/logs/`。 |
| 默认 workspace 范围 | 本 SKILL 默认只把 **`CPU资源空间`**（阶段 A / B：notebook、HPC、数据处理、Ray CPU pipeline）和 **`分布式训练空间`**（阶段 C：单节点 debug / `job` 多节点训练）作为一等公民。这两个是给大多数研究人员共享的通用空间。`inspire account list` / `config context` 下可能还看得到 `整节点任务空间` / `CI-情境智能` / `CI-情境智能-国产卡*` / `可上网GPU资源` / `专属资源开发空间` / `CI-PPU` / `CPU临时测试空间` / `高性能计算` 等——**那些是小组 / 课题专属空间或测试沙箱**，不归默认用户用，别主动往里塞任务。需要在这些空间跑的人会自己把相关命令 / 规格加进仓库级 `INSPIRE.md` 或本地 harness 的 SKILL.md 覆盖层。 |
| 废弃资源清理 | 别让废弃 notebook / job / hpc 堆积污染 Web UI 列表。终态（`SUCCEEDED` / `FAILED` / `STOPPED` / `CANCELLED`）且确认不再需要时就 `delete`；批量用 `inspire --json <res> list -A` 过滤再逐个 `delete --yes`。running 的先 `stop` 再 `delete`；不确定是否还有人用时跳过，不要猜着删。 |
| 排队久 / 莫名失败优先查事件 | 任务卡 PENDING / CREATING 超过预期，或者突然 FAILED 没明显原因时，**第一步**永远是 `inspire <res> events <id>`（`notebook` / `job` / `hpc` / `ray` 都有）。`job` / `ray` 再叠 `--instance <pod>` / `ray instances <id>` 看哪个 pod 的问题。不凭猜重提——原因不明前不烧配额。 |

## 2. 命令速查

### 2.1 Notebook（生命周期 + 远程操作 + alias 管理）

> **一个 notebook ↔ 一条本地 alias**。首次 `notebook ssh <id>` 引导 SSH 时把连接存成 alias（默认用 notebook 的显示名清洗成 alias-safe 形式，清洗空了才回退 `nb-<id 前 8 位>`；`--save-as` 可强制改名）。同一个 `notebook_id` 已有 alias 时直接复用，不会重复建——老用户的 `nb-<id>` 记录不会被改名。`notebook ssh <arg>` 多态——arg 是 id 就 bootstrap，是已保存 alias 就重连（自动重建断开的 tunnel）。
>
> **`shell` vs `exec`（远端执行两种模式）**：
> - `inspire notebook shell <alias>` = 交互式**持久**会话。一次登入后连续敲命令，cwd / env / shell 变量全部保留，直到 `exit`。等价于直接 `ssh <alias>`，所以在 N 个终端里同时 `inspire notebook shell mybox` 就是 N 个互相独立的会话并存，各自 cwd / env / history 互不影响（远端是共享 CPU / 内存的单容器，多路并发会互相抢资源；要真并行算力走 `job` 多节点或 `hpc`）。
> - `inspire notebook exec <alias> "<cmd>"` = **一次性 one-shot**。每次调用起一个独立 SSH 子进程跑完就断，**两次 `exec` 之间不共享 cwd / env / shell 变量**。想接续状态就把多条命令塞进**同一次调用**（`exec "cd foo && export X=1 && ./run.sh"`），或在远端写脚本后 `exec "bash setup.sh"`。

| 命令 | 用途与约束 |
| --- | --- |
| `inspire notebook list` | 列实例；`-A` 跨 workspace，`-s RUNNING` 过滤，`--name` 搜索 |
| `inspire notebook create` | 建实例；常用 `--workspace` / `--resource` / `--image` / `--wait` |
| `inspire notebook status <id>` | 看详情，镜像名在 `image.name` |
| `inspire notebook events <id>` | 实例生命周期事件流（调度 / 镜像拉取 / 保存镜像各阶段）；`--tail N` / `--from-cache` 可组合 |
| `inspire notebook lifecycle <id>` | 多次启停的粗粒度时间线（一次 `start→stop` 一行）；想看一次运行内部状态用 `events` |
| `inspire notebook start/stop <id>` | 启停；做 `ssh` 前先核实状态 |
| `inspire notebook delete <id> [--yes]` | 永久删一个 notebook。running 的要先 `stop`；`-y` 跳过确认。本地 alias 不会同时清掉——用 `notebook forget` |
| `inspire notebook ssh <id>` | **Bootstrap SSH / rtunnel**（平台默认 `allow_ssh=false`，CLI 自动引导）。失败转 troubleshooting.md |
| `inspire notebook ssh <id> --save-as <name>` | 自定义 alias 名 |
| `inspire notebook exec <alias> "<cmd>"` | 远端 `INSPIRE_TARGET_DIR` 下执行**一次性**命令；详情 / 状态不共享的坑见上方 `shell` vs `exec` 说明。对 notebook-backed alias 可自动重建断开的 tunnel |
| `inspire notebook shell [<alias>]` | **持久**交互 SSH shell，同一窗口内命令共享 cwd / env（详见上方 `shell` vs `exec` 说明） |
| `inspire notebook scp <src> <dst>` | 传**非仓库**文件。**不是** repo 同步——源码走本地 `git push` + `notebook exec` 远端 `git pull`。不继承 `INSPIRE_TARGET_DIR`，远端写绝对路径 |
| `inspire notebook test [<alias>]` | 连通性测试（带耗时）；排障首选 |
| `inspire notebook refresh <alias>` | 刷新 alias 连接（notebook 换实例 / 重启后） |
| `inspire notebook connections` | 列本地已保存 alias |
| `inspire notebook forget <alias>` | 删本地 alias 记录（不影响平台上的 notebook） |
| `inspire notebook set-default <alias>` | 设默认 alias |
| `inspire notebook ssh-config --install` | 把所有 alias 写进 `~/.ssh/config`，之后 `ssh <alias>` / `scp` / `rsync` / `git` 原生用 |
| `inspire notebook top` | alias 实例的 GPU 利用率（SSH `nvidia-smi` 实时快照，需要 tunnel）；`--watch` 持续刷新 |
| `inspire notebook metrics <id>` | 资源视图的历史利用率曲线（GPU / GPU Memory / CPU / Memory / Disk IO / Network 共 8 种，默认 `--metric core` 取前 4 个）。默认出 PNG 到 `~/.inspire/metrics/notebook-<id>-<unix>.png`，`--json` 给原始 per-pod 时序。**和 `top` 的区别**：`top` 是实时 `nvidia-smi`（要 tunnel 活着），`metrics` 是历史曲线（不需要 tunnel）。`inspire job/hpc/serving metrics` 同 UX 同 flag |

### 2.2 GPU 多节点任务 (`job`)

> `inspire job` 不等于"训练任务"——**凡是 GPU 上的多节点并行工作负载都走这里**：分布式训练、批量推理、并发单节点 worker pool（shard 各跑一份）同样合用。与 `inspire hpc` 的区别是资源形态：`job` = GPU，`hpc` = CPU。

| 命令 | 用途 |
| --- | --- |
| `inspire job create` | 精细提交。`--priority` 与直觉相反：**`1` = LOW，`9` = HIGH**。提交后**立即** `inspire --json job status <id>` 核对返回的 `priority_level`；若仍是 `LOW` 就 `job stop`，用更高值重提。 |
| `inspire run "<cmd>" [--watch]` | 快速提交：自动选资源组 + 提交；`--watch` 自动跟 `job logs --follow` |
| `inspire job status <id>` | 权威状态（高优 / 低优 / 调度结果） |
| `inspire job logs <id>` | **优先走 SSH tunnel fast path**；无 tunnel 回退其它通道 |
| `inspire job events <id>` | Job-level 事件；`--instance <pod>` 切 per-pod（能看到调度失败的具体节点原因）。`--type` / `--reason` / `--tail` / `--from-cache` 可组合。调度失败优先看 `--instance <pod>` |
| `inspire job stop <id>` | 规格 / 优先级 / 命令提错时立即止损 |
| `inspire job delete <id> [--yes]` | 永久删条目（清理废弃任务）。running 的要先 `stop`；`-y` 跳过确认 |
| `inspire job metrics <id>` | 训练任务的历史利用率曲线，按 `worker-0..N-1` 分开画。stdout 的 `spread=X%` 反映 worker 间离散度——spread 大说明有 worker 掉队 / 通信 hang / 数据加载不均，是多节点训练健康监测的核心指标；正常训练 spread 通常 < 5%。flag 和输出语义见 `inspire notebook metrics` |

### 2.3 HPC（Slurm）

> 提交前先 `resources specs --usage hpc --workspace <ws> --group <group> --json` 拿 `predef_quota_id` / `cpu_count` / `memory_size_gib`。

| 命令 | 用途 |
| --- | --- |
| `inspire hpc create` | 四条约束：（1）`-c` **只写 Slurm 正文**，平台自动补 `#SBATCH` 头，正文程序必须**显式 `srun`** 启动；（2）`--compute-group "<name>"` 按 name 传（如 `"HPC-可上网区资源-2"`，从 `inspire config context` 的 `compute_groups[]` 抄）；（3）`--cpus-per-task` / `--memory-per-cpu` 超规格**静默排队不报错**——CLI 会根据这对参数自动从平台查 `spec_id`，提交前实查 `cpu_count` / `memory_size_gib`；（4）`--image` 必须是**完整 Docker 地址**且带可用 Slurm 环境，通用基底 `docker.sii.shaipower.online/inspire-studio/unified-base:v1`；`--image-type` 通常 `SOURCE_PRIVATE` 或 `SOURCE_PUBLIC`。 |
| `inspire hpc status <id>` | 看 `status` / `priority_level` / `running_time_ms` / `finished_at`。**HPC 常见"假成功"**：`status=SUCCEEDED` 但 payload 实际没跑（entrypoint 早退、srun 命令语法错、shell 变量丢失等）都能返回 SUCCEEDED。不要只信 status——每次新 entrypoint 必须写一个**独一无二的 fingerprint 到共享存储**（例如 `/inspire/<tier>/project/<topic>/<user>/.../probe-<nonce>.log`），再从同项目 notebook `cat` 回验。提交后 `slurm_cluster_spec.nodes` 在 RUNNING 时应非空；SUCCEEDED 后平台会把它清成 `[]`，这不是坏信号。CREATING 卡住或 RUNNING 时 `nodes=[]` 才是坏信号（详见 troubleshooting.md） |
| `inspire hpc list` | 当前 workspace 内所有创建者的任务 |
| `inspire hpc events <id>` | 平台 Slurm 控制器事件（`Created/DeletedSlurmCluster` 等）；`--reason` / `--tail` / `--from-cache` 可组合。**HPC 不暴露 per-pod 事件**，只有 job-level |
| `inspire hpc stop <id>` | 发现提错立即止损 |
| `inspire hpc delete <id> [--yes]` | 永久删条目（清理废弃任务）。running 的要先 `stop`；`-y` 跳过确认 |
| `inspire hpc metrics <id>` | HPC 任务的历史利用率曲线，每个 slurm pod 一条（CPU / Memory / Disk / Network 为主）。除了和 `job metrics` 一样看 spread 判卡住，还能反向诊断"假成功"——`SUCCEEDED` 但曲线全 0 = entrypoint 根本没跑。flag 和输出语义见 `inspire notebook metrics` |

**平台自动注入的 Slurm 头**（不用自己写）：

```bash
#!/bin/bash
#SBATCH -o /hpc_logs/slurm-%j.out
#SBATCH -e /hpc_logs/slurm-%j.err
#SBATCH --ntasks=*
#SBATCH --cpus-per-task=*
#SBATCH --mem=*G
#SBATCH --time=*

## Insert code, and run your programs here (use `srun`).
```

### 2.4 Ray（弹性计算）

> 一个 head + 若干 worker 组，每组实例数按实时负载在 `min_replicas` / `max_replicas` 之间自动扩缩。**和 `job` / `hpc` 的关键差异**：driver 不主动退出集群就不停，worker 一直按 `min_replicas` 占配额。选型：流式 / 异构 worker / 长守护走 Ray；固定规模批处理走 `job` / `hpc`。

| 命令 | 用途与约束 |
| --- | --- |
| `inspire ray create` | 提交 Ray 任务。`-c <cmd>` 是 driver 命令；`--head-image/-group/-spec[/-shm]` 定义 head；重复 `--worker 'name=<g>,image=<URL>,group=<compute_group>,spec=<quota>,min=<n>,max=<n>[,shm=<gib>][,image_type=SOURCE_PUBLIC\|SOURCE_PRIVATE\|SOURCE_OFFICIAL]'` 定义每个 worker 组。`-p` 项目，`--workspace` workspace。`--dry-run` 打印将要提交的 body；`--json-body <file>` 用准备好的 body 整体提交 |
| `inspire ray list [-A] [--created-by user-xxx,...] [--workspace ...]` | 列 Ray 任务。默认只列当前用户的（对齐 Web UI "我的"），`-A` 列所有人 |
| `inspire ray status <id>` | 单任务状态。纯文本只打顶层字段；`inspire --json ray status <id>` 才看得到 head / worker 规格 + 每组的 `min_replicas`/`max_replicas`/`current_replicas` |
| `inspire ray events <id> [--tail N] [--reason R] [--type Normal\|Warning]` | Job-level 事件流。**卡 PENDING 时第一时间看这个**，message 会直接写明调度失败原因（CPU 不够 / node affinity 不匹配 / 镜像拉不下 / taint 等） |
| `inspire ray instances <id>` | pod 级视图：head + 每个 worker 组的实际 pod 状态（`pending / running / ...`）。`events` 说调度失败时看这里定位是哪一个 pod |
| `inspire ray stop <id>` | 停掉运行中的集群；worker 全部回收，条目留在 list 里 |
| `inspire ray delete <id> [--yes]` | 永久删条目。终态清理用；running 的先 `stop` 再 `delete` |

**提交示例**（纯 CPU pipeline，默认 workspace 下就能跑；要 GPU 混合 worker 见"Ray 特有坑"）：

```bash
inspire ray create \
  -n av-pipeline \
  -c 'python driver.py --mode run_and_exit' \
  --head-image docker.sii.shaipower.online/inspire-studio/unified-base:v2 \
  --head-group CPU资源-2 --head-spec <head_ray_quota_id> \
  --worker 'name=decode,image=docker.sii.shaipower.online/inspire-studio/unified-base:v2,group=CPU资源-2,spec=<worker_ray_quota_id>,min=1,max=8,shm=32' \
  -p <project> --workspace CPU资源空间
```

### 2.5 镜像

| 命令 | 用途 |
| --- | --- |
| `inspire image list --source {public,private,all}` | 浏览；`private` = UI 里"个人可见镜像"；`all` 聚合去重 |
| `inspire image save <notebook_id>` | 从运行中实例保存为镜像。`--public` / `--private` 指定可见性（缺省走平台默认，通常 private）；CLI 会把请求发给 `/mirror/save` 再用 `/image/update` 兜一次确保生效。image_id 未在响应中返回时自动回查 `--source private` 解析 |
| `inspire image set-visibility <image_id> --public\|--private` | 翻转已有自定义镜像的可见性（内部走 `/image/update`） |
| `inspire image register` | 注册外部镜像；优先 `--method address` |
| `inspire image set-default --job <url> --notebook <url>` | 设默认镜像。没有位置参数，只接受 `--job` / `--notebook`；写回最近的项目级 `.inspire/config.toml` |

### 2.6 资源 / 项目 / 配置查询

| 命令 | 用途 |
| --- | --- |
| `inspire resources list` | 实时可用量（GPU 默认；`--all --include-cpu` 看全量） |
| `inspire resources nodes` | 整节点空余，多节点任务前必查 |
| `inspire resources specs --usage {hpc,notebook,auto}` | 规格表；`hpc create --spec-id` 填这里的 `predef_quota_id` |
| `inspire project list` | 项目和配额，定高 / 低优策略前必看 |
| `inspire user whoami` | 当前登录人身份 / 角色 |
| `inspire user permissions [--workspace X]` | workspace 下授予的权限码（如 `job.trainingJob.create`） |
| `inspire config show [--compact] [--json]` | 查扁平配置（平台身份 / 代理 / 默认镜像 / 路径），含来源 |
| `inspire config context [--json]` | 查活动账号 / project / workspace alias / compute_groups |
| `inspire account add/list/use/current/remove` | 多账号管理，一账号一目录（`~/.inspire/accounts/<name>/`） |

> 较少使用 / 权限受限的命令（`serving *` / `model *` / `project detail|owners` / `user quota|api-keys`）见 [references/less-used-commands.md](references/less-used-commands.md)。

## 3. 开发主流程

### 远端路径：四种根

GPFS 挂载点按"属于哪个项目"和"个人 vs 共享"交叉出四种根：

| 根 | 路径样例 | 定位 |
| --- | --- | --- |
| 项目-个人 | `/inspire/<tier>/project/<topic>/<user>/…` | 每项目-每用户一份。代码仓库、脚本、小配置、调试输出。`<user>/` 下可自建任意层级（命名空间、多仓库工作区），**不一定直接跟 `<repo>`** |
| 项目-公共 | `/inspire/<tier>/project/<topic>/public/…` | 项目成员共享。大数据集、权重、批量结果、checkpoint。子树由项目自定（按 owner / 子项目分层） |
| 全局-公共 | `/inspire/hdd/global_public/…` | **仅 hdd 有**。全平台所有用户共享。实测 ~250 TB fileset。适合放可能被多个项目复用的通用数据集、基础镜像素材；不要放个人中间产物 |
| 全局-个人 | `/inspire/hdd/global_user/<user>/…` | **仅 hdd 有**。跨所有项目的个人盘。fileset 级容量很大，但**平台侧对单用户有 quota**（比项目盘紧很多），适合放脚本、配置、个人小工具，不适合堆训练数据 / checkpoint |

> **`global_*` 只存在于 hdd**——`/inspire/ssd/`、`/inspire/qb-ilm/`、`/inspire/qb-ilm2/` 都没有 `global_public` / `global_user`，要 SSD 或 qb-ilm 速度就只能走"项目-个人"或"项目-公共"路径。

**先决策放"项目-个人 / 项目-公共 / 全局-个人 / 全局-公共"（按作用域），再选存储池（按冷热）。** 这俩正交。每个项目下 `<user>/` 和 `public/` 的具体子树结构由项目自定，在仓库根的 `INSPIRE.md` 里 `Path Conventions` / `Public Directory Layout` 两节记下来。

### 存储池选择

四条池并列（都是 GPFS fileset，`df` 能看到 fileset-scoped quota）：

| 池 | 项目路径前缀 | 全局路径 | 定位 |
| --- | --- | --- | --- |
| SSD (`gpfs_flash`) | `/inspire/ssd/project/<topic>/` | 无 | 训练 hot path、活跃工作集、checkpoint 热点 |
| HDD (`gpfs_hdd`) | `/inspire/hdd/project/<topic>/` | `/inspire/hdd/global_public/` + `/inspire/hdd/global_user/<user>/` | 通用；项目 fileset 经常 100% 满，新写前先 `df` 看 Avail |
| qb-ilm (`qb_prod_ipfs01`) | `/inspire/qb-ilm/project/<topic>/` | 无 | 大容量，顺序读带宽和 SSD 相当 |
| qb-ilm2 (`qb_prod_ipfs02`) | `/inspire/qb-ilm2/project/<topic>/` | 无 | 最新也最空，新增数据默认往这里落最安全 |

`inspire init --discover` 设 `[paths].target_dir` 前交互式让你选层（catalog 建议 `hdd` 时默认切到 `ssd`，避免继承坏默认）。

### 代码与文件流转

| 场景 | 做法 |
| --- | --- |
| 多仓库工作区 | `INSPIRE_TARGET_DIR` 设为 `<user>/` 下自建的工作区根（可含命名空间 / 多仓库聚合层），里面并列放多个 repo |
| 独立 repo 日常 | 本地 `git push` → `notebook exec` → 远端 `git pull` |
| 目标实例在离线计算组但共享路径可见 | 切到同一路径下的可上网区实例做 git |
| 非 Git 文件 | `notebook scp`，远端路径写绝对 |

**日常闭环**（`<workspace-subpath>` 是你在 `<user>/` 下自建的层级，可能含命名空间 / 聚合层）：

```bash
export INSPIRE_TARGET_DIR=/inspire/ssd/project/<topic>/<user>/<workspace-subpath>

cd /local/path/<repo>
git push origin <branch>
inspire notebook exec "cd <repo> && git pull && git log -1 --oneline"

# 查资源空余 / 规格
inspire resources list --all --include-cpu
inspire resources specs --workspace CPU资源空间 --group HPC-可上网区资源-2 --usage hpc --json

# 建 alias 并复用
inspire notebook ssh <notebook-id> --save-as mybox
inspire notebook exec --alias mybox "hostname"
```

### 阶段 A：CPU 空间起基底 notebook

**镜像选型先做一次判断**：
- **已有项目 / 个人镜像**（装好 uv / nvm / cargo / java / transformer-engine 等环境、环境变量、编译产物）→ **直接复用那个镜像**。缺 `sshd` / `rtunnel` 时 bootstrap 会自动补：`sshd` 走 `apt-get install -y openssh-server`，`rtunnel` 优先用容器里预装的二进制（`command -v rtunnel`），其次在容器有公网时 `curl` 下载。从零重装环境代价很高（transformer-engine 编译、各种 runtime 初始化），**不要**为了统一用 `unified-base` 而丢弃现有镜像；首次建镜像时在可上网区把 rtunnel 下载进镜像后 `inspire image save` 派生，之后所有 notebook 复用这个镜像即可，无论有无公网都能直接 SSH。
- **完全从零起 / 临时脚手架** → `docker.sii.shaipower.online/inspire-studio/unified-base:v1`（Ubuntu 22.04 + slurm 运行环境 + sshd + rtunnel 一体）是一个省事的起点。hpc 提交时平台正常注入 slurm controller，容器内 `srun`/`sbatch`/`sshd`/`rtunnel` 全部可用（slurm 仅在 `inspire hpc create` 路径下生效，普通 notebook 里 slurm 命令会因为无 controller 而报 `Could not establish a configuration source`——这是平台设计，不是镜像问题）。要加一层项目依赖就在这镜像上面 `inspire image save` 再派生。

资源组必须用 `HPC-可上网区资源-2`（§0 硬约束：其它 CPU 计算组只能建 notebook，但那边没公网没法 apt install）。

```bash
inspire notebook create \
  --workspace CPU资源空间 --group HPC-可上网区资源-2 -r 20CPU \
  --name <action-goal-name> \
  --image docker.sii.shaipower.online/inspire-studio/unified-base:v1 \
  --project <project-id-or-alias> --wait --json

inspire notebook ssh <notebook_id> --command "echo ssh-ok"   # fast：sshd 已在镜像里
inspire notebook ssh <notebook_id> --save-as cpu-box
```

想在此基础上加项目依赖（DeepSpeed / 训练栈 / ……）并发布：

```bash
inspire notebook exec --alias cpu-box "apt-get update && apt-get install -y <deps> && pip install ..."
inspire image save <notebook_id> -n <name>-base -v v1 --public --wait --json
inspire image set-default \
  --job docker.sii.shaipower.online/inspire-studio/<name>-base:v1 \
  --notebook docker.sii.shaipower.online/inspire-studio/<name>-base:v1
```

### 阶段 B：CPU 空间跑数据处理

一份离线 workload 在 CPU 空间怎么跑，两条路径并列选一条。计算组选型都遵守 §0：跑 HPC / 想吃公网的都只能用 `HPC-可上网区资源-2`；纯离线的批处理放其它 `CPU资源-*` 计算组也行。**无论哪条路径，小规模 probe 通过 ≠ 正式规模稳定**——放大量级 / 并发后必须再跑一次接近正式规模的验证。

**什么时候选哪条**

| 形态 | 选 HPC（Slurm） | 选 Ray（弹性计算） |
| --- | --- | --- |
| 任务边界 | 有明确的开始 / 结束，一批数据跑完就收 | 长时间流式，数据持续进 / 持续出 |
| 并发模型 | 固定 `--number-of-tasks × --instance-count`，MPI / srun 管调度 | head + 多组 worker，每组按 `min/max` 自动伸缩 |
| 数据流 | 通常读 GPFS → 处理 → 写回 GPFS（阶段间落盘） | worker 组之间走内存（Ray 对象存储），不落盘 |
| CPU / GPU 混用 | 单一节点类型（全 CPU 或全 GPU） | 异构——一个任务同时挂 CPU 预处理组 + GPU 推理组 |
| 结束条件 | `srun` 退出 → 自动 SUCCEEDED | driver 主动 `exit` → 自动结束；driver 常驻 → 手动 `ray stop` |
| CLI 提交 | `inspire hpc create ...`（本节下面的例子） | `inspire ray create ...`（本节下面的例子；wire 契约见 §2.4） |

**路径一：Slurm HPC 批处理**——一次性、固定并发的预处理（数据清洗、特征抽取、format 转换等）首选这条。

```bash
ENTRYPOINT=$(cat <<'EOF'
set -euo pipefail
srun bash -lc 'python preprocess.py'
EOF
)

inspire hpc create \
  -n <name>-hpc-preprocess \
  -c "$ENTRYPOINT" \
  --compute-group HPC-可上网区资源-2 \
  --workspace CPU资源空间 \
  --cpus-per-task <N> --memory-per-cpu <M> \
  --number-of-tasks 1 --instance-count 1 \
  --project <project> \
  --image docker.sii.shaipower.online/inspire-studio/<image>:<ver> \
  --image-type SOURCE_PRIVATE
# --spec-id 省略 — CLI 按 (compute-group, cpus-per-task, memory-per-cpu) 自动匹配预定义规格
```

**路径二：Ray 弹性 pipeline**——长时间、流式、worker 按负载自动伸缩的任务走这条。默认 workspace 范围内只能跑**纯 CPU Ray**（计算组用 `CPU资源` / `CPU资源-2`，`HPC-可上网区资源-2` 不支持 Ray）。GPU-Ray 所在的 workspace 不是默认范围，需要自行加 harness 级 SKILL 覆盖。

```bash
inspire ray create \
  -n <name>-ray-pipeline \
  -c 'python driver.py --mode run_and_exit' \
  --head-image docker.sii.shaipower.online/inspire-studio/unified-base:v2 \
  --head-group CPU资源-2 --head-spec <head_quota_id> \
  --worker 'name=w1,image=docker.sii.shaipower.online/inspire-studio/unified-base:v2,group=CPU资源-2,spec=<worker_quota_id>,min=1,max=8,shm=32' \
  -p <project> --workspace CPU资源空间

# 改之前先打印 body 核对；或者拿一份已知 good body 直接提交
inspire ray create ... --dry-run > body.json
inspire ray create --json-body body.json

# 日常运维
inspire --json ray status <id>    # 看 head/worker 规格 + 实际扩缩
inspire ray events <id>           # 卡 PENDING / 诊断调度失败的第一手
inspire ray instances <id>        # pod 级状态
inspire ray stop <id>             # driver 常驻必须手动停
inspire ray delete <id> --yes     # 终态清理
```

**Ray 使用约束**：
- 镜像必须带 Ray runtime：**用 `docker.sii.shaipower.online/inspire-studio/unified-base:v2`**（v1 不带，head 容器会 BackOff）。自制镜像验证：SSH 进 notebook 跑 `ray start --head --num-cpus=1 --disable-usage-stats && ray stop` 能干净起停就算 OK。
- `--head-spec` / `--worker spec=` 填的是 **Ray 专属 quota_id**，和 notebook / HPC 的规格是不同表。`inspire resources specs` 目前不列 Ray 专属规格——**最稳的拿法**：从同 workspace 里任意一个已有 Ray 任务 `inspire --json ray status <id>` 读 `head_node.quota_id` / `worker_groups[].quota_id` 复用。
- `min` / `max` 都必须 ≥ 1，没有"闲时缩到 0"。
- driver 不 `sys.exit()` 就一直在，占着 `min_replicas` 的配额——长守护任务要接受"手动 `ray stop`"的运维模型。

### 阶段 C：分布式训练空间

| 主题 | 做法 |
| --- | --- |
| 前置 | 依赖 / 权重 / 数据集**先在可上网空间下载到共享存储**，再进训练空间 |
| 资源申请 | `cuda12.8版本H100` / `H200-1/2/3号机房` 空余充足时按真实需求申请，不要惯性缩量 |
| 单节点调试 | 先开小实例做 `nvidia-smi` 和交互式排障 |
| 多节点训练 | `job create` 精细控制；`run "<cmd>" --watch` 快速提交 + 直接跟日志 |

```bash
# 单节点调试
inspire notebook create --workspace 分布式训练空间 --resource 1xH100 \
  --name <name>-gpu-debug --image <base-image> --project <project-id-or-alias> --wait --json
inspire notebook ssh <id> --command "nvidia-smi"

# 多节点训练：精细控制
inspire job create -n <name>-train -r 8xH100 --nodes 2 \
  -c 'bash train.sh' --workspace 分布式训练空间 --location 'cuda12.8版本H100' --image '<ref>'

# 多节点训练：快速提交
inspire run 'bash train.sh' --gpus 8 --type h100 --nodes 2 \
  --workspace 分布式训练空间 --location 'cuda12.8版本H100' --image '<ref>' --watch
```
