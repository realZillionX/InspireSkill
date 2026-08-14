# Browser API v2 实现地图

> **文档类型**：CLI 维护者参考。日常启智操作不要加载本页；Agent 使用公开命令时只依赖 Name-only CLI 合同和对应 `--help`。
>
> **边界**：本页只记录 `/api/v2` 的请求契约、权限边界和已验证的迁移约束。Session、账号隔离、Referer 和输出边界不变量在 [`browser-api-v1.md`](browser-api-v1.md) 第 2 节，对两代接口同时成立，不在本页重复。

`/api/v2` 与 `/api/v1` 是同一台 `qz.sii.edu.cn` 上的两代接口，共用同一个 CAS Session。官方 CLI `qz` 是 `/api/v2` 的一个客户端，不是它的前置依赖；调用 v2 不需要安装任何外部二进制。

## 1. 事实源

维护顺序：

1. `GET {base_url}/discovery` 是平台自己发布的 Action 清单，但**不是完整面**（见第 3 节）。
2. [`cli/inspire/platform/web/browser_api/`](../../cli/inspire/platform/web/browser_api/) 中已迁 v2 的 Wrapper 定义实际在用的请求体与响应解析。
3. `cli/tests/` 锁定 Wrapper 合同。
4. 平台行为变化时，用只读 Action 复核，再同步修改 Wrapper、测试和本页。

只读探针可以自由复核；**写操作不能用来探测**，创建、启动、停止、保存和删除语义只能通过受控验证确认。

## 2. 请求契约

```
POST {base_url}/api/v2/{route}?Action={Action}
Cookie: inspire-session=...        # 与 /api/v1 同一个 CAS Session
Content-Type: application/json
{ ...请求体... }
```

- **认证只需要 `inspire-session` cookie。** `x-inspire-client-source` 头在只读面上非必需，缺失不会触发跳转。
- **响应是 AWS 风格信封**：成功为 `{"ResponseMetadata": {...}, "Result": {...}}`，业务错误为 `{"ResponseMetadata": {"Error": {"Code", "Message"}}}` 且 **HTTP 仍是 200**。不能用状态码判断成败，必须解包 `ResponseMetadata.Error`。
- **请求必须 `allow_redirects=False`。** 认证失败时网关返回 302 到 Keycloak；跟随重定向会把这个信号变成一张 HTML 登录页，和「路由不存在」无法区分。
- 判定顺序固定为 **429 → 401/302 → 非 JSON → `ResponseMetadata.Error` → `Result`**。429 必须在 Content-Type 嗅探之前判掉：网关限流返回的是 HTML 错误页，先嗅探会把它误判成路由不通。

### Session 生命周期

`inspire-session` 的有效期是分钟级，远短于一次完整的批量调用。`get_web_session()` 直接返回磁盘缓存**而不校验**，所以：

- 连续调用必须在 401 / 302 时用 `get_web_session(force_refresh=True)` 重建 Session 再重试一次，不能只在进程启动时取一次。
- 直接读取 `~/.inspire/accounts/<account>/web_session.json` 拿 cookie 是错的：绕过了续期路径，拿到的往往是已过期的值，表现为整批调用统一 401，很容易误判成「v2 不接受我们的 cookie」。

## 3. Discovery 能信什么

`GET {base_url}/discovery` 返回 `{"Result": {"Version": "<etag>", "Services": [...]}}`，每个 Action 带完整的嵌套参数与响应结构。

**不带任何认证头**，且匿名与带 Cookie 的响应逐字节相同——它是静态文档，**不按调用者角色过滤**。因此 Action 出现在 discovery 里**不代表当前账号能调**，权限只能实测（第 6 节）。

不能信的部分：

| 字段 | 问题 |
| --- | --- |
| Service `Name` | 不等于网关路由名，见第 4 节 |
| Service 全集 | **不完整**。`file` 和 `dataset` 两条路由完全不在 discovery 里，但都活着且当前 CLI 正在用；`model_plaza` 同样存在但已无消费者 |
| Action 全集 | **不完整**，且缺口不小。当前 175 个声明之外，实测活着的至少还有 21 个：`train.CreateJobConsole`、`hpc.CreateJobConsole`、`inference_serving.CreateServingConsole`、`image.CreateImage`、`image.UpdateImage`、`image.PreheatImage`、`model-hub.CreateModel`、`model-hub.UpdateModel`、`model-hub.DeleteModel`、`user.GetPermissions`、`user.GetRoutes`、`user.ListSSH`、`user.GetMyPermissions`、`project.ListProjects`、`project.GetProjectDetail`、`project.GetProjectOwners`、`project.GetProjectListV2`、`file.GetSftpgoConnectionInfo`、`dataset.ValidateDataset`、`workspace.GetWorkspaceQuota`、`workspace.GetWorkspaceComputeResource` |
| 分页字段 `PageNumber` | 是唯一的 PascalCase 字段，实际网关同时接受 `PageNumber` / `page_num` / `page`，三者等价且都真实生效 |

**判定「无对应物」时的固定错误模式**：这个错误已经犯过很多次——`inference_serving` 只测了 discovery 里的 `CreateServing` 就说契约变了（其实有 `CreateServingConsole`）；`/cluster_nodes/list` 只测了 `ListWorkspaceNodes` 就说没有（其实是 `ListNodeDimension`，且 `AccessForbidden` 只是 scoping 没写全）；`/user/quota` 只看了 `user` 服务就说没有配额 Action（`workspace.*` 下有 10 个）。**最大的一次**：`/user/permissions`、`/user/routes`、`/project/list`、`/project/{id}`、`/project/owners`、`/file/*`、`/model_plaza/*`、`/image/create`、`/image/update`、`/model/create` 一共 10 个家族被写进第 9 节的「无对应物」表，实际全部有 Action——它们只是不在 discovery 里。**Discovery 只能用来找候选，不能用来否定。下结论前必须把所有 service 里名字沾边的 Action 全部列出来逐个实测，并按下一段的存在性探针枚举未文档化的变体；路由本身也要探，`404 page not found` 才是路由不存在，`InvalidAction` 说明路由活着。**

**穷举名字找不到，不等于不存在——去看控制台调什么。** 用带 Session 的浏览器打开对应页面录网络请求，是比猜名字强得多的方法：平台前端现在**全程走 v2**，它调什么就说明什么存在。`/resource_prices/logic_compute_groups/` 的对应物就是这么找到的（第 9 节），`user.ListSSH` 和 `user.GetMyPermissions` 也是这一趟顺手抓到的。猜名字只适合作为补充。

