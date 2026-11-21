# Network Preparation & Internal Mirrors

Use this guide to decide where to download external resources, how to configure internal package mirrors, and how to bake dependencies for offline GPU workspaces. For workload lifecycles, see [`notebook.md`](notebook.md) and [`compute-workloads.md`](compute-workloads.md).

## 1. Public Internet vs. Internal Mirrors

Outbound network access depends on the Workspace and Compute Group where a container runs:

| Target Resource | Where to Run |
| --- | --- |
| GitHub, Hugging Face, external datasets, public URLs | Requires public internet; run in an internet-enabled CPU Notebook in `CPU资源空间` |
| PyPI (`pip`), Apt, Conda, PyTorch wheels, `npm`, Maven, internal Docker registries, internal OSS, NTP | Served by internal mirrors; typically reachable directly from GPU workspaces such as `分布式训练空间` |

When `分布式训练空间` has no public internet access:
- Never put `git clone`, Hugging Face downloads, or external dataset fetches inside GPU Notebook or GPU Job startup commands. Stage them from a CPU Notebook in `CPU资源空间` onto shared storage (`/inspire/...`) or bake them into an image.
- If you only need packages covered by internal mirrors, configure the mirrors directly inside a Notebook, verify imports, and save a custom image with `notebook save-image` so downstream workloads do not reinstall packages on every start.

To test whether a specific endpoint is reachable from a Notebook, run a one-off probe with `inspire notebook exec <name> --workspace <workspace> "curl -I -m 10 <url>"`.

## 2. Where to Put Staged Artifacts

| Artifact Type | Destination |
| --- | --- |
| Code checkouts, datasets, model weights, checkpoints, preprocessed outputs | Shared storage absolute paths (`/inspire/...`) |
| Python/system packages, Slurm/Ray runtimes, serving dependencies | Validated in a Notebook and saved as a project image via `notebook save-image` |
| Small helper scripts or debugging tools | Project personal path or global personal path |

Bake reusable runtime environments into images, and keep datasets, weights, and checkpoints on shared storage.

## 3. Internal Mirror Endpoints

| Service | Internal Endpoint / Usage |
| --- | --- |
| PyPI (`pip`) | `http://nexus.sii.shaipower.online/repository/pypi/simple/` (`pip download` can also use `http://nexus.sii.shaipower.online/repository/pypi_proxy/simple/`) |
| PyTorch Wheels | `http://nexus.sii.shaipower.online/repository/pytorch/whl/cu126` |
| Conda | `http://nexus.sii.shaipower.online/repository/anaconda/pkgs/main`; community channels (`conda-forge`, `bioconda`, `menpo`, `pytorch`) under `http://nexus.sii.shaipower.online/repository/anaconda/cloud` |
| Ubuntu Apt | `http://nexus.sii.shaipower.online/repository/ubuntu/` (match container distro codename: `plucky`, `noble`, `jammy`, `focal`, or `xenial`) |
| Debian 12 Apt | `http://nexus.sii.shaipower.online/repository/debian/`, `http://nexus.sii.shaipower.online/repository/debian-security` |
| ROS / OpenEuler / NVIDIA CUDA | `http://nexus.sii.shaipower.online/repository/ros/`, `http://nexus.sii.shaipower.online/repository/openeuler/`, `http://nexus.sii.shaipower.online/repository/nvidia-cuda/` |
| npm / Node.js | `http://nexus.sii.shaipower.online/repository/npm_proxy/`, `http://nexus.sii.shaipower.online/repository/nodejs/` |
| Maven | `http://nexus.sii.shaipower.online/repository/maven-proxy/` |
| Rust / Cargo | `http://nexus.sii.shaipower.online/repository/rustup/rustup`, `http://nexus.sii.shaipower.online/repository/rustup` |
| Ruby | `http://nexus.sii.shaipower.online/repository/ruby/` |
| Docker Registries | `docker-qb.sii.edu.cn`, `docker-qbsandbox.sii.edu.cn`, `docker-t.sii.edu.cn` |
| Internal OSS | `oss-nat.sii.edu.cn:8009` |
| NTP | `ntp0.sii.shaipower.online`, `ntp1.sii.shaipower.online` |

## 4. Configuration Snippets

Check the container's OS release and package manager before applying these snippets.

**pip:**

```bash
pip3 config set global.index-url http://nexus.sii.shaipower.online/repository/pypi/simple/
pip3 config set global.trusted-host nexus.sii.shaipower.online
```

**npm:**

```bash
npm config set registry http://nexus.sii.shaipower.online/repository/npm_proxy/
```

**PyTorch CUDA wheels:**

```bash
pip install torch torchvision torchaudio \
  --index-url http://nexus.sii.shaipower.online/repository/pytorch/whl/cu126 \
  --trusted-host nexus.sii.shaipower.online
```

**Conda (`~/.condarc`):** Configure full channel mappings so Conda does not fall back to external URLs:

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

**Apt:** Verify the distro codename (`lsb_release -cs` or `/etc/os-release`) before pointing `/etc/apt/sources.list` to the matching Ubuntu or Debian mirror above.

## 5. Baking Reusable Images

- For HPC or Ray base images on SSH-connected Ubuntu (`jammy` / `noble`) Notebooks, run `inspire notebook install-deps <notebook> --workspace <workspace> [--slurm] [--ray]` (`--ray-version`, `--pip-index-url`) to idempotently install the Slurm userland (`slurm-wlm`, `slurm-client`, `munge`, `hwloc`, `libpmix2`) or Ray runtime.
- Once dependencies are installed and verified inside the Notebook, clean up caches and temporary files and commit the container via `notebook save-image` (see [`notebook.md`](notebook.md#6-base-environments-and-saving-images)). Record shared project base images in `INSPIRE.md` (see [`assets.md`](assets.md)).
