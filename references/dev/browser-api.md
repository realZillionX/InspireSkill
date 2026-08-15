# Browser API：协议与不变量

> **文档类型**：CLI 维护者参考。日常启智操作不要加载本页；Agent 使用公开命令时只依赖 Name-only CLI 合同和对应 `--help`。
>
> 逐 Action 的请求体、响应、参数语义、CLI 映射与限制在 [`browser-api-actions.md`](browser-api-actions.md)。数据广场（`aip.sii.edu.cn`）是另一个平台，记在 [`data-plaza-api.md`](data-plaza-api.md)。

Browser API 是启智控制台自己用的接口面：同一台 `qz.sii.edu.cn`、同一个 CAS Session，控制台 SPA 全程走 `/api/v2` 的 Action 网关。官方 CLI `qz` 是这套接口的另一个客户端，不是它的前置依赖；调用它不需要安装任何外部二进制。

当前 CLI 封装 **12 条路由、93 个 Action**，另有 **4 处 v1 端点**因为 v2 装不下或重建更贵而保留（第 8 节）。

## 1. 事实源

维护顺序，从强到弱：

| 顺位 | 事实源 | 它定义什么 |
| --- | --- | --- |
| 1 | [`cli/inspire/platform/web/browser_api/`](../../cli/inspire/platform/web/browser_api/) | 唯一构造平台请求的地方：CLI 实际发什么、读哪个键、怎么归一化 |
| 2 | [`cli/inspire/platform/web/session/`](../../cli/inspire/platform/web/session/) | 账号隔离、登录、Cookie、代理、传输层重试 |
| 3 | `cli/tests/test_browser_api_*.py` | 锁定 Wrapper 合同与输出边界 |
| 4 | 控制台前端实际发的请求 | 判定「平台有没有这个能力」时唯一可靠的来源 |
| 5 | `GET {base_url}/discovery` | 候选 Action 名与参数形状。**是线索，不是合同**（第 6 节） |
| 6 | 当前 Click Help | 公开 CLI 合同 |

`test_browser_api_boundary.py` 保证平台路径只在 `browser_api/` 内构造：命令层出现 `_browser_api_path` 或 `/api/v1` 字面量会直接让 CI 失败，例外必须写进该测试的 `_ALLOWED` 并注明理由（当前只有两条，见第 8 节）。

本页与 [`browser-api-actions.md`](browser-api-actions.md) 只收**有当前消费者、且可复现**的事实。未闭合的调查、已删除的 v1 域、迁移过程记录都不进入。

## 2. 请求契约

```
POST {base_url}/api/v2/{route}?Action={Action}
Cookie: inspire-session=…
Content-Type: application/json
Referer: {base_url}/{对应控制台页面}

{ …请求体… }
```

- **只有 POST、只有 JSON body**，动作名在 query 里。没有 REST 风格的路径参数，没有 GET/DELETE 变体。
- **认证只需要 `inspire-session` cookie。** `x-inspire-client-source` 头在只读面上非必需，缺失不会触发跳转。
- **`Referer` 必须与页面域匹配**，由各 Wrapper 按对应控制台页面构造，调用方不要自行拼接。
- Base URL、Browser API Prefix 和代理来自当前有效配置；Playwright 登录与后续请求复用同一账号的网络设置。
- 所有平台请求使用**目标 Account Alias** 的浏览器 SSO Session；跨账号 Notebook 命令不能退回当前活动账号的 Session。

### 响应信封

成功与失败共用一个 AWS 风格信封，**HTTP 状态码在两种情况下都是 200**：

```jsonc
{"ResponseMetadata": {…}, "Result": {…}}                                  // 成功
{"ResponseMetadata": {"Error": {"Code": "…", "Message": "…"}}}            // 业务错误
```

解包唯一入口是 [`core.py`](../../cli/inspire/platform/web/browser_api/core.py) 的 `_v2_result()`：它先看 `ResponseMetadata.Error`，再取 `Result`；`Result` 为 `null` 时回退 legacy `data`，取不到就给 `{}`。**不能用状态码判断成败，也不能自己写信封检查**——照 v1 的 `code != 0` 写会把每一个真错误变成 `API error: None`。

`Result` 里的**列表键逐 Action 不同，没有跨 Action 约定**，解包器必须按 Action 显式声明，见 Action 表的「响应」列。

