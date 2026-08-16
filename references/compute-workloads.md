# Job、HPC、Ray、Serving 与 TensorBoard

在 GPU Job、CPU HPC、Ray 和 Serving 之间选型，矩阵提交一批任务，或提交后观察 Events / Logs / Metrics / Instances / Status 和训练曲线时看本页。资源目录和 Profile 看 [`resources.md`](resources.md)，镜像看 [`image.md`](image.md)，模型仓库看 [`model.md`](model.md)，官方数据集看 [`dataset.md`](dataset.md)。命令语法和参数以 CLI Help 为准。

## 1. 先选工作负载类型

| 目标 | 入口 | 适用边界 |
| --- | --- | --- |
| GPU 后台任务 / 分布式训练 / 批量推理 | Job | 固定 GPU 规模，任务开始后跑到结束 |
| CPU Slurm 批处理 | HPC | 固定 CPU 规模，预处理、评测、数据流水线 |
| 弹性 Worker / 长守护 / 流式处理 | Ray | 需要 Head、Driver 和可伸缩 Worker Group |
| 模型 HTTP 部署 | Serving | 从已注册模型创建在线服务 |

固定规模 GPU 不要用 Ray；固定规模 CPU 不要用 Notebook 长跑；普通训练 / 预处理不要用 Serving。能跑不等于选型正确。

## 2. 通用提交判断

提交前确认：

1. Workspace 与 Workload 类型一致：CPU / HPC / 公网准备用 `CPU资源空间`，GPU 训练 / Serving 用 `分布式训练空间`。
2. Quota Live 查询能找到目标 `gpu,cpu,mem`。
3. Image 已 `READY`，且环境在相同角色的 Notebook 或小规模任务里验证过。
4. 代码、数据、权重和输出路径在目标项目共享盘可见。
5. 复杂调度条件先 `dry-run` 或小规模 Probe。

Job 和 HPC 可以在创建时用 `--dataset <数据集名>:<版本名>` 只读挂载数据广场的官方数据集，语义见 [`dataset.md`](dataset.md)；Ray 和 Serving 不支持，需要数据时走共享盘。三类 Workload 都能把项目公共目录降级为只读，用于防止批量任务误写共享结果，默认不开启。

提交前想知道「这个规模到底放不放得下」，看 `resources availability`：它按计算组给出空余卡数和整节点空闲数，而任务正是提交到某个计算组的。集群占满时任务停在 `PENDING` 并伴随 `FailedScheduling` 事件。Workspace 的配额天花板不用planning——它要么无限，要么是集群容量的两倍，详见 [`resources.md`](resources.md)。

离线 GPU 空间不要在启动命令里做公网下载。公网内容提前准备；内部源依赖可在目标 Notebook 验证后保存镜像。

## 3. GPU Job

Job 覆盖 GPU 多节点工作负载，包括分布式训练、批量推理和并发单节点 Worker Pool。它是 GPU 路径；HPC 是 CPU Slurm 路径。

Job 的关键边界：

- 日志和工作目录依赖共享盘约定；训练 Repo 建议在 `me:<repo>`，启动命令里使用相对共享盘路径或让脚本自己切目录。
- Shared Memory 是每个 Job Instance 的 `/dev/shm` / IPC 资源，不等同于 `--quota gpu,cpu,mem` 里的 `mem`，但不能超过该 `mem`。PyTorch DataLoader Workers、多进程数据管线或大模型训练需要更大 `/dev/shm` 时，用 `--shm-size <GiB>` 显式设置。
- 环境变量由平台注入，不必再拼进启动命令；值可能是凭据，CLI 输出只回显变量名。
- 训练曲线走独立的 `inspire tensorboard` 命令组，不是 Job 的附属字段，见本文第 9 节。
- 任务结束后容器默认立即释放。需要事后进容器看现场时，在创建时设置成功 / 失败保留时长，任务会停在保留态等待，过期自动释放；这是排查失败训练最省事的路径，比重跑一次便宜。
- 状态变化通知（`--enable-notification`，收件人固定为当前用户绑定的飞书账号）和自动容错默认关闭，除非明确启用。
- 需要项目级持久默认值时写 `[job]` 配置段；提交前用 `job create --dry-run` 检查 Shared Memory、通知和容错的最终生效值。
- 多节点训练要关注每个 Pod 的 GPU、显存、CPU 和网络曲线是否同步；某个 Worker 长期低负载通常比日志更早暴露问题。
- 排除坏节点是“不要调度到这些 Ready 节点”，不是固定节点；候选节点来自所选 Compute Group。
- Workspace 会按空闲规则回收运行中的任务，判据是 GPU 利用率：`分布式训练空间` 对 Job 声明的是「GPU 低于 40% 持续 3 小时」。长时间不吃卡的阶段（大规模数据预处理、CPU 侧评测、等待外部服务）留在 GPU Job 里会被无声收走，提交前用 `inspire resources policy --workspace <名字>` 读当前规则。

