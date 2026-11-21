# Official Datasets

Use this guide when searching the Dataset Hub, checking dataset permissions and versions, or mounting official datasets with `--dataset`. For shared storage paths, see [`paths.md`](paths.md); for workload creation rules, see [`resources.md`](resources.md).

## 1. Two-Stage Workflow

The Dataset Hub (`aip.sii.edu.cn`) is a separate service that shares CAS single sign-on (SSO) with the compute platform. The Dataset Hub manages the dataset catalog, versions, and access approvals, while the compute platform mounts validated dataset versions into containers at workload creation time.

Always follow this order:
1. Query `inspire dataset` to confirm that the dataset and version exist and your account has access.
2. Validate mount readiness with `inspire dataset validate <name>:<version> --workspace <workspace>` or pass `--dataset <name>:<version>` directly at workload creation.

### Authentication Split (CAS vs. OAuth)

- **Catalog queries (`dataset list | show | tags | applications`):** Require a stored CAS session for the selected account. Run `inspire login --cas feishu` (scan the QR code) or `inspire login --cas sms --phone <number>` (in non-interactive mode, SMS sends a code and exits; finish within 10 minutes on the same account with `--cas sms --phone <number> --sms-code <code>`). Missing or expired CAS sessions fail with a `login --cas` hint; OAuth token refresh cannot renew CAS cookies.
- **Mount validation & workload creation (`dataset validate`, `--dataset`):** Use the regular compute-platform OAuth Bearer session and work even without a CAS session once you know the exact `<dataset-name>:<version-name>`.
- **Access requests:** Submitting or approving dataset access notifies human reviewers and must be performed in the Dataset Hub web UI. Track request status read-only with `inspire dataset applications` (your requests: `pending`, `approved`, `rejected`, `withdrawn`) or `inspire dataset applications --to-approve` (requests awaiting your review). Once approved, run `inspire dataset validate` to confirm the mount is active.

## 2. Address by Name, Never Numeric ID

The Dataset Hub uses internal numeric `datasetId` and `versionId` keys, which the compute-platform mount API rejects (`数据集不存在`). Always use the slug and version string shown by `inspire dataset list` and `inspire dataset show`:

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
| `Grade` | Security classification (`S2`, `S3`, `S4`) | Informational compliance tier; does not grant or deny mount access on its own |
| `Tags` | Modality and domain tags | Grouped across text, image, audio, video, and multimodal; list valid tag names with `dataset tags` |

Individual versions carry their own readiness state: `downloading` and `pending_upload` mean files are not yet on disk even when the parent dataset is `active`.

`--keyword` matches both dataset names and descriptions (case-insensitive). `--tag` is repeatable (combined with OR) and requires exact tag names from `inspire dataset tags`.

## 4. Mounting Semantics

- **Supported workloads:** `notebook create`, `job create`, and `hpc create` accept repeatable `--dataset <name>:<version>` flags (and `datasets` / `dataset` entries in batch files and SDK specs).
- **Unsupported workloads:** `ray` and `serving` reject dataset mounts at the platform level; stage required files onto shared storage instead.
- **Container mount path:** Mounted read-only at `/inspire/dataset/<dataset-name>/<version-name>` without consuming project shared-storage quota.
- **Pre-flight validation:** Workload creation validates every `--dataset` spec up front and aborts if any mount fails. Run `inspire dataset validate <spec>... --workspace <workspace>` to check beforehand.
- **Batch deduplication:** Within a single `job`, `hpc`, or `notebook` batch run, successful `(workspace, dataset, version)` validations are cached in memory across items; failed validations are not cached.
- **Inspecting mounts:** `notebook status`, `job status`, and `hpc status` list mounted datasets and their container paths.

## 5. Minimal Example

```bash
inspire dataset list --keyword pixabay
inspire dataset show pixabay-81k
inspire dataset validate pixabay-81k:v0 --workspace CPU资源空间
inspire notebook create -n prep --workspace CPU资源空间 --project <project> \
  --group CPU资源-2 -q 0,4,32 --image <image> --dataset pixabay-81k:v0
```

Each version row in `inspire dataset show` prints the exact `--dataset` value and container path ready to copy.
