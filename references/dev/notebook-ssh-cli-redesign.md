# Notebook SSH CLI Redesign Plan

## 背景

当前 `inspire` v5.1.18 的 notebook SSH 命令表面是：

```bash
inspire notebook ssh connect <notebook> --workspace <workspace>
inspire notebook ssh test <notebook>
inspire notebook ssh refresh <notebook>
inspire notebook ssh forget <notebook>
inspire notebook shell <notebook>
inspire notebook exec <notebook> "<command>"
inspire notebook scp <notebook> <source> <destination>
```

这个设计把 `ssh` 作为“连接缓存管理”命令组使用，而不是作为 SSH 客户端入口。用户看到 `ssh` 时会自然期待 `ssh <host> [command]`、`scp`、`rsync -e ssh`、VS Code Remote SSH 等 OpenSSH 心智模型。当前 `ssh connect --command`、强制 `--workspace`、以及隐藏 rtunnel 细节的方式没有给原生 OpenSSH 工具留下稳定入口。

本规划目标是把日常路径改成符合 SSH 直觉，同时保留 Inspire notebook 所需的平台解析、bootstrap、rtunnel 和缓存能力。

## 目标

- `workspace` 在日常 SSH 路径中自动探测，只有歧义或用户覆盖时才显式传入。
- `inspire notebook ssh <notebook> [-- <command>...]` 成为人类使用的主入口。
- 重新提供 `inspire notebook ssh-config <notebook>`，输出可被 OpenSSH 原生消费的配置。
- 新增公开的 `inspire notebook ssh-proxy <notebook>`，作为 OpenSSH `ProxyCommand` 的低层适配器。
- 新增 `inspire notebook connection ...`，把连接缓存的查看、刷新、遗忘和清理从 `ssh` 主路径中拆出来。
- 用户无需接触 rtunnel URL、本地临时端口、Jupyter proxy 路径等内部细节。
- 保留现有命令作为兼容入口，避免破坏已有脚本。

## 非目标

- 不在 `inspire notebook ssh` 中完整复刻 `ssh(1)` 的所有参数。
- 不要求用户手动维护 rtunnel 或本地转发端口。
- 不把 `ssh-proxy` 设计成交互 shell 命令。
- 不在首版自动改写用户的 `~/.ssh/config`，默认只打印配置。
- 不让 `connection forget/prune` 修改用户的 OpenSSH 配置；删除 `~/.ssh/config` 中的 Host 片段应由未来单独的 `ssh-config --uninstall` 设计处理。

## Command Surface

### Human-facing Entrypoint

```bash
inspire notebook ssh <notebook> [--workspace <workspace>]
inspire notebook ssh <notebook> [--workspace <workspace>] -- hostname
inspire notebook ssh <notebook> [--workspace <workspace>] -- bash -lc 'pwd && nvidia-smi'
```

语义：

- 不带远程命令时打开交互 SSH shell。
- `--` 之后的参数作为远程命令 argv 传给 SSH，不再使用 `--command`。
- `--workspace` 是消歧和覆盖参数，不是日常必填项。
- 命令可以向 stderr 打印进度、诊断和友好错误。
- 命令可以自动触发 notebook SSH bootstrap 和缓存刷新。

建议保留有限的 Inspire 专有选项：

```bash
inspire notebook ssh <notebook> \
  [--workspace <workspace>] \
  [--cwd <remote-path-or-alias>] \
  [--pubkey <path>] \
  [--timeout <seconds>] \
  [--no-bootstrap] \
  [--debug-playwright] \
  [-- <remote-command>...]
```

完整 OpenSSH 参数兼容交给 `ssh-config` + 原生 `ssh`，例如：

```bash
ssh -L 8080:localhost:8080 inspire-kchen-test-2
scp a.txt inspire-kchen-test-2:/tmp/
rsync -av ./ inspire-kchen-test-2:/workspace/
```

### OpenSSH Config Output

```bash
inspire notebook ssh-config <notebook> [--workspace <workspace>]
inspire notebook ssh-config <notebook> --host inspire-<alias>
```

