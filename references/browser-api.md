# Browser API（`qz.sii.edu.cn` Web 前端用的 API）

> **状态**：非官方、无公开合约、平台侧可随时变更。本文档基于 InspireSkill CLI 侧 [`cli/inspire/platform/web/browser_api/`](../cli/inspire/platform/web/browser_api/) 的反向使用整理，是当前 `qz.sii.edu.cn` 前端仍在打的那套路径。任何变动以 `inspire --debug <cmd>` 观察到的实际流量为准。

## 为什么同时存在 Browser API 和 OpenAPI？

平台对外给了两条调用链路，**覆盖面差距很大**：

| 链路 | Prefix（默认） | 认证 | CLI 侧封装 | 对外承诺 |
| --- | --- | --- | --- | --- |
| **OpenAPI** | `/openapi/v1` | Bearer token (username/password → `/auth/token` 换得) | [`platform/openapi/`](../cli/inspire/platform/openapi/) | 公开合约，**仅 7 条端点**：train_job / hpc_jobs 的 `create / detail / stop` + `cluster_nodes/list` |
| **Browser API** | `/api/v1` | 前端 SSO session cookie（Keycloak），需要 `Referer` 指向对应页面 | [`platform/web/browser_api/`](../cli/inspire/platform/web/browser_api/) | 非公开，但**暴露得比 OpenAPI 全得多** —— 列任务 / 事件查询 / 镜像 CRUD / 资源价格 / 计算组可用量都走这里 |

**经验法则**：

- 能在 OpenAPI 上做的就走 OpenAPI（`job create` / `hpc create` / `notebook status` 关键字段）—— 稳、无头浏览器开销。
- **观测性接口**（列表 / 事件 / 可用量 / 预算 / 镜像管理）**只能走 Browser API**。CLI 里 `inspire job list` / `inspire hpc list` / `inspire project list` / `inspire image *` / `inspire resources *` 全部走 Browser API。
- 探测 OpenAPI 是否存在某端点：CLI 会 404，这时回头看 Browser API 有没有。

## 认证模型

Browser API 拿不到 Bearer token —— 它是前端 JS 打的，带的是浏览器 SSO cookie（Keycloak 侧下发）。CLI 里 [`inspire/platform/web/session.py`](../cli/inspire/platform/web/session.py) 用 Playwright 无头浏览器走一遍 Keycloak 登录拿到 session，之后所有请求都用这个 session。

关键细节：
- 每次请求**必须带 `Referer`**，指向该端点对应的前端页面（如 `/jobs/distributedTraining`、`/jobs/interactiveModeling`）。没 Referer 或 Referer 错了会被后端拒。
- 需要代理。`INSPIRE_PLAYWRIGHT_PROXY` 对 Keycloak 登录生效，`INSPIRE_REQUESTS_HTTP(S)_PROXY` 对后续 XHR 生效。
- Base URL 从 `[api].base_url` 读（默认 `https://qz.sii.edu.cn`）；前缀从 `[api].browser_api_prefix` 读（默认 `/api/v1`），可被 `INSPIRE_BROWSER_API_PREFIX` 覆盖。

## 端点清单

下面列出 CLI 当前在使用的 Browser API 端点，按域分组。`{prefix}` 默认 `/api/v1`。

### 用户 / 权限

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `GET` | `{prefix}/user/detail` | 当前登录用户详情 | `browser_api.jobs.get_current_user`；`inspire user whoami` |
| `GET` | `{prefix}/user/routes/{workspace_id}` | 探一个 workspace 能不能走（切 workspace 用） | `browser_api.workspaces.select_workspace` |
| `GET` | `{prefix}/user/{user_id}` | 按 ID 查其他用户（前端 avatar / 用户名展示用） | —— |
| `GET` | `{prefix}/user/list` | 用户搜索列表 | —— |
| `GET` | `{prefix}/user/permissions/{workspace_id}` | **每页都打**的权限矩阵（返回 `{permissions: ["job.notebook.create", ...]}`，平铺权限码；历史上是 dict 形态，CLI 兼容两种）。前端按它渲染按钮灰化 | `browser_api.users.get_user_permissions`；`inspire user permissions` |
| `GET` | `{prefix}/user/my-api-key/list` | 当前用户的 API Key 列表 metadata（值只在创建时返回） | `browser_api.users.list_user_api_keys`；`inspire user api-keys` |
| `GET` | `{prefix}/user/quota` | 用户配额详情 | `browser_api.users.get_user_quota`；`inspire user quota` |

