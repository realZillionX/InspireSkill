# Changelog

各版本条目记录该版本发布时的用户可见变化；当前用法以 README 和 CLI Help 为准。

## Unreleased

新增与修复：
- 安装器未检测到 Agent 客户端时仍安装 CLI，并提示稍后补装 Skill；`--no-cli` 且无目标客户端时继续报错。
- Bash / PowerShell 安装器新增 `--index-url` / `-IndexUrl`，支持显式选择 PyPI 镜像、保留已有索引环境变量并隐藏 URL 中的凭据；增加包与 Chromium 下载大小提示，安装后指引改用 `inspire login` 和可选 CAS 登录。
- 修复 `notebook list --workspace all` 无匹配结果时误报 APIError；仅全部空间查询失败时报错，部分失败保留正常结果与警告，并在 JSON 中新增不可用空间名称列表 `unavailable_workspaces`。
- `account check` 展示 OAuth 访问／刷新令牌有效期、本次是否刷新及 CAS 数据广场登录状态；新增 `--offline`，仅检查本地会话，不联网、不刷新、不写文件。旧会话明确提示重新 OAuth 登录。
- `account list` 增加本地 OAuth／CAS 状态与刷新有效期；SDK `AccountCheck` 增加可选 `oauth`、`cas` 和 `refreshed` 字段。
- 修复 `inspire login --cas feishu` 扫码确认后必定失败（`Feishu QR login failed`）：确认后飞书 iframe 跳转到 CAS，轮询中的页面查询会抛出 “Execution context was destroyed”；现在跳转期间的查询视为暂未匹配并继续轮询，直到拦截到回跳并保存 CAS 会话（#105）。

## v7.2.0

不兼容变化：
- 登录改用官方 OAuth 设备授权，用户名和密码不再存储或读取（包括旧配置和环境变量）；已有用户需为每个账号重新登录一次。`inspire login --account NAME` 自动创建账号并登录（不带 `--account` 时登录当前活动账号；尚无任何账号时创建 `default`），`account add` 保留为兼容创建入口；`inspire init` 发现未登录时直接发起设备授权。
- `logout` 与 `account remove` 统一为删除本地账号（令牌、配置和 bridge/tunnel 状态），需确认；平台工作负载继续运行。可用 `account add --no-login` 暂缓授权；传入旧的 `--username` / `--password` 时同样跳过授权。
- 查询不存在的 Notebook 改为立即返回 NotFound（退出码 12，原为 APIError/13 且固定等待约 13 秒）；30 秒内刚提交的 Notebook 仍会最多等待 12 秒以等待列表可见。
- `job list` 的 JSON `total` 在结果不完整时为 `null`，并新增 `truncated` / `incomplete_slices`。

新增与修复：
- 新增 `inspire login --cas`（短信两步验证或飞书扫码），按账号私有保存 CAS 会话。数据广场（`dataset list / show / tags / applications`）只支持 CAS 登录；有效 CAS 会话可自动批准 OAuth 设备授权，并在刷新凭据过期后静默续期。
- 修复 Notebook、训练、HPC、Ray、Serving 创建时公共路径挂载不生效：新增 `--public-path-mount-mode`（0 读写、1 只读、2 不挂载）、批量配置同名字段及 SDK `public_path_mount_mode` / `PublicPathMountMode`；旧布尔参数弃用并自动转换，冲突时报错；详情优先显示新挂载模式；batch dry-run 显示解析后的挂载模式。
- 修复 Notebook batch 传入不支持的 `gpu_type` 参数。
- `job list --all` 按控制台 5000 行分页窗口并发扫描各空间，不再依赖平台返回的全平台总数，能够正常返回；超出窗口时显式标记截断并在 stderr 提示。
- 创建、batch 和 dry-run 在提交前本地校验名称：Notebook / Ray / Serving 不超过 32 个字符，Job / HPC / TensorBoard 不超过 255 个字符，并按各自字符规则校验；不合法返回 ValidationError（退出码 12）。
- 默认 metrics 查询中个别指标失败时保留成功的指标，并在 JSON `errors` 或 stderr 警告中列出；显式指定的指标或全部失败仍报错。
- batch 配置 JSON/TOML 格式错误或文件无法读取时返回 ConfigError（退出码 10），并给出文件名和出错位置。
- `update --check` 失败时说明具体原因（本地源码安装、缺少 SKILL.md、版本查询失败等），不暴露本地路径。
- SDK 异常保留平台错误码、HTTP 状态、service/action、request ID 和重试提示；安全读请求遵循 Retry-After 并使用有界抖动退避，写请求不重放；服务端错误回显统一脱敏。
- SDK jobs / HPC 批量状态查询新增可选部分结果（`allow_partial`），保持输入顺序；默认严格行为和返回类型不变。
- 开发工具 `scan_v2_surface.py` 新增 `--fields` 字段令牌报告和离线 `--bundles` 扫描。