### 判定顺序

固定为 **429 → 401/302 → 非 JSON → `ResponseMetadata.Error` → `Result`**。429 必须在 Content-Type 嗅探之前判掉：网关限流返回的是 HTML 错误页，先嗅探会把它误判成「路由不通」。

### 错误码与处置

| Code | 含义 | 处置 |
| --- | --- | --- |
| `InvalidAction` | 该 Action 在这条路由上不存在 | 路由是活的，换 Action 名；`404 page not found` 才是路由不存在 |
| `InvalidParameter` | 请求体不符合合同（含 `proto: unknown field "X"`、必填缺失、`page or page_size too large`） | 我们自己写错了，**不回落 v1** |
| `AccessForbidden` | 真的没权限，**或者** scoping 没写全 | 先按第 5 节把 scoping 补全再下结论 |
| `ResourceNotFound` | 对象不存在 | `notebook`/`hpc`/`serving` 的找不到就是它；`train.DeleteJob` 反而给 `AccessForbidden` |
| `Conflict` | 状态不允许该操作（运行中删除、已运行再启动） | 先 `stop` 或先轮询状态 |
| `InternalError` / `Throttling` / `ServiceUnavailable` / `SlowDown` / `RequestTimeout` / `TooManyRequests` | 平台暂时不回答 | 抛 `TransientAPIError`，由传输层退避重试 |

`InternalError` 有歧义：它既可能是平台真的崩了，也可能是字段被接受后处理器出错。用它判断字段存在性之前必须做对照实验（第 7 节）。

### 传输层

[`session/__init__.py`](../../cli/inspire/platform/web/session/__init__.py) 的 `request_json()` 是 requests 与 Playwright 两条路径唯一的收口：

- **HTTP 408 / 425 / 429 / 500 / 502 / 503 / 504** 和信封里的 transient 错误码统一抛 `TransientAPIError`（`ValueError` 子类，所以既有的 `except ValueError` 边界仍把它映射成 API 错误）。
- 遇到 `TransientAPIError` **最多重试 3 次**，优先采纳平台的 `Retry-After`（上限 8 秒），否则指数退避加抖动；重试耗尽后原样抛给调用方。
- **限流吸收必须留在传输层。** Workspace 级问题（配额目录、资源可用量、节点维度）都是「每个 Compute Group 一次请求」的扇出，正是限流器会反应的形状；Wrapper 不要各自重试。
- **`page_size` 超过 5000 由 `_clamped_page_size()` 无条件截断。** 网关对超限答 `InvalidParameter: page or page_size too large`，而且是**逐 service 执行**的——`hpc` 强制，`ray` 实测给 10000 也照收——所以不能靠某个 service 没报错推断没有上限。截断不损失任何东西：超限请求本来也不会比按上限的请求多返回一行。`page_size: -1` 是「取全部」，平台认，原样放过。
- 请求体不会被就地修改，截断走浅拷贝。

### 「平台没有回答」不等于「平台回答了没有」

这是整层最重要的一条：**只有平台成功返回的空结果，才能读作「这个对象没有」**。Wrapper 不得把 `TransientAPIError`、`SessionExpiredError` 或任何失败折叠成 `[]`、`None`、`0` 或「不存在」。事件、配额目录、资源可用量、节点计数尤其危险——用户会据此做调度决策。

对应地：请求失败与「平台返回空」必须在返回值上可区分，且要有测试覆盖失败那一侧；扇出型 Wrapper 还要能表达「部分成功」（配额目录的 `QuotaCatalog.complete` 就是干这个的）。

### Session 生命周期

`inspire-session` 的实际有效期远短于一次完整的批量调用，而 `get_web_session()` **直接返回磁盘缓存、不做校验**（本地 TTL 是 1 小时，但过期后仍会把缓存交出去，让平台自己判定）。因此：

