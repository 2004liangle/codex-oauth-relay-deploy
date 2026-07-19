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

Supported operations are `image.generate`, `image.edit`, `image.cutout`, and
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
`image.cutout` requires exactly one `role: image` input and accepts only an
optional output file name. The server, not the client, supplies the fixed Agent
instruction `你把人物抠出来，做成透明的png`. Content generation and visual
refinement stay on the OpenAI-compatible image route; Dreamina is used only for
the final background-removal pass. An opaque result fails Alpha validation.

Use `image.cutout` for background removal. It uploads the original image to the
logged-in Dreamina Agent, waits for the foreground-segmentation result, downloads
the original PNG, and validates its Alpha channel before Feishu upload. It does
not call the OpenAI-compatible image edit route first, so the subject is not
redrawn. An unusable or opaque result is marked `failed` and is never reported
as completed. Each submitted Agent cutout currently consumes about one Dreamina
point.

When a request includes both image creation or visual refinement and transparent
delivery, finish the visual content first with `image.generate` and/or
`image.edit`, then submit that final image once through `image.cutout`. If the
input is already final and only needs its background removed, call
`image.cutout` directly.

The older `background=transparent` option remains compatible for generation or
general editing. If that upstream result is opaque, the same Dreamina Agent
cutout backend now handles it.

## Install

The server must already have the managed Codex relay, an authenticated
`lark-cli` identity, Node.js 22+, a Chromium executable, and a valid Dreamina
web login profile. Transparent output requires Python 3.11 or newer; Debian
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
  ARTIFACT_RELAY_DREAMINA_BROWSER='/path/to/chrome' \
  ARTIFACT_RELAY_DREAMINA_PROFILE_SOURCE='/path/to/logged-in-profile' \
  bash install-artifact-relay.sh
```

The folders must be different. The capabilities endpoint reveals only the
input folder so clients can upload source files automatically. The output
folder remains server-side configuration. `ARTIFACT_RELAY_WORKERS` defaults to
`2` and accepts values from `1` through `4`. Dreamina browser work is separately
serialized to one execution slot because one persistent Chromium profile cannot
be opened safely by two processes. Other generation and edit jobs still use the
configured worker count.

The installer copies the login profile once into
`/var/lib/codex-artifact-relay/dreamina-profile`; later installs preserve it.
The profile contains account credentials and is mode `0700`. When the web login
expires, cutout jobs fail with `dreamina_login_required` instead of silently
falling back to another model. The Agent request is submitted only once; an
ambiguous timeout is not retried automatically.

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
