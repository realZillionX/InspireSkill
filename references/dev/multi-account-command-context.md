# 多账号命令上下文设计

> **文档类型**：开发设计草案。用于实现 InspireSkill CLI 的显式账号选择、跨账号只读聚合、Notebook SSH 账号隔离和 Playwright 登录态隔离。
>
> **状态**：待实现。本文描述目标行为和落地顺序，不代表当前 release 已支持这些参数。当前命令事实仍以 `inspire --help` 为准。
>
> **日期**：2026-06-09

## 1. 背景

当前账号模型已经做到一账号一目录：

```text
~/.inspire/accounts/<account>/config.toml
~/.inspire/accounts/<account>/web_session.json
~/.inspire/accounts/<account>/bridges.json
~/.inspire/accounts/<account>/rtunnel-proxy-state.json
```

持久默认账号由 `~/.inspire/current` 指定，`inspire account use <name>` 修改这个指针。这个模型隔离了配置、Web session、Notebook SSH cache 和 rtunnel state，但所有普通命令默认只读取当前 active account。

实际多账号工作流里，用户经常需要：

- 默认账号用于提交新 notebook / job / hpc / ray / serving。
- 查询时临时查看另一个账号或所有账号的资源。
- 不切默认账号就 SSH 到另一个账号的 notebook。
- OpenSSH / VS Code Remote SSH 不受后来 `account use` 的影响。
- 多个账号 session 过期时可以分别重新登录，不互相覆盖 cookies 或 cache。

因此需要把“持久默认账号”和“单次命令有效账号”拆开。

## 2. 目标

1. `inspire account use <name>` 继续设置默认账号，不破坏现有行为。
2. 支持命令级 `--account <name>`，临时覆盖本次命令的账号，不修改 `~/.inspire/current`。
3. 只读聚合命令支持 `--account all`，合并多个已配置账号的结果。
4. SSH / exec / shell / scp / ssh-config / ssh-proxy 在不传 `--account` 时自动跨账号解析目标 notebook；多候选时交互选择并缓存选择，后续默认走缓存路径；`--account <name>` 只用于消歧或强制选择。
5. Playwright 登录态按账号隔离，多个账号重新登录时不共享 browser context、cookies、localStorage 或 session cache。
6. JSON 输出可脚本化，跨账号结果必须带 `account` 字段。
7. 现有无 `--account` 的写操作和普通查询行为保持兼容：仍使用 default account；Notebook 连接类命令升级为跨账号自动解析。

## 3. 非目标

- 不把不同平台账号合并成一个统一身份。
- 不允许 destructive 命令对 `all` 批量执行。
- 不恢复旧的 `[accounts."<user>"]` 合并层。
- 不用 `INSPIRE_ACCOUNT`、`INSPIRE_USERNAME` 等环境变量作为账号选择入口。
- 不要求所有命令第一版同时支持跨账号聚合。

## 4. 术语

| 术语 | 含义 |
| --- | --- |
| default account | `~/.inspire/current` 指向的持久默认账号 |
| command account | 本次命令实际使用的单一账号；普通命令来自 `--account <name>` 或 default account，Notebook 连接类命令还可以来自跨账号 owning-account 自动解析 |
| account selector | 用户输入的账号选择器，取值为账号名或 `all` |
| account-scoped config | `~/.inspire/accounts/<account>/config.toml` |
| account-scoped project config | `./.inspire/accounts/<account>/config.toml` |
| aggregate command | 可对多个账号执行并合并输出的只读命令 |

`current_account()` 只表示 default account。实现层需要新增 resolver 表达 command account，不能把 `current_account()` 改成“当前命令账号”。

## 5. 用户命令面

### 5.1 单账号覆盖

```bash
inspire account use account_a

inspire notebook list --account account_b --workspace all
inspire job list --account account_b --workspace all --active
inspire resources availability --account account_b --workspace 分布式训练空间

inspire notebook ssh --account account_b dev-box --workspace 分布式训练空间
inspire notebook exec --account account_b dev-box "hostname"
inspire notebook scp --account account_b dev-box ./a.txt me:a.txt
```

这些命令不修改 `~/.inspire/current`。命令结束后默认账号仍是 `account_a`。

### 5.2 跨账号只读聚合

```bash
inspire notebook list --account all --workspace all
inspire job list --account all --workspace all --active
inspire config context --account all
inspire resources availability --account all --workspace all --include-cpu
inspire notebook connection list --account all
```