- **CLI 内部已经兜住**：`_request_json_once()` 收到 401 会自动重建 Session 并重试一次。调用 Wrapper 的代码不需要自己处理。
- **外部探针脚本必须自己重试**：连续调用要在 401 / 302 时用 `get_web_session(force_refresh=True)` 重建 Session 再试一次。
- **直接读 `~/.inspire/accounts/<account>/web_session.json` 拿 cookie 是错的**：绕过续期路径，拿到的往往已过期，表现为整批调用统一 401，很容易误判成「v2 不接受我们的 cookie」。
- **探针必须 `allow_redirects=False`。** 认证失败时网关 302 到 Keycloak；跟随重定向会把这个信号变成一张 HTML 登录页，和「路由不存在」无法区分。CLI 传输层没有关闭重定向，它靠「响应非 JSON → `SessionExpiredError`」兜住同一件事。

## 3. 路由名

网关路径用的是**路由名**，不是 discovery 里的 Service `Name`，而且两个带连字符的 Service 行为相反：

| discovery Service `Name` | 网关路由 | 另一种写法 |
| --- | --- | --- |
| `inference-serving` | **`inference_serving`** | 连字符形式 404 |
| `model-hub` | **`model-hub`** | 下划线形式 404 |
| `train` / `hpc` / `ray` / `notebook` / `workspace` / `user` / `project` / `image` / `cluster` | 与 `Name` 相同 | — |
| （不在 discovery 里） | **`file`** | `files` 404 |
| （不在 discovery 里） | **`dataset`** | `datasets`、`dataset-hub`、`dataset_hub`、`data_plaza`、`data`、`plaza` 全部 404 |

**不能从 `Name` 机械推导路由。** 新增 Service 时必须实测两种写法。

`audit`、`billing`、`storage` 三条路由也活着（分别返回 `InvalidAction` 或 `AccessForbidden: user is not system admin`，都不是 404），但没有与当前 CLI 相关的 Action。

## 4. 分页与 total

- **分页参数三种写法等价**：网关同时接受 `PageNumber` / `page_num` / `page`，三者都真实生效。唯一例外是 `train.ListTensorboards`——它**只认 PascalCase 的 `PageNumber`**，`page` 和 `page_num` 被静默忽略并返回空列表。
- **`notebook.ListRunIndex` 无分页**，传 `PageNumber` 直接报错。
- **`page_size: -1`（取全部）逐 Action 不同**：`workspace.ListLogicComputeGroups` 认，`image.ListImages` 认，`workspace.ListNodeDimension` **不认**（只回 10 条，必须按 `total` 显式翻页）。不能类推。
- **省略 `page_size` 可能是灾难**：`ListLogicComputeGroups` 省略时返回空列表却带非零 `total`，读起来就像这个 Workspace 里没有任何计算组。
- **`total` 的类型逐 Action 或 int 或 string**：`notebook.ListNotebooks` 给 int，`hpc.ListJobs` 给 `"202"`。用 `isinstance(total, int)` 判断再回退 `len(items)`，等于把「这一页」当成「全部」——翻页循环第一页就停，`--all` 的展开分支也不再触发。统一走 `core.py` 的 `_coerce_total()`。

## 5. Workspace scoping

**同一个 `workspace_id`，三种放法，逐 Action 固定，不能类推**：

| 放法 | 用它的 Action |
| --- | --- |
| 顶层 `workspace_id` | `train`/`hpc`/`ray` 的 `ListJobs`、`notebook.ListNotebooks`、`inference_serving.ListServings`、`model-hub.ListModels`、`workspace.GetWorkspaceQuota`、`workspace.GetWorkspaceComputeResource`、`workspace.GetLogicComputeGroupResource`、`dataset.ValidateDataset` |
| 嵌套 `filter.workspace_id` | `workspace.ListLogicComputeGroups`、`workspace.ListNodeDimension`、`project.ListProjects`、`file.*` |
| 嵌套 `filter_by`（与 keyword / status / user 同层） | `notebook.ListNotebooks`、`ray.ListJobs`、`inference_serving.ListServings`、`model-hub.ListModels` 的**过滤条件**（workspace 仍在顶层） |
| PascalCase `WorkspaceId` | `user.GetPermissions`、`user.GetRoutes` |

```jsonc
// 错：ListNodeDimension 上被当成集群级请求 -> AccessForbidden
{"workspace_id": "ws-…", "logic_compute_group_id": "lcg-…", "PageNumber": 1}

// 对：工作空间级
{"filter": {"workspace_id": "ws-…", "logic_compute_group_id": "lcg-…"}, "PageNumber": 1}
```

