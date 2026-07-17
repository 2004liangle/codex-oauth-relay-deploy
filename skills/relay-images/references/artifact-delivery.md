# Lark artifact delivery

Use this mode when a large synchronous Base64 response or multipart upload is unreliable. The relay exchanges small JSON job manifests; `lark-cli` moves the file bytes through the same configured Lark app identity.

## Prerequisites and discovery

- Install and configure `lark-cli` on the client with the same Lark app used by the server.
- The normal deployment uses the app's `bot` identity. Do not copy an app secret into this Skill or pass it on a command line.
- The client calls authenticated `GET /v1/artifact-capabilities` to discover the input `folder`/`wiki` token and identity. This keeps long target tokens out of normal commands.
- Environment or `artifact-configure` values are advanced overrides. Precedence is command flag, environment, private client config, then capability discovery.
- Keep the relay URL/key in `CODEX_RELAY_BASE_URL` and `CODEX_RELAY_API_KEY`; keep Lark credentials in `lark-cli`'s own configuration.

Optional override:

```bash
python3 "$RELAY_IMAGES" artifact-configure \
  --lark-as bot --wiki-token '<input-wiki-token>'
```

The config remains mode `0600`, and command output never prints the target token.

## Image commands

Text to image:

```bash
python3 "$RELAY_IMAGES" artifact-generate \
  --prompt '<prompt>' --quality low --size 1024x1024 \
  --format png --output output/generated.png
```

Image/reference/mask edit:

```bash
python3 "$RELAY_IMAGES" artifact-edit \
  --image scene.png --image product.png --mask mask.png \
  --prompt-file prompt.txt --quality high --format webp \
  --compression 85 --output output/edited.webp
```

The edit command validates the same image count, signatures, size limits, and mask constraints as direct mode. It uploads validated snapshots in order. Image entries use `role=image`; an optional mask uses `role=mask` and applies to the first image.

Asynchronous artifact delivery does not support image streaming or partial previews. Use the direct command only when the user explicitly needs those events and the link can carry Base64 reliably.

## General attachments

Upload files without creating a job:

```bash
python3 "$RELAY_IMAGES" artifact-upload \
  --file report.pdf --file data.xlsx
```

Upload and hand files to the trusted server-side processor:

```bash
python3 "$RELAY_IMAGES" artifact-handoff \
  --file report.pdf --file data.xlsx \
  --instruction 'Summarize the report and check the spreadsheet totals'
```

`instruction` is optional plain text. It is data for the trusted processor, never a shell command. `ready_for_processing` means the server downloaded and verified every input; it is a successful handoff, not a completed transformation.

Check or continue a job:

```bash
python3 "$RELAY_IMAGES" artifact-status --request-id '<request-id>'
python3 "$RELAY_IMAGES" artifact-status --request-id '<request-id>' --wait
python3 "$RELAY_IMAGES" artifact-status --request-id '<request-id>' --wait-completed
python3 "$RELAY_IMAGES" artifact-download --request-id '<request-id>' --wait \
  --output output/result.bin
```

`artifact-download --wait` continues through `ready_for_processing` until `completed` or `failed`.

## Job protocol

Create a job with exact `POST /v1/artifact-jobs`:

```json
{
  "request_id": "img-20260717T120000Z-0123456789abcdef",
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
      "file_token": "<token>",
      "name": "scene.png",
      "mime_type": "image/png",
      "size_bytes": 123456,
      "sha256": "<64 lowercase hex characters>",
      "role": "image"
    }
  ]
}
```

Supported operations are `image.generate`, `image.edit`, and `artifact.handoff`. Request IDs must be 8-128 characters from letters, digits, `.`, `_`, and `-`. Repeating the exact ID and payload is idempotent. Reusing an ID with a different payload returns `409`.

Poll exact `GET /v1/artifact-jobs/{request_id}`. Status values are:

- `queued`, `downloading`, `processing`, `uploading`: still running.
- `ready_for_processing`: handoff inputs are verified and ready for a trusted processor.
- `completed`: outputs can be downloaded.
- `failed`: inspect the structured error code and retryable flag; do not create a new image request blindly.

Every input and output manifest uses `file_token`, `name`, `mime_type`, `size_bytes`, and `sha256`. Image edit inputs additionally use `role`.

## Reliability and files

- POST retries reuse the same request ID, so an ambiguous relay response cannot create another image generation job.
- Polling and Lark downloads use bounded retries with backoff. `429` honors a numeric `Retry-After` up to 30 seconds.
- Upload retries default to zero because a lost success response can leave a duplicate Lark file. `--upload-retries` is an explicit opt-in and may leave duplicates.
- Downloads go to `.<name>.part`. The client checks exact byte size and SHA-256 before an atomic local commit. It refuses existing outputs unless `--overwrite` is explicit.
- The current `lark-cli drive +download` restarts a failed file read rather than issuing a Range resume. The artifact job is not repeated, so retrying does not consume image generation quota again.
- Do not automatically delete inputs, handoff files, or outputs. They remain in Lark until the user explicitly requests deletion.

## Security

- All artifact HTTP routes require the same relay Bearer key. Do not log it or the Authorization header.
- A `file_token` does not bypass Lark permissions. Download uses the configured local `bot` or `user` identity.
- Do not print raw `lark-cli` failures, response bodies, prompts, file bytes, or secrets. The client emits redacted categories and structured manifests only.
- Plain remote HTTP still exposes job prompts and manifests in transit. Prefer HTTPS or the explicit, user-approved insecure HTTP override.
