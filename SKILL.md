---
name: inspire
description: "Execution-first Inspire platform playbook for agents driving the inspire CLI as a black-box tool, covering notebook lifecycle, remote-exec/SSH workflows, image and resource ops, job and HPC submission, proxy routing, and failure recovery."
---

# Inspire Skill

> **定位**：用 `inspire` 命令完成启智平台全流程操作。Agent 把 CLI 当黑盒直接用，不要读源码。命令失败先看 [references/troubleshooting.md](references/troubleshooting.md)；代理配置见 [references/proxy-setup.md](references/proxy-setup.md)；次级命令见 [references/less-used-commands.md](references/less-used-commands.md)。

## 1. 必读约束

### 1.1 平台硬约束（CLI 看不出来的事实）

| 主题 | 约束 |
| --- | --- |
| 资源申请 | **切勿保守**。先 `resources list --all --include-cpu` / `resources nodes` / `resources specs` 查实时空余，按真实需求申请（20 张还是 500 张 GPU 都行），只有调度语义 / 项目配额 / 实时空余明确不足时才降档。 |
| 代理 | 公网与 `*.sii.edu.cn` 需**同时可达**。任意覆盖这两段的代理方案都行（仓库提供可选的 Clash Verge `7897` 分流模板，见 `references/proxy-setup.md`）。 |
| 优先级反直觉 | `--priority` **`1` = LOW，`9` = HIGH**。LOW 会被 HIGH 强制抢占，必须高频 checkpoint。提交后**立即** `inspire --json <res> status <name>` 核对 `priority_level`；仍是 LOW 就 `stop` + 更高值重提。 |
| HPC 规格余量 | 平台自身吃 `0.3` 核 CPU + `384 MB` 内存，应用层并发压到 **`cpus-per-task - 4`** 或更低。 |
| CPU 空间唯一 hpc 组 | `CPU 资源空间` 下**只有 `HPC-可上网区资源-2`** 支持 `inspire hpc create`；其它组只能建 notebook。另外该组的 `500GB` 规格运维未配好，提交**静默排队**——真需要 500GB 内存就退化成在 `CPU资源-2` 开 notebook 交互跑。 |
| 项目-实例挂载隔离 | 实例只挂**自身所在项目**的 fileset；其它项目的 `/inspire/{hdd,ssd,qb-ilm,qb-ilm2}/project/<others>/` 路径在该实例里**根本不存在**（`ls` 报 `No such file`）——不是权限问题。访问项目 `<X>` 的存储必须在 `project=<X>` 的实例里。 |
| 跨项目 cp 要 root | `notebook scp` / `exec cp` / 单账号 CLI 都做不到，去**飞书项目群**找管理员。 |
| SSH bootstrap | `inspire notebook ssh <name>` 对**任何镜像 / 计算组 / 有无公网都能直接 ssh**，无需在镜像里预装。冷启时间贵就 `image save` 派生一份固化，否则用完即弃（notebook 停掉痕迹全没）。 |

### 1.2 通用规则