优先级合同——公平调度 Workspace 只接受 `1=LOW`（可抢占）或 `4=HIGH`（默认，稳定档），其他 Workspace 是 `1–10`（默认 10），项目策略还可能再压低一档——见 [`resources.md`](resources.md)。任务需要稳定训练但 Status 显示 LOW 时，先 stop，再按当前 Workspace 和项目策略重提。

平台还会逐行限制 Quota 能用的优先级，`job quota` 的 `Priority` 列显示这条声明：`low` 的行只接受 `--priority 1`（可抢占），`any` 不受这层限制，`unknown` 表示这次没读到。`job create` 在发出创建请求前按这一列预检并说明改法，细节见 [`resources.md`](resources.md)。限制按每个 Job Instance 的 Quota 判断，不按 `quota.gpu × --nodes` 的总卡数判断；例如 2 个 4 GPU Instance 仍是两个碎卡实例。提交后用 Status / Events 核实解析后的优先级和调度结果。

## 4. HPC

HPC 有两层资源模型，不能混：

| 层级 | 含义 |
| --- | --- |
| 节点级 | 每个节点的 GPU / CPU / 内存，以及申请多少个节点 |
| Slurm 级 | 程序如何在这些节点内拆 task、CPU 和内存 |

两层之间**平台不做任何校验**，控制台也不做：`hpc create` 因此在提交前自己挡下三种必然跑不起来的组合——单个任务要的 CPU 超过一个节点、单节点上所有任务的内存超过节点内存、任务总数乘每任务 CPU 超过买下的节点总核数。前两种在平台上的表现是起来后一两分钟内就 `FAILED`，且 `logs` 和 `events` 都不带原因；第三种更隐蔽，平台一直报 `RUNNING`，Slurm 里的 step 却永远排队，直到 Workspace 的运行时长上限把它停掉。`--cpus-per-task` / `--memory-per-cpu` 不给时按「一个节点上落几个任务」推导，所以只调 `--number-of-tasks` 不会再造出上面第三种。

内存只按每 CPU 给（`--memory-per-cpu`，Slurm `--mem-per-cpu`）。网页端另有「每节点使用内存」输入框，**它是坏的**：平台收下并在详情页显示这个值，但生成脚本时只写 `--mem-per-cpu`，于是那一行是空的，任务必然失败。不要在网页端用它，CLI 也不提供对应选项。

**容器里量到的资源不是你的配额，程序不能照着它自动调档。** 一个 `0,4,16` 的 slurmd 容器里 `nproc` 报 **64**、`free -m` 报 **~503 GiB**——那是宿主机的数字。真实约束是 Pod 的 cgroup：`cpu.max` = `400000 100000`（4 核，硬限流），`memory.max` = 16 GiB。任何按 `nproc` / `free` / `multiprocessing.cpu_count()` / OpenBLAS 与 PyTorch 默认线程数自动调档的程序，都会按 64 核和 503 GiB 去开工作进程，然后在 4 核上被限流、被内存墙撞死。**并发度和缓冲区一律显式取自 `SLURM_CPUS_PER_TASK` 和 `--quota` 的内存数**，别让库自己猜。

