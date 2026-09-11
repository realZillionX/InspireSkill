# Python SDK（实验性）

## CLI compatibility changes in Unreleased

The shared workload output projections now scrub status text **before** applying
that workload's own `normalize_status`. This affects JSON and human output from
`job`, `hpc`, `ray`, `serving`, `notebook`, and `tensorboard` `list` / `status`,
including Job/HPC batch status and `job list --watch`. The final detail from
`job wait` and the status field in `serving api` reuse these projections too.
Mixed case (`Running`, `rUnNiNg`) becomes `RUNNING`, and blank or fully
scrubbed status becomes `UNKNOWN`.

| Workload | Previous blank JSON (list / status) | Previous blank human output (list / status) | New blank value |
| --- | --- | --- | --- |
| Job | `N/A` / `N/A` | `N/A` or empty / `N/A` | `UNKNOWN` |
| HPC, Ray | `N/A` / `N/A` | `N/A` / `N/A` | `UNKNOWN` |
| Serving | empty string / field omitted | `-` / `N/A` | `UNKNOWN` |
| Notebook | empty string / field omitted | `Unknown` / `N/A` | `UNKNOWN` |
| TensorBoard | empty string / empty string | empty / field omitted | `UNKNOWN` |

Job retains its specific vocabulary: `job_running` → `RUNNING`,
`CREATING` / `job_creating` → `PENDING`, `STOPPED` / `job_stopped` → `CANCELLED`,
and unrecognised values → `UNKNOWN`. The other five workloads uppercase
unrecognised scrubbed values and retain `STOPPED`. They do not acquire Job's
`JOB_` prefix mapping. TensorBoard strips its own case-insensitive `tb_status_`
prefix before uppercasing: `tb_status_running` and `running` now both produce
`RUNNING` in CLI list/status and SDK status models; update lowercase
comparisons. URLs, paths and raw IDs must not reach public status output;
normalising before scrubbing can leave unknown sensitive strings intact.
Operation acknowledgements such as `created` and `stopped`, instance/node
statuses and run-history records are separate fields, not workload lifecycle
projections covered by this change.

`serving create --model NAME` (including `--dry-run`) now enumerates all filtered
model pages before resolving the name. Each request asks for 100 models and
keeps the keyword, workspace, user and optional project filters. The limit is
100 requests / 10,000 rows with full pages. A stable catalogue with M matches
costs `max(1, ceil(M / 100))` requests up to that limit and O(M) memory; latency
adds sequential network round trips. A first exact match does not end the scan:
a later exact match must participate in the existing ambiguity / `--pick` rules.
The wrapper documents that `page_size=-1` is rejected; no larger page size is
assumed supported without platform evidence.

An empty page before the reported total, repeated/missing model identities, or
the page limit produces `ConfigError` (CLI exit 10) before submission. Errors
state the cause and recommend retrying / asking the platform administrator to
check pagination, or using a more specific model name / a workspace with fewer
matches when the limit is reached. The old single-page lookup could incorrectly
report a model absent beyond the first 100 matches. SDK resource pagination
and typed status models retain their existing contracts. SDK workload `.view`
mappings that reuse these public projections also receive the normalised
`status` values (including batch Job `.view`); callers inspecting those mappings
must update old raw-status comparisons too.

## 接入

同一个 `inspire-skill` 包提供两个受支持的实验性入口：`InspireClient` 用于同步脚本和同步 worker；`InspireAsyncClient` 用于 asyncio 应用、Agent runtime 和异步 Web 服务，在调用方事件循环执行原生异步 I/O。两者均可从 `inspire` 或 `inspire.sdk` 导入，使用相同的资源模型、引用、异常和平台能力；异步入口的并发与取消边界见下文。

SDK 与 CLI 复用 browser_api、共享 services 和 `inspire.platform.web.transport.Transport`；安装依赖和 CLI 默认行为不变。源码安装可在 `cli/` 运行 `uv pip install -e .`，应用项目可用 `uv add /path/to/InspireSkill/cli`。

以下两个完整示例需要已配置的本地账号及可访问的工作区；`login()` 显式建立会话。保存为对应文件后，在 `cli/` 运行 `uv run python sync_example.py my-account "工作区名称"` 或 `uv run python async_example.py my-account "工作区名称"`。

同步快速开始（`sync_example.py`）：

```python
import sys
from inspire import InspireClient

def main(account: str, workspace: str) -> None:
    with InspireClient(account=account) as client:
        client.login()
        ws = client.workspaces.get(workspace)
        for job in client.jobs.iter(ws.ref, max_items=40):
            print(job.name, job.status)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

异步快速开始（`async_example.py`）：

```python
import asyncio
import sys
from inspire import InspireAsyncClient

async def main(account: str, workspace: str) -> None:
    async with InspireAsyncClient(account=account) as client:
        await client.login()
        jobs, notebooks = await asyncio.gather(
            client.jobs.list(workspace, limit=5),
            client.notebooks.list(workspace, limit=5),
        )
        print("Jobs:", [job.name for job in jobs.items])
        print("Notebooks:", [notebook.name for notebook in notebooks.items])
        async for job in client.jobs.iter(workspace, max_items=10):
            print(job.name, job.status)

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
```

SDK 面向能访问平台的本机或控制节点。运行环境需满足网络、账号缓存和文件锁条件；本次文档审阅仅核对代码和离线测试，未验证真实平台当前的会话寿命、分页上限、GPU 容器兼容性或网络共享盘锁语义。下文区分代码实施的限制与平台协议所提供的信息。

## 架构

依赖方向为 `sdk → services → platform`；CLI 也复用 services 与 platform，平台层和服务层不得导入 SDK。请求决策与认证 workflow 位于 `inspire.platform.web.transport_core`；`inspire.platform.web.transport` 持有调用方状态并执行同步驱动，`inspire.platform.web.transport_async` 提供原生异步驱动。兼容重导出及既有补丁入口保留在 transport 中。与传输共用的异常基础层位于 `inspire.platform.errors`，SDK 的 `exceptions` 重导出同一批类；携带 SDK 资源模型的工作负载失败异常仍由 SDK 定义。

两种前端共用请求决策程序和响应策略定义；同步与异步驱动分别提供 HTTP／浏览器 I/O，以策略分支保留异常类型和原始消息。SDK 与 CLI 的连接所有权、Referer、超时格式及请求体规则保持各自既有语义。CLI 的暂时 HTTP 状态仍是固定集合 `408/425/429/500/502/503/504`。重试循环显式保留两种策略的分支：CLI 刷新／浏览器回退不消耗暂时错误重试次数，SDK 按尝试次数与剩余截止时间计费；写请求发送后在进入这些分支前直接分类退出。

Session 层为传输提供公开的浏览器创建／获取／关闭、运行时错误报告、原地刷新与无凭据续期入口。刷新中的 `acquire_web_session` 与普通 `get_web_session` 有意区分：前者供已持有刷新锁的调用者使用，不触发前端 adoption；后者获取刷新锁并通知当前前端。CLI 刷新原地更新既有 WebSession，保留调用方持有的对象；SDK 刷新通过 adoption 接收新的 WebSession，并重新配置自己持有的 HTTP 连接。

browser_api 的控制台 v2 JSON 请求通过 `inspire.platform.web.runtime.get_transport(session).request(...)`，数据广场通过同一 Transport 的 `plaza_request(...)` workflow。控制台发送调用 `_dispatch`，广场及应用请求则在各自 workflow 中标记 `SharedState.dispatched()`；它们共用写入状态和结果分类，并非每次发送都调用 `_dispatch`。runtime 的 ContextVar 选择当前 Transport／CLI 会话接纳器；`inspire.platform.web.flow.async_call` 则在同一线程上把挂起的调用交给异步驱动，不在 CLI／SDK 两套请求实现间分流。

数据广场有独立主机和 CAS 服务票据握手，内部仍有 `PlazaClient`、`requests.Session` 与 `datasets-session` Cookie，但它们由所属 Transport 持有、按账号与会话代次隔离并负责关闭。SDK 没有另一套独立生命周期的数据广场客户端，也不把它的 Cookie 混入控制台 HTTP 会话。Cookie 被拒时先重做广场握手，再升级到平台会话续期；广场使用自己的信封解包和重试分类，不走浏览器请求回退。远程 exec 的 websocket／SSH 是独立协议，执行数据流不经过 JSON dispatcher；TensorBoard 应用读取与 Jupyter Contents 借用独立 Cookie jar，但其 HTTP 请求仍经过 Transport 的预算、续期和重试策略，见其门面说明。

## 账号与会话

本地账号管理可直接使用 `from inspire import Accounts`，无需先构造 Client；`InspireClient.accounts` 和 `InspireAsyncClient.accounts` 指向同一个同步类，均不需要 `await`。账号状态默认保存在 `~/.inspire/accounts/<alias>/`，没有仓库级配置层。

| API | 行为 |
|---|---|
| `Accounts.list()` | 返回排序后的账号别名元组 |
| `Accounts.current()` | 读取磁盘默认账号，忽略临时账号作用域；未设置时为 `None` |
| `Accounts.exists(name)` | 检查账号是否存在 |
| `Accounts.config_path(name)` | 返回账号配置的 `Path` |
| `Accounts.add(name, *, username, password, base_url="https://qz.sii.edu.cn", proxy=None, use=False, overwrite=False)` | 与 CLI 共用配置渲染；首个账号自动成为默认账号，其余仅在 `use=True` 时切换 |
| `Accounts.use(name)` | 切换磁盘默认账号 |
| `Accounts.rename(old, new)` | 重命名账号并迁移 Notebook 目标缓存 |
| `Accounts.remove(name)` | **立即删除账号及其缓存，不做确认** |

账号创建不打印、不提示、不联网、不启动浏览器；环境归一化仅检查本地 Chromium 文件是否存在。重复名称默认抛 `ValidationError`；`overwrite=True` 会替换整个账号目录，包括缓存。账号层错误转换为 `ValidationError` 并保留原消息。

```python
from inspire import Accounts, InspireClient

Accounts.add("research", username="login-name", password="password", use=False)
with InspireClient(account="research") as client:
    identity = client.login()
    result = client.init()
    print(result.config_path, result.changed, result.warnings)

# 也可以直接提供凭据；构造只准备本地配置，不联网登录。
with InspireClient(username="login-name", password="password", account="research") as client:
    identity = client.login()

# 可读性别名，行为相同：
client = InspireClient.from_credentials("login-name", "password", account="research")
client.close()
```

同步构造签名为 `InspireClient(account=None, *, username=None, password=None, base_url=None, proxy=None, allow_browser=False, timeout=30, operation_timeout=120, catalog_ttl=60, catalog_disk_cache=False)`。异步构造签名为 `InspireAsyncClient(account=None, *, username=None, password=None, base_url=None, proxy=None, allow_browser=False, timeout=30, operation_timeout=120, catalog_ttl=60, catalog_disk_cache=False, concurrency=None)`；同步构造时进行的本地配置工作，在异步客户端进入上下文或首次调用时才执行。不传凭据时使用现有账号，省略 account 使用当前账号。username/password 必须成对传入；提供凭据且省略 account 时，以 username 作为本地别名（须符合账号名称规则，邮箱等应显式提供合法 account）。缺失账号会创建；已有账号仅更新显式提供且不同的 auth/api/proxy 字段，保留其他配置。在凭据构造路径中，`proxy=None` 保留原代理，空字符串清空四个代理字段。不传 username/password 时，`base_url` 和 `proxy` 参数不覆盖现有账号配置，应先配置账号或使用凭据构造路径。构造客户端始终不改变默认账号指针，包括创建首个账号时。

`client.login(*, force=False) -> AccountInfo` 立即建立并验证会话：缓存未过期时复用它并查询当前用户，缺失或过期时调用现有 Transport 续期流程；`force=True` 主动进入续期流程。代码按会话创建时间和缓存 TTL 判断有效期（`SESSION_TTL=3600`，即 1 小时），不会因普通请求而延长本地有效期；这不证明服务器当前强制使用相同的失效时间。续期在账号刷新锁内先重读更新的磁盘缓存，再尝试 SSO Cookie 续期，最后执行受账号登录 guard 保护的 `login_without_browser`（requests CAS 凭据登录）。只有 `allow_browser=True` 才允许 Chromium 登录回退及浏览器请求通道。未允许浏览器时，验证码要求保留原提示并抛 `AuthenticationError`。允许浏览器且 HTTP 登录表单要求验证码时，会独立检查 Chromium 表单；Chromium 同样要求验证码时不填写或提交凭据。任何已提交凭据后的失败都不会再尝试另一条登录路径；冷却通过 `AuthenticationCooldownError.retry_at` 暴露。

`client.init(*, force=False) -> InitResult` 先登录，要求会话含真实可访问 workspace ID，然后按需原子写入账号配置，补齐平台地址和缺失的凭据；用户名可取会话中的登录身份。默认 `force=False` 仅合并这些配置字段，保留所有其他键值（包括未知节和旧表），仅在解析后的字典变化时写回；`force=True` 从账号模板和发现值重新构建，像 `inspire init --force` 一样丢弃旧表，并舍弃自定义节。返回 `config_path: Path`、按解析后字典比较的 `changed: bool` 和 `warnings: tuple[str, ...]`（当前实现为空元组）。它不写入仓库配置。**交互提示、Playwright 安装和 ssh-keygen 仍只由 CLI 提供**；SDK init 不执行这些步骤。CLI 的非交互 init 仍要求已有配置时显式指定 `--force`，其原有刷新语义保留。

导入 SDK 不加载 Click、Rich 或 Playwright。同步客户端在构造时固定账号、平台来源和配置，后续切换默认账号不影响它；异步客户端在首次初始化时固定账号。一个 `InspireClient` 只在创建它的进程和线程中使用；线程 worker 或 fork 子进程各自创建同步客户端，使用 `with` 或 `close()` 释放连接。`InspireAsyncClient` 在同一进程、同一事件循环的多个任务间共享，使用 `async with` 或 `await close()`。磁盘会话、刷新锁和登录冷却与 CLI 共用。`account_info` 查询平台信息；`api_keys.plaintext(ref)` 显式返回密钥值，其他密钥视图只包含元数据。

异步登录和初始化分别为 `await client.login(force=False) -> AccountInfo` 与 `await client.init(force=False) -> InitResult`，返回类型和行为与同步形式相同。`InspireAsyncClient.from_credentials(...)` 和构造函数均为同步工厂，无需 await；`Accounts` 会做本地文件操作，事件循环敏感的应用可在启动阶段调用它。

```python
# 放在 async def 内；也可以用 async with InspireAsyncClient(...)。
client = InspireAsyncClient.from_credentials(
    "login-name", "password", account="research",
)
try:
    identity = await client.login()
    result = await client.init()
    print(identity.alias, result.config_path, result.changed, result.warnings)
