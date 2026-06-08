# 开发者手册：Browser API（`qz.sii.edu.cn` Web 前端用的 API）

> **文档类型**：开发者手册。仅用于维护 InspireSkill CLI 封装、排查 Browser API 变更或对照平台前端流量；日常平台使用不要加载本文档。
>
> **状态**：非官方、无公开合约、平台侧可随时变更。本文档基于 InspireSkill CLI 侧 [`cli/inspire/platform/web/browser_api/`](../../cli/inspire/platform/web/browser_api/) 的封装、受控 live smoke、前端抓包和 SPA bundle 检查整理。任何变动以当前前端真实请求和 `inspire --debug <cmd>` 观察到的实际流量为准。

## 为什么以 Browser API 为事实源？

CLI 现在只保留前端同款 Browser API 调用链路。旧的 token 公开接口包和相关配置项已经从仓库删除；维护平台接口时，不再把旧公开接口当作 fallback 或并行事实源。

| 链路 | Prefix（默认） | 认证 | CLI 侧封装 | 维护口径 |
| --- | --- | --- | --- | --- |
| **Browser API v1** | `/api/v1` | 前端 SSO session cookie（Keycloak），需要 `Referer` 指向对应页面 | [`platform/web/browser_api/`](../../cli/inspire/platform/web/browser_api/) | notebook、image、project、resources、models、serving、旧版 list/detail/events/logs 等主要接口 |
| **Browser API v2 Action** | `/api/v2/<domain>?Action=<Action>` | 同上 | 同上 | 当前前端训练任务、HPC、Ray、notebook 等页面正在迁移到的 Action 风格接口 |

**经验法则**：

- 当前 CLI 的平台事实都应来自 Browser API 或前端 bundle / Chrome Network / `inspire --debug` 观察到的真实请求。不要新增 token 公开接口 helper。
- `job create/status/stop`、`hpc create/status/stop` 已切到 v2 Action API；`job list/delete/events/instances/logs` 和部分 HPC 观测仍保留 v1 Browser API wrapper，因为前端和现有平台仍提供这些路径。
- **观测性接口**（列表 / 事件 / 可用量 / 预算 / 镜像管理）全部按 Browser API 维护。CLI 里 `inspire job list` / `inspire hpc list` / `inspire image *` / `inspire resources *` / 项目管理相关接口都在 Browser API 侧。

## 认证模型

Browser API 拿不到 Bearer token —— 它是前端 JS 打的，带的是浏览器 SSO cookie（Keycloak 侧下发）。CLI 里 [`inspire/platform/web/session/`](../../cli/inspire/platform/web/session/) 用 Playwright 无头浏览器走一遍 Keycloak 登录拿到 session，之后所有请求都用这个 session。

关键细节：
- 每次请求**必须带 `Referer`**，指向该端点对应的前端页面（如 `/jobs/distributedTraining`、`/jobs/interactiveModeling`）。没 Referer 或 Referer 错了会被后端拒。
- 需要代理时，标准来源是当前账号配置里的 `[proxy]`，Playwright 登录和后续 XHR 都复用同一账号级代理；不要在 Agent 日常命令前加一次性代理环境变量。独立 reverse-capture 开发脚本可用自己的 `--proxy` 参数。
- Base URL 从 `[api].base_url` 读（默认 `https://qz.sii.edu.cn`）；前缀从 `[api].browser_api_prefix` 读（默认 `/api/v1`），可被 `INSPIRE_BROWSER_API_PREFIX` 覆盖。

## 文档收录规则

本文档只收录已经闭合或行为边界已经明确的 Browser API 合同：请求体、主要响应字段、Referer 来源、权限限制和 destructive 语义必须能从当前代码、测试、live smoke、前端抓包或 SPA bundle 中对齐。只观察到路径存在、字段不完整或 destructive 语义没有验证的内容，不进入端点清单。

读表规则：

- `CLI 引用` 列优先指向当前仓库中的 wrapper、helper 或 Agent 命令；尚未封装但合同已经用 live probe / Chrome 抓包闭合的端点，可以标为 `未封装`，但必须写明已验证的请求体、响应形状和不封装原因。
- 受限端点可以进入清单，但只能写已验证行为。例如普通账号固定返回 `403 user is not system admin`，就标为 `受限`，不能推导 admin 成功响应。
- 新增或修改 Browser API wrapper 前，先用 Chrome DevTools、reverse capture、`inspire --debug` 或受控 live smoke 取得完整合同，再同时更新 wrapper、测试、本文档和 [`cli/scripts/reverse_capture/known_endpoints.py`](../../cli/scripts/reverse_capture/known_endpoints.py)。
- create / delete / stop / save / publish 等 destructive 操作必须有受控 live smoke 或等价前端 bundle payload 证据，不能由只读抓包推导。

## 端点清单

下面按域列出当前仓库已封装或明确使用的 Browser API 端点。`{prefix}` 默认 `/api/v1`。

### 账号 / 权限

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `GET` | `{prefix}/user/detail` | 当前登录账号详情 | `browser_api.jobs.get_current_user`；`inspire user whoami` |
| `GET` | `{prefix}/user/{user_id}` | 指定平台账号详情。实测普通账号可读当前登录账号自己的记录，返回字段与 `/user/detail` 同构：`id`、`name`、`name_en`、`email`、`avatar_url`、`global_role`、`extra_info`、`created_at` | `browser_api.users.get_user_detail` |
| `GET` | `{prefix}/user/routes/{workspace_id}` | 探一个 workspace 能不能走，并从 `routes.name == "userWorkspaceList"` 反解可访问 workspace | `browser_api.workspaces.try_enumerate_workspaces` |
| `GET` | `{prefix}/user/permissions/{workspace_id}` | **每页都打**的权限矩阵（返回 `{permissions: ["job.notebook.create", ...]}`，平铺权限码；历史上是 dict 形态，CLI 兼容两种）。前端按它渲染按钮灰化 | `browser_api.users.get_user_permissions`；`inspire user permissions` |
| `GET` | `{prefix}/user/my-api-key/list` | 当前账号的 API Key 列表 metadata（值只在创建时返回） | `browser_api.users.list_user_api_keys`；`inspire user api-keys` |
| `GET` | `{prefix}/user/quota` | 账号配额详情 | `browser_api.users.get_user_quota`；`inspire user quota` |
| `POST` | `{prefix}/ssh/list` | 当前账号 SSH 公钥列表。body: `{page, page_size}`；返回 `{list, total}`，单条含 `id` / `ssh_id`、`name`、`fingerprint` 等。2026-05-09 Chrome 页面和 live probe 对齐 | `browser_api.users.list_user_ssh_keys`；`inspire user ssh-keys list` |
| `POST` | `{prefix}/ssh/create` | 添加 SSH 公钥。body: `{name, content}`；`content` 是网页表单字段，`public_key` / `key` / `ssh_key` 都被 proto 拒绝。后端实测不校验 key 内容格式，所以 CLI 必须先做 OpenSSH 公钥格式校验 | `browser_api.users.create_user_ssh_key`；`inspire user ssh-keys add` |
| `DELETE` | `{prefix}/ssh/{ssh_id}` | 删除 SSH 公钥。空 body；`POST /ssh/delete` 404。2026-05-09 受控 live smoke 创建并删除临时 key，返回 `code:0` | `browser_api.users.delete_user_ssh_key`；`inspire user ssh-keys delete` |
| `POST` | `{prefix}/user/list` | 管理员账号列表。普通账号用 `{}`、分页 body 或 filter body 均返回 `403` / `code:100004` / `user is not system admin`；不能作为普通 CLI 的账号选择来源 | 受限；不封装为默认 CLI |