未文档化 Action 的命名有规律，可以据此猜候选：基本是 v1 路径去掉资源前缀后的 PascalCase，`GET /project/{id}` → `GetProjectDetail`，`/file/dir/list` → `GetDirList`，`/project/owners` → `GetProjectOwners`。猜不中就换动词（`Get` / `List` / `Create` / `Update` / `Delete`）和单复数重试，一轮十几个名字就能覆盖。

因为 Action 全集不可信，某个 Action 是否存在只能实测。网关对此有明确信号，且不需要发出一次真正的写请求：**空 body 打过去，`InvalidAction: unknown action: X` 表示该 Action 不存在，其它错误码（`InvalidParameter` / `InternalError`，通常带参数校验文案）表示存在但参数不对**。迁移写操作前用这一条确认有没有 Console 变体：`train`、`hpc`、`inference_serving` 有，`ray` 没有。空 body 会在校验阶段被拒，不会创建任何东西，但这只能用来判断存在性——语义仍须按第 18 行的受控验证确认。

**字段有没有，用同一把尺子量。** 网关先按 proto 解析 body，再校验业务必填项，所以只带一个候选字段打过去：`InvalidParameter: invalid JSON: proto: unknown field "X"` 表示该字段不在合同里，**其它任何报错都表示字段在合同里**（通常是「名称不能为空」之类的必填校验）。因为必填项全缺，这同样创建不出任何东西。这是唯一能测未文档化 Console 变体字段面的办法，第 8 节那张创建字段表就是这么得出的。注意读一句 `InternalError: internal server error` 时先做对照实验：`hpc.CreateJobConsole` 对空 body 和对合法字段都回这一句，只有塞进一个确定不存在的字段才会回 `unknown field`——没有对照就无法区分「字段被接受、处理器崩了」和「请求根本没进解析」。

discovery 的 `Version` 是内容 etag，可以直接用来判断平台是否改过接口面。历史上它**双向变动过**：早期版本有 `audit`、`file` 两个 service 和整套节点运维 Action，之后被移除；`image`、`model-hub` 则是后加的。所以不能假设新版本是旧版本的超集。当前 `Version` 是 `e1daec0f`，与上一次记录相比又漂了三处：Action 从 169 涨到 175；`hpc` 的每个 Action 从「不声明任何参数」变成 `CreateJob` 声明 17 个参数；`image` 反而缩到只剩 `ListImages` / `GetImageById` / `DeleteImage` / `ListImageBrands` 四个，`CreateImage` 和 `UpdateImage` 退回未文档化状态。

**控制台 SPA 是代码分割的。** 想从前端 bundle 反推某个表单的字段形状时，只取 `/assets/index.*.js` 入口是不够的，创建表单在惰性加载的 chunk 里，需要从入口递归抓一遍。这条在确认 `mount_path` 有没有前端生产者时用上了——递归抓完 322 个 chunk，`real_path` / `mount_path` 零命中，于是判定「字段虽然被接受，但没有可抄的正确形状」。

## 4. 路由名与 Action 名

网关路径用的是**路由名**，不是 discovery 里的 Service `Name`，而且两个带连字符的 Service 行为相反：

| discovery Service `Name` | 网关路由 | 另一种写法 |
| --- | --- | --- |
| `inference-serving` | **`inference_serving`** | 连字符形式 404 |
| `model-hub` | **`model-hub`** | 下划线形式 404 |
| 其余 9 个 | 与 `Name` 相同 | — |
| （不在 discovery 里） | **`file`** | `files` 404 |
| （不在 discovery 里） | **`dataset`** | `datasets`、`dataset-hub`、`dataset_hub`、`data_plaza`、`data`、`plaza` 全部 404 |

**不能从 `Name` 机械推导路由。** 新增 Service 时必须实测两种写法。当前 Wrapper 用的 `inference_serving` 是正确形式。

`audit`、`billing`、`storage` 三个路由也活着（分别返回 `InvalidAction` 或 `AccessForbidden: user is not system admin`，都不是 404），但没有找到与当前 CLI 相关的 Action。

## 5. Scoping 决定权限判定

多个 `workspace.*` Action 把工作空间放在**嵌套的 `filter` 对象**里，而不是顶层 `workspace_id`。漏掉这层嵌套时，平台把请求理解成集群级查询并返回 `AccessForbidden`。

```jsonc
// 错：被当成集群级请求 -> AccessForbidden
{"workspace_id": "ws-...", "PageNumber": 1, "page_size": 5}

// 对：工作空间级
{"filter": {"workspace_id": "ws-..."}, "PageNumber": 1, "page_size": 5}
```

**`AccessForbidden` 有两种含义**：真的没权限，或者忘了 scoping。区分方法是照 discovery 的参数结构把 `workspace_id` 放到它声明的位置再试一次。把 scoping 问题当成权限问题，会导致本来可用的 Action 被错误地标成不可迁移。

## 6. `cluster.*` 与 `workspace.*`

discovery 里 8 个 Action 在两个 Service 下同名且描述几乎一致，但权限边界完全不同。**普通工作空间成员一律用 `workspace.*`**：

| Action | `cluster.*` | `workspace.*` |
| --- | --- | --- |
| `ListNodeDimension` | AccessForbidden | 可用 |
| `ListTaskDimension` | AccessForbidden | 可用 |
| `ListProjectDimension` | AccessForbidden | 可用 |
| `ListUserDimension` | AccessForbidden | 可用 |
| `ListLogicComputeGroups` | AccessForbidden | 可用 |
| `GetOverviewResourceMetric` | AccessForbidden | 可用 |

`cluster.*` 的 15 个只读 Action 对非集群管理员全部不可用，正确 scoping 也不解除。`workspace.*` 中 `ListUserQuotas` 和 `ListWorkspaceParentProjects` 需要工作空间管理员，普通成员返回 `You are not the admin of the <workspace_id>`。

## 7. 响应形状

分页响应统一是 `Result.total` 加**一个列表键**，但列表键名**没有统一约定**，至少存在 16 种：`items`、`list`、`jobs`、`events`、`logs`、`images`、`quotas`、`serving`、`inference_servings`、`compute_groups`、`node_dimensions`、`task_dimensions`、`project_dimensions`、`user_dimensions`、`node_resource_types`、`support_brand_info_list`。