### 项目

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/project/list` | 列项目 + 剩余预算 + 优先级（实测接受带 `filter` 的 body，**不是完全空 body**；直接传 `{workspace_id:...}` 会被 proto 拒） | `browser_api.projects.list_projects`；`inspire project list` |
| `POST` | `{prefix}/project/list_v2` | 带 `workspace_id + check_admin` 过滤的 v2 list，多数 notebook / 训练相关页面在用 | —— |
| `POST` | `{prefix}/project/list_for_page` | 首页分页版（返回 `{items, total}`，字段更全：budget / children_budget / description / en_name / priority） | —— |
| `GET` | `{prefix}/project/{project_id}` | 项目详情（预算 / 子项目 / 创建人 / 优先级） | `browser_api.projects.get_project_detail`；`inspire project detail` |
| `GET` | `{prefix}/project/owners` | 项目 owner 清单（建任务时的负责人下拉） | `browser_api.projects.list_project_owners`；`inspire project owners` |

### 工作空间 (Workspace)

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/workspace/list` | 列所有 workspace（空 body）。返回 `{items, total}`。前端左上 workspace 切换器用 | —— |

### Notebook

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/notebook/create` | 建 notebook 实例 | `browser_api.notebooks.create_notebook`；`inspire notebook create` |
| `POST` | `{prefix}/notebook/operate` | **只启停，不删除**。body 字段是 `operation`，enum 实测只认 `START` / `STOP`——`DELETE` / `REMOVE` / `DESTROY` / `TERMINATE` / `KILL` / `ARCHIVE` 等 proto 一律拒绝（`code:100002 invalid value for enum field operation`）。删除不走这条，走下一行的 REST DELETE。 | `browser_api.notebooks.start_notebook` / `stop_notebook`；`inspire notebook start/stop` |
| `DELETE` | `{prefix}/notebook/{id}` | 永久删 notebook 条目（REST 风格，与 `DELETE /image/{id}` 同构）。空 body。2026-04-21 实测返回 `code:0 success`。destructive——UI 里的条目也一并消失。 | `browser_api.notebooks.delete_notebook`；`inspire notebook delete` |
| `POST` | `{prefix}/notebook/list` | 列 notebook。body 含 `workspace_id / page / page_size / filter_by:{keyword, user_id[], logic_compute_group_id[], status[], mirror_url[]} / order_by` | —— |
| `POST` | `{prefix}/notebook/users` | 当前 workspace 里用过 notebook 的用户（共用配额时判谁占着；与 `train_job/users` 对称） | —— |
| `GET` | `{prefix}/notebook/{id}` | notebook 详情（状态 / 镜像 / 资源） | `browser_api.notebooks.get_notebook_detail`；`inspire notebook status` |
| `GET` | `{prefix}/notebook/status?notebook_id={id}` | 轻量状态探查（只返回状态字段） | —— |
| `POST` | `{prefix}/notebook/events` | notebook 级生命周期时间轴（调度 → 镜像拉取 → 启动 → 停止 / 保存 / 推送）。body: `{notebook_id, page, page_size}`，返回 `{list, total}`。**注意事件结构和 train/HPC 不同**：只有 `content` 字段装文本 + `created_at` 时间戳，没有 K8s 原生的 `type`/`reason`/`from`。CLI 的 `list_notebook_events` wrapper 会把 `content` 同步到 `message`、把 `created_at` 同步到 `last_timestamp` 方便共用 `cli.utils.events` 渲染器 | `browser_api.notebooks.list_notebook_events`；`inspire notebook events` |
| `POST` | `{prefix}/lifecycle/list` | notebook 生命周期状态转换记录。body: `{notebook_id, page, page_size, start_time, end_time}`。**实测在 2026-04 的平台上对普通 notebook 经常返回 `{list:[], total:0}`** —— 网页的"生命周期"tab 实际是靠 `/run_index/list` 画的，这个端点只为未来可能恢复的用法保留 | `browser_api.notebooks.list_notebook_lifecycle`（thin wrapper） |
| `POST` | `{prefix}/run_index/list` | notebook 运行次数 / 每次运行的起止时间（body: `{notebook_id}`，返回 `{list:[{index, start_time, end_time}], total}`；当前正在运行的 `end_time=""`）—— 网页"生命周期"tab 就是用这个端点拼每行"第 N 次运行 / 时长"的 | `browser_api.notebooks.list_notebook_runs`；`inspire notebook lifecycle` |
| `POST` | `{prefix}/resource_prices/logic_compute_groups/` | compute group 单价 | `browser_api.notebooks.list_resource_prices` |

> **已失效**：`GET {prefix}/notebook/{id}/events` 和 `GET {prefix}/notebook/event/{id}` 两个旧路径在 2026-04 平台升级后全部 404，已由上表的 `POST {prefix}/notebook/events` 替代。另外 `POST {prefix}/notebook/compute_groups` 也同时被移除 —— CLI 现在用 `logic_compute_groups/list`（见 [资源 / 计算组](#资源--计算组)）代替。

### Image

`/image` 前缀下是镜像生命周期，和 notebook / job 共享。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/image/list` | 按 `source` / `visibility` / `registry_hint` 筛镜像 | `browser_api.images.list_images_by_source`；`inspire image list --source {public,private,official,all}` |
| `GET` | `{prefix}/image/{image_id}` | 镜像详情 | `browser_api.images.get_image_detail`；`inspire image detail` |
| `POST` | `{prefix}/image/create` | 注册外部镜像地址 | `browser_api.images.create_image`；`inspire image register` |
| `POST` | `{prefix}/mirror/save` | 把运行中的 notebook 存成私有镜像 | `browser_api.images.save_notebook_as_image`；`inspire image save` |
| `DELETE` | `{prefix}/image/{image_id}` | 删镜像 | `browser_api.images.delete_image` |
| `POST` | `{prefix}/image/update` | 更新镜像元数据（描述 / 可见性等）。**body 字段名未解出** —— 直接 `{image_id:...}` 会被 proto 拒，需以 UI 实际请求为准 | —— |

