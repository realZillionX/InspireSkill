# Job、HPC、Ray 与 Serving

在 GPU Job、CPU HPC、Ray 和 Serving 之间选型，或提交后观察 Events / Logs / Metrics / Instances / Status 时看本页。资源目录和 Profile 看 [`resources-and-paths.md`](resources-and-paths.md)，镜像看 [`image-management.md`](image-management.md)，模型仓库看 [`model.md`](model.md)。命令语法和参数以 CLI Help 为准。

## 1. 先选工作负载类型

| 目标 | 入口 | 适用边界 |
| --- | --- | --- |
| GPU 后台任务 / 分布式训练 / 批量推理 | Job | 固定 GPU 规模，任务开始后跑到结束 |
| CPU Slurm 批处理 | HPC | 固定 CPU 规模，预处理、评测、数据流水线 |
| 弹性 Worker / 长守护 / 流式处理 | Ray | 需要 Head、Driver 和可伸缩 Worker Group |
| 模型 HTTP 部署 | Serving | 从已注册模型创建在线服务 |

固定规模 GPU 不要用 Ray；固定规模 CPU 不要用 Notebook 长跑；普通训练 / 预处理不要用 Serving。能跑不等于选型正确。

## 2. 通用提交判断

提交前确认：

1. Workspace 与 Workload 类型一致：CPU / HPC / 公网准备用 `CPU资源空间`，GPU 训练 / Serving 用 `分布式训练空间`。
2. Quota Live 查询能找到目标 `gpu,cpu,mem`。
3. Image 已 `READY`，且环境在相同角色的 Notebook 或小规模任务里验证过。
4. 代码、数据、权重和输出路径在目标项目共享盘可见。
5. 复杂调度条件先 `dry-run` 或小规模 Probe。

离线 GPU 空间不要在启动命令里做公网下载。公网内容提前准备；内部源依赖可在目标 Notebook 验证后保存镜像。

## 3. GPU Job

Job 覆盖 GPU 多节点工作负载，包括分布式训练、批量推理和并发单节点 Worker Pool。它是 GPU 路径；HPC 是 CPU Slurm 路径。

Job 的关键边界：

- 日志和工作目录依赖共享盘约定；训练 Repo 建议在 `me:<repo>`，启动命令里使用相对共享盘路径或让脚本自己切目录。
- Shared Memory 是每个 Job Instance 的 `/dev/shm` / IPC 资源，不等同于 `--quota gpu,cpu,mem` 里的 `mem`，但不能超过该 `mem`。PyTorch DataLoader Workers、多进程数据管线或大模型训练需要更大 `/dev/shm` 时，用 `--shm-size <GiB>` 显式设置；也可用 `INSPIRE_SHM_SIZE` 或 `[job] shm_size` 作为默认值，命令行参数优先。提交前用 `job create --dry-run` 看解析后的 Shared Memory；提交后用 `job list/status` 确认平台返回的 Per-Instance SHM。
- 需要平台在任务状态变化时通知当前用户，可在 `job create` 使用 `--enable-notification`；平台收件人来自当前用户绑定的飞书账号，CLI 不接受任意收件人 ID。持久默认值用 `[job].enable_notification` 或 `INSPIRE_JOB_ENABLE_NOTIFICATION`，显式 `--enable-notification/--no-enable-notification` 优先。`job batch` 在 item 未设置时继承该默认值，item 内布尔值优先。
- 自动容错默认值用 `[job].auto_fault_tolerance` / `[job].fault_tolerance_max_retry` 或对应环境变量管理；CLI 显式参数和 Batch item 仍优先。通知与自动容错默认都保持关闭，除非明确启用；提交前用 `job create --dry-run` 或 `job batch --dry-run` 检查最终 Payload。
- 多节点训练要关注每个 Pod 的 GPU、显存、CPU 和网络曲线是否同步；某个 Worker 长期低负载通常比日志更早暴露问题。
- 排除坏节点是“不要调度到这些 Ready 节点”，不是固定节点；候选节点来自所选 Compute Group。

优先级是 Workspace 能力限定的调度信号。qz 公平调度 Workspace 只接受 `1=LOW`（可抢占）或 `4=HIGH`（稳定且可抢占 LOW），默认 4；其他 Workspace 保留 `1–10`，默认 10。CLI 从 live `is_fair_workspace` 选择合同，项目策略仍可能降低最终优先级。任务需要稳定训练但显示 LOW 时，先 stop，再按当前 Workspace 和项目策略重提。