### 项目

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/project/list` | 列项目 + 剩余预算 + 优先级（实测接受带 `filter` 的 body，**不是完全空 body**；直接传 `{workspace_id:...}` 会被 proto 拒；`page_size=-1` 在部分部署上会触发后端慢路径，CLI 使用正数分页） | `browser_api.projects.list_projects` / `list_all_projects` |
| `POST` | `{prefix}/project/list_v2` | 当前前端表单通用项目下拉。body 实测两种：`{page:1,page_size:-1,filter:{workspace_id,check_admin:true}}` 和 `{page:1,page_size:-1,filter:{workspace_id}}`；返回 `{items,total}` | `browser_api.projects.list_projects_v2` |
| `POST` | `{prefix}/project/list_for_page` | 项目管理页分页列表。body: `{page:1,page_size:10,filter:{}}`；返回 `{items,total}`，项目条目包含预算、维护者、`space_list` 等完整管理视图字段 | `browser_api.projects.list_project_page_records` |
| `GET` | `{prefix}/project/{project_id}` | 项目详情（预算 / 子项目 / 创建人 / 优先级） | `browser_api.projects.get_project_detail` |
| `GET` | `{prefix}/project/owners` | 项目 owner 清单（平台表单字段） | `browser_api.projects.list_project_owners` |

### 文件页 / 项目目录

Web UI 左侧“文件”页是 `/jobs/files?spaceId=<workspace>&folderPath=<path>`。它分两层取事实：先用 Browser API 查有哪些存储和顶层目录，再用 SFTPGo/WebDAV 读取某个具体目录内的文件。path alias 的 `<topic>` 和 `<path-user>` 来自这里的 `directory` 字段，而不是登录账号、`account_key` 或 `train_job/workdir`。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/file/get_system_storage_type_list` | 文件页存储类型列表。body: `{filter:{workspace_id}}`；返回 `data.system_storages[]`，单条含 `name`、`cluster_id`、`is_primary`。2026-05-28 Chrome 抓包和 live probe 对齐，分布式训练空间返回 `hdd`、`ssd`、`qb-ilm`、`qb-ilm2`、`hdd2` 及若干 `share-*` 存储 | `browser_api.files.list_system_storage_types` |
| `POST` | `{prefix}/file/dir/list` | 文件页左侧目录树。body: `{filter:{workspace_id,system_storage_type,cluster_id,name}}`，其中 `name` 是目录分类：`global_public`、`global_user`、`project`、`user` 等。项目目录返回 `data.files[]`，单条含 `name`、`directory`、`is_share`；例如同一项目会同时返回 `/inspire/hdd/project/<topic>/public` 和 `/inspire/hdd/project/<topic>/<path-user>` | `browser_api.files.list_file_directories`；`browser_api.files.list_project_file_directories`；`inspire init` / `inspire init --scope project` 的 path alias 发现 |
| `POST` | `{prefix}/file/sftpgo/connection_info` | 为浏览器侧 WebDAV 操作换取连接信息。body: `{storage_name}`，可选 `{usage}`；返回 `data.address`、`data.auth`、`data.webdav_port`。`auth` 是 WebDAV Basic opaque 值，普通 CLI 输出不展示 | `browser_api.files.get_sftpgo_connection_info` |

文件页的目录事实有几个边界：

- `project/list` 和 `project/list_v2` 返回项目 `en_name`，通常等于共享盘路径里的 `<topic>`；`file/dir/list` 返回的 `directory` 是共享盘目录事实。CLI 发现 path alias 时优先用项目 `en_name` 对齐目录里的 `<topic>`，再用项目显示名兜住同名目录组。
- `global_user` / 项目个人目录里的 `<path-user>` 是平台共享盘目录名，可能是 `tongjingqi-CZXS25110029` 这类值；登录账号 `253108120116` 只表示身份，不表示文件夹名。
- 非 `share-*` 存储的具体文件列表由前端先调用 `file/sftpgo/connection_info`，再对 `file-server.sii.edu.cn:<webdav_port>/<directory>/` 发 WebDAV `PROPFIND Depth:1`。返回 XML 由前端转换为 `name`、`is_dir`、`size`、`updated_at`、`creator`、`my_permission` 等字段。

Chrome bundle 还定义了这些文件操作入口，当前 CLI 不封装 destructive 文件操作；维护时先做受控 smoke 再进入端点清单：

- `POST {prefix}/file/list`：NFS 分支的文件列表，前端传 `{filter:{directory,cluster_id}}`；2026-05-28 对当前 `qz.sii.edu.cn` 直接探测返回 404，实际 `hdd` 页面走 WebDAV。
- `POST {prefix}/file/create_dir` / `delete` / `update_name`：NFS 分支的新建目录、删除、重命名；非 NFS 分支对应 WebDAV `MKCOL`、`DELETE`、`MOVE`。
- `POST {prefix}/audit/security/apply`、`POST /api/v2/audit?Action=DirectoryFileMount`、`POST /api/v2/project?Action=listMountProjects`：文件分享 / 挂载申请相关入口；当前只从 bundle 确认请求形状，未做 destructive smoke。