### 训练任务 (Train Job)

OpenAPI 这侧只有 `train_job/{create,detail,stop}`。**`list` 和事件都只有 Browser API 有。** 此外 Browser API 自己也重复暴露了 `detail`，前端详情页在用。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/train_job/list` | 列训练任务 | `browser_api.jobs.list_jobs`；`inspire job list` |
| `POST` | `{prefix}/train_job/delete` | 永久删训练任务条目（destructive；**OpenAPI 无对应端点**）。body `{"job_id": <id>}`——2026-04-21 实测成功（`code:0`）；注意这是 train_job 域里唯一一个 POST-delete，notebook / hpc 那边都是 REST `DELETE /<res>/{id}`。 | `browser_api.jobs.delete_job`；`inspire job delete` |
| `POST` | `{prefix}/train_job/detail` | Browser API 侧详情（与 OpenAPI `/openapi/v1/train_job/detail` 平行，返回字段一致）。前端 `/jobs/distributedTrainingDetail/{id}` 页在用 | —— |
| `POST` | `{prefix}/train_job/users` | 当前 workspace 里谁在用资源（共用配额时判谁占着） | `browser_api.jobs.list_job_users` |
| `POST` | `{prefix}/train_job/workdir` | 任务的 train_job_workdir 字段 | `browser_api.jobs.get_train_job_workdir` |
| `POST` | `{prefix}/train_job/job_event_list` | **Job-level K8s 事件**（body: `{jobId:<id>}`；`Unschedulable` / `Pulling` / `Started` / `FailedCreate` / `SetPodTemplateSchedulerName` 等）。返回字段含 `type`/`reason`/`message`/`from`/`first_timestamp`/`last_timestamp`/`object_id`/`object_type`/`age`。 | `browser_api.jobs.list_job_events`；`inspire job events <id>`（带本地缓存到 `~/.inspire/events/<id>.events.json`） |
| `POST` | `{prefix}/train_job/instance_list` | 该任务的 pod 实例 | body: `{jobId, page_num, page_size}` |
| `POST` | `{prefix}/train_job/events/list` | **Per-instance 事件**（按 pod 名查询）。body 形如 `{page_num, page_size, filter:{object_type:"instance", object_ids:[<pod>], start_last_timestamp, end_last_timestamp}}`。返回 scheduler / kubelet 视角事件（`FailedScheduling`/`Scheduled`/`Pulled`/`Started`），对诊断具体调度失败原因更有用 | `browser_api.jobs.list_job_instance_events`；`inspire job events <id> --instance <pod>` |
| `POST` | `{prefix}/logs/train` | Train job 聚合日志（按 podNames + 时间窗）。body 形如 `{page_size, filter:{podNames:[...], start_timestamp_ms, end_timestamp_ms}, sorter:[{field:"time",sort:"descend"}]}` | Web 前端 "聚合日志 → 日志" 子 tab |

### HPC 任务

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/hpc_jobs/list` | 列当前 workspace 的 HPC 任务 | `browser_api.hpc_jobs.list_hpc_jobs`；`inspire hpc list` |
| `DELETE` | `{prefix}/hpc_jobs/{id}` | 永久删 HPC 任务条目（REST 风格，与 `DELETE /notebook/{id}` 同构；destructive；**OpenAPI 无对应端点**）。空 body。2026-04-21 实测返回 `code:0 success`。注意：`POST /hpc_jobs/delete` 返 404——前端就是走 REST DELETE。 | `browser_api.hpc_jobs.delete_hpc_job`；`inspire hpc delete` |
| `GET` | `{prefix}/hpc_jobs/{job_id}` | HPC 任务详情（RESTful 路径；**注意**：不是 `hpc_jobs/detail` + body，那是 OpenAPI 的形态） | Web 前端 `基本信息` tab |
| `POST` | `{prefix}/hpc_jobs/events/list` | **HPC job-level 事件**。body: `{pageNum:-1, pageSize:200, filter:{object_ids:[<job-id>], object_type:"HPC_JOB"}, sorter:[{field:"last_timestamp", sort:"ascend"}]}`。注意顶层 camelCase（`pageNum`/`pageSize`），filter 内 snake_case。返回字段含 `reason`/`message`/`from`/`first_timestamp`/`last_timestamp`/`event_timestamp`/`age`/`object_id`/`object_type`；**不含 `type`**（区别于 train_job 事件）。**实测 `object_type:"instance"` 对 HPC 所有 pod 种类都返回空**——平台没暴露 HPC per-pod 事件，CLI 也没给这条路。 | `browser_api.hpc_jobs.list_hpc_job_events`；`inspire hpc events <id>`（带本地缓存） |
| `POST` | `{prefix}/hpc_jobs/instances/list` | 该 HPC 任务的 pod 实例（launcher / slurmctld / slurmd / worker） | body: `{jobId, page_num, page_size}` |
| `POST` | `{prefix}/logs/hpc` | HPC 聚合日志（按 podNames + 时间窗）。body 形如 `{page_size, filter:{podNames:[...], start_timestamp_ms, end_timestamp_ms}, sorter:[{field:"@timestamp",sort:"descend"}]}`。注意排序字段是 ElasticSearch 风格的 `@timestamp`（train 那侧是 `time`） | Web 前端 "聚合日志 → 日志" 子 tab |