| 主题 | 规则 |
| --- | --- |
| 账号 | 一账号一独立目录 `~/.inspire/accounts/<name>/`（装 `config.toml` / `bridges.json` / `web_session.json`），活动账号写在 `~/.inspire/current`。无活动账号时 CLI 直接报错，没全局 fallback。切账号 = 改一个文件。 |
| 默认 workspace 范围 | 本 SKILL 只把 **`CPU 资源空间`** + **`分布式训练空间`** 视为一等公民。其它 workspace（`整节点任务空间` / `CI-情境智能*` / `可上网GPU资源` / `专属资源开发空间` 等）是课题组专属或沙箱，**别主动往里塞任务**；需要时由仓库级 `INSPIRE.md` 覆盖层指定。 |
| `--json` 位置 | 全局 `--json` **必须放子命令之前**：`inspire --json hpc status <name>`。 |
| Debug | `inspire --debug <cmd>` 把脱敏日志写进 `~/.cache/inspire-skill/logs/`。 |
| 查配置 | **别直接读** `~/.inspire/accounts/<name>/config.toml` 或 `./.inspire/config.toml`；合并由 CLI 负责。扁平字段用 `inspire config show [--compact --json]`，活动账号 / 项目 / workspace alias / compute_groups 用 `inspire config context [--json]`。 |
| 项目叙述上下文 | 仓库根下用 **`INSPIRE.md`** 写非配置性上下文。建议五节：`Default Image` · `Path Conventions` · `Public Directory Layout` · `Existing Notebooks`（角色 → ID） · `Ongoing Jobs`。**不**把 config.toml 内容复制进来。`AGENTS.md` / `CLAUDE.md` / `GEMINI.md` 只放通用工程事项。 |
| 排错第一步 | 任务卡 PENDING / CREATING 超预期，或 FAILED 原因不明，**第一步永远是 `inspire <res> events <name>`**（`notebook` / `job` / `hpc` / `ray` 都有）。`job` / `ray` 叠 `--instance <pod>` 看 per-pod 调度原因。不凭猜重提。 |
| 废弃资源清理 | 终态（`SUCCEEDED` / `FAILED` / `STOPPED` / `CANCELLED`）且不再需要就 `<res> delete <name> [--yes]`；running 先 `stop`。批量用 `inspire --json <res> list -A` 过滤再逐个删。不确定是否还有人用就跳过，不要猜着删。 |
| 大规模 `mv` / `cp` / `rm` | 启智共享盘单目录常到百万文件 / 百 GB / 百 TB 量级，直接 `rm -rf` 能卡几小时。**动手前先 `ls -A \| wc -l` + `du -sh --max-depth=1` 看形状**，策略见 [references/troubleshooting.md](references/troubleshooting.md)。超过 20 分钟的操作一律 `nohup ... &` + sentinel 文件本地轮询，**别**让 `inspire notebook exec` 吊着等。 |

## 2. 命令速查

> **`--quota` / `-q` 通用格式**（`notebook create` / `job create` / `run` 共用）：
> - 三元组：`<gpu>,<cpu>,<mem>`（都是整数，`mem` 以 GiB 计）。例：`1,20,200` = 1 GPU + 20 CPU + 200 GiB。
> - CPU-only：`0,<cpu>,<mem>`，如 `0,4,32`（CPU 批处理另走 `hpc`）。
> - 三元组必须在 workspace 已注册的 `quota_id` 里唯一匹配。零匹配报错并列出可用规格；多个 compute_group 同时匹配同一三元组（比如 H100 组和 H200 组都有 `1,20,200`）需要加 `--group <名字>` 消歧。
>
> GPU 型号由 workspace × compute_group 反推，不在 `--quota` 里指定。列当前 workspace 的合法三元组：`inspire resources specs --usage notebook`（notebook 规格）/ `--usage hpc`（HPC 规格）；train-job 规格目前查 `--usage all`。

### 2.1 资源 / 项目 / 用户 / 配置 / 账号

资源和身份的查询入口，任何后续操作前都可能要这里先看一眼。

| 命令 | 用途 |
| --- | --- |
| `inspire resources list [--all --include-cpu]` | 实时可用量（默认只 GPU） |
| `inspire resources nodes [-A]` | 整节点空余，多节点任务前必查 |
| `inspire resources specs --usage {hpc,notebook,auto} --workspace X --group Y [--json]` | 规格表；`hpc create --spec-id` 取这里的 `predef_quota_id`（CLI 通常自动匹配） |
| `inspire project list` | 项目 + 配额，定高/低优前必看 |
| `inspire user whoami` | 当前登录身份 / 角色 |
| `inspire user permissions [--workspace X]` | workspace 下授予的权限码（如 `job.trainingJob.create`） |
| `inspire config show [--compact --json]` | 扁平配置（平台身份 / 代理 / 默认镜像 / 路径），含来源 |
| `inspire config context [--json]` | 活动账号 / 当前项目 / workspace alias / compute_groups |
| `inspire account {add,list,use,current,remove} <name>` | 多账号管理，一账号一目录 |
| `inspire init --discover` | 交互式绑定当前仓库到某个 Inspire 项目 + 远端存储池，写回 `<repo>/.inspire/config.toml` |

