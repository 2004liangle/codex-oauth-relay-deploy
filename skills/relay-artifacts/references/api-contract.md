# Relay artifact API contract

Read this reference when calling the protocol without the bundled script, diagnosing failures, or selecting image options.

## Configuration

The script accepts private JSON configuration through `--config`, `RELAY_ARTIFACTS_CONFIG`, or `assets/config.json`. Host-tools mode needs only relay settings. Script-direct Lark transfer additionally needs the Lark settings. These environment variables override JSON values:

| Environment variable | Meaning |
| --- | --- |
| `RELAY_ARTIFACTS_BASE_URL` | Relay origin or OpenAI-style URL ending in `/v1` |
| `RELAY_ARTIFACTS_API_KEY` | Relay Bearer key |
| `LARK_APP_ID` | Optional fallback Lark/Feishu custom app ID |
| `LARK_APP_SECRET` | Optional fallback Lark/Feishu custom app secret |
| `LARK_API_BASE_URL` | Usually `https://open.feishu.cn`; use `https://open.larksuite.com` for Lark |
| `LARK_INPUT_FOLDER_TOKEN` | Optional override for capability discovery |

Do not publish `assets/config.json`. The distributed package contains only `config.example.json` placeholders.

The relay base may be either `https://relay.example.com` or `https://relay.example.com/v1`; the client normalizes both to the same artifact endpoints and rejects unrelated path prefixes.

In preferred host-tools mode, use the client's authenticated Drive connector to transfer file bytes and use the script only for relay capabilities, local hashing, submission, polling, and output manifests. After a host upload, `manifest --file LOCAL --file-token TOKEN --role ROLE` builds the verified input manifest. `submit-edit` and `submit-handoff` accept repeated `--input-manifest` values as inline JSON or `@path`. `download REQUEST_ID` returns output manifests; `--output-dir` opts into script-direct Lark download.

## Relay endpoints

All requests use `Authorization: Bearer <relay-key>`.

| Method and path | Purpose |
| --- | --- |
| `GET /v1/artifact-capabilities` | Discover protocol, operations, limits, retention, and input folder |
| `POST /v1/artifact-jobs` | Idempotently submit a job manifest |
| `GET /v1/artifact-jobs/{request_id}` | Poll an existing job |

Request IDs contain 8-128 ASCII letters, digits, `.`, `_`, or `-`, starting with a letter or digit. Repeating an identical ID and payload is idempotent. Reusing an ID with a different payload returns `409`.

Submit JSON:

```json
{
  "request_id": "art-20260717T120000Z-0123456789abcdef",
  "operation": "image.edit",
  "parameters": {
    "model": "gpt-image-2",
    "prompt": "Place Image 2 into Image 1",
    "quality": "high",
    "size": "1536x1024",
    "output_format": "webp"
  },
  "inputs": [
    {
      "file_token": "REMOTE_FILE_TOKEN",
      "name": "scene.png",
      "mime_type": "image/png",
      "size_bytes": 123456,
      "sha256": "64_lowercase_hex_characters",
      "role": "image"
    }
  ]
}
```

Supported operations:

- `image.generate`: no inputs; requires `parameters.prompt`.
- `image.edit`: 1-16 ordered `role=image` inputs and at most one `role=mask`; requires `parameters.prompt`.
- `artifact.handoff`: 1-32 `role=attachment` inputs; accepts only optional `parameters.instruction`.

Image parameters supported by the relay are `model`, `prompt`, `quality`, `size`, `n`, `output_format`, `output_compression`, `background`, `moderation`, `user`, and local `output_name`. The bundled client exposes the commonly used subset.

Statuses:

- `queued`, `downloading`, `processing`, `uploading`: active.
- `ready_for_processing`: handoff inputs arrived and passed integrity checks.
- `completed`: output manifests are available.
- `failed`: terminal; inspect structured `error.code`, `error.message`, and `error.retryable`.

Each input and output manifest contains `file_token`, safe base `name`, `mime_type`, positive `size_bytes`, and lowercase SHA-256. Image edit manifests also contain `role`.

## Lark/Feishu transfer

Obtain an app token with `POST /open-apis/auth/v3/tenant_access_token/internal` and JSON `app_id` plus `app_secret`.

For non-empty files up to 20 MiB, upload with multipart `POST /open-apis/drive/v1/files/upload_all`. Fields are `file_name`, `parent_type=explorer`, `parent_node`, `size`, optional Adler-32 `checksum`, and `file`.

For larger files:

1. `POST /open-apis/drive/v1/files/upload_prepare` with JSON `file_name`, `parent_type=explorer`, `parent_node`, and `size`.
2. Read `upload_id`, `block_size`, and `block_num`.
3. Sequentially call multipart `POST /open-apis/drive/v1/files/upload_part` for every block. Send `upload_id`, zero-based `seq`, exact `size`, Adler-32 `checksum`, and `file`. Do not upload blocks concurrently.
4. `POST /open-apis/drive/v1/files/upload_finish` with JSON `upload_id` and `block_num`; read `file_token`.

Download with `GET /open-apis/drive/v1/files/{file_token}/download`. Resume a partial file with `Range: bytes=<current-size>-<expected-size-minus-one>`. A resumed response must be `206`; if the server returns `200`, restart the local partial file. Verify exact size and SHA-256 before atomically replacing the destination.

## Reliability and retention

- Relay POST retries must reuse the same request ID.
- Keep `.part` files after transient download failures so a later command can resume.
- A lost response from the single-call Lark upload may have created a duplicate file; the script does not blindly retry that call.
- Lark files have manual retention. Never add time-based deletion or delete artifacts without an explicit user request.