解包器不能猜列表键，必须按 Action 显式声明；`Result` 缺失时还要回退 legacy `data`。这条是 [`jobs.py`](../../cli/inspire/platform/web/browser_api/jobs.py) 里 `_v2_result()` 的职责，新增 v2 Wrapper 复用它，不要各自实现。

字段名也有个别陷阱，`ListLogicComputeGroups` 返回的标识字段叫 `logic_compute_group_id` 而不是 `id`。

## 8. 当前已迁到 v2 的域

| 路由 | Action |
| --- | --- |
| `train` | `CreateJobConsole`、`GetJob`、`ListJobs`、`ListJobInstances`、`ListJobEvents`、`GetJobLog`、`StopJob`、`DeleteJob` |
| `hpc` | `CreateJobConsole`、`GetJob`、`ListJobs`、`ListJobEvents`、`ListJobInstances`、`GetJobLog`、`StopJob`、`DeleteJob` |
| `inference_serving` | `CreateServingConsole`、`ListServings`、`GetServing`、`ListServingVersions`、`ListServingInstances`、`ListServingEvents`、`ListServingScaleHistory`、`GetServingLog`、`GetServingApiMetric`、`GetInferenceServingTerms`、`GetServingConfigByWorkspaceId`、`GetInferenceServingUserProjectList`、`StartServing`、`StopServing`、`ScaleServing`、`RollbackServing`、`DeleteServing` |
| `ray` | `CreateJob`、`GetJob`、`ListJobs`、`ListJobCreators`、`ListJobEvents`、`ListJobInstances`、`ListJobScalingHistories`、`StartJob`、`StopJob`、`DeleteJob` |
| `notebook` | `CreateNotebook`、`GetNotebook`、`ListNotebooks`、`ListNotebookCreators`、`ListNotebookEvents`、`ListNotebookLifecycles`、`ListRunIndex`、`StartNotebook`、`StopNotebook`、`DeleteNotebook`、`SaveNotebookImage`、`GetNotebookAccessUrl` |
| `workspace` | `ListLogicComputeGroups`、`ListNodeDimension`、`GetLogicComputeGroupResource`、`GetWorkspaceQuota`、`GetWorkspaceComputeResource` |
| `user` | `GetUserDetail`、`GetPermissions`、`GetRoutes` |
| `project` | `GetProjectForPage`、`ListProjects`、`GetProjectDetail`、`GetProjectOwners` |
| `image` | `ListImages`、`GetImageById`、`CreateImage`、`UpdateImage`、`DeleteImage` |
| `file` | `GetSystemStorageTypeList`、`GetDirList` |
| `dataset` | `ValidateDataset` |
| `model-hub` | `ListModels`、`GetModelDetail`、`ListModelVersions`、`ListModelVersionOptions`、`ListModelCreators`、`ListModelRelatedServings`、`GetHasModelPendingServing`、`GetModelPublishPrefill`、`GetModelPublishStatus`、`GetRecommendedConfig`、`CheckModelVLLMCompatible`、`CreateModel` |
| 各域 | `GetTaskMetric`（`notebook` / `train` / `hpc` / `ray` / `inference_serving` 各一份） |

### 创建面的字段合同

五个 Console / Create 变体接受的字段互不相同，且只有 `notebook.CreateNotebook`、`train.CreateJob` 和 `ray.CreateJob` 在 discovery 里声明过。下表用上一节那把 `unknown field` 尺子逐字段量出来，**是判断某个网页选项能不能进 CLI 的唯一依据**：

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

几处不能靠直觉推的细节：

- **`envs` 的元素是 `{name, value}`，不是 `{key, value}`**，`key` 会被拒为 `unknown field "key"`。`train.GetJob` 的读投影**不回显 `envs`**（容器里明明有变量，读回来是 `[]`），所以不能用读接口核对写了什么。
- **`is_projectuserspath_readonly` 只有 notebook 有**，而且要项目 Maintainer：普通成员传了会拿到 `AccessForbidden: only project maintainer can enable project users path readonly mount`。
- **HPC 的最大运行时长不在顶层**。`max_running_time_ms` 和 `max_running_time_minutes` 打顶层都是 `unknown field`；它嵌在 `sbatch_script` 里，而且控制台**同时发两份**：`job_max_time`（`"D-HH:MM:SS"`，即 Slurm `--time`）和 `max_running_time_days` / `_hours` / `_minutes`。`slurm_cluster_spec` 一个都不收。
- `train.mounts` 的元素是 `{real_path, mount_path, volume}`，没有 `read_only`。`mount_path` 虽然被 notebook 和 train 接受，但递归抓完整个控制台 SPA 也找不到任何生产者，`volume` 的取值无从对照，因此当前**故意不接**。
- `reserve_on_fail_ms` / `reserve_on_success_ms` 和 `max_running_time_ms` 一样是**字符串**类型，不是数字。

### `dataset`：官方数据集挂载

`/api/v2/dataset` 不在 discovery 里，路由活着，穷举四十个候选名后只有一个 Action：

```jsonc
POST /api/v2/dataset?Action=ValidateDataset
{"datasets": [{"dataset_id": "pixabay-81k", "version_id": "v0"}], "workspace_id": "ws-..."}
→ Result: {"datasets_result": [{dataset_id, version_id, success, path, error_message}]}
```

**`dataset_id` 是数据集的 code，`version_id` 是版本的 code，都不是数字主键。** `pixabay-81k` + `v0` 解析成功并返回存储路径 `sftpgo/pixabay-81k/v0`；同一个数据集的 `1710` + `2310` 回 `数据集不存在`。错误码三种：`2000 数据集不存在`、`2001 版本不存在`、`2005 无访问权限`。

返回的 `path` 是**平台内部存储路径**，不是容器内挂载点；容器里固定出现在 `/inspire/dataset/<code>/<version>`，且是只读挂载（受控验证时 `mount` 输出确认为 `ro`）。创建请求里的 `dataset_info[].path` 要填这个存储路径，所以创建前必须先走一次 `ValidateDataset`——这正是控制台「校验数据」按钮做的事。

**检索不在这条路由上，也不在这个平台上。** 控制台侧边栏的「数据集」是外链，目录、搜索、版本和权限都在 `aip.sii.edu.cn`，见第 13 节。

