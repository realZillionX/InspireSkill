# 联网准备与 SII 内部源

判断公网内容去哪准备、SII 内部源怎么配、离线 GPU 空间的依赖怎么固化时先看本页。Notebook / Job / HPC / Ray / Serving 的生命周期看对应 Workload Reference；命令语法回到 CLI Help。

## 1. 先分公网和内部源

联网能力属于 Workspace / Compute Group 的实际环境，不属于命令本身。

| 访问对象 | 判断 |
| --- | --- |
| GitHub、Hugging Face、外部数据源、公开下载地址 | 需要公网，通常放到 `CPU资源空间` 的可上网 CPU Notebook 准备 |
| PyPI / pip、Apt、Conda、PyTorch wheels、npm、Maven、Docker registry、OSS、NTP | 通常是 SII 内部源，即使 GPU Compute Group 没公网也可能可达 |

目标 `分布式训练空间` 不可上网时，不要在 GPU Notebook / Job 启动命令里反复 `git clone`、拉外部权重或访问外部数据源。先在可上网 CPU Notebook 准备内容，写入共享盘或保存成镜像。

只缺内部源覆盖的包、系统依赖、内部镜像或 OSS 时，可以在目标 Notebook 里直接配置内部源并验证；跑通后保存镜像，避免后续 Workload 每次启动重新安装。

真要确认某个出站目标是否可达时，用 `inspire notebook exec <name> "..."` 在容器里对具体端点做一次性验证。验证连通性时挑国内公网端点；GitHub、Hugging Face 等海外站点在部分环境本就不可达，测它们只会得到误导性的结论。

## 2. 准备结果放哪

| 准备结果 | 去向 |
| --- | --- |
| 代码 checkout、数据集、权重、Checkpoint、预处理产物 | 共享盘 `/inspire/...` 绝对路径 |
| Python / System 依赖、Slurm / Ray Runtime、服务启动环境 | Notebook 验证后 `notebook save-image` 成项目镜像 |
| 一次性调试脚本或小工具 | 项目个人路径或全局个人路径，视是否跨项目复用 |

环境能复用时优先固化镜像；数据和 Checkpoint 不进镜像。

## 3. 内部源入口

下表只记录 Agent 需要执行判断的内部入口；上游镜像背景集中维护在本节。

| 类型 | 地址 / 用法 |
| --- | --- |
| PIP / PyPI | `http://nexus.sii.shaipower.online/repository/pypi/simple/`；`pip download` 可用 `http://nexus.sii.shaipower.online/repository/pypi_proxy/simple/` |
| PyTorch wheels | `http://nexus.sii.shaipower.online/repository/pytorch/whl/cu126` |
| Conda | `http://nexus.sii.shaipower.online/repository/anaconda/pkgs/main`；`conda-forge`、`bioconda`、`menpo`、`pytorch` 等 channel 走 `http://nexus.sii.shaipower.online/repository/anaconda/cloud` |
| Ubuntu Apt | `http://nexus.sii.shaipower.online/repository/ubuntu/`；按镜像 codename 选择 `plucky`、`jammy`、`focal` 或 `xenial` |
| Debian 12 Apt | `http://nexus.sii.shaipower.online/repository/debian/`、`http://nexus.sii.shaipower.online/repository/debian-security` |
| ROS / OpenEuler / NVIDIA CUDA | `http://nexus.sii.shaipower.online/repository/ros/`、`http://nexus.sii.shaipower.online/repository/openeuler/`、`http://nexus.sii.shaipower.online/repository/nvidia-cuda/` |
| npm / Node.js | `http://nexus.sii.shaipower.online/repository/npm_proxy/`、`http://nexus.sii.shaipower.online/repository/nodejs/` |
| Maven | `http://nexus.sii.shaipower.online/repository/maven-proxy/` |
| Rust / Cargo | `http://nexus.sii.shaipower.online/repository/rustup/rustup`、`http://nexus.sii.shaipower.online/repository/rustup` |
| Ruby | `http://nexus.sii.shaipower.online/repository/ruby/` |
| Docker 镜像仓库 | `docker-qb.sii.edu.cn`、`docker-qbsandbox.sii.edu.cn`、`docker-t.sii.edu.cn` |
| OSS | `oss-nat.sii.edu.cn:8009` |
| NTP | `ntp0.sii.shaipower.online`、`ntp1.sii.shaipower.online` |

## 4. 快速配置片段

这些片段不是 CLI 手册；它们是平台内部源的环境配置边界。实际执行前仍要确认目标镜像的发行版、包管理器和权限。

PIP：

```bash
pip3 config set global.index-url http://nexus.sii.shaipower.online/repository/pypi/simple/
pip3 config set global.trusted-host nexus.sii.shaipower.online
```

npm：

```bash
npm config set registry http://nexus.sii.shaipower.online/repository/npm_proxy/
```

PyTorch CUDA wheel：

```bash
pip install torch torchvision torchaudio \
  --index-url http://nexus.sii.shaipower.online/repository/pytorch/whl/cu126 \
  --trusted-host nexus.sii.shaipower.online
```

Conda 需要完整 channel 映射，避免只替换一个 URL 后仍回源到外网：

```bash
cat > ~/.condarc <<'EOF'
offline: false
ssl_verify: false
show_channel_urls: yes
channels:
  - conda-forge
  - bioconda
  - menpo
  - pytorch
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/main
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/free
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/r
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/msys2
default_channels:
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/main
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/r
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/msys2
custom_channels:
  conda-forge: http://nexus.sii.shaipower.online/repository/anaconda/cloud
  msys2: http://nexus.sii.shaipower.online/repository/anaconda/cloud
  bioconda: http://nexus.sii.shaipower.online/repository/anaconda/cloud
  menpo: http://nexus.sii.shaipower.online/repository/anaconda/cloud
  pytorch: http://nexus.sii.shaipower.online/repository/anaconda/cloud
EOF
conda clean -i
```

Apt 不要机械粘贴源行。先确认镜像 codename，再把 `/etc/apt/sources.list` 指到对应内部源路径，随后更新并安装。

## 5. 固化原则

依赖安装跑通后，保存为镜像。`notebook save-image` 会触发一段中等时长的镜像保存过程；保存过程中不可操作该 Notebook；保存完毕后 Notebook 不会自动停止。

后续 Notebook / Job / HPC / Ray / Serving 应复用已验证镜像。只有数据、权重和产物继续走共享盘路径。项目基底镜像建立或更换后，按 [`assets.md`](assets.md) 回填 `INSPIRE.md` 资产身份及适用 Project / Workspace。
