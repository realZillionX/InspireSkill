# Changelog

## Unreleased

### 变更

- 恢复 `inspire update` 面向用户的输出：逐步打印进度（检查更新 / 升级 CLI / 刷新
  Skill / 校验安装 / 准备浏览器运行时）、列出刷新到的 harness，并打印新旧版本之间的
  更新摘要（取自 GitHub Releases，回退到 `main` 的 `CHANGELOG.md`）。v6.3.0 把这些
  一并降级成了 `--debug` 日志，只剩一行 `InspireSkill updated to vX`。诊断细节仍然
  只进 `--debug`：harness 只报名称不报本地路径，摘要过滤掉安装 / 构建类条目、URL
  和绝对路径。`--json` 输出相应新增 `skills` 与 `release_notes` 字段。摘要条目会先
  合并硬换行的续行，不再从行尾截断成半句话。

## v6.3.0

### 新增

- `inspire ray metrics` 补齐 Ray 的指标观察，与 job / hpc / serving metrics 同接口。
- `inspire serving events|instances|start` 补齐 Serving 的观察与启动入口。

### 破坏性变更

- 移除 `inspire job id`、`inspire hpc id`、`inspire notebook id`。CLI 不再有任何
  Handle 输出入口，用 `list` 拿 Name 即可。
- `inspire notebook ssh open <name>` 改为 `inspire notebook ssh <name>`；
  `inspire notebook vscode-proxy-suffix` 改为 `inspire notebook vscode`。
- `job|hpc|notebook|serving metrics` 的 `--lcg` 改名为 `--group`，与创建命令一致。
- `serving create --shm-gib` 改名为 `--shm-size`，与 job / notebook / ray 一致。
- `resources nodes` 只保留 `--min-nodes`，去掉同义别名 `--min-free` 与
  `--min-full-free-nodes`（三者本就是同一个选项）。
- 移除 `resources availability --no-cache`：它绕过的可用性缓存已整体删除，该命令现在
  始终读取 Live 数据。
- 移除 `notebook exec` 的 `--artifact-path` / `--download` / `--no-wait` /
  `--denylist`，以及它们背后的 GitHub Actions bridge action 执行后端和整个
  `INSP_GITHUB_*` 配置类别。这条路径需要自建 Actions Runner 挂载集群共享文件系统、
  在用户仓库里安装 workflow，并把产物经 orphan 分支中转；参考文档从未描述过它，随附
  示例 workflow 引用的 `inspire bridge exec` 也早已不存在。拉取远端产物请用
  `notebook scp -d ... -r`（直连 SSH/SCP，无需绕行 GitHub）；无 tunnel 时的命令执行
  仍由 Jupyter terminal transport 承担。残留的 `[github]` 配置段会被静默忽略，不影响
  现有配置加载。
- 移除从 `INSPIRE_USERNAME` / `INSPIRE_PASSWORD` 环境变量读取平台凭据的兜底路径；
  账号是唯一受支持的凭据来源，用 `inspire account add <name>` 配置。其余
  `INSPIRE_*` 配置项（如 `INSPIRE_SHM_SIZE`、`INSPIRE_JOB_ENABLE_NOTIFICATION`）不受影响。
- `inspire update` 不再抓取并打印 GitHub Release 正文。
- 移除 `inspire notebook net-test` 及其背后的 JupyterTerminal 出站探测
  （`probe_notebook_network` 和相关 Browser API 导出）。它唯一的消费者是 SSH transport
  判断，而该判断已改用 Compute Group（见下）。需要确认某个具体端点是否可达时，用
  `inspire notebook exec <name> "..."` 在容器里一次性验证。
- `notebook connection list|status` 的 JSON 输出去掉 `public_internet` 字段，本地
  bridge profile 也不再持久化 `has_internet`：缓存连接只会存在于支持 SSH 的 Notebook。
  旧 `bridges.json` 中残留的 `has_internet` 会被静默忽略。

### 变更

- 重构 Agent 使用手册：新增 `references/project-context.md`，承载项目初始化问询
  （Project / Workspace / Paths / Image 四项须由用户确认，其中 `CPU资源空间` 与
  `分布式训练空间` 是默认公共 Workspace，其余专属 Workspace 须用户亲自指认）、
  `INSPIRE.md` 资产合同（合并原 `project-assets.md`）和项目信息持续维护触发点；
  `resources-and-paths.md` 更名 `resources.md`、`network-and-sources.md` 更名
  `internal-sources.md`、`image-management.md` 更名 `image.md`。删除 SKILL.md 的
  “网络与合规闸门”一节（此类限制由 CLI 层承担），并清理各使用手册中缓存实现、
  内部字段名等开发层细节；`references/dev/` 之外的文档只保留面向 Agent 的操作语义。
- Notebook 的 SSH/Rtunnel 可用性改为直接看 Compute Group 名称是否含 `H100` / `H200`，
  不再靠 JupyterTerminal 联网探测推断。旧路径要为每个非静态受限的 Notebook 开一个完整
  远端终端跑连通性探测（数十秒），且把“能上网”当作“能 SSH”的代理指标。该判断同时收紧到
  `run_notebook_ssh` 内部，因此不带 `--workspace` 的 `notebook ssh` 也无法为受限
  Notebook 建立连接。
- Compute Group 名称随 Notebook 一起进本地 Name 解析索引（`resource_identity` 新增
  `compute_group` 列，旧库自动 `ALTER TABLE` 迁移，schema 版本 3），由后台定时刷新
  一并维护。因此 Transport 判断在缓存命中时不发任何 API 请求；缓存未命中时也只从
  Name 解析本来就要发的 `/notebook/list` 响应里读，不再额外请求 Notebook Detail。