### 写侧已经踩到的坑

- **`ray.StartJob` 会「受理但不执行」。** 对一个 STOPPED 的 Ray Job 调用，第一次返回干净的成功信封，`updated_at` 纹丝不动，再调一次变成 `InternalError`。照信封写的 Wrapper 会打印「已启动」而什么都没发生——和当年 `StartServing` 那个 `API error: None` 是同一类谎报。`ray start` 因此以状态真的离开 `STOPPED` 为准，轮询确认后才报成功。
- **`ray.UpdateJob` 不是弹性伸缩杠杆**，只收 `ray_job_id` / `name` / `description`；`worker_groups`、`head_node`、`entrypoint`、`min_replicas`、`replicas`、`task_priority`、`project_id` 全被拒。它是改名字，不是改集群。
- **`ScaleServing` 的字段是单数 `replica`**，而 create 和 update 用复数 `replicas`。
- **`UpdateServing` 的 `resource_spec_price` 是扁平结构**（`cpu_type` / `cpu_count` / `gpu_type` / …），与其它地方嵌套的 `cpu_info` / `gpu_info` 形状不同。安全的 Wrapper 需要基于 `GetServing` 做读改写，且必须先确认省略字段是保留还是清空——当前账号在九个 Workspace 里都没有 serving，无法受控验证，因此**未接**。
- **`hpc.GetUserProjectWorkingDir` 返回的 `Result` 是一个裸 JSON 字符串**，不是对象，`_v2_result()` 会把它压成 `{}`。要接这个 Action 得先改解包。

`model-hub` 里有两个名字近似、极易搞混的 Action，只能靠响应字段区分：`ListModelVersionOptions` 返回 `{list, total}`，对应 v1 的 `GET /model/{id}/versions`；`ListModelVersions` 多一个 `next_version`，对应 v1 的 `GET /model/{id}`。按名字直觉配对会把两者接反。`ListModelVersions` 不接受 `page`；`ListModelCreators` 接受 `project_id`，与 v1 `/model/users` 的作用域一致。`CreateModel` 不在 discovery 里，逐字接受 v1 `/model/create` 的请求体，返回 `{model_id}`；`model_source_path` 必须落在所给 workspace + project 的路径下，`global_user` 路径会被 `存储路径格式不正确` 拒掉。受控验证走建→读→删（`DeleteModel`）完成。

`project` 有四个 Action，discovery 只声明了 `GetProjectForPage`：

- `ListProjects` 对应 `/project/list`，逐字接受 v1 请求体，实测两个工作空间下**逐叶子相等**（0 处差异）。带 `filter.check_admin` 时也覆盖 `/project/list_v2`——行集相同，且是**严格超集**：v1 `list_v2` 的 `created_at`、`updated_at`、`status`、`notebook`、`training_job` 全是空值，`gpu_limit` / `hpc` / `hpc_limit` 直接没有，v2 全都填满。
- `GetProjectDetail`（`ProjectId`）对应 `GET /project/{id}`，字段集完全一致；唯三不等的 `remain_budget` / `used_budget` / `resource` 是**本来就在跳的值**，v1 连着读两次同样不等。
- `GetProjectOwners` 对应 `/project/owners`，空体调用，返回逐字节相同的 `items`。
- `GetProjectForPage` 是项目管理页自己的行集，比 `ListProjects` 窄：它会滤掉用户已退出或已结束的项目，所以两者条数不同是**设计如此**，不是谁坏了。旧版文档把这条差异当成「`GetProjectForPage` 与 `list_v2` 不等价，所以没有对应物」，方向搞反了。

`user` 有三个 Action，discovery 只声明了 `GetUserDetail`：

- `GetUserDetail` 只覆盖**当前用户**：传空体返回当前账号，字段与 v1 `/user/detail` 完全一致；传 `user_id` / `id` / `UserId` 一律 `InvalidParameter`。它是 train / hpc / ray / model 列表按当前用户过滤时的身份来源。`ListAPIKeys` 也可用，但随 `user api-keys` 命令一起下线，已无消费者。
- `GetPermissions`（`WorkspaceId`）对应 `/user/permissions/{workspace_id}`，权限码集合逐字相同。
- `GetRoutes`（`WorkspaceId`）对应 `/user/routes/{workspace_id}`，四个 route group 与 `userWorkspaceList` 的每个条目逐字相同，**`is_fair_workspace` 也在**——它是 qz 优先级选择器的唯一数据源，缺了就没法迁。

`image` 五个 Action 里 `CreateImage` 和 `UpdateImage` 不在 discovery 里。整组逐字接受 v1 请求体，`ListImages` 在三种 UI 来源下与 v1 **逐字节相等**（官方 17 条、公开 4593 条、个人可见 57 条）。两个必须记住的坑：

- **同一个标识符，三种拼法。** `GetImageById` 要 `ImageId`，`DeleteImage` 要 `image_id`，`UpdateImage` 要 `id`。传错不会报 `unknown field`：`UpdateImage` 收到 `image_id` 时**默默忽略**，然后拿空 id 去查库，回一句 `InternalError: 数据库错误, 请联系管理员`。看到这个错先检查字段名，不要去找 DBA。
- v1 的「`/image/update` 用 `id` 不用 `image_id`」这条老约定 v2 原样保留，没有借迁移统一。

受控验证：建（`CreateImage`，`add_method=2`）→ 读（`GetImageById`）→ 改可见性与描述（`UpdateImage`）→ 删（`DeleteImage`）→ 确认列表与详情都查不到，全程走 v2。`add_method=0`（本地推送）在 v1 和 v2 上给出**同一个**拒绝（`no image uploaded`），语义没有漂移。

「把 Notebook 存成镜像」不在 `image` 服务，在 **`notebook.SaveNotebookImage`**（对应 v1 `/mirror/save`）。它逐字接受 v1 请求体，并且保留了 v1 那条怪规则：**不收 `visibility`**，措辞都一样（`unknown field "visibility"`），要改可见性只能存完再调 `image.UpdateImage`。**两代都不返回新镜像的 id**——v1 回一个光秃秃的 `{"code": 0}`（连 `data` 都没有），v2 回 `Result: null`，解包后同样是 `{}`，所以调用方只能靠列表去找。同一层还有 `EstimateSaveMirrorSize` 与 `CancelSaveMirror`（对应 `/mirror/save/estimate_size` 和 `/mirror/save/cancel`），前者实测与 v1 逐字节相同。

