# Browser API 参考

> **文档类型**：CLI 维护者参考。日常启智操作不要加载本页；Agent 使用公开命令时只依赖 Name-only CLI 合同和对应 `--help`。
>
> **覆盖两个平台**：启智控制台 `qz.sii.edu.cn` 的接口面（第 1–10 章）和数据广场 `aip.sii.edu.cn`（第 11 章）。两者不同 host、不同 API 风格、不同 Session，只共用同一套 CAS SSO，但都由 CLI 的同一层 Web Session 驱动，所以记在一起。
>
> **平台只有 `/api/v2` 一个接口面**：v1 随平台弃用已完全迁移完毕，本页其余部分不再提它。

## 1. 事实源与不变量

Browser API 是启智控制台自己用的接口面：同一台 `qz.sii.edu.cn`、同一个 CAS Session，控制台 SPA 全程走 `/api/v2` 的 Action 网关。官方 CLI `qz` 是这套接口的另一个客户端，不是它的前置依赖。

| 顺位 | 事实源 | 它定义什么 |
| --- | --- | --- |
| 1 | [`browser_api/`](../../cli/inspire/platform/web/browser_api/) | 唯一构造平台请求的地方：实际发什么、读哪个键、怎么归一化 |
| 2 | [`session/`](../../cli/inspire/platform/web/session/)、[`plaza/`](../../cli/inspire/platform/web/plaza/) | 账号隔离、登录、Cookie、代理、传输层重试；数据广场客户端 |
| 3 | `cli/tests/test_browser_api_*.py` | 锁定 Wrapper 合同与输出边界 |
| 4 | 控制台前端实际发的请求 | 判定「平台有没有这个能力」时唯一可靠的来源 |
| 5 | `GET {base_url}/discovery` | 候选 Action 名与参数形状。**是线索，不是合同**（第 6 章） |
| 6 | 当前 Click Help | 公开 CLI 合同 |

| 不变量 | 由谁保证 |
| --- | --- |
| 平台 JSON 请求只在 `browser_api/` 内构造 | `job_shell.py` 的实例 PTY 是 WebSocket；`session/auth.py` 的登录自举发生在 Wrapper 可用之前，两者不属于 JSON Action 请求 |
| 响应解包只有一个入口 | `session/envelope.py` 的 `_v2_result()`，`browser_api/core.py` 再导出 |
| 本页只收有当前消费者、且可复现的事实 | 未闭合的调查与迁移过程记录不进入 |

## 2. 请求契约

```
POST {base_url}/api/v2/{路由}?Action={Action}
Cookie: inspire-session=…
Content-Type: application/json
Referer: {base_url}/{对应控制台页面}

{ …JSON 请求体… }
```

| 契约项 | 规则 |
| --- | --- |
| 方法与形状 | **只有 POST、只有 JSON body**，动作名在 query 里。没有 REST 风格路径参数，没有 GET/DELETE 变体（例外见第 10 章） |
| 认证 | 只需要 `inspire-session` cookie；`x-inspire-client-source` 头在只读面上非必需 |
| `Referer` | **必须与页面域匹配**，由各 Wrapper 按对应控制台页面构造，调用方不要自行拼接 |
| 字段名大小写 | 大小写与下划线**不敏感**：`ImageId` / `image_id` / `Image_Id` 落到同一字段。真正会出事的是**换了一个名字**——`image.UpdateImage` 要裸 `id`，`image_id` 是另一个字段，不会被归一化过去 |
| 路径前缀 | **不可配置**，写死在 `browser_api/` 内 |
| Base URL 与代理 | 来自当前有效配置；Playwright 登录与后续请求复用同一账号的网络设置 |
| 账号隔离 | 所有平台请求使用**目标 Account Alias** 的 SSO Session；跨账号命令不能退回当前活动账号的 Session |

### 响应信封

成功与失败共用一个 AWS 风格信封，**HTTP 状态码在两种情况下都是 200**：

```jsonc
{"ResponseMetadata": {…}, "Result": {…}}                                  // 成功
{"ResponseMetadata": {"Error": {"Code": "…", "Message": "…"}}}            // 业务错误
```

| 规则 | 内容 |
| --- | --- |
| 解包入口 | `_v2_result()`：先看 `ResponseMetadata.Error`，再取 `Result`，取不到给 `{}` |
| 禁止 | **不能用状态码判断成败，也不能自己写信封检查**——手写一个 `code != 0` 会把每一个真错误变成 `API error: None` |
| 列表键 | **逐 Action 不同，没有跨 Action 约定**，解包器必须按 Action 显式声明，见第 8 章「响应」列 |
| 判定顺序 | **429 → 401/302 → 非 JSON → `ResponseMetadata.Error` → `Result`**。429 必须在 Content-Type 嗅探之前判掉：网关限流返回 HTML 错误页，先嗅探会误判成「路由不通」 |

| Code | 含义 | 处置 |
| --- | --- | --- |
| `InvalidAction` | 该 Action 在这条路由上不存在 | 路由是活的，换 Action 名；`404 page not found` 才是路由不存在 |
| `InvalidParameter` | 请求体不符合合同（含 `proto: unknown field "X"`、必填缺失、`page or page_size too large`） | 我们自己写错了，改请求体 |
| `AccessForbidden` | 真的没权限，**或者** scoping 没写全 | 先按第 5 章把 scoping 补全再下结论 |
| `ResourceNotFound` | 对象不存在 | `notebook` / `hpc` / `serving` 的找不到就是它；`train.DeleteJob` 反而给 `AccessForbidden` |
| `Conflict` | 状态不允许该操作（运行中删除、已运行再启动） | 先 `stop` 或先轮询状态 |
| `InternalError` / `Throttling` / `ServiceUnavailable` / `SlowDown` / `RequestTimeout` / `TooManyRequests` | 平台暂时不回答 | 抛 `TransientAPIError`，由传输层退避重试 |

**`InternalError` 有歧义**：既可能是平台真的崩了，也可能是字段被接受后处理器出错。用它判断字段存在性之前必须做对照实验（第 7 章）。它还会吞掉确定性的用户错误——`hpc.GetJobLog` 的超宽时间窗、`hpc.CreateJobConsole` 缺 `priority` 键都走这一条，先白烧三次重试再抛出一条看起来像平台故障的错。这类已知项一律在客户端挡在前面。

### 传输层

[`session/__init__.py`](../../cli/inspire/platform/web/session/__init__.py) 的 `request_json()` 是 requests 与 Playwright 两条路径唯一的收口。

| 机制 | 规则 |
| --- | --- |
| 瞬时错误 | HTTP 408 / 425 / 429 / 500 / 502 / 503 / 504 与信封里的 transient 码统一抛 `TransientAPIError`（`ValueError` 子类） |
| 重试 | 最多 3 次，优先采纳 `Retry-After`（上限 8 秒），否则指数退避加抖动；耗尽后原样抛出 |
| 限流吸收位置 | **必须留在传输层**。Workspace 级问题都是「每个计算组一次请求」的扇出，正是限流器会反应的形状；Wrapper 不要各自重试 |
| `page_size` 上限 | 超过 5000 由 `_clamped_page_size()` 无条件截断。网关**逐 service 执行**这条（`hpc` 强制，`ray` 给 10000 也照收），不能靠某个 service 没报错推断没有上限。`-1` 是「取全部」，原样放过 |
| `train.ListJobs` 的更低上限 | 这是 Action 级例外：`page_size=999` 实测返回 999 行，`1000` 直接答 `InvalidParameter: page or page_size too large`。`jobs.list_jobs` 先按 999 截断，缓存完整刷新也复用这条 Wrapper |
| 请求体 | 不会被就地修改，截断走浅拷贝 |
| 连接 | 复用连接池；Cookie / Header / 代理每次调用重设，刷新过 Session 后不会拿旧凭据作答 |

### 「平台没有回答」不等于「平台回答了没有」

**只有平台成功返回的空结果，才能读作「这个对象没有」。** Wrapper 不得把 `TransientAPIError`、`SessionExpiredError` 或任何失败折叠成 `[]` / `None` / `0` / 「不存在」。事件、配额目录、资源可用量、节点计数尤其危险——用户会据此做调度决策。对应地：请求失败与「平台返回空」必须在返回值上可区分，且要有测试覆盖失败那一侧；扇出型 Wrapper 还要能表达「部分成功」（`QuotaCatalog.complete`）。

### Session 生命周期

`inspire-session` 的实际有效期远短于一次完整的批量调用，而 `get_web_session()` **直接返回磁盘缓存、不做校验**（本地 TTL 1 小时，过期后仍交出缓存，让平台自己判定）。

| 场景 | 做法 |
| --- | --- |
| CLI 内部 | 已兜住：`_request_json_once()` 禁止重定向，收到 401 / 3xx 后先用缓存的 CAS / Keycloak SSO Cookie **无密码续签** Qizhi Session；只有 SSO 也明确失效才进入一次受熔断保护的凭据登录，最后重试原请求一次 |
| 重建预算 | 每次 `request_json()` 只有一次，且**跨 429 重试共享**——上层 Wrapper 不得再包一层认证重试，那是在已经用掉的预算上再加一次登录 |
| 外部探针脚本 | 必须自己重试：401 / 302 时回到 `request_json()` / 正常 Wrapper 的续期路径；直接调用 `get_web_session(force_refresh=True)` 会跳过无密码 SSO 续签并进入凭据登录，只留给明确需要重新登录的账号管理流程 |
| 直接读 `web_session.json` 拿 cookie | **错的**：绕过续期路径，拿到的往往已过期，表现为整批调用统一 401，很容易误判成「网关不接受我们的 cookie」 |
| 探针的重定向 | 必须 `allow_redirects=False`。认证失败时网关 302 到 Keycloak；跟随重定向会把这个信号变成一张 HTML 登录页，和「路由不存在」无法区分 |

### 提交凭据只有一个入口

CAS 在连续若干次登录失败后会锁账号，也会对来源机器加验证码——**实测过一次**：同一台机器上几次失败登录（用的还是不存在的用户名）之后，CAS 给密码表单 `fm1` 加了 `authcode` 字段、页面同时出现 `/cas/captcha.jpg`，正确的密码也登不进去；过一段时间字段自己消失，登录恢复正常。所以「一次会话过期最多换来一次凭据提交」不是保守，是这层的硬约束。

那次验证码还暴露了一个更糟的表现：字段留空提交，CAS 回的是 `账号或密码错误。`——**密码没错的账号被告知密码可能不对**，而每次重试都是一次注定失败的提交。所以解析完表单就要检查 `authcode` / `captcha` / `smscode` 这类字段，在提交**之前**停下并说清楚平台在要验证码。

`login_with_playwright()` 是全 CLI 唯一把密码放上网线的地方，闸门就设在那里（`session/login_guard.py`），不设在调用方——调用方各自限制自己的重试次数，仍然是在别人已经花掉的预算上再加一次。

| 项 | 事实 | 读错的后果 |
| --- | --- | --- |
| `AuthenticationError` | 「凭据已经出去了，且没通过」。凭据一旦 POST 出去，**任何**后续失败（4xx、5xx、连接断开、响应丢失）都是它 | 当成普通 `ValueError` 就会回落 Playwright，把一次提交变成两次 |
| 回落边界 | requests/CAS 路径**只在提交之前**可以回落到 Playwright：取登录页、解析表单、拿 RSA 公钥失败都还没提交 | 把提交之后的失败也当作「换条路再试」，就是第二次提交 |
| 验证码 | 表单里出现 `authcode` / `captcha` / `smscode` 时**提交之前**就停，不提交也不回落浏览器（浏览器同样答不出），且不打开熔断——没提交就没有被拒的凭据 | 留空照submit，拿回 `账号或密码错误。`，把「平台在要验证码」报成「你的密码不对」 |
| 熔断标记 | `web_session.login-block.json`，按 Account 存，记 `failures` / `blocked_until` / 凭据指纹（PBKDF2，不含明文），不含平台响应 | 以为它只在本进程有效——它的意义正是让并发进程读到同一个事实 |
| 冷却档位 | 60s → 5min → 15min → 30min（封顶），按**连续**失败次数升级；1 小时没人再试就清零重来 | 以为等一分钟总能再试一次——连续失败会越等越久，这正是防锁的部分 |
| 解除方式 | 冷却到期、**改凭据**（指纹变了立刻放行）、或有更新的 Session 落盘 | 以为只能干等；用户改完密码应当立刻能登 |
| 无密码续签 | 熔断只禁止重复提交凭据，不禁止拿已有 CAS / Keycloak Cookie 换新 Qizhi Session；续签成功会落盘更新代际，旧标记随即变成 stale | 把熔断误当成所有认证流量都不能走，逼用户复制出一个新 Account Alias |
| 登录成功 | 清掉标记，不计次 | 以为成功也受限——成功的提交不会触发 CAS 锁定，不该限流 |
| 本地缓存写失败 | 登录仍然算成功，Session 照常返回，只是这次没落盘 | 当成登录失败就会把平台刚认过的凭据丢掉，再登一次 |

## 3. 路由名

网关路径用的是**路由名**，不是 discovery 里的 Service `Name`，两个带连字符的 Service 行为相反。

| discovery Service `Name` | 网关路由 | 另一种写法 |
| --- | --- | --- |
| `inference-serving` | **`inference_serving`** | 连字符形式 404 |
| `model-hub` | **`model-hub`** | 下划线形式 404 |
| `train` / `hpc` / `ray` / `notebook` / `workspace` / `user` / `project` / `image` / `cluster` | 与 `Name` 相同 | — |
| （不在 discovery 里） | **`file`** | `files` 404 |
| （不在 discovery 里） | **`dataset`** | `datasets`、`dataset-hub`、`dataset_hub`、`data_plaza`、`data`、`plaza` 全部 404 |

**不能从 `Name` 机械推导路由**，新增 Service 必须实测两种写法。`audit`、`billing`、`storage` 三条路由也活着（返回 `InvalidAction` 或 `user is not system admin`，都不是 404），但没有与当前 CLI 相关的 Action。

## 4. 分页与 total

| 现象 | 事实 | 读错的后果 |
| --- | --- | --- |
| 分页参数拼法 | `PageNumber` / `page_num` / `page` 三者等价且都真实生效 | — |
| 唯一例外 | `train.ListTensorboards` **只认 PascalCase `PageNumber`** | `page` / `page_num` 被静默忽略并返回空列表 |
| `page_size: -1`（取全部） | 逐 Action 不同：`workspace.ListLogicComputeGroups`、`image.ListImages`、`project.ListProjects` 认；`workspace.ListNodeDimension` 与维度族只回 10 条；`model-hub.ListModels` 直接拒 `page or page_size invalid` | 按「-1 就是全部」类推会静默丢数据 |
| 省略 `page_size` | `ListLogicComputeGroups`、`hpc.ListSlurmdPodEvent` 返回**空列表配非零 `total`** | 读起来像「这个工作空间没有计算组」 |
| `total` 类型 | 逐 Action 或 int 或 string（`notebook.ListNotebooks` 给 int，`hpc.ListJobs` 给 `"202"`） | 用 `isinstance(total, int)` 判断再回退 `len(items)`，等于把「这一页」当「全部」，翻页循环第一页就停。统一走 `_coerce_total()` |
| 无分页 Action | `notebook.ListRunIndex` 传 `PageNumber` 直接报错 | — |
| 未知字段是否报错 | 一般报 `unknown field`，但 `train.CreateTensorboard` 与 `notebook.SaveMirror` 的 unmarshaller **静默丢弃** | 「逐字段试，看哪个不被拒」在它们身上全通过。对写侧 Action 用这个方法前，先发一个确定不存在的键确认它真会拒 |

## 5. Workspace scoping

**同一个 `workspace_id`，四种放法，逐 Action 固定，不能类推**：

| 放法 | 用它的 Action |
| --- | --- |
| 顶层 `workspace_id` | `train`/`hpc`/`ray` 的 `ListJobs`、`notebook.ListNotebooks`、`inference_serving.ListServings`、`model-hub.ListModels`、`workspace.GetWorkspaceQuota`、`workspace.GetWorkspaceComputeResource`、`workspace.GetLogicComputeGroupResource`、`workspace.Get*NodeSpecs`、`dataset.ValidateDataset` |
| 嵌套 `filter.workspace_id` | `workspace.ListNodeDimension` / `ListTaskDimension` / `ListUserDimension`、`workspace.ListLogicComputeGroups`、`project.ListProjects`、`file.*` |
| 嵌套 `filter_by`（与 keyword / status / user 同层） | `notebook.ListNotebooks`、`ray.ListJobs`、`inference_serving.ListServings`、`model-hub.ListModels` 的**过滤条件**（workspace 仍在顶层） |
| PascalCase `WorkspaceId` | `user.GetPermissions`、`user.GetRoutes`、`notebook.GetScheduleConfig` |