默认只向 stdout 打印配置，不修改文件：

```sshconfig
Host inspire-kchen-test-2
  HostName kchen-test-2
  User root
  Port 22222
  ProxyCommand inspire notebook ssh-proxy %h --workspace CPU资源空间 --port %p
  IdentityFile ~/.ssh/id_ed25519
  StrictHostKeyChecking accept-new
```

说明：

- `Host` 是本机别名，默认从 notebook 名规整得到，例如 `inspire-kchen-test-2`。
- `HostName` 保留 notebook 解析名，传给 `ssh-proxy` 的 `%h`。
- `Port` 是 notebook 容器内 SSH 服务端口，默认 `22222`。
- `ProxyCommand` 只调用 `inspire`，不暴露 rtunnel URL。
- `ssh-config` 生成时应先解析 workspace，并默认把解析出的 workspace 固化到 `ProxyCommand` 中，避免未来出现同名 notebook 后原生 `ssh` 变成歧义。
- `IdentityFile` 取当前 bootstrap 使用的本机私钥路径；如只知道 pubkey，应按约定推导同名私钥并验证存在。

后续可增加非 MVP 选项：

```bash
inspire notebook ssh-config <notebook> --install
inspire notebook ssh-config <notebook> --path ~/.ssh/config.d/inspire
```

`--install` 需要独立设计幂等更新和冲突处理，首版不做。

### ProxyCommand Entrypoint

```bash
inspire notebook ssh-proxy <notebook> [--workspace <workspace>] [--port <port>]
```

这是公开命令，并在 help 中说明它主要供 OpenSSH `ProxyCommand` 调用。

契约：

- stdin/stdout 必须是到远端 sshd 的纯字节流。
- 诊断、进度、错误只能写 stderr。
- 不打开交互 UI，不解释远程命令。
- 不打印表格、JSON、人类说明到 stdout。
- 退出码准确表示代理通道是否建立成功。
- 可以自动解析 workspace、刷新缓存、bootstrap tunnel。

示例 help 文案方向：

```text
Connect OpenSSH to a notebook SSH server through Inspire's managed tunnel.

This command is intended for ProxyCommand in ssh_config. It streams raw SSH
traffic on stdin/stdout; diagnostics are written to stderr.
```

### 连接管理入口

连接管理从 `ssh` 主路径中拆出，放在独立命令组：

```bash
inspire notebook connection list
inspire notebook connection status <notebook> [--workspace <workspace>]
inspire notebook connection refresh <notebook> [--workspace <workspace>]
inspire notebook connection forget <notebook> [--workspace <workspace>]
inspire notebook connection prune
```

语义：

- `list` 列出本地缓存的 notebook 连接，包括 notebook 名、workspace、notebook id、SSH 端口、上次验证时间和缓存健康状态。
- `status` 检查一个 notebook 的缓存和实际可连性；需要时可触发轻量验证，但不强制重建。
- `refresh` 强制重新解析 workspace/notebook id，重新 bootstrap tunnel/sshd，并更新缓存。
- `forget` 删除本地运行时缓存，不停止 notebook，不删除远端进程，也不修改用户的 OpenSSH 配置。
- `prune` 清理明显失效的缓存，例如 notebook 不存在、workspace 不可见、account 不匹配、TTL 过期且验证失败。

旧命令映射：

```bash
inspire notebook ssh test <notebook>     -> inspire notebook connection status <notebook>
inspire notebook ssh refresh <notebook>  -> inspire notebook connection refresh <notebook>
inspire notebook ssh forget <notebook>   -> inspire notebook connection forget <notebook>
inspire notebook ssh connect <notebook>  -> inspire notebook connection refresh <notebook>
```

`connect` 在新模型中不再是日常必要步骤，因为 `notebook ssh` 和原生 OpenSSH 通过 `ssh-proxy` 都会自动 resolve、bootstrap 和 cache。为了兼容旧脚本，`ssh connect` 可以继续存在，但 help 应明确推荐 `notebook ssh`、`notebook ssh-config` 和 `notebook connection refresh`。

