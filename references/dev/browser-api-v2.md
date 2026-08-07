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

**不带任何认证头**，且匿名与带 Cookie 的响应逐字节相同 —— 它是静态文档，**不按调用者角色过滤**。因此 Action 出现在 discovery 里**不代表当前账号能调**，权限只能实测（第 6 节）。

不能信的部分：

| 字段 | 问题 |
| --- | --- |
| Service `Name` | 不等于网关路由名，见第 4 节 |
| Action 全集 | **不完整**。`train.CreateJobConsole` 和 `hpc.CreateJobConsole` 不在 discovery 里，但真实存在且当前 CLI 正在使用 |
| 分页字段 `PageNumber` | 是唯一的 PascalCase 字段，实际网关同时接受 `PageNumber` / `page_num` / `page`，三者等价且都真实生效 |

因为 Action 全集不可信，某个 Action 是否存在只能实测。网关对此有明确信号，且不需要发出一次真正的写请求：**空 body 打过去，`InvalidAction: unknown action: X` 表示该 Action 不存在，其它错误码（`InvalidParameter` / `InternalError`，通常带参数校验文案）表示存在但参数不对**。迁移写操作前用这一条确认有没有 Console 变体：`train` 和 `hpc` 有，`ray` 没有。空 body 会在校验阶段被拒，不会创建任何东西，但这只能用来判断存在性 —— 语义仍须按第 18 行的受控验证确认。

discovery 的 `Version` 是内容 etag，可以直接用来判断平台是否改过接口面。历史上它**双向变动过**：早期版本有 `audit`、`file` 两个 service 和整套节点运维 Action，之后被移除；`image`、`model-hub` 则是后加的。所以不能假设新版本是旧版本的超集。

## 4. 路由名与 Action 名

网关路径用的是**路由名**，不是 discovery 里的 Service `Name`，而且两个带连字符的 Service 行为相反：

| discovery Service `Name` | 网关路由 | 另一种写法 |
| --- | --- | --- |
| `inference-serving` | **`inference_serving`** | 连字符形式 404 |
| `model-hub` | **`model-hub`** | 下划线形式 404 |
| 其余 9 个 | 与 `Name` 相同 | — |

**不能从 `Name` 机械推导路由。** 新增 Service 时必须实测两种写法。当前 Wrapper 用的 `inference_serving` 是正确形式。

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
| `train` | `CreateJobConsole`、`GetJob`、`ListJobs`、`ListJobInstances`、`ListJobEvents`、`GetJobLog`、`StopJob` |
| `hpc` | `CreateJobConsole`、`GetJob`、`ListJobs`、`ListJobEvents`、`ListJobInstances`、`GetJobLog`、`StopJob`、`DeleteJob` |
| `inference_serving` | 生命周期 Action（动态拼接） |
| `ray` | `CreateJob`、`GetJob`、`ListJobs`、`ListJobCreators`、`ListJobEvents`、`ListJobInstances`、`ListJobScalingHistories`、`StopJob`、`DeleteJob` |
| `notebook` | `CreateNotebook`、`GetNotebook`、`ListNotebooks`、`ListNotebookCreators`、`ListNotebookEvents`、`ListNotebookLifecycles`、`ListRunIndex`、`StartNotebook`、`StopNotebook`、`DeleteNotebook` |

`hpc` 是全域迁移，且是最省事的一个：discovery 对 hpc 的每个 Action **都没有声明任何参数**，但实测下来 v1 的请求体逐字被接受，响应字段也逐字一致，所以 Wrapper 只换了 URL。`DeleteJob` 要求先停止，运行中删除返回 `Conflict`；id 不存在返回 `ResourceNotFound`（不像 `train.DeleteJob` 给的是 `AccessForbidden`）。

`train` 把 v1 两个事件端点合并成了一个 Action：`/train_job/job_event_list`（裸 `job_id`）和 `/train_job/events/list`（`filter` 信封）在 v2 都是 `ListJobEvents`，只靠 `filter.object_type` 取 `job` / `instance` 区分，事件条数与 v1 逐一对得上。另有一个同名易混的 `ListJobInstanceEvents`（参数是 `job_id` + `instance_name`），它无论返回多少条 `total` 都是 `"0"`，需要分页的调用方不要用它。`/train_job/delete` 仍留在 v1：`DeleteJob` 存在且参数相同，但分布式训练任务在 CPU资源空间 的所有 CPU 组都建不起来（平台报 `无法找到对应镜像`，实际是组不支持），受控验证只能在 GPU 工作空间做，因此删除语义尚未验证。

`notebook` 同样是全域迁移，但 `/notebook/lab*` 和 Notebook Proxy 按第 9 节保留 v1。几处与 `ray` 相反、必须逐个实测的地方：列表键是 **`list`** 而不是 `items`，`total` 是 int 而不是字符串；`ListRunIndex` 无分页，传 `PageNumber` 直接报错；v1 用 `operation` 枚举复用的 `/notebook/operate` 在 v2 拆成了 `StartNotebook` / `StopNotebook`，v1 那条 REST 风格的 `DELETE /notebook/{id}` 也有了正式的 `DeleteNotebook`。找不到资源时返回 `ResourceNotFound`（HTTP 仍是 200），不再是 v1 的传输层 404，依赖 404 判断「不存在」的调用方必须同时认这个码。

`ray` 是全域迁移，v1 `/ray_job/*` 九个端点已全部退出。响应逐字段与 v1 一致，因此 Wrapper 的归一化未改动。三条与其它域不同的约束：资源键在每个 Action 上都是 `ray_job_id`（`job_id` 和 `id` 都报 `unknown field`）；工作空间 scoping 是顶层 `workspace_id`，第 5 节那层 `filter` 嵌套在这里会被拒；**没有 `CreateJobConsole` 变体**，创建走 `CreateJob`。

其余域仍在 `/api/v1`，映射见 [`browser-api-v1.md`](browser-api-v1.md) 第 3 节。

## 9. 尚无 v2 对应的 v1 域

以下 v1 家族在当前 discovery 中**没有任何对应 Action**，迁移时必须保留 v1，不能按「v2 全覆盖」规划：

| v1 家族 | 用途 |
| --- | --- |
| `/ssh/*` | SSH 公钥管理，Notebook 连接链路的基础 |
| `/file/dir/list`、`/file/get_system_storage_type_list` | 共享盘目录与存储池发现，Path Alias 的来源 |
| `/notebook/lab*`、Notebook Proxy | Web IDE 入口与容器 HTTP 端口暴露 |
| `/model_plaza/*` | Model Plaza 浏览与部署配置 |
| `/model/create`、`/image/create`、`/image/update` | Model 与 Image 注册；`model-hub` 和 `image` 在 v2 只有只读与删除 |
| `/user/permissions/{workspace_id}`、`/user/routes/{workspace_id}` | 权限与工作空间路由发现 |
| `/project/owners` | Project Owner 列表 |

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
6. 该域若在第 9 节的无对应清单里，不做迁移，并保持 v1 路径。
7. 写操作经过受控验证，不能只凭只读探针的结果推断。