finally:
    await client.close()
```

## 并发、取消与生命周期

**并发模型。** 一个 `InspireAsyncClient` 可在同一进程、同一事件循环内被多个任务共享。直接 `asyncio.gather(client.jobs.get(a), client.jobs.get(b))` 即可重叠执行原生异步请求，无需设置并发参数。业务逻辑始终运行在调用方循环线程，不使用固定线程执行每个操作或同步客户端池。HTTP、CAS 认证、浏览器登录与请求、PTY／Jupyter websocket、SSH exec／SCP 子进程使用原生异步 I/O；轮询等待使用 `asyncio.sleep`。业务校验、解析、错误分类和生成器逻辑复用同步实现，通过同一线程上的栈切换在 I/O 边界挂起。同步 `InspireClient` 的进程／线程亲和性规则不变。

`concurrency` 已弃用：缺省为 `None`；显式传入正整数会发出 `DeprecationWarning`，参数不产生任何效果。保留它是为了让原有代码迁移时不立即失败；它既不是池大小，也不限制在途请求，原有 `concurrency=1` 不再使调用串行。非正整数仍在构造时抛 `ValidationError`。需要限制应用整体并发时，请由调用方使用 `asyncio.Semaphore`；账号认证锁、平台配额和限流继续生效。

构造函数不做 I/O；进入 `async with` 或首次调用时，在事件循环线程读取本地配置、固定账号，并仅保存一次显式凭据。账号、超时和缓存配置错误在此时抛出，完成后可读 `account` 和 `base_url`；提前读取抛 `ConfigurationError`，提示先完成首次操作或通过 `async with client` 显式初始化。每次操作使用独立的预算、写入状态和名称解析上下文，HTTP 连接池按异步客户端生命周期复用并在关闭客户端时释放；客户端共享目录缓存、已获取的认证快照及按会话代次匹配的数据广场登录槽。操作视图退出不关闭共享会话，广场借用竞争通过同一 workflow 调度等待。`await client.cache.clear()` 清空该缓存并保留计数，`await client.cache.stats()` 返回其 hits／misses／entries。`Accounts` 仍是同步账号管理 API。

异步路径的本地 I/O 按操作卸载到**客户端独占、最多 4 个 worker 的线程池**，不借用应用的默认 executor。线程按需启动并在调用间复用；固定 4 个 worker 为短时文件与证书处理提供适度并行，同时限制每客户端的线程开销，不随请求数或 CPU 数量增长。此池处理本地 I/O 和相关 CPU 工作，不承载桥接可达性等待；仍有一个引导例外：`_ensure_rtunnel_binary` 在 worker 中检查本地二进制，缺失或不可用时可能下载 rtunnel。原生网络并发不受池大小限制，4 的上限约束 worker 数量，不约束排队任务数量。卸载范围包括：目录缓存的完整锁／读／写／失效操作（包括 RAM 命中时的跨进程校验）、会话缓存、续期配置和账号读取、认证锁文件操作及登录保护的 PBKDF2、`init()` 配置读写、文件传输的检查／读取／暂存／发布、`output_to` 打开／写入／关闭。SSH 桥接候选选择、探测重试沿用共享 workflow，SSH 探测、exec 和 SCP 使用 asyncio 子进程，探测间隔使用异步等待。浏览器登录和同步登录执行相同的表单选择、提交顺序、认证轮询与错误分类，异步客户端使用 Playwright async API。

HTTP 请求准备中的配置读取与环境设置解析在工作线程执行。netrc 按客户端与目标 authority 首次读取后缓存，证书上下文按客户端与 TLS 配置缓存（包括 HTTPS 代理）；首次加载也在工作线程。更新 netrc 或原路径中的证书文件后，应重建客户端读取新值。Websocket 的代理解析和每次连接的证书加载也卸载到线程。HTTP DNS 由事件循环默认 executor 解析；本地实测同一客户端跨两次操作的 10 个顺序请求仅解析一次并复用一条连接。并发新连接、连接过期或重连仍可能再次解析；PTY DNS 同样使用默认 executor，未引入自定义 DNS 缓存。

**仍可能阻塞事件循环的部分：** 首次使用客户端时的本地构造／账号配置工作（每个客户端一次）；直接调用同步 `Accounts` API；调用方的同步回调、日志 handler；载荷解析、编码、深拷贝等 CPU 工作，以及少量路径处理、懒导入和进程内锁操作。这里不承诺整个 SDK 零阻塞；这些部分仍在调用方循环线程执行，不因引入本地 offload 池而迁移到 worker。

所有异步 exec 与 `exec_stream` 的 `on_output` 均接受同步函数或 `async def`，类型为 `Callable[[str], None | Awaitable[None]] | None`。两种回调都在调用方循环线程执行；异步回调按顺序 await 并施加背压，回调异常原样传播。同步回调仍直接调用，其耗时由调用方控制。同步客户端的回调签名与行为不变。

```python
async def on_output(chunk: str) -> None:
    await output_queue.put(chunk)

result = await client.notebooks.exec(notebook_ref, command="hostname", on_output=on_output)
```

`iter`、`follow_events`、`follow_logs`、`exec_stream` 和 `wait()` 不长期占用 worker；只有其中的本地 I/O 片段使用 offload 池。可以在流循环体内 await 同一客户端的普通请求或缓存操作，无需为轮询和流预留 worker。流的交付队列最多暂存一项并施加背压；分页当前页、去重集合和 exec 捕获仍会占内存，尤其 follow 的去重集合可能随观察历史增长。提前退出流使用 `contextlib.aclosing`，单独 `break` 不保证立即关闭生成器和活动连接。

`operation_timeout` 从操作开始时计算，不包含首次本地初始化；并发操作各自持有预算。Ray／Serving 的一次 `status()` 共用一个操作截止时间，并最多同时读取 **8** 个引用，以免数百个引用同时打开数百条连接。结果及重复引用按输入顺序返回；多个引用失败时，按输入顺序抛出第一个错误，保留同步路径的错误类型和消息，并取消剩余读取。此限制只适用于单次 `status()`；同步客户端继续串行读取。

**取消与关闭。** 取消普通调用或等待下一项的任务，会取消正在等待的原生 HTTP／websocket I/O 和轮询等待，并执行原有 finally 清理，不必等阻塞线程返回或等完轮询间隔。可用 `asyncio.wait_for` 限制调用方等待。SSH／SCP 在取消、超时和回调失败时终止并回收本地子进程；浏览器登录取消会退出 Playwright 上下文。已开始的本地文件／锁操作不能被 asyncio 强制终止，会等待当前操作和必要清理结束后传播取消，因此慢磁盘或目录锁竞争仍可能延迟取消完成，但不占住事件循环。SSH 传输继续执行共享流程中的暂存清理。

取消不证明远端命令已停止，也不撤销已发送写入，更不会重放请求。SDK 异常的类型、消息和异常实例仍原样传播。`async with` 退出及 `await client.close()` 会取消活跃的原生操作与流、关闭连接，取消尚未开始的排队 offload，并等待正在运行的本地工作结束，最终关闭并 join 客户端线程池。等待期间事件循环仍能运行；关闭返回时本客户端 worker 已退出，应用的默认 executor 保持原样。关闭任务被多次取消时，清理仍会完成。线程不能被强制中断：普通请求准备等 offload 的等待可立即取消，但已开始的线程工作会继续，慢磁盘、锁或阻塞的同步输出 sink 可能延迟最终关闭；需要完整文件清理的 offload 仍保持上述延迟传播取消的语义。应显式管理客户端生命周期。关闭后再次调用抛 `ClientClosedError`。

## 资源引用与分页

集合接口为 `list(workspace, *, ...filters, limit=20, cursor=None)`、`iter(workspace, *, ...filters, max_items=None)` 和 `quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)`；无工作区的目录保留其首个选择参数或无参数。以工作区为主要操作对象的方法（包括 `resources.availability/policy/usage`、`account_info.permissions` 和 `servings.configs`）将 `workspace` 作为首参数，接受位置或关键字传入，其余参数仅接受关键字；`account_info.permissions(workspace=None)` 的工作区可省略。其他方法中的工作区筛选参数仅接受关键字。读取或变更已有单资源时通常以 `ref` 为首参数；创建以 `spec`、注册以 `name` 为首参数，数据集申请与验证分别使用 `name` 和 `specs`，完整签名见方法表。批量 `status(refs, *, workspace=None)` 接受名称或类型化引用的序列，按输入顺序返回元组，空序列返回空元组。

训练 Jobs 和 HPC 的 `status()` 在解析引用后通过平台批量 Action 查询，按工作区分组，每 20 个引用一次请求（重复 ID 在分块前去重）。同步 Ray、Serving 和 Notebook 逐个引用读取；异步 Ray／Serving 按最多 8 个在途读取并发执行，Notebook 保持逐个读取。两种路径返回的资源对象与 `get()` 完全相同，包括 `raw` 和 `view`；输入顺序和重复引用均保留。名称选择仍需先进行名称解析。

`Page` 提供 `items`、`next_cursor`、`total`，通过相同过滤条件与 `cursor=page.next_cursor` 继续。Jobs、Notebook、HPC、Ray、Serving 的列表采用服务端分页，按需取页直到收集到 `limit` 项或目录结束；游标记录平台行偏移，并绑定账号、门面及查询条件，不是平台快照。迭代器沿游标继续并对身份去重，`max_items` 控制产出数。Notebook、HPC、Ray、Serving 列表未应用本地过滤时，`total` 使用平台报告的总数；应用本地状态或关键词过滤时为 `None`，平台未提供可靠总数时也为 `None`。其他目录（包括 TensorBoard）仍可能先有界枚举再做本地分页。

Jobs、Notebook 的请求页大小固定为 100，HPC、Ray、Serving 分别为 50、20、20；不会按总数扩大请求。上述工作负载列表和名称扫描使用 SDK 的 `page_num × page_size ≤ 5000` 防护，越界请求发出前就抛 `ResolutionIncompleteError`，提示用 `status`/`keyword` 收窄或用 `max_items` 截止。这一代码防护不适用于所有目录，也不证明所有平台端点当前具有同样上限。缺页、重复页或单次扫描超过 100 页时同样抛 `ResolutionIncompleteError`。Serving、Notebook、TensorBoard 名称解析先发送 `keyword` 再精确匹配；当前 HPC/Ray ListJobs 合同不支持关键词过滤，名称解析最多扫描 100 页，无法确认唯一性时抛 `ResolutionIncompleteError`，可使用已有类型化引用直接查询。

名称按完整名称消歧，多个候选抛 `AmbiguousResourceError`，`.candidates` 中是资源模型对象，元素类型随门面变化（例如 jobs 为 `Job`、images 为 `Image`、models 为 `ModelInfo`），不是 Ref。选定候选后读取 `candidate.ref` 再交给同一门面；这样可以按工作区、来源或状态选择，而不必把名称再次交给消歧器。名称查询工作负载、计算组、镜像和模型时显式给 workspace；已有类型化 Ref 可省略。Ref 校验类型、账号、来源以及显式工作区；`.to_dict()` / `XRef.from_dict()` 可用于保存和恢复，不能跨账号套用。项目目录默认是全局范围；模型 list 和账号 permissions 支持 `workspace="all"`，resources 查询只接受单工作区。

```python
workspace = client.workspaces.get("工作区名称")
page = client.jobs.list(workspace.ref, limit=20)
if page.next_cursor:
    next_page = client.jobs.list(workspace.ref, cursor=page.next_cursor)
statuses = client.jobs.status([job.ref for job in page.items])
```

异步分页使用相同的 Page 和游标，在 `async def` 中：

```python
workspace = await client.workspaces.get("工作区名称")
page = await client.jobs.list(workspace.ref, limit=20)
if page.next_cursor:
    next_page = await client.jobs.list(workspace.ref, cursor=page.next_cursor)