### 资源 / 计算组

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/logic_compute_groups/list` | 列 workspace 下所有 logic compute groups（带 GPU 型号 / 机房） | `browser_api.availability.list_compute_groups` |
| `GET` | `{prefix}/compute_resources/logic_compute_groups/{group_id}` | 某个 compute group 的实时可用量 | `browser_api.availability.get_accurate_resource_availability`；`inspire resources list` 的底层 |
| `POST` | `{prefix}/cluster_nodes/list` | 整节点空余 | `browser_api.availability.get_full_free_node_counts`；`inspire resources nodes` |
| `GET` | `{prefix}/cluster_nodes/workspace/{workspace_id}` | 按 workspace 维度的节点清单（带 `backup / fault / nodes[]`）。前端训练 / HPC list 页顶部的资源概览卡用它 | —— |
| `GET` | `{prefix}/logic_compute_groups/{group_id}` | **RESTful 版 compute group 详情**，返回 `{abnormal_node_count, compute_group_id, compute_group_name, gpu_type_stats[], logic_resources, node_count, ...}`。与上面 `/compute_resources/logic_compute_groups/{id}` 不同：这条偏静态描述，那条是实时可用量。**只接受 `lcg-` 前缀 ID**（`cg-` 前缀会 404） | —— |

> **注**：`cluster_nodes/list` 在 Browser API 和 OpenAPI 两边都存在，且字段一致 —— 但 Browser API 的返回更新得更即时，CLI 的 `inspire resources nodes` 默认走 Browser API。

### 模型 (Model)

**只读部分已封装**（`inspire model list/status/versions`）。创建 / 发版 / 删除平台侧留着但 CLI 不覆盖 —— 参数太多且 body 字段名与 UI 强绑定，目前只能走 `/modelLibrary` 页面。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/model/list` | 工作空间下的模型注册表。body: `{page, page_size, filter_by:{}, workspace_id}` | `browser_api.models.list_models`；`inspire model list` |
| `POST` | `{prefix}/model/detail` | 单个模型详情。body: `{model_id}`；返回 `{model, project_name, user_avatar, user_name}` | `browser_api.models.get_model_detail`；`inspire model status` |
| `GET` | `{prefix}/model/{model_id}` | **注意**：虽然看着是 detail，实际返回的是 `{list, next_version, total}` —— 这是该 model 的全部 **版本清单** | —— |
| `GET` | `{prefix}/model/{model_id}/versions` | 明确的版本清单端点（与上面等价；UI 两处都用） | `browser_api.models.list_model_versions`；`inspire model versions` |
| `POST` | `{prefix}/model/create` | 创建 / 注册模型。body 字段名未解出 | —— |

