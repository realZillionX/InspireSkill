# 安装、更新与初始化

安装、更新、账号配置、项目初始化或代理 setup 时，先查本手册。平台任务运行、notebook、job、HPC、Ray、serving 和镜像操作看对应业务手册。

## 1. 平台支持

macOS + Linux 是一等公民。Windows Agent 请用 WSL2；CLI 依赖 SSH、rsync、GPFS 目录约定和 POSIX 文件权限，Windows 原生不在 roadmap。

## 2. 安装

前置：`bash`、`curl`、`tar`、Python 3.10+，以及 `uv`（推荐）或 `pipx` 任一。两个都没装就先装 `uv`：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

然后一行装好 InspireSkill：

```bash
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

不需要把仓库克隆到本地。脚本会：

1. 从 PyPI 用 `uv tool install` / `pipx install` 安装 `inspire-skill`。
2. 确保 `~/.local/bin` 在 PATH 上。
3. 探测本机 harness，并把 `SKILL.md` 与 `references/` 安装到对应 skill 目录。
4. macOS 上安装每日静默版本检查的 launchd agent。

可选参数：

```bash
curl -fsSL .../install.sh | bash -s -- --harness claude
curl -fsSL .../install.sh | bash -s -- --harness claude,codex
curl -fsSL .../install.sh | bash -s -- --no-cli
curl -fsSL .../install.sh | bash -s -- --no-schedule
```

安装后检查：

- 装完后 `inspire: command not found`：运行 `exec $SHELL` 或开新终端。
- `installer failed` / 包索引超时：先确认本机网络和代理；必要时在工具配置或 shell profile 中持久设置 Python 包索引，再重跑安装脚本。不要把一次性环境变量前缀写进任务命令示例。
- `Playwright does not support chromium on ...` 或 `Executable doesn't exist at ~/.cache/ms-playwright/...`：说明当前 Playwright bundled Chromium 没有可用运行时。若本机已安装 Google Chrome，可设置 `INSPIRE_PLAYWRIGHT_CHROMIUM_CHANNEL=chrome`；需要指定完整路径时，设置 `INSPIRE_PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/google/chrome/chrome`，该路径优先于 channel。
- Notebook SSH 命令面检查：`inspire notebook ssh --help` 应显示 `ssh <notebook>` 主入口；`inspire notebook connection --help` 管理连接缓存；`inspire notebook ssh-config --help` 输出原生 OpenSSH 配置片段。
- 同一台机器已经装过：直接重跑安装脚本，脚本是幂等的。

## 3. 更新

```bash
inspire update                # CLI 包 + SKILL/references 一起升到最新
inspire update --check        # 只检查，不动
inspire update --cli-only     # 仅升 Python 包
inspire update --skill-only   # 仅刷 SKILL.md / references/
```

`inspire update` 会自动识别当前安装由 `uv tool` 还是 `pipx` 管理，并调用对应升级命令。
如果默认 PyPI 因网络或镜像问题超时，命令会自动尝试常见 PyPI 镜像。网络受限环境可提前检查 Clash 虚拟/TUN 网卡，或持久配置 `UV_DEFAULT_INDEX` / `PIP_INDEX_URL`。

从 v3.0.3 之前的版本升级时，先重跑一次安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

落到 v3.0.3 之后，后续 patch 直接 `inspire update`。

## 4. 账号级初始化

账号配置和仓库无关，任意目录运行：

```bash
inspire account add <name>
inspire config show --compact
```

`inspire account add` 会询问平台登录 username、password、base URL、代理和是否设为活动账号。这里的 username 必须是登录 ID（手机号、学号 / 工号或邮箱等），不是网页右上角显示的中文姓名。结果写入 `~/.inspire/accounts/<name>/config.toml`，包含身份、`base_url`、代理等。

如果在启智 Notebook 容器内安装 InspireSkill，CLI 不会继承打开该 Notebook 时浏览器里的 SSO 登录态，仍需要用账号密码生成自己的 Web session。Notebook 内可运行，但 `inspire config show --compact` 里的 `INSPIRE_USERNAME` 也必须是登录 ID；如果误填成显示名，重新运行 `inspire init --username <login-id>` 或重建账号配置。

账号配置不包含远端工作目录。远端路径通过项目级 `[path_aliases]` 管理，`inspire init` 会写入默认 alias，例如 `me`、`public`、`global-me` 以及按存储池区分的 `ssd.me` / `hdd.me` / `qb-ilm2.me`。

## 5. 项目级初始化

每个本地仓库各做一次：

```bash
cd /path/to/your-repo
inspire init
inspire resources availability --workspace all --include-cpu
```

`inspire init` 会发现可用项目、workspace、compute group 和远端存储池，并写入：

- 账号级发现结果：`~/.inspire/accounts/<name>/config.toml`
- 当前仓库项目上下文：`./.inspire/accounts/<name>/config.toml`

CLI 会拒绝账号 config.toml 中出现 `[paths]`，避免项目级路径污染所有仓库。

初始化后，远端命令和传文件优先使用 alias：

```bash
inspire notebook exec <name> --cwd me "pwd"
inspire notebook exec <name> --cwd me:<repo> "git pull"
inspire notebook scp <name> ./config.yaml me:<repo>/config.yaml
```

## 6. 多账号

```bash
inspire account add <name2>
inspire account use <name>
inspire account current
```

每个账号的 config、notebook SSH 连接缓存、Notebook rtunnel proxy state 和 Web session 登录缓存都放在 `~/.inspire/accounts/<name>/` 下；活动账号由 `~/.inspire/current` 选择。`inspire account use <name>` 只切换指针并刷新当前进程里的账号敏感缓存，不删除被切走账号的本地缓存；需要删除账号目录时才用 `inspire account remove <name>`。连接缓存通过 `inspire notebook connection list/status/refresh/forget/prune` 管理；`connection forget/prune` 只影响 Inspire 运行时缓存，不会修改用户的 `~/.ssh/config`。

## 7. 代理 setup

不常驻 SII 的科研人员通常需要让本机代理同时转发公网和 `*.sii.edu.cn` 流量。Clash Verge 示例和账号级代理衔接见 [proxy-setup.md](proxy-setup.md)。
