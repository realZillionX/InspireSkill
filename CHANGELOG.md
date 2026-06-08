# Changelog

本文件同步 GitHub Releases 正文格式；Release 页面是发布说明的标准口径。

# Unreleased

## 更新内容

### 变更

- 调整 `inspire init` 的 scope 语义：默认 `--scope global`，裸 `inspire init` 会执行全局发现，并把 project catalog、compute group catalog 和 `[path_aliases]` 等账号级发现结果写入 `~/.inspire/accounts/<account>/config.toml`。
- `inspire init --scope project` 现在执行项目发现：刷新账号级 catalog，并把当前仓库的 `[context]` 与项目级 `[path_aliases]` 覆盖写入 `./.inspire/accounts/<account>/config.toml`；项目级 path alias 会覆盖账号级默认值。
- 新增 `inspire init --no-discover`，用于跳过平台发现，只走旧的 env smart init / template config 写入路径。

# v6.0.1

## 更新内容

### 修复

- 修复安装器完成后的下一步提示：账号和代理配置统一指向 `inspire account add <name>`，随后再运行 `inspire config show --compact`、`inspire init` 和资源可见性检查。
- 修正 `inspire account add` 与首次 `inspire init` 的 proxy 提示，把 `http://127.0.0.1:7897` 明确标成 Clash mixed port 示例，不再暗示本机端口固定。
- Browser API reverse-capture 开发脚本默认不再启用 `http://127.0.0.1:7897` 代理；需要代理时显式传 `--proxy http://127.0.0.1:<mixed-port>` 或设置 `INSPIRE_PLAYWRIGHT_PROXY`。

### 文档

- 合并 `references/setup/proxy-setup.md` 到 `references/setup/install-and-config.md`，安装、更新、账号、项目初始化和 SII proxy setup 统一看一份文档。
- SII Proxy 文档改为模板语义：本机 mixed port、SII 节点数量和节点端口都按使用者环境填写，不照抄任何本机配置。
- Clash Verge `SII Proxy` 组改为 `select` 模板，并加入 `DIRECT` 选项；公网环境可选 SII proxy 节点，能直连 SII 校园网时可选 `DIRECT`。
- README 和开发文档同步更新，避免把 `7897` 写成 CLI 绑定端口或默认代理事实。

### 验证

- `uv run pytest tests/test_account_commands.py tests/test_config_files.py -q`
- `uv run pytest tests/test_init_command.py tests/test_cli_help_boundaries.py tests/test_main_help.py -q`
- `uv run ruff check inspire/cli/commands/account/add.py inspire/cli/commands/init/init_cmd.py inspire/cli/commands/init/templates.py scripts/reverse_capture/capture.py`
- `git diff --check`

# v6.0.0

## 更新内容

### 破坏性变更

- 完全删除仓库内 OpenAPI 客户端与配置入口；CLI 不再提供 `openapi_prefix`、`auth_endpoint`、`INSPIRE_OPENAPI_PREFIX` 或 `INSPIRE_AUTH_ENDPOINT`，平台交互统一走 Browser API / Web session。
- 删除旧的 `inspire notebook ssh connect/test/refresh/forget` 兼容子命令。SSH 日常入口统一为 `inspire notebook ssh <name> [-- <command>]`；缓存管理统一使用 `inspire notebook connection list/status/refresh/forget/prune`。
- 删除 `references/dev/openapi.md` 和已完成的 `references/dev/notebook-ssh-cli-redesign.md`，避免 Agent 继续按历史设计草稿或 OpenAPI 文档执行。

### 新增

- `inspire job create` 新增 `--exclude-node <NODE_NAME>`，batch job item 支持 `exclude_nodes`，用于提交训练任务时排除指定节点。
- Notebook 创建支持前端同款 `--node <NODE_NAME>` 节点指定，并在创建 / 查询后输出 Web IDE、proxy URL、VS Code proxy suffix 等 name-only URL 辅助信息。
- Browser API 开发文档补充 job / HPC v2 Action 接口、job instances、Jupyter / 终端代理和 notebook SSH bootstrap 关系。

### 变更

- Job create/stop/detail/list/instances 迁移到 Browser API v2 Action：`CreateJobConsole`、`StopJob`、`GetJob`、`ListJobs` 和 `ListJobInstances`。
- HPC create/status/stop 迁移到 Browser API v2 Action：`CreateJobConsole`、`GetJob` 和 `StopJob`。
- Job / HPC batch 提交流程改为复用 Browser API；`AuthManager` 仅保留账号缓存清理兼容 shim，`get_api()` 不再可用。
- Notebook SSH、exec、scp、job logs 和 install-deps 的错误提示统一指向 `notebook ssh` 与 `notebook connection ...` 新入口。
- `notebook connection list/status` 的 JSON payload 不再暴露 notebook / workspace 平台 handle，延续 Name-only 输出边界。

### 验证

- `uv lock --check`
- `uv run pytest -q`
- `uv run ruff check inspire tests`
- `uv run mypy`
- `uv build`
- `git diff --check`

# v5.2.5

## 更新内容

### 修复

- 修复 `inspire job list --workspace all --active` 只在本地过滤最近一页全量 job 的问题；现在会向平台下推 `job_pending`、`job_creating`、`job_queuing` 和 `job_running` 状态查询，不再把 `job_succeeded` 误显示为 active 结果。
- 修复 `inspire job list --workspace all --status RUNNING` 没有向平台下推 `job_running` 状态的问题，避免跨 workspace 扫描慢且无法快速确认当前账号没有 RUNNING job。
- 优化 `inspire notebook list --workspace all -s RUNNING` 的跨 workspace 查询路径，复用并发 notebook lister，避免逐个 workspace 串行等待。

### 验证

- `uv lock --check`
- `uv run pytest -q`
- `uv run ruff check inspire tests`
- `uv run mypy`
- `uv build`
- Live smoke：`uv run inspire job list --workspace all --active`
- Live smoke：`uv run inspire job list --workspace all --status RUNNING`
- Live smoke：`uv run inspire notebook list --workspace all -s RUNNING`

# v5.2.4

## 更新内容

### 修复

- 修复 `inspire update` 在无法获取 GitHub Releases API 数据时不显示跨版本更新摘要的问题；现在会回退读取发布包中与 Releases 同步的 `CHANGELOG.md`。
- 更新摘要继续以 GitHub Releases API 为第一来源，API 不可用时使用同一发布 tarball 的 `CHANGELOG.md` 兜底，避免成功升级后只看到“未能获取更新内容”的提示。

# v5.2.3

## 更新内容

### 修复

- 修复 `inspire update` 自更新后仍用旧进程继续刷新 skills 的问题。升级 CLI 后会调用新安装的 `inspire _post-update` 完成 skill 刷新、安装审计、Playwright runtime 校验和 Release 更新摘要，避免旧版本逻辑继续写过期 harness 路径。
- 修复从旧 Gemini CLI 目录迁移到 Antigravity 时留下 `~/.gemini/skills/inspire/` 的问题；安装器和 `inspire update` 现在会清理该遗留目录，并使用官方 `~/.gemini/config/skills/inspire/`。

### 说明

- 如果已经从 `5.2.1` 升到 `5.2.2` 并看到 `~/.gemini/skills/inspire` 被刷新，运行 `inspire update` 升到 `5.2.3` 后会改用新的 post-update 流程；如需立即重刷 skill，可在升级后运行 `inspire update --skill-only`。

# v5.2.2

## 更新内容

### 新增

- 新增 Antigravity Harness 支持：安装器和 `inspire update` 可通过 `~/.gemini` 探测 Antigravity，并将 InspireSkill 安装 / 刷新到官方全局目录 `~/.gemini/config/skills/inspire/`；旧 Gemini CLI harness 名称和错误的 `~/.gemini/skills/inspire/` 路径不再作为公开入口。
- 新增 Cursor Harness 支持：安装器和 `inspire update` 可探测 `~/.cursor`，并把 InspireSkill 安装 / 刷新到 `~/.cursor/skills/inspire/`。
- `inspire update` 成功升级 CLI 后会显示旧版本到新版本之间的 GitHub Release 更新内容摘要，方便用户一次看完跨版本变化。

### 变更

- README、安装手册和 Agent 可见说明同步将支持矩阵更新为 Claude Code / Codex / Antigravity / Cursor / OpenClaw / OpenCode / Qoder 七家 harness。
- `CHANGELOG.md` 改为同步 GitHub Releases 正文格式，以 Release 页面为发布说明标准口径。

# v5.2.1

## 更新内容

### 新增

- 新增 Qoder Harness 支持：安装器和 `inspire update` 可探测 `~/.qoder`，并把 InspireSkill 安装 / 刷新到 `~/.qoder/skills/inspire/`。

# v5.2.0

## 更新内容

- 修复首次安装路径中 INSTALLER 未初始化导致 install.sh 在 set -u 下退出的问题。
- 修复文件页 Browser API 目录解析不接受布尔型 is_share 的问题。
- 统一 Web session Playwright 运行时失败提示，避免泄漏底层 playwright install 命令。
- 改善 Notebook 当前账号 live 查询失败诊断，保留 /api/v1/user/detail 的真实失败原因。
- 修复 rtunnel 浏览器 terminal fallback 过早清理和清理 URL 错误的问题。
- 收紧 Name-only 边界：HPC / Ray 普通命令和 hpc metrics 拒绝平台 handle，短平台 handle 继续被清洗。
- 修复 config context 的 display-only 项目 context 展示，并更新文档、测试和版本到 5.2.0。

# v5.1.25

## 更新内容

- 修复 `inspire image list` 默认只列官方镜像导致公开可见镜像不出现的问题；现在默认查询 official、public、private 三类可见来源并去重。
- 更新镜像管理文档与 Browser API 开发文档，明确默认列表范围以及按 `--source` 收窄的用法。
- 补充回归测试，覆盖 plain `image list` 能返回公开可见镜像，例如 `lyz-dev:100`。

# v5.1.24

## 更新内容

- 修复 path alias 初始化时把登录账号误当个人目录名的问题，改为从文件页 Browser API 的项目目录结果读取真实共享存储路径。
- 新增文件页 Browser API 封装与开发文档，记录系统存储类型、项目目录、SFTPGo / WebDAV 连接信息等接口边界。
- 补充回归测试，覆盖 path alias 不再依赖 train_job/workdir 以及文件目录 API 的请求行为。