statuses = await client.jobs.status([job.ref for job in page.items])
```

资源结果通常为 frozen dataclass，嵌套字典并非递归冻结；`MetricGroup` 是可变 dataclass，`JobEvent` 是 `dict[str, Any]` 类型别名。提供业务视图的对象可用 `.to_dict()` 取得映射，但并非每个导出类型都有此方法（例如 `ExecResult`、`TransferResult`、`MetricGroup`）；Ref 的 `.to_dict()` 则是引用序列化格式。镜像 `ImageSelector(name, source)` 可指定 official/public/project/private，跨来源同名会报歧义。镜像 list 可返回成功来源的目录，名称 get/detail 要求完整候选集。数据集 get 接受 code 或 DatasetRef，validate 接受 `"name:version"` 或 DatasetMount；applications 的单数据集扫描有界，不保证窗口之外的申请历史。

创建工作负载返回提交句柄（Handle），其中 `.ref` 是可持久化的纯数据身份。同步客户端返回 `JobHandle` 等原有类型，继续使用 `client.jobs.wait(handle.ref)` 或对应门面的等待方法；直接 await 同步句柄会报错并提示改用 `InspireAsyncClient`。异步客户端返回显式导出的 `AsyncJobHandle` 等子类，保留提交结果字段，并绑定产生它的异步门面：

| 异步提交 | 句柄类型 | `await handle` 等价调用 |
|---|---|---|
| `jobs.create` | `AsyncJobHandle` | `await client.jobs.wait(handle.ref)` |
| `hpc.create` | `AsyncHPCJobHandle` | `await client.hpc.wait(handle.ref)` |
| `ray.create` | `AsyncRayJobHandle` | `await client.ray.wait(handle.ref)` |
| `servings.create` | `AsyncServingHandle` | `await client.servings.wait(handle.ref)` |
| `tensorboards.create` | `AsyncTensorboardHandle` | `await client.tensorboards.wait(handle.ref)` |
| `notebooks.create` | `AsyncNotebookHandle` | `await client.notebooks.wait(handle.ref)` |
| `notebooks.save_image` | `AsyncImageSaveHandle` | `await client.notebooks.wait_image_ready(handle)` |
| `images.register` | `AsyncImageRegisterHandle` | `await client.images.wait_ready(handle.ref)` |

`models.register` 仍返回 `ModelRegisterHandle`，没有等待语义。以下三种写法在 `async def` 内并列使用；`handles` 是已经提交的异步句柄列表：

```python
finished = await handle                              # 门面的默认参数
finished = await handle.wait(timeout=7200, raise_on_failure=True)
finished_all = await asyncio.gather(*handles)         # 并发等待全部句柄
```

`handle.wait()` 是协程方法，接受对应门面 wait 的全部关键字参数，默认值及返回对象完全相同；例如 Notebook 支持 `target`，镜像等待仅支持对应的 timeout／poll_interval 等参数，不额外添加 `raise_on_failure`。

**取消等待只会停止轮询，不会停止远端工作负载；任务继续运行，也可能继续计费。** `await handle`、`handle.wait()`、`future()` 的取消均如此。要释放计算资源，显式调用 `await client.jobs.stop(handle.ref)` 或对应工作负载门面的 `stop()`；句柄没有 `cancel()` 方法。镜像句柄仅观察 readiness：保存镜像的取消需单独调用 `notebooks.cancel_save_image(handle.notebook)`，`Images` 没有 `stop()`；镜像等待也没有 `raise_on_failure` 参数，而是沿用镜像 readiness 接口的错误合同。

等待会轮询平台，不是订阅，也不是零成本操作；它沿用门面 wait 的请求预算、总等待超时和会话续期规则。可以反复 await 同一句柄，每次重新读取状态并等待；已处于对应终态／目标状态时，下一次读取后即可返回，不缓存上次结果。工作负载的默认 `raise_on_failure=False` 意味着**失败时返回失败对象而非抛异常**，这一点与通常用 Future 表示失败的习惯不同；设置 True 才抛对应 SDK 失败异常。镜像等待继续沿用其门面自己的异常规则。

Python 3.11 起 `asyncio.wait` 只接受 Task／Future；使用 `future()` 将句柄的默认等待立即调度为当前事件循环上的 `asyncio.Future`（实际为 Task）：

```python
futures = [handle.future() for handle in handles]
done, pending = await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED)
for ready in asyncio.as_completed(futures):
    result = await ready
```

每次 `future()` 都创建一次新的等待，不共享或缓存 Future；取消其中一个不会取消同一句柄的另一次独立等待。它需要正在运行的事件循环，返回的是 asyncio Future，不是跨线程的 `concurrent.futures.Future`。单纯达到 `asyncio.wait(..., timeout=...)` 的时限不会自动取消 pending Future，调用方负责剩余等待任务的生命周期。

Ref 仍 frozen、可序列化且不持有客户端。恢复时使用异步门面的 `bind_ref`，它只初始化本地客户端并校验引用类型、账号和来源，不请求平台、不创建资源：

```python
from inspire import JobRef

ref = JobRef.from_dict(saved_ref_dict)
handle = await client.jobs.bind_ref(ref)
finished = await handle
```

Jobs、HPC、Ray、Servings、Tensorboards、Notebooks 和 Images 都支持 `await client.<facade>.bind_ref(ref)`。Notebook 镜像保存可用 `await client.notebooks.bind_image_ref(image_ref, notebook=notebook_ref)` 恢复，并校验两者工作区一致；只保存了 ImageRef 时也可直接使用 `await client.images.bind_ref(image_ref)`。恢复句柄没有原始提交元数据：`operation_id` 为空字符串，其他非身份字段采用默认值；状态须等待后读取，不能把句柄默认值当作平台快照。保存镜像若暂时没有 Ref，await 仍会明确报身份不可用，需先在目录中解析；不会重新保存镜像。

### 缓存

SDK 与 CLI 的持久化身份及配额缓存共用 `inspire.services.catalog.resource_index.ResourceIndex`，文件仍为账号目录内的 `resource-index.sqlite3`。共享的单 scope 刷新函数 `inspire.services.catalog.resource_refresh.refresh_scope` 负责刷新判断、租约、快照代次检查、完整扫描 reconciliation、部分结果合并和错误记录；SDK 没有另一套 SQLite 写入或刷新循环。CLI 和 SDK 均直接导入实现模块，不保留旧路径转发壳。

`catalog_disk_cache=False` 默认值保持不变：只使用每客户端的短期内存快照，不读写共享目录；显式 `True` 开启共享索引和下面的小型元数据缓存。每个子进程仍独立创建 Client。`catalog_ttl=60` 继续限制 SDK 读取快照的最大年龄，`0` 禁用缓存读取和填充。共享身份行的写入 TTL 和自动刷新周期始终使用 CLI 的分类型默认值（身份通常一天，镜像和配额一周）；SDK 的较短读取期限不会缩短 CLI 行的寿命，也不会仅因读取期限较短而提早触发共享 scope 刷新；唯一的 payload 修复例外是 scope 仍新鲜却缺少可用的完整目录字段，此时允许在租约内补齐。超过 SDK 读取期限、但尚未达到共享刷新周期时，SDK 直接实时读取，不发布另一套定时刷新结果。因此较长 TTL 的另一个 SDK 客户端仍可读取共享身份；小型元数据条目的有效期仍取写者和读者 TTL 的较短者。这两个构造参数仍有实际用途，未弃用。

| 原 SDK 缓存种类 | 现在的持久化位置及范围 |
|---|---|
| `workspaces` | 共享 `workspace` scope，账号主体／服务器全局；每行 payload 保存完整路由投影 |
| `projects` | 共享 `project` scope；全局查询沿用全局范围，按工作区过滤的目录用独立 owner scope，避免把过滤结果误当完整全局目录并删除其他项目 |
| `compute_groups` | 与 CLI 相同的工作区 `compute-group` scope |
| `images` | 共享 `image` 类型，按工作区／来源建立候选 scope；单来源扫描不能替代 CLI 的跨来源完整范围 |
| `prices` | 与 CLI 相同的工作区／工作负载 `quota-*` scope；通过共享配额 loader／完整计算组 fan-out 取价格，规格删除使用同一墓碑规则 |
| `priority_levels` | 小型 SDK JSON 缓存；优先级菜单是调度配置，不是身份映射 |
| `fair_scheduling` | 小型 SDK JSON 缓存；布尔能力标记不建立虚构身份行 |
| `current_user` | 小型 SDK JSON 缓存；当前用户详情不是资源名称候选集 |

`sdk-catalog-v1.json` 仅保留后三类元数据，打开时清理旧版遗留的身份／价格 blob；其账号／服务器隔离、固定类型白名单、256 项／8 MiB 上限、锁和原子替换规则保留。共享身份行的 payload 使用固定平台模型白名单，不使用 pickle。CLI 行只有名称和 ID、缺少 SDK 所需字段或 payload 无法解码时，SDK 拒绝整份 scope，并携带“需要补齐 payload”的原因绕过 CLI 的 freshness 判断，在共享租约内重新读取完整目录并发布。取得租约后再次检查可用快照，避免其他 SDK 已修复后重复联网；成功后的下一次调用命中缓存。后续 CLI identity-only 写入最多再次触发一次成功补齐，失败／不完整枚举不会被伪造为空目录，busy 时只读不发布。SDK 不从身份行推断项目权限、计算组能力或镜像来源。

```python
with InspireClient("my-account", catalog_ttl=60, catalog_disk_cache=True) as client:
    client.workspaces.get("Workspace")
    print(client.cache.stats())
    # {"hits": ..., "shared_hits": ..., "misses": ..., "entries": ...}
    client.cache.clear()
```

名称快路径仅接受完整、新鲜且能证明候选范围一致的索引快照，使用 SDK 的 Python `casefold()` 精确比较并检查全部候选；不能用 SQLite `NOCASE` 代替 Unicode 消歧。工作负载名称命中仍核验实时详情，重命名／删除后回退原有有界扫描。未命中、歧义、过期、身份不稳定或损坏时也回退；仍保留子串不能匹配完整名称的规则、5000 行保护和 `ResolutionIncompleteError`。CLI 跨来源镜像索引没有完整 SDK 来源证据时不会绕过 SDK 的来源消歧。工作负载列表、状态、日志、事件、指标和实时用量不从身份索引构造。

共享读写使用同一个 PID 租约及心跳、generation／revision 检查和墓碑实现；租约竞争的 SDK 可以实时读取，但不能把该读取发布进其他进程持有的 scope。初始化与损坏重建使用同一个稳定文件锁，防止并发打开者重复删除刚修复的数据库；普通锁竞争、只读文件及 I/O 错误不触发损坏删除。SQLite 的文件操作和租约进入／退出在异步客户端的本地 I/O 池运行，网络加载器继续使用调用方的同步或异步驱动。

CLI 与 SDK 的共享刷新仅在完整 scope 成功发布后清理超过 7 天的墓碑；完整工作区目录刷新还按原有 generation／revision 检查清理不可见工作区的 scope，维护失败不影响已发布快照。普通刷新受 scope TTL 限制，显式强制刷新仍可提前执行。新索引启用 SQLite incremental auto-vacuum，实际删除墓碑后请求最多 256 页的增量回收；旧的非增量数据库保持原格式，空闲页供后续写入复用，不自动运行完整 VACUUM 或重写文件。`inspire cache status` 按资源身份去重统计 `cached_names`、按工作区 ID 去重统计 `workspaces`，并显示 `scopes` 数量和 `size_bytes`（包含 SQLite sidecar）。

`hits` 表示本客户端仍有效且代次一致的快照命中，`shared_hits` 表示从共享存储取得的快照，`misses` 表示调用加载器，`entries` 为本客户端的内存条目数。共享身份每次读取都重新检查数据库，不用未经校验的旧内存绕过跨进程失效。未开启共享时仍返回原来的 hits／misses／entries 三键。`clear()` 保留累计计数；开启共享时清理当前账号／服务器的身份目录、配额和 SDK 元数据，CLI 下次查询也会看到该失效。

SDK 镜像写入仍在写前及操作结束后（包括失败路径）使跨工作区镜像目录失效，并同时清理共享索引中的镜像 scope。未开启共享的 SDK 写者也会失效已存在的共享存储，但不会为失效新建存储。失效失败不会被静默当成成功，也不会重放平台写入。会话续期不清空缓存；Notebook 目标缓存不参与此次迁移。

## 观察结果类型

以下九个方法返回类型化观察模型（或包含它们的 tuple／Page），模型均为 frozen dataclass；所有类型均从 `inspire.sdk` 和 `inspire` 导出。序列字段使用元组，`.to_dict()` 返回转换前的共享业务映射，保留条件键的缺省状态；可选标量字段在对象上为 `None`，可选序列为相应的空元组，并不会因此在映射中补键。

| 方法 | 返回类型 |
|---|---|
| `servings.versions` | `tuple[ServingVersion, ...]` |
| `servings.scale_history` | `Page[ServingScaleHistoryEntry]` |
| `servings.configs` | `ServingConfigs`，含 `tuple[ServingConfigItem, ...]` |
| `servings.api` | `ServingInvocationInfo`，继承 `ServingInvocationCredentials` 的扁平字段 |
| `servings.api_metrics` | `ServingAPIMetrics`，含 `ServingAPIMetricTimeRange` 和 `tuple[ServingAPIMetricSeries, ...]` |
| `tensorboards.tags` | `TensorboardTags` |
| `tensorboards.scalars` | `TensorboardScalars`，含 `tuple[TensorboardScalarSeries, ...]` 与 `tuple[TensorboardScalarPoint, ...]` |
| `ray.scaling` | `tuple[RayScalingEvent, ...]` |
| `notebooks.lifecycle` | `tuple[NotebookRun, ...]` |

Serving 调用信息沿用 `credential_env`、`auth_header`、`auth_scheme`、`affinity_header` 字段，不获取密钥。配置的 `auto_stop` 是可选布尔值，配置项的 `auto_stop_rules` 保留服务返回的规则字符串；API 指标系列只有摘要，不添加原始点集。TensorBoard 标量点通过 `.step`、`.value` 读取，`.to_list()` 返回原有 `[step, value]`；外层 `.to_dict()` 将序列还原为列表。`scalar_tags` 按运行名称映射到标签元组。

```python
for version in client.servings.versions(serving_ref):
    print(version.version, version.status)
for series in client.tensorboards.scalars(board_ref, points=10).series:
    print(series.run, series.tag, series.last_value)
    for point in series.points:
        print(point.step, point.value)
```

异步形式同样返回这些类型化模型；例如在 `async def` 中：

```python
for version in await client.servings.versions(serving_ref):
    print(version.version, version.status)
scalars = await client.tensorboards.scalars(board_ref, points=10)
for series in scalars.series:
    for point in series.points:
        print(point.step, point.value)