## v7.1.16

- 修复连接检查、SSH 命令和 SCP 传输未使用缓存的 `identity_file`，非默认文件名私钥认证失败的问题；未缓存私钥时保留 SSH agent 和默认密钥查找。

## v7.1.15

- 修复登录和工作空间枚举失效，支持恢复旧会话中的空目录；发现失败时保留已有缓存。
- 修复外部 Docker 镜像注册，新增 `image sync` 及同步、异步 SDK 的 `images.sync`，完成推送后的镜像目录同步。
- 修复 `job list --all` 因不准确的总数或隐藏页数上限而提前结束；异常分页不再成功返回残缺结果。
- 修正混合规格 GPU 节点组的 8 卡整节点容量计算，统一表格与 JSON 输出。
- 整理 CLI 帮助、错误恢复命令及批量提交、共享配额和跨会话任务操作指引。

## v7.1.14

- 新增 DeepSeek Harness Skill 安装、自动检测、更新与卸载支持，遵循 `DSH_HOME` 自定义目录。
- 整理 CLI、Python SDK 与平台操作参考文档。

## v7.1.13

- 修复配置 SOCKS 代理后，登录、账号检查及同步 HTTP 请求因缺少依赖而失败的问题。
- 账号配置及显式 Inspire 代理变量中的 SOCKS5 代理改由代理端解析域名，支持仅在 VPN 内可解析的平台域名；系统代理变量和浏览器保持原有行为。

## v7.1.12

- 修复 Serving 与 Ray 的交互式 Shell 连接失败。
- Serving、Ray 和 HPC 的实例选择与实例列表标签保持一致。
- Serving Shell 支持从完整分页列表中选择运行实例。

## v7.1.11

- 新增 Pi Skill 安装、更新与卸载支持。
- Pi 安装位置遵循 `PI_CODING_AGENT_DIR`，支持自定义目录和路径展开。
- 改善 Windows 上的 SDK 并发测试稳定性。

## v7.1.10

- 新增 ZCode Skill 安装与自动检测支持。
- Kimi Desktop 的安装参数改为 `--harness kimi-work`，已有安装位置不变。
- Python SDK 转为正式可用，同步与异步 API 延续 v7.1.9。
- 整理安装、账号、代理、Windows、更新与卸载指引。

## v7.1.9

- 新增实验性 Python SDK，提供同步与异步客户端、类型化资源、工作负载管理、远程执行和 Notebook 文件传输。
- 支持 SDK 账号管理、会话续期、目录缓存和流式输出。
- **兼容性变化：**六类工作负载的状态统一为大写，缺失或无法识别的状态使用 `UNKNOWN`；Job 的停止状态统一为 `CANCELLED`。依赖旧状态值的脚本需调整。
- **兼容性变化：**`cache status` 按资源身份去重，JSON 增加 `scopes` 和 `size_bytes`；账号凭据与私有状态文件收紧访问权限。
- 修复 Serving 模型查询分页、浏览器验证码判断及日志错误处理。
- 扩展 macOS 与 Windows 支持，改善异步调用和文件传输稳定性。

## v7.1.8

- Serving 列表与详情显示完整 Endpoint；新增 `serving api` 生成调用说明和 curl 示例。
- 新增 `account api-key list/create/export/run/delete`，支持推理密钥管理、文件导出和子进程环境注入。
- 密钥导出支持 Shell、PowerShell、dotenv 和原始格式，使用私有文件权限且不覆盖已有文件。
- 修复长服务地址显示和并发镜像查询的稳定性问题。

## v7.1.7

- 各命令统一支持 `--account <name>` 单次账号选择，并行命令保持各自账号上下文。
- **兼容性变化：**Notebook 不再跨账号自动查找连接，移除 `--account all`；需要其他账号时显式指定。
- 已配置账号的登录名以账号配置为准；默认 SSH Host 别名包含账号信息。
- 修复账号切换、连接复用、GPU 缓存隔离和异常退出后的上下文恢复。
- 表格统一使用 Rich，改善中文对齐和长内容显示；脚本应使用全局 `--json`。