### 模型部署 (Inference Servings)

OpenAPI 有 `create / detail / stop` 3 条（见 [openapi.md](openapi.md) 第 3 节）；**列表 / 配置 / 用户 / 项目** 只能走 Browser API。CLI 组合二者暴露 `inspire serving list/status/stop/configs`；`create` 参数过多（`model_id / port / replicas / ...`）暂不覆盖。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/inference_servings/list` | 列部署。body: `{page, page_size, filter_by:{my_serving:true}, workspace_id}`；返回 `{inference_servings[], total}` | `browser_api.servings.list_servings`；`inspire serving list` |
| `POST` | `{prefix}/inference_servings/user_project/list` | 当前 workspace 下可用的项目 + 用户清单（建部署弹窗用）。body: `{workspace_id}`；返回 `{projects, users}` | `browser_api.servings.list_serving_user_project` |
| `GET` | `{prefix}/inference_servings/configs/workspace/{workspace_id}` | 该 workspace 的部署可用配置（镜像 / 规格等）。返回 `{configs}` | `browser_api.servings.get_serving_configs`；`inspire serving configs` |
| `GET` | `{prefix}/inference_servings/detail?inference_serving_id={id}` | 部署详情的 Browser API 形式（优先走 OpenAPI `POST /openapi/v1/inference_servings/detail`，`inspire serving status` 就在用这条） | `browser_api.servings.get_serving_detail`（备用）；`openapi.inference_servings.get_inference_serving_detail` |

### SSH 密钥

**CLI 目前不碰平台层 SSH key**（`inspire` 的 SSH 用户功能是在 notebook 内部起 dropbear，见 [cli/examples/setup_ssh_dropbear.sh](../cli/examples/setup_ssh_dropbear.sh)），但平台自带一套 SSH key 管理：

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/ssh/list` | 已添加的 SSH key 列表。body: `{page, page_size}` | —— |
| `GET` | `{prefix}/ssh/keys` / `{prefix}/ssh/my_keys` / `{prefix}/ssh/public_keys` | 同义的三个 GET 查询入口（实测都返回 `数据库错误, 请联系管理员` —— 后端似乎在调整中） | —— |
| `POST` | `{prefix}/ssh/create` | 添加 SSH key。body 字段名未解出 | —— |

