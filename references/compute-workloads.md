# Job、HPC、Ray 与 Serving

在 GPU Job、CPU HPC、Ray 和 Serving 之间选型，或提交后观察 Events / Logs / Metrics / Instances / Status 时看本页。资源目录和 Profile 看 [`resources.md`](resources.md)，镜像看 [`image.md`](image.md)，模型仓库看 [`model.md`](model.md)。命令语法和参数以 CLI Help 为准。

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
- 平台会为训练任务单独跑 TensorBoard，`job tensorboards` 列出当前账号的那些。真正能用的字段是 Summary Path——event 文件在共享盘上的目录，同项目任意 Notebook 直接读，不需要浏览器。
- 任务结束后容器默认立即释放。需要事后进容器看现场时，在创建时设置成功 / 失败保留时长，任务会停在保留态等待，过期自动释放；这是排查失败训练最省事的路径，比重跑一次便宜。
- 状态变化通知（`--enable-notification`，收件人固定为当前用户绑定的飞书账号）和自动容错默认关闭，除非明确启用。
- 需要项目级持久默认值时写 `[job]` 配置段；提交前用 `job create --dry-run` 检查 Shared Memory、通知和容错的最终生效值。
- 多节点训练要关注每个 Pod 的 GPU、显存、CPU 和网络曲线是否同步；某个 Worker 长期低负载通常比日志更早暴露问题。
- 排除坏节点是“不要调度到这些 Ready 节点”，不是固定节点；候选节点来自所选 Compute Group。

优先级是 Workspace 能力限定的调度信号。qz 公平调度 Workspace 只接受 `1=LOW`（可抢占）或 `4=HIGH`（稳定且可抢占 LOW），默认 4；其他 Workspace 保留 `1–10`，默认 10。CLI 按当前 Workspace 的公平调度标记自动选择优先级合同，项目策略仍可能降低最终优先级。任务需要稳定训练但显示 LOW 时，先 stop，再按当前 Workspace 和项目策略重提。

平台还会逐行限制 Quota 能用的优先级，`job quota` 的 `Priority` 列显示这条声明：`low` 的行只接受 `--priority 1`（可抢占），`any` 不受这层限制，`unknown` 表示这次没读到。`job create` 在发出创建请求前按这一列预检并说明改法，细节见 [`resources.md`](resources.md)。限制按每个 Job Instance 的 Quota 判断，不按 `quota.gpu × --nodes` 的总卡数判断；例如 2 个 4 GPU Instance 仍是两个碎卡实例。提交后用 Status / Events 核实解析后的优先级和调度结果。

## 4. HPC

HPC 有两层资源模型，不能混：

| 层级 | 含义 |
| --- | --- |
| 节点级 | 每个节点的 GPU / CPU / 内存，以及申请多少个节点 |
| Slurm 级 | 程序如何在这些节点内拆 task、CPU 和内存 |

两层之间**平台不做任何校验**，控制台也不做：`hpc create` 因此在提交前自己挡下三种必然跑不起来的组合——单个任务要的 CPU 超过一个节点、单节点上所有任务的内存超过节点内存、任务总数乘每任务 CPU 超过买下的节点总核数。前两种在平台上的表现是起来后一两分钟内就 `FAILED`，且 `logs` 和 `events` 都不带原因；第三种更隐蔽，平台一直报 `RUNNING`，Slurm 里的 step 却永远排队，直到 Workspace 的运行时长上限把它停掉。`--cpus-per-task` / `--memory-per-cpu` 不给时按「一个节点上落几个任务」推导，所以只调 `--number-of-tasks` 不会再造出上面第三种。

内存只按每 CPU 给（`--memory-per-cpu`，Slurm `--mem-per-cpu`）。网页端另有「每节点使用内存」输入框，**它是坏的**：平台收下并在详情页显示这个值，但生成脚本时只写 `--mem-per-cpu`，于是那一行是空的，任务必然失败。不要在网页端用它，CLI 也不提供对应选项。

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

创建后用 `ray events` 看调度、`ray instances` 看 Head 与实际 Worker、`ray metrics` 看各组负载；结束后先 `ray stop`，确认不再需要再 `ray delete`。重复配置用 `ray profile`，矩阵提交用 `ray batch`。

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

当前 Custom Serving 生命周期是：`serving configs` / `serving quota` 选配置，`serving create` 或 `serving batch` 创建，`serving list/status/events/instances/metrics` 观察，`serving stop` / `serving start` 控制，最后 `serving delete` 清理。重复调度条件用 `serving profile`。

副本数用 `serving scale` 调整，其余配置原样保留；每个副本各占一份完整规格，扩容前先看 `resources availability`。历史配置用 `serving versions` 列出，`serving rollback --version` 按某个历史版本重新部署——副本会被替换，在途请求和重启一样会断。