## v7.1.6

- `resources availability` 合并配额余额、实时用量与空闲或可回收整节点容量。
- **兼容性变化：**删除 `resources nodes`，改用 `resources availability`。
- **兼容性变化：**`resources usage` 删除 `--by`，默认按 Project → User 汇总；使用 `--details` 查看任务，并支持 `--project`、`--user`、`--task` 过滤。
- 多节点 Job 要求每节点使用 8 GPU 满节点规格；`--nodes` 指定节点数。
- 修复资源分页、节点状态判断与事件时间线过滤。

## v7.1.5

- **兼容性变化：**移除仓库级配置、路径别名和工作负载 Profile；`inspire init` 只处理账号配置。
- 远端工作目录改用显式绝对路径；Job 默认读取平台日志，SSH 日志需显式指定目录。
- 更新流程支持识别并清理退役配置，账号配置保留；普通模式确认后清理，`--yes` 直接执行。
- 过期会话优先通过已有 SSO 状态续期，减少重复提交凭据。
- JupyterTerminal 执行隔离本地 stdin，修复命令读取输入导致的超时。

## v7.1.4

- 修复 Notebook 删除后重建时，过期名称缓存导致 JupyterTerminal 连接失败。
- JupyterTerminal WebSocket 遵循系统代理的 `NO_PROXY` 设置。
- 恢复 HPC Shell 的默认与显式实例连接。

## v7.1.3

- 名称缓存改为按需读取与更新，减少普通命令的后台请求。
- 镜像、跨工作区列表和资源查询支持并发；批处理复用单次命令内的目录查询。
- 新增原生 Windows 安装、Shell 和连接支持。
- 修复缓存分页、刷新恢复及零保障额度计算组的显示。
- 改善更新包解压安全性与诊断脱敏，更新依赖和 CI 运行时。
- **兼容性变化：**移除冗余的 `cache refresh --full`；账号配置不再缓存项目与资源目录。

## v7.1.2

- `job status`、`hpc status` 和 `job events` 支持一次查询多个任务。
- 修复会话失效后的重复认证，减少账号锁定风险。
- 登录失败区分凭据错误、临时限制和验证码要求，并显示可操作的错误提示。
- 默认显示版本更新提醒；修复 `update --check` 在有新版本时误报失败，以及版本查询响应不完整时的异常。
- 修复 Help 示例、参数说明、缩进和终端宽度适配。
- 完成 Browser API v2 迁移，整理使用与开发文档。

## v7.1.1

- 新增 Serving、Ray 和 HPC 的交互式 Shell，支持实例选择。
- `notebook save-image --flatten` 支持将镜像保存为单层。
- 工作负载 Quota 显示每小时点券成本；资源用量支持 `--group` 过滤并显示可抢占容量。
- **兼容性变化：**项目 JSON 的 `remaining_budget` 拆为 `my_remaining_budget` 和 `project_remaining_budget`。
- **兼容性变化：**`cache refresh` 需指定 `--resource`、`--workspace` 或 `--name` 收窄范围。
- 修复空配额缓存、模型分页、重复镜像查询、镜像标签显示和节点容量统计。
- 复用 HTTP 连接，减少重复缓存刷新请求。

## v7.1.0

- 新增数据集目录、版本、标签、校验与申请状态查询；Notebook、Job 和 HPC 支持挂载数据集。
- 新增 TensorBoard 生命周期管理及训练曲线读取。
- 补齐 HPC、Ray、Serving 的日志、实例事件、伸缩与部署观察入口。
- 新增资源用量归因、回收策略、节点事件和 Notebook 即时指标。
- 新增 Ray 启动、Serving 伸缩与回滚、模型部署配置与删除、取消镜像保存等操作。
- 扩展工作负载创建和批处理参数，支持环境变量、保留策略、时长限制及通知。
- **兼容性变化：**`image save` 改为 `notebook save-image`；删除 `config` 命令组，账号相关操作迁入 `account`。
- **兼容性变化：**Project 命令移除 `--workspace`；`--workspace all` 限于支持跨工作区发现的命令。
- **兼容性变化：**事件默认包含工作负载与实例事件；`--workload-level` 可只看工作负载级事件，实例选择使用列表中的标签。
- 修复 Shell 退出、终态等待、日志排序、镜像名称与可见性解析、配额缓存、HPC 规格校验和限流处理。

