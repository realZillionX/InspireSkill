# Notebook Workflows

Use this guide when creating interactive Notebooks, running remote commands (`exec` / `shell`), transferring files (`scp`), exposing HTTP services (`proxy-url`), or saving a running Notebook as a reusable image (`save-image`). For scheduling and quotas, see [`resources.md`](resources.md); for internal mirrors, see [`internal-sources.md`](internal-sources.md); for managing saved images, see [`image.md`](image.md).

## 1. Notebook Roles

| Role | Typical Setup |
| --- | --- |
| Internet Staging Box | CPU Notebook in `CPU资源空间` to clone repos, download external weights/datasets to shared storage, or prepare images |
| Internal Mirror Testbed | Notebook in the target Workspace to configure and verify `pip`, Apt, Conda, `npm`, or Docker internal mirrors |
| GPU Probe | Small GPU Notebook in `分布式训练空间` to validate CUDA, NCCL, data mounts, and training entrypoints before submitting full Jobs |
| File Transfer Gateway | SSH-capable Notebook used with `shell`, `exec`, `scp`, or `rsync` to manage files on `/inspire/...` |
| Interactive Service Host | Running Gradio, FastAPI, or OpenAI-compatible servers exposed via `notebook proxy-url` |

When `分布式训练空间` has no public internet access, perform all external downloads from a CPU Notebook in `CPU资源空间` first.

## 2. Creating a Notebook

Before running `inspire notebook create`:
1. Pick the Workspace matching the role (`CPU资源空间` for CPU staging, `分布式训练空间` for GPU probes).
2. Query `inspire notebook quota --workspace <workspace>` and copy both `--group` and `--quota` (`gpu,cpu,mem`) from the same live row.
3. Pass `workspace`, `project`, `group`, `quota`, and a `READY` `image` explicitly.
4. If mounting official datasets with `--dataset <name>:<version>`, verify access first (see [`dataset.md`](dataset.md)).
5. **Keep Notebook names unique within each Workspace.** The platform allows duplicate names, which forces `--pick` on every subsequent name-based command; `notebook create` checks existing names in the target Workspace (case-insensitive) and rejects duplicates before submitting.

Optional creation controls:
- **Read-only shared mounts:** Shared storage is mounted read-write by default. Use the read-only mount flags on `notebook create` to downgrade the project public directory or project members directory to read-only (`ro`). Making the project members directory read-only requires Project Maintainer permissions.
- **Auto-stop vs. platform policies:** `--auto-stop` requests idle auto-stop, whereas scheduled stop sets a fixed runtime countdown (visible under `Auto-stop In` in `notebook status`). Neither overrides Workspace idle-reclaim rules or daily shutdown limits — check `inspire resources policy --workspace <name>` before leaving a Notebook running. Use Job, HPC, Ray, or Serving for long-running workloads.
- **Node pinning:** Pass a node name from the selected Compute Group only when debugging a specific machine or reproducing hardware issues. `notebook status` shows the assigned `Node` along with its health, cordon, and maintenance state while `RUNNING`.

## 3. Transport & Remote Execution (`exec` / `shell` / `ssh`)

Transport is determined automatically by the machine's actual GPU model:
- **Restricted Notebooks (`H100` / `H200`):** Do not support SSH or `rtunnel`. Commands run over `JupyterTerminal`.
- **Standard Notebooks (all other GPUs and CPU nodes):** Use SSH over `rtunnel`.

The CLI probes `nvidia-smi` over `JupyterTerminal` once per `(account, base_url, compute_group)` and caches the detected GPU model in `~/.inspire/notebook-gpu-models.json` (clearable per account with `inspire cache clear --resource notebook-gpu`). The Notebook must be `RUNNING` before calling `ssh`, `exec`, `shell`, or `scp`.