反过来，`GetWorkspaceQuota` / `GetWorkspaceComputeResource` 要的是**顶层** `workspace_id`，这里套 `filter` 反而被拒。

**`AccessForbidden` 有两种含义**：真的没权限，或者忘了 scoping。区分方法是照 discovery 声明的参数结构把 `workspace_id` 放到它声明的位置再试一次。把 scoping 问题当成权限问题，会导致本来可用的 Action 被错误地标成不可用。

### `cluster.*` 与 `workspace.*`

discovery 里 8 个 Action 在两个 Service 下同名且描述几乎一致，但权限边界完全不同。**普通工作空间成员一律用 `workspace.*`**：`ListNodeDimension`、`ListTaskDimension`、`ListProjectDimension`、`ListUserDimension`、`ListLogicComputeGroups`、`GetOverviewResourceMetric` 在 `cluster.*` 下一律 `AccessForbidden`，在 `workspace.*` 下可用。`cluster.*` 的 15 个只读 Action 对非集群管理员全部不可用，正确 scoping 也不解除。

这条**只适用于两边同名的那 8 个**，不能推广成「集群级端点一律换 `workspace.*`」——Metrics 就是反例（见 Action 表的 `GetTaskMetric`）。

`workspace.*` 内部也有权限分层：`ListUserQuotas`、`ListWorkspaceParentProjects`、`GetUserTaskQuota`、`GetWorkspaceTaskQuota`、`GetDefaultUserTaskQuota` 需要工作空间管理员，普通成员返回 `You are not the admin of the <workspace_id>`，因此都没有封装。`GetDefaultUserQuota` 普通成员能读，但它描述的是**新成员的默认值**、在可达 Workspace 上一律不限，对调用者没有信息量。

## 6. discovery 能信什么

`GET {base_url}/discovery` 返回 `{"Result": {"Version": "<etag>", "Services": [...]}}`，每个 Action 带完整的嵌套参数与响应结构。**不带任何认证头**，且匿名与带 Cookie 的响应逐字节相同——它是静态文档，**不按调用者角色过滤**。

当前 `Version = e1daec0f`，11 个 Service、175 个 Action。CLI 用到的 93 个 Action 里有 **12 个不在 discovery 里**：`train.CreateJobConsole`、`hpc.CreateJobConsole`、`inference_serving.CreateServingConsole`、`notebook.ListNotebookCreators`、`user.GetPermissions`、`user.GetRoutes`、`project.ListProjects`、`project.GetProjectDetail`、`project.GetProjectOwners`、`image.CreateImage`、`image.UpdateImage`、`model-hub.CreateModel`；另有 `file`、`dataset` 两条**整条路由**不在 discovery 里，却都活着且正在用。

| 字段 | 可信度 |
| --- | --- |
| Action 的**参数**结构 | 基本可信，是查 scoping 位置和字段拼写的第一手材料 |
| Action 的**响应**结构 | **不可信**。声明的是 `Items` / `TotalCount`，实际网关回的是 `jobs` / `list` / `logic_compute_groups` / `node_dimensions` 加小写 `total`。响应键只能实测 |
| Service `Name` | 不等于网关路由名，见第 3 节 |
| Service 全集 | **不完整**（`file`、`dataset` 缺席） |
| Action 全集 | **不完整**，缺的正好是写操作的 Console 变体和一批 v1 直迁的 Action |
| Action 是否可调 | **完全不代表**。权限只能实测（第 5 节） |
| `Version` | 可信，是内容 etag，可以直接用来判断平台是否改过接口面 |

`Version` 历史上**双向变动过**：早期有 `audit`、`file` 两个 Service 和整套节点运维 Action，之后被移除；`image`、`model-hub` 是后加的；`image` 一度缩到只剩 4 个 Action，`CreateImage` / `UpdateImage` 退回未文档化状态。**不能假设新版本是旧版本的超集。**

**判定「无对应物」的固定错误模式**：只测 discovery 里那个同名 Action 就下结论。`inference_serving` 只测 `CreateServing` 会漏掉 `CreateServingConsole`；`/cluster_nodes/list` 只测 `ListWorkspaceNodes` 会漏掉 `ListNodeDimension`；`/user/quota` 只看 `user` 服务会漏掉 `workspace.*` 下那 10 个配额 Action。**Discovery 只能用来找候选，不能用来否定。**

## 7. 探针方法

