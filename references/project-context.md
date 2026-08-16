# 项目上下文：初始化与持续维护

把一个本地项目工作区接入启智（项目初始化），或项目事实变化需要更新项目信息时看本页。账号级安装、登录和全局发现见 [`setup/install-and-config.md`](setup/install-and-config.md)；路径作用域和存储池语义见 [`paths.md`](paths.md)。命令语法和参数以 CLI Help 为准。

项目上下文有两个载体，缺一不可：

| 载体 | 性质 | 内容 |
| --- | --- | --- |
| `./.inspire/` 项目配置 | CLI 读取的机器可读层 | `./.inspire/config.toml` 是仓库共享层（所有账号共用的 `[cli]` 设置）；`./.inspire/accounts/<account>/config.toml` 是账号覆盖层（本仓库的 Project Context 和 Path Alias 覆盖） |
| `INSPIRE.md` | 跨 Agent、跨会话、跨成员共享的人类可读资产合同 | 稳定平台拓扑、Canonical Remote Paths、永久基础设施、资产身份与生命周期 |

## 1. 何时初始化

同时满足以下条件时进入初始化流程：

1. 当前工作区对应一个明确的启智科研或工程项目，而不是 CLI、Skill、文档或其它通用工具源码仓库。
2. `INSPIRE.md` 或 `./.inspire/` 项目配置缺失、过期，或用户明确要求把项目接入启智。

账号级全局发现（`inspire init`）是前置条件，先按 [`setup/install-and-config.md`](setup/install-and-config.md) 完成。项目只做一次性临时操作时不初始化，也不创建空壳 `INSPIRE.md`。

## 2. 四项信息先问清

初始化不是跑一条命令，而是一次信息核对。以下四项信息决定这个项目今后所有 Workload 的去向；**Live 查询给出候选，归属判断必须由用户确认，不要按仓库名、列表顺序或相似度猜测**：

| 信息 | 先做什么 | 向用户问清什么 |
| --- | --- | --- |
| Project | `inspire project list` 列出可见候选（项目是全局对象，不按 Workspace 划分） | 本仓库归属哪个平台 Project？多候选时必须让用户指认 |
| Workspace | 无需查询：`CPU资源空间`（CPU、联网准备）和 `分布式训练空间`（GPU）是所有用户默认可用的公共 Workspace | 项目是否还有专属 Workspace（项目空间、国产卡分区等）？**专属 Workspace 只能由用户亲自指认**；确认后记录它的职责和适用任务 |
| Paths | 查看共享盘现状，例如用已有 Notebook `ls` 项目目录 | 默认存储池选哪个（`ssd` / `hdd` / `qb-ilm` / `qb-ilm2`）？远端代码 checkout、公共数据、权重、Checkpoint 是否已有约定路径？老项目沿用现有约定，新项目让用户定 |
| Image | `inspire image list` 查项目可见镜像 | 项目是否已有验证过的基底镜像？名称是什么、覆盖哪些依赖？没有时记为待建立，后续按 [`internal-sources.md`](internal-sources.md) 和 [`notebook.md`](notebook.md) 建立后回填 |

用户暂时回答不了的项（例如镜像还没建），如实记为待建立；不要编造，也不要留下看似已确认的占位值。

## 3. 执行初始化

信息确认后，在仓库根执行：

```bash
inspire init --scope project
```

交互流程会选择 Project 和默认存储池；已从用户拿到答案时用 `--select-project <name>` 直接传入。写入内容是账号覆盖层的 Project Context 和发现到的 Path Alias。不要维护单独的“远端工作目录”字段；远端目录一律用 Path Alias 表达（`me`、`me:<repo>`、`public` 等）。

需要让 `inspire ...` 在本仓库稳定加载项目 `.env` 时，登记到仓库共享层：

```bash
inspire init --scope project --env-file .env
```

真实父进程环境变量仍优先于 `.env`。

初始化后核验，确认写入结果与用户确认的信息一致：

```bash
inspire account context
inspire notebook path list
```

`<workload> create` 省略某个参数时会落到仓库级默认值（`job.shm_size`、`notebook.post_start` 等），写在 `./.inspire/config.toml`；同名环境变量默认优先，`[cli] prefer_source = "toml"` 可以反转这个优先级。

账号覆盖层按账号隔离：同一仓库的新成员或新账号需要各自跑一次 `inspire init --scope project`；`INSPIRE.md` 和仓库共享层则随 Git 共享。