| Command | Purpose | Restricted Notebook (`H100`/`H200`) Behavior |
| --- | --- | --- |
| `exec` | Run a one-off non-interactive command | Uses `JupyterTerminal` (ephemeral terminal per call); rejects `--stdin`, `--bash-stdin`, local pipes, and `< file` redirection |
| `shell` | Interactive TTY shell (`exit` to quit, `Ctrl+]` to force-disconnect) | Uses `JupyterTerminal` |
| `scp` | Upload/download files over SCP | Not supported; transfer via an SSH-capable Notebook through `/inspire/...` shared storage |
| `ssh` / `ssh-config` / `ssh-proxy` | OpenSSH session, `~/.ssh/config` stanza, and `ProxyCommand` | Not supported |
| `connection refresh` | Bootstrap or refresh the SSH / `rtunnel` bridge | Not needed (no SSH bridge) |
| `proxy-url` | Print external HTTP proxy URL for a container port | Rejected by default (requires `--allow-restricted`) |

### `JupyterTerminal` Execution Rules (Restricted Notebooks)

- **No local stdin forwarding:** `JupyterTerminal` feeds its internal control wrapper over stdin and binds the user command's stdin to `/dev/null` so commands like `cat` or `bash -s` cannot swallow the completion marker. Passing `--stdin`, `--bash-stdin`, or piping/redirecting local stdin into `inspire notebook exec` is rejected before any command is sent.
- **Remote redirection works normally:** Redirecting files that already exist on `/inspire/...` inside the remote command string works as expected:
  ```bash
  inspire notebook exec <name> "python task.py < /inspire/.../input.jsonl"
  ```
- **Stale handle recovery:** Before opening a `JupyterTerminal`, the CLI reconciles cached Notebook handles against the live Workspace listing so deleting and recreating a Notebook with the same name automatically picks up the new instance. Pass `--ignore-target-cache` to bypass remembered target caches during diagnostics, or run with root `--debug` to inspect URL, XSRF, proxy (`HTTP(S)_PROXY` / `NO_PROXY`), and WebSocket stages.

### Working Directory & Long-Running Commands

- When `--cwd` is omitted, `notebook exec` and `notebook shell` preserve the transport's default working directory (the project user directory on `JupyterTerminal`, or the SSH login shell's default directory on SSH). Pass an explicit `/inspire/...` path to `--cwd` when your command depends on a fixed directory.
- For commands that run longer than 20 minutes, launch them as background processes on the remote container that write a completion sentinel file, and poll from your local machine rather than holding a synchronous `exec` open.

### Account Isolation & SSH Connection Cache

- Every `notebook` command accepts `--account <alias>` to operate on another account without changing the global default account. Connection caches and remembered targets (`notebook connection list|status|refresh|forget|prune` and `notebook connection target list|forget`) are strictly isolated per account.
- **Newly created SSH-capable Notebooks** do not have a cached SSH bridge yet: run `inspire notebook connection refresh <name> --workspace <workspace>` once before calling `exec`, `shell`, or `scp` (or run `ssh` / `ssh-config`, which bootstrap the bridge automatically). Restricted `H100`/`H200` Notebooks skip this step and use `JupyterTerminal` directly.
- `ssh-config` includes the account alias in the default SSH `Host` name (overridable with `--host`) and pins `--account` inside the generated `ProxyCommand` so VS Code Remote SSH and native `ssh` / `rsync` always use the right account.

## 4. File Transfer & Shared Storage Relay

Prefer Git (`git push` locally, `git pull` remotely) for source code. Use `notebook scp` for small untracked files, configs, and artifacts.

### Relaying Files to Restricted (`H100`/`H200`) Notebooks

Because restricted Notebooks do not support SSH/SCP, relay files through shared storage (`/inspire/...`) using any SSH-capable Notebook (such as a CPU Notebook) in the same account and project:

```bash
# 1. Upload from local machine to shared storage via an SSH-capable CPU Notebook
inspire notebook scp cpu-box ./dataset.tar /inspire/hdd/project/topic/user/dataset.tar

# 2. Access the same shared path directly inside the restricted GPU Notebook
inspire notebook exec gpu-box "ls -lh /inspire/hdd/project/topic/user/dataset.tar"

# 3. Download results from shared storage via the SSH-capable CPU Notebook
inspire notebook scp cpu-box -d /inspire/hdd/project/topic/user/results.tar ./results.tar

# Or use external rsync after generating ssh-config for cpu-box:
rsync -av ./dataset/ cpu-box:/inspire/hdd/project/topic/user/dataset/
```