### Notebook

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/notebook/create` | 建 notebook 实例。body 必须是 Web UI 同款扁平字段：`workspace_id`、`name`、`project_id`、`project_name`、`auto_stop`、`allow_ssh:true`、`mirror_id`、`mirror_url`、`logic_compute_group_id`、`quota_id`、`cpu_count`、`gpu_count`、`memory_size`、`shared_memory_size`，GPU 场景带 `resource_spec_price:{cpu_type,cpu_count,gpu_type,gpu_count,memory_size_gib,logic_compute_group_id,quota_id}`，可选 `task_priority` 和 `node_id`。`node_id` 是前端/后端 create payload 支持的节点名，例如 `qb-prod-gpu1736`；省略时由调度器放置。 | `browser_api.notebooks.create_notebook`；`inspire notebook create --node <NODE_NAME>` |
| `POST` | `{prefix}/notebook/operate` | **只启停，不删除**。body 字段是 `operation`，enum 实测只认 `START` / `STOP`——`DELETE` / `REMOVE` / `DESTROY` / `TERMINATE` / `KILL` / `ARCHIVE` 等 proto 一律拒绝（`code:100002 invalid value for enum field operation`）。删除不走这条，走下一行的 REST DELETE。 | `browser_api.notebooks.start_notebook` / `stop_notebook`；`inspire notebook start/stop` |
| `DELETE` | `{prefix}/notebook/{id}` | 永久删 notebook 条目（REST 风格，与 `DELETE /image/{id}` 同构）。空 body。2026-04-21 实测返回 `code:0 success`。destructive——UI 里的条目也一并消失。 | `browser_api.notebooks.delete_notebook`；`inspire notebook delete` |
| `POST` | `{prefix}/notebook/list` | 列 notebook。body 含 `workspace_id / page / page_size / filter_by:{keyword, user_id[], logic_compute_group_id[], status[], mirror_url[]} / order_by` | `notebook_lookup._list_notebooks_for_workspace`；notebook name resolver |
| `POST` | `{prefix}/notebook/users` | notebook 页面账号筛选下拉。body: `{workspace_id}`；返回 `{list,total}`，单条为账号 metadata | `browser_api.notebooks.list_notebook_users` |
| `GET` | `{prefix}/notebook/{id}` | notebook 详情（状态 / 镜像 / 资源） | `browser_api.notebooks.get_notebook_detail`；`inspire notebook status` |
| `GET` | `{prefix}/notebook/schedule/{workspace_id}` | notebook schedule 配置；UI 使用 path-param 形态，CLI helper 先试这条 | `browser_api.notebooks.get_notebook_schedule`；`inspire init` |
| `GET` | `{prefix}/notebook/schedule?workspace_id={workspace_id}` | notebook schedule query-param fallback；用于兼容旧部署 | `browser_api.notebooks.get_notebook_schedule` fallback |
| `POST` | `{prefix}/notebook/events` | notebook 级生命周期时间轴（调度 → 镜像拉取 → 启动 → 停止 / 保存 / 推送）。body: `{notebook_id, page, page_size}`，返回 `{list, total}`。**注意事件结构和 train/HPC 不同**：只有 `content` 字段装文本 + `created_at` 时间戳，没有 K8s 原生的 `type`/`reason`/`from`。CLI 的 `list_notebook_events` wrapper 会把 `content` 同步到 `message`、把 `created_at` 同步到 `last_timestamp` 方便共用 `cli.utils.events` 渲染器 | `browser_api.notebooks.list_notebook_events`；`inspire notebook events` |
| `POST` | `{prefix}/lifecycle/list` | notebook 生命周期状态转换记录。body: `{notebook_id, page, page_size}`，可选 `start_time`、`end_time` 必须是 int64 可解析值；传空字符串会返回 400 `invalid value for int64 field startTime`。**实测在 2026-04 的平台上对普通 notebook 经常返回 `{list:[], total:0}`** —— 网页的"生命周期"tab 实际是靠 `/run_index/list` 画的；CLI 保留 thin wrapper，主要展示面走 `run_index/list` | `browser_api.notebooks.list_notebook_lifecycle`（thin wrapper） |
| `POST` | `{prefix}/run_index/list` | notebook 运行次数 / 每次运行的起止时间（body: `{notebook_id}`，返回 `{list:[{index, start_time, end_time}], total}`；当前正在运行的 `end_time=""`）—— 网页"生命周期"tab 就是用这个端点拼每行"第 N 次运行 / 时长"的 | `browser_api.notebooks.list_notebook_runs`；`inspire notebook lifecycle` |
| `POST` | `{prefix}/resource_prices/logic_compute_groups/` | compute group 单价 | `browser_api.notebooks.list_resource_prices` |

> **已失效 / 不可用**：`GET {prefix}/notebook/{id}/events` 和 `GET {prefix}/notebook/event/{id}` 两个旧路径在 2026-04 平台升级后全部 404，已由上表的 `POST {prefix}/notebook/events` 替代。`POST {prefix}/notebook/compute_groups` 也同时被移除 —— CLI 现在用 `logic_compute_groups/list`（见 [资源 / 计算组](#资源--计算组)）代替。`GET {prefix}/notebook/status?notebook_id=<id>` 对真实 running notebook 返回 `code:100003` / `记录不存在`，`/notebook/status/{id}` 返回 404；状态以 `GET {prefix}/notebook/{id}` 为准。

### Image

`/image` 前缀下是镜像生命周期，和 notebook / job 共享。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/image/list` | 按 `source` / `visibility` / `registry_hint` 筛镜像；CLI 默认 fanout 到 `official`、`public`、`private` 三个可见来源 | `browser_api.images.list_images_by_source`；`inspire image list --source {public,private,official,all}` |
| `GET` | `{prefix}/image/{image_id}` | 镜像详情 | `browser_api.images.get_image_detail`；`inspire image detail` |
| `POST` | `{prefix}/image/create` | 注册外部镜像地址 | `browser_api.images.create_image`；`inspire image register` |
| `POST` | `{prefix}/mirror/save` | 把运行中的 notebook 存成私有镜像 | `browser_api.images.save_notebook_as_image`；`inspire image save` |
| `DELETE` | `{prefix}/image/{image_id}` | 删镜像 | `browser_api.images.delete_image` |
| `POST` | `{prefix}/image/update` | 更新镜像元数据。body 使用 `{id, visibility?, description?}`，**不是** `{image_id}`；`image_id` 会被 proto 拒。常用于 notebook save 后改公开性 | `browser_api.images.update_image`；`inspire image save` 的 follow-up |

### 训练任务 (Train Job)

当前前端训练任务页面加载的 service bundle 使用 `/api/v2/train?Action=...`：`CreateJobConsole`、`GetJob`、`ListJobs`、`StopJob`、`DeleteJob`、`ListJobInstances`、`ListJobEvents`、`GetJobLog`、`GetJobWorkdir`、`ListJobCreators`。CLI create/detail/list/stop/instances 已使用 v2；delete/events/logs/workdir 仍按现有 v1 Browser API wrapper 维护，直到对应命令迁到 v2。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `/api/v2/train?Action=CreateJobConsole` | 创建分布式训练任务。body 是前端提交体：顶层 `name`、`command`、`framework`、`project_id`、`workspace_id`、`logic_compute_group_id`、`task_priority`、`max_running_time_ms`、`framework_config[]`；`framework_config[0]` 含 `image_type`、`image`、`instance_count`、`quota_id`、`cpu`、`gpu_count`、`mem_gi`、`resource_spec_price:{cpu_type,cpu_count,gpu_type,gpu_count,memory_size_gib,logic_compute_group_id,quota_id}`，可选 `shm_gi`、`exclude_nodes`。`exclude_nodes` 是“排除这些 Ready 节点”，候选来自 `ListNodeDimension`，不是 pin 到节点。 | `browser_api.jobs.create_training_job`；`inspire job create --exclude-node <NODE>` / `job batch` |
| `POST` | `/api/v2/train?Action=GetJob` | 训练任务详情。body: `{job_id}`；响应形状为 `{ResponseMetadata, Result}`，CLI wrapper 返回 `Result`。 | `browser_api.jobs.get_job_detail_v2`；`inspire job logs --follow` polling |
| `POST` | `/api/v2/train?Action=StopJob` | 停止训练任务。body: `{job_id}`。 | `browser_api.jobs.stop_training_job`；`inspire job stop` |
| `POST` | `/api/v2/train?Action=ListJobs` | 当前前端列表页 v2 入口。body 支持顶层 `workspace_id`、`page_num`、`page_size`、`created_by`、`keyword`、`status`；`filter` / `filter_by` / `page` / `name` / `statuses` 会被 proto 拒。响应 `Result.jobs[]` / `Result.total`，job row 字段与 v1 list 兼容并额外含 `exclude_nodes`、`node_infos`、`timeline` 等。 | `browser_api.jobs.list_jobs`；`inspire job list` / name resolver |
| `POST` | `/api/v2/train?Action=ListJobInstances` | 该任务的 pod 实例。body: `{job_id, page_num, page_size}`；只读 probe 也确认 `jobId` 和 `pageNum/pageSize` 可兼容，但 CLI 采用 v2 canonical `job_id` + snake_case 分页。响应 `Result.items[]` / `Result.total`，单条含 `name`、`node`、`instance_status`、`instance_type`、`created_at`、`started_at`、`finished_at`、`running_time_ms`。 | `browser_api.jobs.list_job_instances`；`inspire job instances` / `job shell` / `job logs` |
| `POST` | `{prefix}/train_job/list` | v1 列表 fallback；顶层 `keyword` 可做服务端关键词过滤（2026-05 实测，`filter` / `filter_by` 会被 proto 拒）。新维护优先看 v2 `ListJobs`。 | 低层 fallback；CLI 主路径已迁 v2 |
| `POST` | `{prefix}/train_job/delete` | 永久删训练任务条目（destructive）。body `{"job_id": <id>}`——2026-04-21 实测成功（`code:0`）；注意这是 train_job 域里唯一一个 POST-delete，notebook / hpc 那边都是 REST `DELETE /<res>/{id}`。 | `browser_api.jobs.delete_job`；`inspire job delete` |
| `POST` | `{prefix}/train_job/detail` | v1 详情 fallback。历史前端详情页曾使用；新详情读取维护时优先看 v2 `GetJob` Action。 | `browser_api.jobs.get_job_detail`（低层 fallback） |
| `POST` | `{prefix}/train_job/users` | 当前 workspace 里哪些账号在用资源（共用配额时判断占用方） | `browser_api.jobs.list_job_users` |
| `POST` | `{prefix}/train_job/workdir` | 任务视角的 `train_job_workdir` 字段；返回值形如 `/inspire/<tier>/project/<topic>/<path-user>`。该字段不是文件系统目录目录的权威来源，`inspire init` / `inspire init --scope project` 的 path alias 发现以文件页 `file/dir/list` 为准 | `browser_api.jobs.get_train_job_workdir` |
| `POST` | `{prefix}/train_job/job_event_list` | **Job-level K8s 事件**（body: `{jobId:<id>}`；`Unschedulable` / `Pulling` / `Started` / `FailedCreate` / `SetPodTemplateSchedulerName` 等）。返回字段含 `type`/`reason`/`message`/`from`/`first_timestamp`/`last_timestamp`/`object_id`/`object_type`/`age`。 | `browser_api.jobs.list_job_events`；`inspire job events <name>` |
| `POST` | `{prefix}/train_job/instance_list` | v1 实例列表 fallback。历史 body 是 `{jobId, page_num, page_size}`；新维护优先看 v2 `ListJobInstances`。 | 低层 fallback；CLI 主路径已迁 v2 |
| `POST` | `{prefix}/train_job/events/list` | **Per-instance 事件**（按 pod 名查询）。body 形如 `{page_num, page_size, filter:{object_type:"instance", object_ids:[<pod>], start_last_timestamp, end_last_timestamp}}`。返回 scheduler / kubelet 视角事件（`FailedScheduling`/`Scheduled`/`Pulled`/`Started`），对诊断具体调度失败原因更有用 | `browser_api.jobs.list_job_instance_events`；`inspire job events <name> --instance <pod>` |
| `POST` | `{prefix}/logs/train` | Train job 聚合日志（按 podNames + 时间窗）。body 形如 `{page_size, filter:{podNames:[...], start_timestamp_ms:"...", end_timestamp_ms:"..."}}`；时间戳必须以字符串形式传 epoch-ms。2026-05 实测带 `{field:"time"}` sorter 会被拒，CLI 先不传 sorter 并在客户端排序。 | `browser_api.jobs.list_train_job_logs`；`inspire job logs --source platform` |