**内存墙是节点规格，不是你申请的量，而且要留出运行时自身的占用。** cgroup 的 `memory.max` 恒等于 `--quota` 的内存，**不随 `--memory-per-cpu` 变**——实测只申请 12 GiB（`AllocMem=12288`）的任务照样提交了 15 GiB 而没有任何拦截，也就是说 Slurm 这一层的内存只进记账、不设运行时上限。真正会杀你的是那 16 GiB：单进程提交到 15900 MiB、16100 MiB 都正常，**顶到 16384 MiB 直接被 OOM 杀掉，日志里连一行错都没有**（进程被 SIGKILL，什么都来不及打）。所以按 `--quota` 的内存规划，并给解释器、shell 和 slurmd 自身留出几百 MiB；峰值贴着整数顶格的程序，失败形态就是「莫名其妙没输出」。

关键约束：

- 入口命令只写 Slurm 正文，程序必须显式 `srun` 启动。**没有 `srun` 的正文照样跑完并报 `SUCCEEDED`**——sbatch 会在第一个节点上执行它——只是不产生 Slurm step，多节点时其余节点全程空转。`hpc status` 的 `Steps` 是唯一能看出这件事的字段：`0/0` 表示没有 step，`1/1` 表示 step 跑完了。
- Group 使用完整 Compute Group 名称；并非所有 CPU Compute Group 都支持 HPC。
- 镜像必须带可用 Slurm 运行环境；平台的镜像列表不按这一点过滤，选错了不会有任何提示。
- `status=SUCCEEDED` 不等于业务产出完整；先看 `Steps`，再写 Fingerprint，从同项目 Notebook 回读产物。
- 结束后有一段 `SUCCEEDED_RETAINING` / `FAILED_RETAINING` 保留态，此时 `hpc delete` 答「当前状态（运行中）无法删除」，`hpc stop` 也解除不了——等它自己转成终态（约一分钟）再删。

## 5. Ray

默认不要使用 Ray，除非任务明确需要弹性 Worker、长守护、流式处理或异构 Worker。Ray 集群由 Driver / Head / Worker Group 组成，Driver 不退出就会一直占资源。

Ray 特有风险：

- 镜像必须带 Ray Runtime。
- Head 和 Worker Quota 用 Ray 专属规格表。
- Worker 的 `min` / `max` 决定资源占用上限；长守护任务要接受手动 stop 的运维模型。
- 如果只是固定规模训练或固定 CPU 批处理，回到 Job / HPC。

创建后用 `ray events` 看调度、`ray instances` 看 Head 与实际 Worker、`ray logs` 看程序输出、`ray metrics` 看各组负载；结束后先 `ray stop`，确认不再需要再 `ray delete`。重复配置用 `ray profile`，矩阵提交用 `ray batch`。

弹性本身用 `ray scaling` 看：每行是平台对某个 Worker Group 做的一次副本数变更，从 `initialized` 起，之后每次 `scale_up` / `scale_down`。**空的历史表示这个弹性区间从来没被用到**——组一直跑在起始副本数上，那通常说明 `min` / `max` 设了却没有触发条件，或者这个任务本来就不需要 Ray。`--group` 收窄到某一组，组名与 `ray status` 和 `ray instances` 的 Role 列一致。

停掉的 Ray Job 保留完整集群规格，`ray start` 用原样的 Head、Worker Group 和 Driver 命令拉回来，不需要重新指定。平台在这里会「受理但不执行」——请求返回成功而任务纹丝不动，所以 `ray start` 以状态真的离开 `STOPPED` 为准，没动就报失败。从未真正 `RUNNING` 过的 Job 通常起不来，重建比重试可靠。

支持 Ray 的 Compute Group 是 CPU 组里很窄的一部分。`<workload> quota` 现在只列出真正受理该 Workload 的组——每个组自己声明支持哪几类任务，按这个过滤，所以看到的档位就是能提交的档位。

## 6. Serving

