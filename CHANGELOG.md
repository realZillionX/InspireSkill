# Changelog

## Unreleased

### 性能

- **名称缓存改为按需读穿 / 写穿，普通命令不再启动全账号后台扫描。** 共享账号 `lty` 可见 16 个 Workspace，旧刷新子进程实测运行超过 5 分钟仍未完成，并会为没有当前消费者的 Image、Model 和六类 Workload 逐空间请求，与前台争用同一账号的限流额度。现在 fresh hit 纯本地解析，miss / 过期只查当前名字，创建与删除立即写入或墓碑；Workload 名称映射 TTL 从 5 分钟延长到 1 天，`list` / `status` / Availability 等可变事实仍只读 Live。显式 `cache refresh` 保留，且始终做所选 Scope 的完整对账。

  同批复用登录握手已经保存的 `GetUserDetail`：`job` / `hpc` / `ray` / `notebook` / `tensorboard` / `model` 的 owner filter 不再先串行查询一次当前用户；缺失时才 Live 回源并写回 Session。`account check` 显式强制 Live，仍然用真实往返证明 Session 可用。`lty` 上身份请求实测 `0.408 s`，缓存命中约 `7 µs`。

  `train.ListJobs` 还有一个低于传输层通用上限的 Action 级限制：`page_size=999` 实测返回 999 行，`1000` 返回 `InvalidParameter: page or page_size too large`。Wrapper 现在按 999 截断；`lty` 的 `分布式训练空间` Job 缓存完整刷新实测 999 个名字、5.42 秒完成。

- **镜像全目录并发读取，Job / HPC / Notebook Batch 复用命令内 Live 快照。** `image list --source all` 的 official / public / project / private 是四个互不依赖的 `ListImages` 请求，现改为同时发出，再按原页签顺序合并，部分失败的 warning 语义不变；`lty` 实测 9.05 秒降到 5.49–6.81 秒，剩余时间由最慢的单个目录决定。

  Job Batch 此前每个条目都重读同一 Workspace 的 Quota 优先级菜单、Project 目录、排队拥塞和镜像目录。现在只在本次 Batch 进程里复用第一条刚读到的 Live 快照，不跨命令持久化；3 条 dry-run 的真机请求数 15 → 5，11.32 秒 → 5.32 秒。

  HPC 更严重：每条原本读两次相同 Project 目录、再读一次 Image；3 条从 9 → 2 请求、17.05 秒 → 5.01 秒。CPU Notebook 的 3 条从 9 → 3 请求、10.30 秒 → 2.53 秒。Project 优先级、拥塞和 Quota 限制仍由本次命令刚取得的 Live 数据决定。

### 修复

- **账号配置不再缓存项目和资源目录，隐式远端 cwd 不再注入 `cd`。** 旧版 `inspire init` 会把全部可见 Project、Compute Group、推导出的项目路径和未使用的 `docker_registry` 一起写进 `~/.inspire/accounts/<name>/config.toml`。一个账号跨多个 Project 使用时，账号级 `me` 会把 Notebook/Job 命令带进另一个 Fileset；Compute Group 快照还会在 Live API 失败时冒充资源事实。现在账号加载立即忽略这些旧字段，下一次 `inspire init` 会从磁盘删除 `[projects]`、`[project_catalog]`、`[[compute_groups]]`、账号级 `[path_aliases]` 和未使用的 `api.docker_registry`，其它未知字段保留；全局 init 不再枚举这些目录，项目路径只由 `inspire init --scope project` 写进仓库配置。Notebook exec/shell 省略 `--cwd` 时不生成 `cd`，Job 启动命令也不再由 CLI 添加 `cd`，两者均保留平台、容器或远端 Shell 的初始工作目录；显式 `--cwd` 仍会解析 Path Alias，Job 的共享文件日志目录与执行 cwd 解耦。

- **Windows 成为一等公民，不再要求 WSL。** 此前 `import inspire.cli.main` 在 Windows 上直接 `ModuleNotFoundError: fcntl` —— 断点在 `accounts/cache_lock.py`，而它在第一条子命令的 import 链上，所以不是「某个命令不能用」，是 `inspire --version` 都起不来。五处模块级 POSIX 导入（`fcntl` / `pty` / `termios` / `tty`）按平台分叉，文件锁在 Windows 上用 `msvcrt.locking` 实现。CI 增加 `windows-latest`。

  **ProxyCommand 那条链上纠正了一个流传的判断**：Windows OpenSSH 并不通过 `cmd.exe` 跑 ProxyCommand。`FORK_NOT_SUPPORTED` 下它把整条命令串交给 `posix_spawnp`，最终落到 `CreateProcessW(lpApplicationName=NULL)`，中间没有任何 shell。所以那里的 `2>NUL` 不是重定向，而是 rtunnel 的第 4 个位置参数——rtunnel 收到就 `Error: invalid number of arguments` 退出。而 `_resolve_bridge_and_proxy` 的 `quiet` 默认是 `True`，`notebook ssh` / `exec` / `scp` 和 `is_tunnel_available` 全走它。现在 Windows 分支不含任何 shell 语法，逐 token 加双引号（首字符是 `"` 正是 OpenSSH `build_commandline_string()` 原样透传的分支），proxy override 改由 ssh 自己的环境传给 rtunnel。

  **`UserKnownHostsFile` 保持 `/dev/null`**：Win32-OpenSSH 的 `fileio.c` 已经同时映射 `/dev/null` 和 `NUL`，换成 `NUL` 是没有差异的分叉。

  **`ssh-config` 的引号在所有平台上都是错的。** ProxyCommand 和 `IdentityFile` 都用 `shlex.quote` 拼，而 OpenSSH 的 `strdelim` 只认双引号——POSIX 单引号会留在文件名里。这在 macOS/Linux 上同样坏，只是 ssh-agent 一直兜着所以没人发现；`shlex` 带 `re.ASCII`，中文 workspace 名必然被引号包住，Windows 上则连模块都找不到。ProxyCommand 在 Windows 上改用 `list2cmdline`，`IdentityFile` 改用 OpenSSH 自己的引号规则。

  **交互式 shell 不是 fail-fast，是真的实现了**：`SetConsoleMode` 做 raw 模式（清掉 `PROCESSED_INPUT` 正是让 Ctrl-C 变成 `0x03` 字节而非信号，与 `tty.setraw` 语义一致），stdin 由读线程供给（`select` 在 Windows 上只收 socket），窗口大小改为轮询（没有 `SIGWINCH`）。`job shell` 和 Jupyter terminal 两份重复的循环收进一份实现，POSIX 分支保持原样。`run_remote_shell` 此前没有任何直接测试，这次补上。

  **编码这条线**：`›` 这类字符不是靠换成 ASCII 解决的——仓库里 user-facing 的 `—` 有 89 处、`─` 27 处，还有大量中文，换字符只会让 mac/Linux 的输出一起退化。真正的成因是 Python 在 Windows 上一旦 stdout 被重定向就回落到 ANSI 代码页（中文 Windows 是 cp936），而 agent harness 调 CLI 全是管道。改为在 CLI 入口把 stdout/stderr 重设为 UTF-8。连带修掉：6 处 subprocess 的文本解码没指定编码（远端日志会乱码），bridge 连接缓存读取没指定编码（**这条与 Windows 无关，任何非 UTF-8 locale 都会因为中文 workspace 名而静默丢掉整份缓存**），`uninstall --purge-runtime` 在 Windows 上找错 Playwright 缓存目录，以及后台更新检查每次都闪一个控制台窗口。

  `job logs --follow --transport ssh` 的 `select` on pipe 与 `ssh_exec` 的同款一起收进 `inspire.process_io`。`exec_rtunnel_proxy` 的 `os.execve` 在 Windows 上是 spawn + 父进程退出，会把 OpenSSH 记录的 proxy pid 打掉，改为留一个薄父进程转发退出码。

  **windows-latest 头一次跑，挖出四个此前无人踩到的 Windows bug**，都不是这次改出来的：

  - **后台更新检查没脱离控制台。** `start_new_session=True` 在 Windows 上被静默忽略，于是子进程挂在用户当前终端上，既会闪窗口也共享 Ctrl-C。现在用 `cli/utils/detached.py` 的 `creationflags` 真正脱离。
  - **resource index 的 sqlite 连接从来没关过。** `with sqlite3.connect(...)` 只提交事务、不关连接。POSIX 上只是不整洁，Windows 上句柄没放意味着文件删不掉——「索引损坏就丢掉重建」这条恢复路径必然以 sharing violation 失败。
  - **远端日志路径用 `os.path.join` 拼。** 在 Windows 上产出 `/train/user space\.inspire\xxx.log`，而这条路径会被塞进跑在 Linux 计算节点上的 shell 命令里。远端路径一律走 `join_remote_path`。

  测试侧同样是第一次在 Windows 上跑：`monkeypatch.setenv("HOME", ...)` 是 POSIX 惯用法（`ntpath.expanduser` 只读 `USERPROFILE`），所以这些测试在 Windows 上一直读写**跑测试的人自己的** `~/.inspire`；114 处 `read_text`/`write_text` 没写 `encoding`，中文 workspace 名在 cp1252/cp936 下直接炸。产品代码一处都没漏，这次把测试补齐。

  **真机复审（@expectqwq，Windows 11 / PowerShell 5.1 / cp936）确认全链路可用**：系统 OpenSSH 下 ProxyCommand、真实 ssh/exec/scp roundtrip、cp936 重定向、installer 检测、后台进程零残留全部通过，全量 2831 passed。复审同时抓出两条合并前修掉的问题：`watch_terminal_resize` 在重构时丢了旧实现的 TTY gate——`signal.signal` 只能在主线程调，库调用方在 worker 线程里带管道 stdin 驱动交互 shell 会直接 `ValueError`，现在非 TTY 恒不碰 signal 状态（Windows 侧同样不再空转轮询）；Windows 安装说明从 `install-and-config.md` 拆到 `references/setup/windows-native.md` 并在 `SKILL.md` 单独路由，Linux Agent 的安装/账号任务不再多读约 1.8KB 的 Windows 专属内容。

  `scripts/install.ps1` 装的是 PyPI 上的包（`uv tool` / `pipx`），不是 editable checkout——`inspire update` 靠 `sys.prefix` 里有没有 `uv/tools` 或 `pipx/venvs` 判断能否自更新，editable 装法会静默让用户失去自更新能力；skill 文件交给 CLI 自己铺（新增内部命令 `_refresh-skills`），不在 PowerShell 里复制一份 harness 列表。

## v7.1.2

### 新增

- **`job status` / `hpc status` / `job events` 一次可以问多个任务了。** 平台在 v2 上给 `ListJobs` 加了 `job_ids`、给 `ListJobEvents` 加了 `filter.object_ids`，两条都实测过：`train.ListJobs` 按 `job_ids` 过滤出来的记录与 `train.GetJob` **逐字段完全相同**（running / stopped / failed 三态各比一次，无独有字段、无值差异），所以这是 `GetJob` 扇出的批量形态而不是它的摘要；`ListJobEvents` 严格可加（5 个任务批量 31 条 = 单查的 3+3+3+3+19），每行带 `object_id`，因此能原样拆回各自的任务。N 个名字现在是每 20 个一次请求，而不是每个一次。

  **两条服务端限制写进了代码，因为两条都会静默地骗人**。其一，上限 20 **按列表长度计数而不是集合**——20 个不重复的 id 再加一个重复项就是 21 条，照样被拒，所以去重发生在分片之前而不是之后。其二，两个 Action 对「找不到的 id」的回答**正好相反**：`ListJobs` 静默丢弃（答案只是短了一截，只有拿请求去 diff 才看得出来是任务被删了还是自己没点名），而 `ListJobEvents` 让**整个请求**失败并报 `InvalidParameter: job <id> not found`——一个已被回收的任务足以把同批另外 19 个的事件全部带走。批量事件路径因此会剔掉报错点名的 id 重试，并把剔掉的显式报出来，而不是返回那个会被读成「这些任务没有事件」的空列表。

  名字边界没有变：批量命令同样只收名字，内部解析成 id 再批量查。一个解析不出来的名字**不再终止整条命令**——能答的照常打印，答不了的以 `Unresolved: <名字>: <原因>` 单独列出（`--json` 下是 `unresolved` 数组，刻意不叫 `not_found`：一个匹配到四个任务的名字并不是「不存在」，两者需要不同的处置），退出码仍报告答案是残缺的。`--pick` 与 `--instance` 只对单个名字有意义，多名字时直接拒绝而不是猜。`hpc status` 的批量路径改为**列一次 Workspace 再本地匹配**，因为单名路径走的 `resolve_by_name` 在名字对不上时会直接结束进程——对一个名字是对的，对二十个不是。

  复审又堵上两个口子。其一，事件路径的「not found 剔除重试」写的是 `except ValueError`，而 `TransientAPIError` 恰好是 `ValueError` 的子类——一次 5xx 的响应体里但凡带上 "not found" 三个词，限流就会被翻译成「这批任务都不存在了」；现在瞬态错误在嗅探之前显式放行，平台没回答就是没回答。其二，hpc 的本地名字匹配原来一次 `page_size=10000` 列完就算，而网关对超过 5000 的 page_size 是**静默截断**（这条就记在本文档里），列表被截掉的那部分名字全都会被答成「没有这个任务」——一个错答案而不是一个报错；现在按返回的 `total` 翻页直到列完，翻页间移动的行按 job_id 去重，不会读成两个同名任务。

  多任务事件合并成一条时间线时新增 `Job` 列，因为跨任务的时间线不标出处就读不了——这与 `instance` 列存在的理由是同一条。

### 变更

- **`GetTaskMetricBatch` 复查过了，结论是继续不用它，指标维持逐个扇出。** 运维给的批量清单里包含这个 Action，但它答的不是同一份数据：在 `train` 路由上拿 4 个运行中的任务、同一时间窗逐指标对照，`disk_io_read` / `disk_io_write` **返回 0 个样本而单数版同窗口有 61 个点**（4/4 复现），两个 `network_tcp_ip_io_*` 直接 `InternalError`，并且所有 group **一律没有 `group_name`**，多 Pod 任务的逐 Pod 拆分就此丢失。8 个指标坏 4 个。

  **剩下那 4 个也不是同一份数**——这点上一轮没查，因为只比了样本数没有逐点比数值。取一个已经结束的固定窗口，两个端点各自都是确定的（各查两次逐字节相同），但互相对不上：同一时间戳单数版 `0.20625`、Batch `0.5`。差在聚合样本数上，把全部返回值的公分母算出来，**单数版恒为 Batch 的 2 倍**（4 卡 800:400、8 卡 1600:800，即 `200×卡数` 对 `100×卡数`，4 个任务全部符合）。长期均值大致对得上，单点能差到三倍。所以它不是「批量版少了个字段」而是**另一套聚合**：即便只取那 4 个能跑的指标，画出来的曲线也不是控制台上那一条。它的响应形状也不一样——`task_metrics[].time_series_metric_groups`，而不是单数版顶层的 `time_seris_metric_groups`——这正是它容易被读成「空」而不是「坏」的原因，上一轮调研就是在这里读错了键。控制台自己从不调用这个 Action。顺带复核了单数版 `GetTaskMetric` 一次只认第一个 `metric_types` 的老结论：传 2 / 4 / 8 个类型各试一次，返回的始终只有第一个，扇出确实省不掉。结论写进了 `get_resource_metrics_by_time` 的 docstring。

- **`/api/v1` 从这个客户端里彻底消失了。** 最后三处——登录握手的 `user/detail`、登录时发现 Workspace 的 `user/routes/default`、Notebook 的 `notebook/lab/{id}`——留着的理由此前写的是「Session 自举时还没有 Session 可供 v2 用」和「反向代理不是 Action 能表达的东西」。三条实测全部不成立：

  - `user.GetUserDetail` 空 body 就答，`data` 与 `Result` **逐字段相同**（8 个键，0 差异）。未登录时两边同样是 401，所以登录轮询那段判据一个字都不用改。
  - `user.GetRoutes` 的 `WorkspaceId` **收字面量 `"default"`**，答的是完整 `userWorkspaceList`，与传一个真实 Workspace id 的响应 5794 字节 **0 差异**。「v2 要一个真实 `WorkspaceId`，登录时拿不到」是我们自己写进 Reference 的错误结论——而登录时恰恰没有真实 id，于是这条推断把自己锁死了整整一个迁移周期。空串或缺键才报 `WorkspaceId is required`。
  - `/api/v2/notebook/lab/{id}` 与 v1 同样 `301` 到同一个带 token 的网关地址，跟过去是同一份 8007 字节的 JupyterLab；**控制台自己的 iframe 指的就是这条**。顺带钉死一件事：`/proxy/{port}/` 挂在平台域上时两种前缀都答 `404 page not found`，只有带 token 的网关地址真的会连容器端口。

  **验收是真的跑了一次登录**：备份 Session 缓存，`force_refresh` 走 requests/CAS 与 Playwright 两条路径各一次，10 个 Workspace、名字、`is_fair_workspace`、`user_detail` 与迁移前完全一致；Notebook lab 的解析拿一台运行中的 CPU Notebook 对 v1/v2 各跑一遍，落点相同。

  连带清理：`_v2_result()` 移进 session 层（登录也要用它，而 session 不能反向 import `browser_api`）；`INSPIRE_BROWSER_API_PREFIX` / `api.browser_api_prefix` **配置项删除**——它最后只对 Notebook lab 一处生效，设成别的值只会把那一处弄坏，旧 `config.toml` 里残留这个键会被静默忽略；六处 docstring 还报着 `POST /api/v1/model/list` 这类早已不存在的地址，连同 `browser-api.md` 的「仍在使用的 v1 端点」整节一起重写。边界测试也换了两条更强的不变量：**全树零 `/api/v1` 字面量**（不再是白名单），以及 `/api/v2` 字面量只许出现在 `browser_api/`（例外两条：`job_shell.py` 的四条实例 PTY、`session/auth.py` 的登录自举）。

- **`--help` 里有四条跑不通的示例。** `resources availability` 的正文写着「Requires `--workspace <name|all>`」、示例直接给了 `--workspace all --include-cpu`，而运行时答 `--workspace requires one workspace name for this command.`；`resources nodes` 和 `resources policy` 各有一条同样的 `--workspace all` 示例；`project list` 的示例带 `--workspace all`，而这条命令**根本没有 `--workspace`**（项目是全局对象）。`SKILL.md` 写的是「命令语法和参数始终回到 CLI Help」，所以 Help 里一条抄下来就报错的示例比文档过期更糟。同批修掉两处指向不存在的 `inspire resources quota` 的引用（真实命令是 `<workload> quota`）。

- **README 的命令枚举补齐到真实命令面。** 用 Click 的命令树逐组对过，11 个枚举了子命令的组现在**逐字匹配**：Notebook 补 `delete` / `ssh-proxy`（给 OpenSSH `ProxyCommand` 用的裸流转发，`ssh-config` 生成的配置就指向它）/ `batch` / `profile` / `quota`，Serving 补 `delete`，Job 与 HPC 从「只提 create」改成完整命令面并点出 `job wait` / `job command`，Ray 与 Account 补齐 `batch` / `profile` / `quota` / `check` / `context`。新增 `inspire project` 一节——它是**全局对象、不按 Workspace 划分**，此前在 README 和 `SKILL.md` 的索引里都完全没有出现。

- **Browser API 文档合并成一份。** `browser-api.md` / `browser-api-actions.md` / `data-plaza-api.md` 三份 977 行合成一份 962 行，其中 589 行是表格——协议、探针方法、13 条路由 114 个 Action 的逐条参数与响应、创建面字段合同、非 Action 的那半边网关、数据广场，全在同一页里按章节走。数据广场此前单开一份，理由是「不是启智的一部分」；但它由同一层 Web Session 驱动、`dataset.ValidateDataset` 又要靠它的 code 才挂得上，分开看反而要在两份文档之间来回跳。

  **散落的 bullet 叙述改成表**：原来「参数语义与限制」下几十条并列的自然段，现在按「项 / 事实 / 读错的后果」三列排开，一条陷阱一行。**删掉的是迁移过程记录**——「曾经判定 X、后来发现是错的」这类只对当时的人有用，结论保留、过程删掉；`UpdateModel`、`GetUserQuotaJobs` 这些只在正文里出现过一次的名字用脚本对着老文档逐个查过，一个没丢。

