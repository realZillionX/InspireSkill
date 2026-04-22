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
| 配置查询 | **不要直接读** `~/.config/inspire/config.toml` 或 `./.inspire/config.toml`；两层合并由 CLI 负责。扁平字段 `inspire config show [--json] [--compact]`；活动 `[context]` / 项目 / workspace alias / compute_groups / accounts 用 `inspire config context [--json]`。 |
| 项目叙述上下文 | 项目仓库根下用 **`INSPIRE.md`** 写非配置性上下文。建议五节：`Default Image`（config.toml 未托管的镜像，如 base / HPC 专用）· `Path Conventions`（本地与远端路径派生规则）· `Public Directory Layout`（`public/` 下的共享结构）· `Existing Notebooks`（角色 → ID）· `Ongoing Jobs`（当前长期在跑的任务）。**不**把 config.toml 内容复制进来。`AGENTS.md` / `CLAUDE.md` / `GEMINI.md` 只放通用工程事项。 |
| `--json` 位置 | 全局 `--json` **必须放子命令前**：`inspire --json hpc status <id>`。 |
| Debug | `inspire --debug` 把脱敏日志写进 `~/.cache/inspire-skill/logs/`。 |
| 废弃资源清理 | 别让废弃 notebook / job / hpc 堆积污染 Web UI 列表。终态（`SUCCEEDED` / `FAILED` / `STOPPED` / `CANCELLED`）且确认不再需要时就 `delete`；批量用 `inspire --json <res> list -A` 过滤再逐个 `delete --yes`。running 的先 `stop` 再 `delete`；不确定是否还有人用时跳过，不要猜着删。 |

## 2. 命令速查

### 2.1 Notebook（生命周期 + 远程操作 + alias 管理）

> **一个 notebook ↔ 一条本地 alias**。首次 `notebook ssh <id>` 引导 SSH 时把连接存成 alias（默认 `nb-<id 前 8 位>`，`--save-as` 可改名）。`notebook ssh <arg>` 多态——arg 是 id 就 bootstrap，是已保存 alias 就重连（自动重建断开的 tunnel）。

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
| `inspire notebook ssh <id> --rtunnel-upload-policy {auto,never,always}` | 控制 rtunnel 上传；已有同版本按 `.sha256` sidecar 复用；`exec format error` 是架构不对 |
| `inspire notebook exec <alias> "<cmd>"` | 远端 `INSPIRE_TARGET_DIR` 下执行；对 notebook-backed alias 可自动重建断开的 tunnel |
| `inspire notebook shell [<alias>]` | 交互式 SSH shell |
| `inspire notebook scp <src> <dst>` | 传**非仓库**文件。**不是** repo 同步——源码走本地 `git push` + `notebook exec` 远端 `git pull`。不继承 `INSPIRE_TARGET_DIR`，远端写绝对路径 |
| `inspire notebook test [<alias>]` | 连通性测试（带耗时）；排障首选 |
| `inspire notebook refresh <alias>` | 刷新 alias 连接（notebook 换实例 / 重启后） |
| `inspire notebook connections` | 列本地已保存 alias |
| `inspire notebook forget <alias>` | 删本地 alias 记录（不影响平台上的 notebook） |
| `inspire notebook set-default <alias>` | 设默认 alias |
| `inspire notebook ssh-config --install` | 把所有 alias 写进 `~/.ssh/config`，之后 `ssh <alias>` / `scp` / `rsync` / `git` 原生用 |
| `inspire notebook top` | alias 实例的 GPU 利用率；`--watch` 持续刷新 |

### 2.2 GPU 多节点任务 (`job`)

> `inspire job` 不等于"训练任务"——**凡是 GPU 上的多节点并行工作负载都走这里**：分布式训练、批量推理、并发单节点 worker pool（shard 各跑一份）同样合用。与 `inspire hpc` 的区别是资源形态：`job` = GPU，`hpc` = CPU。

