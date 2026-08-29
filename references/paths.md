# 远端路径

理解共享盘作用域、存储池、挂载隔离和绝对路径时看本页。调度条件和资源规格见 [`resources.md`](resources.md)；联网准备和内部源见 [`internal-sources.md`](internal-sources.md)。

## 路径不是调度条件

CLI 不保存 Path Alias，也不从当前仓库推导远端目录。`workspace`、`project`、`group`、`quota` 和 `image` 决定任务在哪里运行；`--cwd`、SCP 源/目标和业务命令中的 `/inspire/...` 绝对路径决定文件在哪里。

需要缩写时使用当前 shell 的环境变量：

```bash
export REMOTE_ROOT=/inspire/qb-ilm2/project/<topic>/<path-user>
inspire notebook exec <notebook> --cwd "$REMOTE_ROOT/repo" "python train.py"
inspire notebook scp <notebook> ./config.yaml "$REMOTE_ROOT/repo/config.yaml"
```

`notebook exec` / `shell` 的 `--cwd` 只接受绝对远端路径；省略时 CLI 不注入 `cd`，保留远端运行时的初始目录。SCP 仍允许相对远端路径，但会警告其含义依赖远端 Shell。

## 路径作用域

| 作用域 | 路径形状 | 定位 |
| --- | --- | --- |
| 项目个人 | `/inspire/<tier>/project/<topic>/<path-user>/...` | 每 Project、每账号一份，适合代码、脚本、调试输出 |
| 项目公共 | `/inspire/<tier>/project/<topic>/public/...` | Project 成员共享，适合数据、权重、批量结果、Checkpoint |
| 全局个人 | `/inspire/<tier>/global_user/<path-user>/...` | 跨 Project 个人盘，适合小工具和中转 |
| 全局公共 | `/inspire/hdd/global_public/...` | 全平台共享，普通账号通常只读 |

`<path-user>` 是共享盘返回的个人目录名，不一定等于平台登录 username。无法确认时进入目标 Project 的 Notebook 直接查看已挂载目录，不从登录名猜。

`/inspire/dataset/<数据集名>/<版本名>` 不属于共享盘，是创建时用 `--dataset` 挂载的官方数据集，只读且不占 Project 共享盘配额。

## 存储池

| 池 | Project 路径前缀 | 定位 |
| --- | --- | --- |
| SSD `gpfs_flash` | `/inspire/ssd/project/<topic>/` | 训练 Hot Path、活跃工作集、Checkpoint 热点 |
| HDD `gpfs_hdd` | `/inspire/hdd/project/<topic>/` | 通用空间，写前看剩余容量 |
| qb-ilm `qb_prod_ipfs01` | `/inspire/qb-ilm/project/<topic>/` | 大容量，顺序读带宽接近 SSD |
| qb-ilm2 `qb_prod_ipfs02` | `/inspire/qb-ilm2/project/<topic>/` | 新且空余多，新增大数据优先考虑 |

`global_public` 只在 HDD。需要 SSD 或 QB-ILM 速度时，优先使用 Project 个人或公共路径。

## 挂载隔离

实例只挂自身所在 Project 的 Fileset。其它 Project 的 `/inspire/{hdd,ssd,qb-ilm,qb-ilm2}/project/<others>/` 在该实例里通常不存在，`ls` 报 `No such file` 不是权限问题。

跨 Project 搬小文件时，在两个 Project 各起一个 Notebook，用全局个人路径中转。大数据集或全量 Checkpoint 超出个人 Quota 时，联系 Project 管理员处理。
