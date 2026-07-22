# 资源与调度条件

选择 Workspace、Project、Compute Group、`--quota`、镜像和 Workload Profile 时先看本页。公网 / SII 内部源看 [`network-and-sources.md`](network-and-sources.md)；共享盘、存储池和 Path Alias 看 [`paths.md`](paths.md)。具体命令表面始终回到 CLI Help。

## 1. 三类名字

启智任务先分清三类名字：

| 类型 | 决定什么 | 典型字段 |
| --- | --- | --- |
| 调度条件 | 任务在哪跑、用多少资源、基于哪个镜像 | `workspace`、`project`、`group`、`quota`、`image` |
| 远端路径 | 代码、数据、权重、Checkpoint 和产物放在哪 | `me`、`public`、`ssd.me`、`qb-ilm2.public` |
| 对象名字 | 观察、连接或清理哪个平台对象 | Notebook / Job / HPC / Ray / Serving 的名称 |

调度条件没有隐式默认值。创建 Workload 时显式传入，或用 Workload Profile 保存这五类条件。Path Alias 只表示远端路径，不能替代 Workspace、Project、Group、Quota 或 Image。

Restricted Notebook 的文件流转边界是 `/inspire/<storage>/...` 共享路径。不要新增 WebDAV Copy 命令；通过可上网 Notebook 的 `notebook scp` 或外部 `rsync` 搬入 / 搬出共享盘。

## 2. Workspace 判断

日常 Workspace 选择不要抽象化：

| Workspace | 主要职责 |
| --- | --- |
| `CPU资源空间` | CPU Notebook、联网准备、依赖安装、CPU HPC、CPU Ray |
| `分布式训练空间` | GPU Notebook、GPU Job、多节点训练、Serving、GPU 指标观察 |

国产卡分区、`CI-情境智能` 工作空间或小组专属空间只在任务明确要求特殊硬件、特殊权限或特殊项目环境时使用。

## 3. Resource Truth

资源事实来自 Live 查询，不来自本地缓存、旧截图、旧 Reference 或历史任务输出。判断顺序：

1. 先看账号当前可见的 Workspace、Project 和 Compute Group 名字。
2. 按 Workload 类型查对应 Quota：CPU Notebook / HPC / CPU Ray 在 `CPU资源空间`，GPU Notebook / Job / Serving 在 `分布式训练空间`。
3. 用实时 Availability 判断空余；多节点 GPU 任务再看整节点空闲。
4. 创建命令里的 `--group` 使用完整 Compute Group 名称；查询命令里的 Group Filter 可以用关键词收窄候选。

`resources availability`、`resources nodes` 和各 Workload 的 `quota` 是资源事实入口；具体参数和输出以 CLI Help 为准。

`Available` 是平台上当前未被占用的 GPU，`Low Pri` 是低优任务占用、可被高优任务抢占的 GPU，`High Pri` 是 `Available + Low Pri`。判断高优任务时不要只看 `Available`，但 `High Pri` 也只是可抢占容量上限；提交后仍以 Events 为准。公平调度 Workspace 的高优写入值为 4，其他 Workspace 仍按其 `1–10` 策略。

## 4. Quota 语义

`--quota` / `-q` 是 `gpu,cpu,mem` 三元组，`mem` 以 GiB 计。GPU 型号不写进三元组，而由 Workspace + Compute Group 决定。

`mem` 表示实例常规内存规格，不是 Shared Memory。GPU Job 的 `/dev/shm` / IPC 空间用 `--shm-size <GiB>` 控制，且不能超过所选 Quota 的 `mem`；需要项目级默认时用 `INSPIRE_SHM_SIZE` 或 `[job] shm_size`。`job create --dry-run` 和 `job list/status` 会显示解析后的 Shared Memory，方便确认命令行参数和配置默认最终是否生效。

三元组必须在当前可见规格里唯一匹配。如果多个 Compute Group 有同一组三元组，先用查询命令按 Group 关键词收窄，再在 `create` 或 Profile 中写完整 Group 名称。

### qz 开发区与训练区

Compute Group 名称里的资源区前缀也是调度语义。qz 当前规则如下：

| 调度区 | 整节点 GPU 任务 | 碎卡 GPU 任务 |
| --- | --- | --- |
| `开发区` | 支持 | 支持 |
| `训练区` | 优先保障 | 当前公平调度 Workspace 只允许 `1=LOW`，任务可被抢占 |

整节点 / 碎卡按每个 Workload 实例或节点选择的 Quota 判断，不按任务聚合后的 GPU 总数判断。比如每节点 4 GPU、`--nodes 2` 仍是两个碎卡实例，不会因为总计 8 GPU 变成整节点请求；`--nodes` 只放大实例数，不改变单节点 Quota 的调度区语义。

“支持”不代表可以猜测规格。具体可用 GPU 型号、机房和 `gpu,cpu,mem` 三元组仍以当前 Workload 的 Live Quota Row 为准；创建 Workload 或写 Profile 时从同一行复制完整 `group` 和 `quota`。训练区提交碎卡任务时显式选择 LOW 优先级，提交后再从 Status / Events 核实平台解析出的优先级、排队和抢占结果。

申请资源前按真实任务需求和实时空余选择规格。不要因为猜测主动降档；只有调度语义、空余量或项目策略明确不足时再缩小规模。

## 5. Workload Profile

Profile 是调度条件组 Alias，只保存 `workspace`、`project`、`group`、`quota` 和 `image`。它不是 Path Alias，也不是远端工作目录。

适合写 Profile 的场景：

- 同一个项目反复创建同规格 GPU Probe、训练 Job 或 Serving。
- 同一批 Batch 条目共用调度条件，只变名称、命令或输入输出路径。

不适合写 Profile 的场景：

- 只想给远端目录起名字。用 Path Alias。
- 资源只用一次，且当前任务还在探索。
- 想省略 Workspace。没有默认 Workspace；Profile 也必须明确 Workspace。

## 6. 调度与资源观察

创建前看 Quota 和 Availability；提交后先看 Events，再看 Logs / Metrics / Instances。`status=RUNNING` 只说明平台对象在运行，不说明业务健康；`status=SUCCEEDED` 也不说明产物完整。

常见判断：

| 现象 | 优先方向 |
| --- | --- |
| 0 候选或 Quota Match Failed | Workspace / Group / Quota 三元组不匹配 |
| PENDING 很久 | 实时资源不足、优先级不足、节点条件不满足 |
| RUNNING 但业务没推进 | 看 Metrics 是否有 GPU / CPU / I/O 负载，再回到日志和产物 |
| 多节点某个 Worker 掉队 | 先看 Per-Instance Metrics 和 Instances，再看该 Worker 日志 |

终态且不再需要的资源要清理。Running 对象先 stop，再 delete；不确定是否仍有人使用时跳过。
