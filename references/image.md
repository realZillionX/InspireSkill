# Image Management

Use this guide when selecting existing images, registering external Docker images, changing image visibility, or cleaning up images. Saving a running Notebook into an image (`notebook save-image` / `cancel-save-image`) is covered in [`notebook.md`](notebook.md#6-base-environments-and-saving-images); internal package mirrors are covered in [`internal-sources.md`](internal-sources.md).

## 1. Image Responsibilities & Registry Scope

Images store reusable runtime environments for Notebooks, Jobs, HPC, Ray, and Serving. Datasets, model weights, checkpoints, and job outputs belong on shared storage (`/inspire/...`), not inside images.

Images live in a **Registry**, not a single Workspace, and multiple Workspaces commonly share the same Registry:
- `image list`, `detail`, `register`, `sync`, `set-visibility`, and `delete` all require `--workspace` to select the target Registry.
- An image saved with `notebook save-image --workspace X` is immediately visible in every Workspace that shares `X`'s Registry.
- Separate hardware domains or dedicated workspaces may point to disjoint Registries. Always check `inspire image list --workspace <target>` in the target Workspace rather than assuming an image exists across registry boundaries.

Before using an image for production workloads, verify that:
- Its status is `READY` or `SUCCESS`.
- It is visible to the target Workspace and Project.
- Required runtimes (CUDA, Slurm, Ray, or serving frameworks) have been tested.
- Caches, temporary build artifacts, credentials, and private data were cleaned prior to saving (see [`notebook.md`](notebook.md#6-base-environments-and-saving-images)).

## 2. Common Operations

| Goal | Command | Notes |
| --- | --- | --- |
| Find an official or custom image | `image list` / `image detail` | Use `--keyword` (case-insensitive substring match) to search large catalogs |
| Snapshot a running Notebook | `notebook save-image` | Commits the container in place; see [`notebook.md`](notebook.md#6-base-environments-and-saving-images) |
| Register an external Docker image | `image register` | Reserves a registry slot and returns `docker push` instructions |
| Import a completed Docker push | `image sync` | Synchronizes the registry manifest; use `--wait` to confirm readiness |
| Change sharing scope | `image set-visibility` | Switch between `private` and `project`, or promote to `public` |
| Delete a custom image | `image delete` | Ensure no active workloads or collaborators depend on it |

Always wait for a newly saved or synchronized image to reach `READY` or `SUCCESS` before scheduling workloads with it.

## 3. Visibility Levels

Custom images have three visibility levels matching the web UI tabs (`official` is reserved for platform-provided images):
- `private` — visible only to the creator. Keep experimental builds or images with internal debugging state private.
- `project` — shared with members of the project.
- `public` — visible platform-wide.

**Promoting an image to `public` is irreversible.** Once public, the platform removes creator ownership: you can no longer revert it to `private` or delete it (only platform administrators can remove public images). Keep images in `private` or `project` until you are certain they should be permanently public.

`image list --source <level>` filters by these tabs (`official`, `public`, `project`, `private`). `--source all` queries all four catalogs concurrently and merges them in order; if one catalog fails, the remaining results are still returned with a warning.

When a saved image becomes a shared project base image, record its name, target Project/Workspace, and included dependencies in `INSPIRE.md` (see [`assets.md`](assets.md)).

## 4. External Registration (`image register`)

Use `image register` for images built locally, in CI, or in an external registry (for running Notebooks, use `notebook save-image` instead).

`image register` allocates a `<name>:<version>` slot in the platform Registry and prints the `docker login`, `docker tag`, and `docker push` commands for the returned registry host.
- A direct-push slot starts in `PENDING_SYNC`. Push alone does not complete registration: run `image sync` after Docker has finished uploading.
- Use `image sync --wait` to synchronize and wait for readiness. `image register --wait` is only useful when another process performs both push and sync; registering the same slot again is not a substitute for syncing it.
- The CLI does not support `.tar` archive uploads; use the web console if you need to upload a tarball.

```bash
inspire image register --name example-env --version v1 --workspace "Workspace"
# Follow the returned docker login / tag / push instructions.
inspire image sync example-env:v1 --workspace "Workspace" --wait
```

All catalog steps are available through the CLI and SDK; no console claim is required. If synchronization fails, check registry authentication, tag matching, and whether the target Workspace maps to that Registry. A `FAILED` slot is a failure to investigate, not the expected direct-push starting state.

## 5. Cleanup

Before running `image delete`, verify that:
- No running or pending Notebook, Job, HPC, Ray, or Serving workload uses the image.
- No batch files or `INSPIRE.md` contracts reference it.
- Collaborators no longer need it to reproduce prior runs.

Canceling an in-progress snapshot with `notebook cancel-save-image` leaves a `FAILED` record in the image catalog; clean it up explicitly with `image delete`.
