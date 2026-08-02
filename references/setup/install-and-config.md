# 安装、配置与 SII Proxy

安装、更新、账号配置、项目初始化和本机 SII proxy setup 都看这一份。平台任务运行看 notebook / compute workloads / resources 等业务手册；这里不维护命令清单，命令表面以 CLI help 为准。

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
inspire config check
```

`inspire account add` 会询问平台登录 username、password、base URL 和代理。username 必须是登录 ID，不是网页右上角中文显示名。配置写入 `~/.inspire/accounts/<name>/config.toml`。

不常驻 SII、但本机 Clash Verge 能转发 `*.sii.edu.cn` 时，代理填本机 Clash mixed port。端口以本机 Clash Verge 设置为准，下面只用 `7897` 作为示例：

```text
http://127.0.0.1:7897
```

能直连 SII 校园网时，账号 proxy 可以留空；如果想复用同一套 Clash 配置，就仍然填本机 mixed port，然后在 Clash 的 `SII Proxy` 组里选择 `DIRECT`。

账号级 proxy 是标准入口，也会优先于通用 Shell 代理。为兼容现有开发环境，CLI 仍会继承 `http_proxy` / `HTTP_PROXY`、`https_proxy` / `HTTPS_PROXY` 和 `all_proxy` / `ALL_PROXY`；因此即使账号配置里没有 proxy，这些变量也可能改变登录和 Browser API 的实际链路。用下面的命令查看脱敏后的有效代理来源、目标路由和 `NO_PROXY` 匹配结果：

```bash
inspire config show --compact --filter Proxy
```

平台登录和 Browser API 的通用 Shell HTTP(S) 代理来源会遵守 `NO_PROXY` / `no_proxy`。如果当前网络应直连 SII，可把 `.sii.edu.cn` 加入 bypass；确认是 Shell 代理干扰时，也可只对本次命令取消大小写两组变量：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy inspire init
```

不再使用 `INSPIRE_FORCE_PROXY` 等旧式一次性开关；需要稳定代理时写入账号配置。

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

这里的 `<name>` 是本地 account alias，也就是 `~/.inspire/accounts/<name>/` 的目录名；它不要求等于平台登录 username。`~/.inspire/current` 是普通文本文件，不是 symlink，内容只有当前 active account alias 一行。`inspire account use <name>` 只更新这个默认指针，不会移动或合并任何账号目录。
`inspire account rename <old-name> <new-name>` 只改本地 alias：移动 `~/.inspire/accounts/<old-name>/` 到新目录，若旧 alias 是 active account 则同步更新 `~/.inspire/current`，并把 remembered notebook target cache 中的旧 alias 改成新 alias。平台登录 username 保留在该账号的 `config.toml` 中，不会被 rename 修改。

账号目录、Web session、联网 Notebook SSH 连接缓存和 rtunnel proxy state 都在 `~/.inspire/accounts/<name>/` 下。连接缓存用 `inspire notebook connection list/status/refresh/forget/prune` 管理。

Notebook 连接类命令的 `--account <name>` 也使用本地 account alias，不用平台登录 username 反查 alias。`ssh` / `exec` / `shell` / `scp` / `ssh-config` / `ssh-proxy` 在不传 `--account` 时可以跨账号解析已有 notebook connection；传 `--account all` 表示扫描全部账号，传具体 alias 表示只使用该账号。

联网 notebook 才创建或刷新 SSH / rtunnel connection。不可上网区 notebook 的 `exec` / `shell` 走 JupyterTerminal；文件流转先落到 `/inspire/...` 共享路径，再通过可上网 notebook 的 `notebook scp` 或外部 `rsync` 上传 / 下载。

## 5. SII Proxy

Clash Verge 的目标只有一个：把 `*.sii.edu.cn` 分到单独的 `SII Proxy` 组。公网环境在这个组里选 SII proxy 节点；能直连 SII 校园网时选 `DIRECT`；其它流量继续走订阅原有规则。

Clash Verge Rev 的脚本常见路径：

```text
~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles/Script.js
```