# v5.1.23

## 更新内容

### 修复
- 收紧 `inspire init` 的 path alias 生成边界：个人路径 alias 只使用平台 `/train_job/workdir` 返回路径里的 `<path-user>` 段，不再回退到登录账号名。
- 当平台没有返回共享盘个人目录名时，只生成不依赖个人目录的 `public` alias，避免把账号 ID 写进 `/inspire/<tier>/project/<topic>/...` 或 `/inspire/<tier>/global_user/...`。

### 验证
- `uv lock --check`
- `uv run pytest -q`
- `uv run ruff check inspire tests`
- `uv run mypy`
- `uv build`
- GitHub Actions `CI` 通过。
- GitHub Actions `Publish to PyPI` 通过。

# v5.1.22

## 更新内容

### 修复
- 修复 `inspire init` 生成 path alias 时把登录账号名误当作共享盘个人目录名的问题。现在会从平台 `/train_job/workdir` 返回的真实路径中解析 `<path-user>`，例如把 `me` 写成 `/inspire/<tier>/project/<topic>/tongjingqi-CZXS25110029/` 这类真实目录。
- `project_catalog` 保留 `path_user`，后续刷新项目配置时不会丢失平台文件系统目录名。
- `inspire update` 在已经解析到目标版本时，会让 `uv tool install` 显式安装 `inspire-skill==<version>`，降低 PyPI 索引缓存导致全局 CLI 停在旧版本的概率。

### 文档
- 更新 path alias、安装配置和 Browser API 说明，把 `<user>` 改为平台共享盘返回的 `<path-user>` 语义。

### 验证
- `uv lock --check`
- `uv run pytest -q`
- `uv run ruff check inspire tests`
- `uv run mypy`
- `uv build`
- GitHub Actions `CI` 通过。
- GitHub Actions `Publish to PyPI` 通过。

# v5.1.21

## 更新内容

### 修复

- 合并 PR #30：新增 request-based CAS 登录路径，先通过 HTTP 完成 Keycloak CAS broker / CAS 表单 / RSA 加密登录，避免 Playwright renderer / DOM selector 在代理或 headless 环境中卡住导致 CLI 登录无输出。
- CAS RSA 公钥改为从当前 CAS 页面或同源登录脚本动态解析，生产代码不再硬编码平台登录页密钥。
- 保留 Playwright 登录作为 request CAS 登录失败后的 fallback。

### 验证

- `uv lock --check`
- `uv run pytest -q`（`957 passed`）
- `uv run ruff check inspire tests`
- `uv run mypy`
- `uv build`
- PR #30 的 `main` 合并后 CI 通过。
- GitHub Actions：`CI` 和 `Publish to PyPI` 通过。

# v5.1.20

## 更新内容

### 修复

- 安装脚本和 `inspire update` 现在会自动准备并验证全局 `inspire` 使用环境里的 Playwright Chromium runtime，避免 Agent 需要改用绝对路径或底层 Playwright 命令。
- `inspire update --cli-only` 现在只更新 CLI 包与运行时，不刷新 skill 文件。
- 账号初始化和登录错误提示统一指向标准 `inspire update --cli-only` 恢复入口；skill 刷新校验会拦截底层 Playwright 修复命令回流。
- 安装脚本在发布窗口遇到旧包时，会使用已安装 `inspire` wrapper 的解释器做内部兜底，不再走独立 `uvx --from` 路径。

### 验证

- `uv lock --check`
- `uv run pytest -q`（`952 passed`）
- `uv run ruff check inspire tests`
- `uv run mypy`
- `uv build`
- GitHub Actions：`CI` 和 `Publish to PyPI` 通过。

# v5.1.19

## 更新内容

### 新增

- 重构 notebook SSH 命令面：新增 `inspire notebook ssh <name> [-- <command>...]`，让常规 SSH 使用路径回到直觉入口。
- 新增 `inspire notebook connection list/status/refresh/forget/prune`，把连接缓存管理从 SSH 主入口中拆出。
- 新增 `inspire notebook ssh-config` 和 `inspire notebook ssh-proxy`，支持原生 OpenSSH、scp、rsync 和 VS Code Remote SSH 等集成场景。

### 修复

- 修复 `inspire init --scope global` 的模板层边界：账号层模板不再写入仓库级配置，项目层模板不再写入账号级配置。
- 修复 env smart init 在没有匹配变量时可能写入空配置或覆盖已有文件的问题。
- 修复 `inspire account add <name>` 对残留账号目录的处理：读取密码前先报 `Account already exists`。
- 改进 stopped notebook 的 SSH 错误提示，保留停止原因并给出可执行的 `notebook start ... --wait` 命令。

### 文档

- 更新 notebook、workflow、安装配置、service proxy 和开发文档，补齐新 SSH 命令面、连接缓存和 OpenSSH 集成说明。

### 验证

- `uv lock --check`：通过
- `uv run pytest -q`：`943 passed`
- `uv run ruff check inspire tests`：通过
- `uv run mypy`：通过
- `uv build`：通过
- `git diff --check`：通过
- GitHub Actions `CI`：通过（Python `3.10`、`3.11`、`3.12`）
- GitHub Actions `Publish to PyPI`：通过
- PyPI 当前版本：`5.1.19`

# v5.1.18

## 更新内容

### 修复

- 修复 bundled Chromium 不可用时 Playwright 无法复用本机 Chrome / Chromium 的问题：新增 `INSPIRE_PLAYWRIGHT_CHROMIUM_EXECUTABLE` 和 `INSPIRE_PLAYWRIGHT_CHROMIUM_CHANNEL` 配置入口，显式 executable path 优先于 channel，便于在受限环境中完成登录和 Web session 获取。

### 验证

- `uv run pytest -q`：`929 passed`
- `uv run ruff check inspire tests`：通过
- `uv run mypy`：通过
- `uv build`：通过
- GitHub Actions `CI`：通过
- GitHub Actions `Publish to PyPI`：通过

# v5.1.17

## 更新内容

### 修复

- 修复 Notebook SSH bootstrap 的 marker probe 误判：Jupyter terminal 的命令回显不再会被当成 OpenSSH / rtunnel 的真实失败 marker。probe 命令现在会先经过 base64 包装再写入终端，stdout marker 匹配逻辑保持不变。

### 验证

- `uv run pytest -q`
- `uv run ruff check inspire tests`
- `uv run mypy`
- `uv build`

# v5.1.15

## 更新内容

### 修复

- 修正 Ubuntu 22.04 / `jammy` SSH bootstrap 的 OpenSSH 降级来源：不再写公网 Ubuntu apt 源或要求可上网区，改为临时使用 SII 内部 Ubuntu apt 源 `http://nexus.sii.shaipower.online/repository/ubuntu` 安装 / 降级 22.04 OpenSSH；失败提示同步改为内部源可达性问题。

# v5.1.14

## 更新内容

### 修复

- 临时兼容 Ubuntu 22.04 / `jammy` Notebook 的 SSH bootstrap：已存在的 22.04 OpenSSH 会被直接接受；缺失 OpenSSH 或检测到 24.04 OpenSSH 时，会通过 apt 联网安装 / 降级到 22.04 OpenSSH，覆盖 `paper-repro:v2` 这类镜像的 SSH 配置路径。该分支要求 notebook 位于可上网区，失败时会给出明确错误和远端日志路径。

# v5.1.16

## 更新内容

### 修复

- 收敛 Notebook SSH bootstrap 的 OpenSSH 安装逻辑：容器侧动态读取 Ubuntu `VERSION_CODENAME`，统一使用 SII 内部 Ubuntu apt 源安装或校正 `openssh-server`、`openssh-client` 和 `openssh-sftp-server`。Ubuntu 22.04 镜像误装 Ubuntu 24.04 OpenSSH 时会先卸载再按 `jammy` 候选版本重装；不强制降级 `libc6` / `libtinfo6` 等基础包。rtunnel 仍从 `global_public` kit 零拷贝执行，OpenSSH 不再回退 `$KIT/sshd-debs`。

# v5.1.13

## 更新内容

### 修复

- 修复多账号在同一工作区切换时复用旧项目配置的问题：项目级配置现在按活动账号写入 `./.inspire/accounts/<account>/config.toml`，避免 workspace、path alias 和 workload profile 在不同账号之间串用。
- 修复同一进程内多次 `inspire account use` 后可能继续使用旧账号运行时缓存的问题：OpenAPI token/client、Browser API base URL / prefix 和资源 availability 进程缓存会随账号切换刷新。
- 修复 Web session 过期刷新时误清其它账号登录缓存的问题：默认只清当前账号的 `web_session.json`，保留被切走账号的登录态，方便快速切回。
- 修复 Notebook rtunnel proxy state 的账号隔离边界：真实账号 alias 使用 `~/.inspire/accounts/<account>/rtunnel-proxy-state.json`，Notebook SSH bridges 和 Web session 继续保留在同一账号目录；切换账号不会删除被切走账号的本地缓存。

# v5.1.11

## 更新内容

### 变更

- 同步 SII 内部源文档：更新 PIP / PyPI、Conda、npm、Maven、PyTorch wheel、Docker 镜像仓库、OSS 和 NTP 的 Agent 可执行入口。
- 收敛内部源说明边界：根 SKILL.md 继续只保留公网 / 内部源判断原则，具体地址集中在 references/resources-and-paths.md，避免日常入口过重。
- 更新 notebook 基底环境示例，使用新的 PyPI 内部源配置路径。

# v5.1.10

## 更新内容

### 修复

- 修复部分可上网 GPU Notebook 自定义镜像缺失 fontconfig 配置时，Chromium 能启动但在启智登录成功后渲染 SPA 过程中崩溃，导致 `inspire init` 报 `Playwright Chromium closed during Inspire login` 的问题。登录流程现在在认证 cookie 可用后立即通过 Browser API 捕获 session 和 workspace 列表，不再依赖完整渲染启智前端页面。

# v5.1.9

## 更新内容

### 修复

- 修复 `inspire init` 在 Notebook 容器首次接受 Playwright 系统依赖安装后，仍转入 username / password 重新确认提示的问题。现在浏览器运行时修复完成后会直接使用原账号配置刷新 Web session，只有真实账号或 session 失败时才提示重新确认登录信息。

