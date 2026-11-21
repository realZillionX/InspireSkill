# Multi-Stage Project Workflows

Use this guide when planning an end-to-end project across environment preparation, CPU data processing, GPU training, and model deployment. Refer to the domain-specific references for detailed rules on [`assets.md`](assets.md), [`resources.md`](resources.md), [`internal-sources.md`](internal-sources.md), [`paths.md`](paths.md), [`dataset.md`](dataset.md), [`notebook.md`](notebook.md), [`image.md`](image.md), [`compute-workloads.md`](compute-workloads.md), and [`model.md`](model.md).

## 1. Three-Stage Pipeline

Select `project`, `workspace`, remote paths, and `image` explicitly at each stage based on live platform state (and `INSPIRE.md` if present):

| Stage | Goal | Typical Environment | Acceptance Criteria |
| --- | --- | --- | --- |
| **A. Staging & Prep** | Clone code, download external data/weights, install dependencies, and bake an image | Internet-enabled CPU Notebook in `CPU资源空间` | Repo checked out at target commit, imports verified, shared paths readable, custom image `READY` |
| **B. CPU Processing** | Data preprocessing, cleaning, evaluation, packaging, index building | `inspire hpc` (or `ray` for streaming/elastic pipelines) | Small-scale probe passes; full output verified on shared storage with a fingerprint/manifest |
| **C. GPU Training & Serving** | Single-node debugging, multi-node training, or online HTTP serving | GPU Notebook, `job`, or `serving` in `分布式训练空间` | Logs advance, metrics show active GPU/network load, checkpoints or serving smoke tests pass |

**Core principle:** Front-load all internet downloads and environment builds in Stage A so GPU workloads in Stage C only read shared storage, pull a pre-built `READY` image, and run.

## 2. Stage A: Staging & Environment Preparation

Deliverables from Stage A:
- **Shared storage layout:** Absolute `/inspire/...` paths for code, datasets, weights, checkpoints, and outputs.
- **Reusable base image:** Python/system dependencies and required runtimes (CUDA, Slurm/Ray via `notebook install-deps`, or serving frameworks) validated in a Notebook, cleaned of temporary files and caches, and committed via `notebook save-image`.
- **`INSPIRE.md` update:** Record canonical remote paths and the new base image when they will be reused across sessions.

## 3. Stage B: CPU Data Processing

- Prefer **HPC** (`inspire hpc`) for fixed-scale CPU batch jobs; use **Ray** (`inspire ray`) only when elastic worker groups or streaming actors are required.
- Run a small probe matching your production task layout before launching a full batch.
- For HPC jobs, verify `Steps` in `hpc status` (confirming `srun` actually executed) and inspect output file counts, sizes, and checksums from a Notebook in the same project.

## 4. Stage C: GPU Training

Before submitting a multi-node GPU Job, confirm that:
1. Code, datasets, and weights are in place on project shared storage (or mounted via `--dataset`).
2. All dependencies are baked into a `READY` image so no outbound internet access is needed at startup.
3. Target GPU quotas and Compute Groups have sufficient live capacity (`inspire resources availability`).
4. A single-node probe has verified CUDA, NCCL, data loading, and checkpoint writing.

During training, monitor four signals:
- **`job events`** — scheduling, image pulls, and container startup.
- **`job logs`** — application output and stack traces.
- **`job metrics` / `job instances`** — per-Pod GPU utilization, memory, and network balance (a single lagging worker often appears in metrics before logs fail).
- **`inspire tensorboard scalars`** — training loss and evaluation curves read directly from the summary directory without opening a browser.

## 5. Deployment & Delivery

For online serving:
1. For custom weights on project shared storage, register the directory (`inspire model register`) and check minimum hardware specs (`inspire model deploy-config`); for published Model Plaza weights, select `--model-source plaza --model <published-name>`.
2. Create the service (`inspire serving create`, aligning `--port` and optional `--metrics-port` with your server listeners) and monitor `serving events` and `serving instances`.
3. Verify both container load (`serving metrics`) and request traffic (`serving api-metrics`), run an authenticated smoke test with `INF_API_KEY`, and confirm unauthenticated requests are rejected.
4. Record the production model reference, image, launch command, and ports in `INSPIRE.md`.