### 2.2 Notebook

**一个 notebook ↔ 一条本地 alias**。首次 `notebook ssh <name>` 把连接存成 `<clean-name>-sh0`（重名递增 `-sh1/-sh2/...`；`--save-as` 强改名）。同 notebook 已有 alias 则复用，不重复建。`notebook ssh <arg>` 多态——arg 是 alias 就重连（自动重建断开的 tunnel），否则按 notebook name bootstrap。

**`shell` vs `exec`**:
- `inspire notebook shell <alias>` = **持久** SSH 会话，cwd / env / history 保留直到 `exit`。多个终端并开就是多个独立会话（都挂在同一容器，互相抢 CPU / RAM）。
- `inspire notebook exec <alias> "<cmd>"` = **一次性**独立子进程，两次调用间**不共享 cwd / env**。接续状态塞同一调用：`exec "cd foo && export X=1 && ./run.sh"`，或远端写脚本后 `exec "bash setup.sh"`。

| 命令 | 用途 |
| --- | --- |
| `inspire notebook list [-A -s RUNNING --name X]` | 列实例 |
| `inspire notebook create --workspace X --group Y -q <gpu,cpu,mem> --image URL --project P [--wait --json]` | 建实例 |
| `inspire notebook status <name>` | 详情，镜像名在 `image.name` |
| `inspire notebook events <name> [--tail N --from-cache]` | 实例生命周期事件（调度 / 镜像拉取 / 保存镜像） |
| `inspire notebook lifecycle <name>` | 多次启停的粗粒度时间线（一次 `start→stop` 一行） |
| `inspire notebook {start,stop,delete} <name> [--yes]` | 生命周期；`delete` 不清本地 alias，要 `forget` |
| `inspire notebook ssh <name> [--save-as <alias>]` | Bootstrap SSH + alias 保存。失败见 troubleshooting.md |
| `inspire notebook exec <alias> "<cmd>"` | 一次性远端命令（在 `INSPIRE_TARGET_DIR` 下） |
| `inspire notebook shell [<alias>]` | 持久交互 SSH |
| `inspire notebook scp <src> <dst>` | 传**非仓库**文件（源码走 `git push` + `exec "cd <repo> && git pull"`）。不继承 `INSPIRE_TARGET_DIR`，远端写绝对路径 |
| `inspire notebook install-deps <alias> [--slurm --ray]` | 一次性把 hpc/ray 运行时依赖装进 notebook，对齐 unified-base:v2（slurm 客户端 + ray=2.55.1 默认）；准备好后 `image save` 派生项目镜像。仅可上网区计算组可用 |
| `inspire notebook test [<alias>]` | 连通性测试（带耗时），排障首选 |
| `inspire notebook refresh <alias>` | notebook 重启后刷 alias 连接 |
| `inspire notebook {connections,forget,set-default}` | 本地 alias 管理 |
| `inspire notebook top [--watch]` | alias 的 GPU 利用率实时 `nvidia-smi`（要 tunnel 活着） |
| `inspire notebook metrics <name> [--metric core --json]` | 历史利用率曲线 PNG（8 种指标默认取前 4）；`job / hpc / serving metrics` 同 UX 同 flag |

### 2.3 GPU 多节点任务（`job`）

`inspire job` 覆盖**所有 GPU 多节点工作负载**——分布式训练、批量推理、并发单节点 worker pool 全走这里。与 `inspire hpc` 的区别是资源形态：`job` = GPU，`hpc` = CPU。

