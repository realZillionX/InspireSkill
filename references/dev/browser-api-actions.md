# Browser API：Action 参考表

> **文档类型**：CLI 维护者参考。协议、信封、认证、分页、scoping、探针方法和验收标准在 [`browser-api.md`](browser-api.md)，本页不重复。
>
> 每条都是 `POST {base_url}/api/v2/{路由}?Action={Action}`，请求体是 JSON，响应取 `ResponseMetadata` / `Result` 信封里的 `Result`。「请求体」列写的是 **CLI 实际发出的键**，不是 discovery 声明的全集；「响应」列写的是**实测的线上键**，discovery 声明的 `Items` / `TotalCount` 在多数 Action 上不是真的。

12 条路由、108 个 Action。`†` 标记的 Action 不在 `discovery` 里，但路由活着、Action 可调；`‡` 标记的整条路由不在 discovery 里。

| 路由 | 域 | Action 数 | 主要 CLI 命令组 |
| --- | --- | --- | --- |
| [`train`](#train--分布式训练) | GPU 训练任务 | 10 | `job` |
| [`hpc`](#hpc--cpu-slurm-批处理) | CPU Slurm 批处理 | 11 | `hpc` |
| [`ray`](#ray--弹性计算) | 弹性计算 | 12 | `ray` |
| [`notebook`](#notebook--交互式建模) | 交互式建模 | 18 | `notebook`、`image` |
| [`inference_serving`](#inference_serving--模型部署) | 模型部署 | 19 | `serving` |
| [`workspace`](#workspace--工作空间资源) | 计算组、节点、配额、用量 | 9 | `resources`、`<workload> quota`、每个 `create` |
| [`user`](#user--账号) | 账号身份与权限 | 3 | `account permissions`、所有按当前用户过滤的列表 |
| [`project`](#project--项目) | 项目 | 4 | `project`、每个 `create` |
| [`image`](#image--镜像) | 镜像 | 5 | `image` |
| [`model-hub`](#model-hub--模型仓库) | 模型仓库 | 14 | `model`、`serving create` |
| [`file`](#file--文件页-) ‡ | 存储池与目录发现 | 2 | `init --scope project` |
| [`dataset`](#dataset--官方数据集挂载-) ‡ | 官方数据集挂载 | 1 | `dataset validate`、`--dataset` |

**没有 CLI 消费者的 Wrapper**（存在、有测试覆盖，但当前没有命令调用）：`notebook.ListNotebookLifecycles`、`notebook.ListNotebookCreators`、`ray.ListJobCreators`、`inference_serving.GetInferenceServingTerms`、`model-hub.ListModelVersionOptions`、`model-hub.ListModelCreators`、`model-hub.GetModelPublishPrefill`、`model-hub.GetModelPublishStatus`、`project.GetProjectForPage`。它们在表里照常列出，CLI 列写「—」。

---

## `train` — 分布式训练

Referer：`/jobs/distributedTraining`，详情页 `/jobs/distributedTrainingDetail/{job_id}`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateJobConsole` † | 见下方创建字段表 | `{job_id, sub_code, sub_msg}` | `job create`、`job batch` |
| `GetJob` | `{job_id}` | 完整任务对象：`name` / `status` / `command` / `framework_config[]` / `dataset_info[]` / `node_infos[]` / `specified_nodes[]` / `exclude_nodes[]` / `project_id` / `workspace_id` / `logic_compute_group_name` / `created_at` / `finished_at` | `job status`、`job command`、`job logs`、`job metrics`、`job wait` |
| `ListJobs` | `{workspace_id, page_num, page_size, created_by, status?, keyword?}` | `{jobs[], total}` | `job list`、Name Resolver、`cache refresh` |
| `ListJobInstances` | `{job_id, page_num, page_size}` | `{items[], total}` | `job instances`、`job shell`、`job logs`、`job events` |
| `ListJobEvents` | `{PageNumber\|page_num, page_size, filter:{object_type, object_ids[]}}` | `{events[], total}` | `job events` |
| `GetJobLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}` | `job logs` |
| `ListTensorboards` | `{workspace_id, created_by, PageNumber, page_size}` | `{items[], total}` | `job tensorboards` |
| `StopJob` | `{job_id}` | `{code, message}` | `job stop` |
| `DeleteJob` | `{job_id}` | — | `job delete` |
| `GetTaskMetric` | 见 [Metrics](#metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `job metrics` |

**参数语义与限制**

- **`ListJobEvents` 一个 Action 管两种事件**，靠 `filter.object_type` 区分：`"job"` 给控制器级事件（`SetPodTemplateSchedulerName`、`Unschedulable`），`"instance"` 给 pod 级事件（`FailedScheduling` / `Scheduled` / `Pulling` / `Started`，更丰富）。`object_ids` 在前者是 `[job_id]`，在后者是 pod 名列表，**按 200 个一批**分块。
- 另有一个同名易混的 `ListJobInstanceEvents`（参数 `job_id` + `instance_name`）：它无论返回多少条，`total` 都是 `"0"`，**需要分页的调用方不要用它**，`browser_api` 也没有封装。
- **`GetJobLog` 的两个时间戳是字符串字段**，值却是 epoch 毫秒；后端拒绝任何宽于一个月的窗口。不接受 sorter。
- **`ListTensorboards` 有两处不可猜**：分页参数只认 PascalCase `PageNumber`（`page` / `page_num` 被静默忽略并返回空列表）；**不带 `created_by` 时返回整个 Workspace 的 `total` 配一个空 `items`**——读起来像「你一个都没有」，实际是那批行不归你读。行里真正有用的是 `tb_summary_path`（共享盘上的 event 目录，任何同项目 Notebook 都能读），不是 `url`（平台内部路径，要浏览器才有意义）。`status` 是 `tb_status_running` / `tb_status_stopped`，CLI 去掉前缀。
- **`DeleteJob` 要求先停止**，运行中删除返回 `Conflict: 当前状态（运行中）无法删除`。找不到资源时它返回 **`AccessForbidden`**，不是 `ResourceNotFound`（与 `hpc.DeleteJob` 相反）。
- `ListJobs` 的 `total` 当前是 int，但 `hpc` / `ray` 同名字段是 string，一律走 `_coerce_total()`。
- `JobInfo` 从 `framework_config[0]` 读规格：`gpu_count` / `cpu` / `mem_gi` / `shm_gi` / `instance_count`，GPU 型号在 `instance_spec_price_info.gpu_info.gpu_type_display`。
- **节点归属分三个字段，语义不同**：`node_infos[]` 是**实际落点**（元素 `{node_name}`，被调度后才有值，任务停下即清空），`specified_nodes[]` 与 `exclude_nodes[]` 是**创建时的请求侧**（裸字符串数组，`exclude_nodes` 对应 `job create --exclude-node`）。`node_count` 是请求的节点数，与 `framework_config[0].instance_count` 同值——**它不是 `node_infos` 的长度**，排队中的任务两者会不相等。同一层的 pod 级落点在 `ListJobInstances` 行的 `node`（裸字符串），多节点任务只有它能把 rank 和节点对上。

---

## `hpc` — CPU Slurm 批处理

Referer：`/jobs/highPerformanceComputing`，详情页 `/jobs/hpcDetail/{job_id}`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateJobConsole` † | 见下方创建字段表 | `{job_id, sub_code, sub_msg}` | `hpc create`、`hpc batch` |
| `GetJob` | `{job_id}` | `{job_name, status, sbatch_script{}, slurm_cluster_spec{}, nodes[], steps, description, ttl_after_job_finish_seconds, dataset_info[], project_id, workspace_id, …}` | `hpc status`、`hpc metrics` |
| `ListJobs` | `{workspace_id, page_num, page_size, created_by, status?}` | `{jobs[]\|items[], total}`（`total` 是**字符串**） | `hpc list`、Name Resolver、`cache refresh` |
| `ListJobEvents` | `{pageNum: -1, pageSize: 200, filter:{object_ids:[job_id], object_type:"HPC_JOB"}, sorter:[{field:"last_timestamp", sort:"ascend"}]}` | `{events[]\|items[]\|list[]}` | `hpc events` |
| `ListJobInstances` | `{jobId, page_num, page_size}` | `{items[]\|list[], total}` | `hpc instances` |
| `ListSlurmdPodEvent` | `{instance_id, page_size, PageNumber}` | `{events[], total}`（`total` 是字符串） | `hpc events --instance` |
| `GetJobLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}`（`total` 是 **int**） | `hpc logs` |
| `GetHpcScheduleConfig` | `{workspace_id}` | `{enable_auto_stop, auto_stop_ruleset, enable_max_running_time, max_running_time_days/hours/minutes, predef_node_spec}` **或字面量 `null`** | `resources policy` |
| `StopJob` | `{job_id}` | `{job_id, sub_code, sub_msg}` | `hpc stop` |
| `DeleteJob` | `{job_id}` | `{job_id, sub_code, sub_msg}` | `hpc delete` |
| `GetTaskMetric` | 见 [Metrics](#metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `hpc metrics` |

**参数语义与限制**

- **`ListJobInstances` 的 id 键是驼峰 `jobId`**，与同一路由上其它 Action 的 `job_id` 不一致。它的行里有 pod 级落点 `node`（裸字符串，如 `hpc-compute003`），外加 `component`（`slurmctld` / `slurmd`）。
- **`GetJob.steps` 是「程序到底跑没跑」的唯一信号**，形如 `已完成/总数`：创建后立刻是 `-/-`，平台**静态解析 entrypoint** 后变成 `-/N`（N = 正文里 `srun` 的个数，没有 `srun` 就是 `-/0`），跑完变成 `N/N`。所以 `SUCCEEDED` + `steps=0/0`（正文没 `srun`，只有第一个节点执行了 body）和 `SUCCEEDED` + `steps=1/1` 在其它任何字段上都长得一模一样。**报 HPC 状态必须把 `steps` 一起报出来。**
- **`GetJob.nodes` 是 Slurm 集群的实际落点**，discovery 声明为字符串数组，实测停止的任务恒为 `[]`——**空数组要读作「没在跑」而不是「读不到」**。带数据的形状尚未在活任务上复核，pod 级的 `ListJobInstances.node` 是同一事实的已验证来源。
- **`ListJobEvents` 的分页键也是驼峰**（`pageNum` / `pageSize`），并且平台会回收已完成任务的事件，所以「查不到事件」在保留期过后是正常稳态。
- **`GetJobLog` 与 `ListSlurmdPodEvent` 的实例名都必须带命名空间**（`<ns>/<pod>`）。日志端裸名报 `InvalidParameter: … the hpc job ids length of instances expect 1, but got 0`；事件端裸名和 `job_id` 都**静默回空**。
- **`GetJobLog` 的 sorter 要么不发，要么发控制台那一对**：`[{field:"@timestamp"}, {field:"log-id.keyword"}]` 被接受，只发其中一个报 `InternalError: 日志排序字段不合法，仅支持按时间 + log-id 排序`。Wrapper 选择不发，排序在客户端做。
- **`GetJobLog` 的 `start_timestamp_ms` 必须早于 `end_timestamp_ms`**，倒过来报 `InternalError: 日志查询时间参数不合法`。控制台自己发的就是倒序的一对（start 在 end 之后约 12 小时），所以网页端的聚合日志在这条路径上是坏的——CLI 不要照抄。
- **`GetJobLog` 的时间窗超过一个月报 `InternalError: 日志查询时间区间不能超过1个月`。** 这是个确定性的用户错误，却撞进了 transient 名单——不在客户端 clamp 就会先白烧三次退避重试，再抛出一条看起来像平台故障的错。Wrapper 用 `HPC_LOG_MAX_WINDOW_MS`（30 天）挡在前面。
- **`GetJobLog` 的 `page_size` 省略或传 `-1` 都只回 100 条**（不是「全部」），`PageNumber` 被彻底忽略，而且 **`page_size=N` 保留的是最旧的 N 条**。所以「最后 N 条」平台点不到，必须先取满窗口再在客户端截尾。*（这一条来自 Wrapper 作者的实测；复核时可见的 HPC 任务日志已过保留期，未能独立复现。）*
- **`ListSlurmdPodEvent` 的 `page_size` 必发**：省略时回空列表配非零 `total`，与 `ListLogicComputeGroups` 同病。它的行没有 `type` 也没有 `count`——**平台按发生次数逐行重复**（一个实例 `total=106`，去重后只有 20 行），所以读事件前要自己折叠，否则 `--tail 20` 会全是同一条。
- **列表行的名字键是 `job_name`**，`name` 从来没被填充过——读 `name` 会让每个 HPC 任务都没有名字，列表渲染 N/A 且 Name Resolver 匹配不到任何东西。
- **`DeleteJob` 要求先停止**，运行中删除返回 `Conflict`；id 不存在返回 `ResourceNotFound`。**`SUCCEEDED_RETAINING` / `FAILED_RETAINING` 也算「运行中」**：任务已经结束，`DeleteJob` 仍答 `Conflict: 当前状态（运行中）无法删除`，而 `StopJob` 对它返回成功却不解除保留态——只能等平台自己释放（实测约一分钟）。清理脚本要按状态离开 `*_RETAINING` 来重试，不要按 `StopJob` 的返回值判断。
- discovery 对 hpc 的每个 Action 声明的参数与线上不一致（`ListJobs` 声明 `PageNumber`，实发 `page_num`），实测 v1 的请求体逐字被接受。

---

## `ray` — 弹性计算

Referer：`/jobs/ray`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateJob` | 见下方创建字段表 | `{ray_job_id, sub_code, sub_msg}` | `ray create`、`ray batch` |
| `GetJob` | `{ray_job_id}` | 完整任务对象：`status` / `head_node{}` / `worker_groups[]` / `entrypoint` / `creator{}` / `priority_name` | `ray status`、`ray metrics` |
| `ListJobs` | `{workspace_id, page_num, page_size, filter_by:{user_id:[…]}}` | `{items[], total}`（`total` 是字符串） | `ray list`、Name Resolver、`cache refresh` |
| `ListJobCreators` | `{workspace_id}` | `{items[]}` | — |
| `ListJobEvents` | `{ray_job_id, page_num, page_size, sorter:[{field:"last_timestamp", sort}], filter:{object_ids[]}?}` | `{items[], total}` | `ray events`、`ray events --instance` |
| `ListJobInstances` | `{ray_job_id, page_num, page_size}` | `{items[], total}` | `ray instances` |
| `ListJobScalingHistories` | `{ray_job_id, page_num, page_size, worker_group_name?}` | `{items[], total}`（`total` 是字符串） | `ray scaling` |
| `GetJobLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}` | `ray logs` |
| `StartJob` | `{ray_job_id}` | `{ray_job{}}` | `ray start` |
| `StopJob` | `{ray_job_id}` | `{ray_job{}}` | `ray stop` |
| `DeleteJob` | `{ray_job_id}` | `{ray_job{}}` | `ray delete` |
| `GetTaskMetric` | 见 [Metrics](#metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `ray metrics` |

**参数语义与限制**

- **资源键在每个 Action 上都是 `ray_job_id`**；`job_id` 和 `id` 都报 `unknown field`，与 `train` / `hpc` 不同。**唯一的例外是 `GetJobLog`**：它反过来只认 `job_id`，`ray_job_id` 是 `unknown field`。
- **`GetJobLog` 的 `job_id` 不 scope 任何东西**：单独发它答 `InternalError`，与 pod 名同发既不收窄也不解析。真正的定位是 `filter.podNames`，平台把它反解回唯一一个 job，权限检查也落在这里（`InvalidParameter: Invalid instance names, the ray job ids length of instances expect 1, but got 0`）。控制台对 `ray` 不发 `job_id`（只对 `hpc` 发），Wrapper 照做。
- **`GetJobLog` 的 `filter` 只收 `podNames` / `start_timestamp_ms` / `end_timestamp_ms`**；`worker_group_name`、`instance_type`、`keyword`、`object_type` 全是 `unknown field`。时间戳是字符串型 epoch 毫秒，传 int 报 `invalid value for string field endTimestampMs`。
- **空或缺失 `podNames` 回一个干净的 `{"logs": [], "total": 0}`**，与「这个集群什么都没打印」不可区分——Wrapper 在发出前就拒绝，不让这个歧义进到调用方。
- `ListJobScalingHistories` 的行是 `{event_time(epoch ms), event_type ∈ initialized|scale_up|scale_down, replicas_before, replicas_after}`，合同里没有 `filter` 也没有 `sorter`。
- **Workspace scoping 是顶层 `workspace_id`**，`filter` 嵌套在这里会被拒。
- **没有 `CreateJobConsole` 变体**，`ray` 对它答 `InvalidAction`，创建走 `CreateJob`。
- **`ListJobEvents` 的信封是专用的**：资源键是 `ray_job_id`，不是 train / HPC 那对 `filter.object_ids`。但**一次调用就同时给两级事件**——在一个真实两 Pod 集群上实测 17 行里 3 行是 `object_type: "job"`（`CreatedRayCluster` / `CreatedService`，来自 `rayjob-controller`），14 行是 `object_type: "instance"`，`object_id` 就是 Pod 名。**不需要像 `hpc.ListSlurmdPodEvent` 那样按实例扇出。**
- **`filter.object_ids` 有效**，早先记的「没有 `object_type`，传了返回 `参数错误`」已被真实任务证伪：给 Pod 名收窄到那些实例，同时把控制器行一起滤掉；不认识的 id 回空列表。`filter.object_type` 只认字面量 `instance`，`RAY_JOB_INSTANCE` 和 `ray_job` 都回 0 行，所以 Wrapper 只发 `object_ids`。
- 事件是 K8s 形状（`reason` / `type` / `message` / `first_timestamp` / `last_timestamp` / `count`），但上报方在 `source_component` 而不是 `from`，另有一个单调递增的 `id`。**时间戳只到秒**，同一个容器的 `Pulled` / `Created` / `Started` 常常同秒，平台的同秒次序还随 filter 变，所以客户端排序要拿 `id` 做 tiebreaker，否则「按时间倒着取一屏再翻回来」会把因果顺序翻反。关键信号是提交时的 `CreatedRayCluster`（Normal）和卡在 PENDING 时的 `FailedScheduling`（Warning）。
- **`ray` 把状态机拒绝报成 `InternalError: RayJob status not allow <动词>`，而不是 `Conflict`。** 对非 STOPPED 的任务 `StartJob`、对已 STOPPED 的任务 `StopJob` 都走这一条。`InternalError` 在瞬时错误名单里，于是一个「从这个状态永远不可能成功」的拒绝被读成「平台暂时不舒服」，还会白烧三次退避重试。只有 `DeleteJob` 做对了，它给的是 `Conflict: 当前状态（运行中）无法删除`。
- **`UpdateJob` 只能改停止的任务**：运行中答 `Conflict: Ray Job 正在运行中`；缺 id 答 `InvalidParameter: RayJobId is required`。在一个真实拥有的 STOPPED 任务上逐字段量过，**真正可写的只有 `name` 和 `description`**（局部更新，只发一个不会清掉另一个），另外 27 个候选键——含 `worker_groups` / `head_node` / `min_replicas` / `max_replicas` / `replicas` / `task_priority` 等所有可能的伸缩杠杆——全部 `unknown field`；`ScaleJob` / `UpdateWorkerGroup` / `ResizeJob` 一类的兄弟 Action 也都 `InvalidAction`。**弹性区间在创建时就定死了**，所以 `UpdateJob` 不封装：它只能改名，而一个 Name-only 的 CLI 改名等于让自己的名称索引失效。
- **早先记的「`StartJob` 受理但不执行」没有复现。** 在一个真实任务上跑了三轮停/启：`StartJob` 回显的 `ray_job` 与随后 `GetJob` 逐字段一致，任务立刻离开 `STOPPED`，`updated_at` 与 `started_at` 都动了。真实存在的只有另一半——**重复调用答 `InternalError`**，那正是上一条的状态机拒绝。`ray start` 仍然轮询确认状态，这是本仓库对写操作的通用纪律，代价只是成功路径上多一次读。
- **列表行的属主键是 `creator`、优先级键是 `priority_name`**；`created_by` 和 `priority` 恒为 null，只读那两个会让每个任务都没有属主、优先级恒 None。
- **`UpdateJob` 不是弹性伸缩杠杆**，只收 `ray_job_id` / `name` / `description`；`worker_groups`、`head_node`、`entrypoint`、`min_replicas`、`replicas`、`task_priority`、`project_id` 全被拒。它是改名字，不是改集群，因此没有封装。
- 实例行是 pod 形状：`instance_id` / `instance_type`（`head` / `worker`）/ `worker_group_name` / `status` / `cpu_count` / `memory_size` / `gpu_count` / `priority_level` / `created_at`。
- **`GetJobLog` 的成功路径已在真实任务上跑通**：`Result = {logs, total}`，`total` 是 **int**（与 `ListJobScalingHistories` 的字符串 `total` 相反）。行键是 `log_id` / `message` / `node` / `pod_name` / `time` / `timestamp_ms`（字符串）/ `timestamp_str`，与 discovery 声明的**元素**结构逐字段一致；`time` 带 `+08:00` 偏移，`timestamp_str` 是 Z 归一化形式。
- `ListJobInstances` 的行比早先记的多几个键：除已列出的之外还有 `name`、`node_name`、`pod_ip`、`started_at`、`priority_name`、`ray_job_id`。
- **仍未验证**：`ListJobScalingHistories` 的空路径跑通了（`{items: [], total: "0"}`），但**带数据的行没见过**——探针任务的 head 起不来，没有触发任何 `initialized` / `scale_up` 记录，所以行字段仍照 SPA 渲染代码写。`GetJobLog` 的 30 天窗口上限在 `ray` 上同样探不到（实例名解析先于窗口校验），命令防御性 clamp。
- **账号可见的 295 个镜像里没有一个自带 `ray` 二进制**（官方 `inspire-ubuntu:24.04-base-ascend` 的 head 直接 `ray: command not found` 崩溃循环）。建 Ray Job 的人要自带镜像，这不是 CLI 能兜的。

---

## `notebook` — 交互式建模

Referer：`/jobs/interactiveModeling`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateNotebook` | 见下方创建字段表 | `{notebook_id, sub_code, sub_msg}` | `notebook create`、`notebook batch` |
| `GetNotebook` | `{notebook_id}` | 完整实例对象：`status` / `sub_status` / `workspace{}` / `project{}` / `logic_compute_group{}` / `quota{}` / `node{}` / `extra_info{}` / `dataset_info[]` | `notebook status` / `metrics` / `exec` / `shell` / `ssh` / `proxy-url`、`wait` 轮询 |
| `ListNotebooks` | `{workspace_id, page, page_size, filter_by:{keyword, user_id[], logic_compute_group_id[], status[], mirror_url[]}, order_by:[{field:"created_at", order:"desc"}]}` | `{list[], total}`（`total` 是 int） | `notebook list`、Name Resolver、`cache refresh` |
| `ListNotebookCreators` † | `{workspace_id}` | `{list[], total}` | — |
| `ListNotebookEvents` | `{notebook_id, page, page_size}` | `{list[]\|events[], total}` | `notebook events`、`notebook create` 的等待预览 |
| `ListNotebookLifecycles` | `{notebook_id, page, page_size, start_time?, end_time?}` | `{list[]}` | — |
| `ListRunIndex` | `{notebook_id}` | `{list[]}`，每项 `{index, start_time, end_time}` | `notebook lifecycle` |
| `StartNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook start` |
| `StopNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook stop` |
| `DeleteNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook delete` |
| `SaveNotebookImage` | `{notebook_id, name, version, description}` | **恒为 `{}`**（`Result: null`） | `notebook save-image` |
| `EstimateSaveMirrorSize` | `{notebook_id}` | `{active_snapshot_size}` | `notebook save-image`、`--dry-run` |
| `CancelSaveMirror` | `{notebook_id}` | — | `notebook cancel-save-image` |
| `CheckNotebook` | `{name, workspace_id}` | 占用时 `{notebook_id, sub_code, sub_msg}`；空闲时 `Result: null` | `notebook create` 的重名前置校验 |
| `GetNotebookAccessUrl` | `{notebook_id}` | `{jupyter_url, vscode_url}` | `notebook proxy-url`、`exec` / `shell`、SSH 链路 |
| `GetRealtimeNotebookMetric` | `{notebook_id}` | `{resource_metric_list[]}` | `notebook metrics --now` |
| `GetScheduleConfig` | `{WorkspaceId}` | Workspace 调度策略全集：回收 / 定时关机 / 各 Workload 的运行时长，外加四份**规格菜单** `quota` / `predef_train_spec` / `rayjob_quota` / `serving_quota` | `resources policy`、`<workload> quota` 的 Priority 列、`create` 的优先级预检 |
| `GetTaskMetric` | 见 [Metrics](#metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `notebook metrics` |

**参数语义与限制**

- **分页列表键是 `list`，不是 `items`**（与 `ray` 相反），`total` 是 int（与 `hpc` 相反）。**逐 Action 实测，不要类推。**
- **`ListRunIndex` 无分页**，传 `PageNumber` 直接报错。`end_time = ""` 的那条是当前运行周期，按 `index` 从旧到新排。控制台的「生命周期」页就是用它渲染的——`ListNotebookLifecycles` 对绝大多数 Notebook 返回空列表。
- **`ListNotebookEvents` 的事件字段是平台自有形状**，不是 K8s 形状：`content`（正文）、`created_at`（epoch-ms 字符串）、`event_id`。共享渲染器把 `content → message`、`created_at → last_timestamp` / `first_timestamp`。事件按从旧到新返回，Wrapper 默认自动翻页到 `total`（安全上限 100 页）。
- **找不到资源时返回 `ResourceNotFound`，HTTP 仍是 200**，不再是 v1 的传输层 404。依赖 404 判断「不存在」的调用方必须同时认这个码。
- **`node{}` 是整个节点对象，不只是 GPU 型号**：`name`（如 `cpu-nat-351`）、`status`、`cordon_type`、`is_maint`、`resource_pool`、`cpu_count` / `memory_size` / `gpu_count` 都在里面。**STOPPED 的 Notebook 不清空这个对象，而是把 `name` 置空、`status` 置成 proto 零值 `UNKNOWN_NODE_STATUS`**（同族还有 `unknown_node_type` / `unknown_credit_score`）——只判空对象会把「没在跑」读成「有一个状态未知的节点」。同一份落点还有第二个来源 `extra_info`（`NodeName` / `HostIP` / `PodName` / `ContainerID`），停止时同样是空串而不是缺键。
- **`SaveNotebookImage` 不收 `visibility`**（`unknown field "visibility"`），要改可见性只能存完再调 `image.UpdateImage`。它**不返回新镜像的 id**，调用方只能靠列表去找。同一层还有 `EstimateSaveMirrorSize` 和 `CancelSaveMirror`，当前未封装。
- **`GetNotebookAccessUrl` 是 IDE 网关地址，不是 Notebook Proxy。** 两个 URL 归一化后指向同一个网关（两个 IDE 共用同一套 runtime 与 token），任取其一即可。STOPPED 的 Notebook 上它返回两个空字符串，此时回落 Playwright 抓取（那条也会失败，语义不变）。实测 **0.57 秒 vs 6.4–36 秒**。
- **解析顺序是 缓存/热候选 → `GetNotebookAccessUrl` → Playwright**，收口在 `resolve_notebook_vscode_ide_url`。`refresh=True` 时 API 这一档**也走**：refresh 的语义是「别信缓存」，不是「一定要抓」。
- **`exec` / `shell` 全程不起浏览器**：lab URL 取**原始 `jupyter_url`**（不能用 `_ide_gateway_url` 归一化后的形式——terminal 的 REST 与 WebSocket 路由挂在 Jupyter server base 上），`_xsrf` 靠对 `jupyter_url` 发一次普通 GET 拿 cookie，建/删 terminal 是 `POST` / `DELETE api/terminals` 并把 `_xsrf` 放进 `X-XSRFToken` 头。命令结束后回收本次创建的 Terminal。
- **`EstimateSaveMirrorSize` 的 `active_snapshot_size` 单位是字节，而且线上是十进制字符串**（discovery 声明 int64）。它是容器可写层的增量，不是最终镜像的总大小。非 RUNNING 的 Notebook 答 `InvalidParameter: Cannot save image of non-running notebook: <id>`——**这条消息内嵌 raw notebook_id**，投影时必须折掉；未知 id 答 `ResourceNotFound: notebook not found`。取不到大小要读作「未知」，绝不能读作 0。
- **`GetRealtimeNotebookMetric` 收到空 / 缺失的 `notebook_id` 不报错，而是用成功信封返回整个集群的汇总**（实测 CPU total 159682.1、GPU total 7765、已用 1743.12）。任何不做前置校验的调用方都会把这个印成「这一个 Notebook 占了上千张卡」。**Wrapper 必须在发出前拒绝空 handle。**
- `GetRealtimeNotebookMetric` 的 `resource_metric_list` 固定四行 `{resource_name, total, used, available, usage_rate, unit, spec}`，`usage_rate` 是 0–1 比率，`unit` 只有 Memory 是 `"GB"`，`spec` 恒空，没有 disk / network 行。**STOPPED 的 Notebook 四行全 0 且 HTTP 成功**，与「RUNNING 但空闲」不可区分，所以命令层必须同时打印状态。
- **`GetRealtimeNotebookMetricByTime` 刻意不接**：它只收 `notebook_id`（`time_range` / `metric_types` 都是 `unknown field`），固定约一小时窗口、5 秒粒度，返回同样拼错的 `time_seris_metric_groups`。`metrics --window 1h` 已经用同样四个指标覆盖同一小时，而 CLI 的输出预算只打 min/max/avg/last 加 sparkline，5 秒与 60 秒的差别在这个粒度上不可见；它又只存在于 `notebook` 路由，进不了共享的 metrics 命令工厂。
- **规格菜单也在这里，而且只在这里。** 四个 `*_quota` / `predef_train_spec` 字段的值是 **JSON 编码的字符串**（不是数组），元素形如 `{id, cellId, name, cpu_count, memory_size, gpu_count, gpu_type, logic_compute_group_ids, allowed_priority_levels}`。`id` 就是 v1 价格行里的 `quota_id`，实测全部 10 个工作空间 × 4 类 Workload **零 miss**，所以它是把优先级限制 join 到配额目录上的键。`allowed_priority_levels` 取 `null` / `[]`（不限）或 `["low"]`（只能低优先级）；全平台 159 条规格里只有 9 条受限，全是训练区的碎卡档。`logic_compute_group_ids` 为空表示对所有组开放。HPC 的 `predef_node_spec` 不在这份记录里。
- **`notebook.GetScheduleConfig` 是 Workspace 调度策略的全集**，`GetNotebookScheduleConfig`、`ray.GetRayJobScheduleConfig`、`train.GetTrainScheduleConfig` 都是它的严格子集（10 个 Workspace 上逐字段同值、同 `config_id`），所以只接这一个。它与**管理员专用**的 `workspace.GetScheduleConfig` 只是重名，不是同一个东西。
- `notebook create` 的 `allow_ssh: true` 是硬编码的：平台据此在 proxy URL 上暴露容器内的 rtunnel 端口，缺了它 proxy 返回 404，Notebook SSH 的预检就完不成。该字段省略时默认 false，与镜像里有没有 SSH 工具无关。

---

## `inference_serving` — 模型部署

Referer：`/jobs/modelDeployment`。路由名是**下划线**形式，discovery 里的 `inference-serving` 会 404。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateServingConsole` † | 见下方创建字段表 | `{inference_serving_id, sub_code, sub_msg}` | `serving create`、`serving batch` |
| `ListServings` | `{workspace_id, page, page_size, filter_by:{my_serving:true, keyword?, project_id[]?, status[]?, inference_serving_type[]?}}` | `{inference_servings[], total}` | `serving list`、Name Resolver、`cache refresh` |
| `GetServing` | `{inference_serving_id}` | 完整部署对象：`status` / `replicas` / `node_num_per_replica` / `model_id` / `model_version` / `mirror_id` / `resource_spec_price{}` / `extra_info{node_names[]}` | `serving status`、`serving metrics` |
| `ListServingVersions` | `{inference_serving_id}` | `{inference_servings[]\|list[], total}` | `serving versions` |
| `ListServingInstances` | `{inference_serving_id, page, page_size}` | `{groups[{items[]}], total}` | `serving instances`、`serving logs`、`serving events --instance` |
| `ListServingEvents` | `{page, page_size, filter:{object_type:"INFERENCE_SERVING"\|"INFERENCE_SERVING_INSTANCE", object_ids:[id\|pod…]}}` | `{events[]\|items[]\|list[]}` | `serving events`、`serving events --instance` |
| `ListServingScaleHistory` | `{inference_serving_id, page, page_size}` | `{scale_history_items[], total}`（`total` 是字符串） | `serving scale-history` |
| `GetServingLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}` | `serving logs` |
| `GetServingScheduleConfig` | `{workspace_id}` | `{enable_auto_stop, items[{auto_stop_ruleset, gpu_count_min, gpu_count_max}]}` | `resources policy` |
| `GetServingApiMetric` | `{inference_serving_id, metric_types[], time_range:{start_timestamp, end_timestamp, interval_second}}` | `{metric_groups[]}` | `serving api-metrics` |
| `GetInferenceServingTerms` | `{inference_serving_id}` | `{terms[]}` | — |
| `GetServingConfigByWorkspaceId` | `{workspace_id}` | `{configs{}}` | `serving configs` |
| `GetInferenceServingUserProjectList` | `{workspace_id}` | `{projects[], users[]}` | `serving create` 的项目/用户选择 |
| `StartServing` | `{inference_serving_id}` | `{inference_serving_id, sub_code, sub_msg}` | `serving start` |
| `StopServing` | `{inference_serving_id}` | `{inference_serving_id}` | `serving stop` |
| `ScaleServing` | `{inference_serving_id, replica}` | — | `serving scale` |
| `RollbackServing` | `{inference_serving_id, version}` | `{inference_serving_id, sub_code, sub_msg}` | `serving rollback` |
| `DeleteServing` | `{inference_serving_id}` | `{inference_serving_id}` | `serving delete` |
| `GetTaskMetric` | 见 [Metrics](#metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `serving metrics` |

**参数语义与限制**

- **创建必须用 `CreateServingConsole`，不是 discovery 里那个 `CreateServing`。** 后者的 Description 明写「via OpenAPI with simplified config」，契约确实不同：要 `spec_id` 而不是 `resource_spec_price`，`image` 是普通字符串而不是 `mirror_id`，且不收 `description` / `inference_serving_type` / `model_source`。**看到写操作的字段被大面积拒绝时，第一反应应该是「是不是找错 Action 了」，而不是「契约变了」。**
- **`ScaleServing` 的字段是单数 `replica`**，而 create 和 `UpdateServing` 用复数 `replicas`。
- **`StartServing` / `StopServing` 只收 `{inference_serving_id}`**，请求体里的 `version` 会被拒。这两条曾经迁了 URL 却仍用 v1 的 `code != 0` 检查解包，于是对任何输入都返回 `API error: None`——**迁 URL 而不同时换解包器，会把真错误伪装成假错误。**
- **`UpdateServing` 的语义已受控验证，结论是不封装。** 四条约束叠在一起让它对 Agent 不安全：
  1. **只能在 `FAILED` 或 `STOPPED` 状态下调用**，运行中答 `InvalidParameter: 参数错误: This serving can only be updated in FAILED or STOPPED status.`
  2. **`resource_spec_price` 必须是扁平结构**（`cpu_type` / `cpu_count` / `gpu_count` / `memory_size_gib` / `quota_id` / `logic_compute_group_id`），而 `GetServing` 读回的是**嵌套**结构（带 `cpu_info` / `gpu_info` 和一组价格字段）。**读回来的对象不能直接喂回去**——原样发送答 `unknown field "cpu_info"`。
  3. **它是全量替换，省略的字段会被清空。** 受控验证：发一份完整 body 成功后再发一份只去掉 `command` 的，`command` 立刻变成空串。
  4. **每次成功都会 bump `version`**（1 → 2），下一次必须带新的 `version`。
  安全的 Wrapper 因此要把**全部**字段搬运一遍，而 `port_configs` / `runtime_attributes` / `traffic_config` / `custom_mounts` 这些 CLI 根本没有建模的字段一旦漏掉就会被静默清空。改配置重建一个 serving 既便宜又安全，所以不接。单字段的 `ScaleServing` 与 `RollbackServing` 不受这一条影响。
- **`DeleteServing` 要求先停止**，运行中删除答 `Conflict: 当前状态（运行中）无法删除，请先停止后再删除`——与 `train` / `hpc` 一致。任何清理路径都必须走 `StopServing` → 轮询到 `STOPPED` → `DeleteServing`。
- **Serving 不一定要 GPU。** `CPU资源空间` 的 `CPU资源-2` 组提供 CPU-only 档位（最小 `0,4,20`），所以 serving 的写面可以零 GPU 成本受控验证。`GetServingConfigByWorkspaceId` 报的 `gpu=1-119` 是自动停机规则的档位范围，不是创建下限。
- **`GetServingApiMetric` 与 `GetTaskMetric` 是两个不相干的指标族**，共享零个指标名。前者是请求流量：`QPS`、`SUCCESS_QPS`、`FAIL_QPS`、`SUCCESS_RATE`、`FAIL_RATE`、`REQUEST_COUNT`、`LATENCY`、`TTFT`(+`_P50`/`_P95`/`_P99`)、`TTLT`(+`_P50`/`_P95`/`_P99`)、`INPUT_TOKENS`、`OUTPUT_TOKENS`。它**接受整个 `metric_types` 列表**（`GetTaskMetric` 不接受），也不需要 compute-group 句柄。返回项带 `metric_type` / `group_name` / `data_unit` / `time_series[{timestamp, data}]`。
- **`ListServingScaleHistory` 的列表键是 `scale_history_items`**，不是 `items` 也不是 `list`。曾经按 `items` 读，于是任何有扩缩容历史的 serving 都返回空列表——这是一个「读错键就永远看不到数据」的静默失败，而不是报错。
- **`GetInferenceServingTerms` 不是调用说明。** 它的 `terms` 元素是 `{term, start_time, end_time}`，即**运行期次索引**（第 N 次运行的起止时间，控制台用它把详情页各 tab 圈定到某一次运行），里面没有 endpoint、示例请求或 token。调用信息是 `GetServing` 的 `port` 和 `command`。**查过，刻意不接**，Wrapper 保留但没有命令消费。
- **`GetServingLog` 对不在日志库里的 pod 名回 `InternalError`**，看着像平台故障，实际含义是「pod 名不对」。
- **`ListServingInstances` 的行是嵌套的**：`{groups: [{items: [...]}], total}`，一个副本一个 group，Pod 在里面——不是兄弟 Action 那种扁平 `items`。按顶层读会拿到空列表配非零 `total`，而那正好是「这个部署还没有 Pod」的形状，所以失败静默且永久。行是 pod 形状：`name`（**带命名空间**，`<project>/<pod>`）、`component_type`（`LEADER` / `WORKER`）、`status`、`node`、`ready`、`restarts`、`term`、`created_at` / `started_at` / `finished_at`、`running_time_ms`。
- **`ListServingEvents` 一个 Action 管两级**，靠 `filter.object_type` 区分：`INFERENCE_SERVING` 给部署级（`CreatingRevision` / `GroupsProgressing` / `Pending`，来自控制器），`INFERENCE_SERVING_INSTANCE` 给 Pod 级（`Scheduled` / `Pulled` / `Created` / `Started` / `Unhealthy`），两者不相交。**Pod 级的 `object_ids` 必须是带命名空间的实例名**，裸 pod 名答 `InternalError`——和 HPC 那两个端点同病。枚举第三个值 `INFERENCE_SERVERLESS` 未验证。
- **节点落点在 `GetServing` 的 `extra_info.node_names[]`，不在顶层**：顶层只有 `node_num_per_replica`（每副本几个节点，是规格）。按顶层读会让每个部署都显示成没落点。pod 级的同一事实在 `ListServingInstances` 行的 `node`，已在活部署上复核。
- `GetServingScheduleConfig` 的回收规则是**按 GPU 档位**给的（每个 `items` 元素带 `gpu_count_min` / `gpu_count_max`），一个 Workspace 会有多条。
- `DeleteServing` 的 id 不存在时返回 `ResourceNotFound`。
- 读 Action 逐字接受 v1 请求体、响应字段完全一致；**写侧不能照搬**。

---

## `workspace` — 工作空间资源

Referer：`/jobs/distributedTraining`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ListLogicComputeGroups` | `{page_size: -1, page_num: 1, filter:{workspace_id}}` | `{logic_compute_groups[], total}` | `resources availability`、`<workload> quota`、每个 `create` 的组解析、`init`、`cache refresh` |
| `ListNodeDimension` | `{filter:{workspace_id, logic_compute_group_id}, PageNumber, page_size}` | `{node_dimensions[], total}` | `resources availability`、`resources nodes` |
| `ListTaskDimension` | `{filter:{workspace_id, logic_compute_group_id?}, PageNumber, page_size}` | `{task_dimensions[], total}` | `resources usage --by task\|project` |
| `ListUserDimension` | `{filter:{workspace_id}, PageNumber, page_size}` | `{user_dimensions[], total}` | `resources usage --mine` |
| `GetLogicComputeGroupNodeSpecs` | `{workspace_id, logic_compute_group_id}` | `{node_specs[]}` | `resources nodes` |
| `GetWorkspaceNodeSpecs` | `{workspace_id}` | `{node_specs[]}` | `resources nodes` |
| `GetLogicComputeGroupResource` | `{workspace_id, logic_compute_group_id}` | `{logic_resouces{}, gpu_type_stats[], runtime_attributes[]}` | `resources availability` |
| `GetWorkspaceQuota` | `{workspace_id}` | `{gpu_high_running, gpu_high_running_used, cpu_*, memory_*, is_fair_workspace, …}` | — |
| `GetWorkspaceComputeResource` | `{workspace_id}` | `{logic_resouces{cpu_total, cpu_used, memory_gi_total, memory_gi_used, gpu_total, gpu_used, gpu_low_priority_used}}` | — |

另有一个不在本路由上的邻居：**`cluster.ListNodeEvents`**（Referer `/cluster/nodeList`），`{PageNumber, page_size, filter:{node_names[], from?}, sorter:[{field:"last_timestamp", sort}]}` → `{events[], total}`，供 `resources node-events`。

**参数语义与限制**

- **一律用 `workspace.*`，不用 `cluster.*`**：同名的 8 个 Action 在 `cluster.*` 下对非集群管理员一律 `AccessForbidden`。**`cluster.ListNodeEvents` 是例外**，它没有 `workspace.*` 对应物，且对普通成员可读——这也是平台上唯一按节点而不是按工作负载组织的事件源（内核 OOM kill、`TaskHung`、Cordon / Uncordon、`Rebooted`、`NodeNotSchedulable`）。
- **`ListNodeEvents` 的 `filter.node_names` 事实上必填**：不给 filter 答 `{events: [], total: 0}`，与「这个集群很安静」不可区分；节点名不认识同样是空列表而不是报错。一次可以给多个节点，行里带 `node_name` 自己署名。行的类型字段叫 **`event_type`**（不是别处的 `type`），也**没有 `count`**。`filter.from` 按上报组件收窄有效；`event_type` / `type` / `keyword` 全是 `unknown field`；discovery 声明的 `start_last_timestamp` / `end_last_timestamp` 答 `InternalError`，时间窗只能在客户端做。
- **`ListLogicComputeGroups` 省略 `page_size` 会返回空列表配非零 `total`**，看起来就像这个工作空间没有任何计算组。`page_size: -1` 有效，保持原样。
- **`ListNodeDimension` 的 `page_size: -1` 只返回 10 条**，必须按 `total` 显式翻页。它的两级 scoping 也最容易踩：`filter` 里只放 `logic_compute_group_id` 返回 `AccessForbidden`，同时放 `workspace_id` 和 `logic_compute_group_id` 才通。
- **节点行的 GPU 数嵌在 `gpu.total` 里**，不是扁平 `gpu_count`；只读扁平键会让每个节点看起来都是零卡，静默把空闲节点数清零。行里还有 `status`（`READY` 判定）、`tasks_associated` / `task_list`（有没有任务）、`cordon_type`、`is_maint`、`resource_pool`（`fault` 要排除），「完全空闲」需要五项同时成立。
- **维度族（`ListNodeDimension` / `ListTaskDimension` / `ListUserDimension`）的 scoping 只认嵌套 `filter.workspace_id`**：顶层 `workspace_id` 被直接拒为 `unknown field`，缺 workspace 则 `AccessForbidden`。`filter.logic_compute_group_id` 可选且真的收窄；`filter.task_type` 被静默忽略，`task_name_keyword` / `gpu_type` 有效。
- **维度族的 `page_size: -1` 和省略 `page_size` 都只回 10 行**（实测 `total=1289` 时仍只给 10），必须按 `total` 显式翻页。三种分页拼法都认，`total` 是 int。
- **维度族的 `order_by` 元素是 `{field, sort}` 而不是 `{field, order}`**，只有 `created_at` 被采纳且 `sort` 被忽略（恒升序），`{"field":"gpu"}` / `{"field":"cpu"}` 直接 `InternalError`——**排序只能在客户端做**。
- 维度行只含存活工作负载（`RUNNING` 加短暂的 `COMMITTING`），覆盖所有用户与所有 Workload 类型。**`gpu.used` 是死字段（恒 0）**，但 `gpu.usage_rate` / `cpu.usage_rate` 是活的 0–1 比率。
- **`ListProjectDimension` 实测是空的**，不是 scoping 写错：10 个可见 Workspace、逐计算组、逐项目 id 都返回成功信封配 `total: 0`，而它的两个同族兄弟在**同一个** `filter.workspace_id` 位置上答出真实数据。所以它是权限地板，没有封装；按项目聚合改为在客户端折叠任务维度的行。
- **`GetLogicComputeGroupNodeSpecs` / `GetWorkspaceNodeSpecs` 的 scoping 反而在顶层**，套 `filter` 是 `unknown field`；只给组不给 workspace 是 `AccessForbidden`。
- **`node_specs` 是规格目录，不是节点清单**：一个 292 节点的组只发布 17 种形状，行是「形状 × 作业类型」的笛卡尔积，还会因为 GiB 小数差异重复（68 行原始数据里只有 6 种真实形状）。**任何按行数当节点数的读法都是错的**。字段里 `gpu_type` 恒为空、`gpu_memory_size` 恒为 0（真值在 `gpu_info` 里），discovery 声明的 `node_count` 线上根本不存在。
- **组资源汇总的键平台拼错成 `logic_resouces`**（少一个 `r`），`GetLogicComputeGroupResource` 与 `GetWorkspaceComputeResource` 同病。GPU 型号在 `gpu_type_stats[0].gpu_info.gpu_type_display`。
- **`ListLogicComputeGroups` 的标识字段叫 `logic_compute_group_id` 而不是 `id`**；`support_job_type_list` 是 **JSON 编码的字符串**，不是数组（`'["interactive_modeling","hpc_job"]'`）。用 `isinstance(x, list)` 判断会把每个组都读成「没声明」，于是按 Workload 过滤计算组这件事看起来生效了、实际一个都没滤掉。取值域：`interactive_modeling` / `hpc_job` / `ray_job` / `distributed_training` / `inference_serving_customize` / `inference_serving_exclusive`，逐组不同。
- **`GetWorkspaceQuota` / `GetWorkspaceComputeResource` 要顶层 `workspace_id`**，套 `filter` 反而被拒。配额字段是 `{资源}_{high|low}_{running|total}` 加可选的 `_used` 后缀：高优先级（保障）和低优先级（可回收）是**两套独立的上限**，一个运行中的任务只吃其中一套，混着读会两边都报错。`-1` 表示不限。
- 两者回答不同的问题：**配额用完了可以被拒，即使机器闲着；机器忙满了也可以被拒，即使配额还有。**
- `GetScheduleConfig` / `ListUserQuotas` / `GetUserTaskQuota` / `GetWorkspaceTaskQuota` / `GetDefaultUserTaskQuota` 需要工作空间管理员，`GetDefaultUserQuota` 普通成员能读但没有信息量——都没有封装。**`workspace.GetScheduleConfig` 不是各 Workload 路由下 `Get*ScheduleConfig` 的汇总入口**：它对普通成员一律 `AccessForbidden`（顶层 `workspace_id` 与 PascalCase 都试过，id 被回显说明 scoping 是通的），而 Workload 路由下的同族 Action 普通成员可读。

---

## `user` — 账号

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `GetUserDetail` | `{}` | `{id, name, name_en, email, avatar_url, global_role, created_at, extra_info{}}` | 所有「按当前用户过滤」的列表：`job/hpc/ray/notebook/serving/model list`、`config check` |
| `GetPermissions` † | `{WorkspaceId}` | `{permissions[]}`（如 `"job.trainingJob.create"`） | `account permissions` |
| `GetRoutes` † | `{WorkspaceId}` | `{routes[{name, routes[{path, name, is_fair_workspace}]}]}` | Workspace 枚举、优先级选择、`init`、`cache refresh` |

**参数语义与限制**

- **`GetUserDetail` 只覆盖当前用户**：传空体返回当前账号，传 `user_id` / `id` / `UserId` 一律 `InvalidParameter`。
- **两个未文档化 Action 的 workspace 参数 Wrapper 写作 PascalCase `WorkspaceId`**（照 discovery 的声明），`workspace_id` 同样有效——网关对大小写和下划线不敏感。
- **`GetRoutes` 的 `userWorkspaceList` 那个 route group 是 Workspace 枚举的唯一来源**：每个条目的 `path` 是 `ws-…` id，`name` 是显示名，`is_fair_workspace` 是 qz 优先级选择器的唯一数据源，缺了就没法判断该工作空间用哪套优先级。
- **它替代不了登录时的 `/api/v1/user/routes/default`**：v2 要一个真实的 `WorkspaceId`，而登录握手时一个都还不知道。见 [`browser-api.md` 第 8 节](browser-api.md#8-仍在使用的-v1-端点)。
- `ListAPIKeys` 可用但随 `user api-keys` 命令一起下线，已无消费者。`user.ListSSH` / `user.GetMyPermissions` 存在但未封装——账号级 SSH 公钥注册表与 Notebook SSH 链路无关。

---

## `project` — 项目

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ListProjects` † | `{page, page_size, filter:{workspace_id?, check_admin?}}` | `{items[], total}` | `project list`、每个 `create` 的项目解析、`init`、`cache refresh` |
| `GetProjectDetail` † | `{ProjectId}` | `{budget, children_budget, remain_budget, used_budget, created_at, en_name, description, priority, owner…}` | `project detail` |
| `GetProjectOwners` † | `{}` | `{items[{id, name, login_name, …}]}` | `project owners` |
| `GetProjectForPage` | `{page, page_size, filter{}}` | `{items[], total}` | — |

**参数语义与限制**

- **`ListProjects` 是选择器的行集，`GetProjectForPage` 是项目管理页的行集，两者条数不同是设计如此**：后者会滤掉用户已退出或已结束的项目。不要把这条差异读成「谁坏了」。
- **`GetProjectDetail` 的 id 键 Wrapper 写作 PascalCase `ProjectId`**，`project_id` 同样有效。它的 `remain_budget` / `used_budget` / `resource` 是**本来就在跳的值**，连着读两次不相等是正常的。
- CLI 侧翻页固定 `page_size = 100`，直到 `len(items) >= total` 或短页为止；选择器路径用 `page_size: -1` 一次取全。
- 项目行里对调度有意义的是 `gpu_limit`（是否有项目级 GPU-hour 上限）和 `priority_name`（数字字符串），`space_list[]` 给出项目跨哪些 Workspace。

---

## `image` — 镜像

Referer：`/jobs/interactiveModeling`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ListImages` | `{page: 0, page_size: -1, filter:{…见下…}}` | `{images[], total}` | `image list`、`notebook/job/hpc/ray/serving create` 的镜像解析、`cache refresh` |
| `GetImageById` | `{ImageId}` | 单个镜像对象：`image_id` / `name` / `address` / `framework` / `version` / `source` / `visibility` / `status` / `description` / `created_at` | `image detail`、`notebook save-image` 的就绪轮询 |
| `CreateImage` † | `{name, version, registry_hint:{workspace_id}, visibility, add_method, description}` | `{image{image_id, …}}` | `image register` |
| `UpdateImage` † | `{id, visibility?, description?}` | — | `image set-visibility` |
| `DeleteImage` | `{image_id}` | — | `image delete` |

**参数语义与限制**

- **`UpdateImage` 的目标键是裸 `id`，不是 `image_id`。** 这不是大小写问题——网关对大小写和下划线不敏感（`GetImageById` 给 `ImageId` / `image_id` / `Image_Id` 都通），但 `image_id` 是**另一个字段名**，不会被归一化成 `id`。传错不报 `unknown field`：`UpdateImage` 收到 `image_id` 时**默默忽略**，然后拿空 id 去查库，回一句 `InternalError: 数据库错误, 请联系管理员`。**看到这个错先检查字段名。**
- **`ListImages` 的三种 UI 来源用三种 filter**，不是一个简单的 `source` 字段：
  - 官方镜像：`{source: "SOURCE_OFFICIAL", source_list: [], registry_hint:{workspace_id}}`
  - 公开镜像：`{source_list: ["SOURCE_PRIVATE","SOURCE_PUBLIC"], visibility: "VISIBILITY_PUBLIC", registry_hint:{…}}`
  - 个人可见：同上但 `visibility: "VISIBILITY_PRIVATE"`
- **镜像地址在 `address` 字段**，不是 `url`。创建 Workload 时平台匹配的是**注册表 URL 而不是可见名**，发名字会被拒为 `无法找到对应镜像`。
- **`add_method`**：`0` = 本地推送（`docker push`，v1 和 v2 给出同一个 `no image uploaded` 拒绝），`2` = 注册已有镜像地址。
- **就绪状态有两套**：`image register` 产出的镜像走 `READY`，`notebook save-image` 产出的走 `SUCCESS`。终态失败包括 `FAILED` / `FAILURE` / `ERROR` / `CANCELLED` / `TIMEOUT` / `ABORTED` / `INTERRUPTED`——漏掉任何一个都会让轮询挂到超时而不是快速失败。
- 「把 Notebook 存成镜像」不在这条路由，在 `notebook.SaveNotebookImage`——CLI 侧对应 `notebook save-image`，`image` 组只管已经存在的镜像。

---

## `model-hub` — 模型仓库

Referer：`/jobs/modelService?spaceId={workspace_id}`。路由名是**连字符**形式，下划线会 404。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ListModels` | `{workspace_id, page, page_size, filter_by:{user_id, keyword?, project_id[]?, model_type[]?}}` | `{list[], total}` | `model list`、`serving create` 的模型解析、`cache refresh` |
| `GetModelDetail` | `{model_id}` | `{model{}, user_name, project_name, user_avatar}` | `model status` |
| `ListModelVersions` | `{model_id}` | `{list[], total, next_version}` | `model versions`、`model status` |
| `ListModelVersionOptions` | `{model_id}` | `{list[], total}` | — |
| `ListModelCreators` | `{project_id}` | `{list[]\|items[], total}` | — |
| `ListModelRelatedServings` | `{model_id, version, page, page_size}` | `{serving[], total}` | `model status` |
| `GetHasModelPendingServing` | `{model_id, version?}` | `{has_pending_serving}` | `model status` |
| `GetModelPublishPrefill` | `{model_id, version}` | `{model_info{}, technical_specs{}, integration_info{}}` | — |
| `GetModelPublishStatus` | `{model_id, version}` | `{status, publish_reject_detail, has_published}` | — |
| `GetRecommendedConfig` | `{model_id, version}` | `{min_node_count, min_gpu_count_per_node, min_cpu_count_per_node, min_memory_size_gib_per_node}` | `model deploy-config` |
| `CheckModelVLLMCompatible` | `{model_id, version, inference_serving_type}` | `{is_vllm_compatible}` | `model deploy-config` |
| `GetModelVLLMCompatibleData` | `{model_id, inference_serving_type?}` | `{data:[{version, is_vllm_compatible}]}` | `model status`、`model versions` |
| `CreateModel` † | `{name, project_id, workspace_id, model_source_path, model_source_type, model_type[], tags[], description}` | `{model_id}` | `model register` |
| `DeleteModel` † | `{model_id}` | — | `model delete` |

**参数语义与限制**

- **`ListModelVersions` 与 `ListModelVersionOptions` 名字近似、极易接反**，只能靠响应字段区分：前者多一个 `next_version`，是详情抽屉用的**富**视图（含模型路径、源路径、大小、发布状态、运行中的 serving 数）；后者是部署表单用的**精简**版本列表。
- **`ListModelVersions` 不接受 `page`。** `ListModelCreators` 接受 `project_id`。
- **`GetRecommendedConfig` 给的是下限而不是推荐值**：四个 `min_*` 映射到 `serving create --quota gpu,cpu,mem` 和 `--nodes-per-replica`，照抄不等于最优。
- **版本记录里的 `is_vllm_compatible` 是死字段。** 29 个可见模型版本上它无一为 true，而 `GetModelVLLMCompatibleData` 与 `CheckModelVLLMCompatible` 两个 live Action 一致地给出 13 个 true。任何读存量字段的地方都会永远报「不兼容」，**vLLM 兼容性只能问 live**：`GetModelVLLMCompatibleData` 一次回答某模型所有版本，`CheckModelVLLMCompatible` 按版本问。
- **`ListModelVersions` 的 list 元素是嵌套的** `{model: {...}, running_infrence_serving}`（平台把 inference 拼错了），版本号与规格都在内层。
- **`ListModelRelatedServings` 的 `page` 与 `page_size` 都必填**（`page_size: -1` 被拒），`version` 也实质必填——省略时 proto 默认 0，返回空列表而不是「所有版本」。条目 `{name, serving_id, status, version, user_name, user_avatar}` 里有可读服务名，但**条目的 `version` 是推理服务自己的版本号，不是模型版本**，印出来会被误读；`status` 是 int，按 serving 状态枚举下标（`4` = RUNNING，用「该版本 status=4 的条数 == `running_infrence_serving`」在 11 个版本上钉死）。
- **`GetHasModelPendingServing` 的 `version` 可选**，省略即问整个模型；它只在有 PENDING 部署时为 true（DEPLOYING、RUNNING 都是 false），正好补上 `running_infrence_serving` 计数为 0 却已有部署排队的盲区。
- **`CreateModel` 的 `model_source_path` 必须落在所给 workspace + project 的路径下**，`global_user` 路径会被 `存储路径格式不正确` 拒掉。`model_source_type = 1` 对应 UI 的「路径注册」流程，首个版本号由后端推断。
- **`filter_by.project_id` 必须是数组**，传裸字符串会被 protobuf 解码拒绝。
- 列表项是 `{model: {...}, project_name, user_name, latest_version}` 的嵌套形状，扁平化时 `model_id` 优先于内层 `id`。
- `DeleteModel` / `UpdateModel` 存在（受控验证时用过 `DeleteModel` 收尾），当前未封装。

---

## `file` — 文件页 ‡

Referer：`/jobs/files?spaceId={workspace_id}`。**整条路由不在 discovery 里**（历史上出现过又被删掉），但活着，两个 Action 逐字接受 v1 请求体。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `GetSystemStorageTypeList` | `{filter:{workspace_id}}` | `{system_storages[{name, cluster_id}]}` | `init --scope project` 的存储池发现 |
| `GetDirList` | `{filter:{workspace_id, system_storage_type, name, cluster_id?}}` | `{files[{directory}]}` | 同上，逐存储池列项目目录 |

**参数语义与限制**

- **`filter.name` 是前端的类别键**，不是文件名：`project` / `global_public` / `global_user`。
- **`system_storage_type` 取 `GetSystemStorageTypeList` 返回的存储池名**；`share-` 前缀的存储池在项目目录发现里被跳过。
- **与 v1 唯一的差异是行顺序**：12 个存储池和目录列表都会换序，排序后完全相等。当前调用方都不依赖顺序，**新调用方也不要依赖**。

---

## `dataset` — 官方数据集挂载 ‡

Referer：`/jobs/interactiveModeling?spaceId={workspace_id}`。**整条路由不在 discovery 里**，穷举四十个候选名后只有这一个 Action。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ValidateDataset` | `{datasets:[{dataset_id, version_id}], workspace_id}` | `{datasets_result:[{dataset_id, version_id, success, path, error_message}]}` | `dataset validate`、`notebook/job/hpc create --dataset` |

**参数语义与限制**

- **`dataset_id` 是数据集的 code，`version_id` 是版本的 code，都不是数字主键。** `pixabay-81k` + `v0` 解析成功；同一个数据集的数字 id 回 `数据集不存在`。数字主键只存在于数据广场内部，见 [`data-plaza-api.md`](data-plaza-api.md)。
- **错误码三种**：`2000 数据集不存在`、`2001 版本不存在`、`2005 无访问权限`。
- **返回的 `path` 是平台内部存储路径**（如 `sftpgo/pixabay-81k/v0`），不是容器内挂载点。它要原样填进 `create` 请求的 `dataset_info[].path`——所以**创建前必须先走一次 `ValidateDataset`**，这正是控制台「校验数据」按钮做的事。
- **容器内挂载点固定是 `/inspire/dataset/<数据集 code>/<版本 code>`，只读**。CLI 只把两个 code 和这个容器路径投影出去，平台存储路径不进公共输出。
- 整批 mount 走一次请求；平台回的顺序不保证与请求一致，**按 `(dataset, version)` 键回请求，不要 zip**。
- **检索不在这条路由上，也不在这个平台上。**

---

## Metrics — `GetTaskMetric`

v1 用一个集群级端点服务所有 Workload；v2 **没有集群级端点**，每个 service 各有一份 `GetTaskMetric`，接受逐字相同的请求体。

```jsonc
POST /api/v2/{notebook|train|hpc|ray|inference_serving}?Action=GetTaskMetric
{
  "filter": {"logic_compute_group_id": "lcg-…", "task_id": "…", "task_type": "…"},
  "metric_types": ["gpu_usage_rate"],
  "time_range": {"start_timestamp": 1700000000, "end_timestamp": 1700003600, "interval_second": 60}
}
→ Result: {"time_seris_metric_groups": [{group_name, metric_type, resource_name, time_series:[{timestamp, data}]}]}
```

| CLI 资源名 | `task_type` | 路由 |
| --- | --- | --- |
| `notebook` | `interactive_modeling` | `notebook` |
| `job` | `distributed_training` | `train` |
| `hpc` | `hpc_job` | `hpc` |
| `ray` | `ray_job` | `ray` |
| `serving` | `inference_serving` | `inference_serving` |

**参数语义与限制**

- **一次只能问一个 metric。** 一个请求里放 5 个 `metric_types` 只会返回第一个的数据，所以 Wrapper 按 metric 扇出再合并；任何一个失败整次调用抛错。
- **响应键是平台拼错的 `time_seris_metric_groups`**（少一个 `e`）。Wrapper 同时接受拼对的写法，以防平台哪天修正。
- **8 个 metric_type**：`gpu_usage_rate`、`gpu_memory_usage_rate`、`cpu_usage_rate`、`memory_usage_rate`、`disk_io_read`、`disk_io_write`、`network_tcp_ip_io_read`、`network_tcp_ip_io_write`。`*_usage_rate` 是 0–1 的比率，I/O 是字节/秒。
- **间隔选项**：`1m` = 60、`5m` = 300、`30m` = 1800、`1h` = 3600 秒。
- **传不支持的 `task_type` 会拿到一个 Prometheus 422**（抱怨空 label name），所以 Wrapper 在发出前就校验。
- **`workspace.GetOverviewResourceMetricByTime` 不是它的对应物**：那是工作空间级总览，对普通成员返回 `AccessForbidden`。[`browser-api.md` 第 5 节](browser-api.md#cluster-与-workspace)的「用 `workspace.*` 不用 `cluster.*`」只适用于两边同名的那 8 个 Action，**不能推广成「集群级端点一律换 `workspace.*`」**。
- 多 pod 实例（分布式训练、多副本 serving）每个 pod 一个 group；单实例 Notebook 恰好一个。

---

## 创建面的字段合同

五个创建 Action 接受的字段互不相同，且只有 `notebook.CreateNotebook`、`train.CreateJob` 和 `ray.CreateJob` 在 discovery 里声明过。下表用[存在性探针](browser-api.md#7-探针方法)逐字段量出来，**是判断某个网页选项能不能进 CLI 的唯一依据**。

| 字段 | notebook | train Console | hpc Console | ray | serving Console |
| --- | --- | --- | --- | --- | --- |
| `dataset_info` | 接受 | 接受 | 接受 | **拒绝** | **拒绝** |
| `envs` | **拒绝** | 接受 | **拒绝** | **拒绝** | **拒绝** |
| `description` | **拒绝** | 接受 | 接受 | 接受 | 接受 |
| `mounts` / `mount_path` | 接受 | 接受 | **拒绝** | **拒绝** | 接受（`custom_mounts`） |
| `is_publicpath_readonly` | 接受 | 接受 | 接受 | 接受 | 接受 |
| `is_projectuserspath_readonly` | 接受 | **拒绝** | **拒绝** | — | — |
| `queue_id` | 接受 | 接受 | 接受 | **拒绝** | **拒绝** |
| `runtime_attributes` | 接受 | 接受 | 接受 | 接受 | 接受 |

### 各创建 Action 的实发请求体

**`notebook.CreateNotebook`** — 必填：`{workspace_id, name, project_id, project_name, auto_stop, allow_ssh, mirror_id, mirror_url, logic_compute_group_id, quota_id, cpu_count, gpu_count, memory_size, shared_memory_size}`。可选（不传就完全不出现在 body 里）：`resource_spec_price`（GPU Notebook 必需）、`task_priority`、`node_id`、`dataset_info[]`、`enable_notification`、`stop_hour` + `stop_minute`、`is_publicpath_readonly`、`is_projectuserspath_readonly`。

**`train.CreateJobConsole`** — 必填：`{name, command, framework, project_id, workspace_id, logic_compute_group_id, task_priority, enable_notification, framework_config:[{image_type, image, instance_count, resource_spec_price, cpu, gpu_count, mem_gi, shm_gi?}]}`。可选：`max_running_time_ms`、`exclude_nodes[]`、`auto_fault_tolerance` + `fault_tolerance_max_retry` + `fault_tolerance_retry_interval_sec`、`dataset_info[]`、`envs[]`、`description`、`reserve_on_success_ms`、`reserve_on_fail_ms`、`is_publicpath_readonly`。

**`hpc.CreateJobConsole`** — 必填：`{job_name, logic_compute_group_id, project_id, workspace_id, enable_notification, priority, sbatch_script:{number_of_tasks, cpus_per_task, memory_per_cpu, enable_hyper_threading, entrypoint}, slurm_cluster_spec:{predef_quota_id, cpu, mem_gi, image, image_type, instance_count, spec_price}}`。可选：`dataset_info[]`、`description`、`ttl_after_job_finish_seconds`、`is_publicpath_readonly`，以及 `sbatch_script` 里的运行时长四件套。

**`ray.CreateJob`** — `{name, description, workspace_id, project_id, entrypoint, task_priority, head_node:{mirror_id, image_type, logic_compute_group_id, quota_id, shm_gi?}, worker_groups:[{group_name, mirror_id, image_type, logic_compute_group_id, min_replicas, max_replicas, quota_id, shm_gi?}]}`，可选 `is_publicpath_readonly`。

**`inference_serving.CreateServingConsole`** — 必填：`{workspace_id, project_id, inference_serving_type, name, logic_compute_group_id, model_id, model_version, mirror_id, command, port, description, replicas, node_num_per_replica, task_priority, resource_spec_price}`。可选：`custom_domain`、`shm_gi`、`model_source`、`is_publicpath_readonly`、`enable_auto_scaling`。**`queue_id` / `dataset_info` / `envs` / `scale_status` 被拒且没有对应物。**

### 几处不能靠直觉推的细节

- **`envs` 的元素是 `{name, value}`，不是 `{key, value}`**，`key` 会被拒为 `unknown field "key"`。**`train.GetJob` 的读投影不回显 `envs`**（容器里明明有变量，读回来是 `[]`），所以不能用读接口核对写了什么。
- **`is_projectuserspath_readonly` 只有 notebook 有**，而且要项目 Maintainer：普通成员传了会拿到 `AccessForbidden: only project maintainer can enable project users path readonly mount`。
- **HPC 的最大运行时长不在顶层**。`max_running_time_ms` 和 `max_running_time_minutes` 打顶层都是 `unknown field`；它嵌在 `sbatch_script` 里，而且控制台**同时发两份**：`job_max_time`（`"D-HH:MM:SS"`，即 Slurm `--time`）和 `max_running_time_days` / `_hours` / `_minutes`。`slurm_cluster_spec` 一个都不收。
- **HPC 的优先级键是 `priority`，不是 `task_priority`，而且它是必填的。** 拼错时 v2 答 `priority must be set`，读起来像值缺失而不是名字写错；**整个键不发**时答的却是 `InternalError: internal server error`——它在 transient 名单上，于是先白烧三次重试，再抛出一条看起来像平台故障的错。CLI 侧宁可自己先报「拿不到优先级」，也不要把这个键漏掉。
- **`sbatch_script.memory_per_node` 是死字段，而且比 `working_dir` 更能骗人：它完整 round-trip。** 控制台有「每节点使用内存」这个输入框，执行命令模板里写的甚至就是 `#SBATCH --mem=*G`，平台也确实收下、存住、在详情页显示「每节点使用内存：8G」、`GetJob` 原样读回。但**脚本生成器只会写 `--mem-per-cpu`**：只给 `memory_per_node` 时，详情页「执行命令」里那一行是空的 `#SBATCH --mem-per-cpu=`，sbatch 拒掉整个脚本，任务 FAILED 且照例没有任何日志或事件说明原因。同一个 16 GiB 节点上 8 / 15 / 16 GiB 三个值全败，等价的 `memory_per_cpu` 任务成功；两个字段同时发是 `InternalError`。**round-trip 通过不足以判定一个字段可用——要一路核到平台真正拿它去生成什么。**
- **Slurm 级字段与节点级规格之间没有任何校验，两边都没有。** `sbatch_script` 描述程序怎么用节点，`slurm_cluster_spec` 决定买了什么节点，平台照单全收并返回 `job_id`；控制台也不管——它的「最大值」提示来自项目的单任务配额，不是所选规格，而且 Slurm 那几个输入框排在「选择规格」之前。实测（`HPC-可上网区资源-2`，`0,4,16`，单节点）：`cpus_per_task` 超过节点核数、或 `cpus_per_task × memory_per_cpu` 超过节点内存，任务起来后一两分钟内就 FAILED，**`GetJobLog` 空、`ListJobEvents` 只有正常的 Pod 生命周期，没有任何一处带上 sbatch 的拒绝原因**；而 `number_of_tasks × cpus_per_task` 超过 `instance_count ×` 节点核数时 sbatch 反而**收下**，step 永远排队，平台一直报 RUNNING、`steps` 停在 `-/1`，直到 Workspace 自己的运行时长上限把它停掉。守门只能放在客户端。
- **`max_running_time_ms` / `reserve_on_fail_ms` / `reserve_on_success_ms` 是字符串类型**，发数字会被直接拒。
- **`mount_path` / `mounts` 是死字段：接受、存储、不生效。** 元素是 `{real_path, mount_path, volume}`，没有 `read_only`。受控验证里三种 `volume` 写法全部被接受并原样存进 `start_config.mount_path`，但实例起来后 `/mnt/` 是空的，`find / -name 'probe-*'` 零命中。控制台侧也对得上：notebook 和 train 表单的「高级设置」里没有任何自定义挂载入口，递归抓完全部 SPA chunk 也找不到 `real_path` / `mount_path`。**结论不是「契约未知」，是「这个字段当前没有消费者」。**
- **`hpc` 的 `working_dir` 同类，而且更早暴露**：`CreateJobConsole` 接受它，但 `GetJob` 读回来是 `None`——平台连存都没存。对照组是同一次请求里的 `dataset_info` / `description` / `ttl_after_job_finish_seconds`，三个都完整 round-trip。**写进去读不回来，就不要接。**
- **`train_enable_*` 是 Workspace 能力开关，不是可传的参数。** `GetTrainScheduleConfig` 返回 `train_enable_pre_check` / `train_enable_troubleshoot` / `train_enable_specified_nodes` / `train_enable_slow_detect` / `train_enable_vccl`，控制台据此决定渲染哪些控件。`enable_slow_detect` / `enable_vccl` 虽然被 `CreateJobConsole` 接受，但表单里没有对应控件，是平台侧行为而不是用户可选项，CLI 不暴露。**判断某个字段该不该接时，先看这组开关，再看控制台是否真的渲染了控件，两者缺一不可。**
- **`resource_spec_price` 是嵌套的 proto 风格对象**：`{cpu_type, cpu_count, gpu_type, gpu_count, memory_size_gib, logic_compute_group_id, quota_id}`；CPU 档位省略 `gpu_type`。它由 `quota_id` 那一行的原始 price 对象构造，来源是仍在 v1 的规格菜单端点。
- **镜像一律发注册表 URL 或 `mirror_id`，不发可见名**，否则 `无法找到对应镜像`。`image_type` 取 `SOURCE_PUBLIC` / `SOURCE_PRIVATE` / `SOURCE_OFFICIAL`。
- **Shared Memory 是实例级资源**：`shm_gi`（train / ray）和 `shared_memory_size`（notebook）不能超过所选 Quota 的实例内存。
- **可选字段一律「不传就不出现在 body 里」**，包括两个只读挂载开关——即使只读是更安全的值。一个没有指定该选项的创建请求必须与该选项存在之前逐字节相同，平台的默认值由平台自己决定。