| 命令 | 用途 |
| --- | --- |
| `inspire job create` | 精细提交。`--priority` 与直觉相反：**`1` = LOW，`9` = HIGH**。提交后**立即** `inspire --json job status <id>` 核对返回的 `priority_level`；若仍是 `LOW` 就 `job stop`，用更高值重提。 |
| `inspire run "<cmd>" [--watch]` | 快速提交：自动选资源组 + 提交；`--watch` 自动跟 `job logs --follow` |
| `inspire job status <id>` | 权威状态（高优 / 低优 / 调度结果） |
| `inspire job logs <id>` | **优先走 SSH tunnel fast path**；无 tunnel 回退其它通道 |
| `inspire job events <id>` | **Job-level** K8s 事件；`--instance <pod>` 切 **per-pod**（scheduler / kubelet 视角，含 `FailedScheduling` 具体节点诊断）。`--type` / `--reason` / `--tail` / `--from-cache` 可组合。调度失败先看 `--instance <pod>` 里的 scheduler reason |
| `inspire job stop <id>` | 规格 / 优先级 / 命令提错时立即止损 |
| `inspire job delete <id> [--yes]` | 永久删条目（清理废弃任务）。running 的要先 `stop`；`-y` 跳过确认 |

### 2.3 HPC（Slurm）

> 提交前先 `resources specs --usage hpc --workspace <ws> --group <group> --json` 拿 `predef_quota_id` / `cpu_count` / `memory_size_gib`。

| 命令 | 用途 |
| --- | --- |
| `inspire hpc create` | 五条约束：（1）`-c` **只写 Slurm 正文**，平台自动补 `#SBATCH` 头，正文程序必须**显式 `srun`** 启动；（2）`--spec-id` 填 **`predef_quota_id`**（不是 notebook 的 `quota_id`）；（3）`--cpus-per-task` / `--memory-per-cpu` 超规格**静默排队不报错**，提交前实查 `cpu_count` / `memory_size_gib`；（4）`--image` 必须是**完整 Docker 地址**且带可用 Slurm 环境，通用基底 `docker.sii.shaipower.online/inspire-studio/slurm-dev:0.0.0`；（5）`--image-type` 通常 `SOURCE_PRIVATE` 或 `SOURCE_PUBLIC`。 |
| `inspire hpc status <id>` | 看 `slurm_cluster_spec.predef_quota_id` / `priority_level` / `steps`；`steps=-/0` 或 `nodes=[]` 是坏信号（详见 troubleshooting.md） |
| `inspire hpc list` | 当前 workspace 内所有创建者的任务 |
| `inspire hpc events <id>` | 平台 Slurm 控制器事件（`Created/DeletedSlurmCluster` 等）；`--reason` / `--tail` / `--from-cache` 可组合。**HPC 不暴露 per-pod 事件**，只有 job-level |
| `inspire hpc stop <id>` | 发现提错立即止损 |
| `inspire hpc delete <id> [--yes]` | 永久删条目（清理废弃任务）。running 的要先 `stop`；`-y` 跳过确认 |

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

### 2.4 镜像

| 命令 | 用途 |
| --- | --- |
| `inspire image list --source {public,private,all}` | 浏览；`private` = UI 里"个人可见镜像"；`all` 聚合去重 |
| `inspire image save <notebook_id>` | 从运行中实例保存为镜像；CLI 总会解析出 `image_id`（平台响应缺字段时自动回查 `--source private`） |
| `inspire image register` | 注册外部镜像；优先 `--method address` |
| `inspire image set-default --job <url> --notebook <url>` | 设默认镜像。没有位置参数，只接受 `--job` / `--notebook`；写回最近的项目级 `.inspire/config.toml` |

### 2.5 资源 / 项目 / 配置查询

| 命令 | 用途 |
| --- | --- |
| `inspire resources list` | 实时可用量（GPU 默认；`--all --include-cpu` 看全量） |
| `inspire resources nodes` | 整节点空余，多节点任务前必查 |
| `inspire resources specs --usage {hpc,notebook,auto}` | 规格表；`hpc create --spec-id` 填这里的 `predef_quota_id` |
| `inspire project list` | 项目和配额，定高 / 低优策略前必看 |
| `inspire user whoami` | 当前登录人身份 / 角色 |
| `inspire user permissions [--workspace X]` | workspace 下授予的权限码（如 `job.trainingJob.create`） |
| `inspire config show [--compact] [--json]` | 查扁平配置（账号 / 代理 / 默认镜像 / 路径），含来源 |
| `inspire config context [--json]` | 查 `[context]` + project / workspace alias + compute_groups + accounts |