| 命令 | 用途 |
| --- | --- |
| `inspire job create -n <name> -q <gpu,cpu,mem> --nodes N -c <cmd> --workspace X --image Y [--priority 9]` | 精细提交 |
| `inspire run "<cmd>" -q <gpu,cpu,mem> [--group <name> --nodes N --watch]` | 快速提交；`--watch` 自动跟 logs |
| `inspire job status <name>` | 权威状态（高 / 低优 + 调度结果） |
| `inspire job logs <name> [--follow]` | 优先走 SSH tunnel fast path，回退其它通道 |
| `inspire job events <name> [--instance <pod> --type --reason --tail --from-cache]` | 调度失败优先看 `--instance` 定位哪个 pod |
| `inspire job {stop,delete} <name> [--yes]` | 止损 / 清理 |
| `inspire job metrics <name>` | 按 worker-0..N-1 分画。stdout 的 `spread=X%` 反映 worker 间离散度——多节点训练健康核心指标，正常 < 5%；大 = worker 掉队 / 通信 hang / 数据加载不均 |

### 2.4 HPC(Slurm)

`hpc create` 四约束：

1. `-c` **只写 Slurm 正文**，平台自动补 `#SBATCH` 头；程序必须**显式 `srun`** 启动。
2. `--compute-group "<name>"` 按 name 传（从 `inspire config context` 的 `compute_groups[]` 抄）。
3. `--cpus-per-task` / `--memory-per-cpu` 超规格**静默排队不报错**。CLI 按 `(group, cpus, mem)` 自动匹配 `spec_id`。
4. `--image` 必须是**完整 Docker 地址** + 带可用 Slurm 环境。通用基底 `docker.sii.shaipower.online/inspire-studio/unified-base:v2`；`--image-type` 通常 `SOURCE_PRIVATE` / `SOURCE_PUBLIC`。

| 命令 | 用途 |
| --- | --- |
| `inspire hpc create -n <name> -c <body> --compute-group <name> --workspace X --cpus-per-task N --memory-per-cpu M --image <URL> --image-type SOURCE_PRIVATE --project P` | 见上四约束 |
| `inspire hpc status <name>` | **假成功警报**：`status=SUCCEEDED` ≠ payload 真跑过（entrypoint 早退 / srun 语法错 / shell 变量丢失都能 SUCCEEDED）。每次新 entrypoint 写**独一无二的 fingerprint 到共享盘**，同项目 notebook `cat` 回验。`slurm_cluster_spec.nodes` RUNNING 时应非空；CREATING 卡住或 RUNNING 时 `nodes=[]` 才是坏信号 |
| `inspire hpc list` | 当前 workspace 所有创建者的任务 |
| `inspire hpc events <name>` | Slurm 控制器事件。**HPC 不暴露 per-pod 事件**，只有 job-level |
| `inspire hpc {stop,delete} <name> [--yes]` | 止损 / 清理 |
| `inspire hpc metrics <name>` | 每个 slurm pod 一条曲线。`SUCCEEDED` 但曲线全 0 = entrypoint 根本没跑（反向诊断假成功） |

平台自动注入的 Slurm 头（不用自己写）：

```bash
#SBATCH -o /hpc_logs/slurm-%j.out
#SBATCH -e /hpc_logs/slurm-%j.err
#SBATCH --ntasks=*
#SBATCH --cpus-per-task=*
#SBATCH --mem=*G
#SBATCH --time=*
```

### 2.5 Ray（弹性计算）

> **不是 infra 组成员的话默认不要用 Ray**。绝大多数 SII 任务的形态用 `job`（固定规模 GPU）或 `hpc`（固定规模 CPU）就够了。Ray 当前**仅在 `CI-情境智能` workspace（注意：和 `CI-情境智能` project 同名但不是一回事）+ `CPU资源-2` 计算组**有可用配额，**整体仍处于试验性阶段**，无业务理由别选这条路。