### HPC 任务

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `/api/v2/hpc?Action=CreateJobConsole` | 创建 HPC 任务。body 是前端提交体：顶层 `job_name`、`project_id`、`workspace_id`、`logic_compute_group_id`、可选 `task_priority`、`enable_notification:false`；`sbatch_script` 放 Slurm 层字段（`number_of_tasks`、`cpus_per_task`、`memory_per_cpu:"<n>G"`、`memory_per_node:"<n>G"`、`job_max_time`、`entrypoint` 等）；`slurm_cluster_spec` 放节点层字段（`predef_quota_id`、`cpu`、`mem_gi`、`image`、`image_type`、`instance_count`、`spec_price:{cpu_type,cpu_count,gpu_type,gpu_count,memory_size_gib,logic_compute_group_id,quota_id}`）。 | `browser_api.hpc_jobs.create_hpc_job`；`inspire hpc create` / `hpc batch` |
| `POST` | `/api/v2/hpc?Action=GetJob` | HPC 详情。body: `{job_id}`；当前前端 service bundle 未显式导出，但 live probe 返回 `ResponseMetadata.Action=GetJob`，CLI wrapper 返回 `Result`。 | `browser_api.hpc_jobs.get_hpc_job_detail`；`inspire hpc status` |
| `POST` | `/api/v2/hpc?Action=StopJob` | 停止 HPC 任务。body: `{job_id}`。 | `browser_api.hpc_jobs.stop_hpc_job`；`inspire hpc stop` |
| `POST` | `/api/v2/hpc?Action=ListJobs` | 当前 HPC 前端列表页 v2 入口。CLI 现有 `hpc list` 仍使用 v1 wrapper，后续迁移时应以这条为 Chrome 证据。 | 前端已确认；CLI 暂未迁移 |
| `POST` | `{prefix}/hpc_jobs/list` | 列当前 workspace 的 HPC 任务 | `browser_api.hpc_jobs.list_hpc_jobs`；`inspire hpc list` |
| `DELETE` | `{prefix}/hpc_jobs/{id}` | 永久删 HPC 任务条目（REST 风格，与 `DELETE /notebook/{id}` 同构；destructive）。空 body。2026-04-21 实测返回 `code:0 success`。注意：`POST /hpc_jobs/delete` 返 404——前端就是走 REST DELETE。 | `browser_api.hpc_jobs.delete_hpc_job`；`inspire hpc delete` |
| `GET` | `{prefix}/hpc_jobs/{job_id}` | v1 RESTful 详情 fallback；历史 metrics helper 用它取 `logic_compute_group_id`。新 status 维护时优先看 v2 Action。 | `hpc_metrics.get_hpc_metrics` |
| `POST` | `{prefix}/hpc_jobs/events/list` | **HPC job-level 事件**。body: `{pageNum:-1, pageSize:200, filter:{object_ids:[<job-id>], object_type:"HPC_JOB"}, sorter:[{field:"last_timestamp", sort:"ascend"}]}`。注意顶层 camelCase（`pageNum`/`pageSize`），filter 内 snake_case。返回字段含 `reason`/`message`/`from`/`first_timestamp`/`last_timestamp`/`event_timestamp`/`age`/`object_id`/`object_type`；**不含 `type`**（区别于 train_job 事件）。 | `browser_api.hpc_jobs.list_hpc_job_events`；`inspire hpc events <name>` |
| `POST` | `{prefix}/hpc_jobs/instances/list` | HPC pod / component 实例列表。body: `{jobId, page_num, page_size}`；CLI 封装用 `limit` 映射到第一页 `page_size`。返回 `{items,total}`，单条含 `component`、`name`、`node`、`status`、`created_at`、`started_at`、`finished_at`、`running_time_ms` | `browser_api.hpc_jobs.list_hpc_job_instances`；`inspire hpc instances <name> --workspace <workspace> --limit <N>` |
| `POST` | `{prefix}/logs/hpc` | HPC 聚合日志。body: `{page_size, filter:{podNames:[...], start_timestamp_ms:"...", end_timestamp_ms:"..."}}`；时间戳同 train logs 用字符串 epoch-ms。不要传 `sorter:[{field:"@timestamp"...}]`，实测返回 `code:1600003`，消息为日志排序字段不合法 | `browser_api.hpc_jobs.list_hpc_job_logs` |

### Ray 任务（弹性计算）

