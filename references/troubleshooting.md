# 排障速查

## 1. `notebook ssh` bootstrap 失败

CLI 在容器内跑 bootstrap shell 做两件事：
1. 从 `/inspire/hdd/global_public/inspire-skill-bootstrap/v1/rtunnel/linux-<arch>/rtunnel` `cp` 到 `/tmp/rtunnel`
2. 如果容器没 `/usr/sbin/sshd`，从 `/inspire/hdd/global_public/inspire-skill-bootstrap/v1/sshd-debs/*.deb` `dpkg -i`

全部是 GPFS 本地 cp / dpkg，**不走网络**。任何一步失败都先看远端这三个文件：

- `/tmp/rtunnel`（cp 之后应该存在且 `-x`）
- `/tmp/rtunnel-server.log`（rtunnel server 启动日志）
- `/var/log/dpkg.log` 的末尾（sshd deb 安装状态）

### 常见现象

| 现象 | 处理 |
| --- | --- |
| `SSH bootstrap 失败:在容器里没能从 global_public kit 拿到 rtunnel` | kit 路径不可达。容器里 `ls /inspire/hdd/global_public/inspire-skill-bootstrap/v1/rtunnel/linux-amd64/rtunnel` 应当存在且可执行。不存在 → 平台侧 global_public 挂载没覆盖到这台实例，找 SII 运维。 |
| `exec format error` / `/tmp/rtunnel --help` 崩 | kit 里落了一份**非当前容器架构**的 rtunnel，或者文件被截断。CLI 下次 bootstrap 会自动 wipe `/tmp/rtunnel` 重试；手动清也行。持续失败的话提 issue 附 `uname -m` + `file /inspire/hdd/global_public/inspire-skill-bootstrap/v1/rtunnel/linux-*/rtunnel` 输出。 |
| `dpkg: error processing archive ...` | 容器已有部分 openssh 组件且版本冲突。手动 `dpkg -i --force-overwrite /inspire/hdd/global_public/inspire-skill-bootstrap/v1/sshd-debs/*.deb` 一次。 |
| `Tunnel setup completed, but SSH preflight failed` + `404 page not found` on `proxy/31337/` | kit cp + dpkg 都已成功（容器里 `ps -ef \| grep -E 'sshd\|rtunnel'` 能看到进程），但 Jupyter 的 `proxy/<port>/` 路径整段 `404` 说明该镜像的 jupyter 没装 `jupyter-server-proxy` 扩展，外部根本接不到 31337。**bootstrap kit 解决不了这一档**，得换镜像（`pytorch-inspire-base` / `ubuntu-inspire-base` 系列已知带）。已知现象不会出现 `start_config.allow_ssh=false` 提示——那条 hint 自 v2 起被 `notebook create` 主动设 `allow_ssh=true` 消掉。 |

### 手工复现 bootstrap

到容器的 Web 终端里跑（等价于 CLI 自动做的那套）：

```bash
KIT=/inspire/hdd/global_public/inspire-skill-bootstrap/v1
ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')

# rtunnel
cp "$KIT/rtunnel/linux-$ARCH/rtunnel" /tmp/rtunnel && chmod +x /tmp/rtunnel

# sshd
[ -x /usr/sbin/sshd ] || dpkg -i "$KIT/sshd-debs"/*.deb

# 启动
mkdir -p /run/sshd && ssh-keygen -A >/dev/null 2>&1
/usr/sbin/sshd -p 22222 -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes \
  -o PasswordAuthentication=no -o PubkeyAuthentication=yes
nohup /tmp/rtunnel 22222 31337 >/tmp/rtunnel-server.log 2>&1 &
```

之后回本机重跑 `inspire notebook ssh <notebook-name> --save-as <alias>`。

> **镜像固化不是必须的**。bootstrap 装的 rtunnel / sshd 跟着容器走，notebook 停了就没。想保留下次启动不重做 bootstrap 的话 `inspire image save` 派生一份；没必要的话跳过，一次性使用完全 OK。

## 2. HPC 任务异常状态对照

| 现象 | 优先怀疑 |
| --- | --- |
| `slurmctld BackOff` | 镜像不带 Slurm 运行环境 |
| `steps=-/0` | 正文没用 `srun` 启动程序 |
| `nodes=[]` | 调度未分配；可能是配额 / 优先级问题 |
| `status=SUCCEEDED` 但目录 / `stdout.log` / 报告为空 | CPU 并发 / 内存贴边（应用层应留 `cpus-per-task - 4`、`384 MB` 内存） |
| `spec_id not found` | 把 notebook `quota_id` 当成了 `predef_quota_id`；用 `resources specs --usage hpc` 重查 |
| `image not found` | 镜像地址不完整；必须是 `host/namespace/name:tag` 全形式 |
| `429` | 已内置退避；持续失败再等几分钟 |

## 3. 大规模 `mv` / `cp` / `rm` 策略

启智共享盘上单目录常到 "百万文件 / 百 GB / 百 TB" 量级，串行 `rm -rf` 能跑几小时；`inspire notebook exec` 会被吊住。动手前先探形状：

```bash
ls -A <dir> | wc -l                  # 顶层 fan-out
du -sh --max-depth=1 <dir>           # 大小分布
```

按形状选策略：

| 形状 | 策略 |
| --- | --- |
| 顶层 fan-out 大 + 大小均匀（如按 batch 切的数据集） | `find <root> -mindepth 1 -maxdepth 1 -print0 \| xargs -0 -n 1 -P 16 rm -rf --` |
| 一两个巨型子树 | 下钻一两层再 fan-out：`find <root> -mindepth 2 -maxdepth 2 -print0 \| xargs -0 -n 1 -P 16 rm -rf --`，否则 `-P 16` 实际只有 1 路 |
| 百万级小文件（inductor / pip / HF cache） | 瓶颈在 metadata IO。GNU `find -delete`（单进程 syscall、不 fork）或 `rsync --delete-after empty/ target/` 比 fork 的 `rm -rf` 还快 |

**规则**：
- 超过 20 分钟的操作一律 `nohup ... &` + sentinel 文件，本地轮询远端 sentinel；**别**让 `inspire notebook exec` 吊着等 SSH。
- 并行度 `-P` 别无脑拉 64+。GPFS metadata server 是共享的，`-P 16` 已经够，拉太高会影响同组其他人。

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