# v5.1.8

## 更新内容

### 修复

- 收紧 Playwright 系统依赖安装边界：安装器和 `inspire account add` 不再主动运行 `playwright install --with-deps chromium`，避免在已有镜像环境中无提示改动 apt 层；它们只预装浏览器二进制并做启动探测。
- `inspire init` 仍会在 Chromium 无法启动时提示安装 Linux 系统依赖，只有用户确认后才运行 `--with-deps` 修复。已有可用 Playwright / Chromium 运行时会被 launch probe 识别并直接复用。

# v5.1.7

## 更新内容

### 修复

- 修复启智 Notebook 的最小 Ubuntu 镜像内安装 InspireSkill 后，`inspire init` 因 Playwright Chromium 缺少 `libglib-2.0.so.0` 等系统动态库而在 `BrowserType.launch` 阶段失败的问题。Linux root + `apt-get` 环境下，安装器、`account add` 和 `init` 的浏览器修复路径现在会使用 `playwright install --with-deps chromium`。
- 改善 Playwright 浏览器启动失败诊断：当 Chromium 可执行文件缺失或系统依赖缺失时，`inspire init` 会给出可执行的修复命令，并在交互初始化路径中重新尝试安装浏览器运行时。

# v5.1.6

## 更新内容

### 修复

- 修复启智 Notebook 容器内运行 `inspire init` 时，Playwright Chromium 在 `page.goto()` 阶段关闭并报 `Target page, context or browser has been closed` 的问题。所有 CLI Playwright 启动入口现在统一带上容器兼容参数，覆盖 root/no sandbox 和小 `/dev/shm` 环境。
- 登录浏览器仍异常关闭时，CLI 现在会给出浏览器运行时 / 容器环境诊断，不再直接暴露 Playwright 底层 `Page.goto` 错误，也不再把这类失败误导成账号密码问题。

# v5.1.5

## 更新内容

### 修复

- 修复 `inspire init` 在 active account 配置了错误平台 username 时的重新登录恢复路径。缓存 Web session 失效后，`init` 现在会要求 Agent 确认平台登录 username，明确它必须是登录 ID，而不是网页显示名；发现流程成功后，会把修正后的 username 和密码持久化到当前账号配置。
- 改善账号配置 prompt、登录失败诊断和安装配置文档：`INSPIRE_USERNAME` / `auth.username` 必须是平台登录 ID；在启智 Notebook 里运行 InspireSkill 时仍需要 CLI 自己生成 Web session，不会继承打开 Notebook 的浏览器 SSO 登录态。

# v5.1.4

## 更新内容

### 变更

- 统一 CLI 查询命令边界：删除历史分页暴露，统一使用 `--limit/-n`；`resources specs` 收敛为各 workload 的 `quota` 命令；查询类 `--group` 明确支持 keyword / substring，操作类 `--group` 继续要求完整 compute group 名称。
- 统一 CLI 操作命令边界：操作命令只接受单一明确 workspace，不再接受 `all`、`current` 或 raw workspace ID；删除 command-local `--json`，统一使用全局 `inspire --json ...`；删除类命令统一为 `--yes/-y`。
- 收紧 workload create / batch 参数：`--profile` 与显式调度条件互斥，batch defaults 与 item 合并后同样校验；Ray create 删除 `--head-*`，统一使用普通 head 条件参数，并严格校验 repeatable `--worker` schema。
- 收紧边角命令参数：`notebook shell` 和 `notebook ssh test` 不再依赖隐式缓存目标；`image save` 必须显式传 `--workspace`；`image` 可见性统一为 `--visibility private|public`；`init --global/--project` 改为 `--scope project|global`。
- 将 quota 从 `resources` 命名空间拆出到 `notebook quota`、`job quota`、`hpc quota`、`ray quota` 和 `serving quota`，避免把配额语义混进资源节点查询。

### 修复

- 测试套件全局禁用 CLI 启动阶段的后台 update check，避免 CI 中反复调用 CLI 时派生大量孤儿 Python 进程并拖挂 Python 3.12 作业。
- CI 恢复普通 `uv run pytest -q` 执行路径，并保留 job 级超时保护。

# v5.1.3

## 更新内容

### 变更

- 清扫 Agent 手册和 references 的上下文污染：移除内部源说明网址残留、旧 project 元数据提示，以及“为了说明没用而提到没用入口”的文档内容。
- 明确内部源使用和 `image save` 工作流： 内部源可以优先在目标 notebook 中按实际可达性配置，依赖跑通后仍应保存镜像；保存过程中 notebook 暂不可操作，保存完毕后不会自动停止。
- 统一本地文档和记忆里的操作者叫法：泛指操作者、读者、命令消费者和维护执行者时统一写 `Agent`；平台登录实体、权限主体和 API 字段按技术语义写“账号”、`user_id`、`username`、`/user/detail` 等。
- 将开发原则维护进 `CONTRIBUTING.md`，覆盖事实来源、Name-only 合同、配置边界、平台 workflow、文档边界、验证和交付要求。

# v5.1.2

## 更新内容

- 明确日常 workspace 模型：`CPU资源空间` 用于 CPU notebook、联网下载、依赖安装和镜像准备，`分布式训练空间` 用于 GPU notebook、GPU job、serving 和训练调试。
- 根据飞书内部源指南补充 SII 内部源使用说明。
- 更新 notebook、job 和 install-deps 相关 help，覆盖内部源、离线 GPU workflow 和依赖准备路径。
- README hero 增加 SII logo。
- 将 `inspire-skill` 版本 bump 到 `5.1.2`。

# v5.1.1

## 更新内容

这是 v5.1.0 之后的文档 / help 修正补丁，核心是纠正 `inspire project` 和项目点券在使用手册中的定位。

修正内容：
- 将 `project` 从日常个人算力决策主路径中降级为项目组级元数据入口。
- 明确项目点券 / 预算通常是项目小组整体限制，个人日常调用算力一般不把它作为首要瓶颈。
- 日常创建 notebook / job / HPC / Ray / serving 时，优先依据 workspace、compute group、`resources specs` 和实时空余做决策。
- `inspire project list/detail` 只在需要确认项目归属、负责人、组级预算 / 点券，或平台返回项目级限制提示时使用。
- 将 `--priority` help 中的 “Project quota may cap” 改为所选项目的平台策略可能裁剪请求值，避免把点券误写成个人算力限制。
- 同步更新 `SKILL.md`、resources / notebook / compute-workloads references、project / user / workload create help 和相关测试断言。

版本：
- `inspire-skill` 从 `5.1.0` bump 到 `5.1.1`。

验证：
- `uv run inspire --version` -> `inspire, version 5.1.1`
- `uv run pytest -q` -> `882 passed`
- `uv run ruff check inspire tests` -> passed
- `uv run mypy` -> passed
- CLI help 自省检查 `138` 个 help 页面，无不应出现的内部实现术语。
- `git diff --check` -> passed
- `uv build` -> built `dist/inspire_skill-5.1.1.tar.gz` and `dist/inspire_skill-5.1.1-py3-none-any.whl`

# v5.1.0

## 更新内容

这是 v5.0.0 之后的整理发版，覆盖 4 个已合入主线的 CLI 收敛提交，以及本次统一使用手册、CLI help 和版本 bump。发布重点是把 v5.0.0 的 Name-only / explicit-profile 主线继续收口，并让命令帮助与手册成为一套黑盒、可执行的使用说明。

CLI 命令面与兼容性：
- 删除 `inspire notebook top`。GPU 观察统一使用 `inspire notebook metrics <name>`；需要容器内瞬时状态时使用 `inspire notebook exec <name> "nvidia-smi"`。
- 删除 `job status-catalog` 相关命令面和测试，资源节点观察收敛到 `resources nodes`。
- Notebook SSH 连接命令统一归组到 `inspire notebook ssh ...`，连接、测试、刷新、遗忘等操作的 help 与测试跟随更新。
- Notebook path alias 从通用 notebook 命令中拆出为 `inspire notebook path ...`，配置层增加 path alias 专用导出，远端路径语义更清晰。
- 账号 remove / use、notebook remote exec / shell / scp、job logs 等帮助与提示继续按 name-first 使用方式收口。

CLI help：
- 扩展根命令 help，明确日常工作流：`config context`、`resources specs`、create / profile、events / logs / metrics / status / instances。
- 扩展 `resources`、`notebook`、`job`、`hpc`、`ray`、`serving`、`model`、`image`、`project`、`user` 等命令组说明，加入功能边界、使用方法和示例。
- 补齐 `job create`、`hpc create`、`ray create`、`serving create`、`model register`、notebook exec / scp / install-deps 等子命令的黑盒说明。
- 清理用户可见 help 中的内部实现词，保留名称、路径、资源规格、提交计划和运行观察这些可执行概念。

统一使用手册与 references：
- 重写 `SKILL.md` 为统一手册，不再区分人类和模型两套原则；适合人工快速判断的表达，也适合模型稳定执行。
- 把平台使用模型整理成四层：调度条件、远端文件、工作负载、观察与收尾。
- 明确 `workspace`、`project`、`group`、`quota`、`image` 是显式调度条件；path alias 只表示远端路径，不能替代 workload profile。
- 重构 `resources-and-paths`、`notebook`、`compute-workloads`、`workflows`、`image-management`、`model` 等手册，减少零散技巧堆砌，改为围绕平台工作流解释。
- 明确联网边界：联网下载、拉 Git、装 PyPI / apt / Hugging Face 依赖时，优先在 `CPU资源空间` 或其他可上网 CPU notebook 中准备，再把共享盘内容或保存的镜像带到目标 GPU 训练空间。
- README / CONTRIBUTING 同步收敛为统一使用原则，并把开发接口文档降级为维护参考。

版本与发布：
- `inspire-skill` 版本从 `5.0.0` bump 到 `5.1.0`。
- 同步更新 `pyproject.toml`、`inspire/__init__.py` 和 `uv.lock`。

验证：
- `uv run inspire --version` -> `inspire, version 5.1.0`
- `uv run pytest -q` -> `882 passed`
- `uv run ruff check inspire tests` -> passed
- `uv run mypy` -> passed
- CLI help 自省检查 `138` 个 help 页面，无不应出现在用户 help 中的内部实现术语。
- 日常手册术语扫描通过，无 Agent-facing / 面向用户 / Browser API / OpenAPI 等分裂主体或内部接口表述。
- `git diff --check` -> passed
- `uv build` -> built `dist/inspire_skill-5.1.0.tar.gz` and `dist/inspire_skill-5.1.0-py3-none-any.whl`