Serving 面向模型部署服务。通常先用 Model Registry 找到模型和版本，再创建自定义部署。

创建前确认：

- 模型目录已经注册，目标版本状态可用。
- 镜像里有服务 Runtime 和启动命令所需依赖。
- 端口、健康检查和业务 Smoke Test 明确。
- 资源规格来自 Serving Quota，而不是训练 Job Quota。
- 公开访问前应用自身鉴权可用；平台通路不替代 API Key 或登录。

LLM 专属部署、Serverless LLM 和模型广场一键部署有不同平台类型；普通 Custom Serving 不要推导它们的字段。

当前 Custom Serving 生命周期是：`serving configs` / `serving quota` 选配置，`serving create` 或 `serving batch` 创建，`serving list/status/events/instances/logs/metrics` 观察，`serving stop` / `serving start` 控制，最后 `serving delete` 清理。重复调度条件用 `serving profile`。

副本数用 `serving scale` 调整，其余配置原样保留；每个副本各占一份完整规格，扩容前先看 `resources availability`。历史配置用 `serving versions` 列出，`serving rollback --version` 按某个历史版本重新部署——副本会被替换，在途请求和重启一样会断。

**没有重新部署过、延迟或吞吐却变了，先看 `serving scale-history`。** 它按时间列出副本数每一次变化和变成了多少；掉下去的副本数、或者一次没落地的自动伸缩，只会出现在这里，`versions` 里一个字都没有。把它和 `api-metrics` 的时间线对齐，就能确认这次变化解释不解释得了那段流量。

两套指标不要混：`serving metrics` 看 GPU / CPU / 内存这类资源占用，`serving api-metrics` 看请求量、成功率和延迟。「没人调用」和「一直调用一直失败」只有后者分得清。

部署起不来时事件比日志先有线索，而且要看到实例级那一半：`Unhealthy` 是「副本起来了但健康检查一直不过」这个最常见故障的唯一署名，它只出现在实例级事件里，部署级只会说 `GroupsProgressing` / `Pending`。默认的 `serving events` 已经把两级合成一条时间线，不用再加开关。

## 7. 矩阵提交（Batch）

同一组调度条件要提交一批只差名称、命令或输入输出路径的 Workload 时，用 `<workload> batch <文件>` 而不是循环调 `create`。五类都有：`job` / `hpc` / `notebook` / `ray` / `serving`。文件是 JSON 或 TOML，顶层是该 Workload 的条目列表，可选的 `defaults`、`profiles` 和 `matrix` 用来消重复；展开后的每一条必须自带 `create` 的全部必填字段，调度条件可以由条目里的 `profile = "<名字>"` 提供。

**Batch 条目的字段与 `create` 严格对齐**，不是它的弱化版：数据集挂载、环境变量、描述、成功 / 失败保留时长、容错重试间隔、运行时长上限、状态通知和只读挂载都能写进条目。`dataset` 收一条 `"<名字>:<版本>"` 或一个列表，`env` 除了 `KEY=VALUE` 列表还接受表——TOML 和 JSON 表达映射比拼接字符串自然。`ray` 和 `serving` 的条目不收数据集（平台拒绝该字段）。

先跑 `--dry-run`：它展开矩阵、逐条解析并打印计划而不提交任何东西，是唯一能在提交前核对最终生效值的入口。数据集在条目准备阶段就完成校验，所以一个拼错的 spec 会在任何东西提交之前中止整个 Batch，而不是等前几条已经跑起来才发现。

## 8. 观察闭环

| 工具 | 主要回答 |
| --- | --- |
| `events` | 为什么排队、为什么启动失败、调度器或控制器拒绝了什么 |
| `logs` | 程序自身报错、训练进度、业务输出 |
| `metrics` | 已启动任务是否仍在有效工作，Pod / Task / Replica 是否均衡 |
| `instances` | 实际运行单元是否齐全，是否有部分 Pending 或异常；每个 Pod 落在哪个节点 |
| `status` | 平台状态、优先级、基础摘要、落在哪些节点、挂了哪些官方数据集 |
| `job command` | 这个任务当初到底是用什么命令跑起来的——复现实验和对比两次运行时先读它 |
| `inspire tensorboard scalars` | 训练本身写出来的曲线：loss 还在不在降、eval 指标有没有走平（见第 9 节） |