一个 head + 若干 worker 组，每组实例数按实时负载在 `min_replicas` / `max_replicas` 之间自动扩缩。**`job` / `hpc` 是固定规模，Ray 是弹性**——driver 不主动退出集群就不停，worker 按 `min_replicas` 一直占配额。选型：流式 / 异构 worker / 长守护走 Ray；固定规模批处理走 `job` / `hpc`。

| 命令 | 用途 |
| --- | --- |
| `inspire ray create -n X -c <driver-cmd> --head-image URL --head-group <name> --head-spec <quota> --worker 'name=w1,image=URL,group=...,spec=...,min=1,max=8[,shm=32][,image_type=...]' -p P --workspace W` | 提交。重复 `--worker` 定义多个 worker 组。`--dry-run` 打印 body，`--json-body <file>` 整体提交 |
| `inspire ray list [-A --created-by user-X --workspace Y]` | 默认只列当前用户（对齐 Web UI"我的"） |
| `inspire ray status <name>` | 纯文本只打顶层；`--json` 才看 head / worker 规格 + 每组 `min/max/current_replicas` |
| `inspire ray events <name> [--tail N --reason R --type Normal\|Warning]` | **卡 PENDING 第一手**——调度失败原因直接写在 message |
| `inspire ray instances <name>` | pod 级状态，定位是哪一个 pod 失败 |
| `inspire ray {stop,delete} <name> [--yes]` | `stop` 回收 worker，条目留在 list；`delete` 彻底清 |

**Ray 特有坑**：
- 镜像必须带 Ray runtime。基底 `unified-base:v2`；自制镜像先 SSH 进 notebook `ray start --head --num-cpus=1 --disable-usage-stats && ray stop` 能干净起停才算 OK。
- `--head-spec` / `--worker spec=` 是 **Ray 专属 `quota_id`**，和 notebook / HPC 不同表。查当前 workspace 的可用 Ray quota：`inspire resources specs --usage ray [--group <name>]`，`Spec ID` 列就是 `quota_id`。
- `min` / `max` 都必须 ≥ 1，没有"闲时缩到 0"。
- driver 不 `sys.exit()` 就一直在，长守护任务要接受"手动 `ray stop`"的运维模型。

### 2.6 镜像

| 命令 | 用途 |
| --- | --- |
| `inspire image list [--source public\|private\|all]` | 浏览；`private` = UI "个人可见"，`all` 聚合去重 |
| `inspire image save <notebook-name> -n X -v v1 [--public --wait --json]` | 从运行中实例保存为镜像；`--public/--private` 指定可见性 |
| `inspire image set-visibility <name>:<ver> --public\|--private` | 翻转已有镜像可见性 |
| `inspire image register [--method address]` | 注册外部镜像，优先 address 方式 |
| `inspire image set-default --job <URL> --notebook <URL>` | 写回最近的项目级 `.inspire/config.toml`（没有位置参数） |

## 3. 主流程

### 3.1 远端路径 = 作用域 × 存储池

两个正交维度，**先决策作用域，再选存储池**。

**作用域**（谁能看到）：

| 根 | 路径样例 | 定位 |
| --- | --- | --- |
| 项目-个人 | `/inspire/<tier>/project/<topic>/<user>/…` | 每项目-每用户一份。代码、脚本、配置、调试输出。`<user>/` 下可自建任意层级 |
| 项目-公共 | `/inspire/<tier>/project/<topic>/public/…` | 项目成员共享。数据集、权重、批量结果、checkpoint |
| 全局-个人 | `/inspire/hdd/global_user/<user>/…` | **仅 hdd**。跨项目个人盘，单用户 quota 比项目盘紧，适合脚本 / 配置 / 小工具 |
| 全局-公共 | `/inspire/hdd/global_public/…` | **仅 hdd**，~250 TB。全平台共享，适合可能被多项目复用的通用数据；不放个人中间产物 |

