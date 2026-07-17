# Relay contract

Read this reference before the first relay image call in a session.

## Configuration

The client prefers dedicated environment variables:

```bash
export CODEX_RELAY_BASE_URL="https://relay.example.com/v1"
export CODEX_RELAY_API_KEY="<relay-api-key>"
```

- The base URL identifies this relay's `/v1` API root.
- Read the key from the environment. Never accept it as a positional or command-line value, place it in source control, or echo it.
- Do not reuse `OPENAI_API_KEY`; that variable normally identifies a separately billed OpenAI Platform credential.
- If the user wants persistent client configuration, `configure` reads the key through a hidden prompt or `--key-stdin` and writes `~/.config/relay-images/config.json` with mode `0600`. The client refuses group/world-readable or symlinked config files. Treat that file as a secret and never commit or display it.
- Reject a base URL containing user information, a query, or a fragment. Do not follow cross-origin redirects with an Authorization header.
- Use exact endpoint paths without a trailing slash. The public Nginx configuration allowlists exact routes.

Safe persistent setup, when requested:

```bash
printf '%s\n' "$CODEX_RELAY_API_KEY" | \
  python3 "$RELAY_IMAGES" configure \
    --base-url "$CODEX_RELAY_BASE_URL" --key-stdin
```

For a remote plain-HTTP deployment, `configure` also requires `--allow-http`. Environment variables take precedence over the stored config.

## Public image and artifact routes

| Operation | Method and exact path | Request encoding | Output |
| --- | --- | --- | --- |
| Generate | `POST /v1/images/generations` | `application/json` | JSON or SSE |
| Edit/reference/composite | `POST /v1/images/edits` | `multipart/form-data` for local files | JSON or SSE |
| Artifact capability discovery | `GET /v1/artifact-capabilities` | none | Lark input target and supported operations |
| Create/idempotently resume artifact job | `POST /v1/artifact-jobs` | `application/json` | job manifest |
| Poll artifact job | `GET /v1/artifact-jobs/{request_id}` | none | job manifest |

Every call sends:

```text
Authorization: Bearer $CODEX_RELAY_API_KEY
```

`/v1/files` remains closed. Direct mode sends local images and masks in the edit multipart request. Artifact mode uploads them with the locally authenticated `lark-cli` identity and sends only file manifests to `/v1/artifact-jobs`; it never falls back to `/v1/files`.

Run `python3 "$RELAY_IMAGES" check` after configuring a new client. It verifies `/v1/models` plus both image routes with deliberately invalid, non-generating requests; readiness is `200`, `400`, and `400` respectively.

`--dry-run` validates options, input metadata, multipart size, and planned output paths without reading the persisted relay config or API key and without making a network connection. Live calls run the same output collision and parent-writability preflight before network access. Neither path reports Authorization data or image Base64.

Read `artifact-delivery.md` for the asynchronous request schema, capability discovery, states, retries, Lark CLI boundary, hash verification, and general attachment handoff.

## Generate request

Send JSON with `model`, `prompt`, and only the controls required by the request:

```json
{
  "model": "gpt-image-2",
  "prompt": "A clean catalog photograph of a ceramic cup",
  "quality": "low",
  "size": "1024x1024",
  "output_format": "png"
}
```

The installed route accepts request bodies up to 1 MiB. This is ample for the 32,000-character GPT Image prompt limit but not for embedding input images. Use the edit endpoint for all image inputs.

## Edit request

Use multipart form data for local files. Repeat `image[]` in meaningful order:

```text
model=gpt-image-2
prompt=Place the product from Image 2 into the scene in Image 1
image[]=@scene.png
image[]=@product.png
mask=@mask.png              # optional; applies to Image 1
quality=high
size=1536x1024
output_format=webp
output_compression=85
```

- The bundled client accepts one to 16 `image[]` parts and rejects a multipart body above the relay's aggregate 64 MiB safety limit before upload.
- The client snapshots and validates the exact local bytes it will upload, so a path change during the request cannot detach validation from transmission. This can temporarily use up to the aggregate request limit in client memory.
- Send at most one `mask` part. It applies to the first image.
- Preserve image order and state every image's role in the prompt.
- The installed route accepts an aggregate request body up to 64 MiB. Nginx authenticates the exact Relay Key before reading the upload, buffers one authorized edit at a time, and rate-limits starts per client IP. Upstream per-file and model limits still apply; do not treat the Nginx ceiling as an API guarantee.
- The bundled client requires the first image and mask to be valid PNG files with identical dimensions. Each input file must be smaller than 50 MB. The non-interlaced 8-bit or 16-bit mask must decode fully, contain an alpha channel, and contain both editable and protected regions.

