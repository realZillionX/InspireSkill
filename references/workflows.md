# 项目工作流

项目从环境准备、数据处理推进到训练时，先查本手册。这里给跨阶段编排和验收点；具体命令参数以 CLI `--help` 为准，单领域细节分别看 [resources-and-paths.md](resources-and-paths.md)、[notebook.md](notebook.md)、[image-management.md](image-management.md) 和 [compute-workloads.md](compute-workloads.md)。

## 1. 总体框架

把项目推进拆成三段：

| 阶段 | 目标 | 典型入口 | 验收 |
| --- | --- | --- | --- |
| A. 联网准备 | 下载代码、依赖、数据、权重，形成可复用镜像或共享盘布局 | `CPU资源空间` 的可上网 CPU notebook、`image save` | 远端 repo 可更新，依赖可导入，数据 / 权重路径可读，镜像 `READY` |
| B. CPU 处理 | 预处理、清洗、评测、打包、索引构建 | HPC，必要时 Ray | 小规模 probe 通过，正式规模产物完整，有 fingerprint |
| C. GPU 训练 / 部署 | 单节点调试、多节点训练、serving | GPU notebook、job、serving | 日志推进，metrics 有负载，产物 / 服务 smoke 通过 |

核心原则：联网和依赖准备尽量前置到 `CPU资源空间` 的可上网 CPU notebook；`分布式训练空间` 只负责读共享盘、拉已准备镜像、运行目标程序。国产卡分区、`CI-情境智能` 工作空间或其它小组专属空间只在任务明确要求特殊硬件 / 特殊权限时使用。

## 2. 阶段 A：可上网 CPU Notebook 准备

先确认可上网 CPU 规格：

```bash
inspire notebook quota --workspace CPU资源空间 --include-empty
inspire notebook quota --workspace CPU资源空间 --group CPU资源-2
```

创建准备盒：

```bash
inspire notebook create --workspace CPU资源空间 --group CPU资源-2 -q 0,20,256 \
  --name <name>-base --image <BASE_IMAGE> --project <PROJECT> --wait
inspire notebook connection refresh <name>-base --workspace CPU资源空间
```

准备共享盘内容：

```bash
inspire notebook exec <name>-base --cwd me:<repo> "git pull && pip install -r requirements.txt"
inspire notebook exec <name>-base --cwd public "mkdir -p models data outputs"
```

如果依赖要复用于 job、HPC、Ray 或 serving，保存镜像：

```bash
inspire notebook install-deps <name>-base --slurm --ray
inspire image save <name>-base --workspace CPU资源空间 -n <img> -v v1 --visibility public --wait
```

验收点：

- `inspire notebook exec <name>-base --cwd me:<repo> "python -c 'import <pkg>'"` 通过。
- 数据、权重、checkpoint 路径在 `me` 或 `public` 下可读。
- 需要复用的镜像在 `image detail <img>:v1` 中为 `READY`。

## 3. 阶段 B：CPU 数据处理

固定规模 CPU 批处理优先用 HPC；流式、长守护或异构 worker 才考虑 Ray。

| 形态 | HPC | Ray |
| --- | --- | --- |
| 任务边界 | 明确开始和结束 | 长时间流式或服务型 |
| 并发模型 | 固定 `ntasks × instance_count` | `min/max` 弹性伸缩 |
| 数据流 | GPFS 到处理再到 GPFS | worker 间走 Ray 对象存储 |
| 结束条件 | `srun` 退出自动结束 | driver 退出才结束 |

正式放量前先跑接近生产形状的 probe。小规模通过不代表正式规模稳定。

HPC 示例：

```bash
inspire hpc quota --workspace CPU资源空间
inspire hpc create -n <name>-preprocess \
  -c 'srun bash -lc "python <repo>/preprocess.py --out public:dataset-v1"' \
  --workspace CPU资源空间 --project <PROJECT> --group <FULL_GROUP_NAME> \
  -q 0,20,256 --image <IMAGE> --priority 5
```

验收点：

- `inspire hpc events <name>-preprocess --workspace CPU资源空间 --tail 50` 没有持续调度拒绝。
- `inspire hpc metrics <name>-preprocess --workspace CPU资源空间 --metric cpu,mem,disk_read,disk_write --window 2h` 显示资源在工作。
- 同项目 notebook 回读产物目录，能看到预期文件、大小和 fingerprint。

## 4. 阶段 C：分布式训练空间

`分布式训练空间` 多数节点不可上网。进入训练阶段前，应已经具备：

- 代码在共享盘 repo 中，或镜像内已包含固定代码。
- 数据和权重在目标项目共享路径可见。
- 依赖在镜像中，或目标环境无需联网安装。
- `inspire job quota --workspace 分布式训练空间` 能找到目标 `--quota`。

单节点调试：

```bash
inspire notebook create --workspace 分布式训练空间 --group <GPU_GROUP_FULL_NAME> -q 1,20,200 \
  --name <name>-probe --image <IMAGE> --project <PROJECT> --wait
inspire notebook connection refresh <name>-probe --workspace 分布式训练空间
inspire notebook exec <name>-probe --cwd me:<repo> "bash scripts/probe.sh"
```

多节点训练：

```bash
inspire job create -n <name>-train -q 8,160,1800 --nodes 2 \
  -c 'bash <repo>/train.sh' --workspace 分布式训练空间 --group <GPU_GROUP_FULL_NAME> \
  --project <PROJECT> --image <IMAGE> --priority 5
inspire job logs <name>-train --workspace 分布式训练空间 --follow
```

训练失败或长时间排队时，先查：

```bash
inspire job events <name>-train --workspace 分布式训练空间 --tail 50
inspire job logs <name>-train --workspace 分布式训练空间 --tail 100
inspire job status <name>-train --workspace 分布式训练空间
```

训练已进入 `RUNNING` 后，把 `metrics` 当成和日志同级的健康度观察面：

```bash
inspire job metrics <name>-train --workspace 分布式训练空间 --window 30m
inspire job metrics <name>-train --workspace 分布式训练空间 --metric gpu,gpu_mem,cpu,mem,net_read,net_write --sparkline --no-plot
```

多节点训练里，某个 pod 长期低 GPU / 低网络通常意味着数据加载、通信或进程状态异常；所有 pod GPU 接近零且 CPU / I/O 也安静时，不要只盯 `RUNNING`，回到日志和产出文件确认训练是否真的推进。

## 5. 部署或交付

模型服务需要先把模型目录注册到 model registry，再创建 serving：

```bash
inspire model register --name <model> --source-path <REMOTE_MODEL_DIR> \
  --workspace 分布式训练空间 --project <PROJECT>
inspire model versions <model> --workspace 分布式训练空间
inspire serving create --name <service> --model <model> --workspace 分布式训练空间 \
  --project <PROJECT> --group <FULL_GROUP_NAME> --quota 1,18,200 --image <IMAGE> \
  --command "python serve.py" --port 8000 --dry-run
```

确认计划后去掉 `--dry-run`，再用 `serving status <service> --workspace 分布式训练空间`、`serving metrics <service> --workspace 分布式训练空间` 和业务 smoke test 验收。