先在 Clash Verge 设置页确认本机 mixed port，再在 `Script.js` 里按下面模板合并 SII 分流逻辑。下面只是模板：节点数量、节点端口和本机 mixed port 都按自己的环境改。`<...>` 必须替换为组织分发的真实 SII proxy host、port、user、password；不要提交真实凭据。`DIRECT` 是给校园网直连使用的选项，不要删。

```javascript
var SII_PROXY_GROUP_NAME = "SII Proxy";
var SII_PROXY_NAMES = ["SII Proxy 1", "SII Proxy 2", "DIRECT"];
var SII_PROXIES = [
  {
    name: "SII Proxy 1",
    type: "socks5",
    server: "<sii-proxy-host-1>",
    port: <sii-proxy-port-1>,
    username: "<sii-proxy-user-1>",
    password: "<sii-proxy-password-1>",
    tls: false,
    udp: true,
    "skip-cert-verify": true
  },
  {
    name: "SII Proxy 2",
    type: "socks5",
    server: "<sii-proxy-host-2>",
    port: <sii-proxy-port-2>,
    username: "<sii-proxy-user-2>",
    password: "<sii-proxy-password-2>",
    tls: false,
    udp: true,
    "skip-cert-verify": true
  }
];

var SII_PROXY_GROUP = {
  name: SII_PROXY_GROUP_NAME,
  type: "select",
  proxies: SII_PROXY_NAMES
};

var SII_MANAGED_PROXY_NAMES = {
  "SII Proxy 1": 1,
  "SII Proxy 2": 1
};

function ensureArray(v) {
  return Array.isArray(v) ? v : [];
}

function forceUnshift(rules, rule) {
  var idx = rules.indexOf(rule);
  if (idx !== -1) rules.splice(idx, 1);
  rules.unshift(rule);
}

function upsertProxy(proxies, newProxy) {
  var out = [];
  for (var i = 0; i < proxies.length; i++) {
    if (proxies[i] && proxies[i].name === newProxy.name) continue;
    out.push(proxies[i]);
  }
  out.push(newProxy);
  return out;
}

function removeProxyNames(proxies, names) {
  var out = [];
  for (var i = 0; i < proxies.length; i++) {
    var p = proxies[i];
    if (p && p.name && names[p.name]) continue;
    out.push(p);
  }
  return out;
}

function resetSiiProxy(config) {
  config.proxies = removeProxyNames(ensureArray(config.proxies), SII_MANAGED_PROXY_NAMES);
  config["proxy-groups"] = ensureArray(config["proxy-groups"]).filter(function(group) {
    return group && group.name !== SII_PROXY_GROUP_NAME;
  });
}

function injectSiiProxy(config) {
  config.proxies = ensureArray(config.proxies);
  for (var i = 0; i < SII_PROXIES.length; i++) {
    config.proxies = upsertProxy(config.proxies, SII_PROXIES[i]);
  }

  config["proxy-groups"] = ensureArray(config["proxy-groups"]);
  config["proxy-groups"].unshift(SII_PROXY_GROUP);

  var rules = ensureArray(config.rules);
  forceUnshift(rules, "DOMAIN-SUFFIX,sii.edu.cn," + SII_PROXY_GROUP_NAME);
  config.rules = rules;
}

function main(config, profileName) {
  resetSiiProxy(config);
  injectSiiProxy(config);
  return config;
}
```

验证只看三件事：

```bash
lsof -iTCP:<mixed-port> -sTCP:LISTEN
curl -sS -o /dev/null -w "sii: %{http_code}\n" -x http://127.0.0.1:<mixed-port> https://qz.sii.edu.cn
inspire config check
```

如果 `qz.sii.edu.cn` 失败，先查 Clash Verge 规则里是否有 `DOMAIN-SUFFIX,sii.edu.cn,SII Proxy`，再查 `SII Proxy` 组当前选中的是可用代理还是 `DIRECT`，最后用 `inspire config show --compact --filter Proxy` 同时确认账号级 proxy、Shell proxy、有效路由和 `NO_PROXY` 匹配结果。