> `global_*` **只在 hdd**。要 SSD / qb-ilm 速度只能走"项目-个人"或"项目-公共"。

**存储池**（冷热）：

| 池 | 项目路径前缀 | 定位 |
| --- | --- | --- |
| SSD `gpfs_flash` | `/inspire/ssd/project/<topic>/` | 训练 hot path、活跃工作集、checkpoint 热点 |
| HDD `gpfs_hdd` | `/inspire/hdd/project/<topic>/` | 通用；项目 fileset 经常 100% 满，写前 `df` 看 Avail |
| qb-ilm `qb_prod_ipfs01` | `/inspire/qb-ilm/project/<topic>/` | 大容量，顺序读带宽 ≈ SSD |
| qb-ilm2 `qb_prod_ipfs02` | `/inspire/qb-ilm2/project/<topic>/` | 最新也最空，新增数据默认落这里最安全 |

每个项目下 `<user>/` 和 `public/` 的具体子树结构由项目自定，在仓库根的 `INSPIRE.md` `Path Conventions` / `Public Directory Layout` 两节记。

### 3.2 代码流转

| 场景 | 做法 |
| --- | --- |
| 独立 repo 日常 | 本地 `git push` → `notebook exec "cd <repo> && git pull"` |
| 多仓库工作区 | `INSPIRE_TARGET_DIR` 设到 `<user>/` 下自建工作区根，里面并列多 repo |
| 非 Git 文件 | `notebook scp`，远端路径写绝对 |
| 目标计算组不可上网但共享路径可见 | 切到同一路径下的可上网区实例做 git，拉回来即可 |

日常闭环：

```bash
export INSPIRE_TARGET_DIR=/inspire/ssd/project/<topic>/<user>/<workspace-subpath>

cd /local/path/<repo>
git push origin <branch>
inspire notebook exec "cd <repo> && git pull && git log -1 --oneline"

inspire notebook ssh <notebook-name> --save-as mybox
inspire notebook exec --alias mybox "hostname"
```

### 3.3 三阶段工作流

默认范围只跑 `CPU 资源空间` + `分布式训练空间`，分三阶段。

#### 阶段 A：CPU 空间起基底 notebook

**强烈推荐的一次性做法**：项目刚开张时在可上网区 CPU 空间用 `docker.sii.shaipower.online/inspire-studio/unified-base:v2`（自带 ssh + slurm + ray 依赖）起一个基底 notebook，把后续要用到的所有依赖**一次性配齐**——`hpc create` 要的 slurm-client、`ray create` 要的 ray runtime、多节点 `job create` 要的 deepspeed 等等——然后 `image save` 派生为项目通用镜像，后续 notebook / job / hpc / ray 全用它。一次费力，永久省事。

**裸镜像需要补 hpc/ray 时的快捷做法**：在 ubuntu 24.04 系列基底（`sandbox-base:ubuntu24.04-py3.1` 等）上 `notebook ssh` 拿到 alias 后跑：

```bash
inspire notebook install-deps <alias> --slurm --ray
```

这会一次性装好 slurm 客户端依赖（`slurm-wlm slurm-client munge hwloc libpmix2`，对齐 unified-base:v2）+ pin 版本的 ray（默认 `2.55.1`），后续 `image save` 即得到能跑 hpc / ray 的项目镜像。`--ray` 自带清华源，无网区不可用——只有可上网的 `HPC-可上网区资源-2` / `CPU资源-1` 计算组能跑。**`slurm.conf` 由平台在 `hpc create` 时注入，install-deps 不动；分布式训练 lib（deepspeed / accelerate / transformers）项目自己 `notebook exec pip install`**。

**例外：镜像变体太多、不愿固化的**（infra 组的常见模式）→ 镜像不带 sshd 没问题，`notebook ssh` 自带 bootstrap（§1.1），每次连接现装现用；但 **slurm / ray / 分布式训练 lib 必须真实装在镜像里**（用上面的 `install-deps` 或手动 apt/pip），要跑 `hpc create` / `ray create` / 多节点 `job create` 就老老实实在那个镜像里把对应依赖装好。

