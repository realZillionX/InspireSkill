# Job、HPC、Ray 与 Serving

提交 GPU job、CPU HPC、Ray 和 serving，或观察事件、日志和指标时，先查本手册。资源目录、workload profile 和 path alias 看 [resources-and-paths.md](resources-and-paths.md)，镜像来源和可见性看 [image-management.md](image-management.md)。

## 1. 先选工作负载类型

| 目标 | 入口 | 适用边界 |
| --- | --- | --- |
| GPU 后台任务 / 分布式训练 / 批量推理 | `inspire job` | 固定 GPU 规模，任务开始后跑到结束 |
| CPU Slurm 批处理 | `inspire hpc` | 固定 CPU 规模，预处理、评测、数据流水线 |
| 弹性 worker / 长守护 / 流式处理 | `inspire ray` | 需要 Ray driver、head、弹性 worker group |
| 模型 HTTP 部署 | `inspire serving` | 从已注册模型创建在线服务 |

日常 workspace 选择直接按用途走：CPU / HPC / 联网准备用 `CPU资源空间`，GPU 训练 / GPU notebook / serving 用 `分布式训练空间`。目标 `分布式训练空间` 不可上网时，先在 `CPU资源空间` 的可上网 CPU notebook 准备共享盘内容或镜像，再提交目标 workload。不要把公网下载和拉 Git 放进离线训练 job 的启动命令里；临时安装包时先判断 SII 内部源是否可用。

## 2. 通用提交流程

1. GPU job / serving 用 `inspire resources specs --usage <job|serving> --workspace 分布式训练空间` 选择合法 `--quota gpu,cpu,mem`；HPC / CPU Ray 用 `--workspace CPU资源空间`。
2. `inspire image list` / `image detail` 确认镜像 `READY`。
3. 用 create 命令提交；复杂条件先 `--dry-run`。
4. 卡住或失败先看 events；已启动但健康度不明看 metrics；程序行为看 logs 或产出文件。

## 3. GPU Job

`inspire job` 覆盖 GPU 多节点工作负载，包括分布式训练、批量推理和并发单节点 worker pool。它是 GPU 路径；`hpc` 是 CPU Slurm 路径。

命令列表、参数和单命令功能以 CLI help 为准：

```bash
inspire job --help
inspire job create --help
inspire job logs --help
inspire job metrics --help
```

配置了 `me` path alias 时，`job create` 会把 stdout / stderr 捕获到 `me/.inspire/`，`job logs` 可以通过任意一个能看到同一共享盘的 notebook 读取日志。训练 repo 建议放在 `me:<repo>`；job 命令里写相对 `me` 的路径：

```bash
inspire job create -n <name>-train -q 8,160,1800 --nodes 2 \
  -c 'bash <repo>/train.sh' --workspace 分布式训练空间 --group <GROUP> \
  --project <PROJECT> --image <IMAGE> --priority 5

inspire job logs --follow <name>-train
```

`job create` 本身不解析 `me:<repo>`；在启动命令里使用相对 `me` 的路径，或让脚本自己切到正确目录。需要先操作共享盘时，用 notebook 的 `exec --cwd me:<repo>`。

## 4. 优先级

`--priority` 是 1 到 10 的数字，平台映射为三档：

| 数值 | 平台语义 |
| --- | --- |
| 1 到 3 | 低优先级，会被高优任务抢占 |
| 4 | 普通优先级 |
| 5 到 10 | 高优先级，适合稳定训练 |

平台可能按所选项目策略裁剪可请求优先级。提交后用默认文本输出核对：

```bash
inspire job status <name>
```

如果显示为 LOW 且任务需要稳定运行，先 stop，再用更高优先级重提。

## 5. HPC 两层资源模型

`hpc create` 有两层资源，不能混：

| 层级 | 参数 | 含义 |
| --- | --- | --- |
| 节点级 | `--quota gpu,cpu,mem` 和 `--instance-count` | 选择每个节点的 CPU / 内存 / GPU 资源，以及申请多少个节点 |
| Slurm 级 | `--number-of-tasks`、`--cpus-per-task`、`--memory-per-cpu` | 告诉 Slurm 程序如何使用这些节点 |

不传 Slurm 级参数时，默认 `cpus-per-task = quota.cpu`、`memory-per-cpu = quota.mem // quota.cpu`、`number-of-tasks = 1`，即整节点一个 task。

HPC 关键约束：

1. `-c` 只写 Slurm 正文；程序必须显式 `srun` 启动。
2. `--group "<name>"` 按 compute group 名称传。
3. Slurm 级参数超出节点规格时可能静默排队。
4. `--image` 必须是完整 Docker 地址或当前 workspace 可见镜像，并带可用 Slurm 运行环境。
5. 应用层并发不要把 CPU / 内存压满，给平台组件和系统进程留余量。
6. 并非所有 CPU compute group 都支持 `hpc create`；提交前用 `inspire resources specs --usage hpc` 确认目标组可用。

示例：

```bash
inspire hpc create -n <name>-preprocess \
  -c 'srun bash -lc "python preprocess.py"' \
  --workspace CPU资源空间 --project <PROJECT> --group <GROUP> \
  -q 0,20,256 --cpus-per-task 16 --memory-per-cpu 12 \
  --number-of-tasks 1 --instance-count 1 \
  --image <IMAGE>
```

`status=SUCCEEDED` 不等于业务产出完整。每个新 entrypoint 写唯一 fingerprint 到共享盘，再用同项目 notebook 回读确认产出完整。

## 6. Ray 适用边界