人类输出增加 `account` 列。JSON 输出必须让每条资源都能追溯账号。

推荐 JSON 形状：

```json
{
  "items": [
    {
      "account": "account_a",
      "name": "dev-a",
      "workspace": "分布式训练空间"
    }
  ],
  "errors": [
    {
      "account": "account_b",
      "error": "SessionExpired",
      "message": "..."
    }
  ]
}
```

聚合命令的退出码规则：

- 至少一个账号成功：退出码为 0，失败账号写入 warning 或 JSON `errors`。
- 所有账号失败：使用最重要的失败退出码，并输出所有账号错误。
- `--account all` 没有已配置账号：配置错误。

### 5.3 SSH 与 OpenSSH

命令式 SSH 默认不要求传 `--account`。CLI 应先跨账号解析 notebook 归属；唯一命中时自动使用 owning account，多候选且当前终端可交互时提示用户选择：

```bash
inspire notebook ssh dev-box --workspace CPU资源空间
inspire notebook exec dev-box "hostname"
inspire notebook scp dev-box ./a.txt me:a.txt
```

`--account <name>` 只用于消歧或强制指定账号：

```bash
inspire notebook ssh --account account_a dev-box --workspace CPU资源空间
```

OpenSSH config 必须把账号写入 `ProxyCommand`：

```sshconfig
Host inspire-account-a-dev-box
  HostName dev-box
  User root
  Port 22222
  ProxyCommand inspire notebook ssh-proxy --account account_a %h --workspace CPU资源空间 --port %p --quiet
  StrictHostKeyChecking accept-new
```

这样 VS Code / OpenSSH 新建连接时不依赖当前 default account。

`ssh-config` 即使用户没有传 `--account`，也必须把自动解析出的 owning account 固化到 `ProxyCommand`。否则以后 `account use` 改变 default account 后，OpenSSH host 会漂移。

多候选交互选择后，CLI 写入本地选择缓存。后续相同 notebook / workspace 输入默认走已选账号；如果要忽略该选择缓存并重新解析，传 `--ignore-target-cache`：

```bash
inspire notebook ssh dev-box --workspace CPU资源空间 --ignore-target-cache
```

### 5.4 无 `--account` 的 Notebook 连接解析

`ssh` / `exec` / `shell` / `scp` / `install-deps` / `ssh-config` / `ssh-proxy` 这类“使用某个 notebook 连接”的命令在没有 `--account` 时不应只看 default account。解析顺序：

1. 如果传了 `--account <name>`，只在该账号下解析。
2. 如果没有 `--account` 且没有 `--ignore-target-cache`，先读跨账号选择缓存；缓存目标存在且可连接时直接使用。
3. 选择缓存缺失、被忽略或不可用时，扫描所有账号的 `bridges.json`，按 notebook 名和可选 workspace metadata 找精确 cached connection。
4. cached connection 唯一命中：使用该 bridge 所属账号，不触发其它账号登录；可写入选择缓存。
5. cached connection 多账号命中：
   - 可交互终端：列出候选并 prompt 选择，保存选择缓存。
   - 非交互终端：列出候选并要求传 `--account <name>` 或在交互终端重试。
6. 没有 cached connection，但用户传了 `--workspace`：按账号串行做 live notebook lookup，唯一命中后在 owning account 下 bootstrap SSH cache 并写入选择缓存。
7. live lookup 多账号命中时同样按“交互选择 / 非交互报歧义”处理。
8. 没有 cached connection 且没有 `--workspace`：报错要求传 `--workspace <workspace>` 或先运行 `notebook connection refresh`；不要要求用户传 `--account all`。

示例：

```bash
inspire notebook ssh dev-box --workspace all
```

错误形状：

```text
Notebook name is ambiguous across accounts:
  [1] account_a  dev-box  workspace=CPU资源空间  cached=yes  healthy=yes
  [2] account_b  dev-box  workspace=分布式训练空间  cached=yes  healthy=unknown
Interactive terminal detected. Select a target [1-2], or pass --account <name>.
```

如果没有 cache 且没传 workspace：

```text
No cached notebook connection for 'dev-box' across configured accounts.
Pass --workspace <workspace> so Inspire can look up the notebook owner, or run
`inspire notebook connection refresh dev-box --workspace <workspace>` first.
```

### 5.5 写操作