Web UI 左侧"弹性计算"菜单（`/jobs/ray`）背后的就是 Ray 集群：一个 head 节点跑 driver / 调度，加一组或多组 worker；每组 worker 的实例数在 `min_instances` 与 `max_instances` 之间按平台实时负载扩缩。这和训练任务（`train_job`）固定 `instance_count`、HPC（`hpc_jobs`）固定 `number_of_tasks` 的语义完全不同，后端也是单独一套 `ray_job` 域。CLI 侧封装在 [`browser_api.ray_jobs`](../../cli/inspire/platform/web/browser_api/ray_jobs.py)，对应 `inspire ray create/list/status/stop/delete`。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/ray_job/list` | 列 Ray 任务。body: `{workspace_id, filter_by:{user_id:[...]}, page_num, page_size}`；返回 `{items:[...], total}`。`filter_by` 为空对象时列所有账号；传 `{user_id:[<current_user>]}` 对齐 Web UI "我的"页签 | `browser_api.ray_jobs.list_ray_jobs`；`inspire ray list` |
| `POST` | `{prefix}/ray_job/users` | 当前 workspace 里用过 Ray 的账号（`filter_by` 下拉用）。body: `{workspace_id}` | `browser_api.ray_jobs.list_ray_job_users` |
| `POST` | `{prefix}/ray_job/detail` | 任务详情，含 head / worker 规格、各 worker 组的 min/max 实例范围、运行态统计。body: **`{ray_job_id}`**（**不是** `id` 或 `job_id`——这两个名字 proto 会直接拒："unknown field") | `browser_api.ray_jobs.get_ray_job_detail`；`inspire ray status` |
| `POST` | `{prefix}/ray_job/stop` | 停掉运行中的集群（worker 全部回收）但不删条目。body: `{ray_job_id}`。Ray 集群**不像 train_job / hpc_job 那样命令跑完就自动结束**——除非 driver 主动 `exit`，否则要么手动 stop，要么写 entrypoint 时保证它会结束。**注**：SPA 本身已经切到 `POST /api/v2/ray?Action=StopJob` 这种 v2 Action 风格；CLI 继续打 `{prefix}/ray_job/stop` 仍然 200，先保持现状 | `browser_api.ray_jobs.stop_ray_job`；`inspire ray stop` |
| `POST` | `{prefix}/ray_job/delete` | 永久删条目（destructive；running 的先 `stop`）。body: `{ray_job_id}` | `browser_api.ray_jobs.delete_ray_job`；`inspire ray delete` |
| `POST` | `{prefix}/ray_job/create` | 提交新任务。body 见下表 | `browser_api.ray_jobs.create_ray_job`；`inspire ray create` |
| `POST` | `{prefix}/ray_job/events/list` | Job-level K8s 事件。body: **`{ray_job_id, page_num, page_size, sorter:[{field:"last_timestamp", sort:"ascend"}]}`** — 和 HPC / train 风格**完全不同**，顶层裸放 `ray_job_id`，**没有** `filter:{object_ids, object_type}` 包装，传 `object_type` 会被拒 `参数错误`。返回 K8s-event shape：`reason` / `type` / `message` / `first_timestamp` / `last_timestamp` / `count`。卡 PENDING 时 `FailedScheduling` 的 message 直接写明节点紧张原因 | `browser_api.ray_jobs.list_ray_job_events`；`inspire ray events <name>` |
| `POST` | `{prefix}/ray_job/instances/list` | pod 级视图：head + 每个 worker 组的实际 pod。body: `{ray_job_id, page_num, page_size}`；CLI 封装用 `limit` 映射到第一页 `page_size`。返回 `items[]` 每条含 `instance_id` / `instance_type` (`head` / `worker`) / `worker_group_name` / `status` (`pending` / `running` / ...) / `cpu_count` / `memory_size` / `gpu_count` / `priority` / `created_at` | `browser_api.ray_jobs.list_ray_job_instances`；`inspire ray instances <name> --workspace <workspace> --limit <N>` |
| `POST` | `{prefix}/ray_job/scaling_histories/list` | 某 Ray 任务的弹性扩缩事件历史（Web UI "扩缩容历史" tab）。body: `{ray_job_id, page_num, page_size}`。用于 post-mortem 判断 worker 组的 `min_replicas` / `max_replicas` 是否真的动过 | `browser_api.ray_jobs.list_ray_job_scaling_histories` |

> **`ray_job/status` 不存在**：实测返回 404；状态直接从 `ray_job/detail` 顶层字段读（`status` / `sub_status` / `finished_at` 等）。

**`ray_job/create` 完整 body 合约**（从 `/assets/constant.BP_zw-df.js` 的 SPA 提交函数反编译；**不要**换成 HPC / train_job 的字段名）：

```json
{
  "name": "av-pipeline",
  "description": "streaming decode + infer pipeline",
  "workspace_id": "ws-...",
  "project_id": "project-...",
  "task_priority": 9,
  "entrypoint": "<driver command>",
  "head_node": {
    "mirror_id": "<image_id>",
    "image_type": "SOURCE_PUBLIC|SOURCE_PRIVATE|SOURCE_OFFICIAL",
    "logic_compute_group_id": "lcg-...",
    "quota_id": "<quota_id>",
    "shm_gi": 64
  },
  "worker_groups": [
    {
      "group_name": "decode",
      "mirror_id": "<image_id>",
      "image_type": "SOURCE_PUBLIC",
      "logic_compute_group_id": "lcg-...",
      "min_replicas": 1,
      "max_replicas": 4,
      "quota_id": "<quota_id>",
      "shm_gi": 32
    }
  ]
}
```

**反直觉字段映射**（提交时最容易踩的四个坑）：

| 表单字段 / 直觉字段 | 线上字段 | 备注 |
| --- | --- | --- |
| `head` / `head_spec` | `head_node` | 单数；复数 `heads` 或简写 `head` 都会 `proto: unknown field` |
| `image` (Docker URL) | `mirror_id` (内部 image_id) | 要先 `/image/list` 反查到 `image_id` 再提交。CLI 的 `_resolve_image_id()` 走 public→private→official 三层按 URL 精确匹配 |
| `command` | `entrypoint` | 在表单里叫 `command`，序列化时改名成 `entrypoint`（和 hpc 一致） |
| `predef_quota_id` / `spec_id` | `quota_id` | Ray 走 notebook 风格（不是 HPC 的 `predef_quota_id`） |

其它观察：

- `worker_groups[].group_name` 用 snake_case；`min_replicas` / `max_replicas` 也是 snake_case（**不是** `min_instances` — 那个名字只在 detail 响应里出现）。
- `shm_gi` 在 head / 每个 worker 里都可选；为 None 时 SPA 直接不把这个 key 写进 body。
- `description` 是支持字段但非必填。
- `task_priority` 平台默认用 0 / 1 这档，CLI 约定 1=LOW / 9=HIGH（和 `job` / `hpc` 同）。

**相关下拉端点**（create 表单预取，已在其他小节封装）：

- `POST {prefix}/logic_compute_groups/list` —— 计算类型组选择（见 [资源 / 计算组](#资源--计算组)）
- `POST {prefix}/project/list` —— 所属项目选择（见 [项目](#项目)）
- `POST {prefix}/image/list` —— 任务镜像选择（见 [Image](#image)）

**Referer**：所有 `ray_job/*` 请求必须带 `Referer: /jobs/ray?spaceId=<workspace_id>`。CLI 的 `_ray_referer()` 会自动拼上。

### 资源视图 / 监控指标 (`cluster_metric`)

网页 `实例详情 / 资源视图` tab 背后的时间序列端点。覆盖 notebook / 训练任务 / HPC / 部署服务 / Ray 五种 task 的 8 种利用率指标；CLI 侧已暴露 `inspire notebook|job|hpc|serving metrics`，Ray 指标目前只在底层 Browser API helper 中可用。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/cluster_metric/resource_metric_by_time` | 按 task_id + task_type + logic_compute_group_id 查一段时间窗内的指标序列。body 见下；返回 `{time_seris_metric_groups:[{group_name,metric_type,resource_name,time_series:[{timestamp,data}]}]}`（注意响应键是 **`time_seris_metric_groups`** 拼错了的）。 | `browser_api.metrics.get_resource_metrics_by_time`；`inspire notebook|job|hpc|serving metrics` |

请求体模板：

```json
{
  "filter": {
    "logic_compute_group_id": "lcg-...",
    "task_id": "<raw uuid — 不带 nb-/job-/hpc- 前缀>",
    "task_type": "interactive_modeling|distributed_training|hpc_job|inference_serving|ray_job"
  },
  "metric_types": ["gpu_usage_rate"],
  "time_range": {
    "start_timestamp": 1776926077,
    "end_timestamp":   1776933500,
    "interval_second": 60
  }
}
```

**硬约束**（2026-04 实测）：

- **`metric_types` 实测只认第一个**：发 `["gpu_usage_rate","cpu_usage_rate"]` 只返 gpu，cpu 被静默丢弃。CLI wrapper 为每个 metric 拆成一次 POST 再拼结果。
- **`task_type` 合法值只有 5 个**：`interactive_modeling` / `distributed_training` / `hpc_job` / `inference_serving` / `ray_job`（2026-04 补充：Ray 走 `ray_job`，经 SPA 抓包实测）。传 `training_job` / `hpc` / `model_deployment` 会收到 `code:100000 422: ... query="...{=\"...\"})"` 错误（空 label 名），因为后端 Prometheus label 映射表里没有这几个别名。
- **`task_id` 按资源形状不同**（2026-04 每种都实测过）：
  - `interactive_modeling` → **裸 UUID**（没 `nb-` 前缀），例 `91fbc44e-9c40-4c99-99f4-d27d6303266e`
  - `distributed_training` → **带 `job-` 前缀**，例 `job-a211cbef-c30f-4602-aa46-3e61b4ba2f0a`（去掉前缀也能跑但每个 pod 的 group_name 就对不上了）
  - `hpc_job` → **带 `hpc-job-` 前缀**
  - `inference_serving` → **带 `sv-` 前缀**
  - `ray_job` → **带 `rj-` 前缀**
- **`logic_compute_group_id` 必填**，来源按资源：
  - notebook → `GET /notebook/{id}` 的 `start_config.logic_compute_group_id`（顶层 `logic_compute_group.*` 字段平台侧留空）
  - train_job → `POST /train_job/detail` 的顶层 `data.logic_compute_group_id`
  - hpc → `GET /hpc_jobs/{id}` 的顶层 `data.logic_compute_group_id`
  - serving → `GET /inference_servings/<id>` 顶层
  传空或 `cg-` 前缀（而非 `lcg-`）都会返 422。
- **`interval_second` 限定 4 档**：`60 / 300 / 1800 / 3600`（对应 UI 的 1分/5分/30分/1小时）。其它值返回几乎空的序列。
- **指标单位**：`*_usage_rate`（gpu / gpu_memory / cpu / memory）= 0-1 ratio；`disk_io_*` / `network_tcp_ip_io_*` = bytes/second。
- **按 pod 分组**：多 worker 训练每个 worker 一个 `group_name`（`job-<id>-worker-0..N-1`）；多副本 serving 类似；单实例 notebook 只有 1 个 group。**用这个来做多节点训练健康监测**——一个 worker 掉队/hang 马上在 group 之间的 spread 里出现。

### 资源 / 计算组

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/workspace/list` | workspace 管理列表。普通账号实测 `{}`、`{page,page_size}`、`{filter:{}}` 都返回 `code:0` 但 `items:[]`；`page_num` 或 `filter.keyword` 会被 proto 拒绝。普通 CLI 不用它枚举 workspace，仍以 `/user/routes/{workspace_id}` 和 project `space_list` 为准 | 受限/低信息量；不作为 CLI workspace source |
| `POST` | `{prefix}/logic_compute_groups/list` | 列 workspace 下所有 logic compute groups（带 GPU 型号 / 机房） | `browser_api.availability.list_compute_groups` |
| `GET` | `{prefix}/logic_compute_groups/{logic_compute_group_id}` | logic compute group 静态详情；路径必须是 `lcg-...`，传物理 `cg-...` 会返回 `code:100002`。返回 `compute_group_id/name`、`gpu_type_stats`、`storage_disks`、`support_job_type_list` 等 | 未封装；2026-05-09 live probe 确认 |
| `POST` | `{prefix}/compute_groups/list` | 物理 compute group 列表。body 接受 `{}`、`{filter:{workspace_id}}`、`{page_num,page_size,filter:{workspace_id}}`；顶层 `{workspace_id}` 和 `page` 字段会被 proto 拒绝。返回 `{compute_groups,total}` | 未封装；2026-05-09 live probe 确认 |
| `POST` | `{prefix}/compute_resources/cluster_basic_info` | 当前 workspace 的 compute group 基础信息；body: `{workspace_id, filter:{workspace_id}}` | `browser_api.availability.cluster_basic_info` |
| `GET` | `{prefix}/compute_resources/cluster_basic_info?workspace_id={workspace_id}` | `cluster_basic_info` 的 GET fallback | `browser_api.availability.cluster_basic_info` fallback |
| `POST` | `{prefix}/cluster_basic_info` | 旧部署 fallback，body 同 `compute_resources/cluster_basic_info` | `browser_api.availability.cluster_basic_info` fallback |
| `GET` | `{prefix}/compute_resources/logic_compute_groups/{group_id}` | 某个 compute group 的实时可用量 | `browser_api.availability.get_accurate_resource_availability`；`inspire resources list` 的底层 |
| `POST` | `{prefix}/compute_resources/list_node_dimension` | 某个 compute group 的节点维度；body: `{workspace_id, logic_compute_group_id, filter:{logic_compute_group_id}, page_num, page_size}` | `browser_api.availability.list_node_dimension` |
| `POST` | `{prefix}/compute_resources/node_dimension/list` | `list_node_dimension` 的 POST fallback，body 同上 | `browser_api.availability.list_node_dimension` fallback |
| `GET` | `{prefix}/compute_resources/node_specs/logic_compute_groups/{group_id}` | `list_node_dimension` 的 GET fallback；也被 `platform.web.session.workspace.fetch_node_specs` 使用 | `browser_api.availability.list_node_dimension` fallback；`fetch_node_specs` |
| `POST` | `{prefix}/cluster_nodes/list` | 整节点空余 | `browser_api.availability.get_full_free_node_counts`；`inspire resources nodes` |
| `GET` | `{prefix}/cluster_nodes/workspace/{workspace_id}` | workspace 维度节点状态摘要。返回 `data.backup`、`data.fault`、`data.nodes[]` | 未封装；Chrome reverse capture（2026-05-09）确认 |

> **注**：`cluster_nodes/list` 按 Browser API 维护。CLI 的 `inspire resources nodes` 默认走 Browser API，并以 live 返回为资源事实。

### 模型 (Model)

当前封装覆盖 `inspire model list/status/versions/register`，并补齐网页详情页和模型广场的 read-only Browser API helper。当前前端入口是 `/jobs/modelService?spaceId=<workspace_id>`；模型广场入口是 `/modelPlaza`。

网页端注册表单已实测确认：模型名支持字母、数字、下划线、短横线和点，且必须以字母开头；模型存储位置必须是当前空间下的项目目录路径，否则后端会返回“模型源路径不存在或访问异常”。

日常默认看 Agent 可读输出；结构化输出只用于脚本。平台的 `list` 端点会把最新版本属性合入模型条目，而 `detail` 端点只返回模型主记录，所以 `is_vllm_compatible` 在 `list` 与 `status` 中可能不一致。要准确的版本属性，用 `inspire model versions <model-name>`。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/model/list` | 工作空间下的模型注册表。body: `{page, page_size, filter_by:{keyword?, user_id?, project_id?[], model_type?[]}, workspace_id}`。`user_id` 必须是字符串；`project_id` 必须是数组；`keyword` 可做服务端名称搜索 | `browser_api.models.list_models`；`inspire model list` |
| `POST` | `{prefix}/model/detail` | 单个模型详情。body: `{model_id}`；返回 `{model, project_name, user_avatar, user_name}` | `browser_api.models.get_model_detail`；`inspire model status` |
| `GET` | `{prefix}/model/{model_id}` | 详细版本清单，返回 `{list, next_version, total}`；每个版本含 `model_path`、`model_source_path`、`model_size_gi` 和 `running_infrence_serving` | `browser_api.models.list_model_version_records`；`inspire model versions` |
| `GET` | `{prefix}/model/{model_id}/versions` | 紧凑版本状态清单，只含 `model_id/version/status/is_vllm_compatible/workspace_id` 等少量字段 | `browser_api.models.list_model_versions` |
| `POST` | `{prefix}/model/create` | 注册模型首版本。body: `{name, project_id, workspace_id, model_source_path, model_source_type:1, model_type:[], tags:[], description}`。`version_description` 会被 proto 拒绝 | `browser_api.models.create_model`；`inspire model register` |
| `POST` | `{prefix}/model/inference_serving/pending` | 编辑或删除某模型版本前的占用检查。body: `{model_id, version}`；返回 `{has_pending_serving}` | `browser_api.models.check_model_inference_serving_pending` |
| `POST` | `{prefix}/model/inference_servings` | 某模型版本关联的部署列表。body: `{model_id, version, page, page_size}`；返回 `{serving, total}` | `browser_api.models.list_model_inference_servings` |
| `GET` | `{prefix}/model/{model_id}/version/{version}/publish/prefill` | 模型发布到模型广场表单的预填数据，返回 `{model_info, technical_specs, integration_info}` | `browser_api.models.get_model_publish_prefill` |
| `GET` | `{prefix}/model/{model_id}/version/{version}/publish/status` | 某模型版本的模型广场发布状态，返回 `{has_published, status, publish_reject_detail}` | `browser_api.models.get_model_publish_status` |
| `POST` | `{prefix}/model/users` | 模型页账号筛选下拉。body: `{project_id}`；返回 `{list,total}`，单条含 `user_id` / `user_name` | `browser_api.models.list_model_users` |
| `POST` | `{prefix}/model_plaza/list` | 模型广场列表。body: `{page, page_size, filter:{workspace_id, keyword?, source?, model_type?, region?, min_param_size_b?, max_context_len?}}`；返回 `{items,total_count}` | `browser_api.models.list_model_plaza` |
| `GET` | `{prefix}/model_plaza/filters` | 模型广场筛选项 metadata | `browser_api.models.get_model_plaza_filters` |
| `GET` | `{prefix}/model_plaza/detail/{model_plaza_id}` | 模型广场详情 | `browser_api.models.get_model_plaza_detail` |
| `GET` | `{prefix}/model_plaza/related_workspace/{model_plaza_id}` | 模型广场条目可关联 workspace | `browser_api.models.list_model_plaza_related_workspaces` |
| `GET` | `{prefix}/model_plaza/deploy_serving_config/{model_plaza_id}` | 从模型广场一键部署的 serving 表单预填配置 | `browser_api.models.get_model_plaza_deploy_serving_config` |

下列模型写操作已经从 SPA bundle 解析出路径和 payload，但当前 CLI 没有 Agent 命令封装，维护时不要把它们当作只读 helper 使用：

| 方法 | 路径 | 用途 | 证据 / 边界 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/model/{model_id}/versions` | 添加模型新版本。body: `{model_id, model_source_path, version_description, source}` | SPA bundle payload；destructive / write |
| `PUT` | `{prefix}/model/edit/{model_id}` | 编辑某版本来源和描述。body: `{model_source_path, version_description, model_source_type, version}` | SPA bundle payload；destructive / write |
| `POST` | `{prefix}/model/delete` | 删除某模型版本。body: `{model_id, version}`；网页会先调用 `/model/inference_serving/pending` | SPA bundle payload；destructive |
| `PUT` | `{prefix}/model/tryAgain/{model_id}` | 失败版本重试。body: `{version}` | SPA bundle payload；write |
| `POST` | `{prefix}/model/{model_id}/version/{version}/publish` | 发布到模型广场。body: `{model_info, technical_specs, integration_info}` | SPA bundle payload；write |

### 模型部署 (Inference Servings)

当前 `serving` CLI 以 `/jobs/modelDeployment?spaceId=<workspace_id>` 的 Browser API 为准，覆盖自定义部署的 `create/list/status/stop/delete/configs/metrics`。创建表单使用 `mirror_id`、注册模型、`resource_spec_price` 和 v2 start/stop Action；不要用旧公开接口文档里的 `image_type + spec_id + image URL` payload 推导 CLI create。

网页端部署表单已实测确认：当前 CLI Agent 合同覆盖“自定义部署”（`inference_serving_type:"CUSTOM"`）。自定义部署使用注册模型、镜像 `mirror_id`、运行命令、端口、优先级、资源规格、副本数、单副本实例数和共享内存。`inspire serving quota --workspace <name>` 必须查 `SCHEDULE_CONFIG_TYPE_SERVE`，不是训练任务的 `SCHEDULE_CONFIG_TYPE_TRAIN`。在 `H200-2号机房` 实测到的 serving 规格是 `1,18,200`、`2,36,400`、`4,72,900`、`8,168,1800`。

LLM 专属部署不是 `CUSTOM`。网页用 `inference_serving_type` 区分：普通自定义部署是 `CUSTOM`，独占 LLM 是 `EXCLUSIVE`，Serverless LLM 是 `SERVERLESS`；Serverless 资源配置走 `SCHEDULE_CONFIG_TYPE_SERVE_DYNAMIC`。模型广场一键部署会带 `model_source:"MODEL_PLAZA"`，其中 `model_id` 是 `mp-...` 形态，预填配置来自 `/model_plaza/deploy_serving_config/{model_plaza_id}`。

| 方法 | 路径 | 用途 | CLI 引用 |
| --- | --- | --- | --- |
| `POST` | `{prefix}/inference_servings/list` | 列部署。body: `{page, page_size, filter_by:{my_serving, keyword?, project_id?[], status?[], inference_serving_type?[]}, workspace_id}`；返回 `{inference_servings[], total}`。`inference_serving_type` 可传 `["CUSTOM"]` 或 `["EXCLUSIVE","SERVERLESS"]` | `browser_api.servings.list_servings`；`inspire serving list` |
| `POST` | `{prefix}/inference_servings/user_project/list` | 当前 workspace 下可用的项目 + 账号清单（建部署弹窗用）。body: `{workspace_id}`；返回 `{projects, users}` | `browser_api.servings.list_serving_user_project` |
| `GET` | `{prefix}/inference_servings/configs/workspace/{workspace_id}` | 该 workspace 的部署配置。当前实测返回 `{configs:{enable_auto_stop, items:[{gpu_count_min,gpu_count_max,auto_stop_ruleset}], workspace_id}}` | `browser_api.servings.get_serving_configs`；`inspire serving configs` |
| `GET` | `{prefix}/inference_servings/{id}` | 部署详情；返回中 `model`、`mirror`、`resource_spec_price` 常是嵌套对象，readable formatter 必须取名称而不是直接打印 dict | `browser_api.servings.get_serving_detail`；`inspire serving status` |
| `GET` | `{prefix}/inference_servings/{id}/versions` | 部署版本历史，返回 `{inference_servings,total}` 或 `{list,total}` | `browser_api.servings.list_serving_versions` |
| `POST` | `{prefix}/inference_servings/instances/list` | 部署实例 / pod 列表。body: `{inference_serving_id, page, page_size}`；返回 `{items,total}` / `{instances,total}` | `browser_api.servings.list_serving_instances` |
| `POST` | `{prefix}/inference_servings/events/list` | 部署事件。body: `{page, page_size, filter:{object_type, object_ids:[inference_serving_id]}}`；`CUSTOM` / `EXCLUSIVE` 用 `INFERENCE_SERVING`，`SERVERLESS` 用 `INFERENCE_SERVERLESS` | `browser_api.servings.list_serving_events` |
| `POST` | `{prefix}/logs/inference_serving` | 部署聚合日志。body: `{page_size, filter:{podNames:[...], start_timestamp_ms:"...", end_timestamp_ms:"..."}}`；不要传 sorter，和 train / HPC 日志保持客户端排序 | `browser_api.servings.list_serving_logs` |
| `POST` | `{prefix}/inference_servings/scale_history/list` | 部署扩缩容历史。body: `{inference_serving_id, page, page_size}`；返回 `{items,total}` / `{list,total}` | `browser_api.servings.list_serving_scale_history` |
| `GET` | `{prefix}/inference_servings/{id}/terms` | 部署调用说明 / terms 信息 | `browser_api.servings.get_serving_terms` |
| `POST` | `{prefix}/inference_servings/create` | 创建自定义部署。body: `{workspace_id, project_id, inference_serving_type:"CUSTOM", name, logic_compute_group_id, model_id, model_version, mirror_id, command, port, description, replicas, node_num_per_replica, shm_gi?, task_priority, custom_domain?, resource_spec_price:{cpu_type,cpu_count,gpu_type,gpu_count,memory_size_gib,logic_compute_group_id,quota_id}}` | `browser_api.servings.create_serving`；`inspire serving create` |
| `POST` | `/api/v2/inference_serving?Action=StopServing` | 停止部署。body: `{inference_serving_id, version:0}` | `browser_api.servings.stop_serving`；`inspire serving stop` |
| `POST` | `/api/v2/inference_serving?Action=StartServing` | 启动部署。body 同 stop | `browser_api.servings.start_serving` |
| `POST` | `/api/v2/inference_serving?Action=RollbackServing` | 回滚部署版本。body: `{inference_serving_id, version}` | SPA bundle payload；当前无 CLI 命令 |
| `DELETE` | `{prefix}/inference_servings/{id}` | 删除已停止的部署条目 | `browser_api.servings.delete_serving`；`inspire serving delete` |

### Jupyter / 终端代理

Browser API 还代理 Jupyter Lab 和 WebSocket 终端，用来 bootstrap notebook SSH / rtunnel。这个 bootstrap flow 被 `inspire notebook ssh <name>`、`inspire notebook connection refresh <name>`、`inspire notebook ssh-config <name>` 和 OpenSSH `ProxyCommand` 背后的 `inspire notebook ssh-proxy <name>` 复用。

Notebook Web IDE 入口是前端页面 `GET /ide?notebook_id=<id>`，不是 JSON API。`inspire notebook url` 只输出这个浏览器入口。`inspire notebook vscode-proxy-suffix` 和 `notebook proxy-url` 会用 Playwright 打开入口页，从 iframe / 页面 URL 中读取当前容器的网关路径 `/ws-.../project-.../user-.../vscode/<runtime>/<token>`；这个 token 不由 notebook detail API 暴露，生命周期跟容器运行实例绑定。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` / `POST` / `WS` | `{prefix}/notebook/lab/{notebook_id}/proxy/{port}/...` | 经由平台代理透传到 notebook 内部 Jupyter 服务的任意 HTTP / WebSocket 请求。Notebook SSH bootstrap 使用 Jupyter terminal REST + WebSocket，失败时兜底 Playwright 终端自动化，全部走这条 |

## 如何自己看到这些流量

三种方式：

1. **`inspire --debug <cmd>`** —— CLI 会把脱敏过的 HTTP 流量写进 `~/.cache/inspire-skill/logs/`，含完整 URL / method / 响应摘要。
2. **浏览器 DevTools** —— 在 `qz.sii.edu.cn` 里打开 Network 面板，Filter `api/v1`。前端打哪些请求一目了然；一般是 POST + JSON body。
3. **Playwright 网络抓包脚本** —— 想系统性扫一遍只读端点时用。思路：加载 `~/.cache/inspire-skill/web_session-*.json` 里的 `storage_state` → 用 `page.on("request"/"response")` 装监听 → 程序化导航所有已知前端路由 → 对每个列表页点第一行（进 detail）、开 `+ 新建` 弹窗（**别点"提交"**，`Esc` 关闭）→ 导出 JSONL 做 diff。抓包输出里的新路径只是待核验线索；只有请求体、响应、Referer 和 destructive 语义闭合后，才写入本文档和 CLI。

## 稳定性 & 注意事项

- **不是公开合约**。平台前端迭代时可能改路径 / 字段名而不通知。`inspire update` 的主要职责之一就是跟进这些变更。近期例子：`GET /notebook/{id}/events` 和 `POST /notebook/compute_groups` 在 2026-04 悄悄下线（见 Notebook 小节末注）。
- **认证依赖 Playwright / 浏览器 session**。如果 Agent 环境装不了 Chromium（headless 容器、严格沙盒），Browser API 这一整层就不能稳定使用；修复入口是 `inspire update --cli-only` 或重新登录账号，不要回退到旧 token 接口。
- **rate limit**。平台侧有 nginx/openresty 层的速率限制（实测 ≥3 req/s 就可能拿到 `429`）。CLI 里几个 list 类端点做了退避重试；Agent 自己写脚本打 Browser API 时要放一点 sleep。
- **Referer 要对**。每个端点在上面表格对应的 CLI 引用里都能找到它用的 Referer。如果自己 curl，别把 Referer 漏了或填成不相关的页面，会收到 400 / 401。
- **闭环优先于覆盖面**。字段、enum 或 destructive 语义没有摸清楚时，先留在抓包输出或任务记录中继续验证，不写入本文档或 `known_endpoints.py`。实现 CLI 前至少要有 wrapper 测试；涉及创建 / 删除 / 停止时还要有受控 live smoke 或 bundle payload 证据。
- **同名路径、不同含义**。不同前端域的 list / detail / stop 字段名并不统一：例如 train v2 instances 用 `job_id`，Ray instances 用 `ray_job_id`，HPC v2 create 用 `job_name`。不要互相直接替换请求体。
- **Protobuf 字段校验严格**。后端在 APISIX 之后用 protobuf 做请求体校验。常见 400：`proto: (line 1:N): unknown field "..."` —— 说明字段名不对。排查办法：回去看前端真实请求的 body（DevTools 的 Payload 页），别凭感觉猜。比如 train v2 `ListJobs` 用 `page_num`（不是 `page`），并且 `filter` / `filter_by` 会被拒；HPC 的 `hpc_jobs/list` body 里 **不能有 `filter_by`** 字段。
- **workspace 切换不走 URL 参数**。前端的 workspace 实际存在 `localStorage.spaceId`，URL 上加 `?workspace_id=xxx` 不生效（会被忽略 → 回到 localStorage 里那个）。程序化切换要 `page.evaluate("localStorage.setItem('spaceId', ...)")` 然后 reload。
