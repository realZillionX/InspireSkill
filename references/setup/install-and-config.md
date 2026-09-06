# 安装与配置

安装、更新、账号配置、账号初始化和多账号操作看这一份。本机需要通过 Clash Verge 为 `*.sii.edu.cn` 分流时，加载 [`sii-proxy.md`](sii-proxy.md)；持久资产合同见 [`../assets.md`](../assets.md)。平台任务运行看 Notebook、Compute Workloads、Resources 等业务 Reference；命令表面以 CLI Help 为准。

## 1. 安装

macOS、Linux、Windows 都是一等公民。本机是 Windows 时加载 [`windows-native.md`](windows-native.md)；本节其余部分是 macOS / Linux。

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

`inspire update` 会自动识别 `uv tool` / `pipx` 安装来源，升级 CLI 包，刷新 harness skill，并逐步打印进度、刷新到的 harness 列表和新旧版本之间的更新摘要（取自 GitHub Releases，回退到 `main` 的 `CHANGELOG.md`）。`--cli-only` 只升 CLI 包与运行时；`--skill-only` 只刷 `SKILL.md` 和 `references/`。

它同时会扫掉旧版本留下、而当前版本已经不再读的本地状态，包括 `~/.inspire` 下的废弃文件和用户主目录里退役的仓库级 `./.inspire/` 目录。发现内容先按清单打印再问，`--yes` 跳过询问，`--json` 和后台每日检查都只报告不删除。已经是最新版时 `inspire update` 照样扫一遍。`~/.inspire/metrics/` 里的图是明确要过的产物，任何一档都不碰。

## 3. 卸载

```bash
inspire uninstall
inspire uninstall --purge
inspire uninstall --purge-runtime
```

默认删掉安装器写下、且只有 InspireSkill 要的东西：各 harness 的 skill 目录、macOS 每日检查的 launchd agent 及其日志、`~/.inspire/update-status.json`，最后是 CLI 包本身（`uv tool uninstall` / `pipx uninstall`）。执行前打印完整清单并要求确认，`--yes` 跳过；`--json` 下必须带 `--yes`。

分层的依据是归属，不是省事：

- `~/.inspire` 存的是平台凭据，以及 Name 解析索引、Notebook 连接和显卡型号这些本地加速缓存，重装后还能直接用，所以默认保留，`--purge` 才删。
- Playwright 浏览器缓存装在共享位置，本机其它 Playwright 用户也在读，所以默认保留，`--purge-runtime` 才删，且不被 `--purge` 蕴含。
- `INSPIRE.md` 是用户文档，卸载不碰；退役的仓库级 `./.inspire/` 由 `inspire update` 的旧状态清扫处理。

有文件删不掉时会中止并保留 CLI 包，这样清掉阻碍后还能再跑一次 `inspire uninstall`。CLI 已经跑不起来时用安装脚本兜底，参数与上面同名：

```bash
curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash -s -- --uninstall
```

## 4. 账号配置

账号配置和仓库无关，任意目录运行：

```bash
inspire account add <name>
inspire account check
```

`inspire account add` 会询问平台登录 username、password、base URL 和代理。username 使用登录页接受的手机号、学号或邮箱，不是网页右上角中文显示名。配置写入 `~/.inspire/accounts/<name>/config.toml`。

不常驻 SII、但本机 Clash Verge 能转发 `*.sii.edu.cn` 时，先按 [`sii-proxy.md`](sii-proxy.md) 配置本机分流，再把账号 proxy 填为 Clash mixed port。端口以本机设置为准，下面只用 `7897` 作为示例：

```text
http://127.0.0.1:7897
```

能直连 SII 校园网时，账号 proxy 可以留空；如果想复用同一套 Clash 配置，就仍然填本机 mixed port，然后在 Clash 的 `SII Proxy` 组里选择 `DIRECT`。

账号级 proxy 是标准入口，也会优先于通用 Shell 代理。CLI 也会继承 `http_proxy` / `HTTP_PROXY`、`https_proxy` / `HTTPS_PROXY` 和 `all_proxy` / `ALL_PROXY`；因此即使账号配置里没有 proxy，这些变量也可能改变登录和平台请求的实际链路。用下面的命令查看脱敏后的有效代理来源、目标路由和 `NO_PROXY` 匹配结果：

```bash
inspire account check --details
```

平台请求的通用 Shell HTTP(S) 代理来源会遵守 `NO_PROXY` / `no_proxy`。如果当前网络应直连 SII，可把 `.sii.edu.cn` 加入 bypass；确认是 Shell 代理干扰时，也可只对本次命令取消大小写两组变量：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy inspire init
```

需要稳定代理时写入账号配置；临时诊断可按上面的方式显式清除 Shell 代理变量。

## 5. 账号初始化

账号配置完成后运行一次初始化，校验现有 Session，并把账号配置规范化为认证、API、代理等真正的账号级设置：

```bash
inspire init
inspire resources availability --workspace 分布式训练空间 --include-cpu
```

`inspire init` 不把 Project、Compute Group、平台目录或路径复制到本地配置；这些以 Live 查询和每次命令的显式参数为准。旧版留下的 `[context]`、`[profiles]`、`[projects]`、`[project_catalog]`、`[[compute_groups]]`、`[path_aliases]` 和未使用的 API 字段会在重写时删除。

账号初始化和当前仓库无关；CLI 不提供仓库初始化或 Project 绑定。

## 6. 多账号

默认账号与单次账号覆盖：

```bash
inspire account add <name2>
inspire account use <name>
inspire account rename <old-name> <new-name>
inspire account current
inspire --account <name2> notebook list --workspace CPU资源空间
inspire job list --workspace 分布式训练空间 --account <name2>
```

这里的 `<name>` 是本地 Account Alias，也就是 `~/.inspire/accounts/<name>/` 的目录名；它不要求等于平台登录 username。`~/.inspire/current` 保存持久默认账号，`account current` 读取这个默认值。`inspire account use` 只更新默认值，`inspire account rename` 只改本地 Alias，都不会修改平台登录 username。

所有命令都接受 `--account <name>`，可放在根命令、命令组或子命令后；离子命令最近的显式选择生效。它只覆盖本次命令，不写入默认值。不传参数时只使用默认账号。命令开始后固定有效账号，配置、认证、代理、资源索引和 Notebook 连接都跟随这个账号；其他进程切换默认账号不影响已经运行的命令。

账号配置、Web Session、Notebook SSH Connection Cache、资源索引和代理状态按账号保存在 `~/.inspire/accounts/<name>/` 下。切换账号不删除这些缓存，也不停止 SSH 连接；切回后继续复用仍然有效的缓存，失效的 Session / 连接按正常流程刷新。只有显式执行缓存清理、连接遗忘或账号删除等操作才清理相应状态。Notebook Connection Cache 管理和受限 Notebook 文件流转见 [`../notebook.md`](../notebook.md)。