受控验证在 `CPU资源空间` 用 `HPC-可上网区资源-2` 的 `0,1,4` 最小配额 + `ubuntu-original:22.04`（77.9 MB，官方镜像里最小的）起了一个临时 Notebook：v2 提交出一个真镜像（`add_method=Notebook`，196 MB，约 30 秒到 `SUCCESS`），同一个 Notebook 上又用 v1 提交一次做响应体对照，随后 `inspire image save` 走迁移后的 Wrapper 端到端再跑一遍；三个镜像与 Notebook 全部删除。

`file` 整个服务不在 discovery 里（历史上出现过又被删掉，见第 3 节末尾），但路由活着，两个 Action 逐字接受 v1 请求体、返回同一个集合。**唯一差异是行顺序**：`GetSystemStorageTypeList` 的 12 个存储池和 `GetDirList` 的目录都会换序，排序后完全相等。当前调用方都不依赖顺序，新调用方也不要依赖。

**Metrics 不属于 `workspace.*`。** v1 用一个集群级端点 `/cluster_metric/resource_metric_by_time` 服务所有 Workload，v2 没有对应的集群级端点：每个 service 各有一份 `GetTaskMetric`，接受**逐字相同**的 `{filter:{logic_compute_group_id, task_id, task_type}, metric_types, time_range}`，返回同一个（拼错的）键 `time_seris_metric_groups`，五个域实测数量与 v1 一致。看起来同名的 `workspace.GetOverviewResourceMetricByTime` 是工作空间级总览，对普通成员返回 `AccessForbidden`，不是它的对应物。第 6 节「用 workspace.\* 不用 cluster.\*」只适用于两边同名的那 8 个 Action，不能推广成「集群级端点一律换 workspace.\*」。

`ListLogicComputeGroups` 是那 8 个之一，`workspace.*` 可用、`cluster.*` 返回 `AccessForbidden`，与第 6 节一致。但它有个分页陷阱：**省略 `page_size` 时返回空列表却带非零 `total`**，看起来就像工作空间里没有任何计算组。v1 的 `page_size: -1`（取全部）v2 同样接受，保持原样即可。

配额那一族里普通成员只能用两个：`GetWorkspaceQuota`（Workspace 配额上限与当前占用）和 `GetWorkspaceComputeResource`（集群物理容量），两者都不在 discovery 里，而且 **`workspace_id` 要放顶层**——第 5 节那层 `filter` 嵌套在这里反而被拒。配额值 `-1` 表示不限；组级汇总那个键平台拼错成 `logic_resouces`，与 `GetLogicComputeGroupResource` 同病。其余的 `ListUserQuotas`、`GetUserTaskQuota`、`GetWorkspaceTaskQuota`、`GetDefaultUserTaskQuota` 实测都要 Workspace 管理员；`GetDefaultUserQuota` 普通成员能读，但它描述的是**新成员的默认值**、在可达 Workspace 上一律不限，对调用者没有信息量。

**分页语义逐 Action 不同，不能类推。** 同为 `workspace.*`，`ListNodeDimension` 的 `page_size: -1` 只返回 10 条而不是全部，必须显式按 `total` 翻页。它也是第 5 节 scoping 陷阱最典型的一例：`filter` 里只放 `logic_compute_group_id` 返回 `AccessForbidden`，同时放 `workspace_id` 和 `logic_compute_group_id` 才通——**看到 `AccessForbidden` 先把 scoping 补全再下结论**。

节点与组资源这一块，v1 那侧本来就是坏的：`/cluster_nodes/list` 对非管理员返回 `You are not the admin of any workspace`（`resources nodes` 因此整条命令报错），`/compute_resources/list_node_dimension`、`/compute_resources/node_dimension/list` 和三条 `cluster_basic_info` 路径全部 404。对应物是 `ListNodeDimension`（每节点实时状态，GPU 数在嵌套的 `gpu.total` 里，不是扁平 `gpu_count`）和 `GetLogicComputeGroupResource`（组级汇总，字段与 v1 逐一对应，注意平台把键拼成了 `logic_resouces`）。

`inference_serving` 的读 Action 逐字接受 v1 请求体、响应字段完全一致，但**写侧不能照搬**。两个已经踩到的坑：

- `StartServing` / `StopServing` 早先迁了 URL 却仍用 v1 的信封检查（`code != 0`）解包，而 v2 响应根本没有 `code`，于是两条命令对任何输入都返回 `API error: None`。改用 `_v2_result()` 后真正的错误才暴露出来：请求体里的 `version` 字段 v2 也不认，正确的体只有 `{inference_serving_id}`。**迁 URL 而不同时换解包器，会把真错误伪装成假错误。**
- 创建要用 **`CreateServingConsole`**，不是 discovery 里那个 `CreateServing`。后者的 Description 明写「via OpenAPI with simplified config」，契约确实不同：要 `spec_id` 而不是 `resource_spec_price`，`image` 是普通字符串而不是 `mirror_id`，且不收 `description` / `inference_serving_type` / `model_source`。Console 变体和 `train` / `hpc` 一样不在 discovery 里，但**逐字接受 v1 的控制台请求体**，迁移只是换 URL。这里踩过一次弯路：只测了 discovery 里的 `CreateServing`，看到一串 unknown field 就判定「契约不同、不能迁」，而没有先按第 3 节那条规则查 Console 变体。**看到写操作的字段被大面积拒绝时，第一反应应该是「是不是找错 Action 了」，而不是「契约变了」。**

`hpc` 是全域迁移，且是最省事的一个：discovery 对 hpc 的每个 Action **都没有声明任何参数**，但实测下来 v1 的请求体逐字被接受，响应字段也逐字一致，所以 Wrapper 只换了 URL。`DeleteJob` 要求先停止，运行中删除返回 `Conflict`；id 不存在返回 `ResourceNotFound`（不像 `train.DeleteJob` 给的是 `AccessForbidden`）。