## v7.0.2

- Notebook 传输方式按实际 GPU 型号判断，并缓存探测结果。
- `cache status` 与 `cache clear` 新增 `--resource` 过滤。
- 自动清理名称索引中退役的资源类型。

## v7.0.1

- 新增 `inspire uninstall` 和安装脚本的 `--uninstall` 入口。
- 默认移除工具拥有的安装内容；`--purge` 可清理账号数据，`--purge-runtime` 可进一步清理运行时。
- 修复安装完成提示的换行。

## v7.0.0

- **兼容性变化：**删除 `notebook url`、`notebook vscode`；`notebook proxy-url` 改为打印地址。
- **兼容性变化：**删除 `user` 命令组，权限查询改用 `account permissions`；移除旧用户配额与密钥查询入口。
- 恢复 `inspire update` 的进度和版本摘要输出。
- 将主要平台操作迁移至 Browser API v2。
- Notebook 执行和 Shell 减少浏览器依赖，改善网关 URL 解析与并发连接建立。
- 修复 Serving 启停、HPC 创建与名称解析、镜像解析、Ray 列表字段及普通成员资源查询。

## v6.3.0

- 新增 Ray 指标及 Serving 事件、实例和启动命令。
- 新增按账号隔离的名称缓存和 `cache status/refresh/clear`。
- **兼容性变化：**删除资源 `id` 命令；`notebook ssh open` 改为 `notebook ssh`；指标参数 `--lcg` 改为 `--group`，Serving 的 `--shm-gib` 改为 `--shm-size`。
- **兼容性变化：**删除 Notebook 执行的旧产物中转参数与 `net-test`；文件传输使用 `notebook scp`。
- **兼容性变化：**凭据改用账号配置；连接 JSON 移除 `public_internet`，资源查询移除 `--no-cache`。
- 统一列表输出预算与名称显示，修复交互字节流、SSH 配置和旧版本更新交接。

## v6.2.0

- GPU Job 支持原生状态通知，并可通过账号配置或 Batch 条目设置。
- 新增 Qoder Work 和 Kimi Desktop 安装支持。
- 修复系统代理与 `NO_PROXY` 继承、依赖安装代理隔离及 Job 容错配置传递。
- 改善代理和登录诊断脱敏。

## v6.1.4

- 修正 GPU 调度区域、整节点与部分 GPU 规格的使用指引。
- 改善 Quota 匹配和优先级诊断。
- 明确远端网络能力与外部服务授权的使用边界。

## v6.1.3

- 改善登录失败与代理诊断。
- 修复显式账号选择下的 JupyterTerminal 会话隔离。
- 修复跨帧完成标记、远端退出码和 Shell 控制语句处理。
- 优化 Notebook 传输选择与终端发现耗时。

## v6.1.2

- 修复 Job 创建与批处理中的 `--exclude-node` 参数传递。
- 改善计算组与 Quota 的匹配说明和错误提示。
- 补充 Notebook 自动停止与平台回收策略的区别。

## v6.1.1

- 新增 `notebook shell --check` 检查 Shell 通道。
- JupyterTerminal 执行输出去除启动横幅与命令回显。
- `notebook start --wait` 显示当前状态和最近事件。

## v6.1.0

- Notebook 新增 JupyterTerminal 执行、交互 Shell 与依赖安装通道。
- 按 Notebook 网络能力选择可用传输方式。
- 新增网络检查入口与共享存储文件传输指引。

## v6.0.6

- 新增 Kimi Code 安装支持，并遵循 `KIMI_CODE_HOME`。
- 修复 SSH 目标重新发现时选中不可达连接的问题。
- 修复非 root Notebook 用户的 SSH 公钥安装位置。

## v6.0.5

- 资源可用量增加高优先级容量提示。
- Job 创建计划和列表显示共享内存与完整资源摘要。
- 修复中文表格对齐，统一共享内存单位为 GiB。

## v6.0.4

- 新增共享项目配置与按账号覆盖的项目配置。
- 新增 dotenv 加载与登记入口。
- 修复 Notebook SSH 刷新、账号目标缓存及 Job 创建回归。

## v6.0.3

- 修复项目目录发现与路径别名初始化。
- 修复 SSH 配置、代理与连接缓存的兼容问题。
- 整理资源、网络、路径与项目上下文文档。

## v6.0.2

- `inspire init` 区分账号级发现与项目级发现。
- 新增 `--no-discover`，可跳过平台发现并生成配置。
- 整理 Skill 的操作指引与参考索引。