下结论前按这个顺序走，每一步都不需要发出真正的写请求。

**① 路由存在性。** `404 page not found` 才是路由不存在；`InvalidAction` 说明路由活着。

**② Action 存在性。** 空 body 打过去：`InvalidAction: unknown action: X` 表示不存在；其它错误码（`InvalidParameter` / `InternalError`，通常带参数校验文案）表示存在但参数不对。空 body 会在校验阶段被拒，不会创建任何东西。迁移写操作前用这一条确认有没有 Console 变体：`train`、`hpc`、`inference_serving` 有，`ray` 没有。

**③ 字段存在性。** 网关先按 proto 解析 body，再校验业务必填项，所以只带一个候选字段打过去：`InvalidParameter: invalid JSON: proto: unknown field "X"` 表示该字段不在合同里，**其它任何报错都表示字段在合同里**（通常是「名称不能为空」之类的必填校验）。因为必填项全缺，这同样创建不出任何东西。这是唯一能测未文档化 Console 变体字段面的办法。
读到 `InternalError: internal server error` 时**先做对照实验**：`hpc.CreateJobConsole` 对空 body 和对合法字段都回这一句，只有塞进一个确定不存在的字段才会回 `unknown field`——没有对照就无法区分「字段被接受、处理器崩了」和「请求根本没进解析」。

**④ 猜名字。** 未文档化 Action 的命名规律是 v1 路径去掉资源前缀后的 PascalCase：`GET /project/{id}` → `GetProjectDetail`，`/file/dir/list` → `GetDirList`，`/project/owners` → `GetProjectOwners`。猜不中就换动词（`Get` / `List` / `Create` / `Update` / `Delete`）和单复数重试，一轮十几个名字就能覆盖。

**⑤ 穷举名字找不到，就去看控制台调什么。** 用带 Session 的浏览器打开对应页面录网络请求，比猜名字强得多：平台前端**全程走 v2**，它调什么就说明什么存在。`/resource_prices/logic_compute_groups/` 的候选、`user.ListSSH`、`user.GetMyPermissions` 都是这么找到的。
想从前端 bundle 反推某个表单的字段形状时，**只取 `/assets/index.*.js` 入口不够**——创建表单在惰性加载的 chunk 里，需要从入口递归抓一遍（当前约 322 个 chunk）。

**⑥ 写语义只能受控验证。** 只读探针可以自由复核；创建、启动、停止、保存、删除的语义**不能从只读流量推导**，也不能只看响应信封——`ray.StartJob` 会返回干净的成功信封而什么都不做。写操作的成功以**状态真的变了**为准。

**⑦ 写进去要读得回来。** 一个字段被 `Create*` 接受，不代表它生效。判定方法是同一次请求里放几个已知会 round-trip 的字段做对照组，创建后用 `Get*` 读回来比对，必要时进容器看实际效果。当前已确认两个「接受、不生效」的死字段，见 Action 表的创建字段合同。

## 8. 仍在使用的 v1 端点

这张表只收**有当前消费者、且实测确认过**的端点。每条都注明它为什么留着——「v2 装不下」和「换过去更贵」是两类，处置方式不同。

| v1 端点 | 消费者 | 为什么不迁 |
| --- | --- | --- |
| `POST /api/v1/resource_prices/logic_compute_groups/` | [`quota_cache.py`](../../cli/inspire/cli/utils/quota_cache.py) → 每一个 `create` 命令和 `<workload> quota` | **可复现，但重建更贵**：2N+1 次调用 vs N 次，且等于在客户端复制一份平台调度端的过滤逻辑。见下文 |
| `GET /api/v1/user/detail` | [`session/auth.py`](../../cli/inspire/platform/web/session/auth.py) 登录握手 | **Session 自举**：这是判定「登录成功了没有」的探针，此时还没有任何 Session 可供 v2 使用 |
| `GET /api/v1/user/routes/default` | 同上，发现可见 Workspace | **Session 自举**：字面量 `default` 是「还不知道自己在哪个 Workspace」的占位；v2 的 `user.GetRoutes` 要一个真实的 `WorkspaceId`，登录时还拿不到 |
| `GET /api/v1/notebook/lab/{notebook_id}/proxy/{port}/` | [`rtunnel.py`](../../cli/inspire/platform/web/browser_api/rtunnel.py)、`notebook proxy-url`、整条 Notebook SSH 链路 | **不是 Action 能表达的东西**：反向代理，不是 JSON 请求/响应。见下文 |