写操作允许命名账号覆盖，但拒绝 `all`：

```bash
inspire notebook create --account account_b ...
inspire job create --account account_b ...
inspire notebook stop --account account_b dev-box --workspace CPU资源空间
inspire job delete --account account_b train-1 --workspace 分布式训练空间 --yes
```

必须拒绝：

```bash
inspire notebook create --account all ...
inspire job delete --account all ...
```

错误文案：

```text
--account all is not supported for commands that create, stop, delete, or mutate resources.
Pass a single account name.
```

## 6. 命令支持矩阵

| 命令类型 | `--account <name>` | `--account all` | 备注 |
| --- | --- | --- | --- |
| `account list/current/use/add/remove` | 不适用 | 不适用 | 账号管理命令操作本地账号目录 |
| `config show/check/context` | 支持 | `context` 支持，`show/check` 后续评估 | `show` 聚合可能暴露过多配置，不作为第一版 |
| `init` | 支持 | 不支持 | `init --account <name>` 刷新指定账号，不改 default |
| `resources availability/nodes` | 支持 | 支持 | 输出增加 account |
| workload `quota` | 支持 | 支持 | 只读，按账号可见 workspace 计算 |
| `notebook/job/hpc/ray/serving list` | 支持 | 支持 | 第一批聚合命令 |
| `status/events/logs/metrics/instances` | 支持 | 后续支持唯一匹配 | 第一版先单账号 |
| `notebook ssh/exec/shell/scp/install-deps` | 支持 | 不需要 | 无 `--account` 时跨账号解析；多候选交互选择并缓存 |
| `notebook ssh-config/ssh-proxy/connection` | 支持 | `connection list` 支持 | `ssh-config` 必须固化解析出的 account；连接使用命令默认跨账号解析 |
| `create/batch/start/stop/delete/save/publish/register` | 支持 | 不支持 | 所有写操作拒绝 `all` |
| workload `profile` / `notebook path` | 支持 | 不支持 | 写入对应账号的 repo config |

## 7. 配置加载设计

新增显式账号参数，并保持默认兼容：

```python
Config.from_files_and_env(account: str | None = None, require_credentials: bool = True)
config_from_files_and_env(account: str | None = None, require_credentials: bool = True)
Config.get_config_paths(account: str | None = None)
Config.writable_config_path(account: str | None = None)
```

账号路径解析：

```text
account=None
  -> default account from ~/.inspire/current

account="account_b"
  -> ~/.inspire/accounts/account_b/config.toml
  -> ./.inspire/accounts/account_b/config.toml
```

项目配置查找也必须接收 account：

```python
_find_project_config(account: str | None = None)
_project_config_write_path(account: str | None = None)
```

不能通过临时改写 `~/.inspire/current` 来实现 `--account`，否则并发终端、异常退出和 OpenSSH ProxyCommand 都会产生竞态。

## 8. 账号选择基础设施

新增公共 helper：

```python
class AccountSelectorError(Exception):
    pass

def resolve_command_account(raw: str | None, *, allow_all: bool) -> str | Literal["all"]:
    ...

def iter_selected_accounts(selector: str | Literal["all"]) -> list[str]:
    ...
```

解析规则：

1. `raw is None`：普通命令返回 default account；没有 default 时提示 `inspire account use <name>` 或 `inspire account add <name>`。
2. `raw == "all"` 且 `allow_all=True`：返回 `all`。
3. `raw == "all"` 且 `allow_all=False`：报 validation error。
4. 其他值：按账号名校验，必须存在于 `~/.inspire/accounts/`。

