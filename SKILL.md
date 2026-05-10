---
name: inspire
description: "Execution-first Inspire platform CLI usage manual, with on-demand references for platform workflows."
---

# Inspire Skill

`inspire` CLI 的日常资料只描述命令可观察行为：状态、事件、指标、日志、资源余量和配置来源都通过 CLI 实时查询；命令是否存在、参数叫什么、默认值是什么，永远以 CLI help 为准。

`references/dev/` 只放开发者手册。只有维护 CLI 封装、排查 API 合约或用户明确要求看平台接口时，才加载这些文件。

## 1. 使用流程

命令列表、子命令功能和参数说明以 CLI help 为准，不在 SKILL 或 references 中维护硬编码清单。需要确认某个操作时，先查 help，再执行实时查询或提交。不要把旧文档、记忆或历史示例当作命令存在性的事实来源。

```bash
inspire --help
inspire <command-group> --help
inspire <command-group> <subcommand> --help
```

在本仓库源码 checkout 内验证 CLI 行为时，用：

```bash
cd cli
uv run inspire --help
uv run inspire notebook --help
uv run inspire hpc create --help
```

`inspire --help` 的 `Commands` 区给出当前版本真实命令组；`inspire <command-group> --help` 给出该组所有子命令；`inspire <command-group> <subcommand> --help` 给出参数、默认值、必填项和注意事项。

`workspace`、`project`、`group`、`quota` 和 `image` 没有隐式默认值。需要复用条件组时，先查 `inspire <command-group> profile --help`，用 `inspire notebook/job/hpc/ray/serving profile set <name> ...` 保存 workload profile；创建命令显式传 `--profile <name>`，batch 条目写 `profile = "<name>"`。Path alias 只表示远端路径，不能当作 workload profile。

每次任务按这个顺序走：

1. 用 help 确认命令和参数。
2. 加载一份最相关的日常使用手册。
3. 任务跨边界时，再加载第二份使用手册。
4. 执行前用 CLI 实时查询确认平台状态。

## 2. 按需加载索引

每次优先只加载一份日常使用手册；任务跨边界时再加载第二份。不要用开发者手册替代 CLI help 或日常使用手册。

| 场景 | 手册 |
| --- | --- |
| 选择 workspace、compute group、quota、项目配额、存储池、path alias，或解释路径不可见 | [references/resources-and-paths.md](references/resources-and-paths.md) |
| 创建、连接、执行、传文件，或准备 notebook 基底环境 | [references/notebook.md](references/notebook.md) |
| 把 notebook 容器内 HTTP 服务暴露给浏览器、SDK 或小组成员 | [references/notebook-service-proxy.md](references/notebook-service-proxy.md) |
| 提交 GPU job、HPC、Ray、serving，或观察事件、日志和指标 | [references/compute-workloads.md](references/compute-workloads.md) |
| 一个项目要从环境准备、数据处理推进到训练 | [references/workflows.md](references/workflows.md) |
| 浏览、注册、保存、调整可见性或清理镜像 | [references/image-management.md](references/image-management.md) |
| 浏览或注册模型仓库条目，判断 model registry 和 serving 的关系 | [references/model.md](references/model.md) |
| 安装、更新、账号、项目初始化、代理 setup | [references/setup/install-and-config.md](references/setup/install-and-config.md)、[references/setup/proxy-setup.md](references/setup/proxy-setup.md) |

开发者手册：

| 场景 | 手册 |
| --- | --- |
| 维护 CLI 封装、排查 API 合约或对照平台接口 | [references/dev/openapi.md](references/dev/openapi.md)、[references/dev/browser-api.md](references/dev/browser-api.md) |

## 3. 项目上下文

仓库根可用 `INSPIRE.md` 记录非配置性上下文，建议包含：

- `Default Image`
- `Path Conventions`
- `Public Directory Layout`
- `Existing Notebooks`
- `Ongoing Jobs`

不要把账号配置、密码、代理密钥或 `.inspire/config.toml` 内容复制进 `INSPIRE.md`。配置由 CLI 合并和展示。
