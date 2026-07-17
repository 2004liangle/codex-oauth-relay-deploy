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

## Install

The server must already have the managed Codex relay and an authenticated
`lark-cli` identity. Run from this repository checkout:

```bash
sudo env \
  FEISHU_INPUT_FOLDER_TOKEN='<input-folder-token>' \
  FEISHU_OUTPUT_FOLDER_TOKEN='<output-folder-token>' \
  FEISHU_LARK_USER=ubuntu \
  bash install-artifact-relay.sh
```

The folders must be different. The capabilities endpoint reveals only the
input folder so clients can upload source files automatically. The output
folder remains server-side configuration.

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