# v5.0.0

## 更新内容

这是一次 major CLI 发版。`7c331aa Enforce name-only CLI handles` 这个 squash commit 标题只覆盖了其中一部分；v5.0.0 实际包含最近几个小时多个 Agent 合入 `main` 的 CLI 行为、配置、Browser API、文档和测试改动。

### 破坏性变更

- 删除 `inspire run`。GPU 训练入口统一为 `inspire job create`，日志跟随使用 `inspire job logs --follow <name>`。
- 删除旧的顶层 `batch` 命令。批量提交迁移到具体 workload 命令组下。
- 删除根级 `inspire --profile` 和 `INSPIRE_PROFILE_<NAME>_*` env profile。workload 条件 profile 改为按命令组显式管理。
- 删除 config 和 batch defaults 里的隐式 `workspace` / `project` / `group` / `image` / `quota` 默认值。这些调度条件必须来自显式参数、命名 workload profile 或 batch item。
- 收紧为 Name-only CLI 边界。普通命令输入输出使用资源名称、alias、人类可读状态和短表格，不用平台 handle 指代对象；默认 `--json` 也会移除 handle-like 字段。
- Notebook / Job / HPC 的显式平台 handle 查询只保留在专门的 `id` 命令中。
- 删除跨用户命令面。Job、Ray、HPC、model、serving 的 list / resolve 路径默认只查当前 live 用户；无法解析当前用户时 fail closed。
- 删除 serving 的 workspace-wide `--all` 模式和 model 的 `--mine`。model 命令现在默认就是当前用户范围。
- 删除 Ray `create --json-body` 和旧 GitHub job-log retrieval workflow / config（`retrieve_job_log.yml`、`github.log_workflow`）。

### CLI 行为

- 新增 `notebook`、`job`、`hpc`、`ray`、`serving` 下的 workload profile 管理命令。
- 新增 job、HPC、notebook、Ray、serving 的分组 batch 创建能力。
- 新增或对齐 `job instances`、`hpc instances`、`ray instances`，统一使用显式 `--workspace` 和 `--num`。
- 新增本地 `inspire job shell <name>`，通过平台 remote command WebSocket 打开交互 shell。
- 改进 `job logs` 的 live / Web fallback 和日志 follow 行为。
- 重做 Notebook、Job、Ray、HPC、Serving、Image、Model、Project、User、Config、Init、Metrics、Events、tunnel、remote exec / shell / scp 的 CLI 可见输出，避免普通观察面泄露平台 handle。
- 重写大量校验和歧义处理路径。重名时用名称加可读上下文处理，不再让用户复制内部 handle。

### Browser API 与平台覆盖

- 扩展并验证 model registry、model serving、user SSH key、project、notebook、job、HPC、Ray、resources、logs、metrics、workspace selection 的 Browser API 覆盖。
- 新增 user SSH key 管理 helper 和 CLI 覆盖。
- 新增 model registry 的 list / status / versions / register 工作流，以及 serving create / status / logs / events / metrics 的当前 Web UI 合同覆盖。
- 新增 Ray create / list / status / events / instances 支持，并固定当前用户过滤。
- 新增 HPC live instances 支持，并明确 HPC events 仍是 job-level 事件观察。
- `project list`、workload observation 和 events 诊断从本地 cache 事实迁移到 live Browser API 查询。
- resources availability、nodes、specs 继续对齐 live workspace 数据，并保留中文宽度 aware 表格输出。

### 配置与 init

- 重构 config loading，围绕显式 project / workload profile 和 path alias 收口。
- 简化 `inspire init` 的账号、项目、路径发现流程，移除过时 env-detection 和隐藏默认值。
- 保留 path alias 作为唯一类似默认远端路径的机制。
- 删除旧 GitHub log workflow 配置，同时保留 bridge execution 支持。

### 文档与 Agent 指南

- 删除 Agent-facing 材料里的陈旧命令表格；命令面以 CLI help 为准。
- 更新 Notebook、Job / HPC / Ray / Serving、resources、model、image、workflow、setup 和开发者 reference，使其匹配当前 CLI 行为。
- 明确普通使用中，无论是人类还是 Agent，都必须用 Name，而不是平台 handle。

### 验证

- `uv run inspire --version` -> `inspire, version 5.0.0`
- `uv run ruff check inspire tests`
- `uv run mypy`
- `uv run pytest -q` -> `889 passed`
- `uv build`

# v4.1.4

## 更新内容

已发布到 PyPI：<https://pypi.org/project/inspire-skill/4.1.4/>

### 变更

- 收紧 Browser API 开发文档，只保留当前仓库已闭合的 wrapper / helper / CLI 合同。

### 发布验证

- PyPI trusted-publisher workflow：`25612657857` 通过。
- main CI：`25612657122` 通过。
- 本地验证：`uv run pytest -q`（`833 passed`）、`uv run ruff check inspire tests`、`uv run mypy inspire`、`uv build` 均通过。
- 本机全局验证：`inspire --version` 输出 `4.1.4`；`inspire update --check` 验证 PATH executable 和 Claude / Codex / Gemini skill 目录均为 `v4.1.4`。

# v4.1.3

## 更新内容

已发布到 PyPI：<https://pypi.org/project/inspire-skill/4.1.3/>

### 修复

- `uv tool` 更新路径增加 package index refresh，避免 PyPI 已发布新版本但本地 `uv` 缓存仍返回旧版本。
- 安装脚本的 `uv tool install` 同样强制刷新索引，保证重装路径和 `inspire update` 行为一致。

### 发布验证

- PyPI trusted-publisher workflow：`25612579527` 通过。
- main CI：`25612578812` 通过。

# v4.1.2

## 更新内容

已发布到 PyPI：<https://pypi.org/project/inspire-skill/4.1.2/>

### 修复

- 强化 `inspire update` 的全局更新路径：从本地 checkout 或 repo venv 运行时，也会更新 `uv tool` / `pipx` 管理的全局 `inspire`。
- `uv tool` 安装源如果残留为本地 `file://` 路径，`inspire update` 会重置为官方 PyPI 包，避免开发机路径污染全局安装。
- `inspire update` 完成后会验证全局 executable、agent skill 目录和旧 `INSPIRE_TARGET_DIR` / 长环境前缀残留，避免 CLI 最新但 Agent 仍读取旧文档。
- CLI 最新版本检查改为以 PyPI 发布版本为主，GitHub `main` 只作为网络或包索引失败时的 fallback。

### 发布验证

- PyPI trusted-publisher workflow：`25612503179` 通过。
- main CI：`25612499216` 通过。

# v4.1.1

## 更新内容

已发布到 PyPI：<https://pypi.org/project/inspire-skill/4.1.1/>

### 新增与文档

- 精简并重组 Agent-facing `SKILL.md` / `references/`，命令表面统一以 CLI help 为准。
- 补齐 image / model / metrics / path alias / quota / notebook workflow 文档。
- 新增 notebook HTTPS proxy 与容器端口说明，覆盖 `notebook connections`、`/proxy/<port>/`、OpenAI-compatible 服务 base URL 和 API key 验证边界。

### 变更与修复

- human 输出继续收敛到名称、alias、人类可读状态和短表格，避免 raw platform handle 暴露。
- 修复 `inspire update` 在 PyPI 网络失败时的镜像重试路径。
- 同步 `cli/uv.lock` 到 `4.1.1`，恢复 GitHub CI 的 `uv sync --dev --locked`。
- PyPI publish workflow 支持 idempotent retry，tag 重试时会跳过已存在的同版本文件。

### 发布验证

- PyPI trusted-publisher workflow：`25605644934` 通过。
- main CI：`25605638720` 通过，Python `3.10` / `3.11` / `3.12` 全部完成 Ruff、mypy、pytest 和 build。
- 本地 smoke：`uvx --python 3.12 --from inspire-skill==4.1.1 inspire --version` 输出 `inspire, version 4.1.1`。
- 本地验证：`uv run ruff check inspire tests`、`uv run mypy`、`uv run pytest -q`（`816 passed`）、`uv build` 均通过。

# v4.1.0

## 更新内容

已发布到 PyPI：<https://pypi.org/project/inspire-skill/4.1.0/>

### 新增

- 新增 `inspire job shell`，支持进入 running training job 实例，并提供 `--rank`、`--instance` 和 `--pick` 选择器。
- 新增 pre-commit 配置、GitHub Issue 模板、PR 模板、`CONTRIBUTING.md`、`CHANGELOG.md` 和 CI workflow。

### 变更

- `inspire init` 默认进入 discover 流程；首次没有账号时会在 init 内联创建第一个账号并继续初始化。
- `scripts/install.sh` 安装 CLI 后会尽量自动安装 Playwright Chromium，减少首次 SSO 登录中断。
- `inspire job create` 现在通过 `tee` 同时保留平台网页日志和共享盘日志文件，并默认启用 Python 无缓冲输出。
- CI 已升级为 full Ruff、blocking mypy、全量 pytest 和 package build。

### 修复

- `inspire notebook exec` 在没有远端目标目录配置时，会回落到远端默认登录目录执行。
- `inspire notebook ssh --command` 现在会转发本机 stdin，支持 pipe 输入。
- `notebook scp`、`notebook connections` 和 `ssh --command` 现在一致使用 active account 的 tunnel cache。
- 修复并关闭 Issues：#10、#11、#12、#13、#14、#15、#18。

### 验证

- `uv run pytest -q`：`809 passed`
- `uv run ruff check inspire tests`：通过
- `uv run mypy`：通过
- `uv build`：通过

# v4.0.0

## 更新内容

### 三路 audit 一次性闭合 v3.x 边界承诺没贯彻到位的所有缺口

> **破坏性版本** —— v4.0.0 把三路并行 audit（首装 / 升级路径、配置 / 账号 / workspace 深度、用户面错误信息 + name-only 边界）找到的全部 8 条 HIGH 风险 + 周边 MED / LOW 收掉。每条修复都贴近用户实际看到的崩点或误导文案，不引入兼容补丁。

#### 用户视角的破坏性变化