qz 当前公平调度训练区的碎卡任务是明确例外：它只能以 `1=LOW`（可抢占）提交；需要稳定优先级的碎卡任务时改选开发区中真实存在的 Live Quota Row。整节点 / 碎卡按每个 Job Instance 的 Quota 判断，不按 `quota.gpu × --nodes` 的总卡数判断；例如 2 个 4 GPU Instance 仍是碎卡请求。训练区碎卡提交后用 Status / Events 核实解析后的优先级和调度结果。

## 4. HPC

HPC 有两层资源模型，不能混：

| 层级 | 含义 |
| --- | --- |
| 节点级 | 每个节点的 GPU / CPU / 内存，以及申请多少个节点 |
| Slurm 级 | 程序如何在这些节点内拆 task、CPU 和内存 |

关键约束：

- 入口命令只写 Slurm 正文，程序必须显式 `srun` 启动。
- Group 使用完整 Compute Group 名称；并非所有 CPU Compute Group 都支持 HPC。
- 镜像必须带可用 Slurm 运行环境。
- Slurm 级参数超出节点规格时可能静默排队。
- `status=SUCCEEDED` 不等于业务产出完整；正式 Entrypoint 要写 Fingerprint，再从同项目 Notebook 回读产物。

## 5. Ray

默认不要使用 Ray，除非任务明确需要弹性 Worker、长守护、流式处理或异构 Worker。Ray 集群由 Driver / Head / Worker Group 组成，Driver 不退出就会一直占资源。

Ray 特有风险：

- 镜像必须带 Ray Runtime。
- Head 和 Worker Quota 用 Ray 专属规格表。
- Worker 的 `min` / `max` 决定资源占用上限；长守护任务要接受手动 stop 的运维模型。
- 如果只是固定规模训练或固定 CPU 批处理，回到 Job / HPC。

## 6. Serving

Serving 面向模型部署服务。通常先用 Model Registry 找到模型和版本，再创建自定义部署。

创建前确认：

- 模型目录已经注册，目标版本状态可用。
- 镜像里有服务 Runtime 和启动命令所需依赖。
- 端口、健康检查和业务 Smoke Test 明确。
- 资源规格来自 Serving Quota，而不是训练 Job Quota。
- 公开访问前应用自身鉴权可用；平台通路不替代 API Key 或登录。

LLM 专属部署、Serverless LLM 和模型广场一键部署有不同平台类型；普通 Custom Serving 不要推导它们的字段。

## 7. 观察闭环

| 工具 | 主要回答 |
| --- | --- |
| `events` | 为什么排队、为什么启动失败、调度器或控制器拒绝了什么 |
| `logs` | 程序自身报错、训练进度、业务输出 |
| `metrics` | 已启动任务是否仍在有效工作，Pod / Task / Replica 是否均衡 |
| `instances` | 实际运行单元是否齐全，是否有部分 Pending 或异常 |
| `status` | 平台状态、优先级、基础摘要 |

卡住或失败先看 Events；已启动但健康度不明看 Metrics；程序行为看 Logs；产物完整性回到共享盘文件和 Fingerprint。

终态且不再需要的 Job、HPC、Ray 或 Serving 要清理。Running 资源先 `stop`，再 `delete`；不确定是否仍有人使用时跳过。

## 8. 异常判断

| 现象 | 优先怀疑 |
| --- | --- |
| `PENDING` 过久 | 优先级不足、实时配额不足、节点条件不满足 |
| `CREATING` 卡死 | 镜像拉取失败或节点初始化 |
| `instances` 部分 Pending | 多节点或多副本调度不均 |
| `logs` 为空但 `RUNNING` | 主进程未输出、日志路径不在 CLI 管理范围、程序没真正启动 |
| `FAILED` 但无业务报错 | OOM、显存溢出、节点驱逐或控制器失败 |
| HPC `steps=-/0` | Slurm 正文没有用 `srun` 启动程序 |
| `SUCCEEDED` 但产物为空 | 程序提前退出、资源贴边或输出路径不对 |
| Quota Match Failed | Workspace / Group / `gpu,cpu,mem` 三元组不匹配 |