- **`scan_v2_surface.py` 此前漏报了一整类路径。** REST 形状的提取锚在收尾的双引号上，而控制台有将近一半这类地址是模板字符串拼的（`` `${base}/api/v2/notebook/lab/${id}/` ``），于是 21 条只报出 12 条。**Notebook lab 就在漏掉的 9 条里**——也就是说当初判它「v2 没有对应物」时，本该发现它的工具正好看不见它。现在插值段按一个路径段匹配并归一成 `{}`，`/api/v2/notebook/{lab,code,events,open}`、`file/download`、两处附件下载、模型发布上传随之进入清单。

- **升级提醒改成对所有人都印**，不再需要 `INSPIRE_SHOW_UPDATE_NOTICE=1` 才开（这个环境变量随之删除）。它自 v6.3.0 起是 opt-in 且默认关着，理由是「别让升级元数据污染命令输出」——但代价是**没有任何人被告知过有新版本**，包括维护者自己。真正要守的那条不变量由另外两道闸守着，两道都还在：提醒只走 stderr（stdout 永远只有命令本身被要求的东西），并且 `--json` 下完全不印，所以「输出是单一 JSON 文档」这条合同没动。要整条关掉仍然是 `INSPIRE_SKIP_UPDATE_CHECK=1`。

### 修复

- **一次 Session 过期能被放大成多次 CAS 凭据提交，足以把账号送进锁定。** 这个客户端会自己登录：Session 在命令中途失效，某一层悄悄重新认证。问题不是它会登录，而是**没有任何一层知道别人已经登过了**——每一层只限制自己的重试次数，于是限制叠在别人已经花掉的预算上。数出来的放大路径有五条：

  - **跨进程只对「成功」去重。** Account 级文件锁的复用条件是「磁盘上有更新的 Session」，而那只有登录成功才写得出来。第一个进程登录失败之后，后面 7 个等待者依次拿到锁、依次提交同一份密码。新增的 8 进程测试把这条钉死了：去掉闸门 **8 次提交**，装上闸门 **1 次**。
  - **提交之后还会换条路再提交一次。** requests/CAS 路径把密码 POST 出去之后，遇到 5xx、连接断开、响应丢失、甚至本地缓存写失败，都会落回 Playwright 重走一遍登录——而服务端很可能已经收下了那次提交。
  - **上层 Wrapper 各包一层认证重试。** 资源可用性查询和节点计数在 `request_json()` 已经重建过一次 Session 之后，又 `clear_session_cache()` 从头再来一遍；`with_transient_retry` 则在每个 429 重试上把认证预算**重置**，一次被限流的扇出最多能买到三次登录。
  - **同进程内共享 Session 对象时会重复登录。** `_refresh_expired_session()` 拿「此刻的 `session.created_at`」当作失败的那一代，而另一个线程刚刚把刷新结果原地写了回去——于是**没人试过的新 Session** 被判成刚失败的那个，再登一次。
  - **401 先走一趟浏览器。** 同一份已经被拒的 Cookie 再送进 Playwright 问一遍，纯属延迟；而 `allow_redirects=True` 会把「网关 302 到 Keycloak」这个认证信号跟成一张 HTML 登录页，和数据无法区分——和上一条 `scan_v2_surface.py` 踩的是同一个坑，只是发生在客户端自己的传输层里。

  现在提交凭据只有一个入口。`login_with_playwright()` 是全 CLI 唯一把密码放上网线的地方，闸门就设在那里（`session/login_guard.py`），而不是设在调用方——调用方永远不知道别人花了多少。一次提交被拒之后，**同一份凭据**在本地被拒绝再次提交：按 Account 存一个不含明文、不含平台响应的标记，前台命令和其它并发进程读的是同一个事实。冷却按**连续**失败次数升级 60s → 5min → 15min → 30min（封顶），一小时无人再试则清零。解除条件是三者之一：冷却到期、有更新的 Session 落盘、或者**凭据变了**——指纹用 PBKDF2 派生，用户改完密码立刻能登，重新输入同一个错密码则照旧拦下。登录成功清标记、不计次（成功的提交不触发锁定，没有理由限流）。

  同批把叠加的那几层拆掉：`request_json()` 全程禁止重定向，401/3xx 直接进入**唯一**的重建边界，该边界的预算**跨 429 重试共享**；重建改成按调用实际发出去的那一代比较，别人刚换好的 Session 不再被当成失败品；资源可用性与节点计数的第二层重试删除。反过来也补了一处：登录成功但本地缓存写不进去时，Session 照常返回并只记一条 warning——把它当成登录失败，等于把平台刚认过的凭据丢掉再登一次。

  **验收在真实平台上跑过，也真的撞上了它要防的东西。** 用一个不存在的用户名做闸门验收：第 1 次 2.16s 打到 CAS，随后 10 次串行 + 8 个并发进程共 18 次全部在本地 0.17s 返回，标记里的 `failures` 始终是 1；等到冷却结束再试，第 2 次才放出去，冷却随即升到 5 分钟；改一个字符的密码则立刻放行。

  **写操作验收**（CPU资源空间 / 前沿课题探索 / CPU资源-2 / `0,1,4`，0 点券，用完即删）：把缓存 Session 的 Cookie 就地作废制造真实过期，再跑 `notebook create`——建成功了，`--debug` 日志里**一次重建、一次凭据提交**，没有 Playwright 的第二次提交。加压的那一轮更能说明问题：同一份失效 Session 上**同时**起 6 个进程（1 个写 + 5 个读），6 个全都识别出过期，**只有 1 个提交了凭据**，另外 5 个复用它刷出来的 Session。收尾 stop + delete，工作区回到原来的 4 台。