```

## 各门面方法表

两种客户端均有 15 个资源门面；资源门面共有 155 个同步方法、168 个异步方法（多出的 13 个是 5 个 exec_stream、7 个 bind_ref 和 1 个 bind_image_ref）。另有 cache 的 2 个方法，因此全部实例门面合计 157／170 个方法。计数通过真实客户端的门面类内省取得，包含继承的公开方法与异步引用绑定方法，不包含客户端自身的 login/init/close/from_credentials、属性、上下文协议或共享的 Accounts 类；测试同时核对逐门面表、总数和异步差额，新增方法时必须一起更新说明。

| 门面 | 同步方法数 | 异步方法数 |
|---|---:|---:|
| `cache` | 2 | 2 |
| `workspaces` | 2 | 2 |
| `projects` | 4 | 4 |
| `compute_groups` | 2 | 2 |
| `images` | 7 | 8 |
| `jobs` | 19 | 21 |
| `hpc` | 17 | 19 |
| `ray` | 19 | 21 |
| `servings` | 25 | 27 |
| `tensorboards` | 11 | 12 |
| `notebooks` | 23 | 26 |
| `account_info` | 4 | 4 |
| `api_keys` | 5 | 5 |
| `datasets` | 5 | 5 |
| `models` | 8 | 8 |
| `resources` | 4 | 4 |

以下参数签名省略类型注解，返回类型单列；`*` 后参数必须以关键字传入。CLI 栏表示对应平台能力，名称解析、迭代及等待可以组合一个 CLI 子命令的能力；不会启动 CLI 子进程。

### workspaces

同步用 `client.workspaces.方法(...)`；异步用 `await client.workspaces.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `workspaces.get(ref)` | `Resource[WorkspaceRef]` | `config context / account context（名称选择）` |
| `workspaces.list(*, limit=20, cursor=None)` | `Page[Resource[WorkspaceRef]]` | `config context / account context` |

### projects

同步用 `client.projects.方法(...)`；异步用 `await client.projects.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `projects.detail(ref, *, workspace=None)` | `ProjectDetail` | `project detail` |
| `projects.get(ref, *, workspace=None)` | `ProjectInfo` | `project list（名称选择）` |
| `projects.list(workspace=None, *, limit=20, cursor=None)` | `Page[ProjectInfo]` | `project list` |
| `projects.owners()` | `tuple[ProjectOwner, ...]` | `project owners` |

### compute_groups

同步用 `client.compute_groups.方法(...)`；异步用 `await client.compute_groups.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `compute_groups.get(ref, *, workspace=None)` | `Resource[ComputeGroupRef]` | `account context（计算组目录）（名称选择）` |
| `compute_groups.list(workspace, *, limit=20, cursor=None)` | `Page[Resource[ComputeGroupRef]]` | `account context（计算组目录）` |

### images

同步用 `client.images.方法(...)`；异步用 `await client.images.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `images.delete(ref, *, workspace=None)` | `None` | `image delete` |
| `images.detail(ref, *, workspace=None)` | `ImageDetail` | `image detail` |
| `images.get(ref, *, workspace=None)` | `Image` | `image list（名称选择）` |
| `images.list(workspace, *, source=None, keyword=None, limit=20, cursor=None)` | `Page[Image]` | `image list` |
| `images.register(name, *, workspace, version=None, description=None, visibility=None, operation_id=None)` | `ImageRegisterHandle` | `image register` |
| `images.set_visibility(ref, *, visibility, workspace=None)` | `None` | `image set-visibility` |
| `images.wait_ready(ref, *, timeout=600, poll_interval=5, workspace=None)` | `CustomImageInfo` | `image register --wait` |

### datasets

同步用 `client.datasets.方法(...)`；异步用 `await client.datasets.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `datasets.applications(name=None, *, to_approve=False, keyword=None, limit=20, cursor=None)` | `Page[DatasetApplication]` | `dataset applications` |
| `datasets.get(ref)` | `DatasetDetail` | `dataset show` |
| `datasets.list(keyword=None, *, tag=None, limit=20, cursor=None)` | `Page[DatasetInfo]` | `dataset list` |
| `datasets.tags()` | `tuple[DatasetTag, ...]` | `dataset tags` |
| `datasets.validate(specs, *, workspace)` | `tuple[DatasetValidation, ...]` | `dataset validate` |

### models

同步用 `client.models.方法(...)`；异步用 `await client.models.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `models.delete(ref, *, force=False, workspace=None, project=None)` | `None` | `model delete` |
| `models.deploy_config(ref, *, workspace=None, project=None, version=None)` | `ModelDeployConfig` | `model deploy-config` |
| `models.get(ref, *, workspace=None, project=None)` | `ModelInfo` | `model list（名称选择）` |
| `models.list(workspace, *, project=None, keyword=None, limit=20, cursor=None)` | `Page[ModelInfo]` | `model list` |
| `models.register(name, *, source_path, workspace, project, type=None, tag=None, description=None, operation_id=None)` | `ModelRegisterHandle` | `model register` |
| `models.status(refs, *, workspace=None, project=None)` | `tuple[ModelInfo, ...]` | `model list（逐引用状态）` |
| `models.detail(ref, *, workspace=None, project=None)` | `ModelStatus` | `model status` |
| `models.versions(ref, *, workspace=None, project=None)` | `tuple[ModelVersion, ...]` | `model versions` |

### resources

同步用 `client.resources.方法(...)`；异步用 `await client.resources.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `resources.availability(workspace, *, group=None, include_cpu=False)` | `tuple[ResourceAvailability, ...]` | `resources availability` |
| `resources.node_events(nodes, *, since=None, type=None, reason=None, limit=None, from_component=None)` | `EventResult` | `resources node-events` |
| `resources.policy(workspace, *, workload=None)` | `tuple[WorkloadSchedulePolicy, ...]` | `resources policy` |
| `resources.usage(workspace, *, project=None, user=None, task=None, group=None, mine=False, details=False, limit=None)` | `ResourceUsage` | `resources usage` |

### account_info

同步用 `client.account_info.方法(...)`；异步用 `await client.account_info.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `account_info.check()` | `AccountCheck` | `account check` |
| `account_info.context(*, limit=None)` | `AccountContext` | `account context` |
| `account_info.current()` | `AccountInfo` | `account current` |
| `account_info.permissions(workspace=None)` | `tuple[Permission, ...]` | `account permissions` |

### api_keys

同步用 `client.api_keys.方法(...)`；异步用 `await client.api_keys.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `api_keys.create(name)` | `APIKeyInfo` | `account api-key create` |
| `api_keys.delete(ref)` | `None` | `account api-key delete` |
| `api_keys.get(ref)` | `APIKeyInfo` | `account api-key list（名称选择）` |
| `api_keys.list(*, limit=20, cursor=None)` | `Page[APIKeyInfo]` | `account api-key list` |
| `api_keys.plaintext(ref)` | `str` | `account api-key export（只返回明文，不导出文件）` |

### jobs