### 兼容命令

保留以下旧入口：

```bash
inspire notebook ssh connect <notebook> --workspace <workspace>
inspire notebook ssh test <notebook>
inspire notebook ssh refresh <notebook>
inspire notebook ssh forget <notebook>
inspire notebook shell <notebook>
inspire notebook exec <notebook> "<command>"
inspire notebook scp <notebook> <source> <destination>
```

迁移策略：

- `ssh connect` 继续可用，但 help 标注为兼容入口，推荐 `notebook ssh` 或 `ssh-config`。
- `ssh test/refresh/forget` 继续可用，但 help 标注为兼容入口，推荐 `notebook connection status/refresh/forget`。
- `shell` 可逐步成为 `notebook ssh <notebook>` 的别名或保留为明确语义入口。
- `exec` 继续保留，因为它支持 artifact、`--no-wait`、denylist 等非 OpenSSH 能力。
- `scp` 继续保留为简化入口，但推荐高级用户用原生 `scp` + `ssh-config`。

### Click 路由约束

当前 `notebook ssh` 是 Click command group，第一位置参数会被解释成子命令。要同时支持：

```bash
inspire notebook ssh <notebook>
inspire notebook ssh connect <notebook>
```

需要显式处理路由，而不是简单给现有 group 增加一个参数。方案：

- 将 `ssh` 实现为自定义 Click group，保留 `connect/test/refresh/forget` 这些已知子命令。
- 当第一个 token 不是已知子命令时，路由到默认的 `open` 处理器，并把该 token 当作 notebook 名。
- 已知子命令名成为保留词；如果确实存在名为 `connect`、`test`、`refresh` 或 `forget` 的 notebook，提供 `inspire notebook ssh --notebook <name>` 作为逃逸路径。
- 在 help 中显示人类主入口，并把兼容子命令放在单独的“Compatibility commands”说明里。

## Workspace 自动探测

### 解析顺序

1. 如果用户传入 `--workspace`，只在该 workspace 中解析，并刷新缓存。
2. 如果本地缓存命中，验证 notebook id / 状态仍有效；有效则直接使用。
3. 如果缓存缺失或失效，跨可见 workspace 搜索 notebook 名。
4. 如果找到唯一候选，使用并缓存。
5. 如果找到多个候选：
   - 交互式终端中提示用户选择 workspace，并把选择作为 notebook 名的本地偏好缓存。
   - 非交互场景中报错并列出候选 workspace、状态、资源、notebook id，要求用户传 `--workspace`。
6. 如果找不到，报错并提示 `inspire notebook list --workspace all` 或等价发现命令。

### 缓存内容

缓存应按 active account 隔离，并至少包含：

- notebook name
- workspace name
- notebook id
- last observed runtime/container identity
- SSH user
- SSH port
- connection service port
- rtunnel/proxy metadata
- authorized pubkey fingerprint or path
- last verified timestamp

缓存是加速和记忆，不是事实来源。遇到 notebook 重启、id 变化、tunnel 失败、SSH 断开，应自动降级到重新探测和 refresh。

### 歧义处理

同名 notebook 在不同 workspace 中同时存在时，不能按固定顺序猜测。交互式终端中应让用户选择，并把选择缓存为本地偏好。选择编号沿用当前 Inspire CLI 的人工选择约定，从 1 开始：

```text
Notebook name is ambiguous: kchen-dev-copy

Candidates:
  1. CPU资源空间        RUNNING  0xCPU  notebook-...
  2. 分布式训练空间     STOPPED  8xGPU  notebook-...

Select workspace [1-2]: 1

Saved preference: kchen-dev-copy -> CPU资源空间
```

后续 `inspire notebook ssh kchen-dev-copy`、`ssh-proxy` 和 `connection` 命令优先使用该偏好；如果缓存的 workspace 中 notebook 不存在或 id 已失效，再重新探测并进入同样的选择流程。

非交互场景不能阻塞等待输入，错误输出应可执行：