```jsonc
// 错：ListNodeDimension 上被当成集群级请求 -> AccessForbidden
{"workspace_id": "ws-…", "logic_compute_group_id": "lcg-…", "PageNumber": 1}

// 对：工作空间级
{"filter": {"workspace_id": "ws-…", "logic_compute_group_id": "lcg-…"}, "PageNumber": 1}
```

**`AccessForbidden` 有两种含义**：真的没权限，或者忘了 scoping。区分方法是照 discovery 声明的参数结构把 `workspace_id` 放到它声明的位置再试一次。把 scoping 问题当成权限问题，会导致本来可用的 Action 被错误地标成不可用。

### `cluster.*` 与 `workspace.*`

| 项 | 结论 |
| --- | --- |
| 同名的 8 个 Action | `ListNodeDimension`、`ListTaskDimension`、`ListProjectDimension`、`ListUserDimension`、`ListLogicComputeGroups`、`GetOverviewResourceMetric` 等在 `cluster.*` 下对非集群管理员一律 `AccessForbidden`，在 `workspace.*` 下可用。**普通成员一律用 `workspace.*`** |
| 适用范围 | **只适用于两边同名的那 8 个**，不能推广成「集群级端点一律换 `workspace.*`」——Metrics 是反例（第 8.14 节） |
| 唯一该用 `cluster.*` 的 | `cluster.ListNodeEvents`：没有 `workspace.*` 对应物，且对普通成员可读，是平台上唯一按节点而不是按工作负载组织的事件源 |
| `workspace.*` 内部分层 | `GetScheduleConfig`、`ListUserQuotas`、`ListWorkspaceParentProjects`、`GetUserTaskQuota`、`GetWorkspaceTaskQuota`、`GetDefaultUserTaskQuota` 需要工作空间管理员，均未封装。**`workspace.GetScheduleConfig` 不是各 Workload 路由下 `Get*ScheduleConfig` 的汇总入口**，只是重名 |
| `GetDefaultUserQuota` | 普通成员能读，但描述的是**新成员默认值**、在可达 Workspace 上一律不限，无信息量 |

## 6. 接口面清单：discovery 与前端产物

| 来源 | 覆盖 | 能证明什么 |
| --- | --- | --- |
| `GET {base_url}/discovery` | 11 个 Service、175 个 Action（`Version = e1daec0f`） | 候选 Action 名与参数形状。**不带认证头，匿名与带 Cookie 逐字节相同**——静态文档，不按角色过滤 |
| 控制台前端产物 | 25 条路由、187 个 Action，另加 21 条 REST 路径 | **下界**：能证明某个 Action 存在，不能证明不存在 |
| `user.GetMyPermissions` | 有 RBAC 网关的 14 个服务（不含 `train` / `notebook` / `workspace` / `project` / `ray` / `hpc`） | 空请求体，回 `{Services: {服务: {Read, Write, Actions: {Action: bool}}}}`。覆盖范围内权威，且列出了连前端产物都没有的 Action——`job.ListNodeJobs`、`job.GetLcgUsedComputeResourceJobs`、`job.GetProjectQuotaJobs` / `GetUserQuotaJobs` 就是这么找到的。判「我能不能调」先问它，比探针便宜 |
| 探针 | 逐个 | 唯一能否定的手段（第 7 章） |

### discovery 逐字段的可信度

| 字段 | 可信度 |
| --- | --- |
| Action 的**参数**结构 | 基本可信，是查 scoping 位置和字段拼写的第一手材料 |
| 响应的**顶层列表键与 total** | **不可信**。声明 `Items` / `TotalCount`，实际是 `jobs` / `list` / `logs` / `events` / `scale_history_items` / `logic_compute_groups` / `node_dimensions` 加小写 `total` |
| 响应**元素内部的属性名** | 基本可信。错的只有外面那层信封，里面的形状可以照着写，再用一次真实响应确认 |
| Service `Name` | 不等于网关路由名（第 3 章） |
| Service 全集 / Action 全集 | **不完整**：缺 `file`、`dataset`，缺写操作的 Console 变体 |
| Action 是否可调 | **完全不代表**，权限只能实测 |
| `Version` | 可信，是内容 etag，可直接用来判断平台是否改过接口面。**历史上双向变动过**，不能假设新版本是旧版本的超集 |

### 不在 discovery 里、但活着且正在用

| 类型 | 清单 |
| --- | --- |
| 整条路由（`‡`） | `resource-price`、`file`、`dataset`——其中 `resource-price` 挡在每一个 `create` 前面 |
| 单个 Action（`†`，18 个） | `train.CreateJobConsole`、`hpc.CreateJobConsole`、`inference_serving.CreateServingConsole`、`notebook.ListNotebookCreators`、`notebook.CheckNotebook`、`user.GetPermissions`、`user.GetRoutes`、`project.ListProjects`、`project.GetProjectDetail`、`project.GetProjectOwners`、`image.CreateImage`、`image.UpdateImage`、`model-hub.CreateModel`、`model-hub.DeleteModel`、`train.CreateTensorboard` / `StartTensorboard` / `StopTensorboard` / `DeleteTensorboard` |
| 前端产物里有、discovery 没有的 15 条路由 | `audit`(6)、`billing`(3)、`file`(9)、`image_plaza`(7)、`inference_serving`(3)、`job`(1)、`model_plaza`(5)、`operate-log`(1)、`resource-price`(6)、`sandbox`(4)、`sandbox-api-key`(3)、`sandbox-pool`(1)、`sandbox-template`(6)、`serving`(1)、`storage`(10)。**`inference_serving` 是假阳性**——discovery 拼成 `inference-serving`，对账脚本按字符串比不会替你看穿 |
| 探活结果 | 102 个只读 Action 逐个探，`InvalidAction` 为 0。普通成员：`storage` / `operate-log` 整个服务是 `user is not system admin`，`billing` 三个 Action 读超时或 `InternalError`，`image_plaza.ListImages` 答 `total_count: 0`（目录对本账号是空的，不是没权限），其余可读 |

**判定「无对应物」的固定错误模式**：只测 discovery 里那个同名 Action 就下结论。`inference_serving` 只测 `CreateServing` 会漏掉 `CreateServingConsole`；只测 `ListWorkspaceNodes` 会漏掉 `ListNodeDimension`；只看 `user` 服务会漏掉 `workspace.*` 下那 10 个配额 Action。配额目录整条 `resource-price` 路由不在 discovery 里，据此判过一次「没有对应物」，是错的。**discovery 只能用来找候选，不能用来否定。**

### 扫描器：`scripts/scan_v2_surface.py`

抓 `{base_url}/` 的入口 chunk，按 `"./xxx.js"` 递归拉全部产物（当前 322 个 chunk / 18.9 MB），提取写死的 `/api/v2/{route}?Action={Action}` 与 REST 路径，再和 discovery 对账；`--probe` 按第 7 章的判据逐个探活。

| 已知的取样陷阱 | 处理 |
| --- | --- |
| Action 名正则写 `[A-Za-z]+` | 会把 `GetProjectListV2` 截成 `GetProjectListV`，看起来像一个不存在的 Action。必须写 `[A-Za-z0-9]+` |
| REST 路径锚在收尾双引号上 | 模板字符串拼的地址（`` `${base}/api/v2/notebook/lab/${id}/` ``）一条都看不见——21 条里漏 9 条。现在把插值段当一个路径段匹配并归一成 `{}` |
| 只取 `/assets/index.*.js` 入口 | 创建表单在惰性加载的 chunk 里，必须从入口递归抓 |

**每一种取样方法都有盲区，先说清盲区再下「不存在」的结论。**

## 7. 探针方法

下结论前按顺序走，每一步都不需要发出真正的写请求。

| 步骤 | 打法 | 判据 |
| --- | --- | --- |
| ① 路由存在性 | 任意 Action 打过去 | `404 page not found` 才是路由不存在；`InvalidAction` 说明路由活着 |
| ② Action 存在性 | 空 body | `InvalidAction: unknown action: X` = 不存在；其它错误码 = 存在但参数不对。空 body 在校验阶段被拒，创建不出东西 |
| ③ 字段存在性 | 只带一个候选字段 | `InvalidParameter: proto: unknown field "X"` = 不在合同里；**其它任何报错都表示在合同里**（通常是必填校验） |
| ④ 猜名字 | 未文档化 Action 的名字是资源路径去掉前缀后的 PascalCase | `/project/{id}` → `GetProjectDetail`、`/file/dir/list` → `GetDirList`、`/project/owners` → `GetProjectOwners`。猜不中换动词（`Get` / `List` / `Create` / `Update` / `Delete`）和单复数，一轮十几个名字覆盖 |
| ⑤ 看控制台调什么 | 带 Session 的浏览器录网络请求，或扫全量 bundle | 平台前端**全程走 v2**，它调什么就说明什么存在。`resource-price.GetLogicComputeGroupResourceSpecPrices`、`user.ListSSH`、`user.GetMyPermissions` 都是这么找到的 |
| ⑥ 写语义 | **只能受控验证** | 不能从只读流量推导，也不能只看响应信封——`ray.StartJob` 会返回干净的成功信封。**成功以状态真的变了为准** |
| ⑦ 写进去要读得回来 | 同一请求里放几个已知会 round-trip 的字段做对照组，创建后 `Get*` 读回来比对 | 一个字段被 `Create*` 接受不代表它生效。必要时进容器看实际效果 |

**③ 有两个已知的失效条件：**

`InternalError: internal server error` 必须先做对照实验——`hpc.CreateJobConsole` 对空 body 和对合法字段都回这一句，只有塞进一个确定不存在的字段才会回 `unknown field`。没有对照就无法区分「字段被接受、处理器崩了」和「请求根本没进解析」。

**一个解析不到对象的资源 id 会静默废掉这把尺子。** 网关的鉴权中间件在严格 proto 解析**之前**先读一遍资源键：读不到对象就直接返回，body 里其它字段根本没进解析。

| body | 回答 | 说明 |
| --- | --- | --- |
| `{"nonexistent_field_xyz": "x"}` | `unknown field "nonexistent_field_xyz"` | 不带 id，尺子正常 |
| `{"notebook_id": <真实>, "nonexistent_field_xyz": "x"}` | `unknown field "nonexistent_field_xyz"` | **真实 id，尺子照常工作** |
| `{"notebook_id": "nb-does-not-exist", "nonexistent_field_xyz": "x"}` | `ResourceNotFound: notebook not found` | **假 id，未知字段被掩盖** |
| `{"ray_job_id": "x", "nonexistent_field_xyz": "x"}` | `ResourceNotFound: ray job not found` | 同上，另一条路由 |

第三、四行会**主动骗人**：`{"ray_job_id": "x"}` 打给 `ray.GetJobLog` 答 `ResourceNotFound`，看起来 `ray_job_id` 在合同里——其实不在，换成真实 id 或整个去掉就会看到 `unknown field`。所以：**要么不带资源 id，要么带一个真实拥有的对象的 id。**

## 8. Action 参考表

每条都是 `POST {base_url}/api/v2/{路由}?Action={Action}`，响应取信封里的 `Result`。「请求体」列写的是 **CLI 实际发出的键**；「响应」列写的是**实测的线上键**。`†` = 该 Action 不在 discovery 里，`‡` = 整条路由不在 discovery 里。