同步用 `client.jobs.方法(...)`；异步用 `await client.jobs.方法(...)`，本节全部参数、默认值与返回模型相同。例外：`follow_events`、`follow_logs`、`iter` 同步返回迭代器，异步直接用 `async for item in client.jobs.follow_events(...)`，不用先 await；提前退出时用 `contextlib.aclosing`。异步另有 `client.jobs.exec_stream(...) -> AsyncIterator[str]`，参数及默认值与下表 `exec` 完全相同，直接 async for；只交付字符串块，不返回最终 ExecResult。完整输出、回调与关闭规则见[远程执行](#远程执行exec)。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `jobs.command(ref, *, workspace=None)` | `str` | `job command` |
| `jobs.create(spec, *, operation_id=None)` | `JobHandle` | `job create` |
| `jobs.delete(ref, *, workspace=None)` | `None` | `job delete` |
| `jobs.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `job events` |
| `jobs.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `job shell（非交互执行）` |
| `jobs.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]／AsyncIterator[EventResult]` | `job events --follow` |
| `jobs.follow_logs(ref, *, interval=2, **filters)` | `Iterator[LogResult]／AsyncIterator[LogResult]` | `job logs --follow` |
| `jobs.get(ref, *, workspace=None)` | `Job` | `job status` |
| `jobs.instance_names(ref, *, workspace=None)` | `tuple[str, ...]` | `job instances` |
| `jobs.instances(ref, *, workspace=None)` | `tuple[Instance, ...]` | `job instances` |
| `jobs.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[Job]／AsyncIterator[Job]` | `job list --all` |
| `jobs.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[Job]` | `job list` |
| `jobs.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=100, max_chars=None)` | `LogResult` | `job logs` |
| `jobs.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval='1m', group=None)` | `tuple[MetricGroup, ...]` | `job metrics` |
| `jobs.plan(spec)` | `JobPlan` | `job create --dry-run` |
| `jobs.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `job quota` |
| `jobs.status(refs, *, workspace=None)` | `tuple[Job, ...]` | `job status` |
| `jobs.stop(ref, *, workspace=None)` | `None` | `job stop` |
| `jobs.wait(ref, *, workspace=None, timeout=3600, poll_interval=10, raise_on_failure=False)` | `Job` | `job wait` |

与 HPC、Ray、Serving 一样，训练任务 `Job.raw` 保留平台载荷，`Job.view` 是稳定的公开投影，`Job.to_dict()` 返回 `view` 的浅拷贝。`jobs.get()` 的 `view` 与同一详情载荷的 `inspire job status --json` 业务字段一致；`list()` / `iter()` 和批量 `status()` 也提供 `raw` / `view`，字段取决于平台记录。计算组、资源、节点与优先级等公开字段可从 `view` 读取；镜像等未进入公开投影的详情字段可从 `raw` 读取。

四个门面的 `instances()` 都返回 SDK 自己的冻结 `Instance`；CLI 的服务层视图保持独立。`label` 是 CLI 实例表里的可读名称／角色加 Rank，`instance_names()` 在四处都返回这些 label。`handle` 是平台需要的 namespaced 身份，清洗原始 ID 会把它变成噪声，因此只用于调用，绝不能打印；`repr` 也隐藏 handle、pod 和 raw。视图还保留各类实例实际具有的 status、node、role、rank、kind、pod，以及未经投影的 raw 行；缺少的文本字段为空串，rank 沿用服务层或列表位置，raw 字典与其他 SDK 原始载荷一样不递归冻结。四处 `exec(instance=...)` 与 `logs(instance=...)` 均可直接接收 label 或 handle，建议应用传 label，这样人读到的实例和程序选中的实例是同一个；exec 还要求选中唯一的运行中实例，logs 可选多实例。角色等 CLI 选择别名仍可用。

`jobs.logs()` 的默认时间范围及显式 `window` 最长为 30 天：保留结束时间并向后移动开始时间；CLI `job logs` 复用同一截断逻辑。

### notebooks

同步用 `client.notebooks.方法(...)`；异步用 `await client.notebooks.方法(...)`，本节全部参数、默认值与返回模型相同。例外：`follow_events`、`iter` 同步返回迭代器，异步直接用 `async for item in client.notebooks.follow_events(...)`，不用先 await；提前退出时用 `contextlib.aclosing`。异步另有 `client.notebooks.exec_stream(...) -> AsyncIterator[str]`，参数及默认值与下表 `exec` 完全相同，直接 async for；只交付字符串块，不返回最终 ExecResult。完整输出、回调与关闭规则见[远程执行](#远程执行exec)。`lifecycle` 返回 `tuple[NotebookRun, ...]`，`metrics` 与 `realtime_metrics` 是不同接口。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `notebooks.cancel_save_image(ref, *, workspace=None)` | `bool` | `notebook cancel-save-image` |
| `notebooks.create(spec, *, operation_id=None)` | `NotebookHandle` | `notebook create` |
| `notebooks.delete(ref, *, workspace=None)` | `None` | `notebook delete` |
| `notebooks.estimate_image_size(ref, *, workspace=None)` | `NotebookImageSizeEstimate` | `notebook save-image --dry-run` |
| `notebooks.events(ref, *, keyword=None, limit=100, workspace=None)` | `EventResult` | `notebook events` |
| `notebooks.exec(ref, *, command, workspace=None, cwd=None, env=None, timeout=120, transport='auto', on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `notebook exec` |
| `notebooks.upload(ref, *, local, remote, workspace=None, transport='auto', recursive=False, overwrite=True, timeout=120, max_bytes=16777216)` | `TransferResult` | `notebook scp（另提供 Jupyter Contents API）` |
| `notebooks.download(ref, *, local, remote, workspace=None, transport='auto', recursive=False, overwrite=True, timeout=120, max_bytes=16777216)` | `TransferResult` | `notebook scp（另提供 Jupyter Contents API）` |
| `notebooks.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]／AsyncIterator[EventResult]` | `notebook events --follow` |
| `notebooks.get(ref, *, workspace=None)` | `Notebook` | `notebook status` |
| `notebooks.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[Notebook]／AsyncIterator[Notebook]` | `notebook list --all` |
| `notebooks.lifecycle(ref, *, limit=None, workspace=None)` | `tuple[NotebookRun, ...]` | `notebook lifecycle` |
| `notebooks.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[Notebook]` | `notebook list` |
| `notebooks.metrics(ref, *, metric='core', window='1h', start=None, end=None, interval=None, group=None, workspace=None)` | `tuple[MetricGroup, ...]` | `notebook metrics` |
| `notebooks.plan(spec)` | `NotebookPlan` | `notebook create --dry-run` |
| `notebooks.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `notebook quota` |
| `notebooks.realtime_metrics(ref, *, workspace=None)` | `tuple[NotebookResourceSnapshot, ...]` | `notebook metrics --now` |
| `notebooks.save_image(ref, *, name, version=None, description=None, visibility=None, flatten=False, workspace=None)` | `ImageSaveHandle` | `notebook save-image` |
| `notebooks.start(ref, *, workspace=None)` | `None` | `notebook start` |
| `notebooks.status(refs, *, workspace=None)` | `tuple[Notebook, ...]` | `notebook status` |
| `notebooks.stop(ref, *, workspace=None)` | `None` | `notebook stop` |
| `notebooks.wait(ref, *, timeout=600, poll_interval=5, target='RUNNING', raise_on_failure=False, workspace=None)` | `Notebook` | `notebook create --wait / status（轮询）` |
| `notebooks.wait_image_ready(ref, *, timeout=600, poll_interval=5, workspace=None)` | `CustomImageInfo` | `notebook save-image --wait` |

### hpc

同步用 `client.hpc.方法(...)`；异步用 `await client.hpc.方法(...)`，本节全部参数、默认值与返回模型相同。例外：`follow_events`、`iter` 同步返回迭代器，异步直接用 `async for item in client.hpc.follow_events(...)`，不用先 await；提前退出时用 `contextlib.aclosing`。异步另有 `client.hpc.exec_stream(...) -> AsyncIterator[str]`，参数及默认值与下表 `exec` 完全相同，直接 async for；只交付字符串块，不返回最终 ExecResult。完整输出、回调与关闭规则见[远程执行](#远程执行exec)。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `hpc.create(spec, *, operation_id=None)` | `HPCJobHandle` | `hpc create` |
| `hpc.delete(ref, *, workspace=None)` | `None` | `hpc delete` |
| `hpc.events(ref, *, workspace=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `hpc events` |
| `hpc.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `hpc shell（非交互执行）` |
| `hpc.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]／AsyncIterator[EventResult]` | `hpc events --follow` |
| `hpc.get(ref, *, workspace=None)` | `HPCJob` | `hpc status` |
| `hpc.instance_names(ref, *, workspace=None)` | `tuple[str, ...]` | `hpc instances` |
| `hpc.instances(ref, *, workspace=None)` | `tuple[Instance, ...]` | `hpc instances` |
| `hpc.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[HPCJob]／AsyncIterator[HPCJob]` | `hpc list --all` |
| `hpc.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[HPCJob]` | `hpc list` |
| `hpc.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `LogResult` | `hpc logs` |
| `hpc.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `tuple[MetricGroup, ...]` | `hpc metrics` |
| `hpc.plan(spec)` | `HPCJobPlan` | `hpc create --dry-run` |
| `hpc.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `hpc quota` |
| `hpc.status(refs, *, workspace=None)` | `tuple[HPCJob, ...]` | `hpc status` |
| `hpc.stop(ref, *, workspace=None)` | `None` | `hpc stop` |
| `hpc.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None)` | `HPCJob` | `hpc status（SDK 轮询等待终态）` |

### ray

同步用 `client.ray.方法(...)`；异步用 `await client.ray.方法(...)`，本节全部参数、默认值与返回模型相同。例外：`follow_events`、`iter` 同步返回迭代器，异步直接用 `async for item in client.ray.follow_events(...)`，不用先 await；提前退出时用 `contextlib.aclosing`。异步另有 `client.ray.exec_stream(...) -> AsyncIterator[str]`，参数及默认值与下表 `exec` 完全相同，直接 async for；只交付字符串块，不返回最终 ExecResult。完整输出、回调与关闭规则见[远程执行](#远程执行exec)。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `ray.create(spec, *, operation_id=None)` | `RayJobHandle` | `ray create` |
| `ray.delete(ref, *, workspace=None)` | `None` | `ray delete` |
| `ray.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `ray events` |
| `ray.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `ray shell（非交互执行）` |
| `ray.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]／AsyncIterator[EventResult]` | `ray events --follow` |
| `ray.get(ref, *, workspace=None)` | `RayJob` | `ray status` |
| `ray.instance_names(ref, *, workspace=None)` | `tuple[str, ...]` | `ray instances` |
| `ray.instances(ref, *, workspace=None)` | `tuple[Instance, ...]` | `ray instances` |
| `ray.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[RayJob]／AsyncIterator[RayJob]` | `ray list --all` |
| `ray.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[RayJob]` | `ray list` |
| `ray.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `LogResult` | `ray logs` |
| `ray.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `tuple[MetricGroup, ...]` | `ray metrics` |
| `ray.plan(spec)` | `RayJobPlan` | `ray create --dry-run` |
| `ray.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `ray quota` |
| `ray.scaling(ref, *, group=None, limit=None, workspace=None)` | `tuple[RayScalingEvent, ...]` | `ray scaling` |
| `ray.start(ref, *, workspace=None)` | `None` | `ray start` |
| `ray.status(refs, *, workspace=None)` | `tuple[RayJob, ...]` | `ray status` |
| `ray.stop(ref, *, workspace=None)` | `None` | `ray stop` |
| `ray.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None)` | `RayJob` | `ray status（SDK 轮询等待终态）` |

### servings

同步用 `client.servings.方法(...)`；异步用 `await client.servings.方法(...)`，本节全部参数、默认值与返回模型相同。例外：`follow_events`、`iter` 同步返回迭代器，异步直接用 `async for item in client.servings.follow_events(...)`，不用先 await；提前退出时用 `contextlib.aclosing`。异步另有 `client.servings.exec_stream(...) -> AsyncIterator[str]`，参数及默认值与下表 `exec` 完全相同，直接 async for；只交付字符串块，不返回最终 ExecResult。完整输出、回调与关闭规则见[远程执行](#远程执行exec)。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `servings.api(ref, *, affinity_key=None, workspace=None)` | `ServingInvocationInfo` | `serving api` |
| `servings.api_metrics(ref, *, metric=None, window='1h', interval=None, workspace=None)` | `ServingAPIMetrics` | `serving api-metrics` |
| `servings.configs(workspace)` | `ServingConfigs` | `serving configs` |
| `servings.create(spec, *, operation_id=None)` | `ServingHandle` | `serving create` |
| `servings.delete(ref, *, workspace=None)` | `None` | `serving delete` |
| `servings.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `serving events` |
| `servings.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `serving shell（非交互执行）` |
| `servings.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]／AsyncIterator[EventResult]` | `serving events --follow` |
| `servings.get(ref, *, workspace=None)` | `Serving` | `serving status` |
| `servings.instance_names(ref, *, workspace=None)` | `tuple[str, ...]` | `serving instances` |
| `servings.instances(ref, *, workspace=None)` | `tuple[Instance, ...]` | `serving instances` |
| `servings.iter(workspace, *, project=None, status=None, keyword=None, max_items=None)` | `Iterator[Serving]／AsyncIterator[Serving]` | `serving list --all` |
| `servings.list(workspace, *, project=None, status=None, keyword=None, limit=20, cursor=None)` | `Page[Serving]` | `serving list` |
| `servings.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `LogResult` | `serving logs` |
| `servings.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `tuple[MetricGroup, ...]` | `serving metrics` |
| `servings.plan(spec)` | `ServingPlan` | `serving create --dry-run` |
| `servings.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `serving quota` |
| `servings.rollback(ref, *, version, workspace=None)` | `None` | `serving rollback` |
| `servings.scale(ref, *, replicas, workspace=None)` | `None` | `serving scale` |
| `servings.scale_history(ref, *, workspace=None, limit=20, cursor=None)` | `Page[ServingScaleHistoryEntry]` | `serving scale-history` |
| `servings.start(ref, *, workspace=None)` | `None` | `serving start` |
| `servings.status(refs, *, workspace=None)` | `tuple[Serving, ...]` | `serving status` |
| `servings.stop(ref, *, workspace=None)` | `None` | `serving stop` |
| `servings.versions(ref, *, workspace=None)` | `tuple[ServingVersion, ...]` | `serving versions` |
| `servings.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None, target='RUNNING')` | `Serving` | `serving start / create（状态等待）` |

### 远程执行（exec）

Notebook、Job、HPC、Ray 和 Serving 都提供同步 `client.<门面>.exec(...)` 和异步 `await client.<门面>.exec(...)`，返回 frozen dataclass `ExecResult`，可从 `inspire` 或 `inspire.sdk` 导入。遵循统一签名契约，`ref` 之后的参数（包括 `command`）只接受关键字。

```python
result = client.notebooks.exec(
    notebook.ref,
    command="python -u check.py",
    cwd="/inspire/ssd/project/example/public/work",
    env={"MODE": "check"},
    timeout=120,
    transport="auto",
    on_output=lambda chunk: print(chunk, end="", flush=True),
)
print(result.returncode, result.completed, result.transport)

result = client.jobs.exec(job.ref, command="hostname", instance="rank=0")
result = client.hpc.exec(hpc_job.ref, command="hostname")       # launcher
result = client.ray.exec(ray_job.ref, command="hostname")       # head
result = client.servings.exec(serving.ref, command="hostname")  # 首个运行中副本
```

Notebook 支持两种不依赖浏览器的传输：`jupyter` 通过 Jupyter terminal websocket 执行；`ssh` 只使用当前 Client 账号下已缓存、Notebook ID 与工作区 ID 均匹配且可达的 rtunnel 桥。`auto` 优先使用符合这些条件的 SSH 桥，否则选择 Jupyter。显式 `ssh` 找不到可用桥时抛 `ValidationError`，提示先运行 `inspire notebook connection refresh <name>`。创建或刷新 SSH 桥仍为 CLI-only，SDK 不会隐式启动 Chromium 建桥。

训练 Job、HPC Job、Ray Job 和 Serving 的 exec 始终使用平台交互式 PTY websocket，不提供 SSH 或分离输出选项。Job 的 `instance` 接受实例名、`rank=N`、裸数字或角色；不指定时要求恰好一个运行中实例，多实例会抛出列出候选的 `ValidationError`。HPC 默认选择 `launcher`，Ray 默认选择 `head`；默认角色不存在或匹配多个运行中实例时需要显式指定。Serving 默认选第一个运行中副本。显式实例名或工作负载公开标签必须匹配一个运行中实例；角色匹配多个副本同样报歧义。

命令按以下顺序组合：Client 配置的 `remote_env`、调用者的 `env`、可选的 `cd "<cwd>" && `，最后是 `command`。调用者的同名变量覆盖配置值；`env` 值按字面量引用，空字符串保留为空，不从本机环境补值。SSH 沿用 `bash -l` 执行方式。

`ExecResult` 的全部字段为 `returncode`、`output`、`stdout`、`stderr`、`completed`、`transport`、`instance=""`、`truncated=False` 和 `total_output_bytes=0`。`transport` 为 `ssh`、`jupyter` 或 `pty`；工作负载 PTY 的 `instance` 是选中的实例名。SSH 保留独立 stdout/stderr，`output = stdout + stderr`，此拼接不表示跨流时间顺序。PTY（包括 Notebook Jupyter terminal）的 stdout/stderr 已由远端终端合并，`stdout == output`、`stderr == ""`。PTY／Jupyter 结果尽力去除已识别的输入回显前缀，通过唯一完成 marker 提取退出码；SSH 使用子进程退出状态，不依赖终端 marker。普通非零退出码直接返回结果。

同步 exec、异步 exec 及异步 exec_stream 都支持以下仅关键字参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `max_output_bytes` | `4 * 1024 * 1024`（4 MiB） | UTF-8 内存捕获预算，包含省略标记；允许 `None` 显式取消限制，整数至少为 56 |
| `output_to` | `None` | 本地路径（`str` / `os.PathLike`）或可写文本文件对象，逐块写入完整解码流 |
| `capture` | `True` | `False` 时 `output`、`stdout`、`stderr` 均为空，仍检测命令完成情况（PTY／Jupyter 使用 marker）并统计字节数 |

超出捕获预算时保留头尾，中间插入 `\n[... output truncated ...]\n`，并设置 `ExecResult.truncated=True`。`total_output_bytes` 统计实际读到的解码字符串按 UTF-8 编码后的字节数，包括终端提示符、回显和完成标记，与是否捕获无关。新字段在结果末尾提供默认值；字节计数属于观测元数据，不参与结果相等性比较。`capture=False` 的主动关闭捕获不算截断，`truncated=False`。

PTY 和 Jupyter 使用固定扫描窗口及额外有界的终端前缀空间（128 KiB），用于清理输入回显；返回文本仍遵守捕获预算。内存还包括当前传输帧和解码临时对象，预算不是整个进程 RSS 的硬上限。SSH 将预算平分给 stdout/stderr，任一流超过自己的份额就报告截断；返回 `output` 仍按 stdout + stderr 拼接。

| 传输 | `max_output_bytes` / `capture` / `output_to` / `on_output` |
|---|---|
| Job、HPC、Ray、Serving PTY | 全部支持；文件和回调保留原始合并终端流 |
| Notebook Jupyter | 全部支持；文件和回调保留原始合并终端流 |
| Notebook SSH | 全部支持；分别捕获 stdout/stderr，文件和回调按实际读取顺序合并 |

路径以 UTF-8、覆盖模式打开，保留换行；执行完成或失败时关闭 SDK 打开的文件。调用者传入的文本对象由调用者关闭和 flush。文件或回调错误向调用方传播，不自动重放命令。`output_to` 保存的是实际读到的完整原始流，不是清理回显后的 `output`；需原样保留时使用路径或保留换行的文本对象。

```python
from inspire.sdk import iter_output_file

result = client.jobs.exec(
    job_ref,
    command="python produce_large_report.py",
    output_to="report.txt",
    max_output_bytes=1024 * 1024,
)
print(result.returncode, result.truncated, result.total_output_bytes)
# 每次最多读取 65536 个字符；超长单行也不会整行装入内存。
for page in iter_output_file("report.txt", chunk_size=65536):
    consume_page(page)

# 只保存完整文件，不保留返回文本：
result = client.notebooks.exec(
    notebook_ref, command="cat /tmp/large.log",
    output_to="large.log", capture=False,
)
```

`on_output` 在读取时按顺序接收解码后的字符串块。PTY 回调收到原始终端流，可能包含提示符、输入回显、ANSI 控制符及完成 marker；最终 `output` 才是解析后的输出。命令默认没有交互 stdin，需使用命令内的管道或远端文件重定向；人工交互仍用 CLI shell。

PTY／Jupyter 执行等待超时或连接在完成 marker 出现前结束时，返回 `returncode=124`、`completed=False`，尽可能保留已捕获输出；它不证明远端进程已停止。命令自己返回 124 且 marker 完整时，`completed=True`。Notebook SSH 超时同样返回 124／False；SSH 正常返回退出状态（包括 124）时 completed=True，但 SSH 自身连接失败也可能表现为非零退出状态，仍需检查 stderr。SDK 的总 operation 时间预算也会限制传输等待；名称解析和实例查询沿用现有 SDK 错误约定。websocket 握手 401 可在命令发送前续期一次并重试，不经过 `Transport.request` 或 `single_send`，已发送的命令不会因执行失败自动重放。

平台 PTY 接口只给一条终端字节流：stdout／stderr 的合并、退出码需通过命令内 marker 回传、终端可能回显输入，都是该执行路径的限制。SDK 无法从合并后的字节流可靠恢复原始通道，清理回显也只是尽力而为。Notebook 需要分离输出时显式选择已有 SSH 桥，`auto` 的结果需检查 `result.transport`；其他四种工作负载需在远端命令中将两路输出重定向到不同文件，再通过适当文件访问方式读取。

此前的无界内存捕获和反复扫描完整历史输出属于 SDK 实现问题，现已用默认 4 MiB 头尾捕获、固定窗口增量 marker 扫描和摊销线性的缓冲写入修复。`max_output_bytes=None` 仍可显式恢复无限捕获；`capture=False` 配合 `output_to`／回调／异步块流适合长输出。平台 PTY 限制与这些已修复的实现问题应分别理解。

Jobs、Notebooks、HPC、Ray 和 Servings 的 `exec_stream(...)` 参数与各自 `exec(...)` 相同，提供字符串块异步迭代器；它执行命令一次，不在流结束后再次执行。PTY／Jupyter 的块是原始合并终端流；Notebook SSH 的块按读取顺序交错，字符串块不附带 stdout／stderr 标签，因此要取得分离输出应使用 `await client.notebooks.exec(ref, transport="ssh", command=...)` 的最终结果。`exec_stream` 只交付块，不交付最终 `ExecResult`；需要返回码、completed 或截断统计时使用 `await client.jobs.exec(...)` 等普通形式。异步客户端的 PTY／Jupyter／SSH `exec` 和 `exec_stream` 的 `on_output` 类型均为 `Callable[[str], None | Awaitable[None]] | None`，接受同步函数或 `async def`，都在调用方事件循环线程运行；异步回调逐块按顺序 await 并施加背压，回调异常原样传播。同步 exec 只接受 `Callable[[str], None]`，在调用线程直接执行；这与前文并发章节的合同相同。同步回调应避免耗时操作，异步应用也可直接使用 `exec_stream` 消费块。`output_to`、`capture` 和输出大小限制沿用同步接口，长输出建议使用 `capture=False`。

```python
from contextlib import aclosing

async def execute(client: InspireAsyncClient, job_ref) -> None:
    async with aclosing(client.jobs.exec_stream(
        job_ref, command="python -u train.py", capture=False,
    )) as chunks:
        async for chunk in chunks:
            print(chunk, end="", flush=True)

async def check_notebook(client: InspireAsyncClient, notebook_ref) -> None:
    result = await client.notebooks.exec(
        notebook_ref, command="python check.py", transport="ssh",
    )
    print(result.returncode, result.stdout, result.stderr)
```

### Notebook 文件传输（upload / download）

同步与异步 Notebook 门面都支持上传和下载，`ref` 后只接受关键字参数。按名称选择 Notebook 时传 `workspace`；已有 `NotebookRef` 时可以省略。

```python
from inspire import InspireClient

with InspireClient("my-account") as client:
    uploaded = client.notebooks.upload(
        "my-notebook", workspace="my-workspace",
        local="./config.json", remote="experiment/config.json", transport="jupyter",
    )
    downloaded = client.notebooks.download(
        "my-notebook", workspace="my-workspace",
        remote="experiment/result.bin", local="./results/result.bin",
        transport="jupyter", overwrite=False,
    )
    print(uploaded.bytes_transferred, downloaded.transport)
```

```python
from inspire import InspireAsyncClient

# 在 async def 内运行。
async with InspireAsyncClient("my-account") as client:
    uploaded = await client.notebooks.upload(
        notebook_ref, local="./config.json", remote="experiment/config.json",
        transport="jupyter",
    )
    downloaded = await client.notebooks.download(
        notebook_ref, remote="/inspire/project/checkpoints", local="./checkpoints",
        transport="ssh", recursive=True,
    )
    print(downloaded.files_transferred, downloaded.bytes_transferred)
```

返回从 `inspire` 和 `inspire.sdk` 导出的 frozen `TransferResult`：`local` 是本地绝对路径，`remote` 原样保留调用方传入的请求路径，`remote_path` 是解析后的容器绝对路径（上传目标／下载源，两种传输及同步／异步客户端均填充），`bytes_transferred` 为原始文件字节总数，`files_transferred` 为常规文件数量（单文件为 1，空目录为 0），`transport` 是实际使用的 `jupyter` 或 `ssh`。没有 `.to_dict()`，需要时用 `dataclasses.asdict()`。

两种 `exec` 传输共享容器，却从不同工作目录启动：Jupyter 通常在 Jupyter 根目录，SSH 在用户 home（常见为 `/root`）。因此命令中的相对路径与传输的相对 `remote` 不具有相同的基准。用返回的绝对路径连接上传与执行，无需知道这次选中了哪种传输：

```python
import shlex
from pathlib import PurePosixPath

result = client.notebooks.upload(ref, local="model.bin", remote="model.bin")
client.notebooks.exec(ref, command=f"python train.py {shlex.quote(result.remote_path)}")

# 上面的 train.py 本身仍须能从命令工作目录找到。
# 若 train.py 与 model.bin 放在同一目录，显式 cwd 可同时定位两者：
client.notebooks.exec(
    ref, command="python train.py model.bin",
    cwd=str(PurePosixPath(result.remote_path).parent),
)
```

异步客户端使用相同字段，在 upload、download 和 exec 调用前加 `await`；下载结果的 `remote_path` 同样可用于后续命令引用远端源文件。

选择规则与 `exec` 相同，传输开始后失败不会换通道或重放整个传输：

| transport | 选择与用途 |
|---|---|
| `auto`（默认） | 优先使用账号、工作区、Notebook 身份匹配且可达的缓存 SSH 桥接，否则使用 Jupyter。 |
| `jupyter` | 强制使用 Notebook 自身的 Contents API；适合无桥接时传小文件，不支持递归目录。 |
| `ssh` | 必须已有可达缓存桥接，复用 CLI 的 `run_scp_transfer`；适合大文件和目录。缺失时报 `ValidationError`，提示 `inspire notebook connection refresh <name>`（CLI 中同时指定工作区）。 |

任何选择都不会隐式创建桥接。`recursive=True` 只允许 SSH；`auto` 没有桥接时会提示改用 SSH，不会逐个文件改走 Jupyter。

Jupyter 使用单个 base64 JSON 请求／响应，文本和二进制均按原始字节传输，不转换编码或换行。base64 本身约占原文件的 4/3，两端还需要 JSON、原始数据及解析副本，实际峰值高于这个比例；没有流式传输或断点续传。默认 `max_bytes=16 * 1024 * 1024`（16 MiB）限制单文件，编码正文约 21.3 MiB，给小文件配置／脚本／结果提供有界默认值。上传在读取正文前检查文件大小；下载先读取不含内容的元数据，再请求 base64 并复核字节数。文件在检查后增长仍可能增加 HTTP 响应内存，因此应避免并发修改源文件。超限错误明确提示 `transport="ssh"`；确有需要可显式提高 `max_bytes`，例如 `64 * 1024 * 1024`，该参数不限制 SSH。

路径和失败语义：

- `local` 接受 `str` 或 `pathlib.Path`。目标参数总是完整目标文件／目录路径，不采用 SCP 的“已有目录下再追加源文件名”规则。自动创建父目录；SSH 递归覆盖会合并目录、保留目标中未涉及的文件。
- **两种传输的 `remote` 含义一致**：绝对路径是 Notebook 容器内的真实绝对路径；相对路径以 Jupyter Contents 根目录为基准，与 SSH 用户 home 无关。例如根为 `/inspire/project/work` 时，`remote="data/x"` 和 `remote="/inspire/project/work/data/x"` 在 Jupyter、SSH 和 `auto` 中都指同一个文件。返回值 `remote` 保留调用方原始请求，`remote_path` 在两种写法下均为 `/inspire/project/work/data/x`。
- SDK 从 JupyterLab 页面配置的 `serverRoot` 发现根目录。同步客户端在 Notebook 门面生命周期内按 Notebook 缓存；异步操作会创建新门面，因此不同操作之间重新发现根目录。Jupyter 复用已有的入口请求；同步 SSH 相对路径仅首次发现时访问 Jupyter，异步 SSH 相对路径每次操作都需发现。缺失或无效配置会明确失败，不猜测根目录；此时可用绝对路径配合 `transport="ssh"`，该组合不依赖 Jupyter。
- Jupyter 只能访问根目录内可由 Contents API 表达的路径：SDK 将根内绝对路径转成根相对路径，根外路径（例如根为 `/inspire/project/work` 时的 `/tmp/x`）会拒绝并提示 `transport="ssh"`，不会去掉开头 `/` 后写到另一处。`auto` 选中 Jupyter 时遵守同一限制；访问根外文件请显式使用 SSH。
- 两种 `exec` 连接的是同一个容器，但默认工作目录不同：Jupyter 终端通常从 Jupyter 根目录启动，SSH 从用户 home（常见为 `/root`）启动。上传后执行命令，建议使用绝对文件路径；若命令引用相对路径，显式传 `cwd="<Jupyter 根目录>"`，两种 exec 才会从同一目录查找。上传／下载的相对路径规则不会改变 exec 的默认工作目录。
- 拒绝所有 `..` 路径分段（包括编码形式）、反斜杠及控制字符。空格、中文和 URL／shell 特殊字符按字面处理：Contents 路径做 URL 编码，SSH 控制命令的 JSON 参数做 shell 引用，SCP 仅看到 SDK 生成的安全临时远端路径。SSH 不支持源或目标路径中的符号链接；Jupyter 的根目录及符号链接访问边界仍由服务器执行。
- `overwrite=False` 先检查目标，已存在即报错。本地单文件发布还用原子硬链接防止检查后被抢占。Jupyter 没有条件创建 API，因此远端预检查无法排除并发写入；调用方须保证目标没有其他写者。递归目录合并同样不提供并发隔离。
- 下载和 SSH 上传先暂存，逐个文件在目标同目录写完后原子替换，因此中断写入不会暴露半个目标文件；目录合并是逐文件进行，失败前已完成的文件和已创建的父目录可能保留，不保证整个目录事务性回滚。SSH 在远端 `/tmp` 暂存完整副本，下载也需本地临时空间，发布时还需目标文件的临时副本空间；远端需要 `python3`。
- Jupyter 上传依赖服务器 ContentsManager 的原子性保证；失败或响应丢失时目标可能已经改变。每个 PUT 使用 `single_send`，写入不重试，结果未知会抛 `MutationUncertainError`，调用方应先核查目标。任何失败都不返回成功 `TransferResult`。
- `timeout` 与客户端操作预算共同限制网络步骤。异步 Jupyter HTTP 沿用原生请求调度，SSH/SCP 使用 asyncio 子进程；本地文件 I/O 卸载到客户端线程池，base64 编解码仍可能短暂阻塞事件循环。取消等待不能撤回已发送的写入；本地子进程会终止并回收，传输流程继续尝试清理暂存文件。清理失败时仍可能残留 SSH 临时文件，必要时检查 `/tmp/inspire-transfer-*`。

### tensorboards

TensorBoard 资源的创建、状态和生命周期查询走共享控制台传输；`tags`／`scalars` 的运行目录、标签和标量数据来自 TensorBoard 应用自身的 HTTP 接口。两种执行模式均通过 `Transport.application_connection()` 借用独立的临时 Cookie jar；GET 经 `ApplicationConnection.request()` 进入 `Transport.request()`，受共享 READ 重试、续期和操作预算约束。底层 helper 提议的超时为 60 秒，SDK 实际请求还受客户端 `timeout` 及剩余 `operation_timeout` 限制。jar 在上下文退出时关闭，应用 Cookie 不混入控制台会话；异步形式在相同 workflow 中使用原生 HTTP I/O。`points` 只裁剪结果中的尾部点集，底层会读取相应系列再汇总。

同步用 `client.tensorboards.方法(...)`；异步用 `await client.tensorboards.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `tensorboards.create(spec, *, operation_id=None)` | `TensorboardHandle` | `tensorboard create` |
| `tensorboards.delete(ref, *, workspace=None)` | `None` | `tensorboard delete` |
| `tensorboards.get(ref, *, workspace=None)` | `Tensorboard` | `tensorboard status` |
| `tensorboards.list(workspace, *, status=None, job=None, keyword=None, limit=20, cursor=None)` | `Page[Tensorboard]` | `tensorboard list` |
| `tensorboards.scalars(ref, *, tag='', run=None, points=None, workspace=None)` | `TensorboardScalars` | `tensorboard scalars` |
| `tensorboards.start(ref, *, workspace=None)` | `None` | `tensorboard start` |
| `tensorboards.status(refs, *, workspace=None)` | `tuple[Tensorboard, ...]` | `tensorboard status` |
| `tensorboards.stop(ref, *, workspace=None)` | `None` | `tensorboard stop` |
| `tensorboards.tags(ref, *, workspace=None)` | `TensorboardTags` | `tensorboard tags` |
| `tensorboards.url(ref, *, workspace=None)` | `str` | `tensorboard status（应用 URL）` |
| `tensorboards.wait(ref, *, target='RUNNING', raise_on_failure=False, timeout=60, poll_interval=3, workspace=None)` | `Tensorboard` | `tensorboard start / stop（状态等待）` |

## 公共导出与类型清单

`inspire.sdk.__all__` 当前包含 **131 个名称**；`from inspire import X` 对这些名称返回同一个对象。以下按导出名内省分组，不包含内部 facade 类。`inspire` 使用懒加载属性，并未定义同等的 `__all__`；使用显式导入，不依赖 `from inspire import *`。

- 入口与账号工具（3）：`Accounts`、`InspireAsyncClient`、`InspireClient`。
- 引用类型（19）：`APIKeyRef`、`ComputeGroupRef`、`DatasetApplicationRef`、`DatasetRef`、`DatasetTagRef`、`DatasetVersionRef`、`HPCJobRef`、`ImageRef`、`JobRef`、`ModelRef`、`NotebookRef`、`ProjectOwnerRef`、`ProjectRef`、`QuotaRef`、`RayJobRef`、`ResourceRef`、`ServingRef`、`TensorboardRef`、`WorkspaceRef`。
- 创建规格（6）：`HPCJobCreateSpec`、`JobCreateSpec`、`NotebookCreateSpec`、`RayJobCreateSpec`、`ServingCreateSpec`、`TensorboardCreateSpec`。
- 结果、资源与值模型（81）：`AsyncJobHandle`、`AsyncHPCJobHandle`、`AsyncRayJobHandle`、`AsyncServingHandle`、`AsyncTensorboardHandle`、`AsyncNotebookHandle`、`AsyncImageSaveHandle`、`AsyncImageRegisterHandle`、`APIKeyInfo`、`AccountCheck`、`AccountContext`、`AccountInfo`、`DatasetApplication`、`DatasetDetail`、`DatasetInfo`、`DatasetMount`、`DatasetTag`、`DatasetValidation`、`DatasetVersion`、`EventResult`、`ExecResult`、`TransferResult`、`HPCJob`、`HPCJobHandle`、`HPCJobPlan`、`Image`、`ImageDetail`、`ImageRegisterHandle`、`ImageSaveHandle`、`ImageSelector`、`InitResult`、`Job`、`JobHandle`、`Instance`、`JobPlan`、`LogResult`、`MetricGroup`、`ModelDeployConfig`、`ModelInfo`、`ModelRegisterHandle`、`ModelStatus`、`ModelVersion`、`Notebook`、`NotebookHandle`、`NotebookImageSizeEstimate`、`NotebookPlan`、`NotebookResourceSnapshot`、`NotebookRun`、`Page`、`Permission`、`ProjectDetail`、`ProjectInfo`、`ProjectOwner`、`Quota`、`QuotaOption`、`RayJob`、`RayJobHandle`、`RayJobPlan`、`RayScalingEvent`、`Resource`、`ResourceAvailability`、`ResourceUsage`、`Serving`、`ServingAPIMetricSeries`、`ServingAPIMetricTimeRange`、`ServingAPIMetrics`、`ServingConfigItem`、`ServingConfigs`、`ServingHandle`、`ServingInvocationCredentials`、`ServingInvocationInfo`、`ServingPlan`、`ServingScaleHistoryEntry`、`ServingVersion`、`Tensorboard`、`TensorboardHandle`、`TensorboardScalarPoint`、`TensorboardScalarSeries`、`TensorboardScalars`、`TensorboardTags`、`WorkloadSchedulePolicy`。
- 异常（20）：`AmbiguousResourceError`、`AuthenticationCooldownError`、`AuthenticationError`、`ClientClosedError`、`ClientThreadError`、`ConfigurationError`、`HPCJobFailedError`、`InspireError`、`JobFailedError`、`MutationUncertainError`、`NotebookFailedError`、`RayJobFailedError`、`ResolutionIncompleteError`、`ResourceNotFoundError`、`ServingFailedError`、`SubmissionUncertainError`、`TensorboardFailedError`、`TransportError`、`ValidationError`、`WaitTimeoutError`。
- 函数与类型别名（2）：`JobEvent`、`iter_output_file`。

其中 106 个导出对象满足 `dataclasses.is_dataclass`（包括继承 dataclass 的引用类），105 个 frozen；不能把“类型化”理解为所有返回值均不可变或均有 to_dict。`CustomImageInfo` 是两处镜像等待方法的返回类，可从 `inspire.platform.web.browser_api.images` 导入，不在上述顶层导出清单中。

## 创建规格字段

创建统一使用 `create(spec, *, operation_id=None)`；Jobs、Notebooks、HPC、Ray、Servings 支持 `plan(spec)`，只解析与校验，不发送创建请求。TensorBoard 使用 `TensorboardCreateSpec` 直接创建。以下字段与对应 CLI 的平台创建选项共享校验与载荷逻辑；构造规格本身不会提交。

### JobCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `quota` | `str \| Quota \| QuotaRef` | `必填` |
| `image` | `str \| ImageRef \| ImageSelector` | `必填` |
| `command` | `str` | `必填` |
| `nodes` | `int` | `1` |
| `shm_gib` | `int \| None` | `None` |
| `priority` | `int \| None` | `None` |
| `max_time_hours` | `float \| None` | `None` |
| `description` | `str \| None` | `None` |
| `framework` | `str` | `'pytorch'` |
| `auto_fault_tolerance` | `bool \| None` | `None` |
| `fault_tolerance_max_retry` | `int \| None` | `None` |
| `fault_tolerance_retry_interval_sec` | `int \| None` | `None` |
| `datasets` | `list[str \| DatasetMount]` | `[]` |
| `envs` | `dict[str, str]` | `{}` |
| `keep_after_success_hours` | `float \| None` | `None` |
| `keep_after_failure_hours` | `float \| None` | `None` |
| `public_path_readonly` | `bool \| None` | `None` |
| `enable_notification` | `bool \| None` | `None` |
| `exclude_nodes` | `list[str]` | `[]` |
| `specified_nodes` | `list[str]` | `[]` |

### NotebookCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `quota` | `str \| Quota \| QuotaRef` | `必填` |
| `image` | `str \| ImageRef \| ImageSelector` | `必填` |
| `shm_gib` | `int \| None` | `None` |
| `auto_stop` | `bool` | `False` |
| `auto_stop_after` | `int \| None` | `None` |
| `datasets` | `list[str \| DatasetMount]` | `[]` |
| `enable_notification` | `bool \| None` | `None` |
| `public_path_readonly` | `bool \| None` | `None` |
| `project_path_readonly` | `bool \| None` | `None` |
| `priority` | `int \| None` | `None` |
| `node` | `str \| None` | `None` |

### HPCJobCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `entrypoint` | `str` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `quota` | `str \| Quota \| QuotaRef` | `必填` |
| `image` | `str \| ImageRef \| ImageSelector` | `必填` |
| `image_type` | `str` | `'SOURCE_PRIVATE'` |
| `instance_count` | `int` | `1` |
| `priority` | `int \| None` | `None` |
| `number_of_tasks` | `int` | `1` |
| `cpus_per_task` | `int \| None` | `None` |
| `memory_per_cpu` | `int \| None` | `None` |
| `enable_hyper_threading` | `bool` | `False` |
| `max_time_hours` | `float \| None` | `None` |
| `keep_after_finish_hours` | `float \| None` | `None` |
| `datasets` | `list[str \| DatasetMount]` | `[]` |
| `description` | `str \| None` | `None` |
| `enable_notification` | `bool` | `False` |
| `public_path_readonly` | `bool \| None` | `None` |

### RayJobCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `command` | `str` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `quota` | `str \| Quota \| QuotaRef` | `必填` |
| `image` | `str \| ImageRef \| ImageSelector` | `必填` |
| `image_type` | `str` | `'SOURCE_PUBLIC'` |
| `description` | `str` | `''` |
| `priority` | `int \| None` | `None` |
| `shm_gib` | `int \| None` | `None` |
| `workers` | `list[str]` | `[]` |
| `public_path_readonly` | `bool \| None` | `None` |

### ServingCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `model` | `str \| ModelRef` | `必填` |
| `command` | `str` | `必填` |
| `port` | `int` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `quota` | `str \| Quota \| QuotaRef` | `必填` |
| `image` | `str \| ImageRef \| ImageSelector` | `必填` |
| `model_version` | `int \| None` | `None` |
| `replicas` | `int` | `1` |
| `nodes_per_replica` | `int` | `1` |
| `shm_gib` | `int \| None` | `None` |
| `priority` | `int \| None` | `None` |
| `custom_domain` | `str \| None` | `None` |
| `description` | `str` | `''` |
| `auto_scaling` | `bool \| None` | `None` |
| `public_path_readonly` | `bool \| None` | `None` |

### TensorboardCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `summary_path` | `str \| None` | `None` |
| `job` | `str \| JobRef \| None` | `None` |
| `auto_stop_hours` | `float \| None` | `None` |

Quota 内存与 shm_gib 单位为 GiB，时限与保留时间字段以名称中的单位为准。Job 共享 `build_training_job_plan`，优先级按工作区／项目策略解析，部分 None 字段使用账号配置。五种 Plan 的 `image` 均为 `Image`，用 `.name` 展示、`.url` 读取仓库地址、`.ref` 保留镜像身份；直接传入 registry URL 时也返回 Image，但不意味着平台已有镜像记录。Plan 都提供 `summary`、`create_kwargs` 与 `to_dict()`：summary 不含命令或环境变量值，to_dict 是工作负载各自的公开审阅映射，字段并不相同，也不能拿来重建提交请求；完整 create_kwargs 保留平台创建载荷（Job 对应 create_training_job 的 payload 内容），可能含命令、环境变量值和内部身份，应由调用方妥善处理。HPC 的 URL、Ray／Serving 的镜像 ID 仍在 create_kwargs 中，统一 image 类型不会改变提交能力。

HPC entrypoint 是 Slurm 执行正文，与 CLI 一样拒绝完整 shebang / SBATCH 脚本；CPU 布局参数按配额推导并验证。HPC 镜像解析为 URL，Ray 镜像解析为平台 ID。Ray workers 使用 CLI 语法，例如 `name=decode;image=镜像名称;group=完整组名;quota=0,8,32;min=1;max=4`；创建至少需要一个 worker 组，image-type 和 shm-size 可选。

Serving model_version 缺省取目录最新版本，port 范围为 1–65535，副本与每副本节点数至少为 1；运行后 `scale(ref, replicas=0)` 可缩至零。TensorBoard summary_path 创建时必须非空，job 只记录关联、不推导路径；auto_stop_hours 缺省采用 CLI 的 24 小时，上限 72 小时。

```python
from inspire import JobCreateSpec, Quota

spec = JobCreateSpec(
    name="sdk-job", command="python train.py", workspace="工作区名称",
    project="项目名称", group="完整计算组名称", quota=Quota(1, 8, 32), image="镜像名称",
)
plan = client.jobs.plan(spec)
print(plan.summary)
handle = client.jobs.create(spec, operation_id="pipeline-stage-1")
finished = client.jobs.wait(handle.ref, raise_on_failure=True)
```

异步创建使用相同的规格对象，返回对应的可等待 Async 句柄，在 `async def` 中执行：

```python
plan = await client.jobs.plan(spec)
print(plan.summary)
handle = await client.jobs.create(spec, operation_id="pipeline-stage-1")
finished = await handle.wait(raise_on_failure=True)
```

`plan()` 可能读取目录和校验接口，属于只读平台操作；构造 `CreateSpec` 本身不联网。异步 `plan()` 也必须 await。

## 写操作与 single_send

写操作由应用显式调用。注册签名为 `images.register(name, *, workspace, version=None, description=None, visibility=None, operation_id=None)` 和 `models.register(name, *, source_path, workspace, project, type=None, tag=None, description=None, operation_id=None)`。镜像注册预留推送槽位并返回 registry 地址，version 默认 v1、visibility 默认 private；模型注册共享盘目录，不上传本地文件。

SDK 的资源 JSON 变更请求显式进入 `single_send`（不包括认证握手或 exec 数据流），最多发送一次；发送后失败不刷新、不重试、不换通道。创建／注册无法确认结果时抛 `SubmissionUncertainError(operation_id, inspect=...)`，其他变更抛 `MutationUncertainError`。operation_id 默认生成，允许任意非空诊断字符串，**不是服务器幂等键**。异常的 `inspect` 属性与消息指出应检查的资源目录（jobs、notebooks、servings、TensorBoards、images、model versions 或 API keys），传输失败与缺少创建结果使用同一种消息结构。API-key 脱敏仍会清除服务器错误内容。遇到不确定结果，先显式查询确认，再决定后续动作。

写请求发出后，由 `request()` 与 `single_send` 共用分类逻辑区分明确答复和未知结果；所有分支都不会自动补发：

| 写入结果 | SDK 异常 | 调用方后续处理 |
|---|---|---|
| 明确拒绝：一般 HTTP 4xx（401/403/429 除外）、v2 业务错误（如 Conflict） | ValidationError，保留平台消息；HTTP 错误包含状态和最多约 500 字符正文 | 修正参数或资源状态 |
| 明确无权限：HTTP 403 | AuthenticationError | 检查权限 |
| 明确拒绝执行／限流：已解析信封中的 InternalError、Throttling 等 TransientAPIError，或 HTTP 429 | TransportError，retryable=True，保留消息 | 可由调用方安全重试；SDK 不自动重试 |
| 结果未知：发送后 HTTP 401/3xx、HTTP 5xx、网络异常、超时、JSON 解码失败或无效响应 | 创建为 SubmissionUncertainError，其他变更为 MutationUncertainError；retryable=False | 先查询确认，避免重复写入 |

分类转换以 `from error` 保留原因链；block 内已抛出的 ValidationError、AuthenticationError、TransportError 原样透传。

Job/HPC/Ray/Serving 创建响应缺少 ID 直接报不确定。Notebook 和 TensorBoard 复用 CLI 创建后的只读确认，确认失败不会重新创建。API key 创建成功响应没有 ID，因此返回 `ref=None`，之后可显式 `get(name)`。已发送写入的确认读取位于 single_send 外，不能触发重提。

Notebook 保存镜像先尽力估算大小，估算失败不阻止保存；镜像 ID 暂不可查时返回 `ref=None`。可见性是确认镜像 ID 后的独立单次写入，其失败通过 handle.warning 保留；`notebooks.wait_image_ready` 委托 `images.wait_ready`，两者都接受 `str | ImageRef | ImageSelector | ImageSaveHandle`，返回 browser_api 的 `CustomImageInfo`，不会重复保存。名称和 ImageSelector 需要 workspace；已有引用可省略，但显式 workspace 必须与引用一致。保存句柄来自 Notebook 流程，两处都接受；句柄尚无 ref 时立即抛 `ValidationError`，应先从镜像目录取得身份。Models.delete 默认检查所有版本引用和 pending 部署，force=True 跳过同一预检。

## 日志/事件/指标

```python
logs = client.jobs.logs(handle.ref, window="30m", instance="all", tail=50)
print(logs.text)
events = client.jobs.events(handle.ref, type="Warning", reason="sched", limit=20)
metrics = client.jobs.metrics(handle.ref, metric="gpu,cpu", window="2h")
for update in client.jobs.follow_logs(handle.ref, interval=2):
    print(update.text)
```

异步观察返回相同模型；follow 是异步迭代器而不是需 await 的协程：

```python
from contextlib import aclosing

# 放在 async def 内。
logs = await client.jobs.logs(handle.ref, window="30m", instance="all", tail=50)
events = await client.jobs.events(handle.ref, type="Warning", reason="sched", limit=20)
metrics = await client.jobs.metrics(handle.ref, metric="gpu,cpu", window="2h")
async with aclosing(client.jobs.follow_logs(handle.ref, interval=2)) as updates:
    async for update in updates:
        print(update.text)
        break  # aclosing 确保提前退出时关闭生成器和活动连接。
```

训练 Jobs 日志的 window 与 CLI 共用解析器，接受 `30m`、`2h`、`1d` 等正整数窗口。显式 window 以当前时间为终点；默认 None 使用任务创建 / 完成时间并前后各留 10 分钟，缺少创建时间时回看 24 小时。也可传 datetime start/end 指定绝对窗口。

四种实例门面的 `logs(instance=...)` 都接受 `Instance.label` 或 `Instance.handle`，可以传单个字符串或字符串序列；省略、`None` 和 `"all"` 均发现并选择全部实例。显式选择也先发现实例，不匹配时抛 `ValidationError`，避免平台把未知句柄回答为空日志而掩盖选择错误。Jobs 的旧参数 `instances` 已改为 `instance`，字符串始终作为一个选择器处理。tail/head 互斥，与 CLI 共用日志拉取与排序选择逻辑：请求条数为 `max(limit, tail, head)`，按时间排序后取头部或尾部；省略 tail/head 时取 limit 条尾部记录。平台返回有限样本，这不额外保证全局最后 N 条或无损续读。`max_chars=None` 默认不做字符截断；指定时裁剪格式化文本并设置 truncated。items 保留按条数选择的结构化记录。

训练 Jobs、HPC、Ray、Serving 的事件默认合并任务级与实例级事件；Jobs、Ray、Serving 的 type 精确匹配 Normal/Warning（大小写不敏感），四者的 reason 都做子串匹配。HPC 不提供 type 参数：当前 HPC 平台事件行没有 `type` 字段，不支持 Normal/Warning 分类过滤，不能把别的工作负载词表强加给它，否则会把缺少该字段的真实事件过滤成空结果；可用 reason 收窄。instance 接受单个标签或标签列表，按工作负载选择实例（Jobs 支持 `rank=0`、`0` 和角色名称）；workload_level 与 instance 互斥。limit 选择过滤后的最近事件。follow 按事件内容或日志标识去重，是轮询观察接口，不是平台持久订阅或无损游标。Jobs 的事件 follow 到终态停止，日志 follow 在检测终态后再拉取一轮；Notebooks、HPC、Ray、Servings 的事件 follow 持续轮询，需调用方关闭。

指标与 CLI 共用参数解析和平台样本提取：metric 支持 core/all、逗号分隔别名和原始指标名；start/end 支持 CLI 时间字符串，SDK 也接受 datetime。start 优先于 window。group 可覆盖从详情推断的计算组。

HPC 默认日志窗口取实例时间，Ray 取任务详情时间，窗口长度均最多 30 天，保留窗口结束时间。HPC 对 tail 或默认查询在 total 超过返回记录数时扩大一次请求，head 不扩大；Ray 在返回样本中选择。Serving 默认读取 24 小时／100 条，实例发现和日志使用 CLI 的共享核心。均不承诺全局最后 N 条或无损续读。

SDK 的 notebooks 门面不提供平台程序日志接口；`metrics` 返回 `tuple[MetricGroup, ...]`，实时资源快照由 `realtime_metrics` 单独返回。TensorBoard tags/scalars 要求 `RUNNING`；标量按 step 汇总首末值、min/max，points 缺省只返回摘要，指定正整数可获取尾部点集，`points=0` 与省略参数一样只返回摘要。Serving api 返回结构化调用信息，不发送推理请求；端点存在不证明服务已就绪，api_metrics 另提供 QPS／成功率／延迟序列摘要。

## 错误与时间预算

进入 SDK JSON 写入块时，若该 Transport 尚无返回响应记录，或距上次记录已满 60 秒，SDK 先通过普通 READ 路径执行一次 `GetUserDetail` 探测，必要时先续期再发送写入；60 秒内已有返回响应则省略探测；此时间戳不代表业务操作一定成功：控制台／应用请求到达 `_finish` 时更新，广场成功解包后也调用 `_finish` 更新它。探测共享时间预算，其刷新与重试不计入写请求的单次发送。探测失败时不发送写入；写入发出后即使收到 401 也绝不重放，创建抛 `SubmissionUncertainError`，其他变更抛 `MutationUncertainError`。

SDK 定义的异常（包括 `NotebookFailedError`）均从 `InspireError` 派生。参数提示使用 Python 参数名；时间校验指出实际被拒绝的 `timeout`、`poll_interval` 或 `interval`，不统一称为 wait。`iter_output_file` 的分块参数错误为 `ValidationError`，文件读取／UTF-8 解码失败为 `TransportError`；SSH 文件传输超时为 `WaitTimeoutError`。配置、认证、冷却、参数错误、未找到、歧义、不完整枚举、传输失败、写入不确定、等待超时分别可捕获。`AmbiguousResourceError.candidates` 返回该门面的资源模型候选，选定后用 `error.candidates[index].ref` 取得引用；`AuthenticationCooldownError.retry_at` 是允许再次评估认证的 Unix 时间，不是鼓励盲目重试错误密码。冷却沿用 CLI 的账号级 guard。

`timeout` 控制单次请求预算，`operation_timeout` 控制普通操作的协作式总预算，`wait(timeout=...)` 设置整个等待预算；嵌套调用使用更早截止时间。控制台 Transport 的请求、退避和账号刷新锁等待共享剩余预算；TensorBoard 应用 GET 与 Jupyter Contents 请求也使用该预算。同步网络库的底层调用和已有浏览器登录流程不能被强制抢占，显式允许浏览器时登录可能超出总预算；这些参数不是硬实时取消保证。需要硬隔离的编排器应使用独立进程，并在超时后核查任何可能已发送的写请求。

传输策略由调用方声明：普通 `operation` 进入 `Transport.scope(timeout=...)`，按 READ 处理。READ 对 requests 异常、HTTP 429/5xx、共享 `_is_transient_v2_error_code` 判定的 v2 暂时错误最多尝试三次，退避与请求共用截止时间。HTTP 401/3xx 无论 allow_browser 设置均允许一次上述会话刷新，重试仍失效则抛 AuthenticationError；requests 层失败也只有显式允许浏览器才可换通道。

SDK 中真正写入的 JSON browser_api 调用必须包在 `transport.single_send(operation_id, create=True, inspect="对应资源目录")` 或 `transport.single_send()` 中。一个 block 最多允许一次 request，第二次调用抛 RuntimeError；create 参数显式区分创建与其他变更。发送后不刷新、不重试、不换通道；明确拒绝映射为 ValidationError，明确限流／拒绝执行映射为可重试的 TransportError，HTTP 403 保留 AuthenticationError，仅未知结果映射为 SubmissionUncertainError / MutationUncertainError。Transport 不按 URL、Action 或 HTTP 动词猜测幂等性。`request()` 不自动解包 v2 信封，解析后的 JSON 交给 browser_api；只读路径会先识别其中的暂时错误。`plaza_request()` 则使用广场专用的信封解包器返回 data。READ 的一般 HTTP 4xx 返回包含状态码和最多约 500 字符正文的 ValidationError，业务错误保留原消息。

工作负载 wait 的 raise_on_failure=True 抛对应 SDK 失败异常，携带最终资源快照：Job/HPC/Ray 使用 `.job`，Notebook 使用 `.notebook`，Serving 使用 `.serving`，TensorBoard 使用 `.tensorboard`。Job/HPC/Ray 等待终态；Notebook、Serving、TensorBoard 等待目标状态，具体默认目标见方法表。Notebook、Serving、TensorBoard 的 target 均先去首尾空白、按各自词表归一化并转大写，再校验；TensorBoard 另去掉大小写不敏感的 `tb_status_` 前缀。未知 target 在名称解析和轮询前抛 `ValidationError`，消息列出接受值，避免把拼写错误伪装为平台超时。Notebook 接受 RUNNING／STOPPED；Serving 接受 CREATING／PENDING／RUNNING／UPDATING／STOPPING／STOPPED／FAILED／ERROR／DELETED；TensorBoard 接受 CREATING／RUNNING／STOPPED／FAILED／ERROR／DELETED。超时统一抛 WaitTimeoutError。

## 维护接口

维护接口时，在 `cli/` 运行 `uv run python scripts/generate_sdk_async.py` 更新已签入的显式包装方法。`tests/test_sdk_async.py` 比较所有实例 facade、方法签名和返回类型，并检查生成文件完全一致；新增同步方法未生成异步版本会导致测试失败，mypy 可直接检查真实签名。异步提交返回 Async 句柄，以及异步独有的 `bind_ref`／`bind_image_ref`，均在签名对等测试中明确列为有意差异；句柄 wait 的关键字签名仍必须与门面一致。

## Windows 支持与本地私有文件

Windows 是 SDK 的受支持平台；同步与异步入口沿用同一平台接口。异步 SSH exec
和 SCP upload/download 使用 asyncio 子进程，要求 `ProactorEventLoop`。Windows 的
默认事件循环通常已经满足；若宿主框架改用了 `SelectorEventLoop`，调用会抛出
`ConfigurationError`（属于 `InspireError`），说明需要由应用或框架启用 Proactor。
可在启动循环前配置 `WindowsProactorEventLoopPolicy`，或由框架创建 Proactor 循环；
SDK 不会替调用方切换事件循环策略。SSH/SCP 通道仍需本机 OpenSSH 与已可用的桥接，
这项支持不等于在远端 Windows 容器中验证了命令、Linux 路径或 shell 脚本。

账号配置可含明文密码，session 可含 cookie／CAS ticket，桥接及 IDE 缓存地址也可能
带认证参数。它们由共享原子写入器发布：POSIX 临时文件创建时即为 `0600`，Inspire
目录和账号目录使用 `0700`。POSIX 的旧文件修复仍只收紧权限，逐级使用 `O_NOFOLLOW`
打开路径，拒绝硬链接文件；自动读取修复限定在 `~/.inspire` 内。

Windows 通过 PowerShell 为 `~/.inspire`、`accounts/` 和各账号目录设置受保护的、
仅当前用户 FullControl 的目录 ACL，包含 `ContainerInherit,ObjectInherit` 并读回验证。
现有目录须已赋予当前用户直接 FullControl，否则保持原样并警告；保留拒绝规则，移除
其他主体的允许规则，为当前用户补齐对子项的继承。每个目录在每个进程内最多尝试一次，
并发调用也共用该记录。文件写入和配置读取不再逐文件启动 PowerShell；同目录内新建
临时文件从创建时继承父目录 ACL，再原子替换目标。`chmod(0600)` 在 Windows 上不构成
访问控制。自定义私有状态路径也只初始化其目录 ACL，不逐文件修复。

目录继承不能清除旧文件上的显式额外授权，也不能修复禁用继承的旧文件、带原 ACL
移入的文件或经目录外硬链接访问的文件。只读配置不会逐文件迁移这些 ACL；共享原子
写入器重写时以新继承 ACL 的文件替换目标。进程内已检查的目录若被外部替换或更改 ACL，
不会自动重新检查，应修复目录／文件 ACL 后重启进程。不会沿符号链接／reparse point
修改外部目标。目录 ACL 不提供完整的旧文件 ACL 审计或迁移保证。

自动状态写入若缺少 PowerShell、ACL 设置或验证失败，会继续执行，并对每个目录在
进程内警告一次，包含路径和原因；失败的尝试也缓存，修复后重启才重试。不新增状态
文件，也不把可选缓存加固变成登录的前置条件。此时无法承诺已建立额外的仅当前用户
ACL。显式 `api-key export` 保持原合同：包括 `~/.inspire` 外的用户指定文件，仍在空
临时文件上单独设置并验证 ACL，验证失败即报错，不写入密钥，不使用目录尝试缓存。

Windows 短暂的文件占用可能阻止替换，因此共享写入器最多尝试五次，总退避为 0.5 秒；
仍失败时保留旧文件并抛出原文件系统错误，调用者原有的可选缓存降级规则继续适用。
默认 `%USERPROFILE%` ACL 通常已排除其他标准用户，因此 Windows ACL 加固属于纵深
防御；POSIX 上旧的 `0664` 密码文件则确实可被其他本地用户读取。上述保护不是加密，
不防管理员、同一用户权限的进程、备份／同步副本或应用自行输出的凭证，也不保证
自定义共享目录／文件系统的默认 ACL 安全。

## CLI-only 范围

- 交互初始化提示、Playwright 安装、ssh-keygen，以及 `config *`、`update`、`uninstall`、CLI 的磁盘资源缓存命令 `cache *`（SDK 的 `client.cache` 可选择与 CLI 共用身份／配额缓存实现）；非交互账号管理和初始化由 `Accounts`、`client.login()`、`client.init()` 提供。
- `api-key export` 的文件格式、权限和 stdout 渲染，以及 `api-key run` 的子进程和环境处理；平台密钥读写由 `client.api_keys` 提供。
- 所有工作负载的 JSON/TOML `batch`；SDK 应用自行循环或编排。
- Notebook 的 exec 和文件传输（upload/download，含 SCP）由 SDK 提供；`ssh/shell/ssh-config/ssh-proxy/connection */install-deps/proxy-url` 仍为 CLI-only，创建后的 `--post-start/--post-start-script` 及 `job/hpc/ray/serving shell` 也仅保留在 CLI。
- 日志 SSH 文件来源选项 `--path/--remote-log-path/--notebook/--source`，及终端专用格式、字符展示预算；SDK 使用平台日志来源并返回结构化记录。
- 指标 `--plot/--open/--sparkline` 和 TensorBoard 终端趋势渲染；SDK 返回样本或标量摘要。
- `serving api --format` 的 shell 格式输出；SDK 返回共享 access 核心的结构化 endpoint / invocation 信息。
