# Model Registry

Use this guide when browsing the Model Registry, registering model directories on shared storage, inspecting model versions, or sizing Serving deployments. For the Serving deployment lifecycle, see [`compute-workloads.md`](compute-workloads.md#6-serving-inspire-serving).

## 1. Model Registry vs. Serving

| Command Group | Responsibility | Does Not Handle |
| --- | --- | --- |
| `inspire model` | Browse, register, and delete model catalog entries; inspect versions and minimum deployment specs | Creating/stopping online services or uploading local files |
| `inspire serving` | Create, observe, scale, roll back, stop, and delete online HTTP deployments | Uploading model weights or managing model catalog versions |

Typical flow: stage model weights on project shared storage → register the directory with `inspire model register` → deploy a version with `inspire serving create`.

## 2. Registration Rules (`model register`)

`model register` registers an existing shared-storage directory already accessible to the platform; it does not upload local files or mutate existing models.
- The storage path must reside under the target Project's directory in the selected Workspace (personal `global_user` paths are rejected).
- Model names must start with a letter and contain only letters, digits, underscores, hyphens, or dots.
- Use `--type` for the platform's hierarchical model category and `--tag` for task type, framework, license, or custom labels.
- Appending new versions to an existing model entry, editing metadata, or publishing to the public Model Plaza must be done in the web console unless exposed in CLI `--help`.

## 3. Common Operations

| Goal | Command | What to Check |
| --- | --- | --- |
| List models in a Workspace | `model list` | Filter by Project (`--project`) or name keyword (`--keyword`) |
| Check if a model is ready to deploy | `model status` / `model versions` | Storage path, version state, vLLM compatibility, and active consumers |
| Find minimum hardware spec for a version | `model deploy-config` | Minimum node count, GPUs, CPUs, and memory required to load the weights |
| Register a shared-storage directory | `model register` | Target Workspace, Project, and shared-storage path |
| Delete a model entry | `model delete` | Active/stopped Serving consumers across all versions |

`model deploy-config` returns a **hard lower bound**, not a generous recommendation: choosing a `serving quota` smaller than `deploy-config` will typically OOM before the service even starts. It also queries live vLLM compatibility (the static compatibility field stored on version records is always `no` and is ignored by the CLI).

## 4. Deletion Rules (`model delete`)

`model delete` permanently removes the **entire model catalog entry and all of its versions** (individual versions cannot be deleted separately). It does not delete the underlying files on shared storage, so the directory can be re-registered later if needed, though version history and model identity are lost.

Before deleting, the CLI checks every version for dependent Serving deployments:
- Any deployment in `RUNNING`, `STOPPED`, or `SLEEPING` state counts as an active consumer (since stopped deployments can be restarted with `serving start`), and `model delete` will refuse with the offending deployment names.
- Deployments in `FAILED` state do not block deletion.
- If the dependency check itself fails, deletion is aborted rather than assuming zero consumers.
- Pass `--force` to bypass the consumer check when you intentionally want to delete the catalog entry anyway.