`train` 把 v1 两个事件端点合并成了一个 Action：`/train_job/job_event_list`（裸 `job_id`）和 `/train_job/events/list`（`filter` 信封）在 v2 都是 `ListJobEvents`，只靠 `filter.object_type` 取 `job` / `instance` 区分，事件条数与 v1 逐一对得上。另有一个同名易混的 `ListJobInstanceEvents`（参数是 `job_id` + `instance_name`），它无论返回多少条 `total` 都是 `"0"`，需要分页的调用方不要用它。`DeleteJob` 与 `hpc.DeleteJob` 语义一致：要求先停止，运行中删除返回 `Conflict: 当前状态（运行中）无法删除`。差别在找不到资源时它返回 `AccessForbidden` 而不是 `ResourceNotFound`。受控验证在 `分布式训练空间` 用 1 卡 H100 最小规格完成（建→停→删→确认消失，随即释放）；分布式训练任务在 `CPU资源空间` 的所有 CPU 组都建不起来（平台报 `无法找到对应镜像`，实际是组不支持），所以这一条只能在 GPU 工作空间验。

`notebook` 同样是全域迁移，但 `/notebook/lab*` 和 Notebook Proxy 按第 9 节保留 v1。几处与 `ray` 相反、必须逐个实测的地方：列表键是 **`list`** 而不是 `items`，`total` 是 int 而不是字符串；`ListRunIndex` 无分页，传 `PageNumber` 直接报错；v1 用 `operation` 枚举复用的 `/notebook/operate` 在 v2 拆成了 `StartNotebook` / `StopNotebook`，v1 那条 REST 风格的 `DELETE /notebook/{id}` 也有了正式的 `DeleteNotebook`。找不到资源时返回 `ResourceNotFound`（HTTP 仍是 200），不再是 v1 的传输层 404，依赖 404 判断「不存在」的调用方必须同时认这个码。

**网关 URL 现在只有一个消费者：`proxy-url`。** 打开 Web IDE 的 `notebook url` 和 `notebook vscode` 已经删除——这个 CLI 由 Agent 驱动，Agent 没有浏览器可开，「在本机打开一个网页」这个动作对它没有任何意义。留下的是「拿到容器里某个端口的外部地址」，那是 Agent 真正要的：它在 Notebook 里部署完东西之后得能去请求。

`proxy-url` 因此是整个 Notebook 命令组里唯一打印平台 URL 的命令。这不是绕过输出边界，是显式开的口子：`format_json(..., preserve_raw={"url"})`。理由是这个地址的**每一段都是平台句柄**（`ws-`、`project-`、`user-`、runtime、token），默认的 `scrub_raw_ids` 会把它整条洗成 `<redacted>`，洗完就不通了。代价必须说清楚：**这个地址等同于凭据**，内嵌的短期 token 让持有者对该 Notebook 的访问权与你相同，而它会进 Agent 对话记录和 shell 历史。

没有免 token 的形式。实测：平台域上的 `/api/v1/notebook/lab/{id}/proxy/{port}/` 返回 `404 page not found`，只有带 token 的网关 URL 真的会去连容器端口（端口没人监听时返回 500 `connect ECONNREFUSED`，`--check` 据此报 `no_service` 而不是 `blocked`）。

网关 URL 现在**优先向平台要，不再默认起浏览器**：`notebook.GetNotebookAccessUrl` 返回 `{jupyter_url, vscode_url}`，归一化之后与 Playwright 抓取的结果**逐字节相同**（两个 IDE 共用同一套 runtime 与 token，`_split_ide_gateway` 只是重写 IDE 标记，所以两个字段任取其一都行）。实测 **0.57 秒对 6.4–36 秒**。

解析顺序是 **缓存/热候选 → `GetNotebookAccessUrl` → Playwright**，收口在 `resolve_notebook_vscode_ide_url`，因此 `proxy-url` 与 rtunnel 的 SSH 候选路径同时受益。API 在 `refresh=True` 时也会走：refresh 的语义是「别信缓存」，不是「一定要抓」。STOPPED 的 Notebook 上它返回两个空字符串，此时回落浏览器路径（那条也会失败，语义不变）。

**`notebook exec` / `shell` 已经不再起浏览器。** 那条链路以前要拉一个无头 Chromium，只为了三件事，逐件都有更直接的做法：拿 lab URL 用 `GetNotebookAccessUrl`（注意用**原始 `jupyter_url`**，不能用 `_ide_gateway_url` 归一化后的形式——terminal 的 REST 与 WebSocket 路由挂在 Jupyter server base 上，`vscode` 那次重写在这里是错的）；拿 `_xsrf` 只需对 `jupyter_url` 发一次普通 GET，它就是个 cookie；建/删 terminal 是 `POST`/`DELETE api/terminals`，把 `_xsrf` 放进 `X-XSRFToken` 头即可。交互式 `shell` 的会话本来就跑在 Python WebSocket 上（`job_shell.py` 的 `_WebSocketClient`），`exec` 的抓取循环则从页内 JavaScript 逐行移植到了 Python，协议不变：等 prompt（最多等 3 秒就直接发）、按 2048 字节分块喂 stdin、看到 `<marker>:exit:<code>` 就收工。

受控验证在一个 RUNNING 的 CPU Notebook 上完成，全程用 import hook 封死 `playwright` 包：命令正常执行、退出码正确传出（`ls` 不存在的路径回 2）、多行输出完整。**耗时的大头不在这条链路**：实测时间线是横幅 1.5 秒、prompt 3.9 秒、命令结果 30.8 秒——中间那 27 秒是容器里 `echo '<b64>' | base64 -d | bash` 拉起的**内层 bash 在 source rc 文件**，与传输方式无关，老的浏览器路径同样要付。

`ray` 是全域迁移，v1 `/ray_job/*` 九个端点已全部退出。响应逐字段与 v1 一致，因此 Wrapper 的归一化未改动。三条与其它域不同的约束：资源键在每个 Action 上都是 `ray_job_id`（`job_id` 和 `id` 都报 `unknown field`）；工作空间 scoping 是顶层 `workspace_id`，第 5 节那层 `filter` 嵌套在这里会被拒；**没有 `CreateJobConsole` 变体**，创建走 `CreateJob`。

其余域仍在 `/api/v1`，映射见 [`browser-api-v1.md`](browser-api-v1.md) 第 3 节。

## 9. 仍留在 v1 的端点

这张表**只收有当前消费者、且实测确认过的端点**，每条都注明是哪一类：Action 不存在，还是有 Action 但不可用。两类处置方式不同，混在一起写会重演第 3 节那个错误。没有消费者的端点不进这张表——它记录的是「迁不动的活代码」，不是平台接口面的全集。