默认不要使用 Ray，除非任务明确需要弹性 worker、长守护、流式处理或异构 worker。固定规模 GPU 走 `job`，固定规模 CPU 走 `hpc`。

Ray 当前只在部分 compute group 可用，日常先从 `CPU资源空间` 查：

```bash
inspire resources specs --usage ray --workspace CPU资源空间
```

示例：

```bash
inspire ray create -n <name>-pipeline \
  -c 'python driver.py --mode run_and_exit' \
  --workspace CPU资源空间 --project <PROJECT> \
  --head-image <IMAGE> --head-group <GROUP> --head-quota 0,4,16 \
  --worker 'name=w1;image=<IMAGE>;group=<GROUP>;quota=0,4,16;min=1;max=8;shm=32'
```

Ray 特有坑：

- 镜像必须带 Ray runtime。
- `--head-quota` 和 worker `quota=` 用 Ray 专属规格表。
- `min` 和 `max` 都必须大于等于 1。
- driver 不退出，集群就一直占配额；长守护任务要接受手动 stop 的运维模型。

## 7. Serving

`inspire serving` 面向模型部署服务，普通训练 / 预处理任务不要走它。通常先用 `model` 找到模型和版本，再用 `serving create` 创建服务。

创建前查询：

```bash
inspire model list --workspace 分布式训练空间
inspire model versions <model-name> --workspace 分布式训练空间
inspire serving configs --workspace 分布式训练空间
inspire resources specs --usage serving --workspace 分布式训练空间
```

创建示例：

```bash
inspire serving create --name <name> --model <model-name> --model-version 1 \
  --workspace 分布式训练空间 --project <PROJECT> --group <GROUP> \
  --quota 1,18,200 --image <IMAGE> \
  --command "python serve.py" --port 8000 --priority 5 --dry-run
```

确认计划后去掉 `--dry-run` 提交。服务启动后用 `serving list`、`serving status`、`serving metrics`、`serving stop` 和 `serving delete` 做观察和止损。

## 8. 事件、日志、指标和实例

任务卡住或失败时优先查事件，确认调度器、控制器或节点给出的原因：

```bash
inspire job events <name> --tail 50
inspire hpc events <name> --tail 50
inspire ray events <name> --tail 50
```

需要看实际 pod / component 列表时查 instances，并显式传 workspace：

```bash
inspire job instances <name> --workspace 分布式训练空间
inspire hpc instances <name> --workspace CPU资源空间
inspire ray instances <name> --workspace CPU资源空间
```

任务已启动但健康度不明时查指标。`metrics` 对应平台资源视图，适合看 GPU、显存、CPU、内存、磁盘和网络是否持续工作，以及多 pod / 多 task 是否负载均衡：

```bash
inspire job metrics <name> --window 30m
inspire job metrics <name> --metric gpu,gpu_mem,cpu,mem --sparkline --no-plot
inspire hpc metrics <name> --metric cpu,mem,disk_read,disk_write --window 2h
inspire serving metrics <name> --window 30m
```

| 工具 | 主要回答 |
| --- | --- |
| `events` | 为什么排队、为什么启动失败、调度器拒绝了什么 |
| `logs` | 程序自身报错、训练进度、业务输出 |
| `metrics` | 已启动任务是否仍在有效工作、各 pod / task / replica 是否均衡 |
| `instances` | 实际运行单元是否齐全、是否有部分 Pending |
| `status` | 平台状态、优先级、基础摘要 |

多节点训练重点看每个 pod 的 GPU 和网络曲线是否同步；某个 worker 长期低 GPU、低网络，通常比单条日志更早暴露数据加载、通信或进程卡死问题。CPU HPC 重点看 CPU、内存和磁盘读写；Slurm 显示 `RUNNING` 但指标长期为零时，应回到日志和产出文件确认程序是否真的启动。

终态且不再需要的 job、HPC、Ray 或 serving 要清理。Running 资源先 stop，再 delete；不确定是否仍有人使用时跳过。

## 9. 异常状态对照

GPU job：

| 现象 | 优先怀疑 |
| --- | --- |
| `PENDING` 过久 | 优先级不足或配额实时不足，用 `job events` 确认 |
| `CREATING` 卡死 | 镜像拉取失败或节点初始化 |
| `instances` 中部分 Pod `Pending` | 分布式节点调度不均 |
| `events` 出现 `ImagePullBackOff` | `--image` 拼写错误或 registry 不可达 |
| `logs` 为空但 `status=RUNNING` | 主进程未重定向 stdout，或日志路径不在 CLI 管理范围 |
| `status=FAILED` 但无业务报错 | OOM、GPU 显存溢出、节点驱逐 |
| `quota match failed` / 0 候选 | `--quota gpu,cpu,mem` 在当前 workspace 找不到对应规格。用 `resources specs` 重选；多组撞名时加 `--group <name>` |

HPC：

| 现象 | 优先怀疑 |
| --- | --- |
| Slurm controller 启动失败 | 镜像不带 Slurm 运行环境 |
| `steps=-/0` | 正文没用 `srun` 启动程序 |
| `nodes=[]` | 调度未分配；可能是配额 / 优先级问题 |
| `status=SUCCEEDED` 但目录 / `stdout.log` / 报告为空 | 程序没有真正跑到业务产出，或 CPU / 内存贴边 |
| `quota match failed` / 0 候选 | `--quota gpu,cpu,mem` 在当前 workspace 找不到对应规格。用 `resources specs --usage hpc` 重选；多组撞名时加 `--group <name>` |
| `image not found` | 镜像名称不可见或地址不完整 |