## v6.0.1

- 修复安装后的账号、代理和初始化提示。
- 代理示例使用可替换模板，开发脚本不再默认启用固定本地代理。
- 合并安装与代理配置指引。

## v6.0.0

- **兼容性变化：**删除旧 OpenAPI 客户端及其配置入口，平台操作统一使用 Browser API。
- **兼容性变化：**移除旧 `notebook ssh` 管理子命令；日常 SSH 使用 `notebook ssh <name>`，连接管理使用 `notebook connection`。
- Job 支持 `--exclude-node`，Notebook 创建支持 `--node`。
- Job 与 HPC 的主要操作迁移至 Browser API v2。

## v5.2.5

- 修复跨工作区 Job 列表的活动状态过滤。
- 优化 Job、Notebook 与 Project 查询速度和命令退出。

## v5.2.4

- 更新摘要在 GitHub Releases 不可用时，回退读取发布包内的 CHANGELOG。

## v5.2.3

- 修复 Antigravity Skill 安装目录迁移和遗留目录清理。

## v5.2.2

- 新增 Antigravity 与 Cursor 安装支持。
- `inspire update` 显示跨版本更新摘要。
- 同步发布说明与 CHANGELOG。

## v5.2.1

- 新增 Qoder Skill 安装与更新支持。

## v5.2.0

- 修复首次安装、文件目录解析与浏览器终端清理。
- 改善登录与浏览器运行时错误提示。
- 统一 HPC、Ray 和资源查询的名称输入边界。

## v5.1.25

- 镜像列表默认显示所有可见来源，可用 `--source` 收窄范围。

## v5.1.24

- 路径别名改用平台文件目录结果，修复个人共享存储路径解析。

## v5.1.23

- 个人路径别名仅使用平台返回的目录名，缺失时不再以登录名替代。

## v5.1.22

- 修复初始化时的个人共享存储路径解析，并在配置刷新时保留目录名。

## v5.1.21

- 新增 HTTP 登录路径，降低浏览器登录卡顿。
- 登录密钥信息从当前页面读取，保留浏览器兼容回退。

## v5.1.20

- 安装与更新自动准备所需浏览器运行时。
- `update --cli-only` 更新 CLI 与运行时，不刷新 Skill。
- 修复发布窗口内安装到旧包时的更新交接。

## v5.1.19

- 新增 `notebook ssh <name>`、`notebook connection`、`ssh-config` 与 `ssh-proxy`。
- 支持 OpenSSH、SCP、rsync 和编辑器远程连接集成。
- 修复初始化模板、空配置写入和残留账号目录处理。

## v5.1.18

- 浏览器运行时支持复用本机 Chrome 或 Chromium，并可指定可执行文件与通道。

## v5.1.17

- 修复 SSH 初始化时将终端命令回显误判为失败的问题。

## v5.1.16

- SSH 初始化按 Ubuntu 版本选择兼容的 OpenSSH 软件包。

## v5.1.15

- 修复 Ubuntu 22.04 Notebook 的 OpenSSH 安装源选择。

## v5.1.14

- 改善 Ubuntu 22.04 Notebook 的 OpenSSH 兼容性。

## v5.1.13

- 修复多账号切换时的项目配置、运行时缓存与连接状态隔离。
- 会话刷新保留其他账号的有效登录缓存。

## v5.1.11

- 更新依赖源和 Notebook 环境准备指引。

## v5.1.10

- 修复精简容器缺少字体配置时的浏览器崩溃。

## v5.1.9

- 浏览器依赖修复后继续使用已有账号初始化，避免重复询问凭据。

## v5.1.8

- 安装器优先复用可用浏览器运行时；系统依赖安装由初始化流程显式确认。

## v5.1.7

- 改善精简 Linux 容器中的浏览器依赖安装和失败诊断。

## v5.1.6

- 修复 Notebook 容器中的 Chromium 启动与页面关闭问题。
- 区分浏览器运行时错误和账号认证失败。

## v5.1.5

- 修复初始化时登录名错误后的重新登录流程。
- 明确平台登录 ID 与显示名的区别。

## v5.1.4

- **兼容性变化：**查询统一使用 `--limit/-n`，Quota 迁入各工作负载命令组。
- **兼容性变化：**操作命令要求明确工作区；JSON 使用全局 `--json`，删除操作统一使用确认参数。
- 改善计算组过滤、名称匹配和 Help 一致性。