| v1 端点 | 类别 | 依据 |
| --- | --- | --- |
| `/notebook/lab*`、Notebook Proxy | Action 不存在 | `notebook` 下 `GetNotebookLab` / `GetLabUrl` / `GetNotebookProxy` / `GetProxyUrl` 均 `InvalidAction`。唯一沾边的 `GetNotebookAccessUrl` 语义不同，见下文，**故意不接** |
| `/train_job/remote_cmd` | **不是 Action 能表达的东西** | 双向 PTY 流。23 个候选名 × 5 条路由全部 `InvalidAction`，`/api/v2/{train_job,train/remote_cmd,terminal,ws,exec}` 全部 404。与 Notebook Proxy 同类：v2 是「POST + `?Action=` + JSON 信封」的网关，装不下流式连接，所以这里不存在「还没迁完」，而是**不该迁** |
| `/resource_prices/logic_compute_groups/` | **可复现，但重建更贵**（2N+1 次调用 vs N 次） | 见下文 |

**Notebook Proxy 是什么。** 它是平台自带的一条反向代理路径，把 Notebook **容器内部**监听的某个 HTTP 端口，从 `qz.sii.edu.cn` 这个已登录的域名转出来：

```
{base_url}/api/v1/notebook/lab/{notebook_id}/proxy/{port}/
```

JupyterLab / VS Code 打开之后还有一种带 token 的等价形式 `/{jupyter|vscode}/<notebook>/<token>/proxy/<port>/`，token 段必须当敏感信息处理（[`rtunnel.py`](../../cli/inspire/platform/web/browser_api/rtunnel.py) 的 `redact_proxy_url` 专门负责在日志与报错里抹掉它）。

CLI 里有两个消费者，第二个是关键：

1. `inspire notebook proxy-url --port N`——**返回**容器里那个 HTTP 服务（TensorBoard、Gradio、Streamlit、推理端点）的外部地址，供调用方直接请求；它不打开任何东西。H100 / H200 受限 Notebook 上默认拒绝，要 `--allow-restricted` 才放行。
2. **整条 SSH 链路都架在它上面。** 受限环境不接受直连，所以 InspireSkill 在容器里起一个 sshd（默认 22222 端口），再**通过这条 HTTP 代理**去够它——`_wait_for_rtunnel` 轮询的就是这个 proxy URL，等 sshd 应答。`notebook ssh` / `scp` / `ssh-config` 以及外部 OpenSSH 工具能用，全靠这一条。

所以它不是可有可无的遗留：Web IDE 那两条命令已经删了，但删掉 Notebook Proxy 等于同时删掉 `proxy-url` 和整套 Notebook SSH。它与平台用户中心的 SSH 公钥注册表（曾经的 `/ssh/*`）没有任何关系——后者管的是账号级公钥，rtunnel 读的是本机 `~/.ssh/*.pub` 并直接注入容器。

**`/resource_prices/logic_compute_groups/` 的现状。** 这条查过两轮，第一轮的结论是错的，记在这里以免重犯。

先说清楚它是什么：**它是规格菜单，不是价目表**。名字里的 `resource_prices` 有误导性——全仓搜 `total_price_per_hour` / `cpu_price` / `gpu_price` / `memory_price` 零命中，CLI 只读 `quota_id` 加 `(gpu_count, cpu_count, memory_size_gib)`，外加 serving 读的 `cpu_info.cpu_type` / `gpu_info.gpu_type`。它是把用户敲的 `-q 1,20,200` 翻成平台要的 `quota_id` 的**唯一**来源，挡在每一个 `create` 命令前面，`<workload> quota` 打印的也是它。

**第一轮结论「v2 没有这份数据」是错的。** 那一轮只做了名字穷举（27 个候选 × 11 条路由全 `InvalidAction`）就下结论。正确做法是**抓平台自己的前端**：用带 Session 的浏览器打开 `/jobs/interactiveModeling` 和 `/jobs/distributedTraining` 录网络请求，结果是**控制台全程 v2、零 v1 请求**，规格选择器渲染靠的是 `workspace.GetScheduleConfig` + `workspace.ListLogicComputeGroups` + `workspace.ListWorkspaceNodes`。（这一趟还顺手抓到两个未文档化的 Action：`user.ListSSH`、`user.GetMyPermissions`。）**猜 Action 名找不到，不等于没有；去看控制台调什么。**

**但控制台把组↔规格的过滤放在客户端**，规则已经完整复原，**三个工作空间 16 个组 16/16 逐组一致**：

1. **`logic_compute_group_ids` 为空 = 对所有组开放。** 漏掉这条会得出灾难性的错误结论——按「必须包含本组」过滤时 `CPU临时测试空间` 是 0/3 一致，加上这条立刻 3/3。
2. **规格必须装得进组内某个节点**（cpu / memory / gpu 三项都 ≤ 某个 `node_spec`）。`HPC-可上网区资源-2` 的节点是 55 核 375G，配置给 10 条而 v1 只留 7 条，被砍的正是 `110核`、`55核500GB`、`15U500G`。
3. **组必须真的有可分配容量。** 这条最不显眼：`开发区-H200-3号机房` 与 `训练区-H200-1号机房` 节点硬件完全相同、`support_job_type_list` 都含 `interactive_modeling`，但前者 `GetLogicComputeGroupResource` 返回 `cpu_total: 0 / gpu_total: 0`（`node_count` 是 1，那个节点不贡献可分配量），v1 因此对它返回 0 条。**这是运行时状态，不是静态配置**。

**结论：不迁，而且理由是成本，不是不可行。** 规则 2 要 `GetLogicComputeGroupNodeSpecs`、规则 3 要 `GetLogicComputeGroupResource`，两者都是**按组**的，于是重建一个工作空间的配额目录要 **2N+1 次 v2 调用**，而 v1 是 **N 次**（实测 3 组→7 vs 3、4 组→9 vs 4、9 组→19 vs 9）。原本设想的「工作空间级一次调用」并不成立——`GetScheduleConfig` 只提供静态菜单，两条过滤规则都得逐组补齐。

代价还不止请求数：迁过去等于**在客户端维护一份平台调度端过滤逻辑的副本**。平台改一条规则，我们不会收到任何信号，只会开始默默地把不能用的档位报给用户、或者把能用的藏起来。v1 那一个端点直接给出答案，这个职责本来就该在服务端。

要重新评估这个决定，触发条件是明确的：平台出现一个**工作空间级、已经按组解析好**的配额 Action（届时 2N+1 变 1）。在那之前没有理由动。