卡住或失败先看 Events；已启动但健康度不明看 Metrics；程序行为看 Logs；产物完整性回到共享盘文件和 Fingerprint。上面五个都在回答「平台侧这个任务怎么样」，回答不了「模型训得怎么样」——那是 TensorBoard 那一条。

**等待只对还会自己往前走的状态有意义。** `PENDING` / `QUEUING` / `RUNNING` 会动，终态不会：Job 和 HPC 没有 `start`，`job stop` 留下的 `job_stopped` 与 `SUCCEEDED` / `FAILED` / `CANCELLED` 一样是终点，要再跑只能重新 `create`；能从停止态拉回来的只有 Notebook、Ray 和 Serving，它们各有 `start`。所以开始等之前先 `status` 读一次当前状态，别对着一个已经停掉的任务等它变回运行中。`job wait` 和 `job logs --follow` 在任务进入终态时返回；`events --follow` 和 `job list --watch` 不会自己结束，任务结束了也不会，只能被中断——不要把这两条挂在后台终端上当「等任务跑完」用。

节点归属分两层，两层都是 Live 事实，任务离开运行态就清空：`status` 给任务级的节点清单（`job` 的 `Nodes` / `Pinned Nodes` / `Excluded Nodes`、`hpc` 与 `serving` 的 `Nodes`、`notebook` 的 `Node` 带节点健康），`instances` 给 Pod 级的 `Node` 列。多节点任务定位掉队的那一个 Worker 用 `instances`，因为只有它把 rank 和节点对上；`ray` 的节点归属只有 `instances` 这一层，`ray status` 的 `head_node` / `worker_groups` 是规格不是落点。**空的节点清单读作「还没被调度」，不是「查不到」。**

`shell` 在 `job` / `hpc` / `ray` 下有：把本地 stdin 接到实例里的远端 PTY，`exit` 退出、`Ctrl+]` 断开而不结束 shell。默认进哪个实例按 Workload 定：**HPC 进 `launcher`**——`srun` 在那儿跑，也只有那个 Pod 看得见你的进程，`slurmctld` 是调度器本身，要去得 `--instance slurmctld`；**Ray 进 head**——驱动在那儿，`ray status` 和集群自己的日志也在那儿。`serving` 平台侧也有实例 PTY，还没验证过，CLI 不提供。

`logs` 在 `job` / `hpc` / `ray` / `serving` 下都有，共用同一套记录与字符预算和同一份 `--json` schema；Notebook 没有 `logs`，它是交互式容器，用 `notebook exec` 或 `notebook shell` 直接读。日志按实例采集后合并成一条时间线，每行带实例标识（`hpc` / `ray` 用 `instances` 打印的角色或序号，`job` 与 `serving` 用 Rank），`--instance` 只读其中一个或几个。**平台侧根本没有「工作负载级日志」这一层**——日志端点只按 Pod 名取，所以全实例聚合不是选择而是唯一形态。日志记录里另有平台填的 `node` 字段，只在 `--json` 里可见，且不是每类工作负载都填，Pod 与节点的对应关系以 `instances` 的 `Node` 列为准。

`events` 与 `logs` 的默认口径一致：不加参数就是这个工作负载能拿到的全部，`--instance` 收窄到某个实例，`--workload-level` 反过来只留控制器那一半（两者互斥）。四类的默认都把两套不相交的视图合成一条时间线——控制器事件说「任务为什么没被创建、为什么整体排不上」，Pod 事件说「哪个实例没被调度、镜像拉没拉下来、容器起没起来」（`FailedScheduling` / `Pulling` / `Started` / `BackOff`）——并多出一列 `Instance` 指明每行来自哪个实例，控制器行在这一列是 `-`。实例标识与各自 `instances` 一致：`hpc` 与 `ray` 是角色 / 序号，`job` 与 `serving` 是 Rank。取数代价各不相同但对调用方不可见：`job` 一次请求带 200 个 Pod，`ray` 一次调用本来就同时返回两级，`serving` 两级各一次调用，`hpc` 一个实例一次请求、并发取。Notebook 是单实例，两个开关都没有。**排查顺序建议先看默认的合并视图**：只有已经知道问题出在整体调度、不在某个 Pod 上时，`--workload-level` 才值得用——它省掉的是实例那几次请求，不是一次判断。

