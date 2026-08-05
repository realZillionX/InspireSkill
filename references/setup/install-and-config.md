# 安装与配置

安装、更新、账号配置和项目初始化看这一份。本机需要通过 Clash Verge 为 `*.sii.edu.cn` 分流时，加载 [`sii-proxy.md`](sii-proxy.md)。平台任务运行看 Notebook、Compute Workloads、Resources 等业务 Reference；命令表面以 CLI Help 为准。

## 1. 安装

macOS + Linux 是一等公民。Windows Agent 用 WSL2；Windows 原生命令行不支持。

前置只需要 `bash`、`curl`、`tar`、Python 3.10+，以及 `uv` 或 `pipx` 任一。没有 `uv` 时先装：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装 InspireSkill：

```bash
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
```

脚本会从 PyPI 安装 `inspire-skill`，把 `SKILL.md` 和 `references/` 刷到已探测到的 Agent harness，并在 macOS 上安装每日静默版本检查。常用参数：

```bash
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash -s -- --harness claude,codex
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash -s -- --harness antigravity,cursor,qoder
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash -s -- --harness qoder-work,kimi-code,kimi-desktop
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash -s -- --no-cli
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash -s -- --no-schedule
```

Qoder Work 的 skill 目录是 `~/.qoderwork/skills/inspire/`。

Kimi Code 的 skill 目录是 `$KIMI_CODE_HOME/skills/inspire/`，未设置 `KIMI_CODE_HOME` 时默认 `~/.kimi-code/skills/inspire/`。

Kimi Desktop 的 skill 目录是 `~/Library/Application Support/kimi-desktop/daimon-share/daimon/skills/inspire/`。

安装后只查这些：

```bash
inspire --version
inspire --help
inspire update --check
```

如果 `inspire: command not found`，开新终端或运行 `exec $SHELL`。如果 Playwright Chromium 缺失，直接重跑安装脚本或运行 `inspire update --cli-only`；安装 / 更新流程会修全局 CLI 的浏览器运行时。

## 2. 更新

```bash
inspire update
inspire update --check
inspire update --cli-only
inspire update --skill-only
```

`inspire update` 会自动识别 `uv tool` / `pipx` 安装来源，升级 CLI 包，刷新 harness skill，并显示 GitHub Releases 的更新摘要。`--cli-only` 只升 CLI 包与运行时；`--skill-only` 只刷 `SKILL.md` 和 `references/`。

## 3. 账号配置

账号配置和仓库无关，任意目录运行：

```bash
inspire account add <name>
inspire config show --compact
inspire --json config show
inspire config check
```

`inspire account add` 会询问平台登录 username、password、base URL 和代理。username 使用登录页接受的手机号、学号或邮箱，不是网页右上角中文显示名。配置写入 `~/.inspire/accounts/<name>/config.toml`。

`config show --format` 只选择 `table` 或 `env`；结构化 JSON 始终使用根级 `inspire --json config show`。

不常驻 SII、但本机 Clash Verge 能转发 `*.sii.edu.cn` 时，先按 [`sii-proxy.md`](sii-proxy.md) 配置本机分流，再把账号 proxy 填为 Clash mixed port。端口以本机设置为准，下面只用 `7897` 作为示例：

```text
http://127.0.0.1:7897
```

能直连 SII 校园网时，账号 proxy 可以留空；如果想复用同一套 Clash 配置，就仍然填本机 mixed port，然后在 Clash 的 `SII Proxy` 组里选择 `DIRECT`。

账号级 proxy 是标准入口，也会优先于通用 Shell 代理。CLI 也会继承 `http_proxy` / `HTTP_PROXY`、`https_proxy` / `HTTPS_PROXY` 和 `all_proxy` / `ALL_PROXY`；因此即使账号配置里没有 proxy，这些变量也可能改变登录和 Browser API 的实际链路。用下面的命令查看脱敏后的有效代理来源、目标路由和 `NO_PROXY` 匹配结果：

```bash
inspire config show --compact --filter Proxy
```

平台登录和 Browser API 的通用 Shell HTTP(S) 代理来源会遵守 `NO_PROXY` / `no_proxy`。如果当前网络应直连 SII，可把 `.sii.edu.cn` 加入 bypass；确认是 Shell 代理干扰时，也可只对本次命令取消大小写两组变量：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy inspire init
```

需要稳定代理时写入账号配置；临时诊断可按上面的方式显式清除 Shell 代理变量。

## 4. 全局发现与项目初始化

账号配置完成后，先做一次全局发现，把可见 project、project catalog、compute group catalog 和默认远端 path alias 写入账号配置：

```bash
inspire init
inspire resources availability --workspace all --include-cpu
```

每个需要仓库 project context 或覆盖默认 path alias 的本地仓库，再各做一次项目初始化：

```bash
cd /path/to/your-repo
inspire init --scope project
```

项目配置分两层：`./.inspire/config.toml` 是仓库共享层，适合记录所有账号共用的 `[cli]` 设置；`./.inspire/accounts/<account>/config.toml` 是账号项目覆盖层。`inspire init --scope project` 会把当前仓库的项目上下文和发现到的远端 path alias 写入账号项目覆盖层。不要维护单独的“远端工作目录”字段；用 alias：

```bash
inspire notebook exec <name> --cwd me "pwd"
inspire notebook exec <name> --cwd me:<repo> "git pull"
inspire notebook scp <name> ./config.yaml me:<repo>/config.yaml
```

如果本仓库需要让 Agent 运行 `inspire ...` 时稳定加载项目 `.env`，先生成模板再登记到共享项目配置：

```bash
inspire config env --output .env.example
cp .env.example .env
inspire config env use .env
```

也可以在项目初始化时顺手登记：

```bash
inspire init --scope project --env-file .env
```

登记后写入的是 `./.inspire/config.toml` 的 `[cli].env_file`，不是某个账号目录；真实父进程环境变量仍然优先于 `.env`。

多账号只用这些命令：

```bash
inspire account add <name2>
inspire account use <name>
inspire account rename <old-name> <new-name>
inspire account current
```

这里的 `<name>` 是本地 account alias，也就是 `~/.inspire/accounts/<name>/` 的目录名；它不要求等于平台登录 username。`~/.inspire/current` 保存当前 active account alias，每行只有一个值。`inspire account use <name>` 只更新这个默认指针，不会移动或合并任何账号目录。
`inspire account rename <old-name> <new-name>` 只改本地 alias：移动 `~/.inspire/accounts/<old-name>/` 到新目录，若旧 alias 是 active account 则同步更新 `~/.inspire/current`，并把 remembered notebook target cache 中的旧 alias 改成新 alias。平台登录 username 保留在该账号的 `config.toml` 中，不会被 rename 修改。

账号目录、Web Session、联网 Notebook SSH Connection Cache 和 rtunnel proxy state 都在 `~/.inspire/accounts/<name>/` 下。Notebook 连接类命令的 `--account <name>` 同样使用本地 Account Alias；跨账号解析、Connection Cache 管理、SSH / JupyterTerminal Transport 和受限 Notebook 文件流转统一见 [`../notebook.md`](../notebook.md)。