- 缓存 TTL 整体放宽：Workload（notebook / job / hpc / ray / serving）60 秒 → 5 分钟，
  平台目录类（workspace / project / compute-group / image / model / ssh-key）5 分钟 →
  30 分钟。后台刷新进程的最小间隔取所有 TTL 的最小值，因此每账号的后台刷新从最多
  60 秒一个降到最多 5 分钟一个。
- Project 改为按账号全局缓存，不再按 Workspace 分片。一个 Project 可同时归属多个
  Workspace（`ProjectInfo.workspace_ids`），平台的 `project/list` 也支持不带 Workspace
  过滤，此前每个 Workspace 各存一份、刷新时每个 Workspace 各发一次请求。现在刷新走
  `list_all_projects()` 一次拿全。Name 解析的 Scope 由 `scope_workspace_id()` 统一
  归一化，刷新侧和查询侧不会再对同一个名字用不同的 Scope。
- 新增 Quota 缓存，作为 Name 解析索引的普通成员：一条 Quota 行本身就是
  Name → Handle 映射，Name 是 `gpu,cpu,mem` 三元组（正是 `--quota` 传的值），
  Handle 是平台 `quota_id`。`resource_identity` 新增 `payload` 列存原始规格对象
  （`create` 需要回传 `cpu_type` / `gpu_type`），旧库自动 `ALTER TABLE` 迁移，
  schema 版本 4。Resource 名为 `quota-notebook` / `quota-job` / `quota-hpc` /
  `quota-ray` / `quota-serving`，Scope 是 Workspace，TTL 30 分钟，因此
  `cache refresh --resource quota-notebook`、`cache status`、`cache clear` 和后台
  定时刷新全部自动覆盖，Admin 删掉的规格也会被 reconcile 正常 tombstone。
  此前 `<workload> quota` 查询和 `create --quota` 解析都要对 Workspace 里的每个
  Compute Group 各发一次规格请求（1 + N），Scope 新鲜时现在是 0 次。

- 新增按账号隔离、可定时刷新且支持手动管理的本地 Name 解析索引；`inspire cache
  status|refresh|clear` 用于查看、刷新或清理该加速层，平台 Live API 仍是资源事实源。
- CLI 的公共输入、Help、错误、人类输出和 JSON 输出保持严格 Name-only；平台 Handle
  只存在于内部解析器、Browser API 请求和本地调试实现中。
- 发现类列表默认限制为 20 项，Batch 结果和 Job 日志使用明确的输出预算；使用
  `--limit/-n` / `--all` 主动调整集合输出。
- 同类 workload 命令统一使用相同的 Name、Workspace、分页、确认、截断和 JSON 语义。

### 修复

- Handle 识别只覆盖平台真正会签发的前缀。`node-`、`task-`、`pod-`、`instance-`、
  `container-`、`group-`、`cg-`、`compute-group-`、`proj-`、`workspace-` 以及
  `lcg-1` 这类短数字后缀恢复为普通 Name：此前它们在 list / JSON 输出里显示为
  `<redacted>`，同时在输入侧被拒绝，导致这样命名的资源在 Name-only CLI 中完全无法访问。
- 交互式与远端字节流不再改写。`job shell`、`notebook shell`、`notebook exec` 和
  Jupyter 终端此前会扣住每个 chunk 结尾的疑似 Handle 片段，造成 raw 模式下无按键回显、
  提示符残缺、全屏程序刷新不全。日志正文同样按原样输出。
- 带标签的 UUID 完整打码，不再只截掉首段而留下其余部分。
- `_post-update` 重新接受并忽略 `--previous-version`：v6.2.0 及更早版本在自更新时
  一定会传该参数，缺少它会让升级在交接处失败，跳过 Skill 刷新与运行时安装。
- `notebook ssh-proxy` 重新接受并忽略 `--quiet`；`notebook ssh-config` 生成的
  ProxyCommand 恢复使用 `inspire` 的绝对路径（OpenSSH 经 `/bin/sh` 执行，PATH 与交互
  Shell 不同），且 `$HOME` 之外的 IdentityFile 不再被静默丢弃。

### 维护

- 开发依赖只保留 `[dependency-groups] dev`，移除 `[project.optional-dependencies] dev`
  与 black 配置：用 `uv sync --dev` 装开发环境，`pip install -e ".[dev]"` 不再可用。
- 移除 commitizen 配置。发版时需手动同步 `cli/inspire/__init__.py` 的 `__version__`
  与 `cli/pyproject.toml` 的 `version`，并打 `v<version>` tag 触发 publish workflow ——
  原先由 `[tool.commitizen] version_files` 保证的两处一致性现在没有工具兜底。

## v6.2.0

### 新增

- GPU Job 支持平台原生状态通知，可通过 CLI、账号配置或 Batch item 配置开关。
- 支持 Qoder Work 和 Kimi Desktop Harness。

### 变更

- 代理、登录诊断和 JSON 输出统一脱敏，不输出凭据、内部路径、请求包装或平台 Handle。
- Notebook、GPU Job、HPC、Ray、Serving、Image、Model、Project、Resources 和 User
  命令统一采用短、可读、可脚本消费的输出。

### 修复

- 修复通用 Shell proxy 与 `NO_PROXY` 的继承边界。
- 修复受限 Notebook 的内部镜像访问不应继承容器代理的问题。
- 修复 Job 通知、容错默认值和项目优先级在分层配置与 Batch 路径中的传递。

## 历史版本

请参阅 [GitHub Releases](https://github.com/realZillionX/InspireSkill/releases)。