## 4. INSPIRE.md 资产合同

`INSPIRE.md` 让不同 Harness、不同成员和未来 Agent 共享同一套稳定项目事实。只有项目在启智上有需要长期复用的稳定拓扑、共享路径、基础设施或重资产，且这些事实需要跨 Agent、跨会话共享时才创建。

### 应记录的内容

初始化问清的四项信息是它的第一批内容：

| 类别 | 适合记录的稳定事实 |
| --- | --- |
| 平台拓扑 | Project 名称；公共 Workspace 的职责分工；用户指认的专属 Workspace 及其适用任务；持久存储池选择 |
| Canonical Remote Paths | 远端代码 checkout、公共资产根、数据、模型、Checkpoint、运行包和交付物的规范路径 |
| 真源与同步合同 | Git 真源、远端 checkout 的角色、同步方式、目标分支或精确 Commit 要求 |
| 永久基础设施 | 有当前或明确未来消费者的固定 Notebook、服务入口或构建环境；记录稳定名称与职责，不记录动态 URL 或运行状态 |
| 资产身份 | 项目基底镜像名称与用途、Model / Dataset Revision、Tokenizer / Vocabulary、Manifest、Hash、来源、构建配方与消费者 |
| 复现合同 | 正式运行所需的数据、实现、环境、启动参数、结果和校验身份 |
| 生命周期 | 永久资产与一次性中转资产的判断、删除前的消费者 / 引用 / 替代物 / 唯一副本审计 |

资产不会仅因体积大、生成昂贵、曾经使用或可以重建而自动成为永久资产；以当前或明确未来消费者、正式引用、替代关系和唯一副本为准。

### 不应记录的内容

- 账号配置、密码、代理密钥、平台 Session、API Key 或 `.inspire/accounts/<account>/config.toml` 内容。
- 实时资源余量、当前 Notebook / Job 状态、动态 Proxy URL、临时节点名、排队结果或测速数据。
- 原始日志、Smoke 输出、实验流水、进度报告和已完成工作的过程记录。
- 本地 Agent 的短期计划、执行风格或 Harness 私有说明；这些内容留在 `PLAN.md`、`AGENTS.md` 或 `CLAUDE.md`。
- 可从 CLI Live 查询得到、且可能频繁变化的事实。执行前仍应使用 CLI Help 和 Live 查询确认当前平台状态。

### 推荐结构

根据项目实际资产裁剪，不为缺失内容保留空章节：

```markdown
# <项目名> Inspire 平台与资产合同

## 平台拓扑
## Canonical Remote Paths
## 代码真源与同步合同
## 永久基础设施
## 永久资产与复现资产
## 临时资产清理
```

## 5. 持续维护

项目信息不是一次性写完的。执行任务的过程中命中以下触发点时，当场同步项目上下文，不要等用户提醒：

| 触发点 | 维护动作 |
| --- | --- |
| 保存或更换项目基底镜像 | 更新 `INSPIRE.md` 资产身份：镜像名称、用途、覆盖的依赖 |
| Canonical Path 新增、迁移或退役 | 用 `inspire notebook path set/delete` 更新 Path Alias，同步 `INSPIRE.md` 路径章节 |
| 用户指认了新的专属 Workspace | 更新 `INSPIRE.md` 平台拓扑及其职责分工 |
| 新增永久基础设施（固定 Notebook、Serving 入口、构建环境） | 登记稳定名称与职责 |
| 注册了新的 Model、沉淀了新的正式产物 | 记录身份、来源和消费者 |
| 资产退役 | 按生命周期审计消费者后删除条目和远端资产 |
| 平台侧 Project、Workspace 或共享盘布局变化 | 重跑 `inspire init --scope project`，再核对 `INSPIRE.md` |

两条判断规则：

- **漂移以 Live 为准**：发现 `INSPIRE.md` 与 Live 查询不一致时，先核实再当场修正文档；无法判断哪边过期时问用户，不要沿用已知错误的记录继续执行。
- **会话收尾即维护**：本次会话产生的持久资产（镜像、注册模型、固定 Notebook、正式产物路径）写回 `INSPIRE.md` 后再结束；一次性中转资产用完即删，不写入。

只在稳定合同变化时更新 `INSPIRE.md`。运行状态、一次性验证和短期计划完成后不应沉淀到该文件。
