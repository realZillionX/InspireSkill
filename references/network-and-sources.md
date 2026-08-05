# 网络与内部源

判断公网、SII 内部源、离线 GPU 空间、依赖安装和镜像固化时先看本页。Notebook / job / HPC / Ray / serving 的生命周期看对应 workload reference；命令语法回到 CLI help。

## 1. 先分公网和内部源

联网能力属于 workspace / compute group 的实际环境，不属于命令本身。

| 访问对象 | 判断 |
| --- | --- |
| GitHub、Hugging Face、外部数据源、公开下载地址 | 需要公网，通常放到 `CPU资源空间` 的可上网 CPU notebook 准备 |
| PyPI / pip、Apt、Conda、PyTorch wheels、npm、Maven、Docker registry、OSS、NTP | 通常是 SII 内部源，即使 GPU compute group 没公网也可能可达 |

目标 `分布式训练空间` 不可上网时，不要在 GPU notebook / job 启动命令里反复 `git clone`、拉外部权重或访问外部数据源。先在可上网 CPU notebook 准备内容，写入共享盘或保存成镜像。

只缺内部源覆盖的包、系统依赖、内部镜像或 OSS 时，可以在目标 notebook 里直接配置内部源并验证；跑通后保存镜像，避免后续 workload 每次启动重新安装。

`public_internet` 的 live probe 使用国内公网端点，例如 `www.baidu.com:443`、`www.qq.com:443`、`www.163.com:443` 和 `mirrors.tuna.tsinghua.edu.cn:443`。不要把 GitHub、Hugging Face、OpenAI、Anthropic 等海外模型或代码站点作为默认 probe 目标；probe 只判断普通公网出站能力，不判断具体外部服务是否合规可用。

## 2. 连通性与合规授权分开判断

`public_internet=true`、能够解析域名或能够建立 TCP 连接，都只表示技术上可达，不代表可以使用任意外部服务。反过来，`public_internet=false` 也不代表内部源不可用。每次使用外部模型或 AI 编程服务时，都要把网络能力和服务授权分成两步判断。

| 场景 | 允许的处理 | 不要做 |
| --- | --- | --- |
| Notebook 明确不可上网或 Live Probe 为 `false` | 命令走 `notebook exec` / `notebook shell`，文件走 `/inspire/...` 共享路径 | 自建反向隧道、代理、VPN 或中继，把受限服务器穿透到公网；使用 `notebook ssh` 系列命令 |
| Notebook 可上网且平台允许 SSH | 可以使用平台提供的 SSH / SCP 通道处理普通开发任务 | 把“能联网”推导成“所有模型 API 和远程 Agent 服务都可使用” |
| 外部模型 API | 核对实际 API 端点、服务地域、使用条款和项目政策 | 调用仅限海外使用或明确不向中国境内提供服务的模型 API |
| vibe coding（AI 辅助编程）程序或服务 | 核对其实际后端和关键能力是否面向中国境内提供，工具名本身不是一刀切依据 | 在启智远端启动后端或关键能力不向中国境内提供的服务 |
| SII 内部源 | 按目标镜像和包管理器配置并验证，跑通后保存镜像 | 把内部源可达当成普通公网已开放，或由此转接外部服务 |

执行判断按以下顺序闭环：

1. 先用 Workspace 已知策略或 Live Probe 判断普通公网能力。
2. 再识别实际服务提供方、接入端点、服务地域和使用条款；不要只看客户端命令名。
3. 确认服务在中国境内可用，并且符合项目或组织政策。
4. 根据 Notebook 网络类型选择平台支持的连接和文件通道；不可上网区不做任何网络穿透。
5. 信息不足时默认不在启智远端启动该服务。把 Agent 留在本地或已批准的联网环境，远端只承担计算，并通过 Git、共享盘或已允许的传输通道交换代码、数据和产物。

## 3. 准备结果放哪

| 准备结果 | 去向 |
| --- | --- |
| 代码 checkout、数据集、权重、checkpoint、预处理产物 | 共享盘 path alias，例如 `me` / `public` |
| Python / system 依赖、Slurm / Ray runtime、服务启动环境 | notebook 验证后 `image save` 成项目镜像 |
| 一次性调试脚本或小工具 | 项目个人路径或全局个人路径，视是否跨项目复用 |

环境能复用时优先固化镜像；数据和 checkpoint 不进镜像。

## 4. 内部源入口

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

## 5. 快速配置片段

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

## 6. 固化原则

依赖安装跑通后，保存为镜像。`image save` 会触发一段中等时长的镜像保存过程；保存过程中不可操作该 notebook；保存完毕后 notebook 不会自动停止。

后续 notebook / job / HPC / Ray / serving 应复用已验证镜像。只有数据、权重和产物继续走共享盘路径。
