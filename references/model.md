# Model Registry

浏览模型仓库、注册平台可见模型目录、查看模型版本，或判断 model registry 和 serving 的边界时，先查本手册。部署生命周期看 [compute-workloads.md](compute-workloads.md) 的 serving 部分。

命令是否存在、参数名和默认值以 CLI help 为准：

```bash
inspire model --help
inspire model <subcommand> --help
```

## 1. 定位与边界

`inspire model` 是启智平台模型仓库入口。

- `list/status/versions` 是只读观察命令，默认只查当前用户的模型，并支持按 workspace、project 和 keyword 收窄。
- `register` 用 Browser API 把平台可见目录注册成模型首版本；它不上传本地文件，也不修改已有模型。
- 删除、改元数据、追加版本和发布到模型广场仍以平台页面为准。

网页端注册表单的实测规则：

- 模型名支持字母、数字、下划线、短横线和点，且只能以字母开头。
- 模型存储位置必须是当前空间下的项目目录路径。路径不存在或不是该空间项目目录时，后端会返回“模型源路径不存在或访问异常”。
- 模型类型是级联分类；自定义标签适合放任务类型、训练框架、License 和业务标记。

## 2. 与 `serving` 的关系

| 命令组 | 定位 | 不负责 |
| --- | --- | --- |
| `model` | 浏览 / 注册模型仓库条目，选择模型和版本 | 不创建部署、不停止服务 |
| `serving` | 创建、观察和停止模型部署服务 | 不上传模型、不浏览模型版本 |

流程通常是先用 `model` 找到目标模型和版本，再用 `serving create` 创建部署；服务启动后，用 `serving list/status/metrics/stop/delete` 观察、止损和清理。`model` 是“管理仓库条目和版本”，`serving` 是“管理在线服务”。

## 3. 操作判断

| 目标 | 入口 | 后续动作 |
| --- | --- | --- |
| 看当前 workspace 有哪些模型 | `model list` | 找到候选模型名后再看详情 |
| 看某个模型的元数据和版本摘要 | `model status <model-name>` | 确认存储路径、版本和可部署性 |
| 看历史版本 | `model versions <model-name>` | 选择要部署或复现的版本 |
| 注册平台可见目录 | `model register` | 目录必须已在启智共享存储中 |
| 创建部署服务 | `serving create` | 转到 [compute-workloads.md](compute-workloads.md) |

```bash
inspire model list
inspire model list --workspace <WORKSPACE>
inspire model status <model-name>
inspire model versions <model-name>
inspire model register --name <model-name> --source-path <REMOTE_PATH> --workspace <WORKSPACE> --project <PROJECT>
```

## 4. 限制

- `model register` 只注册平台侧已经可访问的目录；本地上传、追加版本、删除和发布不在 CLI 覆盖内。
- model registry 与 model deployment 是两个不同的平台模块；前者是仓库浏览，后者是服务生命周期管理。
- 日常默认看 human 输出；只有脚本消费字段或需要精确结构时才用 `--json`。