### Jupyter / 终端代理

Browser API 还代理 Jupyter Lab 和 WebSocket 终端，用来 bootstrap SSH / rtunnel：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` / `POST` / `WS` | `{prefix}/notebook/lab/{notebook_id}/proxy/{port}/...` | 经由平台代理透传到 notebook 内部 Jupyter 服务的任意 HTTP / WebSocket 请求。`inspire notebook ssh` 的三级 bootstrap（Jupyter Contents API / terminal REST / Playwright）全走这条 |

## 如何自己看到这些流量

三种方式：

1. **`inspire --debug <cmd>`** —— CLI 会把脱敏过的 HTTP 流量写进 `~/.cache/inspire-skill/logs/`，含完整 URL / method / 响应摘要。
2. **浏览器 DevTools** —— 在 `qz.sii.edu.cn` 里打开 Network 面板，Filter `api/v1`。前端打哪些请求一目了然；一般是 POST + JSON body。
3. **Playwright 网络抓包脚本** —— 想系统性扫一遍所有端点时用。思路：加载 `~/.cache/inspire-skill/web_session-*.json` 里的 `storage_state` → 用 `page.on("request"/"response")` 装监听 → 程序化导航所有已知前端路由 → 对每个列表页点第一行（进 detail）、开 `+ 新建` 弹窗（**别点"提交"**，`Esc` 关闭）→ 导出 JSONL 做 diff。上面这张表的很多行（notebook/list、notebook/events、lifecycle/list、model/*、inference_servings/*、SSH、user/permissions 等）都是这样反向挖出来的。

## 稳定性 & 注意事项

- **不是公开合约**。平台前端迭代时可能改路径 / 字段名而不通知。`inspire update` 的主要职责之一就是跟进这些变更。近期例子：`GET /notebook/{id}/events` 和 `POST /notebook/compute_groups` 在 2026-04 悄悄下线（见 Notebook 小节末注）。
- **认证依赖 Playwright**。如果你的环境装不了 Chromium（headless 容器、严格沙盒），Browser API 这一整层就不能用；OpenAPI 那 7 条还能走。
- **rate limit**。平台侧有 nginx/openresty 层的速率限制（实测 ≥3 req/s 就可能拿到 `429`）。CLI 里几个 list 类端点做了退避重试；你自己写脚本打 Browser API 时要放一点 sleep。
- **Referer 要对**。每个端点在上面表格对应的 CLI 引用里都能找到它用的 Referer。如果自己 curl，别把 Referer 漏了或填成不相关的页面，会收到 400 / 401。
- **同名路径、不同含义**。`{prefix}/image/list` 在 Browser API 里的 filter 语义（`source` / `visibility` / `source_list`）**比 OpenAPI 富得多**，不能互相直接替换请求体。
- **Protobuf 字段校验严格**。后端在 APISIX 之后用 protobuf 做请求体校验。常见 400：`proto: (line 1:N): unknown field "..."` —— 说明字段名不对。排查办法：回去看前端真实请求的 body（DevTools 的 Payload 页），别凭感觉猜。比如 `train_job/list` 用 `page_num`（不是 `page`）、`instance_list` 混用 `jobId` + `page_num`、HPC 的 `hpc_jobs/list` body 里 **不能有 `filter_by`** 字段。
- **workspace 切换不走 URL 参数**。前端的 workspace 实际存在 `localStorage.spaceId`，URL 上加 `?workspace_id=xxx` 不生效（会被忽略 → 回到 localStorage 里那个）。程序化切换要 `page.evaluate("localStorage.setItem('spaceId', ...)")` 然后 reload。
