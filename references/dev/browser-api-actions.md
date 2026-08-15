# Browser API：Action 参考表

> **文档类型**：CLI 维护者参考。协议、信封、认证、分页、scoping、探针方法和验收标准在 [`browser-api.md`](browser-api.md)，本页不重复。
>
> 每条都是 `POST {base_url}/api/v2/{路由}?Action={Action}`，请求体是 JSON，响应取 `ResponseMetadata` / `Result` 信封里的 `Result`。「请求体」列写的是 **CLI 实际发出的键**，不是 discovery 声明的全集；「响应」列写的是**实测的线上键**，discovery 声明的 `Items` / `TotalCount` 在多数 Action 上不是真的。

12 条路由、93 个 Action。`†` 标记的 Action 不在 `discovery` 里，但路由活着、Action 可调；`‡` 标记的整条路由不在 discovery 里。

| 路由 | 域 | Action 数 | 主要 CLI 命令组 |
| --- | --- | --- | --- |
| [`train`](#train--分布式训练) | GPU 训练任务 | 10 | `job` |
| [`hpc`](#hpc--cpu-slurm-批处理) | CPU Slurm 批处理 | 9 | `hpc` |
| [`ray`](#ray--弹性计算) | 弹性计算 | 11 | `ray` |
| [`notebook`](#notebook--交互式建模) | 交互式建模 | 13 | `notebook`、`image save` |
| [`inference_serving`](#inference_serving--模型部署) | 模型部署 | 18 | `serving` |
| [`workspace`](#workspace--工作空间资源) | 计算组、节点、配额 | 5 | `resources`、`<workload> quota`、每个 `create` |
| [`user`](#user--账号) | 账号身份与权限 | 3 | `account permissions`、所有按当前用户过滤的列表 |
| [`project`](#project--项目) | 项目 | 4 | `project`、每个 `create` |
| [`image`](#image--镜像) | 镜像 | 5 | `image` |
| [`model-hub`](#model-hub--模型仓库) | 模型仓库 | 12 | `model`、`serving create` |
| [`file`](#file--文件页-) ‡ | 存储池与目录发现 | 2 | `init --scope project` |
| [`dataset`](#dataset--官方数据集挂载-) ‡ | 官方数据集挂载 | 1 | `dataset validate`、`--dataset` |

**没有 CLI 消费者的 Wrapper**（存在、有测试覆盖，但当前没有命令调用）：`notebook.ListNotebookLifecycles`、`notebook.ListNotebookCreators`、`ray.ListJobCreators`、`ray.ListJobScalingHistories`、`hpc.GetJobLog`、`inference_serving.GetServingLog`、`inference_serving.ListServingScaleHistory`、`inference_serving.GetInferenceServingTerms`、`model-hub.ListModelVersionOptions`、`model-hub.ListModelCreators`、`model-hub.ListModelRelatedServings`、`model-hub.GetHasModelPendingServing`、`model-hub.GetModelPublishPrefill`、`model-hub.GetModelPublishStatus`、`project.GetProjectForPage`。它们在表里照常列出，CLI 列写「—」。

---

## `train` — 分布式训练

Referer：`/jobs/distributedTraining`，详情页 `/jobs/distributedTrainingDetail/{job_id}`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateJobConsole` † | 见下方创建字段表 | `{job_id, sub_code, sub_msg}` | `job create`、`job batch` |
| `GetJob` | `{job_id}` | 完整任务对象：`name` / `status` / `command` / `framework_config[]` / `dataset_info[]` / `project_id` / `workspace_id` / `logic_compute_group_name` / `created_at` / `finished_at` | `job status`、`job command`、`job logs`、`job metrics`、`job wait` |
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

---

## `hpc` — CPU Slurm 批处理

Referer：`/jobs/highPerformanceComputing`，详情页 `/jobs/hpcDetail/{job_id}`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateJobConsole` † | 见下方创建字段表 | `{job_id, sub_code, sub_msg}` | `hpc create`、`hpc batch` |
| `GetJob` | `{job_id}` | `{job_name, status, sbatch_script{}, slurm_cluster_spec{}, description, ttl_after_job_finish_seconds, dataset_info[], project_id, workspace_id, …}` | `hpc status`、`hpc metrics` |
| `ListJobs` | `{workspace_id, page_num, page_size, created_by, status?}` | `{jobs[]\|items[], total}`（`total` 是**字符串**） | `hpc list`、Name Resolver、`cache refresh` |
| `ListJobEvents` | `{pageNum: -1, pageSize: 200, filter:{object_ids:[job_id], object_type:"HPC_JOB"}, sorter:[{field:"last_timestamp", sort:"ascend"}]}` | `{events[]\|items[]\|list[]}` | `hpc events` |
| `ListJobInstances` | `{jobId, page_num, page_size}` | `{items[]\|list[], total}` | `hpc instances` |
| `GetJobLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[]\|items[], total}` | —（无 `hpc logs` 命令） |
| `StopJob` | `{job_id}` | `{job_id, sub_code, sub_msg}` | `hpc stop` |
| `DeleteJob` | `{job_id}` | `{job_id, sub_code, sub_msg}` | `hpc delete` |
| `GetTaskMetric` | 见 [Metrics](#metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `hpc metrics` |

**参数语义与限制**

- **`ListJobInstances` 的 id 键是驼峰 `jobId`**，与同一路由上其它 Action 的 `job_id` 不一致。
- **`ListJobEvents` 的分页键也是驼峰**（`pageNum` / `pageSize`），并且平台会回收已完成任务的事件，所以「查不到事件」在保留期过后是正常稳态。
- **`GetJobLog` 拒绝任何显式 sorter**，包括 `@timestamp`；不发 sorter，需要排序就在客户端做。
- **列表行的名字键是 `job_name`**，`name` 从来没被填充过——读 `name` 会让每个 HPC 任务都没有名字，列表渲染 N/A 且 Name Resolver 匹配不到任何东西。
- **`DeleteJob` 要求先停止**，运行中删除返回 `Conflict`；id 不存在返回 `ResourceNotFound`。
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
| `ListJobEvents` | `{ray_job_id, page_num, page_size, sorter:[{field:"last_timestamp", sort}]}` | `{items[], total}` | `ray events` |
| `ListJobInstances` | `{ray_job_id, page_num, page_size}` | `{items[], total}` | `ray instances` |
| `ListJobScalingHistories` | `{ray_job_id, page_num, page_size}` | `{items[], total}` | — |
| `StartJob` | `{ray_job_id}` | `{ray_job{}}` | `ray start` |
| `StopJob` | `{ray_job_id}` | `{ray_job{}}` | `ray stop` |
| `DeleteJob` | `{ray_job_id}` | `{ray_job{}}` | `ray delete` |
| `GetTaskMetric` | 见 [Metrics](#metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `ray metrics` |

**参数语义与限制**

- **资源键在每个 Action 上都是 `ray_job_id`**；`job_id` 和 `id` 都报 `unknown field`，与 `train` / `hpc` 不同。
- **Workspace scoping 是顶层 `workspace_id`**，`filter` 嵌套在这里会被拒。
- **没有 `CreateJobConsole` 变体**，`ray` 对它答 `InvalidAction`，创建走 `CreateJob`。
- **`ListJobEvents` 的信封是专用的**：`{ray_job_id, page_num, page_size, sorter}`，没有 `object_type`——传了返回 `参数错误`。事件是 K8s 形状（`reason` / `type` / `message` / `first_timestamp` / `last_timestamp` / `count`），关键信号是提交时的 `CreatedRayCluster`（Normal）和卡在 PENDING 时的 `FailedScheduling`（Warning）。
- **`StartJob` 会「受理但不执行」。** 对一个 STOPPED 的 Ray Job 调用，第一次返回干净的成功信封而 `updated_at` 纹丝不动，再调一次变成 `InternalError`。照信封写的 Wrapper 会打印「已启动」而什么都没发生，所以 `ray start` **以状态真的离开 `STOPPED` 为准**，轮询确认后才报成功。
- **列表行的属主键是 `creator`、优先级键是 `priority_name`**；`created_by` 和 `priority` 恒为 null，只读那两个会让每个任务都没有属主、优先级恒 None。
- **`UpdateJob` 不是弹性伸缩杠杆**，只收 `ray_job_id` / `name` / `description`；`worker_groups`、`head_node`、`entrypoint`、`min_replicas`、`replicas`、`task_priority`、`project_id` 全被拒。它是改名字，不是改集群，因此没有封装。
- 实例行是 pod 形状：`instance_id` / `instance_type`（`head` / `worker`）/ `worker_group_name` / `status` / `cpu_count` / `memory_size` / `gpu_count` / `priority_level` / `created_at`。

---

## `notebook` — 交互式建模

Referer：`/jobs/interactiveModeling`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateNotebook` | 见下方创建字段表 | `{notebook_id, sub_code, sub_msg}` | `notebook create`、`notebook batch` |
| `GetNotebook` | `{notebook_id}` | 完整实例对象：`status` / `sub_status` / `workspace{}` / `project{}` / `logic_compute_group{}` / `quota{}` / `node{}` / `dataset_info[]` | `notebook status` / `metrics` / `exec` / `shell` / `ssh` / `proxy-url`、`wait` 轮询 |
| `ListNotebooks` | `{workspace_id, page, page_size, filter_by:{keyword, user_id[], logic_compute_group_id[], status[], mirror_url[]}, order_by:[{field:"created_at", order:"desc"}]}` | `{list[], total}`（`total` 是 int） | `notebook list`、Name Resolver、`cache refresh` |
| `ListNotebookCreators` † | `{workspace_id}` | `{list[], total}` | — |
| `ListNotebookEvents` | `{notebook_id, page, page_size}` | `{list[]\|events[], total}` | `notebook events`、`notebook create` 的等待预览 |
| `ListNotebookLifecycles` | `{notebook_id, page, page_size, start_time?, end_time?}` | `{list[]}` | — |
| `ListRunIndex` | `{notebook_id}` | `{list[]}`，每项 `{index, start_time, end_time}` | `notebook lifecycle` |
| `StartNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook start` |
| `StopNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook stop` |
| `DeleteNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook delete` |
| `SaveNotebookImage` | `{notebook_id, name, version, description}` | **恒为 `{}`**（`Result: null`） | `image save` |
| `GetNotebookAccessUrl` | `{notebook_id}` | `{jupyter_url, vscode_url}` | `notebook proxy-url`、`exec` / `shell`、SSH 链路 |
| `GetTaskMetric` | 见 [Metrics](#metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `notebook metrics` |

**参数语义与限制**

- **分页列表键是 `list`，不是 `items`**（与 `ray` 相反），`total` 是 int（与 `hpc` 相反）。**逐 Action 实测，不要类推。**
- **`ListRunIndex` 无分页**，传 `PageNumber` 直接报错。`end_time = ""` 的那条是当前运行周期，按 `index` 从旧到新排。控制台的「生命周期」页就是用它渲染的——`ListNotebookLifecycles` 对绝大多数 Notebook 返回空列表。
- **`ListNotebookEvents` 的事件字段是平台自有形状**，不是 K8s 形状：`content`（正文）、`created_at`（epoch-ms 字符串）、`event_id`。共享渲染器把 `content → message`、`created_at → last_timestamp` / `first_timestamp`。事件按从旧到新返回，Wrapper 默认自动翻页到 `total`（安全上限 100 页）。
- **找不到资源时返回 `ResourceNotFound`，HTTP 仍是 200**，不再是 v1 的传输层 404。依赖 404 判断「不存在」的调用方必须同时认这个码。
- **`SaveNotebookImage` 不收 `visibility`**（`unknown field "visibility"`），要改可见性只能存完再调 `image.UpdateImage`。它**不返回新镜像的 id**，调用方只能靠列表去找。同一层还有 `EstimateSaveMirrorSize` 和 `CancelSaveMirror`，当前未封装。
- **`GetNotebookAccessUrl` 是 IDE 网关地址，不是 Notebook Proxy。** 两个 URL 归一化后指向同一个网关（两个 IDE 共用同一套 runtime 与 token），任取其一即可。STOPPED 的 Notebook 上它返回两个空字符串，此时回落 Playwright 抓取（那条也会失败，语义不变）。实测 **0.57 秒 vs 6.4–36 秒**。
- **解析顺序是 缓存/热候选 → `GetNotebookAccessUrl` → Playwright**，收口在 `resolve_notebook_vscode_ide_url`。`refresh=True` 时 API 这一档**也走**：refresh 的语义是「别信缓存」，不是「一定要抓」。
- **`exec` / `shell` 全程不起浏览器**：lab URL 取**原始 `jupyter_url`**（不能用 `_ide_gateway_url` 归一化后的形式——terminal 的 REST 与 WebSocket 路由挂在 Jupyter server base 上），`_xsrf` 靠对 `jupyter_url` 发一次普通 GET 拿 cookie，建/删 terminal 是 `POST` / `DELETE api/terminals` 并把 `_xsrf` 放进 `X-XSRFToken` 头。命令结束后回收本次创建的 Terminal。
- `notebook create` 的 `allow_ssh: true` 是硬编码的：平台据此在 proxy URL 上暴露容器内的 rtunnel 端口，缺了它 proxy 返回 404，Notebook SSH 的预检就完不成。该字段省略时默认 false，与镜像里有没有 SSH 工具无关。

---

## `inference_serving` — 模型部署

Referer：`/jobs/modelDeployment`。路由名是**下划线**形式，discovery 里的 `inference-serving` 会 404。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateServingConsole` † | 见下方创建字段表 | `{inference_serving_id, sub_code, sub_msg}` | `serving create`、`serving batch` |
| `ListServings` | `{workspace_id, page, page_size, filter_by:{my_serving:true, keyword?, project_id[]?, status[]?, inference_serving_type[]?}}` | `{inference_servings[], total}` | `serving list`、Name Resolver、`cache refresh` |
| `GetServing` | `{inference_serving_id}` | 完整部署对象：`status` / `replicas` / `node_num_per_replica` / `model_id` / `model_version` / `mirror_id` / `resource_spec_price{}` | `serving status`、`serving metrics` |
| `ListServingVersions` | `{inference_serving_id}` | `{inference_servings[]\|list[], total}` | `serving versions` |
| `ListServingInstances` | `{inference_serving_id, page, page_size}` | `{items[]\|list[]\|instances[], total}` | `serving instances` |
| `ListServingEvents` | `{page, page_size, filter:{object_type:"INFERENCE_SERVING", object_ids:[id]}}` | `{events[]\|items[]\|list[]}` | `serving events` |
| `ListServingScaleHistory` | `{inference_serving_id, page, page_size}` | `{items[]\|list[], total}` | — |
| `GetServingLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[]\|items[], total}` | — |
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
- **`UpdateServing` 没有封装**：它的 `resource_spec_price` 是扁平结构（`cpu_type` / `cpu_count` / `gpu_type` / …），与其它地方嵌套的 `cpu_info` / `gpu_info` 形状不同，安全的 Wrapper 需要基于 `GetServing` 做读改写，且必须先确认省略字段是保留还是清空——当前没有可供受控验证的 serving。
- **`GetServingApiMetric` 与 `GetTaskMetric` 是两个不相干的指标族**，共享零个指标名。前者是请求流量：`QPS`、`SUCCESS_QPS`、`FAIL_QPS`、`SUCCESS_RATE`、`FAIL_RATE`、`REQUEST_COUNT`、`LATENCY`、`TTFT`(+`_P50`/`_P95`/`_P99`)、`TTLT`(+`_P50`/`_P95`/`_P99`)、`INPUT_TOKENS`、`OUTPUT_TOKENS`。它**接受整个 `metric_types` 列表**（`GetTaskMetric` 不接受），也不需要 compute-group 句柄。返回项带 `metric_type` / `group_name` / `data_unit` / `time_series[{timestamp, data}]`。
- `DeleteServing` 的 id 不存在时返回 `ResourceNotFound`。
- 读 Action 逐字接受 v1 请求体、响应字段完全一致；**写侧不能照搬**。

---

## `workspace` — 工作空间资源

Referer：`/jobs/distributedTraining`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ListLogicComputeGroups` | `{page_size: -1, page_num: 1, filter:{workspace_id}}` | `{logic_compute_groups[], total}` | `resources availability`、`<workload> quota`、每个 `create` 的组解析、`init`、`cache refresh` |
| `ListNodeDimension` | `{filter:{workspace_id, logic_compute_group_id}, PageNumber, page_size}` | `{node_dimensions[], total}` | `resources availability`、`resources nodes` |
| `GetLogicComputeGroupResource` | `{workspace_id, logic_compute_group_id}` | `{logic_resouces{}, gpu_type_stats[], runtime_attributes[]}` | `resources availability` |
| `GetWorkspaceQuota` | `{workspace_id}` | `{gpu_high_running, gpu_high_running_used, cpu_*, memory_*, is_fair_workspace, …}` | `resources quota` |
| `GetWorkspaceComputeResource` | `{workspace_id}` | `{logic_resouces{cpu_total, cpu_used, memory_gi_total, memory_gi_used, gpu_total, gpu_used, gpu_low_priority_used}}` | `resources quota` |

**参数语义与限制**

- **一律用 `workspace.*`，不用 `cluster.*`**：同名的 8 个 Action 在 `cluster.*` 下对非集群管理员一律 `AccessForbidden`。
- **`ListLogicComputeGroups` 省略 `page_size` 会返回空列表配非零 `total`**，看起来就像这个工作空间没有任何计算组。`page_size: -1` 有效，保持原样。
- **`ListNodeDimension` 的 `page_size: -1` 只返回 10 条**，必须按 `total` 显式翻页。它的两级 scoping 也最容易踩：`filter` 里只放 `logic_compute_group_id` 返回 `AccessForbidden`，同时放 `workspace_id` 和 `logic_compute_group_id` 才通。
- **节点行的 GPU 数嵌在 `gpu.total` 里**，不是扁平 `gpu_count`；只读扁平键会让每个节点看起来都是零卡，静默把空闲节点数清零。行里还有 `status`（`READY` 判定）、`tasks_associated` / `task_list`（有没有任务）、`cordon_type`、`is_maint`、`resource_pool`（`fault` 要排除），「完全空闲」需要五项同时成立。
- **组资源汇总的键平台拼错成 `logic_resouces`**（少一个 `r`），`GetLogicComputeGroupResource` 与 `GetWorkspaceComputeResource` 同病。GPU 型号在 `gpu_type_stats[0].gpu_info.gpu_type_display`。
- **`ListLogicComputeGroups` 的标识字段叫 `logic_compute_group_id` 而不是 `id`**；`support_job_type_list` 是 **JSON 编码的字符串**，不是数组（`'["interactive_modeling","hpc_job"]'`）。用 `isinstance(x, list)` 判断会把每个组都读成「没声明」，于是按 Workload 过滤计算组这件事看起来生效了、实际一个都没滤掉。取值域：`interactive_modeling` / `hpc_job` / `ray_job` / `distributed_training` / `inference_serving_customize` / `inference_serving_exclusive`，逐组不同。
- **`GetWorkspaceQuota` / `GetWorkspaceComputeResource` 要顶层 `workspace_id`**，套 `filter` 反而被拒。配额字段是 `{资源}_{high|low}_{running|total}` 加可选的 `_used` 后缀：高优先级（保障）和低优先级（可回收）是**两套独立的上限**，一个运行中的任务只吃其中一套，混着读会两边都报错。`-1` 表示不限。
- 两者回答不同的问题：**配额用完了可以被拒，即使机器闲着；机器忙满了也可以被拒，即使配额还有。**
- `ListUserQuotas` / `GetUserTaskQuota` / `GetWorkspaceTaskQuota` / `GetDefaultUserTaskQuota` 需要工作空间管理员，`GetDefaultUserQuota` 普通成员能读但没有信息量——都没有封装。

---

## `user` — 账号

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `GetUserDetail` | `{}` | `{id, name, name_en, email, avatar_url, global_role, created_at, extra_info{}}` | 所有「按当前用户过滤」的列表：`job/hpc/ray/notebook/serving/model list`、`config check` |
| `GetPermissions` † | `{WorkspaceId}` | `{permissions[]}`（如 `"job.trainingJob.create"`） | `account permissions` |
| `GetRoutes` † | `{WorkspaceId}` | `{routes[{name, routes[{path, name, is_fair_workspace}]}]}` | Workspace 枚举、优先级选择、`init`、`cache refresh` |

**参数语义与限制**

- **`GetUserDetail` 只覆盖当前用户**：传空体返回当前账号，传 `user_id` / `id` / `UserId` 一律 `InvalidParameter`。
- **两个未文档化 Action 的 workspace 参数是 PascalCase `WorkspaceId`**，与本路由其余部分不同。
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
- **`GetProjectDetail` 的 id 键是 PascalCase `ProjectId`**。它的 `remain_budget` / `used_budget` / `resource` 是**本来就在跳的值**，连着读两次不相等是正常的。
- CLI 侧翻页固定 `page_size = 100`，直到 `len(items) >= total` 或短页为止；选择器路径用 `page_size: -1` 一次取全。
- 项目行里对调度有意义的是 `gpu_limit`（是否有项目级 GPU-hour 上限）和 `priority_name`（数字字符串），`space_list[]` 给出项目跨哪些 Workspace。

---

## `image` — 镜像

Referer：`/jobs/interactiveModeling`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ListImages` | `{page: 0, page_size: -1, filter:{…见下…}}` | `{images[], total}` | `image list`、`notebook/job/hpc/ray/serving create` 的镜像解析、`cache refresh` |
| `GetImageById` | `{ImageId}` | 单个镜像对象：`image_id` / `name` / `address` / `framework` / `version` / `source` / `visibility` / `status` / `description` / `created_at` | `image detail`、`image save` 的就绪轮询 |
| `CreateImage` † | `{name, version, registry_hint:{workspace_id}, visibility, add_method, description}` | `{image{image_id, …}}` | `image register` |
| `UpdateImage` † | `{id, visibility?, description?}` | — | `image set-visibility` |
| `DeleteImage` | `{image_id}` | — | `image delete` |

**参数语义与限制**

- **同一个标识符，三种拼法**：`GetImageById` 要 **`ImageId`**，`DeleteImage` 要 **`image_id`**，`UpdateImage` 要 **`id`**。传错不会报 `unknown field`：`UpdateImage` 收到 `image_id` 时**默默忽略**，然后拿空 id 去查库，回一句 `InternalError: 数据库错误, 请联系管理员`。**看到这个错先检查字段名。**
- **`ListImages` 的三种 UI 来源用三种 filter**，不是一个简单的 `source` 字段：
  - 官方镜像：`{source: "SOURCE_OFFICIAL", source_list: [], registry_hint:{workspace_id}}`
  - 公开镜像：`{source_list: ["SOURCE_PRIVATE","SOURCE_PUBLIC"], visibility: "VISIBILITY_PUBLIC", registry_hint:{…}}`
  - 个人可见：同上但 `visibility: "VISIBILITY_PRIVATE"`
- **镜像地址在 `address` 字段**，不是 `url`。创建 Workload 时平台匹配的是**注册表 URL 而不是可见名**，发名字会被拒为 `无法找到对应镜像`。
- **`add_method`**：`0` = 本地推送（`docker push`，v1 和 v2 给出同一个 `no image uploaded` 拒绝），`2` = 注册已有镜像地址。
- **就绪状态有两套**：`image register` 产出的镜像走 `READY`，`image save` 产出的走 `SUCCESS`。终态失败包括 `FAILED` / `FAILURE` / `ERROR` / `CANCELLED` / `TIMEOUT` / `ABORTED` / `INTERRUPTED`——漏掉任何一个都会让轮询挂到超时而不是快速失败。
- 「把 Notebook 存成镜像」不在这条路由，在 `notebook.SaveNotebookImage`。

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
| `ListModelRelatedServings` | `{model_id, version, page, page_size}` | `{serving[]\|inference_servings[]\|list[], total}` | — |
| `GetHasModelPendingServing` | `{model_id, version}` | `{has_pending_serving}` | — |
| `GetModelPublishPrefill` | `{model_id, version}` | `{model_info{}, technical_specs{}, integration_info{}}` | — |
| `GetModelPublishStatus` | `{model_id, version}` | `{status, publish_reject_detail, has_published}` | — |
| `GetRecommendedConfig` | `{model_id, version}` | `{min_node_count, min_gpu_count_per_node, min_cpu_count_per_node, min_memory_size_gib_per_node}` | `model deploy-config` |
| `CheckModelVLLMCompatible` | `{model_id, version, inference_serving_type}` | `{is_vllm_compatible}` | `model deploy-config` |
| `CreateModel` † | `{name, project_id, workspace_id, model_source_path, model_source_type, model_type[], tags[], description}` | `{model_id}` | `model register` |

**参数语义与限制**

- **`ListModelVersions` 与 `ListModelVersionOptions` 名字近似、极易接反**，只能靠响应字段区分：前者多一个 `next_version`，是详情抽屉用的**富**视图（含模型路径、源路径、大小、发布状态、运行中的 serving 数）；后者是部署表单用的**精简**版本列表。
- **`ListModelVersions` 不接受 `page`。** `ListModelCreators` 接受 `project_id`。
- **`GetRecommendedConfig` 给的是下限而不是推荐值**：四个 `min_*` 映射到 `serving create --quota gpu,cpu,mem` 和 `--nodes-per-replica`，照抄不等于最优。
- **`CheckModelVLLMCompatible` 是按版本问**；`GetModelVLLMCompatibleData` 一次回答所有版本，但部署决策需要的是前者。
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

**`hpc.CreateJobConsole`** — 必填：`{job_name, logic_compute_group_id, project_id, workspace_id, enable_notification, sbatch_script:{number_of_tasks, cpus_per_task, memory_per_cpu, enable_hyper_threading, entrypoint}, slurm_cluster_spec:{predef_quota_id, cpu, mem_gi, image, image_type, instance_count, spec_price}}`。可选：`priority`、`dataset_info[]`、`description`、`ttl_after_job_finish_seconds`、`is_publicpath_readonly`，以及 `sbatch_script` 里的运行时长四件套。

**`ray.CreateJob`** — `{name, description, workspace_id, project_id, entrypoint, task_priority, head_node:{mirror_id, image_type, logic_compute_group_id, quota_id, shm_gi?}, worker_groups:[{group_name, mirror_id, image_type, logic_compute_group_id, min_replicas, max_replicas, quota_id, shm_gi?}]}`，可选 `is_publicpath_readonly`。

**`inference_serving.CreateServingConsole`** — 必填：`{workspace_id, project_id, inference_serving_type, name, logic_compute_group_id, model_id, model_version, mirror_id, command, port, description, replicas, node_num_per_replica, task_priority, resource_spec_price}`。可选：`custom_domain`、`shm_gi`、`model_source`、`is_publicpath_readonly`、`enable_auto_scaling`。**`queue_id` / `dataset_info` / `envs` / `scale_status` 被拒且没有对应物。**

### 几处不能靠直觉推的细节

- **`envs` 的元素是 `{name, value}`，不是 `{key, value}`**，`key` 会被拒为 `unknown field "key"`。**`train.GetJob` 的读投影不回显 `envs`**（容器里明明有变量，读回来是 `[]`），所以不能用读接口核对写了什么。
- **`is_projectuserspath_readonly` 只有 notebook 有**，而且要项目 Maintainer：普通成员传了会拿到 `AccessForbidden: only project maintainer can enable project users path readonly mount`。
- **HPC 的最大运行时长不在顶层**。`max_running_time_ms` 和 `max_running_time_minutes` 打顶层都是 `unknown field`；它嵌在 `sbatch_script` 里，而且控制台**同时发两份**：`job_max_time`（`"D-HH:MM:SS"`，即 Slurm `--time`）和 `max_running_time_days` / `_hours` / `_minutes`。`slurm_cluster_spec` 一个都不收。
- **HPC 的优先级键是 `priority`，不是 `task_priority`。** 拼错时 v2 答 `priority must be set`，读起来像值缺失而不是名字写错。
- **`max_running_time_ms` / `reserve_on_fail_ms` / `reserve_on_success_ms` 是字符串类型**，发数字会被直接拒。
- **`mount_path` / `mounts` 是死字段：接受、存储、不生效。** 元素是 `{real_path, mount_path, volume}`，没有 `read_only`。受控验证里三种 `volume` 写法全部被接受并原样存进 `start_config.mount_path`，但实例起来后 `/mnt/` 是空的，`find / -name 'probe-*'` 零命中。控制台侧也对得上：notebook 和 train 表单的「高级设置」里没有任何自定义挂载入口，递归抓完全部 SPA chunk 也找不到 `real_path` / `mount_path`。**结论不是「契约未知」，是「这个字段当前没有消费者」。**
- **`hpc` 的 `working_dir` 同类，而且更早暴露**：`CreateJobConsole` 接受它，但 `GetJob` 读回来是 `None`——平台连存都没存。对照组是同一次请求里的 `dataset_info` / `description` / `ttl_after_job_finish_seconds`，三个都完整 round-trip。**写进去读不回来，就不要接。**
- **`train_enable_*` 是 Workspace 能力开关，不是可传的参数。** `GetTrainScheduleConfig` 返回 `train_enable_pre_check` / `train_enable_troubleshoot` / `train_enable_specified_nodes` / `train_enable_slow_detect` / `train_enable_vccl`，控制台据此决定渲染哪些控件。`enable_slow_detect` / `enable_vccl` 虽然被 `CreateJobConsole` 接受，但表单里没有对应控件，是平台侧行为而不是用户可选项，CLI 不暴露。**判断某个字段该不该接时，先看这组开关，再看控制台是否真的渲染了控件，两者缺一不可。**
- **`resource_spec_price` 是嵌套的 proto 风格对象**：`{cpu_type, cpu_count, gpu_type, gpu_count, memory_size_gib, logic_compute_group_id, quota_id}`；CPU 档位省略 `gpu_type`。它由 `quota_id` 那一行的原始 price 对象构造，来源是仍在 v1 的规格菜单端点。
- **镜像一律发注册表 URL 或 `mirror_id`，不发可见名**，否则 `无法找到对应镜像`。`image_type` 取 `SOURCE_PUBLIC` / `SOURCE_PRIVATE` / `SOURCE_OFFICIAL`。
- **Shared Memory 是实例级资源**：`shm_gi`（train / ray）和 `shared_memory_size`（notebook）不能超过所选 Quota 的实例内存。
- **可选字段一律「不传就不出现在 body 里」**，包括两个只读挂载开关——即使只读是更安全的值。一个没有指定该选项的创建请求必须与该选项存在之前逐字节相同，平台的默认值由平台自己决定。
