# 资源与调度条件

选择 workspace、project、compute group、`--quota`、镜像和 workload profile 时先看本页。公网 / SII 内部源看 [network-and-sources.md](network-and-sources.md)；共享盘、存储池、path alias 和 `INSPIRE.md` 看 [paths.md](paths.md)。具体命令表面始终回到 CLI help。

## 1. 三类名字

启智任务先分清三类名字：

| 类型 | 决定什么 | 典型字段 |
| --- | --- | --- |
| 调度条件 | 任务在哪跑、用多少资源、基于哪个镜像 | `workspace`、`project`、`group`、`quota`、`image` |
| 远端路径 | 代码、数据、权重、checkpoint 和产物放在哪 | `me`、`public`、`ssd.me`、`qb-ilm2.public` |
| 对象名字 | 观察、连接或清理哪个平台对象 | notebook / job / hpc / ray / serving 的名称 |

调度条件没有隐式默认值。创建 workload 时显式传入，或用 workload profile 保存这五类条件。Path alias 只表示远端路径，不能替代 workspace、project、group、quota 或 image。

## 2. Workspace 判断

日常 workspace 选择不要抽象化：

| Workspace | 主要职责 |
| --- | --- |
| `CPU资源空间` | CPU notebook、联网准备、依赖安装、CPU HPC、CPU Ray |
| `分布式训练空间` | GPU notebook、GPU job、多节点训练、serving、GPU 指标观察 |

国产卡分区、`CI-情境智能` 工作空间或小组专属空间只在任务明确要求特殊硬件、特殊权限或特殊项目环境时使用。普通说明和 `INSPIRE.md` 里的默认语义应直接写真实 workspace 名。

## 3. Resource Truth

资源事实来自 live 查询，不来自本地缓存、旧截图、旧 reference 或历史任务输出。判断顺序：

1. 先看账号当前可见的 workspace、project 和 compute group 名字。
2. 按 workload 类型查对应 quota：CPU notebook / HPC / CPU Ray 在 `CPU资源空间`，GPU notebook / job / serving 在 `分布式训练空间`。
3. 用实时 availability 判断空余；多节点 GPU 任务再看整节点空闲。
4. 创建命令里的 `--group` 使用完整 compute group 名称；查询命令里的 group filter 可以用关键词收窄候选。

`resources availability`、`resources nodes` 和各 workload 的 `quota` 是资源事实入口；具体参数和输出以 help 为准。

## 4. Quota 语义

`--quota` / `-q` 是 `gpu,cpu,mem` 三元组，`mem` 以 GiB 计。GPU 型号不写进三元组，而由 workspace + compute group 决定。

`mem` 表示实例常规内存规格，不是 shared memory。GPU job 的 `/dev/shm` / IPC 空间用 `--shm-size <GiB>` 控制，且不能超过所选 quota 的 `mem`；需要项目级默认时用 `INSPIRE_SHM_SIZE` 或 `[job] shm_size`。

三元组必须在当前可见规格里唯一匹配。如果多个 compute group 有同一组三元组，先用查询命令按 group 关键词收窄，再在 create 或 profile 中写完整 group 名称。

申请资源前按真实任务需求和实时空余选择规格。不要因为猜测主动降档；只有调度语义、空余量或项目策略明确不足时再缩小规模。

## 5. Workload Profile

Profile 是调度条件组 alias，只保存 `workspace`、`project`、`group`、`quota` 和 `image`。它不是 path alias，也不是远端工作目录。

适合写 profile 的场景：

- 同一个项目反复创建同规格 GPU probe、训练 job 或 serving。
- 同一批 batch 条目共用调度条件，只变名称、命令或输入输出路径。
- `INSPIRE.md` 里需要记录团队约定的资源口径，但不应把长配置重复写进文档。

不适合写 profile 的场景：

- 只想给远端目录起名字。用 path alias。
- 资源只用一次，且当前任务还在探索。
- 想省略 workspace。没有默认 workspace；profile 也必须明确 workspace。

## 6. 调度与资源观察

创建前看 quota 和 availability；提交后先看 events，再看 logs / metrics / instances。`status=RUNNING` 只说明平台对象在运行，不说明业务健康；`status=SUCCEEDED` 也不说明产物完整。

常见判断：

| 现象 | 优先方向 |
| --- | --- |
| 0 候选或 quota match failed | workspace / group / quota 三元组不匹配 |
| PENDING 很久 | 实时资源不足、优先级不足、节点条件不满足 |
| RUNNING 但业务没推进 | 看 metrics 是否有 GPU / CPU / I/O 负载，再回到日志和产物 |
| 多节点某个 worker 掉队 | 先看 per-instance metrics 和 instances，再看该 worker 日志 |

终态且不再需要的资源要清理。Running 对象先 stop，再 delete；不确定是否仍有人使用时跳过。