## 9. TensorBoard

TensorBoard 是平台上的一等对象，不是 Job 的字段：计算组在 `support_job_type_list` 里单独声明 `tensorboard` 这个任务类型，控制台给它独立页签，它既可以挂在某个训练任务上，也可以对任意一个 summary 目录单独建。命令组是 `inspire tensorboard`，`metrics` 那套读的是资源占用，这里读的是训练本身写出来的曲线。

- **规格固定 1 CPU / 2 GiB**，所以没有 Quota 要选、没有镜像要挑；唯一的放置输入是计算组，而且这个组必须声明 `tensorboard`——`分布式训练空间` 里有几个训练组没有声明，选中会被平台拒。
- **`--summary-path` 是全部**：board 直接读共享盘上已有的 event 文件，训练侧不需要为它改任何东西；同一个目录反复建 board 不会影响文件。
- **`--job` 只是归属记号**，让 board 出现在那个任务的行上，不会替你推导 summary 路径。
- **自动停机上限 72 小时**（`--auto-stop-hours`，默认 24），到点自停；`stop` 后 `start` 会重新开始计时，summary 路径和窗口都不用重填。
- **`tags` 和 `scalars` 直接读运行中的 board**：`tags` 给出 run 和 scalar tag，`scalars` 给出每条曲线的首尾值、step 区间、最小最大值，`--points N` 再加最后 N 个 `(step, value)`。这一层让 Agent 不需要浏览器也能判断 loss 还在不在降、eval 指标有没有走平、某次训练是不是发散了。
- **点按 step 排序，不按 event 文件顺序**：续训和多 worker 写出来的序列在文件里是交错的，"最后一个点"问的是 step。
- **运行中的 board 没有任何 tag，和路径写错长得完全一样**——两种情况都只是空列表，要靠核对 `--summary-path` 区分。
- 删除 board 不动共享盘上的 event 文件，指向同一目录重建一个 board 读到的是同一份数据；运行中的 board 拒绝删除，先 `stop`。

## 10. 异常判断

| 现象 | 优先怀疑 |
| --- | --- |
| `PENDING` 过久 | 优先级不足、实时配额不足、节点条件不满足 |
| `CREATING` 卡死 | 镜像拉取失败或节点初始化 |
| `instances` 部分 Pending | 多节点或多副本调度不均 |
| `logs` 为空但 `RUNNING` | 主进程未输出、日志路径不在 CLI 管理范围、程序没真正启动 |
| `FAILED` 但无业务报错 | OOM、显存溢出、节点驱逐或控制器失败 |
| HPC `Steps` 是 `0/0` 或 `-/0` | Slurm 正文没有用 `srun` 启动程序，只有第一个节点跑了 body |
| HPC 一直 `RUNNING`、`Steps` 停在 `-/N` | Slurm 级请求超过买下的节点总量，step 在排队；停掉重提，别等 |
| HPC 程序中途没了、日志断在半截且无报错 | 撞 Pod cgroup 内存墙被 OOM 杀掉；或程序按 `nproc`（报宿主机 64 核）自动开了几十个工作进程 |
| `SUCCEEDED` 但产物为空 | 程序提前退出、资源贴边或输出路径不对 |
| Quota Match Failed | Workspace / Group / `gpu,cpu,mem` 三元组不匹配 |
| 挂在后台的等待命令长期没有任何输出 | 任务早已是终态，等不到它变回运行中；先 `status` 读一次，再决定重新 `create` 还是收工 |