| 路由 | 域 | Action 数 | 主要 CLI 命令组 |
| --- | --- | --- | --- |
| [`train`](#81-train--分布式训练与-tensorboard) | GPU 训练任务、TensorBoard | 16 | `job`、`tensorboard` |
| [`hpc`](#82-hpc--cpu-slurm-批处理) | CPU Slurm 批处理 | 11 | `hpc` |
| [`ray`](#83-ray--弹性计算) | 弹性计算 | 12 | `ray` |
| [`notebook`](#84-notebook--交互式建模) | 交互式建模 | 18 | `notebook`、`image` |
| [`inference_serving`](#85-inference_serving--模型部署) | 模型部署 | 19 | `serving` |
| [`workspace`](#86-workspace--工作空间资源) | 计算组、节点、配额、用量 | 9 | `resources`、`<workload> quota`、每个 `create` |
| [`user`](#87-user--账号) | 账号身份与权限 | 3 | `account permissions`、所有按当前用户过滤的列表 |
| [`project`](#88-project--项目) | 项目 | 4 | `project`、每个 `create` |
| [`image`](#89-image--镜像) | 镜像 | 5 | `image` |
| [`model-hub`](#810-model-hub--模型仓库) | 模型仓库 | 14 | `model`、`serving create` |
| [`resource-price`](#811-resource-price--配额目录-) ‡ | 按计算组解析好的配额目录 | 1 | `<workload> quota`、每个 `create` |
| [`file`](#812-file--文件页-) ‡ | 存储池与目录发现 | 2 | `init --scope project` |
| [`dataset`](#813-dataset--官方数据集挂载-) ‡ | 官方数据集挂载 | 1 | `dataset validate`、`--dataset` |

| 状态 | 清单 |
| --- | --- |
| 有 Wrapper、暂无 CLI 消费者 | `notebook.ListNotebookLifecycles`、`notebook.ListNotebookCreators`、`ray.ListJobCreators`、`inference_serving.GetInferenceServingTerms`、`model-hub.ListModelVersionOptions`、`model-hub.ListModelCreators`、`model-hub.GetModelPublishPrefill`、`model-hub.GetModelPublishStatus`、`project.GetProjectForPage`（表中 CLI 列写「—」） |
| 查过、刻意不封装 | 见各节末尾的「不接」表 |

---

### 8.1 `train` — 分布式训练与 TensorBoard

Referer：`/jobs/distributedTraining`，详情页 `/jobs/distributedTrainingDetail/{job_id}`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateJobConsole` † | 见[创建面字段合同](#9-创建面的字段合同) | `{job_id, sub_code, sub_msg}` | `job create`、`job batch` |
| `GetTrainScheduleConfig` | `{workspace_id}` | `train_enable_specified_nodes` | `job create --specified-node`、Job Batch 的 `specified_nodes` |
| `GetJob` | `{job_id}` | `name` / `status` / `command` / `framework_config[]` / `dataset_info[]` / `node_infos[]` / `specified_nodes[]` / `exclude_nodes[]` / `project_id` / `workspace_id` / `logic_compute_group_name` / `created_at` / `finished_at` | `job status` / `command` / `logs` / `metrics` / `wait` |
| `ListJobs` | `{workspace_id, page_num, page_size, created_by, status?, keyword?}`；或 `{workspace_id, job_ids[]}` 按 id 批量取，见 [8.15](#815-批量读--listjobs--listjobevents-的复数形态) | `{jobs[], total}`（`total` 是 int） | `job list`、`job status`、Name Resolver、`cache refresh` |
| `ListJobInstances` | `{job_id, page_num, page_size}` | `{items[], total}` | `job instances` / `shell` / `logs` / `events` |
| `ListJobEvents` | `{PageNumber\|page_num, page_size, filter:{object_type, object_ids[]}}`，`object_ids` 可多个，见 [8.15](#815-批量读--listjobs--listjobevents-的复数形态) | `{events[], total}` | `job events` |
| `GetJobLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}` | `job logs` |
| `StopJob` | `{job_id}` | `{code, message}` | `job stop` |
| `DeleteJob` | `{job_id}` | — | `job delete` |
| `GetTaskMetric` | 见 [8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `job metrics` |
| `ListTensorboards` | `{workspace_id, created_by, PageNumber, page_size, status?, keyword?}` | `{items[], total}` | `tensorboard list`、Name Resolver |
| `GetTensorboard` | `{tb_id}` | `name` / `status` / `tb_summary_path` / `url` / `job_id` / `job_name` / `auto_stop_time_ms` / `running_time_ms` / `project_name` / `logic_compute_group_name` | `tensorboard status` / `tags` / `scalars`、启停轮询 |
| `CreateTensorboard` † | `{name, workspace_id, project_id, logic_compute_group_id, tb_summary_path, auto_stop_time_ms, job_id?}` | **恒为 `{}`** | `tensorboard create` |
| `StartTensorboard` † | `{tb_id}` | — | `tensorboard start` |
| `StopTensorboard` † | `{tb_id}` | — | `tensorboard stop` |
| `DeleteTensorboard` † | `{tb_id}` | — | `tensorboard delete` |

**Job 要点**

| 项 | 事实 | 读错的后果 |
| --- | --- | --- |
| `ListJobEvents` 的两级 | 一个 Action 管两种事件，靠 `filter.object_type` 区分：`"job"` 给控制器级（`SetPodTemplateSchedulerName`、`Unschedulable`），`"instance"` 给 pod 级（`FailedScheduling` / `Scheduled` / `Pulling` / `Started`，更丰富）。`object_ids` 在后者是 pod 名列表，**按 200 个一批**分块 | 只读一级会漏掉真正说明调度失败的那一半 |
| `ListJobInstanceEvents` | 同名易混的另一个 Action（`job_id` + `instance_name`），无论返回多少条 `total` 恒为 `"0"` | 需要分页的调用方不要用，`browser_api` 未封装 |
| `GetJobLog` 时间戳 | 两个字段是**字符串**型 epoch 毫秒；后端拒绝任何宽于一个月的窗口；不接受 sorter | 传 int 被拒 |
| `DeleteJob` | 要求先停止，运行中答 `Conflict`。**找不到资源时答 `AccessForbidden`**，不是 `ResourceNotFound`（与 `hpc.DeleteJob` 相反） | 按 `ResourceNotFound` 判「已删除」会漏判 |
| 规格来源 | `JobInfo` 从 `framework_config[0]` 读 `gpu_count` / `cpu` / `mem_gi` / `shm_gi` / `instance_count`；GPU 型号在 `instance_spec_price_info.gpu_info.gpu_type_display` | — |
| 节点归属三个字段 | `node_infos[]` 是**实际落点**（`{node_name}`，被调度后才有值，停止即清空）；`specified_nodes[]` / `exclude_nodes[]` 是**创建时的请求侧**（裸字符串数组）。`node_count` 是请求的节点数，与 `framework_config[0].instance_count` 同值 | `node_count` **不是** `node_infos` 的长度，排队中的任务两者不等。多节点任务把 rank 和节点对上只能靠 `ListJobInstances` 行的 `node` |
| 跨 Workspace 列表 | `ListJobs` 本身只收一个 `workspace_id`，所以 `job list --workspace all` 必须扇出；CLI 按页以最多 8 个 Workspace 并发 round-robin，逐 Workspace 保留 `total` / page 状态，再合并排序和应用全局输出 limit | 串行 16 个慢 Workspace 会直接相加；一次把所有页全并发又会冲击限流器，所以并发单位是“当前每个 Workspace 的下一页” |

**TensorBoard 要点**

| 项 | 事实 | 后果 |
| --- | --- | --- |
| 它是这条路由上的第二个对象 | 共 7 个 Action，discovery 只有读侧三个，写侧四个靠探针发现。没有 `CreateTensorboardConsole` 变体，也没有 `Update` / `Restart` / `Check` / `GetTensorboardLog` / `ListTensorboardEvents` / `ListTensorboardInstances` / `GetTensorboardAccessUrl` / `GetTensorboardScheduleConfig`——逐个探过全是 `InvalidAction` | — |
| `CreateTensorboard` 的字段名与报错名不一致 | 缺字段时报 `tensorboard_auto_stop_time_ms is required`，线上字段却是 **`auto_stop_time_ms`**，而且是**字符串**（传 int 报 `invalid value for string field autoStopTimeMs`）。上限 72 小时 | 照报错原样发字段名会被当未知字段丢掉，再报同一句 required，死循环 |
| 它的 unmarshaller 忽略未知字段 | 不能靠 `unknown field` 反推契约，只能照 `GetTensorboard` 的响应字段拼 | — |
| 它会建出没法用的 board | `name` 可省（建出来没名字，Name-only CLI 再也指不到）；`tb_summary_path` 可省（board 什么都读不到）。`workspace_id` 缺报 `ResourceNotFound: record not found`，`project_id` 缺报 `InternalError`，`logic_compute_group_id` 缺报 `ResourceNotFound: 已选择的计算类型组不存在`。真正可选的只有 `job_id` | Wrapper 把 `name` 和 `tb_summary_path` 也挡成必填 |
| 不返回 id | `Result` 恒为 `{}`，新建的 board 只能回头用 `ListTensorboards` 按名字找 | — |
| 规格与优先级不可选 | 平台固定 1 CPU / 2 GiB；`priority_name` 被静默忽略，`task_priority` 是 int32，新建一律落 `NORMAL` / `4` | — |
| `GetTensorboard` 的未知 id | 答 `InvalidParameter: 用户不存在。`——一条谈用户的消息，实际含义是这个 board 不归你或不存在 | **不要读成账号问题** |
| 删除顺序 | `DeleteTensorboard` 要求先停止（运行中答 `Conflict`）；`StopTensorboard` 对已停止的 board 幂等成功。状态取值 `tb_status_creating` / `_running` / `_stopped`，CLI 去掉前缀 | — |
| `ListTensorboards` 两处不可猜 | 分页只认 PascalCase `PageNumber`；**不带 `created_by` 时 `total` 数的是整个 Workspace，`items` 却只有你读得到的几行**（实测 `total=1626` 配 1 行） | 两个数对不上。`status` 只收单个裸值，传数组报错；`keyword` 按名字子串真实生效；**`job_id` 被受理但滤不出东西**，按任务收窄只能在客户端做 |
| 行里的 `url` 是一个真能打的 TensorBoard 应用 | 同一个 `inspire-session` cookie 直接认，`data/runs`、`data/plugin/scalars/tags`、`data/plugin/scalars/scalars?run=&tag=` 都返回 JSON（scalars 是 `[[wall_time, step, value], …]`）。两种形状：绝对地址 `https://notebook-inspire.sii.edu.cn/tensorboard/{tb_id}/`（今天新建的一律给这种）与站内路径（历史行），Wrapper 按 `startswith("/")` 补 base | **这是「Agent 能不能自己看训练曲线」的全部答案**，所以 `url` 不投影给用户，改由 `tensorboard tags` / `scalars` 代读 |
| 计算组的 `tensorboard` 作业类型 | `support_job_type_list` 里的取值之一，逐组不同——`分布式训练空间` 里就有训练组没声明它 | 建 board 前要按这个过滤计算组 |

**查过、刻意不接**

| Action | 理由 |
| --- | --- |
| `ListTensorboardUsers` | `{workspace_id}` → 整个 Workspace 建过 board 的 158 个人，答的是控制台「创建人」下拉框，对只看自己资源的 CLI 没有消费者 |
| `ListPreCheckItems` / `GetPreCheckResult` | 不是「提交前校验规格」，是**每个训练任务创建时可选开启**的节点健康检查（创建面收 `enable_troubleshoot` 加 `pre_check_items`；没开的任务答 `train job does not enable troubleshoot precheck`）。而 `train_enable_troubleshoot` 在全部 10 个可见 Workspace 都是 `False`，接出来是一个谁也用不了、也验不了的开关 |

`notebook.GetScheduleConfig` 里还有三个同族能力位：`train_enable_slow_detect`（`分布式训练空间` / `CI-情境智能` 开）、`train_enable_specified_nodes`（两个 `CI-情境智能*` 开，是真正的节点绑定，与 `--exclude-node` 不是一回事）、`train_enable_vccl`（`分布式训练空间` 开）。节点绑定已经接入 `job create --specified-node` 和 Job Batch 的 `specified_nodes`；创建前用 `train.GetTrainScheduleConfig` 读取同名能力位，关闭时不发创建请求。其余能力接入前仍要同时核对开关和控制台是否真正渲染控件。

---

### 8.2 `hpc` — CPU Slurm 批处理

Referer：`/jobs/highPerformanceComputing`，详情页 `/jobs/hpcDetail/{job_id}`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateJobConsole` † | 见[创建面字段合同](#9-创建面的字段合同) | `{job_id, sub_code, sub_msg}` | `hpc create`、`hpc batch` |
| `GetJob` | `{job_id}` | `{job_name, status, sbatch_script{}, slurm_cluster_spec{}, nodes[], steps, description, ttl_after_job_finish_seconds, dataset_info[], project_id, workspace_id, …}` | `hpc status`、`hpc metrics` |
| `ListJobs` | `{workspace_id, page_num, page_size, created_by, status?}`；或 `{workspace_id, job_ids[]}` 按 id 批量取，见 [8.15](#815-批量读--listjobs--listjobevents-的复数形态) | `{jobs[]\|items[], total}`（`total` 是**字符串**） | `hpc list`、`hpc status`、Name Resolver |
| `ListJobEvents` | `{pageNum: -1, pageSize: 200, filter:{object_ids:[job_id], object_type:"HPC_JOB"}, sorter:[{field:"last_timestamp", sort:"ascend"}]}`，`object_ids` 可多个，见 [8.15](#815-批量读--listjobs--listjobevents-的复数形态) | `{events[]\|items[]\|list[]}` | `hpc events` |
| `ListJobInstances` | `{jobId, page_num, page_size}` | `{items[]\|list[], total}` | `hpc instances` |
| `ListSlurmdPodEvent` | `{instance_id, page_size, PageNumber}` | `{events[], total}`（字符串） | `hpc events --instance` |
| `GetJobLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}`（**int**） | `hpc logs` |
| `GetHpcScheduleConfig` | `{workspace_id}` | `{enable_auto_stop, auto_stop_ruleset, enable_max_running_time, max_running_time_days/hours/minutes, predef_node_spec}` **或字面量 `null`** | `resources policy` |
| `StopJob` | `{job_id}` | `{job_id, sub_code, sub_msg}` | `hpc stop` |
| `DeleteJob` | `{job_id}` | `{job_id, sub_code, sub_msg}` | `hpc delete` |
| `GetTaskMetric` | 见 [8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `hpc metrics` |

| 项 | 事实 | 后果 |
| --- | --- | --- |
| 键名不一致 | `ListJobInstances` 的 id 键是驼峰 **`jobId`**；`ListJobEvents` 的分页键也是驼峰 `pageNum` / `pageSize` | 与同路由其它 Action 的 `job_id` / `page_num` 不一致，不能类推 |
| `GetJob.steps` | 「程序到底跑没跑」的唯一信号，形如 `已完成/总数`：创建后 `-/-`，平台**静态解析 entrypoint** 后变 `-/N`（N = 正文里 `srun` 的个数，没有就是 `-/0`），跑完 `N/N` | `SUCCEEDED` + `steps=0/0`（正文没 `srun`）与 `SUCCEEDED` + `steps=1/1` 在其它字段上完全一样。**报 HPC 状态必须把 `steps` 一起报** |
| `GetJob.nodes` | Slurm 集群的实际落点，停止的任务恒为 `[]` | **空数组要读作「没在跑」而不是「读不到」**。带数据的形状未在活任务上复核，pod 级落点用 `ListJobInstances.node`（裸字符串，如 `hpc-compute003`，另有 `component` ∈ `slurmctld` / `slurmd`） |
| 实例名必须带命名空间 | `GetJobLog` 与 `ListSlurmdPodEvent` 都要 `<ns>/<pod>` | 日志端裸名报 `InvalidParameter: … expect 1, but got 0`；事件端裸名和 `job_id` 都**静默回空** |
| `GetJobLog` sorter | 要么不发，要么发控制台那一对 `[{field:"@timestamp"}, {field:"log-id.keyword"}]`；只发其中一个报 `InternalError: 日志排序字段不合法` | Wrapper 不发，排序在客户端做 |
| `GetJobLog` 时间窗 | `start` 必须早于 `end`（倒过来报 `InternalError: 日志查询时间参数不合法`）；超过一个月报 `InternalError: 日志查询时间区间不能超过1个月`，Wrapper 用 `HPC_LOG_MAX_WINDOW_MS`（30 天）挡在前面 | **控制台自己发的就是倒序的一对**（start 在 end 之后约 12 小时），网页端的聚合日志在这条路径上是坏的，不要照抄 |
| `GetJobLog` 分页 | `page_size` 省略或传 `-1` 都只回 100 条，`PageNumber` 被彻底忽略，而且 `page_size=N` **保留的是最旧的 N 条** | 「最后 N 条」平台点不到，必须先取满窗口再在客户端截尾。*（来自 Wrapper 作者实测；复核时日志已过保留期，未独立复现）* |
| `ListSlurmdPodEvent` | `page_size` 必发（省略回空列表配非零 `total`）。行没有 `type` 也没有 `count`——**平台按发生次数逐行重复**（一个实例 `total=106`，去重后 20 行） | 读事件前要自己折叠，否则 `--tail 20` 全是同一条 |
| 名字键 | 列表行是 `job_name`，`name` 从来没被填充过 | 读 `name` 会让每个任务都没有名字，列表 N/A 且 Name Resolver 匹配不到 |
| 删除顺序 | 要求先停止，运行中 `Conflict`，id 不存在 `ResourceNotFound`。**`SUCCEEDED_RETAINING` / `FAILED_RETAINING` 也算「运行中」**：`DeleteJob` 答 `Conflict`，`StopJob` 返回成功却不解除保留态，只能等平台释放（实测约一分钟） | 清理脚本按状态离开 `*_RETAINING` 重试，**不要按 `StopJob` 的返回值判断** |
| discovery 与线上不一致 | `ListJobs` 声明 `PageNumber`，实发 `page_num` | 以实发为准 |

---

### 8.3 `ray` — 弹性计算

Referer：`/jobs/ray`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateJob` | 见[创建面字段合同](#9-创建面的字段合同) | `{ray_job_id, sub_code, sub_msg}` | `ray create`、`ray batch` |
| `GetJob` | `{ray_job_id}` | `status` / `head_node{}` / `worker_groups[]` / `entrypoint` / `creator{}` / `priority_name` | `ray status`、`ray metrics` |
| `ListJobs` | `{workspace_id, page_num, page_size, filter_by:{user_id:[…]}}` | `{items[], total}`（字符串） | `ray list`、Name Resolver |
| `ListJobCreators` | `{workspace_id}` | `{items[]}` | — |
| `ListJobEvents` | `{ray_job_id, page_num, page_size, sorter:[{field:"last_timestamp", sort}], filter:{object_ids[]}?}` | `{items[], total}` | `ray events`、`--instance` |
| `ListJobInstances` | `{ray_job_id, page_num, page_size}` | `{items[], total}` | `ray instances` |
| `ListJobScalingHistories` | `{ray_job_id, page_num, page_size, worker_group_name?}` | `{items[], total}`（字符串） | `ray scaling` |
| `GetJobLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}`（int） | `ray logs` |
| `StartJob` | `{ray_job_id}` | `{ray_job{}}` | `ray start` |
| `StopJob` | `{ray_job_id}` | `{ray_job{}}` | `ray stop` |
| `DeleteJob` | `{ray_job_id}` | `{ray_job{}}` | `ray delete` |
| `GetTaskMetric` | 见 [8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `ray metrics` |

| 项 | 事实 | 后果 |
| --- | --- | --- |
| 资源键 | 每个 Action 都是 `ray_job_id`（`job_id` / `id` 报 `unknown field`）。**唯一例外是 `GetJobLog`**：它反过来只认 `job_id` | 与 `train` / `hpc` 不同，不能类推 |
| `GetJobLog` 的定位方式 | `job_id` 不 scope 任何东西（单独发答 `InternalError`）。真正的定位是 `filter.podNames`，平台反解回唯一一个 job，权限检查也落在这里 | 控制台对 `ray` 不发 `job_id`，Wrapper 照做 |
| `GetJobLog` 的 filter | 只收 `podNames` / `start_timestamp_ms` / `end_timestamp_ms`；`worker_group_name`、`instance_type`、`keyword`、`object_type` 全是 `unknown field`。时间戳是字符串型 epoch 毫秒 | — |
| 空 `podNames` | 回一个干净的 `{"logs": [], "total": 0}` | 与「这个集群什么都没打印」不可区分，Wrapper 在发出前就拒绝 |
| `ListJobEvents` 的信封是专用的 | 资源键是 `ray_job_id`，不是 `filter.object_ids`。但**一次调用同时给两级事件**——实测 17 行里 3 行 `object_type: "job"`（`CreatedRayCluster` / `CreatedService`），14 行 `"instance"` | **不需要像 `hpc.ListSlurmdPodEvent` 那样按实例扇出** |
| `filter.object_ids` | 有效：给 Pod 名收窄到那些实例并滤掉控制器行；不认识的 id 回空列表。`filter.object_type` 只认字面量 `instance` | Wrapper 只发 `object_ids` |
| 事件形状 | K8s 形状（`reason` / `type` / `message` / `first_timestamp` / `last_timestamp` / `count`），但上报方在 `source_component` 而不是 `from`，另有单调递增的 `id`。**时间戳只到秒** | 同秒次序还随 filter 变，客户端排序要拿 `id` 做 tiebreaker，否则「倒着取一屏再翻回来」会把因果顺序翻反。关键信号：提交时 `CreatedRayCluster`(Normal)、卡 PENDING 时 `FailedScheduling`(Warning) |
| 状态机拒绝报成 `InternalError` | `InternalError: RayJob status not allow <动词>`，而不是 `Conflict`。对非 STOPPED `StartJob`、对已 STOPPED `StopJob` 都走这条 | `InternalError` 在瞬时名单里，于是一个「永远不可能成功」的拒绝被读成「平台暂时不舒服」，还白烧三次重试。只有 `DeleteJob` 给的是 `Conflict` |
| 实例行 | `instance_id` / `instance_type`(`head`/`worker`) / `worker_group_name` / `status` / `cpu_count` / `memory_size` / `gpu_count` / `priority_level` / `created_at` / `name` / `node_name` / `pod_ip` / `started_at` / `priority_name` / `ray_job_id` | — |
| 属主与优先级键 | 列表行用 `creator` 和 `priority_name`；`created_by` 和 `priority` 恒为 null | 只读那两个会让每个任务都没有属主、优先级恒 None |
| 日志行 | `log_id` / `message` / `node` / `pod_name` / `time` / `timestamp_ms`（字符串）/ `timestamp_str`。`time` 带 `+08:00`，`timestamp_str` 是 Z 归一化形式 | — |
| Workspace scoping | 顶层 `workspace_id`，`filter` 嵌套会被拒 | — |
| 没有 Console 变体 | `ray` 对 `CreateJobConsole` 答 `InvalidAction`，创建走 `CreateJob` | — |
| 仍未验证 | `ListJobScalingHistories` 的空路径跑通（`{items: [], total: "0"}`），**带数据的行没见过**，字段仍照 SPA 渲染代码写。`GetJobLog` 的 30 天窗口上限在 `ray` 上探不到（实例名解析先于窗口校验），命令防御性 clamp | — |
| 镜像前提 | 账号可见的 295 个镜像**没有一个自带 `ray` 二进制**（官方 `inspire-ubuntu:24.04-base-ascend` 的 head 直接 `ray: command not found` 崩溃循环） | 建 Ray Job 的人要自带镜像，这不是 CLI 能兜的 |

**`UpdateJob` 查过、刻意不接**：只能改停止的任务（运行中答 `Conflict: Ray Job 正在运行中`）。在真实 STOPPED 任务上逐字段量过，**真正可写的只有 `name` 和 `description`**；另外 27 个候选键——含 `worker_groups` / `head_node` / `min_replicas` / `max_replicas` / `replicas` / `task_priority` 等所有可能的伸缩杠杆——全部 `unknown field`，`ScaleJob` / `UpdateWorkerGroup` / `ResizeJob` 也都 `InvalidAction`。**弹性区间在创建时就定死了**，而 Name-only 的 CLI 改名等于让自己的名称索引失效。

---

### 8.4 `notebook` — 交互式建模

Referer：`/jobs/interactiveModeling`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateNotebook` | 见[创建面字段合同](#9-创建面的字段合同) | `{notebook_id, sub_code, sub_msg}` | `notebook create`、`notebook batch` |
| `GetNotebook` | `{notebook_id}` | `status` / `sub_status` / `workspace{}` / `project{}` / `logic_compute_group{}` / `quota{}` / `node{}` / `extra_info{}` / `dataset_info[]` | `notebook status` / `metrics` / `exec` / `shell` / `ssh` / `proxy-url`、`wait` 轮询 |
| `ListNotebooks` | `{workspace_id, page, page_size, filter_by:{keyword, user_id[], logic_compute_group_id[], status[], mirror_url[]}, order_by:[{field:"created_at", order:"desc"}]}` | `{list[], total}`（int） | `notebook list`、Name Resolver |
| `ListNotebookCreators` † | `{workspace_id}` | `{list[], total}` | — |
| `ListNotebookEvents` | `{notebook_id, page, page_size}` | `{list[]\|events[], total}` | `notebook events`、create 的等待预览 |
| `ListNotebookLifecycles` | `{notebook_id, page, page_size, start_time?, end_time?}` | `{list[]}` | — |
| `ListRunIndex` | `{notebook_id}` | `{list[{index, start_time, end_time}]}` | `notebook lifecycle` |
| `StartNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook start` |
| `StopNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook stop` |
| `DeleteNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook delete` |
| `SaveNotebookImage` | `{notebook_id, name, version, description, flatten}` | **恒为 `{}`**（`Result: null`） | `notebook save-image` |
| `EstimateSaveMirrorSize` | `{notebook_id}` | `{active_snapshot_size}` | `notebook save-image`、`--dry-run` |
| `CancelSaveMirror` | `{notebook_id}` | — | `notebook cancel-save-image` |
| `CheckNotebook` † | `{name, workspace_id}` | 占用时 `{notebook_id, sub_code, sub_msg}`；空闲时 `Result: null` | create 的重名前置校验 |
| `GetNotebookAccessUrl` | `{notebook_id}` | `{jupyter_url, vscode_url}` | `notebook proxy-url` / `exec` / `shell`、SSH 链路 |
| `GetRealtimeNotebookMetric` | `{notebook_id}` | `{resource_metric_list[]}` | `notebook metrics --now` |
| `GetScheduleConfig` | `{WorkspaceId}` | Workspace 调度策略全集 + 四份**规格菜单** `quota` / `predef_train_spec` / `rayjob_quota` / `serving_quota` | `resources policy`、`<workload> quota` 的 Priority 列、create 的优先级预检 |
| `GetTaskMetric` | 见 [8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `notebook metrics` |

| 项 | 事实 | 后果 |
| --- | --- | --- |
| 列表键与 total | 分页列表键是 **`list`**（与 `ray` 相反），`total` 是 int（与 `hpc` 相反） | 逐 Action 实测，不要类推 |
| `ListRunIndex` | 无分页，传 `PageNumber` 直接报错。`end_time = ""` 的那条是当前运行周期，按 `index` 从旧到新 | 控制台「生命周期」页用的是它；`ListNotebookLifecycles` 对绝大多数 Notebook 返回空 |
| `ListNotebookEvents` 的事件形状 | 平台自有形状而非 K8s：`content`（正文）、`created_at`（epoch-ms 字符串）、`event_id`。共享渲染器把 `content → message`、`created_at → last_timestamp` / `first_timestamp` | 事件从旧到新返回，Wrapper 默认自动翻页到 `total`（安全上限 100 页） |
| 找不到资源 | 返回 `ResourceNotFound`，HTTP 仍是 200 | 靠状态码判断「不存在」的调用方会读到「找到了」 |
| `node{}` | 是整个节点对象：`name`（如 `cpu-nat-351`）、`status`、`cordon_type`、`is_maint`、`resource_pool`、`cpu_count` / `memory_size` / `gpu_count`。**STOPPED 时不清空对象，而是把 `name` 置空、`status` 置成 proto 零值 `UNKNOWN_NODE_STATUS`** | 只判空对象会把「没在跑」读成「有一个状态未知的节点」。第二个来源 `extra_info`（`NodeName` / `HostIP` / `PodName` / `ContainerID`）停止时同样是空串而不是缺键 |
| `GetRealtimeNotebookMetric` 的空 handle | 收到空 / 缺失的 `notebook_id` **不报错**，而是用成功信封返回整个集群的汇总（实测 CPU total 159682.1、GPU total 7765、已用 1743.12） | 不做前置校验会把这个印成「这一个 Notebook 占了上千张卡」。**Wrapper 必须在发出前拒绝空 handle** |
| 它的返回形状 | 固定四行 `{resource_name, total, used, available, usage_rate, unit, spec}`，`usage_rate` 是 0–1，`unit` 只有 Memory 是 `"GB"`，`spec` 恒空，没有 disk / network | **STOPPED 的 Notebook 四行全 0 且 HTTP 成功**，与「RUNNING 但空闲」不可区分，命令层必须同时打印状态 |
| `GetNotebookAccessUrl` | 是 **IDE 网关地址**，不是 Notebook 反向代理。两个 URL 归一化后指向同一个网关，任取其一。STOPPED 时返回两个空字符串 | 实测 **0.57 秒 vs Playwright 的 6.4–36 秒**。解析顺序：缓存/热候选 → 本 Action → Playwright，收口在 `resolve_notebook_vscode_ide_url`；`refresh=True` 时本档**也走**（refresh 是「别信缓存」，不是「一定要抓」） |
| `exec` / `shell` 全程不起浏览器 | lab URL 取**原始 `jupyter_url`**（不能用归一化后的形式——terminal 的 REST 与 WebSocket 路由挂在 Jupyter server base 上）；`_xsrf` 靠对 `jupyter_url` 发一次普通 GET 拿 cookie；建/删 terminal 是 `POST` / `DELETE api/terminals`，`_xsrf` 放进 `X-XSRFToken` 头 | 命令结束后回收本次创建的 Terminal |
| JupyterTransport 不消费未核验的名称缓存句柄 | Transport 预检发现 GPU 探针不通或将使用 H100/H200 JupyterTerminal 时，会按同一名称和 Workspace Live 重解一次；旧实例被删后重建时，当前句柄覆盖缓存后才进入用户命令。`--ignore-target-cache` 从第一次解析起就强制 Live | 恢复发生在只读探针之前，不会为了修缓存而重放用户命令；手动 `cache refresh` 不是恢复步骤 |
| `EstimateSaveMirrorSize` | `active_snapshot_size` 单位是字节，线上是**十进制字符串**（discovery 声明 int64）。它是容器可写层的增量，不是最终镜像大小。非 RUNNING 答 `InvalidParameter: Cannot save image of non-running notebook: <id>`（**内嵌 raw id，投影时必须折掉**），未知 id 答 `ResourceNotFound` | 取不到大小要读作「未知」，**绝不能读作 0** |
| `SaveNotebookImage` 的字段面 | discovery 声明 `notebook_id`（唯一必填）/ `name` / `version` / `description` / `accessible`(int32) / `support_brand_list` / `flatten`(bool)，逐个探过全在合同里。**不收 `visibility`**（`unknown field`）；**`accessible` 只有两档**（1 个人可见 / 2 公开可见） | CLI 可见性有三档，所以 `accessible` 替代不了存完再 `image.UpdateImage` 那一步。它也不返回新镜像 id，只能靠列表找 |
| 优先级限制只在 `GetScheduleConfig` 里 | 四个 `*_quota` / `predef_train_spec` 的值是 **JSON 编码的字符串**（不是数组），元素 `{id, cellId, name, cpu_count, memory_size, gpu_count, gpu_type, logic_compute_group_ids, allowed_priority_levels}`。`id` 就是配额目录里的 `quota_id`，10 个工作空间 × 4 类 Workload **零 miss** | 这是把优先级限制 join 到配额目录上的键。`allowed_priority_levels` 取 `null` / `[]`（不限）或 `["low"]`；全平台 168 条规格只有 9 条受限，全是 `分布式训练空间` 训练区的碎卡档，整节点档不受限。`logic_compute_group_ids` 为空表示对所有组开放。HPC 的 `predef_node_spec` 不在这份记录里 |
| 它是调度策略的全集 | `GetNotebookScheduleConfig`、`ray.GetRayJobScheduleConfig`、`train.GetTrainScheduleConfig` 都是它的严格子集（10 个 Workspace 逐字段同值、同 `config_id`），所以只接这一个 | 它与**管理员专用**的 `workspace.GetScheduleConfig` 只是重名 |
| `allow_ssh: true` 硬编码 | 平台据此在代理地址上暴露容器内的 rtunnel 端口 | 缺了它代理返回 404，Notebook SSH 的预检完不成。该字段省略时默认 false，与镜像里有没有 SSH 工具无关 |

**`flatten` 是真生效的字段**（2026-08-17 受控验证）：一台新建 CPU Notebook 连存两次，从 `docker-qb.sii.edu.cn` 的 Harbor 读回两份 manifest 比对。

| 镜像 | 层数 | 体积 | 保存耗时 |
| --- | --- | --- | --- |
| 基底 `sandbox-base` | 7 | 162.91 MB | — |
| `flatten=false` | 8 | 331.78 MB | 33.4 s |
| `flatten=true` | **1** | 286.90 MB | 55.5 s |

分层保存把基底 7 层逐个 digest 原样保留再追加一层；压平合并成一层且**更小**（-13.5%），被后层覆盖或删除的内容不再随镜像走。多出来的 22 秒落在镜像的 `CREATING` 上，**不落在 Notebook 上**——两次都在 t≈33 秒回到 `RUNNING`。压平出来的镜像能正常起 Notebook。两个附带数字：全新 Notebook 什么都不做，提交层也有 168.9 MB（平台自己注入的 runtime）；`EstimateSaveMirrorSize` 对它报 523321344 B，是未压缩的可写层大小。

**`SaveMirror` 与 `SaveNotebookImage` 是两个 handler**：控制台调的是前者（不在 discovery 里，活着）。`SaveNotebookImage` 空 body 答 `InvalidParameter: NotebookId is required` 并做严格 proto 解析；`SaveMirror` 空 body 答 `InternalError: 非法的镜像名称或版本`，**未知字段一声不吭**。CLI 留在 `SaveNotebookImage`：它在 discovery 里、报错能用来测字段，`flatten` 也已在它上面验过。

**`GetRealtimeNotebookMetricByTime` 刻意不接**：只收 `notebook_id`（`time_range` / `metric_types` 都是 `unknown field`），固定约一小时窗口、5 秒粒度。`metrics --window 1h` 已用同样四个指标覆盖同一小时，而 CLI 只打 min/max/avg/last 加 sparkline，5 秒与 60 秒的差别在这个粒度上不可见；它又只存在于 `notebook` 路由，进不了共享的 metrics 命令工厂。

---

### 8.5 `inference_serving` — 模型部署

Referer：`/jobs/modelDeployment`。路由名是**下划线**形式。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `CreateServingConsole` † | 见[创建面字段合同](#9-创建面的字段合同) | `{inference_serving_id, sub_code, sub_msg}` | `serving create`、`serving batch` |
| `ListServings` | `{workspace_id, page, page_size, filter_by:{my_serving:true, keyword?, project_id[]?, status[]?, inference_serving_type[]?}}` | `{inference_servings[], total}` | `serving list`、Name Resolver |
| `GetServing` | `{inference_serving_id}` | `status` / `replicas` / `node_num_per_replica` / `model_id` / `model_version` / `mirror_id` / `port` / `command` / `resource_spec_price{}` / `extra_info{node_names[]}` | `serving status`、`serving metrics` |
| `ListServingVersions` | `{inference_serving_id}` | `{inference_servings[]\|list[], total}` | `serving versions` |
| `ListServingInstances` | `{inference_serving_id, page, page_size}` | `{groups[{items[]}], total}` | `serving instances` / `logs` / `events --instance` |
| `ListServingEvents` | `{page, page_size, filter:{object_type, object_ids[]}}` | `{events[]\|items[]\|list[]}` | `serving events`、`--instance` |
| `ListServingScaleHistory` | `{inference_serving_id, page, page_size}` | `{scale_history_items[], total}`（字符串） | `serving scale-history` |
| `GetServingLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}` | `serving logs` |
| `GetServingScheduleConfig` | `{workspace_id}` | `{enable_auto_stop, items[{auto_stop_ruleset, gpu_count_min, gpu_count_max}]}` | `resources policy` |
| `GetServingApiMetric` | `{inference_serving_id, metric_types[], time_range:{start_timestamp, end_timestamp, interval_second}}` | `{metric_groups[]}` | `serving api-metrics` |
| `GetInferenceServingTerms` | `{inference_serving_id}` | `{terms[]}` | — |
| `GetServingConfigByWorkspaceId` | `{workspace_id}` | `{configs{}}` | `serving configs` |
| `GetInferenceServingUserProjectList` | `{workspace_id}` | `{projects[], users[]}` | create 的项目/用户选择 |
| `StartServing` | `{inference_serving_id}` | `{inference_serving_id, sub_code, sub_msg}` | `serving start` |
| `StopServing` | `{inference_serving_id}` | `{inference_serving_id}` | `serving stop` |
| `ScaleServing` | `{inference_serving_id, replica}` | — | `serving scale` |
| `RollbackServing` | `{inference_serving_id, version}` | `{inference_serving_id, sub_code, sub_msg}` | `serving rollback` |
| `DeleteServing` | `{inference_serving_id}` | `{inference_serving_id}` | `serving delete` |
| `GetTaskMetric` | 见 [8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `serving metrics` |

| 项 | 事实 | 后果 |
| --- | --- | --- |
| 创建必须用 Console 变体 | discovery 里那个 `CreateServing` 的 Description 明写「via OpenAPI with simplified config」，契约确实不同：要 `spec_id` 而不是 `resource_spec_price`，`image` 是普通字符串而不是 `mirror_id`，且不收 `description` / `inference_serving_type` / `model_source` | **看到写操作的字段被大面积拒绝时，第一反应应该是「是不是找错 Action 了」，而不是「契约变了」** |
| `ScaleServing` 的字段是单数 | `replica`，而 create 和 `UpdateServing` 用复数 `replicas` | — |
| `StartServing` / `StopServing` | 只收 `{inference_serving_id}`，请求体里的 `version` 会被拒 | 这两条曾经用手写的 `code != 0` 解包，对任何输入都返回 `API error: None`——**解包不走 `_v2_result()` 会把真错误伪装成假错误** |
| `DeleteServing` | 要求先停止，运行中答 `Conflict`；id 不存在答 `ResourceNotFound` | 清理路径必须 `StopServing` → 轮询到 `STOPPED` → `DeleteServing` |
| `ListServingInstances` 的行是嵌套的 | `{groups: [{items: [...]}], total}`，一个副本一个 group | 按顶层读会拿到空列表配非零 `total`，而那正好是「这个部署还没有 Pod」的形状——**失败静默且永久** |
| 实例行字段 | `name`（**带命名空间** `<project>/<pod>`）、`component_type`(`LEADER`/`WORKER`)、`status`、`node`、`ready`、`restarts`、`term`、`created_at` / `started_at` / `finished_at`、`running_time_ms` | — |
| `ListServingEvents` 的两级 | `INFERENCE_SERVING` 给部署级（`CreatingRevision` / `GroupsProgressing` / `Pending`），`INFERENCE_SERVING_INSTANCE` 给 Pod 级（`Scheduled` / `Pulled` / `Created` / `Started` / `Unhealthy`），两者不相交 | **Pod 级的 `object_ids` 必须带命名空间**，裸 pod 名答 `InternalError`。枚举第三个值 `INFERENCE_SERVERLESS` 未验证 |
| 列表键 | `ListServingScaleHistory` 是 `scale_history_items`，不是 `items` 也不是 `list` | 曾经按 `items` 读，任何有扩缩容历史的 serving 都返回空列表——读错键就永远看不到数据 |
| 节点落点 | 在 `GetServing` 的 `extra_info.node_names[]`，顶层只有 `node_num_per_replica`（每副本几个节点，是规格） | 按顶层读会让每个部署都显示成没落点 |
| `GetServingLog` 的坏 pod 名 | 答 `InternalError` | 看着像平台故障，实际含义是「pod 名不对」 |
| 两个指标族不相干 | `GetServingApiMetric` 是请求流量：`QPS`、`SUCCESS_QPS`、`FAIL_QPS`、`SUCCESS_RATE`、`FAIL_RATE`、`REQUEST_COUNT`、`LATENCY`、`TTFT`(+`_P50`/`_P95`/`_P99`)、`TTLT`(+同)、`INPUT_TOKENS`、`OUTPUT_TOKENS`。它**接受整个 `metric_types` 列表**（`GetTaskMetric` 不接受），也不需要计算组句柄 | 与 `GetTaskMetric` 共享零个指标名。返回项带 `metric_type` / `group_name` / `data_unit` / `time_series[{timestamp, data}]` |
| Serving 不一定要 GPU | `CPU资源空间` 的 `CPU资源-2` 组提供 CPU-only 档位（最小 `0,4,20`） | serving 的写面可以零 GPU 成本受控验证。`GetServingConfigByWorkspaceId` 报的 `gpu=1-119` 是自动停机规则的档位范围，**不是创建下限** |
| `GetServingScheduleConfig` | 回收规则**按 GPU 档位**给（每个 `items` 元素带 `gpu_count_min` / `gpu_count_max`），一个 Workspace 会有多条 | — |

**`UpdateServing` 受控验证过，结论是不封装。** 四条约束叠在一起让它对 Agent 不安全：

| 约束 | 内容 |
| --- | --- |
| 状态限制 | 只能在 `FAILED` 或 `STOPPED` 下调用，运行中答 `This serving can only be updated in FAILED or STOPPED status.` |
| 结构不对称 | `resource_spec_price` 必须是**扁平**结构，而 `GetServing` 读回的是**嵌套**结构（带 `cpu_info` / `gpu_info`）。**读回来的对象不能直接喂回去**，原样发送答 `unknown field "cpu_info"` |
| 全量替换 | 省略的字段会被清空。受控验证：完整 body 成功后再发一份只去掉 `command` 的，`command` 立刻变成空串 |
| 版本递增 | 每次成功 bump `version`（1 → 2），下一次必须带新的 |

安全的 Wrapper 要把**全部**字段搬运一遍，而 `port_configs` / `runtime_attributes` / `traffic_config` / `custom_mounts` 这些 CLI 没有建模的字段一旦漏掉就被静默清空。改配置重建一个 serving 既便宜又安全。单字段的 `ScaleServing` 与 `RollbackServing` 不受这条影响。

**`GetInferenceServingTerms` 不是调用说明**：`terms` 元素是 `{term, start_time, end_time}`，即**运行期次索引**（控制台用它把详情页各 tab 圈定到某一次运行），里面没有 endpoint、示例请求或 token。调用信息是 `GetServing` 的 `port` 和 `command`。

---

### 8.6 `workspace` — 工作空间资源

Referer：`/jobs/distributedTraining`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ListLogicComputeGroups` | `{page_size: -1, page_num: 1, filter:{workspace_id}}` | `{logic_compute_groups[], total}` | `resources availability`、`<workload> quota`、每个 create 的组解析 |
| `ListNodeDimension` | `{filter:{workspace_id, logic_compute_group_id}, PageNumber, page_size}` | `{node_dimensions[], total}` | `resources availability`、`resources nodes` |
| `ListTaskDimension` | `{filter:{workspace_id, logic_compute_group_id?}, PageNumber, page_size}` | `{task_dimensions[], total}` | `resources usage --by task\|project` |
| `ListUserDimension` | `{filter:{workspace_id}, PageNumber, page_size}` | `{user_dimensions[], total}` | `resources usage --mine` |
| `GetLogicComputeGroupNodeSpecs` | `{workspace_id, logic_compute_group_id}` | `{node_specs[]}` | `resources nodes` |
| `GetWorkspaceNodeSpecs` | `{workspace_id}` | `{node_specs[]}` | `resources nodes` |
| `GetLogicComputeGroupResource` | `{workspace_id, logic_compute_group_id}` | `{logic_resouces{}, gpu_type_stats[], runtime_attributes[]}` | `resources availability` |
| `GetWorkspaceQuota` | `{workspace_id}` | `{gpu_high_running, gpu_high_running_used, cpu_*, memory_*, is_fair_workspace, …}` | — |
| `GetWorkspaceComputeResource` | `{workspace_id}` | `{logic_resouces{cpu_total, cpu_used, memory_gi_total, memory_gi_used, gpu_total, gpu_used, gpu_low_priority_used}}` | — |
| `cluster.ListNodeEvents` | `{PageNumber, page_size, filter:{node_names[], from?}, sorter:[{field:"last_timestamp", sort}]}` | `{events[], total}` | `resources node-events`（Referer `/cluster/nodeList`） |

| 项 | 事实 | 后果 |
| --- | --- | --- |
| `ListNodeEvents` 的 filter | `filter.node_names` 事实上必填：不给 filter 答 `{events: [], total: 0}`；节点名不认识同样是空列表而不是报错。`filter.from` 按上报组件收窄有效；`event_type` / `type` / `keyword` 全是 `unknown field`；discovery 声明的 `start_last_timestamp` / `end_last_timestamp` 答 `InternalError` | 空列表与「这个集群很安静」不可区分。行的类型字段叫 **`event_type`**，也**没有 `count`**；时间窗只能在客户端做。一次可给多个节点，行里带 `node_name` 自己署名 |
| 它覆盖的信号 | 内核 OOM kill、`TaskHung`、Cordon / Uncordon、`Rebooted`、`NodeNotSchedulable` | 平台上唯一按节点组织的事件源 |
| `ListNodeDimension` 的两级 scoping | `filter` 里只放 `logic_compute_group_id` 返回 `AccessForbidden`，同时放 `workspace_id` 和 `logic_compute_group_id` 才通 | 最容易踩的一处 |
| 维度族的 scoping | 只认嵌套 `filter.workspace_id`：顶层被拒为 `unknown field`，缺 workspace 则 `AccessForbidden`。`filter.logic_compute_group_id` 可选且真收窄；`filter.task_type` 被**静默忽略**，`task_name_keyword` / `gpu_type` 有效 | — |
| 维度族的分页 | `page_size: -1` 和省略都只回 10 行（实测 `total=1289` 时仍只给 10），必须按 `total` 显式翻页。正整数 `5000` 实测可用，Wrapper 以此为默认页并继续按 `total` 翻页；三种分页拼法都认，`total` 是 int | — |
| 维度族的排序 | `order_by` 元素是 `{field, sort}` 而不是 `{field, order}`，只有 `created_at` 被采纳且 `sort` 被忽略（恒升序），`{"field":"gpu"}` / `{"field":"cpu"}` 直接 `InternalError` | **排序只能在客户端做** |
| 维度行的内容 | 只含存活工作负载（`RUNNING` 加短暂的 `COMMITTING`），覆盖所有用户与所有 Workload 类型。**`gpu.used` 是死字段（恒 0）**，但 `gpu.usage_rate` / `cpu.usage_rate` 是活的 0–1 比率 | TensorBoard 不在维度里（`train.GetLcgUsedComputeResourceJobs` 才有），不过它一张卡都不占 |
| 两把优先级刻度 | `task_dimensions[].priority` 是**提交值**（`--priority` 的 1–10 档，线上取到 1/3/4/6/8/10），`resources usage` 的 `Reclaimable` 用它；`train.GetJob` 回的是平台**存储值**（提交 10 存成 35，配 `priority_level: HIGH`） | 两者之间**没有验证过的换算**，CLI 不做反查，`job status` 原样回显存储值并同时给出 `priority_level` |
| 节点行的 GPU 数 | 嵌在 `gpu.total` 里，不是扁平 `gpu_count` | 只读扁平键会让每个节点看起来都是零卡，静默把空闲节点数清零 |
| 「完全空闲」的判据 | `status=READY` + 无 `tasks_associated` / `task_list` + 无 `cordon_type` + 非 `is_maint` + `resource_pool != fault`，五项同时成立 | 少判一项就会把调度不上去的节点算成空闲 |
| `node_specs` 是规格目录不是节点清单 | 一个 292 节点的组只发布 17 种形状，行是「形状 × 作业类型」的笛卡尔积，还会因 GiB 小数差异重复（68 行原始数据只有 6 种真实形状） | **任何按行数当节点数的读法都是错的**。`gpu_type` 恒空、`gpu_memory_size` 恒 0（真值在 `gpu_info` 里），discovery 声明的 `node_count` 线上不存在 |
| `Get*NodeSpecs` 的 scoping 在顶层 | 套 `filter` 是 `unknown field`；只给组不给 workspace 是 `AccessForbidden` | 与维度族相反 |
| 平台拼错的键 | 组资源汇总是 **`logic_resouces`**（少一个 `r`），`GetLogicComputeGroupResource` 与 `GetWorkspaceComputeResource` 同病。GPU 型号在 `gpu_type_stats[0].gpu_info.gpu_type_display` | — |
| `logic_resouces.gpu_total` 不是硬件分类 | 它是当前 Workspace 在该组的 GPU 保障额度；公平调度可以出现 `gpu_total=0`、`gpu_used=7`，同时 `gpu_type_stats` 报 H200、NodeDimension 有 2 台 8 卡节点。`gpu_total - gpu_used` 因而可以为负 | 不能靠 `gpu_total > 0` 识别 GPU 组，否则零保障组会被当成 CPU 并从默认 Availability 消失。Wrapper 综合 Live 使用量、`gpu_type_stats` 和 NodeDimension 分类；负余量原样保留，表示已超保障额度 |
| `ListLogicComputeGroups` 的两个坑 | 标识字段叫 `logic_compute_group_id` 而不是 `id`；`support_job_type_list` 是 **JSON 编码的字符串**，不是数组 | 用 `isinstance(x, list)` 判断会把每个组都读成「没声明」，按 Workload 过滤计算组看起来生效、实际一个都没滤掉。取值域：`interactive_modeling` / `hpc_job` / `ray_job` / `distributed_training` / `tensorboard` / `inference_serving_customize` / `inference_serving_exclusive` |
| 配额字段的结构 | `{资源}_{high\|low}_{running\|total}` 加可选 `_used`：高优先级（保障）和低优先级（可回收）是**两套独立的上限**，一个运行中的任务只吃其中一套。`-1` 表示不限 | 混着读会两边都报错。`GetWorkspaceQuota` / `GetWorkspaceComputeResource` 要顶层 `workspace_id`，套 `filter` 反而被拒 |
| 配额与容量是两个问题 | **配额用完了可以被拒，即使机器闲着；机器忙满了也可以被拒，即使配额还有** | 两者都要看 |
| 资源视图的命令内复用 | Availability 对各 Compute Group 的 `GetLogicComputeGroupResource` + `ListNodeDimension` 以 4 路有界并发读取；Nodes 直接复用这批 NodeDimension 计算 8-GPU 整节点数，只额外请求 NodeSpecs。原始节点行只活在内部 `GPUAvailability.node_dimensions`，公共投影不暴露 | 不复用时 Nodes 会把每个组的 NodeDimension 完整读取两遍；持久缓存又会把实时余量冒充当前事实，所以复用范围只限本次命令 |
| `ListProjectDimension` | 实测是空的：10 个可见 Workspace、逐计算组、逐项目 id 都返回成功信封配 `total: 0`，而同族兄弟在**同一个** `filter.workspace_id` 位置上答出真实数据 | 是权限地板，没有封装；按项目聚合改为在客户端折叠任务维度的行 |

---

### 8.7 `user` — 账号

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `GetUserDetail` | `{}` | `{id, name, name_en, email, avatar_url, global_role, created_at, extra_info{}}` | 所有「按当前用户过滤」的列表、`account check`、登录自举 |
| `GetPermissions` † | `{WorkspaceId}` | `{permissions[]}`（如 `"job.trainingJob.create"`） | `account permissions` |
| `GetRoutes` † | `{WorkspaceId}` | `{routes[{name, routes[{path, name, is_fair_workspace}]}]}` | Workspace 枚举、优先级选择、`init`、登录自举 |

| 项 | 事实 |
| --- | --- |
| `GetUserDetail` 只覆盖当前用户 | 传空体返回当前账号；传 `user_id` / `id` / `UserId` 一律 `InvalidParameter` |
| 当前用户身份在登录时已经读过 | 登录握手把完整 `GetUserDetail` 存进账号隔离的 `WebSession`；所有只需要 owner id 的列表直接复用，缺失时才 Live 回源并写回。`account check` 是例外：它显式强制 Live，用这次往返证明 Session 仍能被平台接受。共享慢账号上这会从每条列表命令稳定省掉一个串行请求 |
| `userWorkspaceList` 是 Workspace 枚举的唯一来源 | 每个条目 `path` 是 `ws-…` id，`name` 是显示名，`is_fair_workspace` 是优先级选择器的唯一数据源，缺了就没法判断该工作空间用哪套优先级 |
| 登录自举就用这两个 | `GetRoutes` 的 `WorkspaceId` 传字面量 `"default"` 网关照收，答完整的 `userWorkspaceList`——与传真实 Workspace id 的响应逐字节相同（5794 字节 0 差异）。空串或省略才报 `WorkspaceId is required`。这正是登录握手时的处境：还一个 id 都不知道 |
| 未封装 | `user.ListSSH`（账号级 SSH 公钥注册表，与 Notebook SSH 链路无关）、`user.GetMyPermissions`（见第 6 章）。`ListAPIKeys` 随 `user api-keys` 命令一起下线 |

---

### 8.8 `project` — 项目

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ListProjects` † | `{page, page_size, filter:{workspace_id?, check_admin?}}` | `{items[], total}` | `project list`、每个 create 的项目解析、`init` |
| `GetProjectDetail` † | `{ProjectId}` | `{budget, children_budget, remain_budget, used_budget, created_at, en_name, description, priority, owner…}` | `project detail` |
| `GetProjectOwners` † | `{}` | `{items[{id, name, login_name, …}]}` | `project owners` |
| `GetProjectForPage` | `{page, page_size, filter{}}` | `{items[], total}` | — |

| 项 | 事实 |
| --- | --- |
| 两个列表条数不同是设计如此 | `ListProjects` 是选择器的行集，`GetProjectForPage` 是项目管理页的行集，后者会滤掉用户已退出或已结束的项目。**不要读成「谁坏了」** |
| `GetProjectDetail` 的跳动字段 | `remain_budget` / `used_budget` / `resource` 本来就在跳，连着读两次不相等是正常的 |
| 翻页策略 | CLI 侧固定 `page_size = 100` 直到 `len(items) >= total` 或短页；选择器路径用 `page_size: -1` 一次取全 |
| 对调度有意义的字段 | `gpu_limit`（是否有项目级 GPU-hour 上限）、`priority_name`（数字字符串）、`space_list[]`（项目跨哪些 Workspace） |

---

### 8.9 `image` — 镜像

Referer：`/jobs/interactiveModeling`。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ListImages` | `{page: 0, page_size: -1, filter:{…见下…}}` | `{images[], total}` | `image list`、各 create 的镜像解析、`cache refresh` |
| `GetImageById` | `{ImageId}` | `image_id` / `name` / `address` / `framework` / `version` / `source` / `visibility` / `status` / `description` / `created_at` | `image detail`、`notebook save-image` 的就绪轮询 |
| `CreateImage` † | `{name, version, registry_hint:{workspace_id}, visibility, add_method, description}` | `{image{image_id, …}}` | `image register` |
| `UpdateImage` † | `{id, visibility?, description?}` | — | `image set-visibility` |
| `DeleteImage` | `{image_id}` | — | `image delete` |

| 项 | 事实 | 后果 |
| --- | --- | --- |
| `UpdateImage` 的目标键是裸 `id` | 不是大小写问题——`image_id` 是**另一个字段名**，不会被归一化成 `id` | 传错不报 `unknown field`：**默默忽略**，然后拿空 id 去查库，回 `InternalError: 数据库错误, 请联系管理员`。**看到这个错先检查字段名** |
| `ListImages` 的三种来源用三种 filter | 官方：`{source: "SOURCE_OFFICIAL", source_list: [], registry_hint:{workspace_id}}`；公开：`{source_list: ["SOURCE_PRIVATE","SOURCE_PUBLIC"], visibility: "VISIBILITY_PUBLIC", registry_hint:{…}}`；个人可见：同上但 `VISIBILITY_PRIVATE` | 不是一个简单的 `source` 字段 |
| 镜像地址在 `address` | 不是 `url`。创建 Workload 时平台匹配的是**注册表 URL 而不是可见名** | 发名字会被拒为 `无法找到对应镜像` |
| `add_method` | `0` = 本地推送（`docker push`，网关直接拒 `no image uploaded`），`2` = 注册已有镜像地址 | — |
| 就绪状态有两套 | `image register` 产出的走 `READY`，`notebook save-image` 产出的走 `SUCCESS`。终态失败：`FAILED` / `FAILURE` / `ERROR` / `CANCELLED` / `TIMEOUT` / `ABORTED` / `INTERRUPTED` | 漏掉任何一个都会让轮询挂到超时而不是快速失败 |
| 目录按 Registry 组织 | `registry_hint: {workspace_id}` 指的是一个 Registry，多个 Workspace 正常共用同一份目录 | 按 Workspace 各读一遍是重复下载；用行里的 `registry_id` 判组 |
| 四种来源彼此独立 | official / public / project / private 各是一条完整 `ListImages`；`image list --source all` 同时读取，再按这个稳定顺序合并。单档失败只产生该档 warning | 串行读取会把四条目录延迟直接相加；并发不改变任何请求体或空结果语义 |

「把 Notebook 存成镜像」不在这条路由，在 `notebook.SaveNotebookImage`；`image` 组只管已经存在的镜像。

---

### 8.10 `model-hub` — 模型仓库

Referer：`/jobs/modelService?spaceId={workspace_id}`。路由名是**连字符**形式。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ListModels` | `{workspace_id, page, page_size, filter_by:{user_id, keyword?, project_id[]?, model_type[]?}}` | `{list[], total}` | `model list`、`serving create` 的模型解析 |
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

| 项 | 事实 | 后果 |
| --- | --- | --- |
| 两个近似名字极易接反 | `ListModelVersions` 多一个 `next_version`，是详情抽屉用的**富**视图（含模型路径、源路径、大小、发布状态、运行中的 serving 数）；`ListModelVersionOptions` 是部署表单用的**精简**版本列表 | 只能靠响应字段区分 |
| `ListModels` 不收 `page_size: -1` | 答 `InvalidParameter: page or page_size invalid`，要按 `total` 翻页 | 「-1 表示全要」在 `ListImages` / `ListLogicComputeGroups` / `ListProjects` 上成立，这里不成立 |
| `ListModelVersions` 不接受 `page` | `ListModelCreators` 接受 `project_id` | — |
| `GetRecommendedConfig` 给的是下限 | 四个 `min_*` 映射到 `serving create --quota gpu,cpu,mem` 和 `--nodes-per-replica` | 照抄不等于最优 |
| 版本记录里的 `is_vllm_compatible` 是死字段 | 29 个可见模型版本上无一为 true，而两个 live Action 一致地给出 13 个 true | 读存量字段的地方会永远报「不兼容」。**vLLM 兼容性只能问 live**：`GetModelVLLMCompatibleData` 一次回答某模型所有版本，`CheckModelVLLMCompatible` 按版本问 |
| 嵌套形状 | `ListModelVersions` 的元素是 `{model: {...}, running_infrence_serving}`（平台把 inference 拼错了），版本号与规格都在内层；列表项是 `{model: {...}, project_name, user_name, latest_version}`，扁平化时 `model_id` 优先于内层 `id` | — |
| `ListModelRelatedServings` | `page` 与 `page_size` 都必填（`-1` 被拒），`version` 也实质必填——省略时 proto 默认 0，返回空列表而不是「所有版本」 | 条目的 `version` 是**推理服务自己的版本号**，不是模型版本，印出来会被误读；`status` 是 int（`4` = RUNNING） |
| `GetHasModelPendingServing` | `version` 可选，省略即问整个模型；只在有 PENDING 部署时为 true（DEPLOYING、RUNNING 都是 false） | 正好补上 `running_infrence_serving` 计数为 0 却已有部署排队的盲区 |
| `CreateModel` 的路径约束 | `model_source_path` 必须落在所给 workspace + project 的路径下，`global_user` 路径被 `存储路径格式不正确` 拒掉。`model_source_type = 1` 对应 UI 的「路径注册」流程，首个版本号由后端推断 | — |
| `filter_by.project_id` 必须是数组 | 传裸字符串会被 protobuf 解码拒绝 | — |
| `UpdateModel` | 存在但未封装 | — |

---

### 8.11 `resource-price` — 配额目录 ‡

Referer：`/jobs/interactiveModeling`。**整条路由不在 discovery 里**，控制台一直在用。它挡在每一个 `create` 命令前面：把用户敲的 `-q 1,20,200` 翻成平台要的 `quota_id`，是这个翻译的唯一来源。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `GetLogicComputeGroupResourceSpecPrices` | `{workspace_id, logic_compute_group_id, schedule_config_type}` | `{lcg_resource_spec_prices[{quota_id, gpu_count, cpu_count, memory_size_gib, gpu_info{gpu_type, gpu_type_display, brand, gpu_memory_size_gb}, cpu_info{cpu_type}, total_price_per_hour, gpu_price, cpu_price, memory_price}]}` | `<workload> quota`、每个 create 的配额解析 |

| 项 | 事实 |
| --- | --- |
| `schedule_config_type` | `SCHEDULE_CONFIG_TYPE_` 加 `DSW`（Notebook）/ `TRAIN` / `HPC` / `RAY_JOB` / `SERVE`；另有 `SERVE_DYNAMIC` 对应 Serverless，CLI 不用 |
| 两个字段都必填 | 少 `schedule_config_type` 答 `unspecified schedule config type`，少 `logic_compute_group_id` 答 `Logic compute group id should not be empty`。**没有工作空间级的形态** |
| 它的价值是「已经按计算组解析好」 | `notebook.GetScheduleConfig` 的 `*_quota` 菜单是工作空间级的整张表，要自己按 `logic_compute_group_ids` 过滤，还要再叠「装得进组内某个节点」和「组真的有可分配容量」两条规则；这个 Action 直接给该组当下真正可用的那几行。两边的 `quota_id` 一一对应，用来 join `allowed_priority_levels` |
| `total_price_per_hour` 单位 | 点券/小时，实测等于 GPU 数（1/2/4/8 卡各 1/2/4/8）。同族的 `GetResourceAndInferencePrices` 与 `GetStoragePrices`（点券/TB/天）也可读，CLI 不接 |
| **空列表是权威回答** | 该组不跑这类 Workload 时就是 0 行（实测 `训练区-H200-1号机房` 的 `RAY_JOB` / `HPC` 都是 0 行）。请求失败要抛，两者**不能同值**——它们曾经同值，于是一次被限流的刷新把整个工作空间缓存成了「没有配额」 |
| 覆盖度 | 10 个工作空间 × 全部计算组 × 5 种 `schedule_config_type` 共 225 组实测 |

---

### 8.12 `file` — 文件页 ‡

Referer：`/jobs/files?spaceId={workspace_id}`（`GetSftpgoConnectionInfo` 在用户中心页下）。**整条路由不在 discovery 里**（历史上出现过又被删掉），但活着。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `GetSystemStorageTypeList` | `{filter:{workspace_id}}` | `{system_storages[{name, cluster_id}]}` | `init --scope project` 的存储池发现 |
| `GetDirList` | `{filter:{workspace_id, system_storage_type, name, cluster_id?}}` | `{files[{directory}]}` | 同上，逐存储池列项目目录 |
| `GetSftpgoConnectionInfo` † | `{storage_name}`（小写池名，可选 `usage`） | `{address, webdav_port, auth}` ⚠️ **`auth` 是明文凭据** | — |
| `ListFileCopyTasks` † | `{page, page_size}` | `{items, total}` | — |

| 项 | 事实 |
| --- | --- |
| `filter.name` 是前端的类别键 | 不是文件名：`project` / `global_public` / `global_user` |
| `system_storage_type` | 取 `GetSystemStorageTypeList` 返回的存储池名；`share-` 前缀的池在项目目录发现里被跳过 |
| 行顺序不稳定 | 12 个存储池和目录列表都会换序。当前调用方都不依赖顺序，**新调用方也不要依赖** |
| `CheckPermission` 形状未探明 | 只给 `{file_path}` 会返回一条字段全空的记录（`file_path` 和 `name` 都是空串、`size` 为 `"0"`）且不报错——读起来像「路径存在但什么都没有」。**探明之前不要拿它判断任何东西** |

**`GetSftpgoConnectionInfo` 是一条不需要计算资源的共享盘读写通道。** `{storage_name}` 取池名**转小写**（`hdd` / `ssd` / `qb-ilm` / `share-*`；不认 `workspace_id` 也不认 `filter`）。名字里的 sftpgo 有误导性——sftpgo 同时提供 WebDAV，控制台走的就是 WebDAV。

- **`auth` 是 base64 的 `用户名:密码`，一对可以直接用的明文凭据。** 必须和 Notebook 代理地址的 token 同等对待：不进日志、不进报错、不进 `--json`、不进任何文档。本页只记形状。
- 实测（`hdd` / `ssd` 两池）：`https://{address}:{webdav_port}` 是一台读写都通的 WebDAV 服务器，根下就是容器里那套 `/inspire/<池>/…` 全命名空间，逐级 `PROPFIND` 都是 207。写侧完整往返：`PUT` 201 → `GET` 200（逐字节一致）→ `DELETE` 204 → `GET` 404。
- **它不需要任何工作负载在跑。** 目前 CLI 的文件流转只有 `notebook scp`，要一台运行中的 Notebook 加容器内 sshd 加 rtunnel；WebDAV 把这三样都省掉。**尚未封装。**

**`CreateCopy` 不是复制，是一张要审批的申请。** 控制台里叫「新建数据传输」，表单只有 `source_path` / `target_path` / `overwrite`，提交按钮写的是「提交审批」——它和 `audit` 服务是一条链，不是即时的服务端 `cp`。同族的 `WithdrawFileCopyTask` / `DeleteFileCopyTask` 都只收 `{task_id}`。`ListFileCopyTasks` 是**用户级**的（不认 `workspace_id` 也不认 `filter`，只收分页），本账号 0 条，所以响应行的字段形状未知。

---

### 8.13 `dataset` — 官方数据集挂载 ‡

Referer：`/jobs/interactiveModeling?spaceId={workspace_id}`。**整条路由不在 discovery 里**，穷举四十个候选名后只有这一个 Action。

| Action | 请求体 | 响应（`Result` 内） | CLI |
| --- | --- | --- | --- |
| `ValidateDataset` | `{datasets:[{dataset_id, version_id}], workspace_id}` | `{datasets_result:[{dataset_id, version_id, success, path, error_message}]}` | `dataset validate`、`notebook/job/hpc create --dataset` |

| 项 | 事实 |
| --- | --- |
| 两个 id 都是 code，不是数字主键 | `dataset_id` 是数据集 code、`version_id` 是版本 code。`pixabay-81k` + `v0` 解析成功；同一数据集的数字 id 回 `数据集不存在`。数字主键只活在数据广场内部（第 11 章） |
| 错误码 | `2000 数据集不存在`、`2001 版本不存在`、`2005 无访问权限` |
| 返回的 `path` 是平台内部存储路径 | 如 `sftpgo/pixabay-81k/v0`，不是容器内挂载点。它要原样填进 create 请求的 `dataset_info[].path`——所以**创建前必须先走一次 `ValidateDataset`**，这正是控制台「校验数据」按钮做的事 |
| 容器内挂载点 | 固定 `/inspire/dataset/<数据集 code>/<版本 code>`，只读。CLI 只投影两个 code 和这个容器路径，平台存储路径不进公共输出 |
| 批量顺序 | 整批走一次请求，平台回的顺序**不保证**与请求一致，按 `(dataset, version)` 键回请求，**不要 zip** |
| 检索不在这条路由上 | 也不在这个平台上，见第 11 章 |

---

### 8.14 Metrics — `GetTaskMetric`

**没有集群级端点**：每个 service 各有一份 `GetTaskMetric`，请求体逐字相同。

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

| 项 | 事实 |
| --- | --- |
| **一次只能问一个 metric** | 一个请求里放 5 个 `metric_types` 只返回第一个的数据，所以 Wrapper 按 metric 扇出再合并；任何一个失败整次调用抛错 |
| 响应键是平台拼错的 | `time_seris_metric_groups`（少一个 `e`）。Wrapper 同时接受拼对的写法，以防平台哪天修正 |
| 8 个 metric_type | `gpu_usage_rate`、`gpu_memory_usage_rate`、`cpu_usage_rate`、`memory_usage_rate`、`disk_io_read`、`disk_io_write`、`network_tcp_ip_io_read`、`network_tcp_ip_io_write`。`*_usage_rate` 是 0–1 比率，I/O 是字节/秒 |
| 间隔选项 | `1m` = 60、`5m` = 300、`30m` = 1800、`1h` = 3600 秒 |
| 不支持的 `task_type` | 会拿到一个 Prometheus 422（抱怨空 label name），Wrapper 在发出前就校验 |
| 分组 | 多 pod 实例（分布式训练、多副本 serving）每个 pod 一个 group；单实例 Notebook 恰好一个 |
| 没有对应物的那个 | `workspace.GetOverviewResourceMetricByTime` **不是**它的对应物：那是工作空间级总览，对普通成员 `AccessForbidden`。第 5 章的「用 `workspace.*` 不用 `cluster.*`」只适用于两边同名的那 8 个 Action |

#### `GetTaskMetricBatch` 为什么不用（2026-08-18 实测）

同样五个路由上都有一个 `GetTaskMetricBatch`，请求体是 `{task_ids, metric_types, time_range}`（不收 `filter` / `logic_compute_group_id`，传了报 proto unknown field）。它**答的不是同一份数据**，不能当作上面那条的批量形态。

响应形状先不一样：`Result.task_metrics[] {task_id, time_series_metric_groups[]}`，多包一层且这里的键拼**对**了。按单数版的顶层 `time_seris_metric_groups` 去读会读出空，于是它看起来像「没数据」而不是「坏了」——这一点让它被误判过一次。

`train` 路由、4 个运行中任务、同一时间窗逐指标对照：

| metric | 单数版 | Batch |
| --- | --- | --- |
| `gpu_usage_rate` / `gpu_memory_usage_rate` / `cpu_usage_rate` / `memory_usage_rate` | 61 点 | 61 点，但 `group_name` 缺失，**且数值不同（见下）** |
| `disk_io_read` / `disk_io_write` | 61 点 | **0 个样本**（4/4 复现） |
| `network_tcp_ip_io_read` / `network_tcp_ip_io_write` | 61 点 | **`InternalError`** |

8 个指标坏 4 个；`group_name` 全缺意味着多 Pod 任务的逐 Pod 拆分丢失。

**剩下那 4 个也不是同一份数**。取一个已经结束的固定窗口（排除「现在还在变」），两个端点**各自都是确定的**——各查两次结果逐字节相同——但互相对不上：

| timestamp | 单数版 | Batch |
| --- | --- | --- |
| 1787049449 | 0.20625 | 0.5 |
| 1787049509 | 0.69625 | 0.785 |
| 1787049569 | 0.7575 | 0.505 |

差在聚合样本数上：把每个端点全部返回值的公分母算出来，**单数版恒为 Batch 的 2 倍**——4 卡任务 800 : 400，8 卡任务 1600 : 800，即 `200×卡数` 对 `100×卡数`（4 个运行中任务全部符合）。同一个信号，Batch 只聚合了一半的样本。长期均值大致对得上（0.536 vs 0.528、0.965 vs 0.967），但**任意单点可以差到 0.206 vs 0.5**。

所以这不是「批量版少了个字段」，是**另一套聚合**：即便只取那 4 个能跑的指标，Batch 画出来的曲线也不是控制台上那一条。控制台自己在全部 bundle 里**一次都没有调用**这个 Action。省四分之三的请求换一块既会静悄悄读成 0、数值又对不上控制台的面板，不划算。

### 8.15 批量读 — `ListJobs` / `ListJobEvents` 的复数形态

`train` 与 `hpc` 各有两个 Action 接受 id 列表。两条都实测可用，但**限制与失败形态不对称**，照着一条的直觉写另一条会出事。

```jsonc
POST /api/v2/{train|hpc}?Action=ListJobs
{"workspace_id": "ws-…", "job_ids": ["job-a", "job-b"]}      // ≤ 20

POST /api/v2/{train|hpc}?Action=ListJobEvents
{"page_num": 1, "page_size": 200,                             // hpc 要 pageNum/pageSize
 "filter": {"object_type": "job", "object_ids": ["job-a"]}}   // hpc 是 "HPC_JOB"，≤ 20
```

| 项 | 事实 | 读错的后果 |
| --- | --- | --- |
| `ListJobs` 的记录 | 与 `GetJob` **逐字段完全相同**——在 `train` 上比过，running/stopped/failed 三态各一次，无独有字段、无值差异。**`hpc` 未实测**：当时账号在所有 Workspace 里都没有 HPC 任务，只验到两条路由的校验语句逐字一致（必填 `workspace_id`、上限 20、未知 id 静默丢弃）。记录等价性是推断，第一次拿到真实 HPC 任务时补验 | 这是 `GetJob` 扇出的批量形态，不必再补一次详情请求 |
| 上限 20 | `job_ids count exceeds limit 20` / `object_ids count exceeds limit 20`，**按列表长度计数**，20 个不重复 + 1 个重复 = 21 也被拒 | 先去重再分片；分片后去重仍然会撞上限 |
| `object_type: "instance"` **不受这个上限** | 500 个实测通过 | `list_job_instance_events` 按 200 分片是对的，不要跟着改成 20 |
| `ListJobs` 找不到 id | **静默丢弃**，`total` 只反映命中数 | 缺失只能由调用方拿请求 diff 出来；短列表不等于「就这些」 |
| `ListJobEvents` 找不到 id | **整个请求失败**：`InvalidParameter: job <id> not found`，train 与 hpc 都是 | 一个被 GC 的任务会带走同批另外 19 个的事件；必须剔掉报错点名的 id 重试 |
| `workspace_id` | `job_ids` 存在时必填，否则 `workspace_id is required when job_ids is set` | **Workspace 传错会静默返回 0**，与「任务不存在」不可区分 |
| `job_ids: []` | 掉回分页路径并报 `page or page_size too large` | 空列表必须短路，不能发出去 |
| 返回顺序 | 不是入参顺序 | 按 id 建索引，不要跟请求 zip |
| 事件可加性 | 严格可加（5 个任务批量 31 条 = 单查 3+3+3+3+19），每行带 `object_id` | 可以原样拆回各自任务 |
| 事件分页 | `total` 准确（20 个任务 651 = 200+200+200+51） | — |

### 8.16 `job.ListJobs` — 跨 workload 的批量状态（**未接入**）

`/api/v2/job` 这个路由在 discovery 里没有，控制台的 `schedulingService` chunk 里有（`a.GET_JOBS`）。它一次可以问**多种 workload 类型**的状态：

```jsonc
POST /api/v2/job?Action=ListJobs
{"filter": {"train_job_ids": [...], "notebook_job_ids": [...], "slurm_job_ids": [...]}}
→ Result.jobs[] {task_id, task_name, task_type, <类型专属状态字段>, project, creator, worksapce_id}
```

| task_type | 入参键 | 状态字段 |
| --- | --- | --- |
| `distributed_training` | `train_job_ids` | `train_status` |
| `interactive_modeling` | `notebook_job_ids` | `notebook_status` |
| `hpc_job` | `slurm_job_ids` | `slurm_job_status` |
| `ray_job` | `ray_job_ids` | `ray_job_status` |
| `tensorboard` | `tensorboard_job_ids` | `tensorboard_status` |
| `model_serving` / `inference_serving_customize` | `inference_serving_job_ids` | `model_serving_status` / `inference_serving_status` |
| `sandbox` | `sandbox_ids` | `sandbox_status` |

与 8.15 那两条相比：**不需要 `workspace_id`**、未知 id 静默丢弃且不报错、**未见 20 上限**（60 个不重复 id / 1000 条含重复均正常，服务端自行去重）。代价是记录很瘦——只有状态、项目、创建者，没有配额、节点、命令。响应里的 `worksapce_id` 是平台的拼写错误，要照抄。

**目前没有接入**：CLI 的每条状态命令都是按 workload 分组的，瘦记录填不满 `job status` 的输出，而没有消费者的 Wrapper 就是死代码。要做「一条命令看完所有在跑的东西」时，从这里开始。

---

## 9. 创建面的字段合同

五个创建 Action 接受的字段互不相同，且只有 `notebook.CreateNotebook`、`train.CreateJob`、`ray.CreateJob` 在 discovery 里声明过。下表用[字段存在性探针](#7-探针方法)逐字段量出来，**是判断某个网页选项能不能进 CLI 的唯一依据**。

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

| Action | 必填 | 可选 |
| --- | --- | --- |
| `notebook.CreateNotebook` | `workspace_id`、`name`、`project_id`、`project_name`、`auto_stop`、`allow_ssh`、`mirror_id`、`mirror_url`、`logic_compute_group_id`、`quota_id`、`cpu_count`、`gpu_count`、`memory_size`、`shared_memory_size` | `resource_spec_price`（GPU Notebook 必需）、`task_priority`、`node_id`、`dataset_info[]`、`enable_notification`、`stop_hour` + `stop_minute`、`is_publicpath_readonly`、`is_projectuserspath_readonly` |
| `train.CreateJobConsole` | `name`、`command`、`framework`、`project_id`、`workspace_id`、`logic_compute_group_id`、`task_priority`、`enable_notification`、`framework_config:[{image_type, image, instance_count, resource_spec_price, cpu, gpu_count, mem_gi, shm_gi?}]` | `max_running_time_ms`、`exclude_nodes[]`、`specified_nodes[]`、`auto_fault_tolerance` + `fault_tolerance_max_retry` + `fault_tolerance_retry_interval_sec`、`dataset_info[]`、`envs[]`、`description`、`reserve_on_success_ms`、`reserve_on_fail_ms`、`is_publicpath_readonly` |
| `hpc.CreateJobConsole` | `job_name`、`logic_compute_group_id`、`project_id`、`workspace_id`、`enable_notification`、`priority`、`sbatch_script:{number_of_tasks, cpus_per_task, memory_per_cpu, enable_hyper_threading, entrypoint}`、`slurm_cluster_spec:{predef_quota_id, cpu, mem_gi, image, image_type, instance_count, spec_price}` | `dataset_info[]`、`description`、`ttl_after_job_finish_seconds`、`is_publicpath_readonly`，以及 `sbatch_script` 里的运行时长四件套 |
| `ray.CreateJob` | `name`、`description`、`workspace_id`、`project_id`、`entrypoint`、`task_priority`、`head_node:{mirror_id, image_type, logic_compute_group_id, quota_id, shm_gi?}`、`worker_groups:[{group_name, mirror_id, image_type, logic_compute_group_id, min_replicas, max_replicas, quota_id, shm_gi?}]` | `is_publicpath_readonly` |
| `inference_serving.CreateServingConsole` | `workspace_id`、`project_id`、`inference_serving_type`、`name`、`logic_compute_group_id`、`model_id`、`model_version`、`mirror_id`、`command`、`port`、`description`、`replicas`、`node_num_per_replica`、`task_priority`、`resource_spec_price` | `custom_domain`、`shm_gi`、`model_source`、`is_publicpath_readonly`、`enable_auto_scaling`。**`queue_id` / `dataset_info` / `envs` / `scale_status` 被拒且没有对应物** |

### 不能靠直觉推的细节

| 项 | 事实 | 后果 |
| --- | --- | --- |
| `envs` 元素形状 | 是 `{name, value}`，不是 `{key, value}`（`key` 被拒为 `unknown field`） | **`train.GetJob` 的读投影不回显 `envs`**（容器里明明有变量，读回来是 `[]`），所以不能用读接口核对写了什么 |
| `is_projectuserspath_readonly` | 只有 notebook 有，而且要项目 Maintainer | 普通成员传了会拿到 `AccessForbidden: only project maintainer can enable project users path readonly mount` |
| HPC 的最大运行时长不在顶层 | `max_running_time_ms` / `_minutes` 打顶层都是 `unknown field`；它嵌在 `sbatch_script` 里，控制台**同时发两份**：`job_max_time`（`"D-HH:MM:SS"`，即 Slurm `--time`）和 `max_running_time_days` / `_hours` / `_minutes` | `slurm_cluster_spec` 一个都不收 |
| HPC 的优先级键 | 是 `priority`，不是 `task_priority`，而且必填 | 拼错时答 `priority must be set`（读起来像值缺失而不是名字写错）；**整个键不发**答 `InternalError`——在 transient 名单上，先白烧三次重试再抛出一条像平台故障的错。CLI 宁可自己先报「拿不到优先级」 |
| `sbatch_script.memory_per_node` 是死字段 | 而且比 `working_dir` 更能骗人：**它完整 round-trip**——平台收下、存住、详情页显示「每节点使用内存：8G」、`GetJob` 原样读回。但**脚本生成器只会写 `--mem-per-cpu`**，只给 `memory_per_node` 时详情页那一行是空的 `#SBATCH --mem-per-cpu=`，sbatch 拒掉整个脚本，任务 FAILED 且没有任何日志或事件说明原因 | 同一个 16 GiB 节点上 8 / 15 / 16 GiB 三个值全败，等价的 `memory_per_cpu` 成功；两个字段同发是 `InternalError`。**round-trip 通过不足以判定一个字段可用——要一路核到平台真正拿它去生成什么** |
| Slurm 级字段与节点级规格之间没有校验 | 两边都没有。`sbatch_script` 描述程序怎么用节点，`slurm_cluster_spec` 决定买了什么节点，平台照单全收并返回 `job_id`；控制台也不管 | 实测（`HPC-可上网区资源-2`，`0,4,16`，单节点）：`cpus_per_task` 超过节点核数、或 `cpus_per_task × memory_per_cpu` 超过节点内存 → 一两分钟内 FAILED，**`GetJobLog` 空、`ListJobEvents` 只有正常的 Pod 生命周期，没有一处带上 sbatch 的拒绝原因**；而 `number_of_tasks × cpus_per_task` 超过 `instance_count ×` 节点核数时 sbatch 反而**收下**，step 永远排队，平台一直报 RUNNING、`steps` 停在 `-/1`。**守门只能放在客户端** |
| 时间类字段是字符串 | `max_running_time_ms` / `reserve_on_fail_ms` / `reserve_on_success_ms` 发数字被直接拒 | — |
| `mount_path` / `mounts` 是死字段 | 接受、存储、不生效。元素是 `{real_path, mount_path, volume}`，没有 `read_only`。受控验证里三种 `volume` 写法全部被接受并原样存进 `start_config.mount_path`，但实例起来后 `/mnt/` 是空的，`find / -name 'probe-*'` 零命中。控制台侧也对得上：notebook / train 表单没有任何自定义挂载入口 | **结论不是「契约未知」，是「这个字段当前没有消费者」** |
| `hpc` 的 `working_dir` 同类 | `CreateJobConsole` 接受它，但 `GetJob` 读回是 `None`——平台连存都没存。对照组是同一次请求里的 `dataset_info` / `description` / `ttl_after_job_finish_seconds`，三个都完整 round-trip | **写进去读不回来，就不要接** |
| `train_enable_*` 是 Workspace 能力开关 | 不是可传的参数。`GetTrainScheduleConfig` 返回 `train_enable_pre_check` / `_troubleshoot` / `_specified_nodes` / `_slow_detect` / `_vccl`，控制台据此决定渲染哪些控件。`enable_slow_detect` / `enable_vccl` 虽被 `CreateJobConsole` 接受，但表单里没有对应控件 | **判断某个字段该不该接时，先看这组开关，再看控制台是否真的渲染了控件，两者缺一不可** |
| `specified_nodes` | discovery 的 `train.CreateJob` 创建 Schema 明确声明 `string[]`，`GetJob` 原样回显；`train_enable_specified_nodes` 决定 Workspace 是否开放。CLI 顶层发送，不塞进 `framework_config`，与 `exclude_nodes` 重名时本地拒绝 | 把它当成 `--nodes` 会混淆“要几个节点”和“必须落在哪些节点”；能力位关闭时发送没有可靠语义 |
| `notebook` 的第二个 `flatten` | `flatten_mode`（`FLATTEN_OFF` / `FLATTEN_ON` / `FLATTEN_AUTO`，控制台标签「停机自动保存时压平镜像」）在合同里——三个取值逐个探过都过了 proto 解析——但**控制台里这组单选带 `disabled`**，平台侧没放开 | 和 `train_enable_troubleshoot` 同类，刻意不接。它管的是「停机自动保存」这条独立链路，**不是 `notebook save-image --flatten`** |
| `resource_spec_price` 的形状 | 嵌套 proto 风格对象 `{cpu_type, cpu_count, gpu_type, gpu_count, memory_size_gib, logic_compute_group_id, quota_id}`；CPU 档位省略 `gpu_type`。由 `quota_id` 那一行的原始 price 对象构造，来源是 [8.11](#811-resource-price--配额目录-) | — |
| 镜像一律发注册表 URL 或 `mirror_id` | 不发可见名，否则 `无法找到对应镜像`。`image_type` 取 `SOURCE_PUBLIC` / `SOURCE_PRIVATE` / `SOURCE_OFFICIAL` | — |
| Shared Memory 是实例级资源 | `shm_gi`（train / ray）和 `shared_memory_size`（notebook）不能超过所选 Quota 的实例内存 | — |
| 可选字段一律「不传就不出现在 body 里」 | 包括两个只读挂载开关——即使只读是更安全的值 | 一个没有指定该选项的创建请求必须与该选项存在之前**逐字节相同**，默认值由平台自己决定 |

---

## 10. `/api/v2` 里不是 Action 的那部分

网关有两种形状（第 6 章）。CLI 用到 REST 那一半的两处，都不是「还没接完」，而是本来就装不进 `?Action=` + JSON 信封里：一个是双向流，一个是反向代理。

| 路径 | 是什么 | 状态 |
| --- | --- | --- |
| `/api/v2/train_job/remote_cmd` | 训练任务的 PTY WebSocket | ✅ `job shell` |
| `/api/v2/hpc_jobs/instances/exec` | HPC 实例的 PTY | ✅ `hpc shell` |
| `/api/v2/ray_job/instances/exec` | Ray 实例的 PTY | ✅ `ray shell` |
| `/api/v2/inference_servings/instances/exec` | Serving 实例的 PTY | ✅ `serving shell` |
| `/api/v2/notebook/lab/{}/` | JupyterLab 入口 | ✅ `notebook ssh` / `proxy-url` 全链路的起点 |
| `/api/v2/notebook/code/{}/`、`/api/v2/notebook/open/inspire_code/{}` | VS Code 与 inspire_code 的同类入口 | 未接 |
| `/api/v2/notebook/events/{}` | Notebook 事件流 | 未接（事件走 `notebook` 的 Action） |
| `/api/v2/file/list` / `create_dir` / `delete` / `update_name`、`/api/v2/file/download/{}` | 文件页的目录操作与下载 | 未接 |
| `/api/v2/logs/ray_job/download`、`/api/v2/logs/inference_serving/download` | 日志下载 | 未接 |
| `/api/v2/project/upload_appendix`、`/api/v2/project/{}/download_appendix/{}` | 项目附件 | 未接 |
| `/api/v2/audit/{}/upload_appendix`、`/api/v2/audit/{}/download_appendix/{}` | 审批附件 | 未接 |
| `/api/v2/model/{}/version/{}/publish/upload`、`/api/v2/billing/detail/export` | 模型发布上传、账单导出 | 未接 |

**这几条只能拿一个自己的运行中任务去握手验证。** 网关在路由之前先鉴权：普通 GET、带 `Referer` 的 GET、真实的 WebSocket 握手，三种打法对一条确知可用的路径和一条随手编的路径回的都是同一个 401，所以第 7 章那套「按报错区分」的判据在这里整个失效。

### 实例 PTY

四条共用控制台里同一个 URL 构造器，参数走 query string，进容器执行 `command -v bash >/dev/null 2>&1 && exec bash || exec sh`，改窗口大小发 `stty columns N rows M`。构造在 [`job_shell.py`](../../cli/inspire/cli/utils/job_shell.py)，不属于 JSON Action Wrapper。

| Workload | 路由 | 句柄参数 | 实例参数 |
| --- | --- | --- | --- |
| `job` | `train_job/remote_cmd` | `job_id` | `instance_name` |
| `hpc` | `hpc_jobs/instances/exec` | `job_id` | `instance_id` |
| `ray` | `ray_job/instances/exec` | `job_id` | `instance_id` |
| `serving` | `inference_servings/instances/exec` | **`inference_serving_id`** | `instance_id` |

**两个参数名逐 Workload 重映射，用错的两种失败都不给报错：**

| 组合 | 表现 |
| --- | --- |
| `hpc` + `instance_name` | socket 照常 upgrade，然后**一个字节都不回**——没有报错、没有 close 帧，只是一个永远不说话的 shell（实测 `instance_id` 回 53 字节，这边回 0） |
| `ray` / `serving` + 错的键 | 握手被拒，回一个光秃秃的 `HTTP/1.1 200 OK` 而不是 101 |

Serving 连句柄都不叫 `job_id`。所以控制台那个按 Workload 重映射参数的动作是必需的，不是风格；`build_remote_cmd_ws_url` 对表里没有的 Workload **直接抛错而不是按类比猜**。

### Notebook Lab 与反向代理

```
{base_url}/api/v2/notebook/lab/{notebook_id}
```

GET 它答 `301`，Location 是带 token 的网关地址 `https://<gateway>/ws-…/project-…/user-…/jupyter/<notebook>/<token>/lab`，跟过去就是 JupyterLab 本体。控制台自己的 JupyterLab iframe 指的就是这条路径。CLI 走 `_resolve_direct_lab_url` 跟这一跳，`build_jupyter_proxy_url` 再从**跳完的结果**上派生代理地址。

| 项 | 事实 |
| --- | --- |
| `/proxy/{port}/` 只在带 token 的网关地址上有效 | 平台域上的 `{base_url}/api/v2/notebook/lab/{id}/proxy/{port}/` 返回 `404 page not found`。**不存在免 token 的形式** |
| token 段是敏感信息 | `rtunnel.py` 的 `redact_proxy_url` 专门负责在日志与报错里抹掉它 |
| 消费者 1 | `inspire notebook proxy-url --port N`——**返回**容器里那个 HTTP 服务（TensorBoard、Gradio、Streamlit、推理端点）的外部地址，不打开任何东西。H100 / H200 受限 Notebook 上默认拒绝，要 `--allow-restricted` |
| 消费者 2（关键） | **整条 SSH 链路都架在它上面**：受限环境不接受直连，所以在容器里起一个 sshd（默认 22222），再通过这条 HTTP 代理去够它——`_wait_for_rtunnel` 轮询的就是这个带 token 的地址。`notebook ssh` / `scp` / `ssh-config` 以及外部 OpenSSH 工具能用全靠它 |
| 没有 Action 对应物 | `GetNotebookLab` / `GetLabUrl` / `GetNotebookProxy` / `GetProxyUrl` 均 `InvalidAction`。唯一沾边的 `GetNotebookAccessUrl` 语义不同（给的是 IDE 网关地址，见 [8.4](#84-notebook--交互式建模)），**故意不接** |
| 与账号级 SSH 公钥无关 | 平台用户中心的公钥注册表管的是账号级公钥；rtunnel 读的是本机 `~/.ssh/*.pub` 并直接注入容器 |

---

## 11. 数据广场 `aip.sii.edu.cn`

**数据广场（上海创智学院数据广场）不是启智的一部分**：不同 host、不同 API 风格、不同 Session，只共用同一套 CAS SSO。启智控制台侧边栏的「数据集」是外链过来的，`qz` 那侧只有 [`dataset.ValidateDataset`](#813-dataset--官方数据集挂载-) 负责把某个版本挂进容器，**检索、目录、版本和权限全在这边**。实现在 [`plaza/`](../../cli/inspire/platform/web/plaza/)。

**两套标识符必须分清**：

| 标识符 | 是什么 | 出现在哪 |
| --- | --- | --- |
| `datasetCode` / `versionCode` | 用户可见身份 | 挂载 API 接受的值、容器路径的组成部分、CLI 唯一展示的东西 |
| `datasetId` / `versionId` | 数据广场内部句柄 | `findDatasets` 需要它；拿去挂载会被启智拒为「数据集不存在」，所以只活在 resolver 内部 |

### 握手

纯 HTTP，三步，不需要浏览器——CLI 的 Web Session 里已经有 CAS 的 ticket-granting cookie：

```
1. GET  https://cas.sii.edu.cn/cas/login?service=<urlencode("https://aip.sii.edu.cn/")>
        带现有 Session 的 CASTGC → 302，Location 上带 ?ticket=ST-…     （必须 allow_redirects=False）
2. POST https://aip.sii.edu.cn/api/base/login   {"ticket": "…", "service": "https://aip.sii.edu.cn/"}
        → 下发 `datasets-session` cookie，body 里带 userInfo
3. 之后的调用带该 cookie；前端还会发 `x-user-id: <userInfo.ID>`，实测不带也通
```

| 项 | 事实 |
| --- | --- |
| CAS 登录路径 | 是 `/cas/login`，不是 `/login`（后者 404） |
| 第 1 步必须 `allow_redirects=False` | 否则 ticket 被 SPA 首页消费掉 |
| 拿不到 ticket | 说明 **CAS 不认这个 cookie**，也就是平台 Session 过期了，不是数据广场的问题 |
| 缓存策略 | 握手只有两次请求，所以签入后的客户端**只在进程内缓存**，按 `(账号, Session 创建时间)` 作键；刻意不落盘，避免留下过期或跨账号的状态 |

### 信封与错误

```jsonc
{"code": 0, "data": {…}, "msg": "…"}
```

**不是 AWS 风格**，`code != 0` 即失败，`msg` 是原因。判定顺序与启智那侧一致：**限流与服务端故障先于读 body**（限流回的是错误页而不是 JSON 信封），然后 3xx / 401，最后才看 `code`。

| 项 | 事实 |
| --- | --- |
| HTTP 状态码基本无意义 | **唯一有意义的是 401**：未登录返回 `401 {"code": 7, "msg": "未登录或非法访问"}`，这是重新握手的信号 |
| `code: 7` 有歧义 | `findDatasets` 的失败和未登录共用它：坏 `datasetId` 是 **HTTP 200** + `查询失败:record not found`，未登录是 **HTTP 401**。**判断要不要重新握手只能看 HTTP 状态码，不能看 `code`** |
| 401 时的续期顺序 | 重新握手 → 还不行就 `get_web_session(force_refresh=True)` 重建平台 Session 再握手。重新登录很贵，而多数过期只是数据广场自己的 |
| 限流 | 走与启智同一套 `TransientAPIError` 退避重试 |

### 已封装的端点

| 端点 | 方法 | 输入 | 输出（`data` 内） | CLI |
| --- | --- | --- | --- | --- |
| `/api/datasets/getDatasetsList` | GET | query：`page`、`pageSize`、`keyword?`、`tags?` | `{list[], total, page, pageSize}`（同层） | `dataset list`，以及按 code 定位的 resolver |
| `/api/datasets/findDatasets` | POST | `{datasetId}` | 详情 + `versions[]` | `dataset show` |
| `/api/datasetTags/getDatasetTagsList` | GET | query：`pageSize`（前端发 999） | `{list[]}` | `dataset tags`，以及 `--tag` 名字→handle 解析 |
| `datasetApplyApprove/getDatasetApplyList` | GET | `page`、`pageSize`、`keyword?` | 我提交的申请：`id, datasetName, authorityName, applyTime, approveTime, approveUser, applyDescr, state` | `dataset applications` |
| `datasetApplyApprove/getDatasetApproveList` | GET | 同上加 `role?` | 待我审批，行另含 `applyUser, projectName` | `dataset applications --to-approve` |
| `datasetApplyApprove/intoApproveById` | GET | `id` | 单条审批详情，含 `datasetCode`；id 不存在时 HTTP 200 + `code != 0`、`msg` 为 `申请记录不存在` | `dataset applications <名字>` |

| 项 | 事实 |
| --- | --- |
| `tags` 是逗号连接的数字 tagId，语义 OR | **空字符串不是通配符**，`tags=""` 返回 0 行。前端在没有选中标签时把这个键整个删掉，客户端也必须省略 |
| `pageSize` 无上限 | 1000 一次取回全部；`page` 越界返回空 `list` 但 `total` 仍然正确 |
| `keyword` 连描述一起匹配 | 且不区分大小写，命中范围通常比预期宽——**按 code 定位必须在结果里取精确相等的那一行，不能取第一条**。短 code 可能被别的数据集描述里的散文挤到后面，此时按 `total` 再取一次全量而不是逐页走 |
| 单位与类型 | `filesSize` 单位是 **MiB**；`dataFormats` 是一个 **JSON 编码的字符串**，不是数组 |
| `hasPermission` 按账号给 | 为 false 的数据集挂载时报 `2005 无访问权限`；**申请权限只有网页端有入口**，CLI 只能如实报出这一列 |
| `super` 是数据分级 | `S1` 保密 / `S2` 有限访问控制 / `S3` 学院内部 / `S4` 公开。当前账号上 `hasPermission` 完全是它的函数（532 条里 S4 261 条与 S3 165 条全可挂，S2 106 条全不可，S1 看不到）——但**这是账号级的授权结果，不是平台合同**，判断能不能挂仍要读 `hasPermission` |
| `state` 有四个值 | `active` / `wanted` / `processing` / `error`，版本另有 `downloading` / `pending_upload` |
| 名字唯一性 | `datasetCode` 全表唯一，标签名也唯一，两个 name→handle 映射都不会歧义 |
| 版本号不保证是 `vN` | 实际存在 `v1-br`、`2026-07-30`、`v3again`，**不要按序号猜** |
| 标签分类 `categoryId` | `0` / `1` = 文本（前端把 0 折进文本，客户端照做）、`2` = 图像、`3` = 音频、`4` = 视频、`5` = 多模态 |
| 申请单的 `state` | `0` 待审批 / `1` 已通过 / `2` 已驳回 / `-1` 已撤回。**`0` 是假值**，用「取真值否则默认」的写法解析会让每一条待审批都渲染成空 |
| 申请单投影未 live 验证 | 当前账号三个 GET 都回 `total: 0`（正常空态），只有 `intoApproveById` 的错误路径是实测的 |

### 存在但未封装

| 端点 | 为什么不接 |
| --- | --- |
| `datasetApplyApprove/datasetApply`（POST） | 会以用户的名义触达真人审批者。将来若要接：除 `datasetId` 外还要 `projectId`，而**广场侧的项目句柄在 CLI 表面完全不存在**，等于要新引入一个广场 resolver（来源 `project/getProjectListByUser`） |
| `datasetApplyApprove/datasetApprove`（POST） | 同上。**一个端点三用**：`1` 同意、`2` 驳回、`-1` 申请人撤回；受控验证要覆盖三种 `state`，而其中两种会作用在别人提交的申请上 |
| `datasetUserRole/createDatasetUserRole`（POST） | 赋权，同上 |
| `getDatasetsListUserCenter` | 查过、刻意不接：它是 owner / 角色过滤而不是「我项目下的数据集」（某项目主目录 267 行、这里 0 行），对不持有任何数据集角色的普通账号**恒为空**；行字段与主目录**存在别名差异**（SPA 自己在做 `datasetId\|\|id` 的归一化），当前账号取不到行，投影无法验证；它是写操作视图的读取面，全部超出 CLI 动线 |
| `getDatasetsWithVersions` | 不带参数即返回全目录的 `[{datasetCode, versions:[{versionCode, versionId}]}]` 扁平索引（**不是信封分页，`data` 直接是数组**）。一次请求拿到全部 code→版本映射——**若以后要做 `<名字>:<版本>` 的本地预校验，这比现在的 resolve + `findDatasets` 两步便宜** |
| 其余只读 | `checkHuggingFaceDataset`、`datasetLicenses/{list,detail,download,delete}`、`datasetForks/getDatasetForksList`、`sftpgo/user/files`、`project/{getProjectListByUser,findProject}` |
| 其余写操作 | `createDatasets`、`createDatasetVersion`、`updateDatasets`(PUT)、`updateDatasetsValue`、`deleteDatasets`、`checkDatasetsName`、`datasetVersion/{updateDatasetVersion,deleteDatasetVersion,confirmComplete}`、`datasetUserRole/*`。CLI 不创建也不编辑数据集，这一族没有动线 |

---

## 12. 输出边界

| 规则 | 内容 |
| --- | --- |
| 原始响应不得穿透 | 平台原始响应不得直接进入公共输出。命令层必须先解析、投影和清洗，Human 与 JSON 输出使用显式 Allowlist |
| 稳定身份只有 Name 和 Alias | 不透明句柄（`ws-`、`project-`、`lcg-`、`quota_id`、`mirror_id`、`notebook_id`、`job_id`）只存在于 `browser_api/` 和 Session 层 |
| 唯一的例外 | `notebook proxy-url` 走 `format_json(…, preserve_raw={"url"})` 打印完整网关 URL——这个地址的每一段都是平台句柄，默认的 `scrub_raw_ids` 会把它整条洗成 `<redacted>`，洗完就不通了。**代价必须说清楚：这个地址等同于凭据**，内嵌的短期 token 让持有者对该 Notebook 的访问权与你相同，而它会进 Agent 对话记录和 shell 历史 |
| 明文凭据一律不出现 | `file.GetSftpgoConnectionInfo` 的 `auth`：不进日志、不进报错、不进 `--json`、不进文档 |
| 内嵌 id 的报错要折掉 | 例如 `Cannot save image of non-running notebook: <id>` |

## 13. 变更验收

改动 Browser API 至少完成：

1. Wrapper 只暴露调用方需要的最小归一化数据，响应解包走 `_v2_result()`，列表键显式声明。
2. 路由名经过两种写法实测，不是从 discovery `Name` 推导的。
3. Workspace scoping 放在 discovery 声明的位置，并确认返回的不是 scoping 造成的 `AccessForbidden`。
4. 命令使用 Name 输入，同名时提供可读候选与 `--pick`；Human 与 JSON 输出使用显式 Allowlist。
5. 请求失败与「平台返回空」在返回值上可区分，且有测试覆盖失败那一侧；扇出型 Wrapper 还要能表达「部分成功」。
6. 写操作经过受控验证，**成功以状态真的变了为准，不以响应信封为准**；接受的字段还要能读得回来。
7. 判某条平台能力「没有对应物」之前，第 7 章的探针跑完整了，并说清本次取样方法的盲区——discovery 缺一条路由不构成结论。
8. 对应命令 Help、Wrapper 测试和本页表格同步更新。

**未闭合的调查结果不进入本页。**