两套指标不要混：`serving metrics` 看 GPU / CPU / 内存这类资源占用，`serving api-metrics` 看请求量、成功率和延迟。「没人调用」和「一直调用一直失败」只有后者分得清。

## 7. 观察闭环

| 工具 | 主要回答 |
| --- | --- |
| `events` | 为什么排队、为什么启动失败、调度器或控制器拒绝了什么 |
| `logs` | 程序自身报错、训练进度、业务输出 |
| `metrics` | 已启动任务是否仍在有效工作，Pod / Task / Replica 是否均衡 |
| `instances` | 实际运行单元是否齐全，是否有部分 Pending 或异常；每个 Pod 落在哪个节点 |
| `status` | 平台状态、优先级、基础摘要、落在哪些节点 |

卡住或失败先看 Events；已启动但健康度不明看 Metrics；程序行为看 Logs；产物完整性回到共享盘文件和 Fingerprint。

节点归属分两层，两层都是 Live 事实，任务离开运行态就清空：`status` 给任务级的节点清单（`job` 的 `Nodes` / `Pinned Nodes` / `Excluded Nodes`、`hpc` 与 `serving` 的 `Nodes`、`notebook` 的 `Node` 带节点健康），`instances` 给 Pod 级的 `Node` 列。多节点任务定位掉队的那一个 Worker 用 `instances`，因为只有它把 rank 和节点对上；`ray` 的节点归属只有 `instances` 这一层，`ray status` 的 `head_node` / `worker_groups` 是规格不是落点。**空的节点清单读作「还没被调度」，不是「查不到」。**

`logs` 在 `job` / `hpc` / `ray` / `serving` 下都有，共用同一套记录与字符预算和同一份 `--json` schema；Notebook 没有 `logs`，它是交互式容器，用 `notebook exec` 或 `notebook shell` 直接读。日志按实例采集后合并成一条时间线，每行带实例标识（`hpc` / `ray` 用 `instances` 打印的角色或序号，`job` 与 `serving` 用 Rank），`--instance` 只读其中一个或几个。**平台侧根本没有「工作负载级日志」这一层**——日志端点只按 Pod 名取，所以全实例聚合不是选择而是唯一形态。日志记录里另有平台填的 `node` 字段，只在 `--json` 里可见，且不是每类工作负载都填，Pod 与节点的对应关系以 `instances` 的 `Node` 列为准。

`events` 与 `logs` 的默认口径一致：不加参数就是这个工作负载能拿到的全部，`--instance` 收窄到某个实例，`--workload-level` 反过来只留控制器那一半（两者互斥）。四类的默认都把两套不相交的视图合成一条时间线——控制器事件说「任务为什么没被创建、为什么整体排不上」，Pod 事件说「哪个实例没被调度、镜像拉没拉下来、容器起没起来」（`FailedScheduling` / `Pulling` / `Started` / `BackOff`）——并多出一列 `Instance` 指明每行来自哪个实例，控制器行在这一列是 `-`。实例标识与各自 `instances` 一致：`hpc` 与 `ray` 是角色 / 序号，`job` 与 `serving` 是 Rank。取数代价各不相同但对调用方不可见：`job` 一次请求带 200 个 Pod，`ray` 一次调用本来就同时返回两级，`serving` 两级各一次调用，`hpc` 一个实例一次请求、并发取。Notebook 是单实例，两个开关都没有。**排查顺序建议先看默认的合并视图**：只有已经知道问题出在整体调度、不在某个 Pod 上时，`--workload-level` 才值得用——它省掉的是实例那几次请求，不是一次判断。

## 8. 异常判断

| 现象 | 优先怀疑 |
| --- | --- |
| `PENDING` 过久 | 优先级不足、实时配额不足、节点条件不满足 |
| `CREATING` 卡死 | 镜像拉取失败或节点初始化 |
| `instances` 部分 Pending | 多节点或多副本调度不均 |
| `logs` 为空但 `RUNNING` | 主进程未输出、日志路径不在 CLI 管理范围、程序没真正启动 |
| `FAILED` 但无业务报错 | OOM、显存溢出、节点驱逐或控制器失败 |
| HPC `Steps` 是 `0/0` 或 `-/0` | Slurm 正文没有用 `srun` 启动程序，只有第一个节点跑了 body |
| HPC 一直 `RUNNING`、`Steps` 停在 `-/N` | Slurm 级请求超过买下的节点总量，step 在排队；停掉重提，别等 |
| `SUCCEEDED` 但产物为空 | 程序提前退出、资源贴边或输出路径不对 |
| Quota Match Failed | Workspace / Group / `gpu,cpu,mem` 三元组不匹配 |