## v5.1.3

- 整理依赖源、环境准备和镜像保存工作流。
- 清理重复与过期文档说明。

## v5.1.2

- 补充工作负载选型、依赖安装和镜像准备说明。
- 更新相关命令 Help 与 README。

## v5.1.1

- 明确项目预算、资源容量与任务优先级的不同用途。
- 更新资源选择和工作负载创建指引。

## v5.1.0

- **兼容性变化：**删除 `notebook top`，改用 `notebook metrics`；删除 `job status-catalog`。
- Notebook SSH 与路径管理分别归入对应子命令组。
- 扩展主要命令的 Help、参数说明和工作流示例。

## v5.0.0

- **兼容性变化：**删除 `inspire run`，改用 `job create`；批处理迁入各工作负载命令组。
- **兼容性变化：**移除根级 Profile 与隐式调度默认值，创建条件需显式提供。
- 统一资源名称输入和紧凑输出，隐藏平台内部句柄。
- 工作负载查询默认限定当前账号，统一创建、观察与批处理行为。

## v4.1.4

- 整理 Browser API 开发参考，使说明与现有功能一致。

## v4.1.3

- 安装与更新强制刷新包索引，修复缓存导致无法获取新版本的问题。

## v4.1.2

- 版本检查优先使用 PyPI，网络失败时回退到 GitHub。

## v4.1.1

- 补充镜像、模型、指标、路径与配额使用文档。
- 改善名称显示与更新时的包镜像重试。
- 发布流程支持跳过已存在的同版本文件。

## v4.1.0

- 新增 Job 交互式 Shell 与实例选择。
- 初始化支持发现流程与首次账号创建，安装器准备浏览器运行时。
- Job 输出同时保留平台日志与共享存储日志。
- 修复 Notebook 工作目录、stdin 传递及账号连接缓存选择。

## v4.0.0

- **兼容性变化：**项目配置拒绝账号级字段；资源操作要求明确名称与工作区。
- 强化账号、项目和连接状态隔离。
- 改善旧配置迁移、初始化诊断和资源名称解析。

## v3.2.0

- 账号添加自动检查运行环境与浏览器依赖。
- 隔离旧版本配置，提示退役的工作区默认值。
- 修复 Notebook SSH 连接和无活动账号时的初始化错误。

## v3.1.0

- **兼容性变化：**移除隐式默认工作区，创建任务需显式传入 `--workspace`。
- 初始化与配置展示不再生成或依赖旧工作区默认值。

## v3.0.3

- 修复安装引用解析、PATH 配置与重复安装处理。
- 安装时按实际环境读取代理，改善升级失败提示。
- 较早版本若无法自动升级，可重新运行安装器，或使用 `uv tool upgrade inspire-skill`、`pipx upgrade inspire-skill`。

## v3.0.2

- 修复 uv tool 与 pipx 安装识别，恢复自动升级。
- 旧版本若无法运行更新，可先通过对应包管理器升级。

## v3.0.1

- 文本配置统一使用 UTF-8，修复 Windows 中文配置读写失败。
- 更新安装与平台支持说明。

## v3.0.0

- **兼容性变化：**资源规格统一使用 `--quota gpu,cpu,mem`；Ray 使用 `--head-quota` 和 Worker 规格。
- HPC 区分节点规格与 Slurm 任务参数。
- **兼容性变化：**Notebook 名称成为连接标识，移除旧 SSH 别名与相关参数。
- 统一优先级、资源发现和依赖安装行为。

## v2.0.1

- 默认通过 PyPI 安装与更新。
- PyPI 项目页显示 README，发布使用可信发布流程。

## v2.0.0

- **兼容性变化：**资源命令使用名称，移除原始 ID 与旧工作区角色参数。
- 新增账号管理和按账号隔离的配置与会话存储。
- 新增 Ray 管理与多类工作负载指标。
- 资源规格按平台目录解析，改善配置写入与名称查找。

## v1.0.1

- 新增 Notebook、Job 和 HPC 删除操作。
- 新增镜像可见性设置与 Notebook 计算组选择。
- 初始化支持存储层选择，修复镜像保存和镜像结果解析。

## v1.0.0

- 首次发布 InspireSkill CLI 与 Agent Skill。
- 支持 Notebook 生命周期、SSH、远程执行和文件传输。
- 支持 GPU Job、HPC、镜像、资源、项目与账号相关操作。
- 提供安装、配置和工作负载使用指引。
