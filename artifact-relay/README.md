# Feishu Artifact Relay

This sidecar moves image and attachment bytes through Feishu Drive while the
existing relay only carries small JSON job manifests. It keeps the original
OpenAI-compatible endpoints unchanged.

## Public API

All public requests use the existing relay key:

```text
Authorization: Bearer <relay-key>
```

Endpoints:

- `GET /v1/artifact-capabilities`
- `POST /v1/artifact-jobs`
- `GET /v1/artifact-jobs/{request_id}`

Supported operations are `image.generate`, `image.edit`, and
`artifact.handoff`. Remote requests cannot provide a shell command or Codex
instruction to execute. `artifact.handoff` only downloads and validates the
declared inputs, then becomes `ready_for_processing` for a trusted local agent.

Job statuses are `queued`, `downloading`, `processing`, `uploading`,
`ready_for_processing`, `completed`, and `failed`. Stored jobs and Feishu files
have manual retention: this service has no scheduled deletion.

Every input and output manifest uses:

```json
{
  "file_token": "box-token",
  "name": "image.png",
  "mime_type": "image/png",
  "size_bytes": 12345,
  "sha256": "64-lowercase-hex-characters"
}
```

`image.edit` inputs may also contain `role: image` or `role: mask`.

For a true transparent result, set `background` to `transparent` and
`output_format` to `png`. The sidecar validates the returned Alpha channel. If
the upstream returned an opaque image, it runs the pinned local CPU background
remover before upload and validates the PNG again. Optional
`background_removal_model` values are `isnet-general-use` and `isnet-anime`.
An unusable result is marked `failed`; an opaque PNG is never reported as a
completed transparent job.

## Install

The server must already have the managed Codex relay and an authenticated
`lark-cli` identity. Transparent output requires Python 3.11 or newer; Debian
12 and Ubuntu 24.04 provide a suitable system Python by default. On Ubuntu
22.04, install Python 3.11 alongside the system Python and set
`ARTIFACT_RELAY_PYTHON=/usr/bin/python3.11`; do not replace the system
`python3` symlink. Run from
this repository checkout:

```bash
sudo env \
  FEISHU_INPUT_FOLDER_TOKEN='<input-folder-token>' \
  FEISHU_OUTPUT_FOLDER_TOKEN='<output-folder-token>' \
  FEISHU_LARK_USER=ubuntu \
  ARTIFACT_RELAY_WORKERS=2 \
  bash install-artifact-relay.sh
```

The folders must be different. The capabilities endpoint reveals only the
input folder so clients can upload source files automatically. The output
folder remains server-side configuration. `ARTIFACT_RELAY_WORKERS` defaults to
`2` and accepts values from `1` through `4`.

The installer creates an isolated Python environment and preloads both local
background-removal models. Background removal is serialized to one CPU process
at a time so two artifact workers do not load both models concurrently. It does
not call a paid background-removal API.

## Local Handoff

A trusted local agent can list attachments waiting for processing:

```bash
sudo codex-artifact-relay-local list-ready
```

After producing one or more result files, publish them to Feishu and complete
the job:

```bash
sudo codex-artifact-relay-local publish \
  --request-id '<request-id>' --file './result.pdf'
```

The CLI is local-only. No equivalent public publish endpoint is installed.