Container-local files outside `/inspire/...` on a restricted Notebook must be copied onto `/inspire/...` before they can be downloaded via an SSH Notebook (or use the Python SDK's `notebooks.upload` / `download` with `transport="jupyter"` for small files; see [`sdk.md`](sdk.md)).

## 5. HTTP Proxy (`notebook proxy-url`)

To access an HTTP server (Gradio, FastAPI, TensorBoard, OpenAI-compatible API) running inside a Notebook container:

```bash
inspire notebook proxy-url <name> --workspace <workspace> --port 7860
```

- Prints the external URL without opening a browser. Use `--path` to append a sub-path and `--check` to probe reachability (`reachable`, `no_service` if nothing is listening on the port, or `blocked` if rejected by the gateway).
- **Treat the proxy URL as a credential.** It embeds a short-lived gateway token that grants full access to the Notebook. Never commit it or share it publicly.
- **Configure application-level authentication** (API key or login) on your web server before sharing access with collaborators, as the proxy only provides network routing.

## 6. Base Environments and Saving Images

To turn a running Notebook into a reusable image for Jobs, HPC, Ray, Serving, or future Notebooks, use `inspire notebook save-image <notebook-name> --workspace <notebook-workspace> --name <image-name>`.

### Pre-Save Cleanup Checklist

**Always clean the container before saving an image:**
1. Remove temporary directories, package manager caches (`pip cache purge`, `conda clean --all`, `apt-get clean`), downloaded archives, build intermediates, logs, and debug artifacts.
2. Ensure no credentials, tokens, private keys, or datasets/checkpoints meant for shared storage are left inside the container filesystem.
3. Re-verify that key imports and entrypoints still work after cleanup.

### Snapshot Lifecycle & `--flatten`

- `notebook save-image` is a **Notebook lifecycle operation** addressed by the Notebook's name and Workspace:
  - Before committing, the CLI queries and prints the platform's estimated uncompressed writable-layer size (`--dry-run` prints the estimate and exits without saving). The Notebook must be `RUNNING`.
  - During the snapshot, the Notebook enters `COMMITTING` state and cannot be operated on.
  - Once the snapshot finishes, the Notebook remains running and usable; wait for the resulting image to reach `READY` before scheduling workloads with it.
- **Layered vs. flattened snapshots (`--flatten`):**
  - By default, each `save-image` appends a new incremental layer on top of the base image.
  - Passing `--flatten` squashes the image into a single layer and permanently removes files that were overwritten or deleted from older layers. Use `--flatten` whenever you deleted large files from an earlier layer, when finalizing a project base image, or after multiple iterative snapshots. (`--flatten` increases background image build time in `CREATING` state but does not lengthen the Notebook's `COMMITTING` lock window.)
- **Canceling a snapshot (`notebook cancel-save-image <notebook-name>`):** Releases the Notebook back to its pre-save state even after the initial commit phase, leaving behind a `FAILED` image record that can be removed with `image delete` (see [`image.md`](image.md)).

*(Note: Slurm commands like `sinfo` or `srun` will fail inside a regular Notebook because there is no Slurm controller; Slurm is active only inside running `hpc` workloads.)*

## 7. Observation & Large File Operations

| Command | What It Answers |
| --- | --- |
| `notebook status` | Platform state, assigned `Node` and node health, priority, `Auto-stop In` countdown, mounted datasets |
| `notebook events` | Scheduling delays, image pull progress, startup failures |
| `notebook lifecycle` | Historical start/stop run intervals for the instance |
| `notebook metrics` | Historical time-series load (or pass `--now` for an instant GPU/CPU/Memory utilization snapshot) |
| `notebook exec` / `shell` | Process table, container logs, and filesystem inspection (Notebooks do not have a platform `logs` subcommand) |

### Large Directory Operations on Shared Storage

Before running large `mv`, `cp`, or `rm` operations on `/inspire/...`, inspect the directory structure first:
- **Wide top-level fan-out:** Parallelize across top-level subdirectories with bounded concurrency.
- **One or two massive subtrees:** Drill down a level before parallelizing so workers do not serialize on a single directory.
- **Millions of tiny files:** Prefer low-fork commands such as `find <dir> -delete` or `rsync --delete-after`. Run operations taking over 20 minutes in the background with a sentinel file.