`/train_job/remote_cmd`（`job shell` 的双向 PTY WebSocket）同理属于「v2 装不下」，它在 [`job_shell.py`](../../cli/inspire/cli/utils/job_shell.py) 里构造，是 `test_browser_api_boundary.py` 的两条 `_ALLOWED` 之一（另一条是 `session/auth.py`）。v2 是「POST + `?Action=` + JSON 信封」的网关，装不下流式连接，所以这里不存在「还没迁完」，而是**不该迁**。

### Notebook Proxy

平台自带的一条反向代理路径，把 Notebook **容器内部**监听的某个 HTTP 端口，从 `qz.sii.edu.cn` 这个已登录的域名转出来：

```
{base_url}/api/v1/notebook/lab/{notebook_id}/proxy/{port}/
```

JupyterLab / VS Code 打开之后还有一种带 token 的等价形式 `/{jupyter|vscode}/<notebook>/<token>/proxy/<port>/`，token 段必须当敏感信息处理（`rtunnel.py` 的 `redact_proxy_url` 专门负责在日志与报错里抹掉它）。

两个消费者，第二个是关键：

1. `inspire notebook proxy-url --port N`——**返回**容器里那个 HTTP 服务（TensorBoard、Gradio、Streamlit、推理端点）的外部地址，供调用方直接请求；它不打开任何东西。H100 / H200 受限 Notebook 上默认拒绝，要 `--allow-restricted` 才放行。
2. **整条 SSH 链路都架在它上面。** 受限环境不接受直连，所以 InspireSkill 在容器里起一个 sshd（默认 22222 端口），再通过这条 HTTP 代理去够它——`_wait_for_rtunnel` 轮询的就是这个 proxy URL。`notebook ssh` / `scp` / `ssh-config` 以及外部 OpenSSH 工具能用，全靠这一条。

`notebook` 服务下没有对应 Action：`GetNotebookLab` / `GetLabUrl` / `GetNotebookProxy` / `GetProxyUrl` 均 `InvalidAction`。唯一沾边的 `GetNotebookAccessUrl` 语义不同（它给的是 IDE 网关地址，见 Action 表），**故意不接**。

它与平台用户中心的 SSH 公钥注册表（曾经的 `/ssh/*`）没有任何关系——后者管的是账号级公钥，rtunnel 读的是本机 `~/.ssh/*.pub` 并直接注入容器。

### `resource_prices/logic_compute_groups/`

**它是规格菜单，不是价目表。** 名字里的 `resource_prices` 有误导性——全仓搜 `total_price_per_hour` / `cpu_price` / `gpu_price` 零命中。CLI 只读 `quota_id` 加 `(gpu_count, cpu_count, memory_size_gib)`，外加 serving 读的 `cpu_info.cpu_type` / `gpu_info.gpu_type`。它是把用户敲的 `-q 1,20,200` 翻成平台要的 `quota_id` 的**唯一**来源，挡在每一个 `create` 命令前面。

请求 `{workspace_id, logic_compute_group_id, schedule_config_type}`，`schedule_config_type` 取 `SCHEDULE_CONFIG_TYPE_DSW`（Notebook）/ `_HPC` / `_TRAIN` / `_RAY_JOB`；响应是 `lcg_resource_spec_prices` 列表。走 legacy `{code, data}` 信封，`code != 0` 即失败。

**v2 侧的等价数据要靠客户端重算，规则已经完整复原，三个工作空间 16 个组 16/16 逐组一致**：

1. **`logic_compute_group_ids` 为空 = 对所有组开放。** 漏掉这条会得出灾难性的错误结论。
2. **规格必须装得进组内某个节点**（cpu / memory / gpu 三项都 ≤ 某个 `node_spec`）——要 `workspace.GetLogicComputeGroupNodeSpecs`。
3. **组必须真的有可分配容量**——要 `workspace.GetLogicComputeGroupResource`。这条最不显眼：两个节点硬件完全相同、`support_job_type_list` 也相同的组，其中一个 `gpu_total: 0`，v1 因此对它返回 0 条。**这是运行时状态，不是静态配置。**