```text
Notebook name is ambiguous: kchen-dev-copy

Candidates:
  1. CPU资源空间        RUNNING  0xCPU  notebook-...
  2. 分布式训练空间     STOPPED  8xGPU  notebook-...

Retry with:
  inspire notebook ssh kchen-dev-copy --workspace CPU资源空间
```

显式 `--workspace` 永远覆盖本地偏好，并应刷新该 notebook 名的偏好缓存。

偏好缓存是用户选择，不是平台事实。建议和连接缓存放在同一 account 作用域下，但作为单独字段保存：

- notebook name
- preferred workspace
- selected notebook id when known
- selected at timestamp
- last verified timestamp

## `ssh` 与 `ssh-proxy` 的边界

二者可以复用底层 resolver、bootstrap 和 tunnel 代码，但对外契约不同。

`inspire notebook ssh`：

- 面向人类。
- 可以显示进度。
- 可以打开交互 shell。
- 可以执行远程命令。
- 可以使用友好错误和下一步建议。

`inspire notebook ssh-proxy`：

- 面向 OpenSSH。
- stdout 是协议数据，不能污染。
- stderr 是诊断通道。
- 不执行远程命令。
- 不管理本机 OpenSSH 参数。

这条边界应反映在代码结构中：公共 resolver/bootstrap 逻辑放在共享模块，Click 命令只负责参数和 I/O 契约。

## `connection`、`ssh-config` 与 `ssh-proxy` 的关系

三者共享同一套 resolver/cache，但属于不同层：

```text
OpenSSH
  reads ssh_config
    calls ProxyCommand: inspire notebook ssh-proxy ...
      uses connection resolver/cache
        resolves workspace/notebook/runtime/tunnel
          connects to notebook sshd
```

`connection` 是运行时状态管理，负责缓存、检查、刷新和遗忘动态连接事实。它可以保存 notebook id、workspace、runtime/container identity、rtunnel/proxy metadata、上次验证时间等易变信息。

`ssh-config` 是 OpenSSH 接入配置生成器，负责输出稳定配置片段。它应该固化 Host alias、notebook 解析名、workspace、SSH user、SSH port、ProxyCommand 和 IdentityFile，但不应该固化 rtunnel URL、Jupyter proxy URL、本地临时端口、runtime id 或临时 token。

`ssh-proxy` 是 OpenSSH 到 Inspire 管理隧道的协议边界。每次原生 `ssh`、`scp`、`sftp`、`rsync -e ssh` 或 VS Code Remote SSH 连接时，OpenSSH 调用 `ssh-proxy`，再由 `ssh-proxy` 使用 `connection` resolver/cache 找到当前可用通道。

因此：

- `ssh-config` 输出应尽量稳定，用户不需要在 notebook 重启后重新生成配置。
- `connection` 缓存可以频繁变化，坏了可以自动刷新或由用户显式 `refresh/forget/prune`。
- `connection forget/prune` 不删除 `~/.ssh/config` 中的 Host 片段。
- 如果将来需要管理 `~/.ssh/config` 文件，应设计在 `ssh-config --install/--uninstall`，不要混进 `connection`。

## 实现步骤

1. 抽出 notebook connection resolver
   - 支持按 notebook 名自动查 workspace。
   - 支持缓存读取、验证、刷新。
   - 支持歧义错误结构化输出。

2. 新增 `inspire notebook ssh-proxy`
   - 复用现有 rtunnel ProxyCommand 能力。
   - 保证 stdout 只承载 raw stream。
   - 为 stderr 诊断加测试。

3. 改造 `inspire notebook ssh`
   - 从命令组调整为人类主入口。
   - 解析 `<notebook> [-- <remote-command>...]`。
   - 将旧 `connect/test/refresh/forget` 保留为兼容子命令或迁移到 `connection` 命令组。

4. 新增 `inspire notebook connection`
   - 提供 `list/status/refresh/forget/prune`。
   - 复用 resolver/cache，不输出或管理 OpenSSH 配置。
   - 将旧 `ssh test/refresh/forget/connect` 映射为兼容入口。

