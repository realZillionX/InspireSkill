# 持久资产合同

需要跨 Agent、成员和会话复用启智平台上的稳定资产时看本页。平台 Project 的负责人、预算和优先级用 `inspire project`查 Live 事实；远端路径形状和挂载隔离见 [`paths.md`](paths.md)。

## 不存在仓库绑定

Inspire CLI 只有账号级持久配置 `~/.inspire/`，不读写仓库级 `./.inspire/`。一个仓库可以在多个 Project、Workspace 和存储池之间切换；每次 Workload 都显式传入 `workspace`、`project`、`group`、`quota` 和 `image`，每次文件操作都显式使用远端绝对路径。

`INSPIRE.md` 不是 CLI 配置，CLI 不解析它。它只是可选的人类可读资产合同，可以同时描述多个 Project 中的资产。仓库没有需要长期复用的启智资产时不创建空壳文件。

## 应记录的稳定事实

| 类别 | 内容 |
| --- | --- |
| 适用范围 | 每项资产所属的 Project、适用 Workspace 和存储池 |
| Canonical Remote Paths | 代码 checkout、公共数据、权重、Checkpoint、模型和交付物的绝对路径 |
| 真源与同步 | Git 真源、目标分支或 Commit、远端 checkout 的角色和同步方式 |
| 永久基础设施 | 有当前或明确未来消费者的固定 Notebook、Serving 入口或构建环境 |
| 资产身份 | 基底镜像、Model / Dataset Revision、Tokenizer / Vocabulary、Manifest、来源、构建配方与消费者 |
| 复现合同 | 正式运行所需的数据、实现、环境、启动参数、结果和校验身份 |
| 生命周期 | 删除前的消费者、引用、替代物和唯一副本审计 |

资产不会仅因体积大、生成昂贵、曾经使用或可以重建而自动成为永久资产；以当前或明确未来消费者、正式引用、替代关系和唯一副本为准。

## 不应记录

- 账号配置、密码、代理密钥、Session 或 API Key。
- 实时资源余量、当前 Workload 状态、动态 URL、排队结果或测速数据。
- 原始日志、Smoke 输出、实验流水、进度报告和短期计划。
- 可从 CLI Live 查询、且可能频繁变化的调度条件。

## 持续维护

保存或更换基底镜像、迁移 Canonical Path、新增永久基础设施、注册新 Model 或退役资产时，当场同步 `INSPIRE.md`。发现文档与 Live 查询漂移时以 Live 为准并修正。一次性中转资产用完即删，不写入持久文档。