**这张表在 2026-08-08 大幅缩水过一次。** 它原先列着 `/user/permissions`、`/user/routes`、`/project/list`、`/project/{id}`、`/project/owners`、`/file/*`、`/model_plaza/*`、`/image/create`、`/image/update`、`/model/create` 共 10 个家族，理由都是「discovery 里没有」。实测下来这 10 个全部有可用 Action：9 个已迁完，`/model_plaza/*` 因为始终没有 CLI 消费者，整族连同 Wrapper 一起删除。唯一真正没有对应物的只有上面这几条。**往这张表里加行之前，先按第 3 节把存在性探针跑一遍。**

## 10. 回落纪律

v1 与 v2 并存期间，**只有「v2 这条路由不通」才回落 v1**：网关 404 `page not found`、405、5xx，或响应非 JSON。

不回落的情况，逐条都有理由：

- **`AccessForbidden` 不回落。** v2 的权限判断更严格且更正确；回落等于用 v1 的宽松答案盖掉 v2 的正确答案。
- **`InvalidParameter` 不回落。** 这是我们自己请求写错了，回落只会让错误一直不被发现。
- **429 绝不回落。** 平台正在要求降速，回落等于把请求量翻倍。限流要靠退避重试处理，重试耗尽后让调用方看见。

同一端点的回落告警每进程只提示一次；批量命令会对十几个工作空间循环调同一个端点，逐次提示会刷屏。

## 11. 性能预期

v2 相对 v1 **没有可测量的延迟或吞吐优势**：等价端点单请求中位延迟基本持平，个别 v1 更快；并发压测下两者状态码和 wall time 无差异，均未触发限流。

因此迁移的理由是**接口面和长期可用性**（`image`、`model-hub` 只在 v2 存在；平台在 v2 上迭代），不是性能。任何以「v2 更快」为由的改造都缺乏依据。

## 12. 迁移验收

把一个域从 v1 换到 v2，除 [`browser-api-v1.md`](browser-api-v1.md) 第 6 节的五条外，还要满足：

1. 路由名经过两种写法实测，不是从 discovery `Name` 推导的。
2. 工作空间 scoping 放在 discovery 声明的位置，并确认返回的不是 scoping 造成的 `AccessForbidden`。
3. 响应解包走 `_v2_result()`，列表键显式声明，不依赖猜测。
4. 401/302 触发 Session 续期并重试一次。
5. 回落条件符合第 10 节，且 `AccessForbidden`、`InvalidParameter`、429 都不回落。
6. 该域若在第 9 节的留守清单里，不做迁移，并保持 v1 路径；反过来，判它「没有对应物」之前必须跑完第 3 节的存在性探针。
7. 写操作经过受控验证，不能只凭只读探针的结果推断。
8. 写操作的成功以**状态真的变了**为准，不以响应信封为准。`ray.StartJob` 会返回成功却什么都不做（第 8 节），这类谎报只有回读状态才发现得了。

## 13. 数据广场是另一个平台

`aip.sii.edu.cn`（上海创智学院数据广场）不是启智的一部分：不同 host、不同 API 风格、不同 Session，只共用同一套 CAS SSO。启智控制台侧边栏的「数据集」是外链，`qz` 这边只有 `dataset.ValidateDataset` 负责把某个版本挂进容器（第 8 节），检索、版本和权限全在这边。

信封是 `{"code": 0, "data": {...}, "msg": "..."}`，**不是 AWS 风格**，`code != 0` 即失败。握手三步，纯 HTTP，不需要浏览器：

```
1. GET  https://cas.sii.edu.cn/cas/login?service=<urlencode("https://aip.sii.edu.cn/")>
        带现有 Session 里的 CASTGC → 302，Location 上带 ?ticket=ST-...   （必须 allow_redirects=False）
2. POST https://aip.sii.edu.cn/api/base/login   {"ticket": "...", "service": "https://aip.sii.edu.cn/"}
        → 下发 `datasets-session` cookie，body 里带 userInfo
3. 之后的调用带该 cookie；前端还会发 `x-user-id: <userInfo.ID>`，实测不带也通
```

CAS 的登录路径是 `/cas/login`，不是 `/login`——后者 404。

已封装的端点：

| 端点 | 说明 |
| --- | --- |
| `GET /api/datasets/getDatasetsList?page=&pageSize=&keyword=&tags=&sortBy=&order=` | 目录；`total` 与 `list` 同层 |
| `POST /api/datasets/findDatasets` `{datasetId}` | 详情 + `versions[]` |
| `GET /api/datasetTags/getDatasetTagsList` | 全部 52 个标签，五个 `categoryId` 分类 |

几条只有实测才知道的约束：

- **`tags` 是逗号连接的数字 tagId，语义为 OR**；但**空字符串不是通配符**，`tags=""` 返回 0 行。前端把空值整个删掉，客户端也必须省略这个键，不能急着把查询字典拼满。
- **`pageSize` 无上限**，1000 一次取回全部；`page` 越界返回空 `list` 但 `total` 仍然正确。
- **`keyword` 连描述一起匹配**且不区分大小写，命中范围通常比预期宽，所以按 code 定位要在结果里取精确相等的那一行，不能取第一条。
- **`findDatasets` 的失败和未登录共用 `code: 7`**：坏 `datasetId` 是 HTTP 200 + `查询失败:record not found`，未登录是 HTTP 401 + `未登录或非法访问`。**判断要不要重新握手只能看 HTTP 状态码，不能看 `code`。**
- `state` 有四个值（`active` / `wanted` / `processing` / `error`），版本另有 `downloading` / `pending_upload`；`hasPermission` 按账号给，为 false 的数据集挂载时报 `2005 无访问权限`，申请权限只有网页端有入口。
- `filesSize` 的单位是 MiB；`dataFormats` 是一个 **JSON 编码的字符串**，不是数组。
- `datasetCode` 全表唯一，标签名也唯一，两个 name→handle 映射都不会歧义。版本号**不保证是 `vN`**，实际存在 `v1-br`、`2026-07-30`、`v3again`。

存在但当前没有消费者、也未封装的端点：`getDatasetsListUserCenter`、`datasetApplyApprove/*`（权限申请流）、`createDatasets`、`createDatasetVersion`、`updateDatasetsValue`、`checkDatasetsName`、`datasetUserRole/*`。