5. 重新引入 `inspire notebook ssh-config`
   - 默认打印 OpenSSH config。
   - 使用 `ProxyCommand inspire notebook ssh-proxy %h --workspace <resolved-workspace> --port %p`。
   - 支持 `--host` 自定义 Host alias。

6. 更新文档
   - `references/notebook.md` 改为新主路径。
   - `references/notebook-service-proxy.md` 中涉及 SSH 前置步骤的例子同步。
   - skill 文档中的默认路径同步为 `notebook ssh` / 原生 OpenSSH。

7. 兼容与弃用
   - 旧命令继续通过测试。
   - help 中明确推荐新命令。
   - 至少一个 minor 版本后再考虑隐藏或移除旧 `ssh connect` 入口。

## 测试计划

- CLI help 边界测试：
  - `notebook ssh --help` 展示主入口和兼容说明。
  - `notebook ssh-proxy --help` 说明 ProxyCommand 契约。
  - `notebook ssh-config --help` 存在并展示示例。

- 参数解析测试：
  - `notebook ssh nb` 打开交互路径。
  - `notebook ssh nb -- hostname` 生成远程命令 argv。
  - `notebook ssh nb --workspace CPU资源空间 -- hostname` 使用显式 workspace。

- resolver 测试：
  - 缓存命中。
  - 缓存失效后自动探测。
  - 唯一候选自动缓存。
  - 多候选时报歧义错误。
  - 显式 workspace 覆盖缓存。

- `ssh-config` 输出测试：
  - stdout 是合法 ssh_config 片段。
  - `ProxyCommand` 使用 `inspire notebook ssh-proxy %h --workspace <resolved-workspace> --port %p`。
  - `Host` alias 可自定义且默认值稳定。

- `ssh-proxy` I/O 测试：
  - 正常路径不向 stdout 写诊断。
  - 错误路径只向 stderr 写诊断。
  - 退出码传播正确。

- `connection` 命令测试：
  - `connection list` 展示缓存条目和健康状态。
  - `connection status` 验证缓存但不强制重建。
  - `connection refresh` 强制重建并更新缓存。
  - `connection forget` 删除缓存但不修改 ssh_config。
  - `connection prune` 只删除失效缓存。

- 回归测试：
  - 旧 `ssh connect/test/refresh/forget` 仍可用。
  - `exec`、`shell`、`scp` 继续复用缓存。
  - rtunnel bootstrap 和自动重连行为不回退。

## 风险与取舍

- 跨 workspace 自动探测会增加首次连接延迟。缓存命中后应避免重复查询。
- 同名 notebook 歧义必须报错，不能按 workspace 顺序猜测。
- `ssh-proxy` 内部如果需要 bootstrap，可能在 OpenSSH 连接建立前产生较长等待；需要 stderr 进度和合理 timeout。
- `ProxyCommand` 无法表达所有 Inspire 专有上下文，因此 notebook 名、workspace 和 account 隔离必须由 `inspire` 自己处理。
- 如果用户在 `~/.ssh/config` 中固定了 `HostName`，重命名 notebook 后需要重新生成配置或使用新的 alias。
- 用户可能误以为 `connection forget` 会删除 OpenSSH Host 配置；help 和输出必须明确它只删除 Inspire 运行时缓存。

## 建议的发布口径

用户日常入口：

```bash
inspire notebook ssh <notebook>
inspire notebook ssh <notebook> -- hostname
```

OpenSSH 集成入口：

```bash
inspire notebook ssh-config <notebook> >> ~/.ssh/config
ssh inspire-<notebook>
scp file inspire-<notebook>:/tmp/
```

连接管理入口：

```bash
inspire notebook connection list
inspire notebook connection refresh <notebook>
inspire notebook connection forget <notebook>
```

底层代理入口：

```bash
ProxyCommand inspire notebook ssh-proxy %h --workspace <workspace> --port %p
```

一句话说明：`inspire notebook ssh` 像 SSH 一样使用；`ssh-config` 让原生 OpenSSH 工具接入；`ssh-proxy` 是 OpenSSH 调用 Inspire 管理隧道的稳定边界。
