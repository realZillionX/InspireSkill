# Image Management

Use this guide when selecting existing images, registering external Docker images, changing image visibility, or cleaning up images. Saving a running Notebook into an image (`notebook save-image` / `cancel-save-image`) is covered in [`notebook.md`](notebook.md#6-base-environments-and-saving-images); internal package mirrors are covered in [`internal-sources.md`](internal-sources.md).

## 1. Image Responsibilities & Registry Scope

Images store reusable runtime environments for Notebooks, Jobs, HPC, Ray, and Serving. Datasets, model weights, checkpoints, and job outputs belong on shared storage (`/inspire/...`), not inside images.

Images live in a **Registry**, not a single Workspace, and multiple Workspaces commonly share the same Registry:
- `image list`, `detail`, `register`, `sync`, `set-visibility`, and `delete` all require a single `--workspace` to select the target Registry (`--workspace all` is not supported).
- An image saved with `notebook save-image --workspace X` is immediately visible in every Workspace that shares `X`'s Registry.
- Separate hardware domains or dedicated workspaces may point to disjoint Registries. Always verify `inspire image list --workspace <target>` in the target Workspace rather than assuming an image exists across registry boundaries.

Before scheduling production workloads with an image, verify that:
- Its status is `READY` or `SUCCESS`.
- It is visible to the target Workspace and Project.
- Required runtimes (CUDA, Slurm, Ray, or serving frameworks) have been tested.
- Package caches, build artifacts, credentials, and private data were cleaned prior to saving (see [`notebook.md`](notebook.md#6-base-environments-and-saving-images)).

## 2. Common Operations

| Goal | Command | Notes |
| --- | --- | --- |
| Find an official or custom image | `image list` / `image detail` | Use `--keyword` (case-insensitive substring match) to search large catalogs |
| Snapshot a running Notebook | `notebook save-image` | Commits the container in place; see [`notebook.md`](notebook.md#6-base-environments-and-saving-images) |
| Register an external Docker image | `image register` | Reserves a `PENDING_SYNC` registry slot and prints `docker push` instructions |
| Synchronize a pushed Docker image | `image sync` | Imports the pushed manifest into the catalog; pass `--wait` to poll until ready |
| Change sharing scope | `image set-visibility` | Switch between `private` and `project`, or promote to `public` |
| Delete a custom image | `image delete` | Ensure no active workloads or collaborators depend on it |

Always wait for a newly saved or synchronized image to reach `READY` or `SUCCESS` before scheduling workloads with it.

## 3. Visibility Levels

Custom images have three visibility levels matching the web console tabs (`official` is reserved for platform-provided images):
- `private` — visible only to the creator. Keep experimental builds or images with internal debugging state private.
- `project` — shared with members of the project.
- `public` — visible platform-wide.

**Promoting an image to `public` is irreversible.** Once public, creator ownership is removed: you can no longer revert it to `private` or delete it (only platform administrators can remove public images). Keep images in `private` or `project` until you are certain they should be permanently public.

`image list --source <level>` filters by catalog tab (`official`, `public`, `project`, `private`). `--source all` queries all four tabs concurrently and merges them in order; if one tab fails, the remaining results are still returned with a warning.

When a saved image becomes a shared project base image, record its name, target Project/Workspace, and included dependencies in `INSPIRE.md` (see [`assets.md`](assets.md)).

## 4. External Registration (`image register` & `image sync`)

Use `image register` for images built locally, in CI, or in an external registry (for running Notebooks, use `notebook save-image` instead).

```bash
inspire image register --name example-env --version v1 --workspace <workspace>
# Run the printed docker login, docker tag, and docker push commands
inspire image sync example-env:v1 --workspace <workspace> --wait
```

- `image register` allocates a `<name>:<version>` slot in `PENDING_SYNC` state and prints the exact `docker login`, `docker tag`, and `docker push` commands for that Registry.
- Pushing layers to Docker alone does not update the platform catalog: run `image sync --wait` after `docker push` finishes so the slot transitions from `PENDING_SYNC` to `SUCCESS` (`image register --wait` only helps when an external process performs both push and sync).
- If synchronization fails (`FAILED`), verify registry authentication, exact `<name>:<version>` tag matching, and whether the target Workspace maps to that Registry.
- `.tar` archive uploads are not supported by the CLI; use the web console if you must upload a tarball.

## 5. Cleanup

Before running `image delete`, verify that:
- No running or pending Notebook, Job, HPC, Ray, or Serving workload uses the image.
- No batch files or `INSPIRE.md` contracts reference it.
- Collaborators no longer need it to reproduce prior runs.

Canceling an in-progress snapshot with `notebook cancel-save-image` leaves a `FAILED` record in the image catalog; remove it explicitly with `image delete`.