升级到 v4.0.0 后，以下三类原本被静默接受的写法会立刻报错：

- **项目 `./.inspire/config.toml` 不再吃账号级字段**。在 repo 的 toml 里写 `[auth]` / `[api]` / `[proxy]` / `[workspaces]` / `[projects]` 等账号级 key，加载时直接 ConfigError 拒绝，提示移到 active account 的 `~/.inspire/accounts/<n>/config.toml`。这条约束 v3.0.0 就立了，但 loader 一直没强制执行 —— `scope` 元数据只用来 init 模板生成。结果是同一份代码 cd 到不同 repo 时一个 repo 的 `[auth]` 会污染另一个 repo 的命令行为。**v4.0.0 真的拒绝了。**
- **`inspire notebook list` / `notebook ssh` 等命令不再偷偷回退到 SSO 会话当前所在的 workspace**。v3.1.0 砍了「默认 workspace」概念，但 `notebook_create_flow.py` 与 `notebook_lookup.py` 在 `select_workspace_id()` 没解析出值时仍然会 fall back 到 `session.workspace_id`，相当于把砍掉的概念从 SSO 会话态偷偷捡回来。v4.0.0 删掉这条 fallback —— 入口的 workspace 现在完全靠 `[workspaces]` alias map（`inspire init --discover` 写出的那张表）。
- **`notebook shell` / `exec` / `scp` / `refresh` / `forget` 与 `job logs --notebook` 不再接收 raw notebook id**。v2.0.0 立的 name-only-at-user-boundary 边界，原本只在前端的 status / start / stop / delete 等命令上 enforce；这六个 cached-tunnel 命令一直直接拿 raw 字符串去 `tunnel_config.bridges` 查表，传 raw id 会静默 miss，给一个让人困惑的「No cached connection」错误。v4.0.0 在它们入口加了统一的 `reject_id_at_boundary` 拒绝层，传 raw id 直接退码 12。

#### 体验变好的部分（不破坏，单纯改进）

- `inspire update` 升级完之后会主动跑一次 `normalize_environment()` —— 从 v3.1.x 升上来的用户，不再需要等到下次 `account add` / `notebook ssh` 才触发 pre-v3 unscoped 文件的 quarantine + `INSPIRE_WORKSPACE_ID` 老 env 提示。一次 `inspire update` 就把环境清理完。
- 所有错误 hint 里散布的「Set `[auth].username` and configure password via INSPIRE_PASSWORD or `[accounts."<username>"].password`」（约 14 处）统一替换为单一的 `WEB_AUTH_HINT` 常量，指向 `inspire account add <name>` 这一条命令。
- `--help` 例子和命令成功时打印的 `Use 'inspire notebook status {notebook_id}'` 类提示，里面所有 raw platform id（`notebook-abc-123` / `nb-abc123de` / `78822a57-3830-...` / `image-xxxx` 等）一律改用 name 形式。原来的版本会让用户 copy-paste 后被自家 resolver 用退码 12 拒绝 —— 自相矛盾。
- Tunnel cache key 严格 name-only。原本「notebook 没有 display name 时 fallback 到 `nb-<id[:8]>`」这条路径会从源头破坏 name-only-at-user-boundary 边界（生成出来的 cache key 后来又被 P1-3 boundary check 拒绝）。v4.0.0 直接拒绝创建无 name 的 cache 入口，附上「请给 notebook 起个名字」的清晰指引。
- `~/.inspire/current` 损坏 / 指向已删除账号 / 内含非法名字时，`current_account()` 会 sanitize 成「无活动账号」，让所有调用方走同一条 fail-fast 路径，不再读路径返回 None / 写路径直接信任的语义分裂。
- `normalize_environment` 用 `O_CREAT | O_EXCL` 原子声明 sentinel，两个并发 `inspire account add` 不会再 race 在 rename pass 上。
- `normalize_environment` 同时清理 v2.x 的 `~/.inspire/accounts/<n>/sessions/` 老目录，重命名为 `sessions.legacy`。
- `INSPIRE_PROJECT_ID` env 与 repo `[context].project` 冲突时不再静默 shadow，而是 fail-fast 提示用户决定取舍。
- `[context].project` 别名在 active account 下解析不到时不再把原始文本当 `project_id` 透传到平台 API（v3 的旧行为会让你切账号后命令静默 404）；v4 直接返回 None 让加载层报错。
- 同名歧义错误现在描述为「name 冲突」而不是「Partial ID 冲突」，与 name-only 契约一致。
- `cli.main` 顶层 catch 改走 `human_formatter`，再也不会从某个忘了包异常的路径冒出 `Error: <python repr>` 这种半生不熟的输出。

#### 文件级修复清单

- `inspire/accounts/normalize.py` —— 原子 sentinel + v2.x sessions/ quarantine
- `inspire/accounts/storage.py` —— `current_account()` sanitization
- `inspire/cli/commands/update.py` —— 升级后跑一次 normalize
- `inspire/cli/commands/notebook/notebook_lookup.py` —— 删 session.workspace_id fallback
- `inspire/cli/commands/notebook/notebook_create_flow.py` —— 同上 + 改 hint 文案
- `inspire/cli/commands/notebook/notebook_ssh_flow.py` —— 删 `nb-<id[:8]>` cache key fallback
- `inspire/cli/commands/notebook/{remote_shell,remote_exec,remote_scp,refresh_cmd,forget_cmd}.py` —— 加 `reject_id_at_boundary` 入口
- `inspire/cli/commands/job/job_logs.py` —— 同上 + 修 fallback wording
- `inspire/cli/commands/notebook/notebook_commands.py` —— 删 raw id 的 `--help` 示例与成功信息中的 raw id
- `inspire/cli/commands/image/image_commands.py` —— 同上
- `inspire/cli/utils/id_resolver.py` —— 加 `reject_id_at_boundary` helper + 歧义文案改写
- `inspire/cli/utils/notebook_cli.py` —— 加 `WEB_AUTH_HINT` 常量
- `inspire/cli/main.py` —— 顶层错误格式收敛
- `inspire/config/load_layers.py` —— 项目层 scope 强制
- `inspire/config/load_runtime.py` —— `INSPIRE_PROJECT_ID` 冲突检测 + credential 错误文案统一
- `inspire/config/load_common.py` —— alias 解析失败 fail-fast
- `inspire/config/load_env.py` —— credential 错误文案统一
- `inspire/config/options/api.py` —— `auth.username` scope 修正为 global
- `inspire/cli/commands/init/{json_report,templates}.py`、`inspire/cli/commands/config/check.py` —— 旧文案统一替换

#### 升级路径

```bash
inspire update
```

升级完会立刻看到一行 stderr，把 v3.1.x 残留的 `INSPIRE_WORKSPACE_ID` 老 env / pre-v3 unscoped 文件等告诉你怎么处理（如果有的话）。

#### 测试与验证

- 单元测试：793 passed（v3.2.0 baseline 是 792；新增 1 个 project_keys_accepted 场景，3 个测试改名/改断言对齐 v4 语义）。
- 真实路径模拟（`uv tool install --force` + mktemp HOME），覆盖：
  - 项目 toml 含 `[auth]` → ConfigError 拒绝
  - 给 cached-tunnel cmd 传 raw notebook id → 退码 12
  - 损坏的 `~/.inspire/current` → `inspire init` 友好 fail-fast
  - notebook ssh fast-path（v3.2.0 修的 #4 回归测试）依然干净
  - 无活动账号下的 `inspire init` 给 `inspire account add` 引导

#### 致谢

感谢三路并行 audit 的 Codex agent 群（first-run / 升级路径、config / account 深度、错误信息 + name-only 边界），把散在十几个文件里的 8 条 HIGH 风险一次性扫到。

# v3.2.0

## 更新内容

### 账号 UX 大改：`inspire account add` 一次性把整个主机环境配齐

修复 Issue #4 与 #5，顺带把 v3.1.0 留下的几处不彻底清理一起收掉。

不再有「我加完账号、下一条命令就崩」这条路径。v3.2.0 把环境正常化的责任收拢到 `inspire account add` 自己 —— 从 Inspire-cli 或更早 InspireSkill 版本升上来的用户，不会在后续任何命令里再看到账号相关的报错。

#### `inspire account add` 现在多做三件事

- **隔离 v3 之前的 unscoped 残留**：`~/.inspire/{bridges,web_session,jobs,config}.json` 与 `~/.cache/inspire-skill/rtunnel-proxy-state.json` 共五个文件，全部重命名为 `*.legacy`（不静默删除，不自动迁移到 v3.x 路径）。一个 sentinel `~/.inspire/.environment-normalized-v3` 保证整台机器只跑一次。
- **检测过期 `INSPIRE_WORKSPACE_ID` 环境变量**：v3.1.0 已经废掉这个 env，v3.2.0 顺带把仍然偷偷读它的四处运行时分支（`session/auth.py`、`openapi/client.py`、`cli/utils/profile.py`）一并删掉。
- **检查 Playwright chromium**：交互式 `account add` 若发现没装，会主动跑 `playwright install chromium`，避免首次 SSO 登录时撞错。

`inspire notebook ssh` 也调用同一个正常化函数（sentinel 保证幂等，零成本），所以已经在 v3.1.0 上用了一段时间、不会立刻重新跑 `account add` 的用户，第一次跑 ssh 时也会自动触发清理。

#### 修复

- **#4** `inspire notebook ssh <name>` 不再因 `TypeError: bridge_ssh() got an unexpected keyword argument 'bridge'` 崩溃，fast-path kwarg 名修正完成。任何 bridges cache 非空的用户都会立即受益。
- **#5** 没有活动账号时跑 `inspire init` 不再抛 `AttributeError: 'NoneType' object has no attribute 'exists'`，统一给出清晰指引：`Run `inspire account add` first.`。`init --discover`、`init --template --project` 等所有 mode 一致 fail-fast。
- Playwright chromium 缺失时，从原先的 `Unhandled exception in inspire CLI` 转入标准 CLI 错误路径，附上一行直接可执行的安装命令。

#### 兼容策略说明

为了维护代码简洁、不在主线散布历史兼容分支，v3.2.0 选择了「主动一次性清理 + 不写兼容补丁」的折衷做法：

