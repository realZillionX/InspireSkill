# 资源、规格与远端路径

日常选择 workspace、compute group、`--quota` 三元组、项目 / 用户配额、存储池、path alias，或解释实例里的路径为什么不可见时，先查本手册。Notebook、job、HPC、Ray 和 serving 的生命周期操作看对应业务手册。

## 1. 资源查询入口

资源、规格、项目和用户相关能力以 CLI help 为准，不在手册里维护硬编码命令清单。

```bash
inspire resources --help
inspire resources specs --help
inspire project --help
inspire user --help
```

日常先看 resources 的 human 表格，再按需锁定 workload、workspace 或 compute group。看项目预算和配额时使用 project 入口；确认当前身份、workspace 权限码或 API Key 元数据时使用 user 入口。

日常读取人类表格。结构化输出只用于脚本，不作为默认观察面。

资源和可用量以平台实时查询为准。`resources specs`、`resources list`、`resources nodes`、`project` 和 `user` 的输出是当前决策依据；本地缓存、历史截图和旧文档不能当作资源事实。

不存在默认 workspace。创建 notebook、job、HPC、Ray 或 serving 时，`workspace`、`project`、`group`、`quota` 和 `image` 必须显式传入，或通过 workload profile 填入；path alias 只用于远端路径，不能替代这些调度条件。

申请资源前先查实时空余，再按真实需求申请。不要因为模型保守而主动缩小规模；只有调度语义、项目配额或实时空余明确不足时才降档。

## 2. 规格三元组

`--quota` / `-q` 的格式是：

```bash
<gpu>,<cpu>,<mem>
```

`mem` 以 GiB 计。例如 `1,20,200` 表示 1 张 GPU、20 核 CPU、200 GiB 内存；CPU-only 写 `0,4,32`。

GPU 型号由 workspace 和 compute group 决定，不写进三元组。三元组必须在当前可见规格里唯一匹配；如果多组撞上同一三元组，加 `--group <name>` 或命令对应的 compute group 参数。

## 3. 远端路径作用域

先决定作用域，再选存储池。CLI 侧不要维护单独的“远端工作目录”字段；项目路径统一通过仓库级 `[path_aliases]` 表达。

| 作用域 | 路径样例 | 定位 |
| --- | --- | --- |
| 项目个人 | `/inspire/<tier>/project/<topic>/<user>/...` | 每项目、每用户一份。适合代码、脚本、配置、调试输出 |
| 项目公共 | `/inspire/<tier>/project/<topic>/public/...` | 项目成员共享。适合数据集、权重、批量结果、checkpoint |
| 全局个人 | `/inspire/<tier>/global_user/<user>/...` | 跨项目个人盘，适合脚本、配置、小工具和跨项目小文件中转 |
| 全局公共 | `/inspire/hdd/global_public/...` | 全平台共享，普通用户只读，稳定共享物由维护者统一放置 |

## 4. 存储池

| 池 | 项目路径前缀 | 定位 |
| --- | --- | --- |
| SSD `gpfs_flash` | `/inspire/ssd/project/<topic>/` | 训练 hot path、活跃工作集、checkpoint 热点 |
| HDD `gpfs_hdd` | `/inspire/hdd/project/<topic>/` | 通用空间，写前看剩余容量 |
| qb-ilm `qb_prod_ipfs01` | `/inspire/qb-ilm/project/<topic>/` | 大容量，顺序读带宽接近 SSD |
| qb-ilm2 `qb_prod_ipfs02` | `/inspire/qb-ilm2/project/<topic>/` | 新且空余多，新增大数据默认优先考虑 |

`global_public` 只在 hdd。需要 SSD 或 qb-ilm 速度时，优先走项目个人或项目公共路径。

## 5. 挂载隔离

实例只挂自身所在项目的 fileset。其它项目的 `/inspire/{hdd,ssd,qb-ilm,qb-ilm2}/project/<others>/` 在该实例里通常不存在，`ls` 报 `No such file` 不是权限问题。

跨项目搬小文件时，在两个项目各起一个 notebook，用 `/inspire/hdd/global_user/<user>/` 中转。大数据集或全量 checkpoint 超出个人 quota 时，联系项目管理员处理。

## 6. Path Alias 配置入口

项目远端路径由 `inspire init` 写入当前仓库的 `.inspire/config.toml`，落在 `[path_aliases]`。查看生效配置用：

```bash
inspire config show --compact
inspire config context
```

默认 alias 语义：

| Alias | 指向 |
| --- | --- |
| `me` | 当前项目、当前用户、初始化时选择的默认存储池 |
| `public` | 当前项目公共目录、初始化时选择的默认存储池 |
| `global-me` | 当前用户全局目录 |
| `<tier>.me` | 指定存储池下的项目个人目录，例如 `ssd.me`、`hdd.me`、`qb-ilm2.me` |
| `<tier>.public` | 指定存储池下的项目公共目录 |
| `<tier>.global-me` | 指定存储池下的全局个人目录 |

路径参数支持三种写法：

```bash
inspire notebook exec <name> --cwd me "pwd"
inspire notebook exec <name> --cwd me:<repo> "git pull"
inspire notebook scp <name> ./config.yaml me:<repo>/config.yaml
```

需要给常用子目录命名时，从任意可达 notebook 记录 alias：

```bash
inspire notebook set-path <name> /inspire/ssd/project/<topic>/<user>/<repo> as repo
inspire notebook exec <name> --cwd repo "pytest -q"
```

不要把配置文件内容复制到项目说明里；仓库级语义说明写在 `INSPIRE.md`。

## 7. 项目、负责人和用户元数据

日常看配额、预算和优先级时先查 `inspire project --help`，更细的项目详情和负责人下拉只在确认归属或预算拆分时使用。当前登录身份、workspace 权限码和 API Key 元数据从 `inspire user --help` 选择对应子命令；项目配额是普通任务决策的默认依据。

API Key 值只在创建时一次性下发；创建 / 删除走平台用户中心页面。