规则 2 和 3 都是**按组**的，于是重建一个工作空间的配额目录要 **2N+1 次 v2 调用**，而 v1 是 **N 次**（实测 3 组→7 vs 3、4 组→9 vs 4、9 组→19 vs 9）。代价还不止请求数：迁过去等于在客户端维护一份平台调度端过滤逻辑的副本，平台改一条规则我们收不到任何信号，只会开始默默地把不能用的档位报给用户、或者把能用的藏起来。

**重新评估这个决定的触发条件是明确的**：平台出现一个**工作空间级、已经按组解析好**的配额 Action（届时 2N+1 变 1）。在那之前没有理由动。

## 9. 回落纪律

v1 与 v2 并存期间，**只有「v2 这条路由不通」才回落 v1**：网关 404 `page not found`、405、5xx，或响应非 JSON。

不回落的情况，逐条都有理由：

- **`AccessForbidden` 不回落。** v2 的权限判断更严格且更正确；回落等于用 v1 的宽松答案盖掉 v2 的正确答案。
- **`InvalidParameter` 不回落。** 这是我们自己请求写错了，回落只会让错误一直不被发现。
- **429 绝不回落。** 平台正在要求降速，回落等于把请求量翻倍。限流靠退避重试处理，重试耗尽后让调用方看见。

同一端点的回落告警每进程只提示一次；批量命令会对十几个工作空间循环调同一个端点，逐次提示会刷屏。

**v2 相对 v1 没有可测量的延迟或吞吐优势**：等价端点单请求中位延迟基本持平，个别 v1 更快；并发压测下两者状态码和 wall time 无差异。任何以「v2 更快」为由的改造都缺乏依据——留在 v2 的理由是接口面和长期可用性（`image`、`model-hub` 只在 v2 存在，平台在 v2 上迭代）。

## 10. 输出边界

- **Browser API 的平台原始响应不得直接穿透到公共输出。** 命令层必须先解析、投影和清洗，Human 与 JSON 输出使用显式 Allowlist。
- CLI 对 Agent 的稳定资源身份只有 **Name 和 Alias**；不透明句柄（`ws-`、`project-`、`lcg-`、`quota_id`、`mirror_id`、`notebook_id`、`job_id`）只存在于 `browser_api/` 和 Session 层。
- **唯一的例外是 `notebook proxy-url`**：它走 `format_json(…, preserve_raw={"url"})` 这个显式开关打印完整网关 URL。理由是这个地址的每一段都是平台句柄，默认的 `scrub_raw_ids` 会把它整条洗成 `<redacted>`，洗完就不通了。代价必须说清楚：**这个地址等同于凭据**，内嵌的短期 token 让持有者对该 Notebook 的访问权与你相同，而它会进 Agent 对话记录和 shell 历史。没有免 token 的形式——平台域上的 `/api/v1/notebook/lab/{id}/proxy/{port}/` 返回 `404 page not found`，只有带 token 的网关 URL 真的会去连容器端口（端口没人监听时返回 500 `connect ECONNREFUSED`，`--check` 据此报 `no_service` 而不是 `blocked`）。

## 11. 变更验收

改动 Browser API 至少完成：

1. Wrapper 只暴露调用方需要的最小归一化数据，响应解包走 `_v2_result()`，列表键显式声明。
2. 路由名经过两种写法实测，不是从 discovery `Name` 推导的。
3. Workspace scoping 放在 discovery 声明的位置，并确认返回的不是 scoping 造成的 `AccessForbidden`。
4. 命令使用 Name 输入，同名时提供可读候选与 `--pick`；Human 与 JSON 输出使用显式 Allowlist。
5. 请求失败与「平台返回空」在返回值上可区分，且有测试覆盖失败那一侧；扇出型 Wrapper 还要能表达「部分成功」。
6. 回落条件符合第 9 节，`AccessForbidden`、`InvalidParameter`、429 都不回落。
7. 写操作经过受控验证，且**成功以状态真的变了为准，不以响应信封为准**。
8. 判某个 v1 端点「没有对应物」之前，第 7 节的探针跑完整了。
9. 对应命令 Help、Wrapper 测试和 [`browser-api-actions.md`](browser-api-actions.md) 的表格同步更新。

未闭合的调查结果不进入长期 Reference。