- **不接受老 schema**：从 Inspire-cli 0.2.4 写出的 `~/.inspire/bridges.json` 等 v3 之前的文件不会被解析复用，而是被原样重命名为 `*.legacy` 后保留。
- **不写 fallback 分支**：`inspire init` 没有活动账号时直接 fail-fast 引导用户跑 `inspire account add`，不再悄悄回退到老的全局路径。
- **不偷偷读老 env**：`INSPIRE_WORKSPACE_ID` 在 v3.1.0 已经声明被砍，v3.2.0 把残存的四处运行时读取一并删除。

#### 升级路径

```bash
inspire update
```

第一次跑 `inspire account add` 或 `inspire notebook ssh` 时会自动隔离老残留并在 stderr 上告诉你重命名了哪些文件，例如：

```
Quarantined legacy file: /Users/<you>/.inspire/bridges.json → bridges.json.legacy
```

**任何文件都不会被删除**，`*.legacy` 后缀的副本留在原位、随时可以查阅或自行清理。

#### 测试与验证

- 单元测试：792 passed（在 v3.1.0 的 787 之上新增 9 个 normalize 测试，删除 4 个迁移走的 bridge-config 测试，净 +5）。
- 真实路径模拟：通过 `uv tool install --force` 把 working tree 装到隔离的临时 HOME 下，逐一复现 #4 报告者的「Inspire-cli 0.2.4 残留 + 多账号」场景、#5 的「无活动账号首次 init」场景，以及 Playwright chromium 缺失场景，全部通过。

#### 致谢

感谢 @JingYiJun 提供的精确栈跟踪 + 根因定位，两条 issue 含金量都极高，为定位 + 设计 v3.2.0 的环境正常化方向节省了大量时间。

# v3.1.0

## 更新内容

> **⚠️ 破坏性 —— 之前依赖 `[job].workspace_id` / `INSPIRE_WORKSPACE_ID` / `[context].workspace` 静默兜底默认 workspace 的用户，现在每条命令都必须显式 `--workspace <alias>`。** 这几个字段在加载时会被忽略，并打一行 stderr 警告。`[workspaces]` 下既有的 alias 仍可继续用，只是不能再当默认值，得在命令行里显式拼出来。

一位用户反馈他同学的 CLI 默认把 workspace 选成了 **CPU 临时测试空间** —— 那个同学根本没主动选过这个。顺着拉了一下，所谓的「默认 workspace」是由三块拼出来的：

1. **`inspire init --discover`** 里有一段 fuzzy-match 兜底逻辑：当它没法把名字 "cpu" / "gpu" / "internet" 匹配到任何已发现的 workspace 时，会回退去把 `[workspaces].cpu` 写成「用户当前 web session 凑巧落在的那个 workspace」。这个 session 凑巧选中的 workspace 就此**永久**变成「cpu」alias。**这正是随机抽签的来源。**
2. **`select_workspace_id()`** 在命令没显式 `--workspace` 时回退到 `config.job_workspace_id`，命中刚才那个被随机抽中的值。
3. **Schema 层**：`[job].workspace_id` / `[context].workspace` / `INSPIRE_WORKSPACE_ID` 在 template 和 `inspire config show` 里都被宣传为「默认 workspace」，鼓励了同样的坏习惯。

那位用户的论点是对的：每个 SII 研究者都会**根据任务在多个 workspace 之间切换**（CPU 资源空间做预处理、分布式训练空间跑训练、弹性走 Ray 等等）。静默把其中一个钉成默认是危险的。真正有用的「按仓库的默认值」是 `[paths].target_dir` 和 `[context].project` —— 这两个不会随每条命令变。

### 改动概览

* **Schema**：删除 `[job].workspace_id` ConfigOption。`INSPIRE_WORKSPACE_ID` 不再被 CLI 识别为默认值。`Config.job_workspace_id` 属性下线。`[context].workspace` 从 loader 识别的键里去掉。
* **解析器**：`select_workspace_id()` 不再读任何 config 兜底 —— 既没传 `explicit_workspace_id` 也没传 `explicit_workspace_name` 就返回 `None`，迫使调用方报错出来。
* **错误信息**：新增 `workspace_required_hint(config)` helper，给 `run` / `job create` / `hpc create` 拼出一行 `pass --workspace <alias> (configured aliases: cpu, gpu, ...)`，让用户看到自己 config 里到底有哪些 alias。
* **Discover**：`inspire init --discover` 在没法解析 workspace 名字时不再回头写 `cpu` / `gpu` / `internet` 这种 legacy alias。（之前那条 fallback 路径就是 random-roll 那个 bug 的源头。）
* **Template**：`inspire init --template` 模板里删掉了 `[job]` 下面的 `# workspace_id = ...` 行，并改写 `[workspaces]` 注释，明确这些 alias 不是默认值。
* **`inspire config context`**：去掉 `active.workspace` 字段。`[workspaces]` alias map 还在打印，只是不再带「这个是默认」的语义。
* **Loader 迁移提示**：第一次见到 `[job].workspace_id` / `[context].workspace` / `INSPIRE_WORKSPACE_ID` 时打一行 stderr 警告，指出哪个文件 / env 上的字段命中了，并指向 `--workspace <alias>`。

### 已有用户须知

如果你的脚本本来就在每条命令上传 `--workspace cpu`（或别的 alias），**没有任何变化**。

如果你之前依赖隐式默认值：

```bash
# 之前（v3.0.x 能跑，v3.1.0 不行）：
inspire job create -n test -q 1,20,200 -c "echo hi"

# 之后（永远能跑）：
inspire job create -n test -q 1,20,200 -c "echo hi" --workspace cpu
```

升级到 v3.1.0 之后第一次跑命令时，会在 stderr 上打出你的 TOML / env 里命中的具体 legacy 字段，告诉你需要清掉哪些。这些字段是**静默忽略**的 —— 你的 config 仍能加载，只是必须在命令行里加上 `--workspace <alias>`。

### 从 v3.0.x 迁移

```bash
# uv tool / pipx 用户：
uv tool upgrade inspire-skill
pipx upgrade inspire-skill

# 或者重跑安装脚本（--force 直接覆盖）：
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

完整 diff：[v3.0.3...v3.1.0](https://github.com/realZillionX/InspireSkill/compare/v3.0.3...v3.1.0)
PyPI：https://pypi.org/project/inspire-skill/3.1.0/

# v3.0.3

## 更新内容

> **⚠️ 已经在 v1.x / v2.x / v3.0.0–v3.0.2 上的用户**：`inspire update` 自身在更早版本里有 bug，**没法靠它自动升上来**。两条路任选其一。
>
> 重跑安装脚本（脚本内已加 `--force`，原地强制覆盖，不需要先卸载）：
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
> ```
>
> 或直接调底层包管理器：
>
> ```bash
> uv tool upgrade inspire-skill        # uv tool 用户
> pipx upgrade inspire-skill           # pipx 用户
> pip install -U inspire-skill         # 自管 venv / pip install -e 用户
> ```
>
> 这次一次性迁移完成后，所有未来 patch 都能正常 `inspire update` 自动升级。

---

一位用户报告 `inspire update` 在标准 `uv tool install` 装出来的环境上居然拒绝自动升级。顺着这个线索拉出来一串新装用户都可能踩的坑，这次发版一并收掉。

### 改动

#### `install.sh` —— 首装链路加固

1. **`--ref <tag|SHA>` 直接 404**。脚本之前硬写 `tar.gz/refs/heads/${REF}`，那条路径只对 branch 有效。`--ref v3.0.0`（一个 tag）会被 codeload 返回 404，安装中断。改成短形式 `tar.gz/${REF}`，codeload 对 branch / tag / commit SHA 都能解析。README 里 `--ref` 的例子也补齐三种形态。

2. **首装完 PATH 没配好**。`uv tool install` 不会改用户的 shell rc；pipx 会打提示但不会自动改。所以新机器上跑 `curl | bash`，看着一片绿，到了 `inspire ...` 就 `command not found`。脚本现在在 `inspire` 不在 PATH 时自动跑 `uv tool update-shell` / `pipx ensurepath`，并提示用户 `exec $SHELL`。

3. **launchd plist 里硬编码 proxy `127.0.0.1:7897`（Clash Verge 默认值）**。维护者本地的 setup 被烤进了每个用户的 Mac —— 没用同一套 proxy 的用户每天都有一个静默失败的 launchd 后台 job。现在改成在安装时读调用 shell 的 `$http_proxy` / `$HTTPS_PROXY`，有就嵌进去，没有就留空。

4. **重跑 `install.sh` 在同时装了 uv 和 pipx 的机器上会留一个孤儿 pipx 安装**，跟 uv tool 互抢 `~/.local/bin/inspire`。现在 uv 路径胜出后，如果之前用 pipx 装过同一个包，pipx 那份会被自动卸掉。

5. **更好的安装结束 summary**：无论 PATH 是否生效，都把刚装上的版本号打出来，让用户立即看到一个具体的成功信号。

#### `install-dev.sh`

同样的 proxy fix —— 不再硬编码 7897。

#### `inspire update`

6. **tarball 解压安全/前向兼容**：在 Python 3.11.4+ 上钉死 `filter='data'` 防止路径穿越解压（codeload 是 GitHub 受信源，风险低，但 explicit > implicit），更老的 Python 上静默回退。

7. **顶层目录探测**：扫所有成员断言只有恰好一个顶层段，不再信赖 `members[0]`。tarball layout 哪天变了能立刻看到清晰错误，而不是默默解压到错路径。

8. **更好的「无法自动升级」错误提示**：当装出来的不是 `uv tool` / `pipx`（而是自管 venv、`pip install -e .` 等），错误信息现在会同时打出 `python=` 和 `prefix=`，再加三条具体的修复路径。

#### README

Install + Update 段重写：
- 前置依赖 / 一行安装 / `--ref` 例子（现在带 tag、branch、SHA 三种形式）/ 常见问题块全部分块清楚。
- Update 段显式说明 `inspire update` 每个 flag 的语义和适用场景。
- 新增「**从 v3.0.3 之前的版本升级**」子段，写清楚上面那两条一次性迁移路径。
- 「🔧 维护承诺」一节砍掉了重复的命令清单。

### 完整 diff

