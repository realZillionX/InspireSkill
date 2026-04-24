# 排障速查

## 1. `notebook ssh` 自动 bootstrap 失败 → 手工补 sshd + rtunnel

CLI 优先通过 Jupyter terminal REST API + WebSocket 下发脚本；WS 不可用时回退到 Playwright 终端自动化。rtunnel 二进制只有两个合法来源：镜像里预装（推荐；`unified-base:v1` 及其派生镜像自带），或容器有公网时 bootstrap 自动 curl 下载。两条都失败时会拿到结构化报错；要手工复现，先看远端的诊断文件：

- `/tmp/setup_ssh.log`
- `/tmp/rtunnel-server.log`
- `/tmp/rtunnel`

确认是 sshd / rtunnel 没起来后，回到容器的 Web 终端跑：

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq openssh-server

curl -fsSL "https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz" \
  -o /tmp/rtunnel.tgz && tar -xzf /tmp/rtunnel.tgz -C /tmp && chmod +x /tmp/rtunnel

mkdir -p /run/sshd && ssh-keygen -A >/dev/null 2>&1
/usr/sbin/sshd -p 22222 -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes \
  -o PasswordAuthentication=no -o PubkeyAuthentication=yes
nohup /tmp/rtunnel 22222 31337 >/tmp/rtunnel-server.log 2>&1 &
```

之后回本机重跑 `inspire notebook ssh <notebook-name> --save-as <alias>`，应能成功。**保存基础镜像时，从已装好 SSH 工具链的实例 `image save` 出来的镜像会保留 `sshd`**——这是阶段 A 的目的之一。

## 2. `rtunnel` 相关错误

| 现象 | 原因 / 处理 |
| --- | --- |
| `SSH bootstrap 失败：rtunnel 在容器内找不到，且容器无公网` | 镜像没有预装 rtunnel 且计算组无公网。换成 `unified-base:v1` 或它的派生镜像，或先在可上网区 `curl` 下载 rtunnel 后 `inspire image save` 成自己的镜像。 |
| `exec format error` | 镜像里预装了一份**非当前容器架构**的 rtunnel（比如 darwin 的二进制被误烘进去）。重新在目标容器架构下构建镜像，或删掉镜像里那份错误二进制改走 curl。 |

## 3. HPC 任务异常状态对照

| 现象 | 优先怀疑 |
| --- | --- |
| `slurmctld BackOff` | 镜像不带 Slurm 运行环境 |
| `steps=-/0` | 正文没用 `srun` 启动程序 |
| `nodes=[]` | 调度未分配；可能是配额 / 优先级问题 |
| `status=SUCCEEDED` 但目录 / `stdout.log` / 报告为空 | CPU 并发 / 内存贴边（应用层应留 `cpus-per-task - 4`、`384 MB` 内存） |
| `spec_id not found` | 把 notebook `quota_id` 当成了 `predef_quota_id`；用 `resources specs --usage hpc` 重查 |
| `image not found` | 镜像地址不完整；必须是 `host/namespace/name:tag` 全形式 |
| `429` | 已内置退避；持续失败再等几分钟 |

## 4. 远程命令 / 文件操作

| 现象 | 处理 |
| --- | --- |
| `notebook exec` 报 alias 找不到 | `notebook connections` 看本地 alias；`notebook test [<alias>]` 看连通性；必要时 `notebook refresh <alias>` 重建 |
| `notebook scp` 把仓库文件传慢 / 不一致 | 它**不是**源码同步工具。源码走 `git push` + `notebook exec` 远端 `git pull` |
| 跨计算组无法 `git push` | 切到同一共享路径下的可上网区实例做 git；离线计算组本身不联网 |

## 5. 调度优先级误判

`--priority 1` 在平台语义里是 `priority_level: LOW`（**不是** "高优"）。要高优就用高值（如 9），提交后**立刻**：

```bash
inspire --json job status <job-id>
```

核对 `priority_level`。若仍为 `LOW`，先 `job stop`，再用更高的值重提。