> 普通 notebook 里 slurm 命令因无 controller 会报 `Could not establish a configuration source`——平台设计如此，不是镜像问题，`inspire hpc create` 路径下才会注入 controller。

计算组按实际需求选（需要 `pip install` / `apt install` 就挑有公网的 `HPC-可上网区资源-2`，否则 `CPU资源-1/2` 都行）：

```bash
inspire notebook create --workspace CPU资源空间 --group CPU资源-2 -q 0,20,256 \
  --name <action-goal> --image <任意镜像> --project <P> --wait --json

inspire notebook ssh <notebook-name> --save-as cpu-box
```

想固化依赖并发布成可复用镜像：

```bash
inspire notebook exec --alias cpu-box "apt-get update && apt-get install -y <deps> && pip install ..."
inspire image save <notebook-name> -n <img> -v v1 --public --wait --json
inspire image set-default --job <URL> --notebook <URL>
```

> 一次性用完就扔的场景跳过 `image save`，notebook 停掉后容器整个回收，痕迹全没。

#### 阶段 B：CPU 空间跑数据处理

**形态决定路径**：

| 形态 | HPC（Slurm） | Ray（弹性） |
| --- | --- | --- |
| 任务边界 | 明确开始 / 结束 | 长时间流式 |
| 并发模型 | 固定 `ntasks × instance_count` | `min/max` 自动伸缩 |
| CPU / GPU 混用 | 单节点类型 | 异构（CPU 预处理 + GPU 推理） |
| 结束条件 | srun 退出自动 SUCCEEDED | driver exit 才结束；常驻需手动 `ray stop` |
| 数据流 | GPFS → 处理 → GPFS（阶段间落盘） | worker 间走 Ray 对象存储 |

**规模坑**：小规模 probe 通过 ≠ 正式规模稳定。放量前再跑一次接近正式规模的验证。

HPC 批处理：

```bash
inspire hpc create -n <name>-preprocess \
  -c 'srun bash -lc "python preprocess.py"' \
  --compute-group HPC-可上网区资源-2 --workspace CPU资源空间 \
  --cpus-per-task <N> --memory-per-cpu <M> \
  --number-of-tasks 1 --instance-count 1 \
  --project <P> --image <URL> --image-type SOURCE_PRIVATE
```

Ray pipeline（默认范围内**仅 CPU Ray**，GPU Ray 需 workspace 级 SKILL 覆盖）：

```bash
inspire ray create -n <name>-pipeline \
  -c 'python driver.py --mode run_and_exit' \
  --head-image docker.sii.shaipower.online/inspire-studio/unified-base:v2 \
  --head-group CPU资源-2 --head-spec <head-quota> \
  --worker 'name=w1,image=...,group=CPU资源-2,spec=<quota>,min=1,max=8,shm=32' \
  -p <P> --workspace CPU资源空间
```

#### 阶段 C：分布式训练空间

**前置**：依赖 / 权重 / 数据集**先在可上网空间下到共享盘**，再进训练空间（训练空间多数节点不可上网）。

```bash
# 单节点调试
inspire notebook create --workspace 分布式训练空间 -q 1,20,200 --group H100 \
  --name <name>-debug --image <ref> --project <P> --wait --json
inspire notebook ssh <name>-debug --command "nvidia-smi"

# 多节点训练(精细)
inspire job create -n <name>-train -q 8,160,1800 --nodes 2 \
  -c 'bash train.sh' --workspace 分布式训练空间 --group H100 --image <ref>

# 多节点训练(快速 + 跟日志)
inspire run 'bash train.sh' -q 8,160,1800 --nodes 2 \
  --workspace 分布式训练空间 --group H100 --image <ref> --watch
```