[v3.0.2...v3.0.3](https://github.com/realZillionX/InspireSkill/compare/v3.0.2...v3.0.3)
PyPI：https://pypi.org/project/inspire-skill/3.0.3/

# v3.0.2

## 更新内容

补丁发布 —— 修复 `inspire update` 安装器探测的一个真实 bug。

### 改动

一位 v3.0.1 用户报告 `inspire update` 输出：

```
✗ Can't auto-upgrade: this build isn't managed by uv tool / pipx
  (python=/Users/.../.local/share/uv/tools/inspire-skill/bin/python).
  Reinstall via scripts/install.sh ...
```

—— 即使 python 路径里明明就写着 `/uv/tools/inspire-skill/`。

#### 根因

探测器之前用的是 `Path(sys.executable).resolve()`。`.resolve()` 会顺着 venv 的 `bin/python` symlink 一路跟到底层解释器二进制。对 uv tool 装出来的环境，最终落在 `~/.local/share/uv/python/cpython-3.x.x-.../bin/python3` —— 这条路径里有 `uv` 但**没有** `tools`。后续 `"uv" in parts and "tools" in parts` 判定失败，函数返回 `None`，于是健康的 uv tool 装出来的环境被错判为「不是 uv tool 装的」，自动升级被拒。

pipx 也一样有这个坑：pipx 的 venv python 经常 resolve 到 system Python，跑出 pipx 目录树外。

#### 修复

直接探 `sys.prefix`（venv 根目录）。`sys.prefix` 不受 symlink resolve 影响 —— uv tool 装出来必然是 `~/.local/share/uv/tools/<pkg>`，pipx 装出来必然是 `~/.local/share/pipx/venvs/<pkg>`。

加了 `tests/test_update_installer_detection.py`，用 11 个参数化用例覆盖两种布局、dev install（必须返回 `None` 让 edit-install 分支提示生效）、system Python，以及部分匹配的边界情况。

### 用户侧

如果你的 `inspire update` 之前就能用，这次发版你不用动。如果你卡在 v3.0.1 因为自动升级拒绝运行，这次发版会让你能原地升上来。v3.0.1 用户走一次手动升级就能升到 v3.0.2：

```bash
uv tool upgrade inspire-skill        # uv tool 用户
pipx upgrade inspire-skill           # pipx 用户
pip install -U inspire-skill         # 普通 pip
```

之后从 v3.0.2 升到任何后续 patch 直接 `inspire update` 就行。

完整 diff：[v3.0.1...v3.0.2](https://github.com/realZillionX/InspireSkill/compare/v3.0.1...v3.0.2)
PyPI：https://pypi.org/project/inspire-skill/3.0.2/

# v3.0.1

## 更新内容

补丁发布 —— 修复 Windows 用户的一个真实 UTF-8 bug，外加 README 调整。

### 改动

#### 修复 —— 全部走 UTF-8（#2）

`Path.write_text(...)` / `open(..., "w")` 不显式带 `encoding=` 时会回退到主机 locale，中文 Windows 上是 `cp936` / GBK。在该 locale 下写出含 CJK 内容（workspace 名、路径）的 TOML，得到的就是 GBK 编码文件，下次 `tomllib.load`（按规范严格 UTF-8）会拒绝解析，CLI 直接起不来。

修复：CLI 内每一处文本模式的 `write_text` / `read_text` / `open` 都显式钉死 `encoding="utf-8"`（共扫了六处）。补一个回归测试 [`tests/test_init_encoding.py`](https://github.com/realZillionX/InspireSkill/blob/v3.0.1/cli/tests/test_init_encoding.py)，用生产路径写 CJK 内容然后用严格 UTF-8 读回校验。

由 @Amadeus0079 在 [#2](https://github.com/realZillionX/InspireSkill/issues/2) 报告。

#### 平台支持

README 快速开始段现在显式写出支持矩阵：macOS + Linux 一等公民，Windows 用户请走 [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install)。CLI 依赖 POSIX 的 SSH / rsync / GPFS 约定，Windows 原生（PowerShell）不在 roadmap 上。

#### README —— 与其它启智 CLI 的对比

新增一节，把 InspireSkill 跟 [EmbodiedForge/Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) 与 [tianyilt/qzcli_tool](https://github.com/tianyilt/qzcli_tool) 横向比较，结构与已有的「vs InspireCode」一节对齐：一段引子 + 一张表 + 一句话总结。

### 升级方式

```bash
inspire update                        # 原地升级
pip install -U inspire-skill          # PyPI 默认
uv tool install --upgrade inspire-skill
pipx upgrade inspire-skill
```

如果之前在 Windows 上 config.toml 来回读写失败，这个补丁会修好。

完整 diff：[v3.0.0...v3.0.1](https://github.com/realZillionX/InspireSkill/compare/v3.0.0...v3.0.1)
PyPI：https://pypi.org/project/inspire-skill/3.0.1/

# v3.0.0

## 更新内容

**主版本号跳号** —— v3.0.0 把自 v2.0.1 以来累计的所有破坏性重构整合成一份连贯的发布，并端到端对齐了平台 web 表单（「新建交互式建模」/「新建训练任务」/「新建弹性任务」）与 CLI 表面。

### 改动概览（相对于 v2.0.1）

#### 配额模型 —— 全部统一为 `--quota gpu,cpu,mem`

自由格式的 `--resource "1xH200"` 字符串已废除。所有五个 create 命令现在都接受显式三元组：

```bash
inspire notebook create -q 1,20,200 ...
inspire job create      -q 8,160,1800 --nodes 2 ...
inspire run "<cmd>"     -q 1,20,200 ...
inspire hpc create      -q 0,32,256 --instance-count 2 ...
inspire ray create      --head-quota 0,2,8 --worker 'name=w1;quota=0,4,16;min=1;max=8;...'
```

CLI 自动按 workspace 的 quota 表解析；多组撞同一三元组时用 `--group <name>` 消歧。

#### `inspire resources specs` —— 默认跨 workspace 搜

```bash
inspire resources specs                       # 所有 workspace × 所有 family（notebook + job + hpc + ray）
inspire resources specs --usage notebook      # 按 family 窄化
inspire resources specs --workspace CPU资源空间 # 按 workspace 窄化
```

`--usage job` 是 v3 新增（查 `SCHEDULE_CONFIG_TYPE_TRAIN`；之前只能退到 `--usage all` 碰运气）。输出表里所有 ID 列（`spec_id` / `quota_id`）一并删除 —— Agent 看到的只有 workspace + group + GPU 型号 + `(gpu, cpu, mem)` 三元组。

#### HPC 两层模型

`inspire hpc create` 把「节点规格」与「Slurm 切分」清晰分开：

- **节点级别**（`--quota gpu,cpu,mem` + `--instance-count`） —— 选 web UI 上的「计算资源规格」+「节点数」。
- **Slurm 级别**（`--number-of-tasks` + `--cpus-per-task` + `--memory-per-cpu`） —— 节点内独立切分。默认 `cpus-per-task = quota.cpu`、`memory-per-cpu = quota.mem // quota.cpu`、`number-of-tasks = 1`。

#### 三档优先级语义全面对齐

四个 create 命令现在都用同一套档位语义，由 `IntRange(1, 10)` 校验：

| 取值 | 档位 | 行为 |
| --- | --- | --- |
| 1-3 | LOW | 会被 HIGH 抢占；需要频繁 checkpoint |
| 4 | NORMAL | |
| 5-10 | HIGH | 稳定运行，可抢占 LOW |

`notebook create` / `job create` / `hpc create` / `ray create` 的 help 文案统一指向这个表。项目配额仍可能进一步限制实际生效优先级。

#### SSH alias 概念彻底删除

之前：`inspire notebook ssh <name> --save-as <alias>` 创建一条 alias 键的 bridge 条目，后续 `notebook shell --alias <alias>` 据此分发。

现在：notebook name 是**唯一**标识。一个 notebook 对应一个缓存连接。代码里再也看不到 `--save-as`、`--alias`、`--bridge`、`notebook set-default`。

v3 具体下游清理：
- `inspire job logs --bridge / -b` → `--notebook`（不要短选项 —— `-n` 给 `--tail` 留着）。
- `inspire notebook top --bridge / -b` → `--notebook / -n`。
- `--keepalive / --no-keepalive` 这条 no-op flag 从 `notebook create / start` 完全移除。
- 错误提示里所有 `--save-as <name>` 字样删除。
- `notebook lifecycle` 文档串行错误纠正（`save-as-image` → `image-save`）。

#### Ray 集群语法重建

```bash
inspire ray create -n my-ray -c 'python driver.py' \
  --head-image <URL> --head-group CPU资源-2 --head-quota 0,2,8 \
  --worker 'name=w1;image=<URL>;group=CPU资源-2;quota=0,4,16;min=1;max=8;shm=32' \
  --worker 'name=w2;...'  # 可重复
```

worker 字段用 `;` 分隔（不用 `,`），避免 `quota=0,4,16` 内部的 `,` 与外层字段分隔符冲突。多个 `--worker` 定义多个 worker 组。

#### SSH bootstrap —— GPFS zero-copy + 全场景可用

`inspire notebook ssh <name>` 现在在**任何镜像**（不需要预装 sshd）和**任何 compute group**（不需要公网）上都能直接跑通。bootstrap 步骤：

- 直接从 `/inspire/hdd/global_public/inspire-skill-bootstrap/v1/rtunnel/linux-<arch>/rtunnel` exec 出 `rtunnel`（不复制到容器内 —— 每次冷启省一份 ~6 MB 拷贝 + chmod）。
- 容器没有 `/usr/sbin/sshd` 时才 `dpkg -i` kit 里的 `sshd-debs/*.deb`，并自动补上 `sshd` user 与最小化的 `/etc/ssh/sshd_config`（offline 路径下 postinst 不会跑）。

#### `inspire notebook install-deps`

新增 helper，往 notebook 里装 hpc + ray 依赖以便 `image save`：

```bash
inspire notebook install-deps <name> --slurm --ray
# 先 probe 再下手，幂等，清华源不通自动 fallback 到 pypi.org。
```

#### 账号隔离

每个账号独占一份完整目录 `~/.inspire/accounts/<name>/`。当前活动账号用 `~/.inspire/current` 一行文件标记。`[accounts."<user>"]` 合并层和环境变量优先级链全部移除。

### 升级方式

```bash
inspire update                                                           # 原地升级
pip install -U inspire-skill                                             # PyPI 默认
uv tool install --upgrade inspire-skill
pipx upgrade inspire-skill

# 国内镜像：
pip install -U inspire-skill -i https://pypi.tuna.tsinghua.edu.cn/simple
```

如果你用脚本调 `inspire`，可能咬人的几处破坏性 flag 改名：
- `--resource "1xH200"` → `--quota 1,20,200`
- `--save-as <alias>` → 删掉这个 flag，直接用 notebook name
- `--bridge`（在 `job logs` / `notebook top` 里）→ `--notebook`
- `--keepalive` → 删掉这个 flag
- `--usage train`（如果你用过）→ `--usage job`

完整 diff：[v2.0.1...v3.0.0](https://github.com/realZillionX/InspireSkill/compare/v2.0.1...v3.0.0)

PyPI：https://pypi.org/project/inspire-skill/3.0.0/

# v2.0.1

## 更新内容

补丁发布 —— 只动了发版管线，CLI 行为没变。

### 改动

- PyPI 项目页现在直接渲染仓库 README 作为 long description（之前显示「The author of this package has not provided a project description」）。
- 后续 tag 走 [GitHub Actions trusted publisher](https://docs.pypi.org/trusted-publishers/)（OIDC）自动发版到 PyPI —— 仓库里不存任何 API token，也不需要再手动跑 `uv publish`。只要 `git tag vX.Y.Z && git push --tags` 就会自动走完。

### 升级方式

无需主动操作，`inspire update` 照常工作。如果是新装：

```bash
pip install inspire-skill                     # PyPI 默认
uv tool install inspire-skill                 # uv 用户
pipx install inspire-skill                    # pipx 用户

# 国内镜像（大陆访问更快）：
pip install inspire-skill -i https://pypi.tuna.tsinghua.edu.cn/simple
```

完整 commit 列表见 [v2.0.0...v2.0.1](https://github.com/realZillionX/InspireSkill/compare/v2.0.0...v2.0.1)。

# v2.0.0

## 更新内容

破坏性版本。`inspire --version` 现在报告 `2.0.0`。

### 破坏性改动

- **用户 / Agent 边界只接收 name，不接收 id**。所有资源命令（`job` / `hpc` / `ray` / `serving` / `image` / `notebook` 及它们的 `metrics` / `events` / `lifecycle` 子命令）拒绝 id 形态输入，退出码 12。请使用 `inspire <resource> list` 里看到的 name；name 真有歧义时用 `--pick <N>` 指定第几条。
- **按账号分目录的存储布局**。每个账号独占 `~/.inspire/accounts/<name>/{config.toml, bridges.json, sessions/…}`；`~/.inspire/current` 单行指向当前活动账号。老的全局路径不再被读取。**不附带迁移工具** —— 重新跑 `inspire account add` / `inspire init --discover` 即可。
- 移除 `--workspace-id` 与 `workspace_cpu` / `workspace_gpu` / `workspace_internet` 这套角色概念；compute group 现在统一以 name 引用。
- HPC 与 train-job 的规格表改为按 `(compute_group, cpu, memory)` 实时解析，不再硬编码 spec id。
- 删除 `inspire notebook ssh-config` 子命令。
- SSH alias 默认值改为 `<name>-sh<N>`。

### 重要新增

- `inspire ray` 命令组（`create` / `stop` / `delete` / `status` / `events` / `instances` / `scaling-histories`），支撑弹性计算任务，从 web 前端 JS bundle 反向工程而来。
- `inspire account` 命令组，含交互式 `account add`。
- `inspire <resource> metrics`（notebook / job / hpc / serving）：GPU / CPU / 内存 / 磁盘 / 网络的时间序列曲线，多 pod 任务按 pod 分别绘制。
- `inspire image save --public/--private` 与新的 `inspire image set-visibility`。
- `inspire init --select-project`。
- `inspire notebook create --group` 显式指定 compute group。

### 正确性 / 安全

- 配置写入全部走原子操作（同目录临时文件 + fsync + `os.replace`），覆盖 `config.toml` / `~/.inspire/current` / `bridges.json` 与 `init` 输出。
- name 解析过程中遇到 session / auth 失败时如实抛错，不再被掩盖成「resource not found」。
- `wait_for_image_ready` 把 CANCELLED / TIMEOUT / ABORTED / INTERRUPTED 都识别为终态失败。
- name 歧义提示不再泄露平台 id。

### 文档

- SKILL.md / README.md 重写为黑盒 Agent 手册：不出现 `<id>` 占位符，不暴露内部机制。
- 默认基底镜像改为 `docker.sii.shaipower.online/inspire-studio/unified-base:v2`。

完整 changelog 见 [v1.0.1...v2.0.0](https://github.com/realZillionX/InspireSkill/compare/v1.0.1...v2.0.0)（77 个 commit）。

# v1.0.1

## 更新内容

### 亮点

#### 新增 CLI 命令
- **`inspire notebook delete` / `inspire job delete` / `inspire hpc delete`** —— 清理废弃资源，默认交互确认，`-y/--yes` 跳过（388a637）
- **`inspire image set-visibility <id> --public/--private`** —— 翻转已存在镜像的公开/私有属性（1e4cbe5）

#### 新增 flag
- **`inspire image save --public/--private`** —— 落盘时直接设置镜像可见性，保存后会追一次 `/image/update` 兜底（1e4cbe5）
- **`inspire notebook create --group NAME`** —— 绕过 CPU 自动选组的启发式，强制把 notebook 钉在指定 compute group（例如需要外网时的 `HPC-可上网区资源-2`）（88ac647）

#### Init 交互改进
- **`inspire init --discover`** 新增存储分层选择器（`ssd` / `hdd` / `qb-ilm` / `qb-ilm2`），并在平台默认为 HDD 时把提示默认翻到 SSD，避免继承已写满的 HDD 路径导致首次写入 ENOSPC（155b791）

### 修复

- **`/mirror/save` 不接受 `visibility` 字段**，CLI 改为 `save` 之后再调 `/image/update` 补写；`/image/update` 的正确字段是 `id` 而非 `image_id`（889b139）
- **`/mirror/save` 可能不回传 `image_id`**，现在回退到 `list_images_by_source("private")` 按 `(name, version)` 最新 `created_at` 解析（cf86804）

### 文档（SKILL.md）

- **§0 平台硬约束**：新增 compute group → hpc 映射（`CPU 资源空间` 下 hpc 只走 `HPC-可上网区资源-2`；500GB 档因 ops 误配只能退到 `CPU资源-2` 上的 notebook）、project-instance mount scoping、`HPC-可上网区资源-2` 500GB 档假排队 bug（389a50f, 9e047c0, 66399bb, bf73502, 13cd86c）
- **§2.1 shell vs exec**：明确 `shell` 是持久会话、`exec` 是一次性 one-shot；同一 alias 可从多个终端并发开 `shell`，但会共享容器资源（838b5e1, 10e7fd6, 646d4f5）
- **§2.3 / §3 阶段 A**：CPU 预装基底从 `slurm-dev:0.0.0` / `base:20250920` 切到 `inspire-studio/unified-base:v1`（已内置 sshd + rtunnel + slurm 客户端，2026-04-22 实测在 hpc 下正常拉起 slurm）（b524bc3, e60d986, bc16595, 6d32e08）
- **§3 路径与分层**：`<user>/` 与 `public/` 都只是根目录，子树由项目自定；四档存储池表（ssd/hdd/qb-ilm/qb-ilm2）与 init tier picker 对齐；`<他>` 改为 `<others>`（bf78269, 9e047c0, cf86804）
- **HPC 假成功验证**：`SUCCEEDED` 不代表 payload 真正运行过，改用「fingerprint 写到共享存储 + 兄弟 notebook `cat` 回读」的确认方式（bc16595）

### 致谢

致谢 [EmbodiedForge/Inspire-cli](https://github.com/EmbodiedForge/Inspire-cli) 提供的 CLI 初步框架（722966c）

# v1.0.0

## 更新内容

**让 AI Agent 在本机聊天里直通启智平台（`qz.sii.edu.cn`）——无需再点 Web UI。**

### 亮点

**CLI 覆盖日常科研全流程**
- `inspire notebook` —— 实例生命周期 + SSH 引导 + alias 管理 + 远程 `exec` / `scp` / `shell` + 事件与生命周期时间线
- `inspire job` —— **GPU 多节点任务**（分布式训练 / 批量推理 / 并发 worker pool 全走这里）
- `inspire hpc` —— **CPU Slurm 任务**，`#SBATCH` 头由平台自动注入
- `inspire image` —— 自定义镜像 `save` / `register` / `set-default`
- `inspire resources` —— 实时可用量、整节点空余、规格表（`predef_quota_id`）
- `inspire project` / `inspire config` / `inspire user` —— 配额、配置、权限一次查清

**Agent skill 自动装入 5 家 harness**

Claude Code / Codex CLI / Gemini CLI / OpenClaw / OpenCode —— 安装脚本自动探测，把 `SKILL.md` + `references/` 拷进对应 skills 目录；Codex 额外生成 `agents/openai.yaml`。

**零漂移同步**

维护者高频跟进上游变更，`inspire update` 一条命令同时刷新 CLI 与 skill；可选 Clash Verge 7897 分流模板面向差旅 / 非 SII 内网研究者。

### 快速安装

```bash
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

想指定 harness / 仅装 CLI / 不装后台检查：见 [README](https://github.com/realZillionX/InspireSkill#readme)。

### 文档入口

- [SKILL.md](https://github.com/realZillionX/InspireSkill/blob/main/SKILL.md) —— Agent 主规约：认证、命令速查、开发主流程
- [references/browser-api.md](https://github.com/realZillionX/InspireSkill/blob/main/references/browser-api.md) / [openapi.md](https://github.com/realZillionX/InspireSkill/blob/main/references/openapi.md) —— 端点目录
- [references/troubleshooting.md](https://github.com/realZillionX/InspireSkill/blob/main/references/troubleshooting.md) —— SSH / rtunnel / HPC 异常排障
- [references/less-used-commands.md](https://github.com/realZillionX/InspireSkill/blob/main/references/less-used-commands.md) —— `serving` / `model` / 管理员端点

### 反馈

请走 [Issue](https://github.com/realZillionX/InspireSkill/issues)（不建议提 PR；详见 README "开发与贡献" 一节）。附 `inspire --debug <cmd>` 的脱敏日志最有效。
