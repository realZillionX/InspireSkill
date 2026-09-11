# InspireSkill CLI

`inspire-skill` is the installable command-line interface for the Inspire
compute platform. The package exposes the `inspire` executable.

```bash
python -m pip install inspire-skill
inspire --help
```

The public CLI resolves resources by name. Human-readable output and the root
`--json` mode expose names, aliases, readable state, and bounded collection
metadata rather than implementation metadata. Use `--limit/-n` or `--all`
where offered, and consult the installed command help for the current syntax.

`inspire account use <name>` sets the saved default account. Every command
accepts `--account <name>` to override it for that invocation, including at the
root, command-group, or subcommand position. Commands keep their selected
account throughout execution. Switching the default preserves each account's
sessions, SSH connections, and resource caches.

Project documentation:

- [Project overview](https://github.com/realZillionX/InspireSkill/blob/main/README.md)
- [Capability overview](https://github.com/realZillionX/InspireSkill/blob/main/README.md#能力一览)
- [Agent Skill](https://github.com/realZillionX/InspireSkill/blob/main/SKILL.md)
- [Usage references](https://github.com/realZillionX/InspireSkill/tree/main/references)
- [Development guide](https://github.com/realZillionX/InspireSkill/blob/main/CONTRIBUTING.md)

## Python SDK

实验性 Python SDK 提供两个入口：同步脚本使用 `from inspire import InspireClient`，asyncio 应用使用 `from inspire import InspireAsyncClient`。两者复用同一包的账号、共享服务与统一 Transport，提供 `workspaces`、`projects`、`compute_groups`、`images`、`datasets`、`models`、`resources`、`account_info`、`api_keys`、`jobs`、`notebooks`、`hpc`、`ray`、`servings`、`tensorboards`。交互配置、SSH 建桥工具和终端渲染仍由 CLI 提供；参见 [Python SDK 指南](../references/sdk.md)。

两种客户端均支持凭据构造、默认无浏览器的登录与非交互初始化，另有同步的 `Accounts` 本地账号管理。同步用 `with`、`login()`／`init()`；异步用 `async with`、`await login()`／`await init()`。异步客户端用专用线程池运行底层同步栈，`concurrency=1` 默认串行，可按同时请求与长期流的数量增加成员；普通方法 await，迭代和 follow 用 async for，五个 exec 门面另有 `exec_stream`。Exec 默认捕获最多 4 MiB，支持写文件、回调及关闭捕获；详见 [SDK 文档](../references/sdk.md)。