## Responses

For non-streaming GPT Image calls, decode each image from:

```text
data[].b64_json
```

Do not request or depend on `response_format`; GPT Image returns Base64 image data. Validate Base64 strictly and compare the decoded signature with `output_format`. If the relay normalizes the format, warn and use the actual format and extension rather than discarding a valid image. Never print the encoded image to logs or stdout.

The Codex OAuth image backend may normalize requested dimensions or format. Record `requested_size`, `requested_format`, and each decoded file's actual dimensions and format. The client sets `output_contract_met=false` on a mismatch. With `--strict-output`, it saves the returned file under its actual extension, emits `ok=false`, and exits nonzero; callers must not infer output properties from the request alone.

## Streaming

Set `stream=true` to receive server-sent events. Set `partial_images` to `0` through `3`; the bundled client automatically enables streaming when `--partial-images` is greater than zero. Streaming currently requires `n=1`. Values above zero request previews but the server may send fewer previews when the final image finishes first.

Generation events:

```text
image_generation.partial_image
image_generation.completed
```

Edit events:

```text
image_edit.partial_image
image_edit.completed
```

Each event's JSON contains Base64 image data. Treat only the single completed event as the final deliverable. The client rejects more partials than requested, partials after completion, or multiple completed events before writing files. Save accepted partials to distinct filenames, do not print them, and do not automatically replay a request after the stream has started.

## Transport and retention risk

The default deployment exposes plain HTTP unless the operator configures TLS. Over remote HTTP, the bearer key, prompt, source images, mask, and generated image can be read or modified in transit. Prefer HTTPS. If the user knowingly keeps HTTP, restrict the firewall or cloud security group to the user's source IP and pass the explicit `--allow-http` client option. For normal commands it is a global option before the subcommand, for example `python3 "$RELAY_IMAGES" --allow-http check`.

CLIProxyAPI Request Log is enabled in the default deployment. It can retain full request and response bodies, including prompts, source-image multipart data, masks, and generated Base64. The configured request-log storage is capped by total size, not immediate redaction. Anyone with authorized management access or root access to the relay host may be able to read that content.

Artifact inputs and outputs are deliberately retained in Lark. There is no seven-day expiry or automatic deletion. Delete them only after an explicit user request through the appropriate Lark Drive workflow.

Never log:

- `CODEX_RELAY_API_KEY` or the Authorization header
- request multipart bytes
- source, mask, partial, or final Base64
- full gateway error pages or management responses

## Error handling

| Status or condition | Meaning and action |
| --- | --- |
| `400` | Invalid prompt, model, parameter, multipart field, or missing image. Correct the request; do not retry unchanged. |
| `401/403` | Invalid relay key, expired authorization, or account/model eligibility issue. Do not print the key. |
| `404/405` | Wrong route, trailing slash, or method. Use the exact POST path; do not fall back to `/v1/files`. |
| `413` | Request exceeds an Nginx or upstream limit. Reduce the input files; do not increase limits silently. |
| `429` | Rate limited. Honor `Retry-After` and lower concurrency. |
| `5xx`, timeout, disconnect | Completion may be unknown. Do not retry automatically without warning about duplicate quota use. |
| Non-JSON or malformed SSE | Gateway or compatibility failure. Return a short redacted diagnostic, never the full body. |

Artifact POST retries are different from direct image retries: a client-generated request ID makes an identical artifact job submission idempotent. Always reuse the same ID and payload after a network ambiguity. A different payload with the same ID is a conflict and must not be retried unchanged.

Hash an upstream request ID before reporting it; the client exposes only `request_id_sha256`, never a remote-controlled raw header. Keep diagnostics concise and free of request bodies.

## Sources

- [OpenAI image generation guide](https://developers.openai.com/api/docs/guides/image-generation)
- [Generate image API reference](https://developers.openai.com/api/reference/resources/images/methods/generate)
- [Edit image API reference](https://developers.openai.com/api/reference/resources/images/methods/edit)
