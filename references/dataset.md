# Official Datasets

Use this guide when searching the Dataset Hub, checking dataset permissions and versions, or mounting official datasets with `--dataset`. For shared storage paths, see [`paths.md`](paths.md); for workload creation rules, see [`resources.md`](resources.md).

## 1. Two-Stage Workflow

The Dataset Hub is a separate service that shares single sign-on (SSO) with the compute platform. The Dataset Hub manages the catalog, versions, and access approvals, while the compute platform mounts a validated dataset version into your container at creation time.

Always follow this order:
1. Query `inspire dataset` to verify that the dataset and version exist and your account has access.
2. Pass `--dataset <dataset-name>:<version-name>` when creating the workload.

**Enable catalogue queries with CAS login.** Run `inspire login --cas feishu` and scan the QR, or `inspire login --cas sms --phone NUMBER`. In noninteractive mode, SMS sends a code and exits; finish within ten minutes with `inspire login --cas sms --phone NUMBER --sms-code CODE`. Use the same `--account NAME` in both steps. The Dataset Hub (`aip.sii.edu.cn`) uses the separately stored CAS session for `dataset list / show / tags / applications`. Missing or expired CAS sessions raise `PlazaUnavailable` with a login hint; OAuth token refresh cannot repair CAS. `inspire dataset validate` and `--dataset` mounts continue to use the platform Bearer session.

**Access requests must be submitted in the Dataset Hub web UI.** Requesting or approving access triggers notifications to human reviewers, so the CLI only provides read-only status tracking via `inspire dataset applications` (your submitted requests: `pending`, `approved`, `rejected`, or `withdrawn`) and `inspire dataset applications --to-approve` (requests awaiting your approval). Once an application turns `approved`, run `inspire dataset validate <name>:<version> --workspace <workspace>` to confirm mount readiness.

## 2. Address by Name, Never Numeric ID

The Dataset Hub uses internal numeric `datasetId` and `versionId` keys, but the mount API rejects numeric IDs (`数据集不存在`). Always use the slug and version string shown by `inspire dataset list` and `inspire dataset show`:

| Field | Format | Example |
| --- | --- | --- |
| Dataset Name | Lowercase hyphenated slug | `pixabay-81k` |
| Version Name | Free-form string (not always `vN`) | `v0`, `v1-br`, `2026-07-30` |
| Mount Spec | `<dataset-name>:<version-name>` | `--dataset pixabay-81k:v0` |

Never guess version names (`v0`, `v1`, etc.); read them from `inspire dataset show <name>`.

## 3. Catalog Fields & Filtering

| Field | Meaning | Impact |
| --- | --- | --- |
| `Access` | Whether the current account can mount the dataset | Creation fails with `无访问权限` if false; check `dataset applications` for pending requests |
| `State` | Dataset state | Only `active` is mountable; `processing`, `wanted`, and `error` cannot be mounted |
| `Grade` | Security classification (`S2`, `S3`, `S4`) | Informational compliance tier; does not directly grant or deny mount access |
| `Tags` | Modality and domain tags | Grouped across text, image, audio, video, and multimodal; list valid tag names with `dataset tags` |

Individual versions also have their own state: `downloading` and `pending_upload` indicate that files are not yet on disk even if the parent dataset is `active`.

`--keyword` matches both dataset names and descriptions (case-insensitive). `--tag` can be repeated (combined with OR) and requires exact tag names from `inspire dataset tags`.

## 4. Mounting Semantics

- **Supported workloads:** `notebook create`, `job create`, and `hpc create` accept repeatable `--dataset <name>:<version>` flags (and `dataset` entries in batch files).
- **Unsupported workloads:** `ray` and `serving` reject dataset mounts at the platform level; stage data on shared storage instead.
- **Container mount path:** Mounted read-only at `/inspire/dataset/<dataset-name>/<version-name>` without consuming project shared-storage quota.
- **Pre-flight validation:** The platform validates every dataset mount before creating a workload and aborts the entire creation if any mount fails. Use `inspire dataset validate <spec>... --workspace <workspace>` to test mounts beforehand.
- **Batch deduplication:** Within a single `job`, `hpc`, or `notebook` batch run, successful `(workspace, dataset, version)` validations are cached in memory for subsequent items in that invocation; failed validations are not cached.
- **Inspecting mounts:** `notebook status`, `job status`, and `hpc status` display all mounted datasets and their container paths.

## 5. Minimal Example

```bash
inspire dataset list --keyword pixabay
inspire dataset show pixabay-81k
inspire dataset validate pixabay-81k:v0 --workspace CPU资源空间
inspire notebook create -n prep --workspace CPU资源空间 --project <project> \
  --group CPU资源-2 -q 0,4,32 --image <image> --dataset pixabay-81k:v0
```

Each version row in `inspire dataset show` prints the exact `--dataset` value and container path ready to copy.