- **登录失败时平台给的原话，一直被 InspireSkill 自己丢掉了。** 参考竞品 [qzcli_tool](https://github.com/tianyilt/qzcli_tool) 的登录处理时挖出来的，它 v0.4.4 修过同一类问题的另一半。CAS 把失败原因写进 `<div class="form-error">`，而 `_extract_login_failure_hint()` 只认**可见**容器——抓真实页面看，三个登录 tab 面板在服务端**全是** `style="display: none"`，由 JS 在运行时决定显示哪个，于是「这个容器可见吗」是收到的 HTML 根本回答不了的问题，答错的代价就是平台原话全丢，用户只剩一句「检查密码是否正确」。今天误判成密码问题，根因就是这个。

  改成先读 CAS 真正的错误槽：失败时它往 `form-error` 里插一个 `<span name="error_fm1">` 承载文案（`error_fm1` 是密码表单，`error_fm2` 是短信 tab、`error_fm4` 是扫码 tab），干净页面里这个 span 根本不存在。这个锚点按表单区分、不依赖运行时可见性，也不会碰竞品踩过的那个坑——**「验证码」三个字在登录页上永远有 5 处**（全是旁边短信 tab 的固定文案），拿它判失败会把任何一次退回登录页都翻译成「需要验证码」。原来那条可见容器规则保留作兜底。实测：干净页读到空串，真实失败页读到 `必须录入用户名。 必须录入密码。`。

- **凭据错和临时被挡，用的是同一套冷却，而它们需要相反的处理。** 同样来自 qzcli 的实战教训——他们 2026-08-12 用一个被锁的账号换来的：对所有失败一律 60 秒冷却，于是密码一旦失效就每分钟自动送一次错密码，攒够次数 CAS 把账号锁死。InspireSkill 这边是 30 分钟封顶，一个密码错的定时任务照样每天送 48 次，而 **CAS 是按失败次数延长锁定的**。

  现在按平台原话分类（上一条修好之后才拿得到）：平台点名说凭据本身有问题（`账号或密码错误`、`账号被锁定`）就**不再按定时器恢复**，改成 6 小时的长扣留；限流、验证码这类「临时被挡」仍走 60s → 5min → 15min → 30min，因为它们自己会好。两种情况下改凭据都立刻放行——指纹变了就是变了。

  复审修掉分类器自己的一个盲区：它扫的是**整条报错文本**，而提交后才出现验证码的那条路径，文案里同时有平台引文「账号或密码错误」和「这是机器在被要求证明自己、不是账号问题」的结论——关键词一扫命中引文，一个明确说了不是凭据问题的失败被打上 6 小时长扣留，熔断文案还反过来宣称「平台说凭据本身有问题」，与触发它的那条消息自相矛盾。引文必须保留（那是平台的原话），所以分类不能再依赖文本：抛错方现在直接在异常上标注 `credential_rejection`，闸门**先读标注、读不到才回落关键词扫描**。被挡的场景照样记一次失败、走短冷却——提交确实花掉了，但它自己会好。

- **验证码检测改成要两个信号同时成立。** 误判成「要验证码」的代价是**彻底登不上**，比多花一次提交严重得多，所以宁可漏也不可错：既要密码表单 `fm1` 里有 `authcode`，又要页面引用一个**同源**的验证码图片。同源这个限定是必须的——模板里那个 `<img>` 指向 `mapp.suda.edu.cn`（苏州大学），是没清干净的死代码，在每一张登录页上都在。实测干净页两个信号都不成立、被挡时两个都成立。

  复审发现这条规则只装在了提交后的文案判断上，**提交前的硬停止反而只查字段一个信号**——后果最重的位置（拒绝登录、也不回落浏览器）用的是最弱的判据，方向恰好装反。现在提交前同样要求两个信号：字段孤立出现而页面不引用同源验证码图（一个休眠的模板残留正是这个形状）就照常提交，表单里带什么就交什么；真被挡的页面两个信号齐备照旧停下，即便漏判，提交后的那道检测也会接住并如实说明。真实平台复跑过一次登录：干净页不误报，正常放行。

- **登录页要验证码时，CLI 报的是「密码可能不对」，而且会照样把密码提交上去。** 上一条的验收撞出来的：连续几次失败登录之后，**CAS 会给这台机器的密码表单加一个 `authcode` 字段**（页面同时出现 `/cas/captcha.jpg`），requests 路径照旧把这个字段留空提交，CAS 回一张 `账号或密码错误。` 的登录页——于是**密码完全正确的账号被告知密码可能不对**，而每一次「再试试」都是一次注定失败的凭据提交，正好把账号推向真正的锁定。过一段时间这个字段会自己消失，登录恢复正常，实测前后两次抓的表单字段就差这一个。

  现在解析完登录表单就检查这个字段，**在提交之前**停下：既不提交，也不回落 Playwright（浏览器同样答不出验证码，只会再花掉一次尝试），并且**不打开熔断**——没提交过就没有「被拒绝的凭据」可记。报错直接说清楚平台在要验证码、这通常不是账号或密码的问题、去浏览器登一次即可解除，以及这次没有提交任何凭据。

  **闸门自审又挖出三个洞，一并堵上。**

  - **认证预算是 per `request_json` call，而一条命令是几十个 call。** 预算管住了单次调用内部的重建，熔断管住了**失败**的登录，两者都盖不住第三种：登录一直成功、但它铸出来的 Session 一直被拒——下一个调用拿到的是全新预算，熔断也看不出有什么不对。量出来是 5 次调用 5 次登录，扇出型命令会更多。现在多记一件事：某一代 Session 是重建产物、且还没有任何调用用它成功过，就不再为了替换它而登录（有调用成功过就清掉，所以一小时后真正的过期照样能重建）。

    复审在「清掉」这半边找到同一类代际混淆的镜像：成功回执记的是**回执那一刻**的 `session.created_at`，而共享 Session 对象可能在请求飞行途中被别的线程就地换代——一个用旧 Cookie 发出去、成功回来的迟到调用，会把别人刚铸出来、谁都没用过的新代际错误地记成「已验证」，于是下一个 401 又买到一次登录，保护每次都被重新解除。失败那半边修过的教训（比较调用实际发出去的那一代）现在对成功回执同样成立：记 `observed_created_at`，不记此刻。连带删掉 `request_json` 的 `_retry_count` 参数——生产代码里没有任何调用方传它，唯一作用是把预算归零，属于上一版重试结构的残留。
  - **`init` 直接调 `login_with_playwright()`，绕过那把账号级刷新锁。** 熔断标记的读-判-写因此不是原子的，两个并发的 `inspire init` 会同时读到「没有失败记录」然后各提交一次。现在闸门在自己的标记上加独立文件锁（和 Session 缓存锁、刷新锁三个不同的文件，不可能互相嵌套），8 进程直接调的测试：拆掉锁 8 次提交，装上 1 次。
  - **验证码字段可能是提交之后才出现的。** 提交前的检测挡不住这一种，而 CAS 回的还是「账号或密码错误」——正是我实测撞上的那个形状。现在失败时再解析一次响应页的密码表单，字段在就直说是机器在被要求证明自己，并且不再印「检查密码是否正确」那句误导。复审补齐了它的另一半：这道提交后检测原来只在 requests 路径上有，**Playwright 路径失败时同一张被挡页面仍被报成「检查密码」**——现在浏览器路径读的是同一份页面 HTML，同样给出验证码说明、同样标注为临时失败。

  这条修复由 [#73](https://github.com/realZillionX/InspireSkill/pull/73) 提出（@expectqwq），闸门位置、冷却档位和解除条件在合并时做了调整，并补上了它没覆盖的两条放大路径。

  连带修掉一个测试隐患：会话缓存和这个新标记都写在**当前 Account** 目录下，而测试套件跑在开发者自己的 `~/.inspire/` 上——模拟一次失败登录就会给开发者自己的账号写一个熔断标记，用的还是一个根本不存在的密码。现在 `conftest.py` 让会话存储在测试里解析不到 Account（要持久化的测试自己传 Account 名并隔离 `Path.home`），验证这条解析逻辑本身的 5 个测试改用 `active_account_session_storage` 显式退出。

- **`inspire update --check` 恰好在有新版本可升的时候报「检查失败」。** 实测本机 v7.1.0 对着刚发布的 v7.1.1：版本缓存正确写下了 `latest: 7.1.1`，而命令印的是 `✗ InspireSkill check failed.` 并以 1 退出——也就是这条命令唯一有意义的那个结果被它自己当成了故障。根因是检查路径把 `latest` 喂给了 `_audit_update_state`，而那个审计里的版本比较是**升级完之后**的验收器（「装完了，可执行文件真的变成新版本了吗」），于是「你还在旧版本上」这个正确答案被判成审计不通过。现在检查路径不传 `expected_version`，审计只保留它真正该管的那部分：可执行文件在不在 PATH 上、版本读不读得出来、全局 uv tool 有没有被钉在本地源、检测到的 harness 里有没有 `SKILL.md`。

  连带影响是每日那个 launchd agent（跑的正是 `update --check --silent`）一直在静默地以 1 退出——`launchctl list` 里那一列就是 `1`。`--check` 此前没有任何测试覆盖（现有的 8 处调用一律传 `check_only=False`），这个 bug 因此活了下来；现在三种情形各有一条测试：有新版本、已是最新、装坏了。

- **PyPI 响应被截断会让整个检查带着 traceback 崩掉**，而不是回落到 GitHub 上的 `pyproject.toml`。`http.client.IncompleteRead` 是 `HTTPException` 而**不是** `OSError`，所以它穿过了 `fetch_latest_version_info` 那个 `except (URLError, TimeoutError, OSError, JSONDecodeError)`。本机 `~/Library/Logs/inspire-skill-update-check.log` 里就留着这么一次崩溃。两个分支的 except 都补上 `http.client.HTTPException`，回落链因此真的能用——模块开头承诺的「失败时完全无副作用」现在对前台的 `update --check` 也成立，此前只有后台那条路径靠外层的兜底 `except Exception` 撑着。

- **13 条多行示例在 `--help` 里被压成了一整行。** 根因是 docstring 里的续行写成了单个 `\`——在非 raw 字符串里那是 **Python 的续行符**，两行在 Click 拿到之前就已经被拼掉，只留下续行处那串缩进空格。渲染出来是 `--workspace 分布式训练空间           --project <project>`，一行拖到两百多字符。涉及 `inspire`(根)、`job create`、`notebook create`、`hpc create`、`serving create`。写法本来就有正确的样板：`ray create` 用的是 `\\`，这批照它改。

  **`account add` 是另一个原因，症状相同**：它的 `\\` 一直是对的，但两条示例之间空了一行——而 Click 的 `\b` 只保护**紧跟其后的那一个段落**，空行之后就是新段落，于是第二条被照常重排。删掉那个空行即可。

  同一类还有 **`hpc create` 的「两层」要点列表整个缺 `\b`**，四行带缩进的 `*` 列表被 Click 重排成一段连续文本（`Two independent layers:   * Node-level: ... per     node; ...`），是这次可读性最差的一处。

- **`notebook metrics` 的正文整体多缩进 8 格。** 它的 help 是拼出来的：`metrics_shared.py` 里工厂函数的 docstring（嵌在函数里，缩进 8 格）后面直接追加一段 0 缩进的 `--now` 说明。`inspect.cleandoc` 脱的是**各行的公共缩进**，被这段 0 缩进的尾巴一压就成了 0，共享那四行于是原样带着 8 格印出来。改成先 `cleandoc` 再拼。同一份 docstring 在 `job` / `hpc` / `ray` / `serving` 上一直是正常的——只有 notebook 这条走了拼接。

- **`serving create` 的 `--replicas` 和 `--nodes-per-replica` 没有 help**，`--help` 里这两行只有一个 `[default: 1; x>=1]`。补的说明按 `browser-api.md` 已经记下的语义写：`node_num_per_replica` 是「每副本几个节点」的规格，`GetRecommendedConfig` 的四个 `min_*` 正是它和 `--quota` 的下限——也就是 `inspire model deploy-config` 印的那张表。

- **三处 docstring 讲的是一个用法行里不存在的参数名。** `notebook exec` / `notebook scp` / `notebook install-deps` 正文都写 `NOTEBOOK is the notebook name`，而 Click 印出来的用法行是 `NAME`。仓库里其余同类命令（`save-image`、`cancel-save-image`）写的就是 `NAME is the notebook name`，按这个改齐。

- **58 段正文卡在 `\b` 里，`--help` 因此不随终端宽度走。** `\b` 是 Click 的「这段别重排」标记，本来只该给换行有意义的内容用；包住散文的副作用是**把段落钉死在作者当初折的那个宽度上**——120 列的终端上留一半空白，70 列的终端上由终端自己在边缘断行，而且**续行会掉到第 0 列**，丢掉 Click 那两格缩进。去掉 `\b` 之后 Click 归一化空白、按当前终端宽度重排，70 / 100 / 120 列下都排得整齐、缩进保持。

  **`\b` 该留的地方一处没动**：示例块、`*` 要点列表、`Required fields after expansion:` 这类字段表共 **82 处**全部保留——它们的换行是内容本身（列表一项一行、示例里的 `\` 是 shell 续行符），本来就不该被重排。判据先用的是「首行不以 `:` 结尾」，漏掉了 `model status` / `project list` / `ray scaling` 三段——那三处的冒号只是句中标点，后面直接接散文，改按「段内有没有缩进行或列表项」重新判。

  **docstring 源码仍按仓库惯例折在约 79 列**（中位 71 / 90 分位 79，`ruff line-length = 100`）。中途一版把每段合并成源码里一整行，最长 551 列、34 行越过 100——那是拿 Markdown 的写法套 Python，已回退：`cli/inspire/cli` 里超 100 列的行数与改动前同为 94。这一层怎么折对输出**没有任何影响**，因为非 `\b` 段落的空白 Click 一律归一化。

  **验收是逐字比对**：182 条命令的 help 正文与 921 条 help 字符串各自把空白全部归一化后前后对比，**0 处字词变化**；源码回折那一步再单独比一次渲染结果，**0 字节差异**。示例仍是 284 条全部解析通过。2783 tests / ruff / mypy 全过。

  **验收是全量渲染前后逐字对比**：182 条命令的 `--help` 各渲染一遍，前后只有上述 10 处 hunk 变化，没有一处旁落。另有两项自动核查这次**没有再查出问题**，结论记在这里免得重跑：help 文本里的 284 条 `inspire ...` 示例拿 Click 命令树逐条解析（未知子命令、未知选项、位置参数个数）全部通过；help 里写着 `default: N` 的选项对着真实 `default` 逐个比对，差异全部是 `--limit` 这类「Click 侧 `None`、取值在下游兜底 20/100」的有意设计，以及 `job wait --timeout` 的 `14400` 秒即正文说的 4 小时。

## v7.1.1

### 新增

- `inspire notebook save-image --flatten`：把保存出来的镜像压成单层，而不是在起点镜像上再堆一层（[#71](https://github.com/realZillionX/InspireSkill/issues/71)）。默认仍是分层保存。

  **先验了它是不是真生效**，因为 discovery 声明过、网关收下了，都不等于平台照做——这仓库已经记过两个「接受、不生效」的死字段。拿一台新建的 CPU Notebook 连存两次，再从 `docker-qb.sii.edu.cn` 那台 Harbor 把两份 manifest 读回来比对：分层保存是基底那 7 层逐个 digest 原样保留再追加一层、共 8 层 331.78 MB；压平保存是 **1 层 286.90 MB**。

  **压平反而更小**（-13.5%），因为被后面层覆盖或删掉的内容不再随镜像走。而多出来的 22 秒（33.4 s → 55.5 s）落在镜像的 `CREATING` 上，**不落在 Notebook 上**——两次都在 t≈33 秒把容器还回来，所以这个开关不会让 Notebook 多停一秒，命令的提示也照这个说。压平出来的镜像另建了一台 Notebook 确认能起，验完连镜像带 Notebook 都已删除。

  同一个 Action 上还查到 `accessible`(int32) 和 `support_brand_list` 也在合同里。`accessible` 只有两档（个人可见 / 公开可见），顶不掉 CLI 三档可见性存完再调 `image.UpdateImage` 那一步，因此没有顺手换过去。

  另有一个同名易混的 `flatten_mode`（`FLATTEN_OFF` / `FLATTEN_ON` / `FLATTEN_AUTO`）在 `CreateNotebook` 的合同里，管的是「停机自动保存时压平」这条独立链路。**控制台里那组单选是 `disabled` 的**，平台侧没放开，所以刻意不接，只把结论记进 Action 表。

- `inspire serving shell`：进 Serving 实例的交互式 shell，默认进第一个运行中的副本（副本跑的是同一个镜像和命令，除非就是某个副本在出问题，那就 `--instance` 点名）。四条实例 PTY 至此齐了。

  **Serving 连句柄参数都不叫 `job_id`，叫 `inference_serving_id`。** 这是拿 `job_id` 打了两次被拒才发现的——错的键不给报错，只把握手拒成一个光秃秃的 `HTTP/1.1 200 OK`。`_PTY_ROUTES` 因此把句柄键也参数化了。

- 记录两条 `file` 服务的实测结论（只有文档，没有行为变更）：

  **`GetSftpgoConnectionInfo` 是一条不需要计算资源的共享盘读写通道。** `{storage_name}`（池名转小写）换回一台 WebDAV 服务器，根下就是容器里那套 `/inspire/<池>/…` 全命名空间。写侧在自己的个人目录下做过完整往返：`PUT` 201 → `GET` 200（内容逐字节一致）→ `DELETE` 204 → `GET` 404，探针文件已清除。目前 CLI 的文件流转只有 `notebook scp`，要一台运行中的 Notebook 加容器内 sshd 加 rtunnel，这条路把三样都省掉。**响应里的 `auth` 是明文凭据**，须与 Notebook Proxy 的 token 同等对待——不进日志、不进报错、不进 `--json`、不进文档。尚未封装。

  **`CreateCopy` 不是服务端复制，是一张要审批的申请。** 控制台里它叫「新建数据传输」，表单只有 `source_path` / `target_path` / `overwrite`，提交按钮写的是「提交审批」，和旁边的 `audit` 服务是一条链。`ListFileCopyTasks` 是用户级的（不认 workspace / filter），本账号 0 条。

- 记录训练任务「预检」的实测结论，并说明为什么不接（只有文档，没有行为变更）。`ListPreCheckItems` / `GetPreCheckResult` 不是「提交前校验规格」那种东西，而是**每个训练任务创建时可选开启的节点健康检查**——创建面收 `enable_troubleshoot` 加一份 `pre_check_items`，没开的任务读结果直接答 `train job does not enable troubleshoot precheck`。而 `train_enable_troubleshoot` 在全部 10 个可见 Workspace 都是 `False`：平台侧没放开，接出来是一个谁也用不了、我也验不了的开关。

  顺带记下同族另外三个能力位和它们各自在哪些 Workspace 开着，其中 `train_enable_specified_nodes` 是真正的节点绑定，**和我们已有的 `--exclude-node` 不是一回事**。要接这一族里的任何一个，先读这四个位。

- `inspire ray shell`：进 Ray 实例的交互式 shell，默认进 **head**——驱动在那儿，`ray status` 和集群自己的日志也在那儿；要看某个 worker group 的进程用 `--instance <Role-Rank>`。走 `/api/v2/ray_job/instances/exec`。

- `inspire hpc shell`：进 HPC 实例的交互式 shell，和 `job shell` 同一套（`exit` 退出、`Ctrl+]` 断开）。**默认进 `launcher`**——`srun` 在那儿跑，也只有那个 Pod 看得见你的进程；`slurmctld` 是调度器本身，`--instance slurmctld` 才去。走 `/api/v2/hpc_jobs/instances/exec`，同样是网关 REST 形状的那一半。

  **参数名不能照搬 train 那条**：`hpc` 和 `ray` 都只认 `instance_id`，用错的两种失败都不给报错——`hpc` 收到 `instance_name` 会照常 upgrade 然后一个字节都不回（实测 `instance_id` 回 53 字节，这边回 0），`ray` 则直接把握手拒成一个光秃秃的 `HTTP/1.1 200 OK`。`serving` 平台侧也有实例 PTY，没验证过，`build_remote_cmd_ws_url` 对它直接抛错而不是猜一个参数名。

- `<workload> quota` 增加 `Points/h` 列（`--json` 里是 `points_per_hour`）：该 Quota 行每实例每小时消耗多少点券。数据本来就在配额目录的响应里，只是一直被丢掉。**只有 GPU 计费**——所有 CPU-only 行都是 0，同一份预处理放进 `CPU资源空间` 就不花点券；GPU 按卡型定价，实测 H100 / H200 是 1 点券/卡/小时而 4090 是 0.33，差三倍。按实例计费，`--nodes 2` 跑 8 点券的行是每小时 16。`null` 是「平台没给这行定价」，和 `0`（免费）是两件事。

- `scripts/scan_v2_surface.py` 补上 `/api/v2` 的第二种形状。`?Action=` 之外还有一片 **REST 风格**的路径（当前 12 条），只认 Action 的清单会把它们报成不存在——`job shell` 走 v1 的那条 `/api/v2/train_job/remote_cmd` 就在里面，而我们的边界测试里写着「v2 没有任何 Action 暴露 shell」，这句话对 Action 成立、对 `/api/v2` 不成立。同批还有 HPC / Ray / Serving 三条实例 PTY（我们完全没有对应命令）、文件页的目录操作、日志下载。**这几条都还没验证**：网关在路由之前先鉴权，普通 GET、带 Referer 的 GET、真实 WebSocket 握手三种打法对 v1（确知可用）和一条随手编的路径回的都是同一个 401，要确认只能拿一个自己的运行中任务去握手。已把结论和未验证状态写进 `browser-api.md` 与边界测试的白名单理由。

- `scripts/scan_v2_surface.py`：把控制台前端产物里写死的 `/api/v2/{route}?Action=` 全抓出来，和 `GET /discovery` 对账，`--probe` 再按 `browser-api.md` §7 的判据逐个探活。这是这次唯一抓出我们自己错误结论的东西——配额目录被判成「v2 无对应物」正是因为只查了 discovery，而 `resource-price` 整条路由不在里面。当前 25 条路由 / 187 个 Action，discovery 是 11 / 175。只探 `Get` / `List` / `Search` 开头且不含写动词的 Action，空请求体在校验阶段就被拒，建不出任何东西。

- `resources usage --group <关键词>`：把「谁占着」收窄到计算组，也就是任务真正提交进去的那个单位。整个 Workspace 看着满、你要投的那个组未必满，反过来也一样，所以这个判断本来就该在组这一层做。关键词是子串（`--group H200` 覆盖所有带这个硬件的组），输出顶部列出实际匹配到哪几个，`--json` 里是 `compute_groups`。底层的 `ListTaskDimension` 一直收 `logic_compute_group_id`，只是没有命令用；平台确实认这个过滤（实测 1125 → 183 行）。`--mine` 读的是按项目预聚合的记录，里面没有计算组，两者互斥。

  同族的 `job.GetLcgUsedComputeResourceJobs` 看着更像是为这件事准备的，实测不值得接：对同一个组两边逐条吻合（同样 183 个任务 id、同样 1400 张卡），而任务维度还额外带着用户、项目和 GPU 利用率——那个 Action 没有。它唯一多出来的行是 TensorBoard，而 TensorBoard 一张卡都不占。

- `project detail` 增加点券花在哪儿：`Spent` 拆成 `on training` / `on inference` / `on storage` / `on private workspace`（`project.GetProjectBudgetUsageOverview`）。此前只有总额和余额，中间那笔差额去了哪没人答得上。逐项目实测它的 `remain` 与 `GetProjectDetail` 的 `remain_budget` 一致，所以这是同一个数的展开，不是第二个说法；这一次请求失败不影响详情本身照常打印。

### 破坏性变更

- **`project list --json` 的 `remaining_budget` 键消失**，拆成 `my_remaining_budget` 和 `project_remaining_budget`。原因是那个键报的是**当前账号**的额度而不是项目的，而键名和列名都没说：实测同一个项目里项目还剩 233,107 而本账号只有 337——差 690 倍，按前者做决定会直接撞上「预算不足」。表里对应 `My Budget`（决定你的任务能不能起的那个数）和 `Project Budget`（全体成员共用的池子），平台不给成员额度时两列相同。读这个键的脚本必须改：**它不是被重命名成某一个新键，而是被拆成语义不同的两个**，照旧取「余额」会静默拿到另一个数。

- **`inspire cache refresh` 不再接受裸形式**：不带 `--resource` / `--workspace` / `--name` 会直接报错并给出收窄的写法，此前这样敲会刷全部。刷一遍全部是几百个请求，读的还是几乎不动的目录，而正常情况下这条命令根本不需要跑——Workload 名字后台一直在补，其余的解析一次就自己缓存了。真正要跑的场合只有一种：你知道缓存底下的东西变了（管理员改过计算组规格、镜像在网页上被删了）。先 `cache status` 看哪个 Scope 真的不对，再只刷那一块。把裸形式写进脚本或定时任务的要改成点名刷。

### 变更

- `resources usage` 的表里用 `Reclaimable` 换掉 `GPU Busy`。利用率回答不了这条命令要回答的问题——卡在谁手里跟它忙不忙没关系，持有者就是持有者；能被拿走的只有低优卡。新列是这个人持有的 GPU 里有多少落在以可抢占优先级提交的任务上，`--by task` 另给一列 `Prio` 显示提交原值。判据跟着 Workspace 的优先级合同走：公平调度空间小于 `4`，其余空间 `≤3`（后者拿平台按计算组给的口径逐组核对过，4 个组全中）。读不到合同时是 `-` 不是 `0`——「没有可抢的」和「不知道能不能抢」导向相反的决定。`--json` 里 `gpu_usage_rate` 照旧给，新增 `low_priority_gpus` 和 `priority`。

  数据一直在 `ListTaskDimension` 的行里（`priority` 字段），我们从来没读过。**它和 `resources availability` 的 `Reclaimable` 不保证对得上**：那一列是平台自己按计算组算的，非公平空间里两边逐组精确一致，公平调度空间里对不上，而且差额在两次调用之间自己会变（实测同一个组 100 → 72）。所以这一列只说「按提交时的优先级，这些卡属于哪一档」，不声称复现平台那个总数。

  **`Prio` 和 `job status` 里那个优先级不是同一把刻度，别互相印证。** 维度行给的是**提交值**（`--priority` 的 1–10 档，实测线上取到 1 / 3 / 4 / 6 / 8 / 10），`Reclaimable` 和 `Prio` 用的都是它；`train.GetJob` 回的是平台**存储值**（本账号四个任务实测提交 10 全部存成 35，配 `priority_level: HIGH`）。两者之间没有验证过的换算——四个数据点只覆盖一档，所以 CLI 不做反查，`job status` 原样回显存储值并同时给出 `priority_level`，那一列才是可解读的部分。

- 后台补 Workload 名字改成增量：只读列表最新的那一头（平台按创建时间倒序返回，`job` / `hpc` / `tensorboard` 已逐一实测），读到一页全是已知的就停，而且**只合并、不对账**。此前每 5 分钟把 10 个 Workspace 里全部 1458 个历史任务重新拉一遍，只为了缓存名字——一趟 6.3 MB，折合每小时 76 MB。现在这一趟 86 个请求 / 35.8 s / 8 MB → **70 个请求 / 5.3 s / 2.1 MB**。

  后台因此永远不会删掉缓存里的行。平台那边消失的任务靠 TTL 自己掉出去——没人再刷新它的 `expires_at`，5 分钟后就查不到了；用 CLI 删的当场打墓碑。代价是「网页上删掉的任务名还能解析 5 分钟」，而完整对账（能立刻清掉平台不再列出的行）改成只有手敲 `cache refresh` 时才做。冷缓存不受影响：`known` 为空时增量那一趟本来就会一直翻到 `total`，第一次仍然是完整的。

  `cache status` 里 Workload 那几行因此常态显示 `partial`——后台只读了最新的一头，没做完整扫描。同一处顺便修掉一个假话：这些 Scope 的 `updated` 此前只看 `last_full_refresh_at`，于是一分钟前刚补过的 Scope 被印成 `never`。

- 缓存 TTL 从两档改成三档，按东西实际变多快排：Workload 名字（`job` / `hpc` / `ray` / `serving` / `notebook` / `tensorboard`）仍是 5 分钟；账号结构（`workspace` / `project` / `compute-group` / `model`）从 30 分钟拉到 1 天；目录类（`image` 和 `quota-<workload>`）从 30 分钟拉到 7 天。Quota 行是管理员在计算组上配的硬件档位，镜像目录是共享 Registry 的内容，两者都几乎不动，却恰好是这里最贵的两项读取——Quota 是「每个计算组 × 每个 Workload 一次请求」的扇出，镜像是每个 Registry 好几兆的目录。TTL 同时是读有效期：过期只会让一次解析回落到 Live，那总是安全的；长档换来的风险在另一个方向——平台已经删掉的规格或镜像可能还会被缓存报出来直到 Scope 过期，`cache refresh --resource <kind> [--workspace <name>] --full` 是随时可用的对齐手段。

### 修复

- `serving create` 回显的镜像引用把 tag 拼了两遍：`sandbox-base:ubuntu24.04-py3.12-1.0.0:ubuntu24.04-py3.12-1.0.0`。`ImageInfo.name` 对平台发布的镜像本来就带着 tag，代码又接了一次 `version`。创建本身没坏（请求体走的是 `mirror_id`），坏的是 `--dry-run` 和 `--json` 报出一个解析不到任何东西的引用——而那正是有人会抄进脚本的那个字符串。

- `cache refresh --resource model` 把 9 个 Workspace 上挂了两天的陈年错误清掉了（底层那个 `page_size=-1` 的 bug 早已修好，只是每个 Workspace 要各自刷一次才清）。没有代码变更。

- `cache status` 不再把「刷新过、在有效期内、却一个名字都拿不出来」印成 `ready`。这正是配额目录出事时的状态——每个 `create` 都被拒，而看板显示的是最健康的那一档，故障因此完全看不出来。现在这种资源报 `empty`。判定只按整个资源画，不按单个 Scope：一个 Workspace 里没有 Notebook 是正常的，按 Scope 判会对几乎每个账号误报。

- `job shell` 从 `/api/v1/train_job/remote_cmd` 迁到 `/api/v2/train_job/remote_cmd`，这是最后一处非自举的 v1 依赖，边界测试的白名单因此只剩 `session/auth.py` 一条。此前留着的理由写的是「v2 没有任何 Action 暴露 shell」——对 Action 成立，对 `/api/v2` 不成立：PTY 走的是网关 REST 形状的那一半，不带 `?Action=`，所以按 Action 名做的清单一直把它报成不存在。用一个 1 卡低优的一次性任务实测：v1 与 v2 各握一次手、各发一条 `echo`，**两边逐字节相同**（各 45 字节），验完即删。等价所以不留回落。

- **`<workload> quota` 和每一个 `create` 会因为一份空缓存全线报「没有配额」。** 实测 `job quota` 在全部三个工作空间返回 `No quota rows found.`，而平台侧行都在——也就是说 CLI 建不出任何 Workload。根因在 `CachedPricesLoader`：Scope 只要标着「已完整刷新」，读到的记录哪怕一条没有也照样当答案，于是每个计算组都权威地回空。真实触发是 Scope 键变过之后遗留的 150 条记录读不到，而 Scope 仍标着完整。

  整份目录一行都没有不是工作空间的回答，是缓存出了事故，现在读作未命中并回落 Live。**单个计算组的空依旧权威**——那是「这个组不跑这类 Workload」的正常事实。代价是真的一行配额都没有的工作空间每次要按组回源，这一侧值得错：多几个请求，换的是不会有一个建不了东西的 CLI。已用真实缓存复现：同一个坏状态下未修复版报空、修复版正常。

- 镜像目录按 Registry 读一次，不再按 Workspace 各读一遍。`registry_hint: {workspace_id}` 指的是一个 Registry，不是一份按 Workspace 切分的目录，多个 Workspace 正常共用同一个：实测本账号 10 个 Workspace 只有两份目录，7 个用 `qbHarbor`、3 个国产卡空间用 `sjHarbor`，组内 `image_id` 集合逐字节相同。此前后台每轮把那份约 5400 个镜像的目录重复下载 7 遍——一轮完整刷新 51 MB 里的 42 MB、120 s 里的 68 s 都是它。现在先用一行探针（`page_size: 1`，读响应里的 `registry_id`，约 80 ms）问出每个 Workspace 属于哪个 Registry，同一个 Registry 只读一次：`image` 一轮 30 个请求 / 42.0 MB / 82.9 s → 16 个请求 / 6.2 MB / 11.8 s。探针答不出来的 Workspace（Registry 里一个公开镜像都没有）照旧单独读，绝不会被当成和别人同一份。

  `inspire image --help` 此前写着「An image saved by `notebook save-image --workspace X` is only visible under `--workspace X`」，这是错的：同一个 Registry 上的任何一个 Workspace 都看得到它。真正会挡住人的是 Registry 边界，而这条线基本沿着卡的类型走——国产卡空间和 NVIDIA 空间读的是两份不相交的目录。

- 测试不再读运行 pytest 那台机器上真实的 `~/.inspire/` 名称索引。任何一次名称解析都会为当前账号打开索引，没有重定向时那就是开发者自己的库：`test_workload_quota_and_resources` 明明把平台打了桩，却会拿到开发者自己的计算组，同一个测试通不通取决于他最近跑没跑过 CLI——TTL 拉长之后这件事立刻暴露出来。conftest 里加一条 autouse 重定向，和已有的几条「别碰真实 home」同一个理由。

- 每次 HTTP 请求不再新建一个 `requests.Session`，连接因此能复用。此前 `_request_json_once` 每次调用都 `build_requests_session(...)`，用完 `finally: http.close()`，于是每一个请求都要重新建 TCP 连接、重新握手 TLS——走本地 SII 代理连 `qz.sii.edu.cn` 实测每请求约 300 ms，复用连接后约 30 ms。平常一条命令察觉不到，但 Name 缓存把很多操作变成了扇出（Quota 目录每个计算组一次、镜像每个 Workspace 三次、后台刷新一轮几百次），这一项就变成了主要开销。同一份完整的后台刷新，实测网络时间 201.5 s → 97.2 s、整体 202.8 s → 120.4 s；扣掉本来就是传输量瓶颈的镜像，单请求 397 ms → 91 ms。共享的只有连接池，Cookie / Header / 代理设置每次调用照旧重设，所以刷新过 Session 之后不会拿旧凭据作答；需要自己 `close()` 的调用方继续用 `build_requests_session`。

- `cache refresh` 的 `model` 刷新一直在失败，而且每 5 分钟重试一次。它是唯一给 `ListModels` 传 `page_size=-1` 的调用点，而这个 Action 不像 `ListImages` / `ListLogicComputeGroups` 那样接受「-1 表示全要」，回的是 `InvalidParameter: page or page_size invalid`。失败不推进 `last_full_refresh_at`，于是这个 Scope 在「够不够新」这个问题下永远是 due，后台每次醒来都对 10 个 Workspace 各试一次——本机这个账号已经这样白跑了两天多。现在按 `total` 翻页，`list_models()` 的默认值也从注定失败的 `-1` 改成 100。

- 刷新引擎给每个 Scope 加了尝试节流：一个 Scope 两次**尝试**之间至少隔它自己的 TTL，无论上一次是成功、报错还是只读到一半。此前只看「读到的数据够不够新」，而报错和 incomplete 都不推进 `last_full_refresh_at`，所以坏掉的 `model` 每 5 分钟重试、被限流的 Quota 扇出也每 5 分钟重跑整个扇出——恰恰在平台正在推回来的时候跑得最勤。读侧不受影响：`scope_due()` 的语义没动，缓存不新鲜时该走 Live 还是走 Live。`cache refresh --full` 仍然能立刻强制。

- 后台刷新按批量翻页，不再按界面翻页。五个 Workload 的 fetcher 都写死 `page_size = 100`，一个存了约 1400 个任务的 Workspace 因此每 5 分钟要 14 个来回；网关的上限是 `MAX_PAGE_SIZE` 且会自动收敛，所以调到 1000 只会更少请求、不会更少行。实测 `job` 一轮 24 → 11 个请求。

- 打开索引时删掉全局资源类型上残留的按 Workspace 分区行。`project` 在 v7.0.0 前按 Workspace 存，之后 `scope_workspace_id()` 会把它的 Workspace 抹平，于是老库里那些带 Workspace 的 `project` 行既刷不到也查不到，只会让 `cache status` 多报几个 Workspace。和已有的「删掉本版本不认识的资源类型」同一处、同一个理由。

- 配额目录从 `/api/v1/resource_prices/logic_compute_groups/` 迁到 v2 的 `resource-price?Action=GetLogicComputeGroupResourceSpecPrices`。这是挡在每一个 `create` 前面的那个请求——把 `-q 1,20,200` 翻成平台要的 `quota_id`，也是 `<workload> quota` 的数据源，此前是仅存的几处 v1 依赖里唯一一处在关键路径上的。此前判定「v2 没有对应物」是照 `/discovery` 下的结论，而 `resource-price` 整条路由根本不在 discovery 里——正是我们自己文档里写着的那个错误模式。两侧请求体相同、响应键相同：10 个 Workspace × 全部计算组 × 5 种 `schedule_config_type` 共 225 组比对，225/225 行集一致，字段集无差异。随之删掉最后一个 v1 转发助手。

- `resources nodes` 的整节点空闲数不再把调度不上去的节点算进去。`resources availability` 的 `Free Nodes` 一直在排除 `cordon_type` / `is_maint` / `resource_pool=fault`，而同一份 `ListNodeDimension` 数据在 `get_full_free_node_counts` 里只判了 `status=READY` 且无任务——同一个「整节点空闲」在两处有两个定义，而 `resources nodes` 恰恰是提交多节点任务前用来看放不放得下的那个视图。实测这三个字段在现网真的会被置上（`CPU资源空间/HPC-可上网区资源-2` 436 个节点里 101 个带 cordon），只是目前被 cordon 的节点同时也不是 `READY`，所以暂时没有暴露成错数；判据现在收敛到一个 `_node_is_schedulable_and_idle`，两处共用。

## v7.1.0

### 新增

- `inspire dataset list|show|validate`：数据广场（`aip.sii.edu.cn`）的目录、版本和挂载前校验。这是一个和启智并列的独立平台，只共用同一套 CAS SSO，控制台侧边栏的「数据集」就是外链过去的——启智那侧根本没有检索接口，只有一个把某个版本挂进容器的 `dataset.ValidateDataset`。CLI 用现有 Session 里的 CASTGC 走一次 CAS 握手换 `datasets-session`，纯 HTTP，不起浏览器。

  **数据集用名字寻址，不用数字 ID。** 数据广场内部的 `datasetId` / `versionId` 拿去挂载会被拒为「数据集不存在」；认的是 `pixabay-81k` 和 `v0` 这样的 code。`list` 因此把名字当第一列，数字主键只留在 resolver 里；`show` 的每个版本行直接给出可粘贴的 `--dataset` 值和容器内路径。列表还给出 Access 一列：全平台 531 个数据集里有 106 个当前账号无权挂载，不先看这一列就会在创建时才撞上「无访问权限」，而申请权限只有网页端有入口。版本号不保证是 `vN`，`v1-br`、`2026-07-30`、`v3again` 都真实存在，不要按序号猜。

- `notebook create`、`job create`、`hpc create` 支持 `--dataset <数据集名>:<版本名>`，可重复。挂载点固定为 `/inspire/dataset/<数据集名>/<版本名>`，只读，不占项目共享盘配额，也不归 Path Alias 管。创建前平台会逐条校验并解析真实存储路径，任何一条不通就整体失败，不会先建出一个缺数据的 Workload。`ray` 和 `serving` 没有这个选项：平台直接拒绝该字段，网页端对应表单也没有这一项。

- `job create` 支持 `--env KEY=VALUE`（可重复）、`--keep-after-success` / `--keep-after-failure`、`--description`、`--fault-tolerance-retry-interval`。环境变量此前只能拼进启动命令；保留时长让任务结束后容器停在保留态等待，可以直接进去看现场，比重跑一次便宜得多。值可能是凭据，所以 CLI 输出只回显变量名。

- `notebook create` 支持 `--auto-stop-after MINUTES`（平台侧运行时长计时器，与 `--auto-stop` 的空闲判断是两回事）和 `--enable-notification`；三类 Workload 都支持把项目公共目录降级为只读，`notebook` 另有项目成员目录只读一档，后者还要求当前账号是项目 Maintainer，否则平台直接拒绝创建。默认全部保持关闭，不传时的请求体与此前逐字节一致。

- `hpc create` 支持 `--max-time`、`--keep-after-finish`、`--description`，并且 `--enable-notification` 不再被硬编码成关闭（默认值仍是关闭）。

- `inspire ray start`：停掉的 Ray Job 保留完整集群规格，此前 CLI 有 `stop` 却没有 `start`，停了就只能重建。平台在这里会「受理但不执行」——请求返回成功而任务纹丝不动，再调一次变成 `InternalError`，所以命令以状态真的离开 `STOPPED` 为准，没动就报失败。

- `inspire serving scale|versions|rollback|api-metrics`：副本伸缩、部署历史、按历史版本重新部署，以及请求量 / 成功率 / 延迟 / TTFT。`api-metrics` 和既有的 `serving metrics` 是两套不相交的指标——后者看资源占用，只有前者能把「没人调用」和「一直调用一直失败」分开。

- `inspire model deploy-config`：某个模型版本能被部署的最小节点规格，正好是 `serving create --quota` 的下限，同时给出 vLLM 兼容判断。

- `notebook status`、`job status`、`hpc status` 显示实例挂了哪些官方数据集，以及各自在容器里的路径。此前 CLI 能设不能读：建的时候可以 `--dataset`，建完想知道「这里面到底有什么数据」只能回网页看。平台在 `GetNotebook` / `GetJob` 里一直回显这份信息，只是没接。它同时给出容器路径而不是平台内部存储路径——后者命名的是用户既不寻址也用不上的内部布局。

- `inspire tensorboard create|list|status|start|stop|delete|tags|scalars`：TensorBoard 从「Job 底下一条只读列表」变成完整的命令组，因为它在平台上本来就是一等对象——计算组在 `support_job_type_list` 里单独声明 `tensorboard`，控制台给它独立页签，它既能挂在训练任务上也能对任意一个 summary 目录单独建。写侧的四个 Action（`Create` / `Start` / `Stop` / `Delete`）不在 discovery 里，是探针探出来的。

  **真正的收获是 `tags` 和 `scalars`。** board 的 `url` 不是内部路径，而是一个真的能打的 TensorBoard 应用，同一个 Session cookie 直接认，于是 `data/runs` 和 `data/plugin/scalars/*` 都能读成 JSON。Agent 因此可以自己建一个 board 指向训练目录，然后把 loss 曲线、eval 指标当数字读回来——首尾值、step 区间、最小最大值，`--points N` 再要最后 N 个点——不需要浏览器，也不需要有人替它去看一眼图。点按 step 排序而不是按 event 文件顺序，因为续训和多 worker 写出来的序列在文件里是交错的。

  规格由平台固定成 1 CPU / 2 GiB，所以没有 quota 也没有镜像要选；唯一的放置输入是计算组，而且必须声明 `tensorboard`——`分布式训练空间` 里有几个训练组没有声明，CLI 在发请求前就挡下来。自动停机上限 72 小时。`create` 另外挡住两件平台会照单全收、但收完这个 board 就废了的事：不给名字（建出来的行 Name-only 的 CLI 再也指不到）和不给 summary 路径（建出来什么都读不到）。

- `inspire dataset tags`：列出 `dataset list --tag` 接受的全部 52 个标签及其所属模态。标签名是固定的中文词（`视频生成`、`具身智能`……），猜不出来，此前唯一的发现路径是故意填错一个再去看报错里的候选。

- `inspire hpc logs`、`inspire serving logs`、`inspire ray logs`：补齐「每种 Workload 都能读到程序输出」这条线上最后三个缺口——此前只有 `job logs`，HPC、Serving 和 Ray 的容器输出在 CLI 里根本看不到。三条命令与 `job logs` 共用同一套记录与字符预算（默认 100 条 / 16,000 字符）和同一份 `--json` schema，实例筛选一律用 `instances` 已经在打印的角色/序号而不是被 `scrub_raw_ids` 洗过的 pod 名。平台侧有三个坑：日志端点的实例名要带命名空间（HPC 裸名报「expect 1, but got 0」，Ray 干脆静默回空）；`page_size=N` 保留的是**最旧**的 N 条，所以「最后 N 条」必须先取满窗口再在客户端截尾；时间窗超一个月平台答 `InternalError`，而这个码在瞬时错误名单里，不在客户端 clamp 就会先白烧三次退避重试再抛出一条看着像平台故障的错。

- `inspire resources usage --workspace <名字> [--by user|project|task] [--mine]`：共享集群上「卡被谁占着」此前从 CLI 完全看不出来——`resources availability` 只回答「还剩多少」。现在按用户、项目或任务报出存活工作负载持有的 GPU / CPU / 内存，以及其中有多少真的在忙（`gpu.used` 是死字段恒 0，`usage_rate` 才是活的）。平台的 `ListProjectDimension` 在 10 个可见工作空间上全部返回空，是权限地板而不是 scoping 写错，所以按项目聚合改在客户端折叠任务维度的行；`--mine` 走 `ListUserDimension`，它只答调用者自己的行。

- `inspire resources policy --workspace <名字>`：报出每类 Workload 的空闲回收规则与运行时长上限。第二天回来发现 Notebook 没了、任务在某个时刻被杀，此前只能猜；这些都是平台明确声明过的配置，只是从来没接进 CLI。AND/OR 条件按原样呈现而不是拍平，Serving 的规则按 GPU 档位逐条给。平台在部分工作空间对 HPC 返回字面量 `null`，渲染成「未声明」而不是「无限制」——那两者是相反的结论。

- `inspire ray scaling` 与 `inspire serving scale-history`：弹性伸缩是 Ray 存在的理由，而「`min_replicas` / `max_replicas` 到底动没动过」此前看不到；Serving 同理，副本数变化是排查延迟突增的第一手材料。两条都做成独立子命令而不是塞进 `status`，因为它们是需要 `--limit` / `--all` 预算的增长型集合。

- `inspire model status` 现在会说出哪些推理服务还占着这个模型版本、整个模型有没有排队中的部署、以及哪些别的版本还在跑。此前从 CLI 完全看不出一个版本有没有人在用，换版本或删模型只能盲操作。只报仍可能起来的服务（`RUNNING` / `STOPPED` / `SLEEPING`），已经失败的不算「有人在用」。

- `inspire notebook save-image` 在发出保存请求之前先报平台估算的快照大小，并新增 `--dry-run` 只估不存；非 RUNNING 的 Notebook 在任何保存请求之前就被拦下。存镜像是一段该 Notebook 不可操作的等待，此前按下去之前完全不知道要等多久、会产出多大的东西。估算失败不阻断保存，只是不打印那一行——取不到的大小读作「未知」，不读作 0。

- `inspire notebook metrics --now`：给出 CPU / 内存 / GPU / 显存的当下快照。此前只有历史时序，而「这个 Notebook 现在到底在不在用卡」需要的是当下的值。

- `inspire hpc events` 支持 `--instance` / `--all-instances` 的实例级事件，并把重复发生的事件折叠进 `Count` 列（job 级也生效）。平台在 HPC 事件上从不填 `count`，而是按发生次数逐行重复：一个失败任务 106 行事件里 `--tail 20` 全是同一条 BackOff，折叠之后 20 行才露出真正的死因。只出现一次的行完全不变。

- `inspire notebook cancel-save-image`：中止进行中的 Notebook 存镜像并立刻把 Notebook 交还。存镜像是一段该 Notebook 不可操作的等待，此前一旦按下去就没有退路。受控验证过两次——保存开始 1 秒时、以及 38 秒后平台已经打出「已提交镜像层，等待推送」之后，**都能成功中止**，Notebook 回到 RUNNING。代价要知道：半成品镜像会以 `FAILED` 留在镜像目录里，得自己删。

- `inspire model delete`：CLI 此前能注册模型却不能删，与仓库自己的清理纪律矛盾。删除前逐版本核对推理服务占用与排队中的部署，有占用就点名拒绝，`--force` 放行；占用探测失败**拒绝删除**而不是当作没占用。

- `inspire dataset applications`：只读查看数据集权限申请与待我审批的条目，状态显示为可读词。提交与审批仍然只在网页端——那两个动作会以你的名义触达真人审批者，CLI 不接。

- `<workload> quota` 新增 `Priority` 列，`job` / `notebook` / `serving` 的 `create` 在提交前拒绝该档位不接受的优先级。平台在工作空间的调度记录里逐档声明 `allowed_priority_levels`——训练区的 1/2/4 卡是 `["low"]`，8 卡满节点不限——此前 CLI 把这四档平等地列出来，用户按 `--priority 4` 提交要等平台拒绝才知道，而拒绝理由看不出与优先级有关。这条知识以前是一段按组名里有没有「训练区」三个字来推断的硬编码提示，现在改成读平台，那段提示已删除。

  三种状态严格区分：`any`（平台声明不限）、`low`（只能低优先级）、`unknown`（菜单没读到）。**读不到不等于不限**，所以 `unknown` 既不显示成 `any`，也不阻断创建——一次平台抖动不该让一个可用的配额看起来不可用。

- `inspire notebook create` 提交前挡住工作空间内的重名。**平台自己不校验重名**——实测用同一个名字连建两个都成功，而重名会让此后每一条 `notebook <动词> <名字>` 都变成歧义、必须 `--pick`。校验是大小写不敏感的，也会忽略尾部空格。探测失败时让路、不拦创建。

- `job / hpc / notebook / serving status` 报出工作负载落在哪些节点，`job / hpc / ray / serving instances` 新增 `Node` 列给出每个 Pod 的落点。此前 CLI 能回答「要了几个节点」却回答不了「是哪几个」——而排查坏节点、复现某次实验、判断掉队的是哪个 Worker，问的都是后者。平台在这些详情里一直回显落点，只是投影层把它当内部字段丢掉了：`train.GetJob` 有 `node_infos[]`（外加请求侧的 `specified_nodes` / `exclude_nodes`，后者正是 `job create --exclude-node` 传进去的那份），`hpc.GetJob` 有 `nodes[]`，`GetServing` 把 `node_names[]` 放在 `extra_info` 里，四个 `ListJobInstances` 族的行都带 pod 级的 `node`。

  **节点名不是平台 handle。** `qb-prod-4090-gpu105` 这类名字是基础设施身份，人和平台同学都按它对话，所以它照常输出；同一行里的 `instance_id` 仍然被洗掉。空的节点清单读作「还没被调度」而不是「查不到」——排队中的任务 `node_count` 有值而落点为空，两者不相等是正常的。

- `notebook status` 的 `Node` 一并给出该节点的健康状态，被 Cordon 或处于维护窗口时标出。**STOPPED 的 Notebook 不会清空节点对象**，而是把名字置空、状态置成 proto 零值 `UNKNOWN_NODE_STATUS`，照直读会印出一个「状态未知的节点」——投影按空名字判定未落点，这一行随之消失。

- `inspire serving events` 补上实例级：输出多一列 `Instance`（`rank=N`），并支持 `--instance` 收窄。部署级事件是控制器的话（`CreatingRevision` / `GroupsProgressing` / `Pending`），实例级才有 `Scheduled` / `Pulled` / `Started` / **`Unhealthy`**——「副本起来了但健康检查一直不过」这个最常见的部署故障，此前在 CLI 里一个字都看不到。平台侧走同一个 Action 换 `filter.object_type`，Pod 级的 `object_ids` 必须是带命名空间的实例名，裸名答 `InternalError`。

- `serving logs` / `serving events` 的 `--instance` 收 `serving instances` 打印的 `rank=N`（或裸 `0`），与 `job` 同一套规则。

- `inspire ray events` 补上实例级：输出多一列 `Instance`（head / worker 组名），并支持 `--instance` 收窄。**Ray 的事件本来就是两级都给的**——一次 `ListJobEvents` 里既有 `object_type: "job"` 的 `CreatedRayCluster` / `CreatedService`，也有 `object_type: "instance"` 的逐 Pod 行——CLI 此前把 `object_id` 丢掉，于是 17 行事件谁都不知道来自哪个 Pod。收窄走平台的 `filter.object_ids`，不是客户端过滤。

  这条推翻了仓库里一条记错的事实：`ray.ListJobEvents` 的 `filter` **是有效的**，此前记的「没有 `object_type`，传了返回 `参数错误`」在真实任务上不成立（拿不存在的任务去探，平台先答 `ResourceNotFound`，看不到字段层的真相）。为定论专门建了一个最小 CPU Ray 集群（1 CPU / 4 GiB，head + 1 worker），量完即 `stop` + `delete`。顺带发现事件时间戳只到秒，同一容器的 `Pulled` / `Created` / `Started` 常常同秒、而平台的同秒次序还随 filter 变，所以排序加了 `id` 作 tiebreaker——否则「倒着取一屏再翻回来」会把因果顺序翻反。

- `inspire resources node-events <节点名>...`：**唯一按节点而不是按工作负载组织的事件源**。工作负载的 Events 只说平台对这个任务做了什么，说不了机器本身发生了什么——内核 OOM kill（`kernel-monitor` 上报的 `TaskHung`、`Memory cgroup out of memory`）、Cordon / Uncordon、`Rebooted`、`InvalidDiskCapacity`、`NodeNotSchedulable`。「同一台机器上反复失败」此前在 CLI 里没有任何可查的东西，实测一台 4090 上有 149 条、一台 HPC 计算节点上有 88 条 Warning。

  `cluster.*` 这条路由对普通成员基本全是 `AccessForbidden`，**这个 Action 是例外**，本账号读得通。契约有三处得记住：`filter.node_names` 事实上必填（不给 filter 答 `total: 0`，读起来像「集群很安静」而不是「你什么都没问」）；行里的类型字段叫 `event_type` 而不是别处的 `type`，共享渲染与 `--type` 过滤都在一个地方吸收这个差异；平台声明的 `start_last_timestamp` / `end_last_timestamp` 时间窗答 `InternalError`，所以时间收窄留在客户端。节点名不认识时回空列表而不是报错，因此帮助里明说「查不到不等于机器没问题，先核对拼写」。

### 破坏性变更

- **`inspire project` 整组不再接受 `--workspace`。** 项目根本不按 Workspace 划分：`ListProjects` 不带 `workspace_id` 就是全局目录，`GetProjectDetail` 只认项目自己的 id，那个 `--workspace` 是个过滤器却被写成了 `required=True`。实测这个账号全局 4 个项目，扇出 10 个 Workspace 拿回来的还是同样 4 个——扇出唯一多产出的是一列 `Workspace`，而它是靠「逐空间查一遍看谁答得出来」反推的（全局调用里 `space_list` 是空的），10 个请求换一列截断到看不清的文字。现在 `project list` 一次调用给出全部项目，`Workspace` 列随扇出一起删除；`project detail <名字>` 直接寻址，名称候选也从全局目录取——按 Workspace 收窄只会把一个在别处可见的项目报成「找不到」。

- **`--workspace all` 收敛到「按名字找东西」这一类。** 还接受扇出的只剩 `<workload> list`（`job` / `notebook` / `hpc` / `ray` / `serving` / `model`）和 `account permissions`——不知道东西在哪个 Workspace 时，本来就没法先给出空间名。`resources availability` / `resources nodes`、`<workload> quota`、`serving configs` 一律改为要一个 Workspace 名字（本轮新增的 `resources policy` / `resources usage` 从一开始就按这条规则发布）：档位目录、计算组余量、回收策略、部署配置面和当前占用都是按 Workspace 定义的事实，扇出不会多回答一个问题，只会把逐空间的行拼起来再按输出预算截断，把「前 N 名」悄悄变成「最先枚举到的那个空间的前 N 名」。随之删掉的还有只为扇出存在的 `Workspace` 列与 `show_workspace` 分支，以及 `get_accurate_resource_availability` 的 `all_workspaces` 参数和它的多空间目标解析。

  **跨 Workspace 的扇出保留在它真正有意义的地方**：`<workload> list`（不知道东西在哪个空间时按名字找）、`<workload> quota`、`serving configs`、`account permissions`、`cache refresh`。分界线是拼接诚不诚实——这些命令逐空间给一行或一段，`Workspace` 列能分辨归属；`resources` 给的是本来就该逐个读的单空间事实。

- **`events` 的默认口径改成和 `logs` 一样：不加参数就给全部。** `job` / `hpc` / `ray` / `serving` 的 `events` 现在把控制器事件与每个实例的 Pod 事件合成一条时间线，`--all-instances` 随之删除（默认即是），`--instance` 从「切换到 Pod 视图」变成「收窄到某个实例」。旧的默认视图由新的 `--workload-level` 保留——只要控制器那一半，与 `--instance` 互斥，并且跳过实例枚举，所以它也是最省请求的一条路径。

  两条命令此前口径不一致是有原因的，只是那个原因不该由调用方承担：**平台根本没有「工作负载级日志」这一层**（日志端点只按 Pod 名取），所以 `logs` 天生就是全实例聚合；而事件有两套**不相交**的视图——控制器的 `Unschedulable` / `SuccessfulCreatePod` 和 Pod 的 `FailedScheduling` / `Pulling` / `BackOff`——CLI 早先把前者当默认、后者做成开关。于是「这东西为什么没起来」在 logs 上问一次就够，在 events 上要先问一遍、发现没有线索、再加个 flag 问第二遍，而**第二遍才是答案所在的地方**。

  代价照实说：`hpc` 的 `ListSlurmdPodEvent` 一次只收一个实例，默认读全部就是每个 Pod 一次请求（实测单次约 0.3 秒），所以这条路径改成线程池并发（上限 8），结果按输入顺序重组而不是完成顺序。`job` 那一侧一次请求可以带 200 个 Pod，不受影响。

- **`job events` / `job logs` 的 `--instance` 改收 `inspire job instances` 打印的身份**（`rank=0`，或者就写 `0`；角色名选中该角色全部实例），不再收平台 pod 名。

  旧取值形态**从来就没有取得途径**：训练 Pod 的平台名是 `job-<uuid>-worker-0-0`，`scrub_raw_ids` 洗成 `<redacted>-worker-0-0`，`job instances` 正因为这个丢掉 Name 列改印 `rank=N`——也就是说这个选择器唯一合法的值是 CLI 任何地方都不打印的东西。`hpc` 和 `ray` 早就用角色名解决了同一个问题，`job` 这次跟上。选择器认不出来时报错并列出该任务真实有的实例，而不是悄悄退化成空范围（那会读成「这个实例没有事件」）。

- `inspire image save` 移到 `inspire notebook save-image`。选项、输出、退出码和 `--json` schema 一个字没变，只换了命令路径，**不保留别名**，所以脚本和 Agent 合同要跟着改。（同组的取消命令是本轮新增的，从未以 `image cancel-save` 发布过，只会以 `notebook cancel-save-image` 出现。）

  归属本来就错了，三处都指向 notebook：这三个动作的平台 Action 全在 notebook 路由上（`SaveNotebookImage` / `EstimateSaveMirrorSize` / `CancelSaveMirror`），`image` 服务下一个都没有；`image` 组其余命令的 NAME 是**镜像名**，而 `save` / `cancel-save` 的 NAME 是 **Notebook 名**——同一个命令组、同一个参数位、两种名词，对 Name-only 的合同是实打实的陷阱；`--workspace` 在这个组里同样有两种含义，`image save --workspace` 指的是 Notebook 所在空间，而镜像 registry 的空间是另一回事。被操作的对象也确实是 Notebook：它在保存期间进入 COMMITTING、不可操作，产物才是镜像。

  没有沿用平台自己的 `CommitNotebook` 叫法：那是控制台的「停止并保存」，镜像名由平台按 `<基底>:stopsave-<notebook>-<hash>-<时间戳>` 自动生成，调用方选不了也猜不到，与这条命令不是一回事。

  `references/notebook.md` 现在承载保存镜像这条动线，`references/image.md` 只留保存之后的可见性与清理并指过去。

### 变更

- **`inspire update` 会扫掉旧版本留下、当前版本已经不读的本地状态。** 停用某个状态文件的那个版本没法在退场时删掉它——知道它存在的代码正是被删掉的那部分——于是这些文件会永远留在 `~/.inspire` 下。本机实测积了五处：`jobs.json.legacy`、`.environment-normalized-v3`、`events/`（34 个文件、956K）、`accounts/<n>/project_list.json`、`accounts/<n>/config.toml.bak-7897`，当前代码里没有一行读它们。

  新增的 `inspire/accounts/state_inventory.py` 是当前版本拥有哪些路径的唯一声明，`update` 拿它和磁盘对账。写了新状态文件却忘了在这里登记，它就会被报成孤儿并提议删除——吵闹但可恢复，好过无声累积。删除前必然先打印清单：交互模式下询问，`--yes` 跳过询问，`--json` 和后台每日检查只报告不删除（前者没有可回答的人，后者没有人）。已经是最新版时 `update` 照样扫，所以它也是随时手动跑这件事的入口。`metrics/` 里的图是用户明确要过的产物，不参与清扫。

  清单模块是在升级装完之后才 import 的，所以读到的是**新版本**的声明而不是当前进程这一版的——正好是想要的那份，它才知道新版停用了什么。代价是把一个本进程没有依赖过的模块加载进来，因此扫描的任何失败都被吞掉：清扫是顺带的家务，绝不能把一次已经成功的升级变成 traceback，下次 `update` 会重试。另外这个能力从本版起才有，而驱动升级的是**升级前**那个版本，所以装上本版的那一次升级本身不会清扫，之后每次都会。

- **`inspire account check` 会发现本仓库钉住了一个平台上已经不存在的 Project。** 仓库的 `[context] project` 只在 `inspire init` 时写一次，之后再不复查；平台上把这个 Project 删掉或改名之后，仓库就钉在一个解析不到任何东西的名字上，这里的每一条 `<workload> create` 都会栽在它上面。账号级的 `project_catalog` 帮不上忙——它是写下这个 pin 的同一次 `inspire init` 留下的缓存，和 pin 口径一致地一起错，只有实时列一次才看得出来。判定为失效时报 `Project context: STALE` 并退 `EXIT_CONFIG_ERROR`（不是认证错误：账号是好的，是这个仓库的绑定坏了）。提示同时给出两种修法：重新绑定用 `inspire init --scope project`，而本来就不该有绑定的仓库应该把 `./.inspire/` 删掉——[`project-context.md`](references/project-context.md) 早就写明 CLI、Skill、文档这类通用工具源码仓库不做项目初始化，这类仓库要的是解绑而不是改绑。列表调用本身失败时不作判断——网络问题不是失效的证据。

- **删除 `inspire config` 整个命令组，其中管账号的部分并入 `inspire account`。** `config check` → `account check`，`config context` → `account context`；`config show` **没有替代命令**，理由见下一条。归属本来就错了：schema 里 15 个 option 有 10 个是账号作用域（Authentication / API / Proxy / Tunnel），它们全部由 `account add` 写入 `~/.inspire/accounts/<name>/config.toml`。`config context` 更直接：它列出的 project 和 compute group 就是 `inspire init` 的发现结果写进账号配置的那份缓存，只有 workspace 名单是实时查的。

  剩下的 5 个仓库级 option（`job.*`、`notebook.post_start`）不再有查看命令。它们照常生效，只是没有专门的表格：值写在 `./.inspire/config.toml`，同名环境变量默认优先，`[cli] prefer_source = "toml"` 反转优先级。一个只为 5 个键存在的命令组，撑不起顶层一个名字。

  有效代理诊断（原 `config show --filter Proxy`）落在 `account check --details`：账号 `[proxy]` 块、Shell 的 `http_proxy` / `NO_PROXY`，合并之后 requests / playwright / rtunnel 三条链路各自走直连还是走代理。这一段任何配置文件里都没有。

- **`config show` 的账号视图没有替代命令，因为那张表不是信息。** 它每一行要么恒真，要么被别的输出覆盖：`INSPIRE_USERNAME` / `INSPIRE_PASSWORD` 只会显示 `<configured>`，因为没配的话 `account check` 早就带着「Run `inspire account add`」失败了；`INSPIRE_BASE_URL` 现在有正确默认值，永远显示已配置；4 个 Proxy 行说「账号文件里有代理」，而 `account check` 的有效代理段落说的是「这个代理到底赢没赢」——后者严格更强。一张每行 1 bit 且这 1 bit 你已经知道的表，不值得占一个命令名。

- **`account check` 失败时自己把话说完。** 此前不带 `--details` 的失败只会印一行 `Authentication: FAILED`，理由和实际路由都要再跑一次才看得到——而这一次失败往往就是网络或代理出的问题，重跑的成本正好落在最不该付的时候。现在失败时无条件附上认证错误原因和有效代理路由（照旧全部脱敏），`--details` 仍然额外给来源优先级和配置文件存在性。输出同时补上当前账号名，因为「我到底在用哪个账号」是排查的第一个问题。

- **`account context` 不再列 `accounts`。** 那一段和同组的 `inspire account list` 逐字重复。

- **删掉 config schema 上没人读的三个字段和四个查询函数。** 两个视图命令删掉之后，`ConfigOption.description`、`.category`、`.default` 和 `get_categories` / `get_options_by_category` / `get_option_by_env` / `get_options_by_scope` / `CATEGORY_ORDER` 全部零消费者——它们唯一的读者就是那两张表。其中 `.default` 不只是死代码，而是第二份事实来源：真正生效的默认值在 `load_common._default_config_values()` 里，两处各写一遍，上面那条 `base_url` 的修复就得同时改两个文件才不漏。现在只有一处。补了一条不变量测试：每个 option 的 `field_name` 必须在 loader 的默认值表里存在，否则解析完会被直接丢掉——正是这类漂移的失败形态。

- **占位主机报错现在说得出是哪个主机。** 原消息印的是完整 URL，而 URL 在出口会被脱敏成 `<redacted>`，于是用户拿到一条「检测到占位主机」却看不出哪里不对的错误。改成印匹配到的主机名（`api.example.com` 这类固定文档占位符，本身不敏感）。

- **删除 `inspire config env`。** `config env use .env` 和 `inspire init --scope project --env-file .env` 调的是同一个函数，纯重复；模板生成器 `config env [--template]` 只是把 schema 里的 description 重排成注释，没有回答任何 `--help` 回答不了的问题。`show` 的 `--format env` 和 `--compact` 随命令组一起消失：前者对账号级 10 项只会印 `# INSPIRE_USERNAME=<configured; redacted>`，后者只是同一张无信息量的表再压一遍行。

- **来源标签 `global` 改叫 `account`。** 这一层读的就是 `~/.inspire/accounts/<name>/config.toml`，叫 `global` 与另一个同名概念（`inspire init --scope global`）撞车。`--json` 里 `source` 字段和 `account check --details` 的 `Config files:` 一行同步改口。

### 修复

- **`job wait` 把「已经停掉」读成「还会动」，对着一个永远不会再变的任务轮询四个小时。** 它自己抄了一份终态集合，抄的时候漏掉 `job_stopped`——`job stop` 停下来的任务就停在这个状态。命令打完一行 `Status: job_stopped` 之后再没有任何输出，直到默认四小时超时才报 `Timeout`。而 `job` 根本没有 `start`：终态就是终点，要再跑只能重新 `job create`，所以这段等待从第一秒起就注定失败。`--help` 里那句「until it reaches a terminal state (SUCCEEDED, FAILED, or CANCELLED)」是同一个错误的另一半——它把 STOPPED 明确排除在终态之外，等于告诉调用方一个停掉的任务还值得等下去。终态集合现在只有 `_JOB_TERMINAL_STATUSES` 一处定义，`job wait` 和 `job logs --follow --source ssh`（漏的是同一个值）都引用它；help 改成写清真实终态、退出码，以及「只对 PENDING / QUEUING / RUNNING 等待」。

- **`job logs --follow` 读平台日志时从不看任务状态，任务结束后继续空转。** 同一条命令的 SSH 路径一直会在任务进终态时收尾，平台路径——也就是默认路径——只是 `while True` 地轮询，一个已经结束的任务会让它永远挂在那里，不再输出任何东西。现在它同样按终态收尾，收尾前多取一轮日志接住最后几行，并说明为什么停了。

- **`events --follow` 和 `job list --watch` 的 help 没说它们不会自己结束。** 这两条确实是「跑到被中断为止」，任务结束也不退出；`--watch --active` 更是连一个已经终态的任务都不会再出现。此前 help 只有「Follow the event timeline and print new events」和「Continuously refresh job list」，把它们挂进后台当「等任务跑完」用是很自然的误读。六条 `events --follow`（`job` / `hpc` / `notebook` / `ray` / `serving` / `resources node-events`）和 `job list --watch` 现在都写明这一点，手册也补上了「等待只对还会自己往前走的状态有意义」这条判据和各 Workload 有没有 `start` 的对照。

- **`base_url` 的默认值是一个 `config check` 自己判为非法的占位主机。** `https://api.example.com` 在七处被硬编码成 base_url 的默认或兜底值，而 `config check` 又把 `example.com` 列为占位主机直接报错——同一个值既是默认值又是错误条件。后果落在最不该出错的地方：全新用户按 issue 模板跑第一条 `inspire config check`，拿到的是「Placeholder host values detected」，值还被脱敏成 `<redacted>` 看不出哪里不对，而真正该给的「Missing platform credentials. Run `inspire account add`」永远不会触发，因为占位检查排在凭据检查前面。真值 `https://qz.sii.edu.cn` 此前只存在于 `account add` 的交互 prompt 里。现在它是 `inspire.config` 里唯一的 `DEFAULT_BASE_URL`，配置默认值、运行时兜底、登录入口和 `inspire init` 写出的账号模板全部引用它；`example.com` 只剩下识别用户手输占位值和修补旧配置文件两个用途。`base_url` 本身仍可配置。

- **`inspire init` 写出的账号模板里，`${{VARNAME}}` 多了一层大括号。** `[remote_env]` 那段注释教用户用 `"${VARNAME}"` 从本地环境取值，但模板是按字面量写盘的，用户照抄注释就会拼出一个取不到值的变量名。

- **`image register` 的默认模式永远不会成功。** `--method push` 发的是 `add_method=0`，平台一律回 `InvalidParameter: no image uploaded`——那是控制台的「文件上传」，要先真的传一个镜像 tar，而 CLI 根本没实现上传。真正能用的是 `--method address` 走的 `add_method=2`，也就是控制台的「本地推送」：平台留一个 `<name>:<version>` 的位置并回一个镜像地址，你 `docker push` 上去。两个名字正好接反了，而 `docker tag` / `docker push` 提示偏偏只在那个必然失败的模式里打印。

  现在只保留这一条能走通的路：`--method` 整个删掉，命令固定申请槽位，并把 `docker login` / `tag` / `push` 三行连同「推上去之前镜像一直是 FAILED」一起打出来——不打这三行的话，这条命令留下的就是一个没人知道怎么用的空位置。login 的主机名从平台回的地址里取，三行自洽。

- **`image detail/delete/set-visibility` 传一个不带版本的名字，回的是「找不到」。** 镜像的身份是 `name:version`，`mostar-u1-runtime` 这种裸名永远匹配不上，可 `_resolve_image_name` 的 docstring 一直写着「裸名会匹配任意版本并走歧义列表」——没有的事。现在报出这个名字下真实存在的版本（`Existing: ...:v1, ...:v2, ...:v3`），并且只报一次：原本那条通用「找不到」被压住，否则一个错误会印两段。真不存在的名字仍然回「找不到」。

- **`image list` 没有关键字过滤，而一个 registry 里有五千多个镜像。** 控制台镜像列表自带名称搜索，CLI 这边只能靠 `--limit` / `--all` 翻页，等于找不到。新增 `--keyword`，大小写不敏感的子串匹配。

- **`notebook save-image` 把平台给的原因吞了。** 最常见的失败是 `Conflict: Duplicated image name and version: <name>:<version>`——换个 `-v` 就好——CLI 只说「Could not save notebook as an image.」。现在把平台点名的原因带出来，重名时还给出改版本号的完整命令。`image register` 的失败同样带原因。异常原文仍然不整段外抛，只回显认得出的那一截，因为它同时带着请求体。

- **镜像的 Visibility 一栏读的是 `source`，不是 `visibility`。** 这两个字段都在每条镜像记录里，含义完全不同：`source` 是镜像被构建进哪个 registry 命名空间，`visibility` 才是谁能看见它、才是 `set-visibility` 写的那个字段、也才是 `--source public` / `--source private` 实际筛选的依据。从 Notebook 存出来的个人镜像一律是 `SOURCE_PUBLIC` + `VISIBILITY_PRIVATE`，于是 `image list --source private`（网页端「个人可见镜像」）把整张表的 Visibility 全标成 `public`——正好和事实相反。`image list`、`image detail` 和 `notebook save-image` 的回读现在都取 `visibility`；官方镜像自己没有这个字段，仍按 `source` 认。

- **`image list --source` 少了一整类：网页镜像选择器有四个页签，CLI 只有三个。** 「项目可见镜像」（`VISIBILITY_PROJECT`）在 CLI 里既列不出来也设不了，`--source all` 也扫不到——`CPU资源空间` 里有 2 个这样的镜像，此前按名字根本解析不到。`--source` 增加 `project`，`image set-visibility` 和 `notebook save-image --visibility` 同步增加 `project` 一档。

- **在 `job shell` / 受限 Notebook 的 `notebook shell` 里敲 `exit`，远端 shell 结束了，本地进程永远不退。** 网关不会因为远端 shell 死掉就关连接——实测敲完 `exit` 之后 40 秒内既没有 close frame 也没有 EOF，一个字节都不再来，于是客户端一直阻塞在 `select` 上。唯一能脱身的是 `Ctrl+]`，而它在任何一处 help 里都没写过。

  现在 shell 自己宣告退出：bootstrap 不再 `exec` 那个 shell，而是把它当子进程跑，父进程在它结束后 `printf` 一个标记。标记字面量在 bootstrap 里被引号拆成两段，所以终端回显的那行命令不含连续的标记，只有 shell 自己的 `printf` 会吐出来——这是敢直接匹配它的前提。客户端跨帧扫描这个标记（只扣下真正构成标记前缀的那几个字节，否则每次按键回显都会被延迟），看到就干净退出，标记本身不进终端。`job shell` 和 JupyterTerminal 那条走同一套。两条命令的 help 也写上了 `exit` 和 `Ctrl+]`。

- **公开镜像删不掉，而 `image delete` 只说「Could not delete image.」。** 平台在镜像转 public 之后就不再把创建者当属主：`AccessForbidden: 您没有权限删除该镜像。`——既删不掉也改不回 private，只有平台管理员能清理。旧消息读起来像一次可以重试的失败。现在报出这是单向操作，`set-visibility` 的 help 和 [`references/image.md`](references/image.md) 也写清楚了这一点：放开可见性之前先确认这个镜像值得长期留着。

- **`job logs` 会打乱同一毫秒内的输出。** 平台的 `time` 是纳秒精度，`timestamp_ms` 是四舍五入到毫秒的；排序用的是后者，于是一次 `nvidia-smi --format=csv` 的表头和数据行并列成同一个键，日志存储怎么给就怎么印——实测数据行印在了表头前面，`ls` 的三行也被打散。现在按 `time` 的亚毫秒部分排序。

- **`job quota` / `notebook quota` 把 Compute Group 一列截断到 28 列，而 `--group` 恰恰只认逐字相同的全名。** `分布式训练空间` 里 `开发区-H100-cuda12.8版本-119核` 和 `...-183核` 只差后缀，两条的 quota 三元组还不一样（`1,10,200` 对 `1,20,200`），在表里全都显示成 `开发区-H100-cuda12.8版本-...`，等于这张表印出来的值没一个能直接拿去用。该列改为按内容自适应宽度。

- **`notebook create` 传了一个不存在的 `--project` 会打出完整 Python traceback。** `job create` 和 `hpc create` 早就是一行报错，只有 notebook 这条路径让 `ConfigError` 一路冒到顶层，43 行里 42 行是栈。

- **`job create --dry-run` 从不报告 `--max-time`、容错三件套和 `--framework`。** 这些字段会照常解析、照常提交，只是计划里一个字都不提——而手册明确说 dry-run 用来核对容错的最终生效值。`job batch --dry-run` 更彻底：help 说它「print plans」，实际只印一串名字，`--json` 里也漏掉 `dataset`、`env`、`description` 和保留时长。两者现在都把提交什么就印什么（`env` 只印变量名，值可能是凭据）。

- **`notebook lifecycle` 用平台时区，其余命令用本机时区，同一个瞬间差 12 小时。** `ListRunIndex` 是唯一一个回裸墙钟字符串而不是 epoch 的 Action，CLI 原样打印；同一次启动在 `events` 里是 `13:58:03`，在 `lifecycle` 里是 `01:58:03`。现在按平台时钟（+08:00，无夏令时）换算到本机时区。

- **`notebook status` / `job status` 把创建时间印成裸 epoch 毫秒**（`Created: 1786816657000`），而同一份数据在 `list` 里一直是正常时间。`--json` 仍给 epoch。

- **`notebook status` 读不出「剩余运行时长」和优先级。** `--auto-stop-after` 设的那个计时器此前没有任何回读入口，平台的 `left_time` 一直在回；优先级则是 CLI 找错了层级——Notebook 把它放在 `project` 下面，网页端「优先级」列就是从那里读的。现在 `Auto-stop In` 和 `Priority` / `Priority Level` 都出现在 `status` 里。

- **`notebook events` 的 Type / Reason / Count 三列永远是空的。** Notebook 的生命周期事件只有 `{time, message}`，没有 K8s 那套分类；三列 `-` 白占宽度，还暗示存在并不存在的筛选。事件表改为只渲染这批记录真的带着的列，Job / HPC 那侧不受影响。

- **路径脱敏会把 `/inspire/<storage>/...` 这类占位路径拦腰截断。** 豁免规则写成「不匹配以 `/inspire` 开头的路径」，于是扫描到 `>` 之后又把剩下的 `/...` 当成一条新的绝对路径抹掉，`notebook scp` 对受限 Notebook 的提示里就带着一个 `<redacted>`。改为整条路径匹配完再按首段决定去留。

- **等待 Notebook 超时时报的是 `Notebook '' did not reach RUNNING`。** 消息里插的是平台 handle，而 CLI 会把 handle 从所有对外文本里洗掉，于是名字位置就空了。改用 Notebook 名字。

- `notebook exec` / `notebook shell` 的 help 补上 SSH 型 Notebook 首次要跑一次 `connection refresh`：受限 H100/H200 走 JupyterTerminal 不需要任何准备，同一条命令在两类机器上的前置条件不同，此前只有 `scp` 的 help 写了这件事。

- **`hpc create` 会提交一份 Slurm 规格，平台收下、返回 job id，然后什么都不跑。** 节点级（`slurm_cluster_spec`：买几个什么样的节点）和 Slurm 级（`sbatch_script`：程序怎么用这些节点）在平台上互不校验，**控制台也不校验**——它的「最大值」提示来自项目的单任务配额而不是所选规格，而且那几个输入框排在「选择规格」之前，结构上就没法比。受控验证（`CPU资源空间` / `HPC-可上网区资源-2` / `0,4,16`，12 个任务）测出三种形态：

  - `--cpus-per-task` 超过一个节点的核数（一个任务跨不了节点），或者单节点上所有任务的内存超过节点内存 → 任务跑约两分钟后 `FAILED`，**`hpc logs` 是空的、`hpc events` 只有正常的 Pod 生命周期，没有任何一处带上 sbatch 的拒绝原因**。
  - 任务总数乘每任务 CPU 超过买下的节点总核数 → sbatch 反而**收下**，step 永远排在队里，平台一路报 `RUNNING`，直到 Workspace 自己的运行时长上限（该空间是 10 天）把它停掉。这就是「假装成功但一条命令都没执行」。

  现在这三条在提交前就被拒绝，报出算式和该改哪个参数。**默认值也一起改了**：`--cpus-per-task` / `--memory-per-cpu` 不给时按「一个节点上落几个任务」推导，此前是「一个任务独占整个节点」——于是只调 `--number-of-tasks` 而不动其它参数，必然造出上面第二种永久排队的任务。单任务单节点的默认值与此前逐字节一致。`hpc batch` 走同一个 resolver。

- **`hpc status` 不报 `steps`，于是「跑完了」和「什么都没跑」长得一模一样。** 正文没有 `srun` 的任务照样 `SUCCEEDED`——sbatch 会在第一个节点上执行 body——只是不产生 Slurm step，多节点时其余节点全程空转。`GetJob.steps` 是唯一能区分这件事的字段（`0/0` 对 `1/1`），平台一直在回它，CLI 从来没接。现在人读和 `--json` 都给出 `Steps`；`create` 在正文里找不到 `srun` 时也会警告。顺带修掉一个投影 bug：`-/1` 这种形状会被路径脱敏读成绝对路径，`--json` 里输出成 `-<redacted>`，正好把这个字段抹掉。

- **`hpc create --dry-run --json` 的 `priority` 永远是 `null`。** 计划投影按参数名 `task_priority` 去请求体里取，而请求体里的键叫 `priority`——于是一份真的带着优先级出发的请求，在计划里看起来没有优先级。人读那一行读的是局部变量，所以只有 `--json` 是错的。

- **`priority` 这个键漏掉时，平台答的是 `InternalError: internal server error`。** 它在瞬时错误名单上，于是先白烧三次退避重试，再抛出一条看起来像平台故障的错。`build_hpc_create_payload` 此前把它当可选字段；现在拿不到优先级就直接报「拿不到优先级」，不再把这个键漏出去。同样地，`CreateJobConsole` 回来没有 job id 时不再打印「创建成功」。

- **`sbatch_script.memory_per_node`（控制台「每节点使用内存」）查过了，不接。** 它看起来是个正常字段：平台收下、存住、详情页照原样显示「每节点使用内存：8G」、`GetJob` 完整 round-trip，控制台的执行命令模板写的甚至就是 `#SBATCH --mem=*G`。但平台的脚本生成器只会写 `--mem-per-cpu`——只给 `memory_per_node` 时生成出来的是一行空的 `#SBATCH --mem-per-cpu=`，sbatch 直接拒掉整个脚本。8 GiB、15 GiB、16 GiB 在同一个 16 GiB 节点上全部 `FAILED`，而等价的 `--mem-per-cpu` 任务成功；两个字段同时发则是 `InternalError`。**网页端那个输入框本身就是坏的**，CLI 不复制它。

- HPC 容器里的资源观测与真实配额的关系写进参考。一个 `0,4,16` 的 slurmd 容器里 `nproc` 报 **64**、`free -m` 报 **~503 GiB**——那是宿主机；真实约束是 Pod 的 cgroup（`cpu.max` = 4 核硬限流，`memory.max` = 16 GiB）。照 `nproc` / `free` 自动调档的程序（`multiprocessing.cpu_count()`、OpenBLAS 与 PyTorch 的默认线程数）会按 64 核 503 GiB 开工，然后被限流并撞死在内存墙上。另外 **cgroup 的内存上限恒等于 `--quota` 的内存，不随 `--memory-per-cpu` 变**：实测只申请 12 GiB 的任务照样提交了 15 GiB 没被拦，Slurm 这层内存只进记账不设运行时上限。16 GiB 节点上单进程提交 15900 / 16100 MiB 都正常，顶到 16384 MiB 被 OOM 直接 SIGKILL，**日志里连一行错都没有**——这正是「跑着跑着没了、什么都查不到」的来源。

- HPC 的保留态写进参考：任务结束后会先停在 `SUCCEEDED_RETAINING` / `FAILED_RETAINING`，这段时间 `hpc delete` 答「当前状态（运行中）无法删除」，而 `hpc stop` 返回成功却解除不了保留——只能等它自己转成终态。清理脚本按状态重试，别按 `stop` 的返回值判断。

- **`GetJobLog` 的 sorter 结论此前记反了。** 仓库里写的是「平台拒绝任何显式 sorter」，实测是：控制台那一对 `[@timestamp, log-id.keyword]` 被接受，只发其中一个才报 `InternalError: 日志排序字段不合法，仅支持按时间 + log-id 排序`。另外 `start_timestamp_ms` 必须早于 `end_timestamp_ms`，倒过来报 `日志查询时间参数不合法`——而**控制台自己发的就是倒过来的一对**，所以网页端的聚合日志在这条路径上是坏的，不要照抄。Wrapper 的行为不变（不发 sorter，客户端排序），改的是注释和开发参考里的结论。

- **`inspire serving instances` 从来没列出过任何实例，`serving logs` 也因此从来读不到日志。** `ListServingInstances` 的行是嵌套的——`{groups: [{items: [...]}], total}`，一个副本一个 group——而 Wrapper 按兄弟 Action 的扁平 `items` / `list` / `instances` 读，于是永远拿到空列表配非零 `total`。那正好是「这个部署还没有 Pod」的形状，所以失败静默且永久：`serving instances` 报「没有实例」，`serving logs` 报「没有实例可读」，两条都读起来像部署自己的状态而不是 CLI 的 bug。在一个真实运行中的部署上复核：修好之后实例、落点和日志端点全部通了。

  这是本仓库第三次撞上同一类错误（`ListServingScaleHistory` 的 `scale_history_items`、`ListLogicComputeGroups` 的空列表配非零 `total`）：**「空列表 + 非零 total」永远不能读作「没有数据」。**

- **`job events` / `hpc events` 的实例级事件此前混成一条没有署名的时间线。** 行里唯一指明来源的字段是 `object_id`——平台句柄，按设计不进输出——而公共投影把它丢掉了，于是范围一开到 `--all-instances`，拿到的是一堆看不出归属的 `FailedScheduling`，「哪个 Worker 没排上」恰恰在最需要它的场合答不出来。现在按实例查询时输出多一列 `Instance`（`--json` 里是 `instance` 字段），标识一律取各自 `instances` 已经在打印的那一个：`hpc` 是角色 / 序号（`slurmd`、`launcher`），`job` 是 Rank（`rank=0`）——训练 Pod 叫 `job-<uuid>-worker-0-0`，洗过之后是 `<redacted>-worker-0-0`，那正是 `job instances` 当初丢掉名字改用 Rank 的原因，事件不该再把它捡回来。实例表里认不出的 Pod 宁可不标，也不回退到句柄。工作负载级事件不带这一列，输出与此前逐字节一致。

- **配额目录此前会丢掉大半计算组，原因是缓存主键漏了组。** 平台把同一档规格在多个组之间**复用同一个 `quota_id`**（分布式训练空间实测：9 个组、11 个不同的 `quota_id`，其中 7 个被 4–7 个组共用），而本地资源索引的主键里没有 `owner_id`，缓存行又是按裸 `quota_id` 存的。于是每个组写入时都覆盖掉前一个组的同名行，只剩最后一个写入者：索引里恰好 11 条 = 11 个不同的 `quota_id`，一条不多一条不少。

  后果是实打实的：`训练区-H200-1号机房` 有 1368 张 H200，v1 也一直正常返回它的四档规格，但 CLI 既列不出也建不了——`--group` 指名道姓同样报「matches no quota row」。修法是让缓存键以计算组打头（真正的 `quota_id` 一直存在行的 payload 里，创建时回显的是那个，不受影响）。修复后索引从 11 条 / 3 组变成 32 条 / 8 组，与平台逐组返回的结果完全一致（第 9 个组 `gpu_total` 真的是 0）。

- `--quota` 匹配不到时，如果同时给了 `--group`，错误不再说「这个 workspace 没有配额」。行集那时已经被 `--group` 收窄过了，那句话把责任推给了错误的作用域；现在按实际作用域说话，并列出该组真实有的档位。

- **私有镜像此前在整个 CLI 里都够不着。** 镜像是按 Workspace 的 registry 存的（每个 `ListImages` / `CreateImage` 都带 `registry_hint`），但读取一侧从来没有把 Workspace 当参数——它从当前 session 上取。这个账号的 session 默认空间恰好是一个空 registry，而 67 个自定义镜像都在另一个空间，于是 `image list` 报「没有镜像」，`job create --image <自己存的镜像>` 报「不在任何目录里」，**「把环境存成镜像再拿去跑任务」这条主动线是断的**。

  修法是把 registry 所在的 Workspace 一路显式传下去：`list_images_by_source` 新增 `workspace_id` 参数，`image list/detail/register/set-visibility/delete` 新增**必填** `--workspace`（语义统一为「镜像 registry 所在的工作空间」），`resolve_image_url`（train / HPC 创建）、serving 与 ray 的镜像解析、以及 `notebook save-image` 存完之后回查镜像，全部改成传入各自已经解析好的目标 Workspace。CLI 层的解析函数把这个参数设成必填而不是留默认值——静默默认正是这个 bug 的成因。有一条测试扫描全仓，任何漏传的调用点都会失败。

- `job logs` / `hpc logs` / `serving logs` / `ray logs` 的 `--json` 不再把日志正文里的绝对路径洗成 `<redacted>`。共享的路径清洗器是为了别让平台句柄漏进输出，但**日志正文是程序自己的话**：`+ /bin/bash -c ...` 被洗成 `+ <redacted> -c ...`、栈回溯的 `File "/opt/conda/.../site.py"` 被洗成 `File "<redacted>"`，正好抹掉这条命令存在的理由。human 输出一直是原样打印的，所以此前同一条日志在两种输出模式下还不一致。清洗只在日志正文字段上豁免，记录里其它字段照旧。

- `inspire ray start` / `ray stop` 不再把平台的状态机拒绝报成 `InternalError`。`ray` 用 `InternalError: RayJob status not allow <动词>` 表达「从这个状态不可能成功」，而这个错误码在瞬时名单里，于是一个永久性的拒绝被读成「平台暂时不舒服」，还会白烧三次退避重试。同时删掉了 `ray start` 那条被实测证伪的提示——它声称没到过 RUNNING 的任务无法重启，而实测这样的任务连续重启了三次。

- `inspire model status` 的 `vllm_ready` 与 `inspire model versions` 的 vLLM 列不再恒为 no。它们读的是版本记录里的存量 `is_vllm_compatible`，而那个字段是死的——29 个可见模型版本上无一为 true，同时两个 live Action 一致地给出 13 个 true。这不只是显示错误：同一个 CLI 里的 `model deploy-config` 一直问的是 live，于是两处对同一个模型给出相反的答案。现在三处都问平台。

- `inspire serving scale-history` 接上时发现 Wrapper 读错了列表键：线上是 `scale_history_items`，而代码读的是 `items` / `list`，于是任何有扩缩容历史的 serving 都会返回空列表。这是「读错键就永远看不到数据」的静默失败，不会报错。（`servings.py` 的模块 docstring 早就写着正确的键名，代码没跟上。）

- 五个 Batch 命令补齐了创建命令这一轮新增的全部字段，此前 Batch 条目严格弱于单条 `create`。`ray batch` 补 `public_path_readonly`，`serving batch` 补 `public_path_readonly` 和 `auto_scaling`；这两类不收数据集挂载，平台直接拒绝该字段，网页端对应表单也没有这一项。`notebook batch` / `job batch` / `hpc batch` 补的是：`dataset`、`env`、`description`、`keep_after_success` / `keep_after_failure`、`fault_tolerance_retry_interval`、`auto_stop_after`、`keep_after_finish`、`max_time`、`enable_notification` 和两档只读挂载都能写进条目了。`dataset` 接受一条 `"<名字>:<版本>"` 或一个列表，`env` 除了 `KEY=VALUE` 列表还接受表——TOML 和 JSON 表达映射比表达拼接字符串自然。数据集在条目准备阶段就完成校验，所以一个拼错的 spec 会在任何东西提交之前中止整个 Batch，而不是等前几条已经跑起来才发现。没有写这些键的条目产生的请求体与此前逐字节一致。

- `hpc status|stop|delete <name>` 不再可能报 `InvalidParameter: page or page_size too large`。名称解析按 `page_size=10000` 请求，而网关上限是 5000。这条上限逐 service 生效——`hpc` 拦，`ray` 给 10000 照收——所以截断放在 `browser_api` 的传输入口无条件生效，而不是逐个 Wrapper 去记：没有哪个调用方应该靠一次失败去学这个上限，而且截断不损失任何东西，超过上限的请求本来也不可能比按上限的请求多返回一行。`page_size: -1`（取全部）平台认，原样放过。

- `hpc list` 的总数不再是「这一页有多少条」。`hpc.ListJobs` 的 `total` 是字符串 `"202"`，而 Wrapper 用 `isinstance(total, int)` 判断后回退到 `len(items)`，于是 202 条任务被报成 100 条。影响不止显示：名称解析的翻页循环以 `已读 >= total` 为终止条件，拿到假的 total 后第一页就停，第 100 条之后的任务按名字根本查不到；`hpc list --all` 的展开分支同样不再触发。`total` 的类型逐 Action 不同，现在统一走一个共享的解析函数。

- `<workload> quota` 不再列出并不受理该 Workload 的计算组。每个组自己声明 `support_job_type_list`，`CPU资源空间` 四个组里只有两个收 `ray_job`，但 `ray quota` 把四个都列了出来，照着选到最后是创建时报「已选择的计算类型组不支持此类型任务」——一条要走到提交才暴露的死路。过滤同时作用于 `quota` 展示、创建时的 `--quota` 解析和配额目录缓存，三条路径用同一个判定。踩到的坑是这个字段是 **JSON 编码的字符串**而不是数组，按数组判断会让过滤看起来生效、实际一条都没滤掉。无法解析该字段时保留该组：读不出来是我们的无知，不是平台的拒绝，而藏掉一个可用的组是更糟的失败——它读起来像「这个空间跑不了这个」，没有任何错误信息会来纠正。

- 平台限流不再被当成「这个 Workspace 没有 Quota」。Quota 目录是每个 Compute Group 一次请求的扇出，`get_resource_prices()` 却把任何失败都 `return []`，于是一次 429 和「这个组确实没有规格」返回同一个值。刷新引擎照单全收：`FetchResult(complete=True)` → `index.reconcile()` → 把上一轮读到的行全部 tombstone，再把空目录标成完整且 fresh。之后 `job quota` / `notebook quota` 报 `No quota rows found.`，`job create` 报 `--quota 1,10,100 matches no quota row`、`Available: (workspace has no quotas)`——同一时刻 Web UI 能在那个组建出 1 卡 Notebook，`resources availability` 也显示还有几十张卡空着。缓存过 TTL 才会自己好，`cache clear` 之后紧接着的另一个进程还可能再踩一次，只有强制完整刷新并真的缓存到行才稳定恢复。（[#68](https://github.com/realZillionX/InspireSkill/issues/68)）

  修在语义上：`get_resource_prices()` 和 `list_notebook_compute_groups()` 不再吞掉请求失败，空列表从此只可能是平台成功回答的空。`fetch_quota_catalog()` 改为返回 `QuotaCatalog(records, complete, error)`——扇出会分片失败，一个组读不到就把这一轮记成 incomplete：读到的行照常写入，读不到的保留旧行，Scope 不算完整刷新，所以要求完整刷新的读者会回到 Live 查询。只有完整的目录才允许 `reconcile()` 去 tombstone。列不出 Compute Group 是另一回事——那时根本没有目录，直接抛错。构建缓存事实时不再接受 `config.toml` 的 Compute Group 兜底：那是给离线显示用的，写进权威缓存会把陈旧的组 ID 变成事实。

  `cache refresh` 因此多一档 `partial` 结果，汇总里报 `N incomplete` 并逐条给出原因，原因同时落到该 Scope 的 `last_error` 上，`cache status` 直接可见，下一次完整刷新才清掉。Quota 解析这一侧也不再把读不到的组当成没有行的组：一个组读不到就抛 `QuotaCatalogUnavailable`，而不是拿剩下的组给答案——那个没读到的组可能正好也有同一个三元组，那是本该要求 `--group` 消歧的情形。`--group` 会在取价格之前先把组过滤掉，所以指定了组的创建不受其他组失败的影响。

- 平台限流统一在传输层退避重试，并作为独立的错误类型抛出。429、408、5xx 和 v2 信封里的 `Throttling` / `ServiceUnavailable` 一类错误码现在抛 `TransientAPIError`（`ValueError` 子类，既有的 `except ValueError` 边界仍然把它映射成 API 错误）。`request_json()` 是 requests 与 Playwright 两条路径唯一的收口，在这里最多重试 3 次，优先采纳平台的 `Retry-After`，否则指数退避加抖动；重试耗尽才让调用方看见。Workspace 级的问题本来就是「每个 Compute Group 一次请求」的扇出，正是限流器会反应的形状，所以这层吸收留在传输层，Wrapper 不各自重试。

- 同一类「一时限流就判定不可用」的结论一并纠正：

  - `resources availability` 不再在某个组被限流时把它整条跳过，也不再把节点维度读不到的组报成 `0` 节点 / `0` 空闲——两者读起来都像是测量结果。现在直接报 API 错误。
  - `job create --image` / `job batch` 的镜像回退不再把「没搜到的镜像源」变成 `Image not found`。
  - `job events` / `hpc events` 不再把过期 Session 和限流一起吞成「没有事件」；平台确实没有事件时仍然返回空。
  - 缓存的名称句柄不会因为 429 被 tombstone：`is_stale_handle_error()` 现在明确排除瞬时状态码，平台没回答不等于句柄失效。
  - Notebook 命令查不到当前账号 ID 时，限流不再被报成 `AuthenticationError`——那会把用户支去重新登录，而实际只需要等一会儿。

### 维护

- **`inspire resources usage` 不接受 `--workspace all`**，传了直接报 `--workspace requires one workspace name for this command.`。开发中做过这个扇出，量完发现它答不出看起来在答的问题：聚合是在逐 Workspace 的循环里做的，同一个人在每个空间各占一行，所以 `--by user` 给的是「(空间, 人) 组合」的排名而不是人的排名——实测一个人在四个空间分别持有 2122 / 1176 / 888 / 560 卡，排行榜上就是四个不同的条目。截断更糟：总排序被跳过，默认 20 行只会来自最先枚举到的那个空间，而提示照样写 `Showing 20 of 857`。

  没有改成真正的跨空间聚合，是因为聚合完也没有对应的决定：配额和调度都按 Workspace 走，这个命令服务的三个动作（等、去找人要、换个地方提交）也都是。跨 Workspace 找地方本来就是 `resources availability` 和 `resources nodes` 的活，它们逐空间一行、拼接是诚实的。

- **CLI 不提供读 Workspace 配额天花板的命令，这是量过之后的结论。** 10 个可见 Workspace 逐个实测：GPU 上限要么是 `unlimited`（4 个），要么是整个集群容量的两倍（`分布式训练空间` 10000/20000 对 5589 张卡、`CI-情境智能` 4236/8472 对 1536、`可上网GPU资源` 2894/5788 对 1438），要么是 0（`CI-PPU`、`专属资源开发空间`——那是「这个空间根本没有你的份」，不是配给）；CPU 和内存则处处 `unlimited`。也就是说这条命令唯一有意义的那一列「配额余量」永远要么是 `-`，要么是一个比整个集群还大的数，**先耗尽的永远是硬件**——`QUOTA_PENDING` 不会因为这个天花板发生。Workspace 级汇总也答不出提交决定：任务是提交到某个计算组的，`resources availability` 给的按组余量严格更可用。结论写进 [`resources.md`](references/resources.md)；`workspace.GetWorkspaceQuota` 与 `GetWorkspaceComputeResource` 两个 Action 因此没有 CLI 消费者，对应的 `WorkspaceQuotaUsage` / `get_workspace_quota_usage` / `UNLIMITED_QUOTA` 一并从 `browser_api.workspaces` 删除。

- 手册补上「资源能留多久」和「谁能起高优」这两条一直只在 CLI help 里的规则。`inspire resources policy` 此前只有命令自己的 help 提过，手册里从头到尾没有出场，于是空闲回收在文档里只是「回收策略」这四个字；现在 [`resources.md`](references/resources.md) 说明它逐行给出的 `Reclaim` / `Idle Rule` / `Time Limit` 各是什么，并点明触发条件是 GPU 利用率而不是有没有人连着（`分布式训练空间` 实测：Job「GPU 低于 40% 持续 3 小时」，Notebook「GPU 低于 15% 持续 3 小时，或运行超过 18 小时」），`-` 是「没声明策略」而不是「没有限制」。[`notebook.md`](references/notebook.md) 的 `--auto-stop` 段和 [`compute-workloads.md`](references/compute-workloads.md) 的 Job 边界各自指过去——夜里挂着不吃卡的 Notebook 第二天不在了，是规则生效不是故障。

  优先级那一半的问题是位置错了：`1=LOW` / `4=HIGH` 的公平调度合同只写在 `compute-workloads.md` 的 GPU Job 一节，而 `SKILL.md` 把「优先级」路由到 `resources.md`，于是那边只能读到一句没有前提的「公平调度 Workspace 里就是 `--priority 1`」。合同移到 `resources.md`，Job 一节留一行摘要并指过去。同时把 `分布式训练空间` 的实际形状写成表：开发区四档全部不限，训练区 1 / 2 / 4 卡只调度低优先级、只有 8 卡整节点才不受限——要一个不会被抢占的小规格任务就去开发区，在训练区想拿高优先级只能整节点起。`--priority` 的 help 也补上一句指向 `<workload> quota` 的 `Priority` 列，此前它只讲两套合同和项目封顶，逐行限制要等到创建预检才知道。

- Browser API 文档重写。`references/dev/browser-api-v1.md` 和 `browser-api-v2.md` 被删除，替换为三份可独立阅读的参考：[`browser-api.md`](references/dev/browser-api.md)（请求契约、响应信封、认证与 Session、分页、Workspace scoping、错误码、探针方法、仍在用的 v1 端点、回落纪律、输出边界、变更验收）、[`browser-api-actions.md`](references/dev/browser-api-actions.md)（12 条路由 93 个 Action 的请求体、响应键、参数语义、CLI 映射与限制，加上五个创建 Action 的字段合同）、[`data-plaza-api.md`](references/dev/data-plaza-api.md)（数据广场是另一个平台，独立成篇）。

  旧的两份是按迁移顺序累积的工程日志：v1 那份的「当前公开命令映射」有十行只写着「全部已迁 v2」，v2 那份把契约、踩坑记录和迁移复盘混在同一节里，而两份都必须对照才能读懂任何一条。新文档按「维护者要查什么」组织，不再记录迁移过程；已废弃的 v1 域一并移除，只保留四处仍有消费者的 v1 端点及其保留理由。

  随后接入这一批 Action 时，实测又推翻了文档里三处说法：**字段存在性探针会被资源 id 静默废掉**——网关的鉴权中间件在严格 proto 解析之前先读一遍资源键，读不到对象就直接返回，于是 body 里的未知字段根本没被解析，探针给出的「这个字段在合同里」是假的（`ray.GetJobLog` 上四路对照可复现，它让 `ray_job_id` 看起来在合同里，其实不在）；**网关对字段名的大小写和下划线不敏感**，`ImageId` / `image_id` / `Image_Id` 落到同一个字段，所以旧文档那套「同一个标识符三种拼法」的陷阱只剩 `image.UpdateImage` 要裸 `id` 这一处真的存在；**discovery 的响应声明只有最外层的列表键和 total 不可信**，元素内部的属性名是准的。另外把 `workspace.GetScheduleConfig` 归入管理员专用（它与各 Workload 路由下的同名 Action 只是重名），并补上 12 个新接入 Action 的请求体、响应键与限制，Action 总数 93 → 105。

  重写时对着 `browser_api/` 逐个 Wrapper 和一份现取的 `/discovery`（`Version = e1daec0f`，11 个 Service / 175 个 Action）核了一遍，修正了三处旧文档的错误：登录握手仍在用 `/api/v1/user/detail` 和 `/api/v1/user/routes/default`（v2 的 `GetRoutes` 要一个真实 `WorkspaceId`，登录时还没有），此前被漏记成「账号与用户全部已迁 v2」；`workspace.GetWorkspaceQuota` 与 `GetWorkspaceComputeResource` 现在在 discovery 里，此前记的是「两者都不在」；discovery 声明的**响应**结构与线上不符（声明 `Items` / `TotalCount`，实际是 `jobs` / `list` / `logic_compute_groups` 加小写 `total`），此前只说了参数不可信。另外如实标出 15 个有 Wrapper 和测试、但当前没有任何 CLI 命令调用的 Action。

## v7.0.2

### 变更

- Notebook 的 SSH / Rtunnel 可用性改为读机器上真正插的显卡型号，不再看 Compute Group 名称含不含 `H100` / `H200`。名字是人填的标签：可以被改、可以简写，也可以和机器上的硬件对不上，而这条判断决定的是能不能建 Rtunnel。现在用 JupyterTerminal 在机器上跑一次 `nvidia-smi --query-gpu=name`，型号是 `H100` / `H200` 的就是受限 Notebook。

  这不是 v6.3.0 删掉的那条联网探测：那条探的是「能不能上网」，把它当作「能不能 SSH」的代理指标，还要为此起一个完整远端终端竞速四个 TCP 探针（数十秒）。现在探的就是判断本身要的那个事实，一条命令，而且答案会记住。

  记住的粒度是 **Compute Group**，不是 Notebook：一个组就是一池同型号机器，组里第一个 Notebook 探完，之后落在该组的任何 Notebook 都直接命中。组名跟着 Name 解析一起回来（`_resolve_notebook_target` 的第三个返回值、索引里 notebook 记录的 `compute_group`），所以拿这个 Key 不额外发请求；只有平台回包里没有组名时才多一次 Detail 请求，换掉的是该组每个 Notebook 各探一次。结果存 `~/.inspire/notebook-gpu-models.json`，30 天过期纯粹是为了不让文件无限长。`run_notebook_ssh` 里那道内部关卡读同一份，不带 `--workspace` 的 `notebook ssh` 仍然拦得住受限 Notebook 且不重复探测。

  **机器答不上来时直接报错退出，不猜 Transport。** 探测走的就是受限 Transport 那条通道，一台答不上来的机器同样跑不了 JupyterTerminal，而 `ssh` / `exec` / `shell` / `scp` 本来就都要求 Notebook 在 `RUNNING`。此时会去查一次真实状态，未启动就直说未启动并给出 `inspire notebook start <name> --workspace <workspace>`；已经 `RUNNING` 却仍然连不上，就说明是 JupyterTerminal 不通，提示稍后重试或重启 Notebook。

- `inspire cache clear` 和 `inspire cache status` 支持 `--resource <kind>`，与 `cache refresh` 同名同形（可重复，不带即全部）。`clear` 此前只有「全炸」一档：想让一个 Notebook 重新探测显卡，得把 Job、Image、Quota 目录一起赔进去，下一条命令再全部重建；`status` 则只能一次列全部。

  可选的 kind 是 `refresh` 那 15 种，外加 `notebook-gpu`——Notebook 显卡型号那层，不在 Name 解析索引里，但同样是缓存，`status` 和 `clear` 都认它。它进不了 `refresh`：那 15 种背后都有列表接口可以整个 Scope 批量拉，显卡型号只能一个组一个组地开远端终端探，没有「全刷一遍」这个动作。

  `status --resource X` 一定会给 X 一行，没缓存就报 `empty`——问「X 现在什么状态」，沉默不是回答。不带 `--resource` 且整个缓存确实空着时，仍然只打印一句 `Resource name cache is empty.`。`clear` 的人类输出和 `--json` 都报告各清掉多少条；部分清理照样 bump generation，否则一个在途 refresh 会把结果写回刚清空的 Scope。

### 修复

- 打开 Name 解析索引时清掉当前版本已经不认识的资源类型。`ssh-key` 在 v7.0.0 随 `inspire user` 一起删了，但老库里它的 scope 行还在：刷不到（不在刷新注册表里）、点不到（不在 `--resource` 候选里），却一直出现在 `cache status` 里。现在开库时按 `DEFAULT_TTL_SECONDS` 已知的类型扫一遍，孤儿行直接删；索引本来就是可丢弃的加速状态，留着一个谁也读不到的类型没有意义。

## v7.0.1

### 新增

- `inspire uninstall` 卸载自己。三档按归属分，不按省事分：默认删安装器写下、且只有 InspireSkill 要的东西（各 harness 的 skill 目录、macOS 每日检查的 launchd agent 及其日志、`~/.inspire/update-status.json`），最后才是 CLI 包本身；`~/.inspire` 存的是重装后仍然能用的平台凭据，要 `--purge`；Playwright 浏览器缓存装在本机所有 Playwright 工具共读的位置，要 `--purge-runtime`，且不被 `--purge` 蕴含。仓库自己的 `INSPIRE.md` 和 `./.inspire/` 是项目资产，任何一档都不碰。执行前打印完整清单并要求确认（`--yes` 跳过，`--json` 下必须带），路径一律显示成 `~/...`，本机用户名不进输出。

  三处顺序是有讲究的：launchd agent 先 `unload` 再删 plist，否则 launchd 会一直抱怨一个指向不存在二进制的 job；CLI 包最后删，且只在前面每个文件都删干净之后才删——半清理的机器上还得留着那条能把活干完的命令；包删掉之后进程立即 `os._exit`，不做解释器收尾。最后这条是因为卸载会把本进程自己的 venv 抽走：已经载入内存的代码照常跑完（POSIX 会为持有 inode 的进程保留它），但此后任何一次 import 都会落空，而正常的解释器退出路径是会 import 的。所以两种结果的输出都在动包之前就渲染好。

- `scripts/install.sh --uninstall` 做同一件事，参数同名（`--purge` / `--purge-runtime` / `--yes`），供 CLI 已经跑不起来的机器兜底。它在 `curl | bash` 下也能问：此时 stdin 是脚本自己，所以确认提示直接走 `/dev/tty`——并且是真去打开它，不是看 `-r`，因为没有控制终端的会话里那个节点照样存在且可读，只有 open 会失败。

### 修复

- 安装器结尾的 `InspireSkill installed.` 少一个换行，后面的步骤提示会贴在同一行上。

## v7.0.0

### 破坏性变更

- 移除 `inspire notebook url` 和 `inspire notebook vscode`。这个 CLI 由 Agent 驱动，而这两条命令唯一的作用是在本机默认浏览器里打开一个网页——Agent 没有浏览器可开，这个动作对它没有任何意义。要人来看 Web IDE，直接在启智控制台打开 Notebook 即可。

- `inspire notebook proxy-url` 改为**打印**地址，不再打开浏览器。它的用途是拿到 Notebook 容器里某个端口的外部 HTTP 地址，好去请求部署在里面的服务，所以地址本身就是结果；此前它把地址藏起来、只开一个浏览器窗口，对 Agent 完全不可用。human 输出就是一行 URL，`--json` 输出 `{name, url}`（带 `--check` 时多一个 `service_check`）。

  这条命令因此成为整个 Notebook 命令组里唯一打印平台 URL 的命令。**这个地址等同于凭据**：它内嵌一段短期 token，持有者对该 Notebook 的访问权与你相同，而它会进 Agent 对话记录和 shell 历史。没有免 token 的替代形式——平台域上的 `/api/v1/notebook/lab/{id}/proxy/{port}/` 实测 404，只有带 token 的网关 URL 真的会去连容器端口。JSON 输出走新增的显式开关 `format_json(..., preserve_raw={"url"})`，因为默认的句柄清洗会把这条 URL 整条洗成 `<redacted>`，洗完就不通了；这个开关比既有的 `preserve_paths` 更强，只在「洗过就没有意义」的值上使用。

  顺带修正 `--check` 的判定：端口上没有服务时网关返回 500 `connect ECONNREFUSED`，此前落进 `blocked` 这一档，读起来像是权限问题；现在报 `no_service`，与「去把服务起起来」这个真实动作对应。

- 移除 Model Plaza 的全部 Wrapper（`list_model_plaza`、`get_model_plaza_filters`、`get_model_plaza_detail`、`list_model_plaza_related_workspaces`、`get_model_plaza_deploy_serving_config`）。它们从未被任何 `inspire` 命令调用，只存在导出和自测。Model Plaza 是平台侧的公共模型货架（一键部署用），与 `inspire model` 操作的 Model Hub 是两回事——后者是本工作空间自己注册、带版本管理的模型仓库，两者靠 `plaza_publish_status` 和 `model-hub.GetModelPublish*` 相连，那条发布链路仍然保留。随之 `/model_plaza/list` 从「仍留在 v1」清单里消失。

- 移除 `inspire user` 整个命令组。`inspire user permissions` 迁到 `inspire account permissions`，选项、Name-only 输出和 `--limit` / `--all` 边界都不变；`whoami`、`api-keys`、`ssh-keys` 直接删除。这三条各自的理由：`whoami` 只打印姓名和角色，其底层查询作为内部实现仍在（train / hpc / ray / model 列表靠它按当前用户过滤）；API Key 的值只在创建时可见，列出元数据在 CLI 里无法转化为任何操作；`ssh-keys` 管的是平台用户中心的公钥注册表，而 `notebook ssh` / `scp` / `exec` 读的是本机 `~/.ssh/*.pub` 并直接注入，两者互不相干，删除不影响 Notebook SSH。随之移除的还有 `ssh-key` 这一资源索引类型——它存在的唯一目的是让 `ssh-keys delete <name>` 按名字解析。

- 移除 `inspire user quota`。它调用的是 admin-only 端点，普通用户恒定收到 `用户不存在`（账号是存在的，这只是平台拒绝权限的说法），命令自身的 `--help` 和错误提示早已写明这一点。工作空间级配额用 `<workload> quota`，实时占用用 `resources availability`，项目级信息用 `project list`。没有改接 v2 的同名 Action：`workspace.GetDefaultUserQuota` / `GetWorkspaceQuota` 普通成员确实可用，但都是工作空间级的，接过去需要新增必填的 `--workspace`，回答的也不再是「我的账号配额」这个问题。

### 变更

- 恢复 `inspire update` 面向用户的输出：逐步打印进度（检查更新 / 升级 CLI / 刷新 Skill / 校验安装 / 准备浏览器运行时）、列出刷新到的 harness，并打印新旧版本之间的更新摘要（取自 GitHub Releases，回退到 `main` 的 `CHANGELOG.md`）。v6.3.0 把这些一并降级成了 `--debug` 日志，只剩一行 `InspireSkill updated to vX`。诊断细节仍然只进 `--debug`：harness 只报名称不报本地路径，摘要过滤掉安装 / 构建类条目、URL 和绝对路径。`--json` 输出相应新增 `skills` 与 `release_notes` 字段。摘要条目会先合并硬换行的续行，不再从行尾截断成半句话。

- Browser API 按域从 `/api/v1` 迁到 `/api/v2`：notebook、ray、train、hpc、inference_serving、model-hub、project、user、image、file，以及计算组、节点维度、组资源统计和五个 Workload 的 metrics。公开 CLI 合同不变——命令名、参数、Name-only 语义、human 与 JSON 输出都保持原样，写操作全部经过受控验证（在 `CPU资源空间` 起最小规格临时资源跑完整生命周期，train 的删除因为 CPU 组不支持该任务类型，在 `分布式训练空间` 用 1 卡 H100 验证后随即释放；镜像与模型注册各跑了一遍建→读→改→删；「存镜像」用最小 CPU 配额加最小官方镜像起了一个临时 Notebook，真提交出一个 196 MB 的镜像，全部痕迹随即清除）。两代接口的契约差异记在 `references/dev/browser-api.md`。

  第二轮迁移推翻了第一轮的一个前提：**平台的 `/discovery` 清单是不完整的，不能用来否定一个端点有没有对应物。** 第一轮把 `/user/permissions`、`/user/routes`、`/project/list`、`/project/{id}`、`/project/owners`、`/file/*`、`/model_plaza/*`、`/image/create`、`/image/update`、`/model/create` 共 10 个家族判成「没有对应 Action」并保留 v1，依据都是「discovery 里查不到」。逐个实测下来它们全部有可用 Action，只是没被声明——`file` 和 `model_plaza` 连整个路由都不在清单里。判断一个 Action 是否存在只能靠空 body 探针（`InvalidAction` 才是不存在），路由是否存在只能靠 `404` 与 `InvalidAction` 的区别。

  仍留在 v1 的只剩三处，各有实测依据：

  - `/notebook/lab*` 与 Notebook Proxy——反向代理，要转发任意 HTTP 流量，整套 Notebook SSH 也架在它上面。v2 的 Action 模型装不下。
  - `/train_job/remote_cmd`——双向 PTY 流，同理。23 个候选名 × 5 条路由全部 `InvalidAction`。
  - `/resource_prices/logic_compute_groups/`——**不是没有对应物，是换过去更贵**。它一次答完「这个组能选哪些规格」；v2 的 `workspace.GetScheduleConfig` 只给静态菜单，还要按组补 `GetLogicComputeGroupNodeSpecs`（规格得装得进组内机器）和 `GetLogicComputeGroupResource`（组得真有可分配容量）才能筛出同样结果——实测 9 个组从 9 次请求变 19 次，且等于在客户端维护一份平台调度端筛选逻辑的副本。完整规则与逐组验证记在 `references/dev/browser-api.md` 第 8 节。

  平台用户中心的 SSH 公钥接口 `/ssh/*` 不在此列：它随 `inspire user ssh-keys` 一起下线后已无任何消费者，文档里那几行「留在 v1」是残留，一并删除。

### 修复

- `inspire notebook exec` 和 `inspire notebook shell` 走 Jupyter Terminal 时不再启动无头浏览器。那个浏览器只做三件事：取 lab URL、取 `_xsrf`、建/删 terminal。现在分别由 `notebook.GetNotebookAccessUrl`、一次普通 GET（`_xsrf` 本来就是个 cookie）和 `POST`/`DELETE api/terminals` 完成。交互式 shell 的会话本就跑在 Python WebSocket 上；`exec` 的抓取循环从页内 JavaScript 移植到 Python，协议未变（等 prompt、分块喂 stdin、见到 `<marker>:exit:<code>` 收工）。受控验证在 RUNNING 的 CPU Notebook 上完成，全程用 import hook 封死 `playwright` 包，退出码与多行输出都正确。

  顺带说明一个容易误判的事实：`exec` 在该容器上端到端约 31 秒，其中 **27 秒是容器里内层 `bash` 在 source rc 文件**（`build_jupyter_exec_command` 的执行方式一直如此），与传输方式无关，老的浏览器路径同样要付这笔钱。

- Notebook 网关 URL 的解析不再默认起一个无头 Chromium：先问平台的 `notebook.GetNotebookAccessUrl`，拿不到才回落浏览器抓取。两者归一化后的结果**逐字节相同**，耗时 **0.57 秒对 6.4–36 秒**。收口在 `resolve_notebook_vscode_ide_url`，所以 `notebook proxy-url` 和 rtunnel 的 SSH 候选路径同时受益。`--refresh` 也走 API——它的语义是「别信缓存」而不是「一定要抓」；STOPPED 的 Notebook 上 API 返回空串，照旧回落浏览器。

- Notebook 网关 URL 的归一化丢了结尾的斜杠，任何直接打开它的地方都会落到 404。网关对这个 URL 的响应是一个 **302 到相对路径** `./?folder=...`：带斜杠时 `./` 落在 token 目录上，正常加载；不带斜杠时 `./` 被解析到上一级，`<token>` 那段被吃掉，重定向终点就是 404。两种写法对第一个请求都回 302，所以存活探测（只看 2xx/3xx）一直判定「可达」，故障只在跟完重定向后才显现。修复同时会重写磁盘上已缓存的旧地址——那些条目照样探活通过，不主动修就会继续发出坏链接。

  这个 bug 是在删掉 `notebook vscode` 之前发现的，当时正是它打开 404。现存命令里 `proxy-url` 不受影响（它拼 `/proxy/<port>/` 时本来就补了尾斜杠），修复的价值在于归一化结果现在对任何消费方都是可用的。

- `inspire serving start` 与 `inspire serving stop` 此前对任何输入都失败，返回 `API error: None`。这两条命令的 URL 早先已指向 `/api/v2`，但仍用 v1 的信封检查（`code != 0`）解包，而 v2 响应根本没有 `code` 字段，于是每次调用都被判成错误。换用 v2 解包器后暴露出第二个问题：请求体里的 `version` 字段 v2 同样不接受，正确的请求体只有 `{inference_serving_id}`。

- `inspire hpc create` 此前对任何输入都失败，返回 `InternalError: priority must be set`。请求体把优先级写成了 `task_priority`，而 v2 要 `priority`；平台的措辞像是「值没传」而不是「字段名写错」，所以这个问题一直没被认出来。

- `inspire job create` 和 `inspire hpc create` 传镜像显示名（如 `ngc-pytorch:25.02-cuda12.8.0-py3`）会被拒绝，报 `无法找到对应镜像`——平台按 registry URL 匹配，而报错读起来像是镜像不存在。现在显示名会先在镜像目录里解析成 URL；已经是 URL 的直接透传、不查目录，`--image NAME|URL` 的合同不变，刚推送尚未出现在列表里的镜像也仍然可用。

- `inspire hpc list` 的名字列此前恒为 N/A，`inspire hpc status|stop|delete <name>` 也无法按名字定位任何任务：解析读的是 `name`，而平台返回的字段是 `job_name`。

- `inspire ray list` 的 Created By 列此前恒为 N/A：解析读的是 `created_by` 和 `priority`，而平台返回的是 `creator` 和 `priority_name`（前两者始终为 null）。

- `inspire resources nodes` 此前对非工作空间管理员整条命令失败，报 `You are not the admin of any workspace`；同时 `resources availability` 的 Free Nodes 列恒为 0。两者都源于节点数据取自管理员专属端点，而退化路径取到的是硬件规格表、不含实时状态，因此没有任何节点会被判为空闲。现在改用工作空间级的节点维度查询，普通成员可用，空闲节点数是真实统计。

- 并发冷启动的 Notebook SSH 连接不再互相踩踏。VS Code Remote SSH 这类客户端会同时拉起多个 `ssh-proxy` ProxyCommand 进程，此前它们各自看到共享状态缺失或过期，于是同时写 Notebook Target Cache、同时刷新 Web Session、同时跑一遍 Bootstrap，表现为间歇性的平台账号识别失败。现在 Target Cache 的读改写、`web_session.json` 的写入与按账号的登录刷新、以及按账号 / Workspace / Notebook 的冷启动 Bootstrap 都跨进程串行化：等待方复用赢家产出的 Session 与连接，过期的 Session 不再覆盖新的，重新登录后也会回到正常 HTTP 请求路径而不是留在浏览器回退上。等待有上限（Bootstrap 为 `--timeout` 加 60 秒），卡死的持有者不会把后续 `ssh` 永久堵住。

## v6.3.0

### 新增

- `inspire ray metrics` 补齐 Ray 的指标观察，与 job / hpc / serving metrics 同接口。
- `inspire serving events|instances|start` 补齐 Serving 的观察与启动入口。

### 破坏性变更

- 移除 `inspire job id`、`inspire hpc id`、`inspire notebook id`。CLI 不再有任何 Handle 输出入口，用 `list` 拿 Name 即可。
- `inspire notebook ssh open <name>` 改为 `inspire notebook ssh <name>`；`inspire notebook vscode-proxy-suffix` 改为 `inspire notebook vscode`。
- `job|hpc|notebook|serving metrics` 的 `--lcg` 改名为 `--group`，与创建命令一致。
- `serving create --shm-gib` 改名为 `--shm-size`，与 job / notebook / ray 一致。
- `resources nodes` 只保留 `--min-nodes`，去掉同义别名 `--min-free` 与 `--min-full-free-nodes`（三者本就是同一个选项）。
- 移除 `resources availability --no-cache`：它绕过的可用性缓存已整体删除，该命令现在始终读取 Live 数据。
- 移除 `notebook exec` 的 `--artifact-path` / `--download` / `--no-wait` / `--denylist`，以及它们背后的 GitHub Actions bridge action 执行后端和整个 `INSP_GITHUB_*` 配置类别。这条路径需要自建 Actions Runner 挂载集群共享文件系统、在用户仓库里安装 workflow，并把产物经 orphan 分支中转；参考文档从未描述过它，随附示例 workflow 引用的 `inspire bridge exec` 也早已不存在。拉取远端产物请用 `notebook scp -d ... -r`（直连 SSH / SCP，无需绕行 GitHub）；无 tunnel 时的命令执行仍由 Jupyter terminal transport 承担。残留的 `[github]` 配置段会被静默忽略，不影响现有配置加载。
- 移除从 `INSPIRE_USERNAME` / `INSPIRE_PASSWORD` 环境变量读取平台凭据的兜底路径；账号是唯一受支持的凭据来源，用 `inspire account add <name>` 配置。其余 `INSPIRE_*` 配置项（如 `INSPIRE_SHM_SIZE`、`INSPIRE_JOB_ENABLE_NOTIFICATION`）不受影响。
- `inspire update` 不再抓取并打印 GitHub Release 正文。
- 移除 `inspire notebook net-test` 及其背后的 JupyterTerminal 出站探测（`probe_notebook_network` 和相关 Browser API 导出）。它唯一的消费者是 SSH transport 判断，而该判断已改用 Compute Group（见下）。需要确认某个具体端点是否可达时，用 `inspire notebook exec <name> "..."` 在容器里一次性验证。
- `notebook connection list|status` 的 JSON 输出去掉 `public_internet` 字段，本地 bridge profile 也不再持久化 `has_internet`：缓存连接只会存在于支持 SSH 的 Notebook。旧 `bridges.json` 中残留的 `has_internet` 会被静默忽略。

### 变更

- 重构 Agent 使用手册：新增 `references/project-context.md`，承载项目初始化问询（Project / Workspace / Paths / Image 四项须由用户确认，其中 `CPU资源空间` 与 `分布式训练空间` 是默认公共 Workspace，其余专属 Workspace 须用户亲自指认）、`INSPIRE.md` 资产合同（合并原 `project-assets.md`）和项目信息持续维护触发点；`resources-and-paths.md` 更名 `resources.md`、`network-and-sources.md` 更名 `internal-sources.md`、`image-management.md` 更名 `image.md`。删除 SKILL.md 的“网络与合规闸门”一节（此类限制由 CLI 层承担），并清理各使用手册中缓存实现、内部字段名等开发层细节；`references/dev/` 之外的文档只保留面向 Agent 的操作语义。
- Notebook 的 SSH / Rtunnel 可用性改为直接看 Compute Group 名称是否含 `H100` / `H200`，不再靠 JupyterTerminal 联网探测推断。旧路径要为每个非静态受限的 Notebook 开一个完整远端终端跑连通性探测（数十秒），且把“能上网”当作“能 SSH”的代理指标。该判断同时收紧到 `run_notebook_ssh` 内部，因此不带 `--workspace` 的 `notebook ssh` 也无法为受限 Notebook 建立连接。
- Compute Group 名称随 Notebook 一起进本地 Name 解析索引（`resource_identity` 新增 `compute_group` 列，旧库自动 `ALTER TABLE` 迁移，schema 版本 3），由后台定时刷新一并维护。因此 Transport 判断在缓存命中时不发任何 API 请求；缓存未命中时也只从 Name 解析本来就要发的 `/notebook/list` 响应里读，不再额外请求 Notebook Detail。
- 缓存 TTL 整体放宽：Workload（notebook / job / hpc / ray / serving）60 秒 → 5 分钟，平台目录类（workspace / project / compute-group / image / model / ssh-key）5 分钟 → 30 分钟。后台刷新进程的最小间隔取所有 TTL 的最小值，因此每账号的后台刷新从最多 60 秒一个降到最多 5 分钟一个。
- Project 改为按账号全局缓存，不再按 Workspace 分片。一个 Project 可同时归属多个 Workspace（`ProjectInfo.workspace_ids`），平台的 `project/list` 也支持不带 Workspace 过滤，此前每个 Workspace 各存一份、刷新时每个 Workspace 各发一次请求。现在刷新走 `list_all_projects()` 一次拿全。Name 解析的 Scope 由 `scope_workspace_id()` 统一归一化，刷新侧和查询侧不会再对同一个名字用不同的 Scope。
- 新增 Quota 缓存，作为 Name 解析索引的普通成员：一条 Quota 行本身就是 Name → Handle 映射，Name 是 `gpu,cpu,mem` 三元组（正是 `--quota` 传的值），Handle 是平台 `quota_id`。`resource_identity` 新增 `payload` 列存原始规格对象（`create` 需要回传 `cpu_type` / `gpu_type`），旧库自动 `ALTER TABLE` 迁移，schema 版本 4。Resource 名为 `quota-notebook` / `quota-job` / `quota-hpc` / `quota-ray` / `quota-serving`，Scope 是 Workspace，TTL 30 分钟，因此 `cache refresh --resource quota-notebook`、`cache status`、`cache clear` 和后台定时刷新全部自动覆盖，Admin 删掉的规格也会被 reconcile 正常 tombstone。此前 `<workload> quota` 查询和 `create --quota` 解析都要对 Workspace 里的每个 Compute Group 各发一次规格请求（1 + N），Scope 新鲜时现在是 0 次。

- 新增按账号隔离、可定时刷新且支持手动管理的本地 Name 解析索引；`inspire cache status|refresh|clear` 用于查看、刷新或清理该加速层，平台 Live API 仍是资源事实源。
- CLI 的公共输入、Help、错误、人类输出和 JSON 输出保持严格 Name-only；平台 Handle 只存在于内部解析器、Browser API 请求和本地调试实现中。
- 发现类列表默认限制为 20 项，Batch 结果和 Job 日志使用明确的输出预算；使用 `--limit/-n` / `--all` 主动调整集合输出。
- 同类 workload 命令统一使用相同的 Name、Workspace、分页、确认、截断和 JSON 语义。

### 修复

- Handle 识别只覆盖平台真正会签发的前缀。`node-`、`task-`、`pod-`、`instance-`、`container-`、`group-`、`cg-`、`compute-group-`、`proj-`、`workspace-` 以及 `lcg-1` 这类短数字后缀恢复为普通 Name：此前它们在 list / JSON 输出里显示为 `<redacted>`，同时在输入侧被拒绝，导致这样命名的资源在 Name-only CLI 中完全无法访问。
- 交互式与远端字节流不再改写。`job shell`、`notebook shell`、`notebook exec` 和 Jupyter 终端此前会扣住每个 chunk 结尾的疑似 Handle 片段，造成 raw 模式下无按键回显、提示符残缺、全屏程序刷新不全。日志正文同样按原样输出。
- 带标签的 UUID 完整打码，不再只截掉首段而留下其余部分。
- `_post-update` 重新接受并忽略 `--previous-version`：v6.2.0 及更早版本在自更新时一定会传该参数，缺少它会让升级在交接处失败，跳过 Skill 刷新与运行时安装。
- `notebook ssh-proxy` 重新接受并忽略 `--quiet`；`notebook ssh-config` 生成的 ProxyCommand 恢复使用 `inspire` 的绝对路径（OpenSSH 经 `/bin/sh` 执行，PATH 与交互 Shell 不同），且 `$HOME` 之外的 IdentityFile 不再被静默丢弃。

### 维护

- 开发依赖只保留 `[dependency-groups] dev`，移除 `[project.optional-dependencies] dev` 与 black 配置：用 `uv sync --dev` 装开发环境，`pip install -e ".[dev]"` 不再可用。
- 移除 commitizen 配置。发版时需手动同步 `cli/inspire/__init__.py` 的 `__version__` 与 `cli/pyproject.toml` 的 `version`，并打 `v<version>` tag 触发 publish workflow——原先由 `[tool.commitizen] version_files` 保证的两处一致性现在没有工具兜底。

## v6.2.0

### 新增

- GPU Job 支持平台原生状态通知，可通过 CLI、账号配置或 Batch item 配置开关。
- 支持 Qoder Work 和 Kimi Desktop Harness。

### 变更

- 代理、登录诊断和 JSON 输出统一脱敏，不输出凭据、内部路径、请求包装或平台 Handle。
- Notebook、GPU Job、HPC、Ray、Serving、Image、Model、Project、Resources 和 User 命令统一采用短、可读、可脚本消费的输出。

### 修复

- 修复通用 Shell proxy 与 `NO_PROXY` 的继承边界。
- 修复受限 Notebook 的内部镜像访问不应继承容器代理的问题。
- 修复 Job 通知、容错默认值和项目优先级在分层配置与 Batch 路径中的传递。

## 历史版本

请参阅 [GitHub Releases](https://github.com/realZillionX/InspireSkill/releases)。
