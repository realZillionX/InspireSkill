# 远端路径

理解共享盘作用域、存储池、挂载隔离和 Path Alias 时先看本页。调度条件和资源规格看 [`resources.md`](resources.md)；联网准备和内部源看 [`internal-sources.md`](internal-sources.md)。

## 1. 路径不是调度条件

Path Alias 是远端路径 Alias，不是 Workload Profile。它只回答“文件在哪里”，不回答“任务在哪里跑”。

| 概念 | 保存什么 | 不能替代 |
| --- | --- | --- |
| Workload Profile | `workspace`、`project`、`group`、`quota`、`image` | 远端目录 |
| Path Alias | 共享盘上的目录 | `workspace`、`project`、`group`、`quota`、`image` |

Path Alias 只保存在仓库级、账号隔离的 `[path_aliases]` 中。账号配置不保存项目路径，因为一个账号可以访问多个互相隔离的 Project Fileset。省略 `--cwd` 时 CLI 不注入 `cd`，保留远端运行时的初始目录；只有显式传 `--cwd <alias>` 时才解析 Alias。

## 2. 路径作用域

| 作用域 | 路径形状 | 定位 |
| --- | --- | --- |
| 项目个人 | `/inspire/<tier>/project/<topic>/<path-user>/...` | 每项目、每账号一份，适合代码、脚本、调试输出 |
| 项目公共 | `/inspire/<tier>/project/<topic>/public/...` | 项目成员共享，适合数据集、权重、批量结果、checkpoint |
| 全局个人 | `/inspire/<tier>/global_user/<path-user>/...` | 跨项目个人盘，适合小工具和中转 |
| 全局公共 | `/inspire/hdd/global_public/...` | 全平台共享，普通账号通常只读 |

`<path-user>` 是共享盘返回的个人目录名，不一定等于平台登录 username。路径事实以 `inspire init` 的平台发现结果为准。

`/inspire/dataset/<数据集名>/<版本名>` 不属于共享盘，是创建时用 `--dataset` 挂进来的官方数据集，只读，不占项目配额，也没有 Alias；语义见 [`dataset.md`](dataset.md)。

## 3. 存储池

| 池 | 项目路径前缀 | 定位 |
| --- | --- | --- |
| SSD `gpfs_flash` | `/inspire/ssd/project/<topic>/` | 训练 Hot Path、活跃工作集、Checkpoint 热点 |
| HDD `gpfs_hdd` | `/inspire/hdd/project/<topic>/` | 通用空间，写前看剩余容量 |
| qb-ilm `qb_prod_ipfs01` | `/inspire/qb-ilm/project/<topic>/` | 大容量，顺序读带宽接近 SSD |
| qb-ilm2 `qb_prod_ipfs02` | `/inspire/qb-ilm2/project/<topic>/` | 新且空余多，新增大数据默认优先考虑 |

`global_public` 只在 HDD。需要 SSD 或 QB-ILM 速度时，优先走项目个人或项目公共路径。

## 4. 挂载隔离

实例只挂自身所在项目的 Fileset。其它项目的 `/inspire/{hdd,ssd,qb-ilm,qb-ilm2}/project/<others>/` 在该实例里通常不存在，`ls` 报 `No such file` 不是权限问题。

跨项目搬小文件时，在两个项目各起一个 Notebook，用全局个人路径中转。大数据集或全量 Checkpoint 超出个人 Quota 时，联系项目管理员处理。

## 5. Alias 语义

默认 Alias 由 `inspire init` 写入账号配置；当前 Repo 需要覆盖时，由 `inspire init --scope project` 写入仓库级配置，日常增删改用 `inspire notebook path list/set/delete`。项目初始化问询和路径约定的持续维护见 [`project-context.md`](project-context.md)。

| Alias | 指向 |
| --- | --- |
| `me` | 当前项目、当前账号、初始化时选择的默认存储池 |
| `public` | 当前项目公共目录、初始化时选择的默认存储池 |
| `global-me` | 当前账号全局目录 |
| `<tier>.me` | 指定存储池下的项目个人目录，例如 `ssd.me`、`hdd.me`、`qb-ilm2.me` |
| `<tier>.public` | 指定存储池下的项目公共目录 |
| `<tier>.global-me` | 指定存储池下的全局个人目录 |

路径参数支持 `alias`、`alias:<subdir>` 和命名 Alias 三种模型。具体命令语法看 CLI Help；Reference 只强调语义：把代码、数据、权重和产物放到远端路径约定里，不把路径含义塞进 Workload Profile。