> 较少使用 / 权限受限的命令（`serving *` / `model *` / `project detail|owners` / `user quota|api-keys`）见 [references/less-used-commands.md](references/less-used-commands.md)。

## 3. 开发主流程

### 远端路径：个人目录 vs 项目公共目录

每个项目在每条存储池下都是固定三级前缀 `/inspire/<tier>/project/<topic>/`，下一级只有两种根：

| 根 | 定位 |
| --- | --- |
| `<user>/…` | **个人目录根**（每用户一份）：代码仓库、脚本、小配置、少量调试输出。下面可自建任意层级（命名空间、多仓库工作区等），**`<user>/` 不一定直接跟 `<repo>`** |
| `public/…` | **项目公共目录根**（项目成员共享）：大体积数据集、权重、批量结果、checkpoint。下面按项目约定自组织（常见按 owner / 子项目分层） |

**先决策放 `<user>/` 还是 `public/`（按"个人 vs 共享"），再选存储池（按冷热）。** 这俩正交，两步都要做。`<user>/` 和 `public/` 下的具体子树结构由项目自定，在仓库根的 `INSPIRE.md` 里 `Path Conventions` / `Public Directory Layout` 两节记下来。

### 存储池选择

四条池并列（都是 GPFS fileset，`df` 能看到 fileset-scoped quota）：

| 池 | 路径前缀 | 定位 |
| --- | --- | --- |
| SSD (`gpfs_flash`) | `/inspire/ssd/project/<topic>/` | 训练 hot path、活跃工作集、checkpoint 热点 |
| HDD (`gpfs_hdd`) | `/inspire/hdd/project/<topic>/` | 通用；项目 fileset 经常 100% 满，新写前先 `df` 看 Avail |
| qb-ilm (`qb_prod_ipfs01`) | `/inspire/qb-ilm/project/<topic>/` | 大容量，顺序读带宽和 SSD 相当 |
| qb-ilm2 (`qb_prod_ipfs02`) | `/inspire/qb-ilm2/project/<topic>/` | 最新也最空，新增数据默认往这里落最安全 |

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

### 阶段 A：CPU 空间准备基础镜像（带 sshd + rtunnel）

推荐用 `HPC-可上网区资源-2`，默认镜像 `docker.sii.shaipower.online/inspire-studio/base:20250920`。

```bash
inspire resources specs --workspace CPU资源空间 --group HPC-可上网区资源-2 --json

inspire notebook create \
  --workspace CPU资源空间 --resource 4CPU \
  --name <action-goal-name> \
  --image docker.sii.shaipower.online/inspire-studio/base:20250920 \
  --project <project-id-or-alias> --wait --json

inspire notebook ssh <notebook_id> --command "echo ssh-ok"   # 自动 bootstrap，失败见 troubleshooting.md
inspire notebook ssh <notebook_id> --save-as cpu-box         # 保存 alias

# 保存基础镜像并设默认值
inspire image save <notebook_id> -n <name>-base -v v1 --json
inspire image set-default \
  --job docker.sii.shaipower.online/inspire-studio/<name>-base:v1 \
  --notebook docker.sii.shaipower.online/inspire-studio/<name>-base:v1
```

### 阶段 B：CPU 空间跑 HPC 数据处理

计算组选型见 §0（只能用 `HPC-可上网区资源-2`）。**小规模 probe 通过 ≠ 正式规模稳定**：放大量级 / 并发后必须再跑一次接近正式规模的验证。

```bash
ENTRYPOINT=$(cat <<'EOF'
set -euo pipefail
srun bash -lc 'python preprocess.py'
EOF
)

inspire hpc create \
  -n <name>-hpc-preprocess \
  -c "$ENTRYPOINT" \
  --logic-compute-group-id <id> --spec-id <predef_quota_id> \
  --workspace CPU资源空间 \
  --cpus-per-task <N> --memory-per-cpu <M> \
  --number-of-tasks 1 --instance-count 1 \
  --project <project-id-or-alias> \
  --image docker.sii.shaipower.online/inspire-studio/<image>:<ver> \
  --image-type SOURCE_PRIVATE
```

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