Notebook 连接类命令不能直接用这条 `raw is None -> default account` 规则；它们使用 [Tunnel / SSH 设计](#11-tunnel--ssh-设计) 里的 owning account resolver，先跨账号找唯一目标，找不到或有歧义时再报错。

命令层使用共享 Click option decorator，避免每个命令手写不一致：

```python
def account_option(*, allow_all: bool = False):
    return click.option(
        "--account",
        "account_selector",
        required=False,
        help="Account name. Use 'all' only on supported read-only commands.",
    )
```

`Context` 增加可选字段：

```python
class Context:
    json_output: bool
    debug: bool
    account_selector: str | None
    account_name: str | None
```

命令内部仍应显式把 `account=` 传给 config/session/tunnel helper。`Context` 只用于输出和错误文案，不作为深层隐式状态。

## 9. Web Session 与 Playwright 隔离

### 9.1 Session cache

`WebSession` 已支持 `load(account=...)` 和 `save(account=...)`。需要把 session 入口补齐：

```python
get_web_session(
    force_refresh: bool = False,
    require_workspace: bool = False,
    account: str | None = None,
) -> WebSession
```

登录流程必须用同一个 account 读取配置和保存 session：

```python
config = Config.from_files_and_env(account=account, require_credentials=False)
session = login_with_playwright(config.username, config.password, base_url=config.base_url)
session.save(account=account)
```

刷新失效 session 时，只清理该账号：

```python
clear_session_cache(account=account)
get_web_session(account=account, force_refresh=True)
```

### 9.2 Playwright browser / context

账号隔离边界是 browser context 与 storage state，不要求常驻两个独立 browser 进程。

要求：

- 每次登录使用新的 browser context。
- 每个账号的 `storage_state` 只写入该账号的 `web_session.json`。
- 请求 fallback 使用 `browser.new_context(storage_state=session.storage_state)`。
- 不使用共享 `user_data_dir`。
- 不复用其它账号的 context、cookies 或 localStorage。

可以串行登录多个账号：

```text
account_a login -> context_a -> storage_state_a -> accounts/account_a/web_session.json
account_b login -> context_b -> storage_state_b -> accounts/account_b/web_session.json
```

### 9.3 Browser request client cache

当前 thread-local browser request client 按 session fingerprint 复用。多账号后 cache key 必须包含账号和代理相关信息：

```text
(account, base_url, browser_api_prefix, proxy_fingerprint, session_fingerprint)
```

建议把单一 thread-local client 改成 thread-local dict：

```python
_BROWSER_CLIENT_TLS.clients: dict[BrowserClientKey, _BrowserRequestClient]
```

关闭逻辑按 key 关闭，进程退出时关闭全部。

### 9.4 账号级登录锁

为每个账号加文件锁，避免两个并发命令同时刷新同一个 `web_session.json`：

```text
~/.inspire/accounts/<account>/.web_session.lock
```

锁范围只包围“判断 cache 不可用 -> Playwright 登录 -> 写 web_session.json”。只读加载 session 不需要持锁。

聚合命令第一版串行执行账号，避免一次打开多个 Playwright 登录流程。后续可以并发，但必须保留 per-account lock。

## 10. Browser API 与运行时缓存

这些缓存当前按 active account 或进程全局缓存，需要改成 account-aware：

| 缓存 | 当前风险 | 目标 |
| --- | --- | --- |
| Browser API base URL | `--account b` 可能读到账号 A 的 base URL | cache key 包含 account |
| Browser API prefix | 同上 | cache key 包含 account |
| availability cache | `--account all` 可能复用另一个账号的资源可见性 | cache key 包含 account 和 query scope |
| Playwright proxy / requests proxy / rtunnel proxy | 账号代理可能不同 | 从 account-scoped config 读取 |
| Browser request client | 可能复用其它账号 storage_state | key 包含 account 和 session fingerprint |

所有 `clear_*_cache()` 需要支持清理单账号，或者在临时账号命令结束后不需要全量清理。

## 11. Tunnel / SSH 设计

Tunnel config 底层已有 `load_tunnel_config(account=...)`。命令层需要补齐：

```python
load_tunnel_config(account=command_account)
save_tunnel_config(config)
```

`TunnelConfig.account` 必须保持为实际账号名，这样保存路径是：

```text
~/.inspire/accounts/<account>/bridges.json
```

`BridgeProfile` 可不新增 account 字段。跨账号输出时由读取该文件的账号作为 owner。若未来需要导出单条连接配置，可在 JSON payload 增加 `account`，不写入 bridge 本身。

跨账号“我上次选了哪个 notebook”不应写进某个账号的 `bridges.json`，而应写入账号外层的选择缓存：

```text
~/.inspire/notebook-targets.json
```

该文件只记录目标偏好，不复制 proxy URL、cookie、密码或其它敏感值。示例：

```json
{
  "version": 1,
  "targets": {
    "dev-box|workspace=CPU资源空间": {
      "account": "account_a",
      "notebook_name": "dev-box",
      "workspace_name": "CPU资源空间",
      "bridge_name": "dev-box",
      "notebook_id": "nb-...",
      "updated_at": 1781000000
    }
  }
}
```

Cache key 使用用户输入归一化后的 notebook 名和 workspace selector。带 `--workspace all` 的选择必须和不带 workspace 的选择分开，避免过宽输入误复用窄 workspace 结果。

`ssh-proxy` 必须支持 `--account`，因为它是 OpenSSH / VS Code 实际执行的命令：

```bash
inspire notebook ssh-proxy --account account_a dev-box --workspace CPU资源空间 --port 22222
```

连接类命令需要一个统一的 owning account resolver：

```python
def resolve_notebook_connection_account(
    *,
    notebook: str,
    account: str | None,
    workspace: str | None,
    allow_live_lookup: bool,
    ignore_target_cache: bool,
    interactive: bool,
) -> NotebookConnectionTarget:
    ...
```

目标返回值至少包含：

```python
@dataclass
class NotebookConnectionTarget:
    account: str
    notebook_name: str
    workspace_name: str | None
    cached_bridge_name: str | None
    notebook_id: str | None
    source: Literal["target_cache", "bridge_cache", "live_lookup", "prompt"]
```

解析优先级：

1. 显式 `--account <name>`：只加载该账号的 tunnel cache；需要 live lookup 时只用该账号 session。
2. 无 `--account` 且 `ignore_target_cache=False`：先查 `notebook-targets.json`。缓存目标存在、对应 account 仍存在、对应 bridge 仍存在且轻量 SSH preflight 通过时，直接使用。
3. 目标缓存缺失、被 `--ignore-target-cache` 跳过、账号/bridge 不存在或 preflight 失败：标记该缓存 entry stale，本次解析不再使用它。
4. 扫描所有账号的 `bridges.json`，匹配 bridge name、notebook name 和 workspace metadata。
5. bridge cache 唯一命中：直接用该账号的 tunnel cache 执行 SSH；写入或刷新 `notebook-targets.json`。
6. bridge cache 多命中：构造候选列表。若 `interactive=True`，prompt 用户选择并写入 `notebook-targets.json`；否则打印候选并拒绝。
7. bridge cache 未命中且 `allow_live_lookup=True` 且传了 `--workspace`：对账号串行 live lookup notebook。
8. live lookup 唯一命中：bootstrap owning account 的 `bridges.json`，再写入 `notebook-targets.json`。
9. live lookup 多命中：可交互则 prompt 选择并 bootstrap / 写缓存；非交互则打印候选并拒绝。
10. bridge cache 未命中且没有 `--workspace`：拒绝并提示传 workspace。

交互能力判断：

- `stdin` 和 `stderr` 都是 TTY，且没有 `--json`。
- SSH byte stream 场景必须保持 stdout 干净；prompt、候选列表和警告都写 stderr。
- `ssh-proxy` 作为 OpenSSH `ProxyCommand` 执行时默认视为非交互，即使底层 fd 是 TTY，也不能 prompt。`ssh-config` 负责提前解析并把 account 固化到 ProxyCommand。

Prompt 选择规则：

- 候选按 `(workspace_name, account, notebook_name)` 稳定排序。
- 候选显示 `account`、`notebook`、`workspace`、`cached`、`healthy`、`notebook_id` 的脱敏短标签。
- 用户选择后写入 `notebook-targets.json`，并继续连接。
- 用户取消时退出，不写缓存。

`--ignore-target-cache` 只忽略跨账号选择缓存，不忽略每个账号自己的 tunnel cache。它用于重新列出候选或改选账号：

```bash
inspire notebook ssh dev-box --workspace CPU资源空间 --ignore-target-cache
```

缓存路径连不上时的恢复规则：

1. 对 target cache 指向的 bridge 做轻量 SSH preflight。
2. 若失败，先按现有自动重连逻辑在同一 account 下 refresh tunnel。
3. refresh 后仍失败，则本次命令忽略 target cache 并重新构造候选。
4. 如果重新构造后只有一个候选，直接使用并刷新 target cache。
5. 如果有多个候选且可交互，重新 prompt；用户的新选择覆盖旧缓存。
6. 如果有多个候选但非交互，打印候选并退出，要求传 `--account <name>` 或在交互终端运行 `--ignore-target-cache`。

`ssh-config` 生成的 `ProxyCommand` 必须包含 `--account <resolved-account>`。如果用户没有显式传 `--account`，也应把自动解析后的 owning account 写入 ProxyCommand，避免以后 default account 变化导致连接漂移。

## 12. 聚合执行模型

新增聚合 helper：

```python
T = TypeVar("T")

@dataclass
class AccountRunResult(Generic[T]):
    account: str
    ok: bool
    value: T | None = None
    error_type: str | None = None
    message: str | None = None
```

第一版串行：

```python
def run_for_accounts(accounts: list[str], fn: Callable[[str], T]) -> list[AccountRunResult[T]]:
    results = []
    for account in accounts:
        try:
            results.append(AccountRunResult(account=account, ok=True, value=fn(account)))
        except Exception as exc:
            results.append(AccountRunResult.from_exception(account, exc))
    return results
```

后续可加并发参数，但默认仍串行，避免 Playwright 登录和平台 API 同时打满。

聚合输出要求：

- 资源 row 增加 `account`。
- 错误不吞掉，聚合到 `errors`。
- 人类输出中，失败账号以 warning 显示，不中断已成功账号的结果。

## 13. 错误与歧义处理

### 13.1 未配置账号

```text
Account not found: account_b
Run `inspire account list` to inspect configured accounts, or `inspire account add account_b`.
```

### 13.2 无 default account

普通命令无 `--account` 且没有 default account 时：

```text
No active account. Use `inspire account use <name>` or pass `--account <name>`.
```

Notebook 连接类命令无 `--account` 时可以不依赖 default account；唯一命中时直接连接，多候选时可交互选择，无法解析时按连接类错误提示处理。

### 13.3 `all` 不允许

```text
--account all is not supported for this command. Pass a single account name.
```

### 13.4 跨账号同名资源

无显式账号的跨账号唯一匹配模式下：

```text
Resource name is ambiguous across accounts:
  [1] account_a  dev-box  workspace=CPU资源空间  cached=yes  healthy=yes
  [2] account_b  dev-box  workspace=分布式训练空间  cached=yes  healthy=unknown
Select target [1-2], or press Ctrl-C to cancel:
```

非交互模式：

```text
Resource name is ambiguous across accounts:
  [1] account_a  dev-box  workspace=CPU资源空间  cached=yes
  [2] account_b  dev-box  workspace=分布式训练空间  cached=yes
Pass --account <name> to select one, or rerun in an interactive terminal.
```

### 13.5 部分账号失败

人类输出：

```text
Warning: account_b failed: session expired and login failed
```

JSON：

```json
{
  "items": [],
  "errors": [
    {
      "account": "account_b",
      "error": "AuthError",
      "message": "session expired and login failed"
    }
  ]
}
```

## 14. 迁移与兼容

不迁移磁盘布局。现有文件继续有效：

```text
~/.inspire/current
~/.inspire/accounts/<account>/config.toml
~/.inspire/accounts/<account>/web_session.json
~/.inspire/accounts/<account>/bridges.json
```

无 `--account` 的写操作和普通查询命令完全沿用 default account。Notebook 连接类命令是例外：它们会跨账号自动解析目标 notebook，必要时交互选择，以便直接连接其它账号的开发机。

新增 `--account` 不改变 `account use`。`account use` 仍是设置默认账号的命令，不负责切换当前进程里的临时账号。

第一版文档需要明确：

- `--account <name>` 是临时覆盖。
- `--account all` 只在 help 标明支持的命令可用。
- `notebook ssh <name>` 不需要 `--account all`；没有显式账号时会自动跨账号解析，必要时交互选择。
- `ssh-config` 输出会固化解析出的账号，后续 `account use` 不影响这条 OpenSSH host。

## 15. 分阶段交付

### Phase 1：账号上下文基础设施

- `Config.from_files_and_env(account=...)`
- `_find_project_config(account=...)`
- `_project_config_write_path(account=...)`
- `get_web_session(account=...)`
- `get_base_url(account=...)`
- account selector helper 和 Click decorator
- Browser API / availability / proxy cache key account-aware
- per-account web session lock

验收：

- current 为 B 时，`Config.from_files_and_env(account="A")` 读取 A 的账号配置和 A 的 repo 配置。
- current 为 B 时，`get_web_session(account="A")` 只读写 A 的 `web_session.json`。

### Phase 2：Notebook SSH 自动账号解析与单账号覆盖

- `notebook ssh --account <name>`
- `notebook ssh-proxy --account <name>`
- `notebook ssh-config --account <name>`
- `notebook ssh/exec/shell/scp/install-deps/ssh-config --ignore-target-cache`
- `notebook connection list/status/refresh/forget/prune --account <name>`
- `notebook exec/shell/scp/install-deps --account <name>`
- 无 `--account` 的 `notebook ssh/exec/shell/scp/install-deps/ssh-config` 跨账号扫描 cache，唯一命中即使用 owning account。
- 多候选时，交互终端 prompt 选择并写入 `~/.inspire/notebook-targets.json`。
- 选择缓存存在时，后续相同 notebook / workspace 默认使用该缓存目标。
- `--ignore-target-cache` 跳过选择缓存并重新解析 / 重新选择。
- 选择缓存目标连不上时，先尝试刷新同账号 tunnel；仍失败则忽略旧缓存并重新列候选。
- 无 cache 但传了 `--workspace` 时，跨账号 live lookup 并 bootstrap owning account 的 SSH cache。

验收：

- default 为 B 时，`notebook ssh --account A` 使用 A 的 `bridges.json` 和 A 的 Web session。
- default 为 B 时，`notebook ssh dev-a` 可通过 A 的唯一 cached connection 直接进入 A 的 notebook。
- 无 cache 时，`notebook ssh dev-a --workspace CPU资源空间` 可通过 live lookup 找到 A 并 bootstrap。
- A/B 都有同名 `dev-box` 时，交互模式 `notebook ssh dev-box --workspace all` 列出候选并 prompt 选择；选择后写入 target cache。
- A/B 都有同名 `dev-box` 时，非交互模式 `notebook ssh dev-box --workspace all` 拒绝并要求 `--account <name>` 或交互重试。
- target cache 指向 A 后，再次 `notebook ssh dev-box --workspace all` 默认走 A。
- `notebook ssh dev-box --workspace all --ignore-target-cache` 跳过 target cache 并重新列候选。
- target cache 指向 A 但 A 的 bridge preflight 和 refresh 都失败时，CLI 重新列候选；交互模式可改选 B 并覆盖 target cache。
- `ssh-config --account A` 生成的 `ProxyCommand` 包含 `--account A`。
- `ssh-config dev-a` 自动解析到 A 后，生成的 `ProxyCommand` 包含 `--account A`。
- `connection list --account A` 不显示 B 的连接。

### Phase 3：只读聚合

- `config context --account all`
- `resources availability --account all`
- workload `quota --account all`
- `notebook list --account all`
- `job list --account all`
- `notebook connection list --account all`

验收：

- 输出包含 account。
- 某个账号失败时仍显示其它账号结果。
- JSON 有 `items` 和 `errors`。

### Phase 4：观测命令账号覆盖

- `notebook status/events/metrics/url/lifecycle --account <name>`
- `job status/events/logs/metrics/instances --account <name>`
- `hpc/ray/serving` 对应观测命令 `--account <name>`
- 可选：非连接类观测命令的无账号跨账号唯一匹配模式

验收：

- 同名资源在不同账号下不会误解析。
- 跨账号唯一匹配出现多匹配时拒绝并列出候选。

### Phase 5：写操作账号覆盖

- `create/batch/start/stop/delete/save/register/publish --account <name>`
- workload `profile` 和 `notebook path` 写入指定账号的 repo config
- 所有写操作拒绝 `--account all`

验收：

- default 为 A 时，`create --account B` 只使用 B 的项目、workspace、image 和 session。
- `--account all` 在写操作上统一 validation error。

## 16. 测试计划

### 单元测试

- account selector：
  - omitted 使用 default。
  - named account 必须存在。
  - `all` 只在 `allow_all=True` 时通过。
- config：
  - current 为 B，显式 account A 读取 A 的 account config。
  - 显式 account A 读取 `./.inspire/accounts/A/config.toml`。
  - 写入 path/profile 时落到指定账号项目配置。
- session：
  - `WebSession.load(account=A)` 不受 current B 影响。
  - `get_web_session(account=A)` 登录后 `save(account=A)`。
  - `clear_session_cache(account=A)` 不删除 B。
- Playwright client：
  - A/B 不同 storage_state 不复用同一个 client。
  - proxy/base_url/prefix cache key 包含 account。
- tunnel：
  - `load_tunnel_config(account=A)` 只读 A 的 `bridges.json`。
  - `ssh-config --account A` 的 ProxyCommand 含账号。
  - default 为 B 时，`notebook ssh dev-a` 使用 A 的唯一 cached bridge。
  - A/B 都有 `dev-box` cache 时，交互模式 prompt 选择并写入 `notebook-targets.json`。
  - A/B 都有 `dev-box` cache 时，非交互模式报 ambiguity。
  - target cache 命中时优先使用缓存目标。
  - `--ignore-target-cache` 跳过 target cache，但仍允许使用 per-account bridge cache。
  - target cache 对应 bridge 连不上且 refresh 失败时，resolver 标记旧 entry stale 并重新列候选。

### CLI 回归测试

- `notebook connection list --account all` 合并 A/B cache。
- `notebook ssh --account A` 在 current B 时使用 A cache。
- `notebook ssh <A-only-notebook>` 在 current B 时自动使用 A cache。
- `notebook ssh-config <A-only-notebook>` 在 current B 时生成包含 `--account A` 的 ProxyCommand。
- `notebook ssh dev-box` 多候选交互选择 A 后，第二次默认使用 A。
- `notebook ssh dev-box --ignore-target-cache` 重新列出 A/B 候选。
- cached selection A 连不上且 refresh 失败时，交互模式重新 prompt 并允许选择 B。
- `notebook list --account all` 合并输出并带 account。
- `job list --account all --active` 合并输出并带 account。
- `create --account all`、`delete --account all` 拒绝。
- 普通命令无 default 且无 `--account` 时提示传 `--account` 或 `account use`。
- 无 default 时，`notebook ssh <A-only-notebook>` 仍可通过 A 的唯一 cached connection 自动解析并连接。

### Live smoke

只在受控账号和小资源上跑：

```bash
inspire account list
inspire notebook list --account <a> --workspace all
inspire notebook list --account <b> --workspace all
inspire notebook list --account all --workspace all
inspire notebook connection list --account all
inspire notebook ssh-config --account <a> <notebook> --workspace <workspace>
inspire notebook ssh-config <a-only-notebook> --workspace <workspace>
inspire notebook ssh <ambiguous-notebook> --workspace all --ignore-target-cache
```

写操作 live smoke 需要人工确认资源和费用边界。

## 17. 开放问题

1. 是否提供顶层全局选项 `inspire --account <name> notebook list ...`？
   - Click 全局选项必须放在 command group 前，用户体验不如 leaf command。
   - 第一版建议只做 leaf command `--account`，后续再评估全局 alias。
2. `--account all` 是否默认触发过期账号重新登录？
   - 第一版建议触发，但串行执行并收集失败。
   - 后续可加 `--no-login`，只使用现有 session。
3. 无 `--account` 的 SSH live lookup 是否默认扫描所有账号和所有 workspace？
   - 第一版建议：无 cache 时必须传 `--workspace`，可以传 `--workspace all`，但不在完全无 workspace 的情况下触发全账号全 workspace live scan。
   - cached connection fast path 不需要 workspace。
4. `config show --account all` 是否安全？
   - 账号配置可能包含敏感字段，即使 compact 输出会脱敏，也不作为第一版聚合目标。
5. 聚合命令是否并发？
   - 第一版串行。并发需要 per-account login lock 和更严格的 Playwright client key。
6. 是否提供 target cache 管理命令？
   - 第一版可以先只提供 `--ignore-target-cache` 和交互选择后的自动覆盖。
   - 后续可加 `inspire notebook connection target list/forget`，用于查看或删除 `~/.inspire/notebook-targets.json` 条目。

## 18. 实现原则

- 不通过临时改写 `~/.inspire/current` 实现 `--account`。
- 深层 helper 尽量显式接收 `account=`，不要引入新的进程全局状态。
- `current_account()` 保持“持久默认账号”语义。
- `--account all` 默认只用于只读聚合；写操作统一拒绝。
- Notebook 连接类命令不需要 `--account all`；无显式账号时跨账号解析，唯一命中直接连接，多候选时交互选择并缓存，非交互时要求 `--account <name>`。
- `--ignore-target-cache` 只忽略跨账号选择缓存，不忽略账号内 tunnel cache。
- 选择缓存只存目标偏好，不存 cookie、proxy URL、密码或平台 session。
- 输出跨账号资源时永远带 account。
- OpenSSH / VS Code Remote SSH 的 `ProxyCommand` 必须固化账号。
- 多账号 Playwright 隔离以 storage_state + browser context 为边界，不共享 cookies 或 localStorage。
