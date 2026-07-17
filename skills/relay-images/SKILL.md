---
name: relay-images
description: "Generate and edit raster images through a user-configured OpenAI-compatible relay, including optional Lark Drive delivery for weak networks and attachment handoff. Use when the user explicitly asks for relay-based 文生图, 图生图, local reference images, multi-image composition, masks, quality or size controls, or Lark-delivered artifacts. Do not use for ordinary image requests that should use the built-in image generator, for SVG or other deterministic vector work, or for direct OpenAI Platform billing."
---

# Relay Images

Use the bundled relay client to create or edit images while keeping project files on the local client. For weak links, use asynchronous artifact delivery: large inputs and outputs travel through the configured Lark Drive identity while the relay carries only small manifests and job status.

## Execution boundary

- Generate new images with exact `POST /v1/images/generations`.
- Edit, compose, or use local reference images with exact `POST /v1/images/edits` and multipart form data.
- For Lark delivery, use `POST /v1/artifact-jobs`, `GET /v1/artifact-jobs/{request_id}`, and authenticated capability discovery at `GET /v1/artifact-capabilities`.
- Send multiple local inputs as ordered `image[]` parts. Send an optional mask as the single `mask` part.
- Do not use `/v1/files`; it is intentionally closed on this relay.
- Do not substitute the Responses API for the two image endpoints unless the user explicitly requests a different integration.
- Prefer `CODEX_RELAY_BASE_URL` and `CODEX_RELAY_API_KEY`. The optional `configure` command stores the same values in a mode-`0600` client config when the user explicitly wants persistence. Never substitute `OPENAI_API_KEY`, because that can route traffic to a separately billed Platform account.

## Workflow

1. For a live call or `check`, confirm that `CODEX_RELAY_BASE_URL` and `CODEX_RELAY_API_KEY` are present, or that the user explicitly chose the mode-`0600` client config, without printing either secret. A `--dry-run` is exempt: it deliberately reads neither config nor key. Use `configure --key-stdin` only when the user asks to persist credentials locally.
2. Read `references/relay-contract.md` before the first call in a session. If the endpoint is remote plain HTTP, warn that the key, prompt, source images, and generated image can be observed in transit. Prefer HTTPS; require the client's explicit insecure-HTTP override or user confirmation before continuing.
3. Before any quota-consuming call, ask one concise clarification when an essential subject, source role, visible text, or preservation requirement is missing. Do not invent a product identity or other central content merely to avoid asking.
4. Choose the operation and delivery:
   - Reliable/fast direct link, no source image: `generate`.
   - Reliable/fast direct link with sources: `edit`.
   - Weak link or requested Lark delivery, no source image: `artifact-generate`.
   - Weak link or requested Lark delivery with sources/mask: `artifact-edit`.
   - General attachment handoff for later trusted processing: `artifact-handoff`.
5. Inspect every local input image before editing. Record each image's ordered role and identify which first image a mask targets.
6. Shape the prompt with `references/prompting.md`. Preserve the user's exact requested text, constraints, and invariants.
7. Select output controls with `references/image-options.md`. This Skill deliberately uses the verified `gpt-image-2` relay path and does not guess alternate-model parameter contracts.
8. Locate the bundled client and inspect its current interface before calling it:

   ```bash
   export RELAY_IMAGES="${CODEX_HOME:-$HOME/.codex}/skills/relay-images/scripts/relay_images.py"
   python3 "$RELAY_IMAGES" --help
   python3 "$RELAY_IMAGES" generate --help
   python3 "$RELAY_IMAGES" edit --help
   python3 "$RELAY_IMAGES" artifact-generate --help
   python3 "$RELAY_IMAGES" artifact-edit --help
   ```

9. Run the no-generation route check before the first real call after configuration:

   ```bash
   python3 "$RELAY_IMAGES" check
   ```

10. Use the direct commands on a reliable link. Use artifact commands when large Base64/multipart transfers over the relay are unreliable. The stable forms are:

   ```bash
   python3 "$RELAY_IMAGES" generate \
     --prompt "<prompt>" --quality low --size 1024x1024 \
     --format png --output output/generated.png

   python3 "$RELAY_IMAGES" edit \
     --image input-1.png --image input-2.png --mask mask.png \
     --prompt-file prompt.txt --quality high --format webp \
     --compression 85 --output output/edited.webp

   python3 "$RELAY_IMAGES" artifact-generate \
     --prompt "<prompt>" --quality low --size 1024x1024 \
     --format png --output output/generated.png

   python3 "$RELAY_IMAGES" artifact-edit \
     --image input-1.png --image input-2.png --mask mask.png \
     --prompt-file prompt.txt --quality high --format webp \
     --compression 85 --output output/edited.webp
   ```

   Write outputs to the user's requested path or a project-local output directory. Use `--dry-run` to inspect a redacted request plan. Add `--strict-output` when exact returned dimensions and file format are hard requirements; a mismatch is still saved with its actual extension, reported as unmet, and exits nonzero. Do not create an ad hoc curl or SDK wrapper when the bundled client supports the request.
11. Inspect the final saved image. Verify subject, composition, exact text, requested dimensions and format, and edit invariants. For a mask, verify the surrounding region did not drift materially; masking guides the model but is not pixel-exact.
12. Report the final path, operation, model, quality, size, and format. Never print the API key, Authorization header, raw Base64, or full request/response body.

## Operation rules

### Generate

- Treat `n` as variants of one prompt, not different assets.
- Use `quality=low` for drafts when the user has not asked for final quality. Use `medium`, `high`, or `auto` for final work according to the request.
- Use streaming only when partial previews are useful or explicitly requested. Partial images consume additional output tokens and are not final deliverables.

### Edit

- Preserve input order. The bundled client accepts up to 16 repeated `--image` values, subject to the aggregate request limit. Describe every image as `Image 1`, `Image 2`, and so on in the prompt.
- Use the first image as the primary edit target when a mask is present; the mask applies only to that image. The bundled client requires both that first image and the alpha-channel mask to be valid PNG files with identical dimensions. The mask must decode fully and contain both editable and protected alpha regions.
- Repeat invariants in the prompt, such as `change only the background; preserve the subject, pose, crop, lighting direction, and all text`.
- Treat exact subject or identity preservation as best effort. If unchanged pixels are a hard requirement and no suitable protective mask exists, ask the user for one or explain the limitation before consuming quota.
- Do not promise exact mask boundaries, identity preservation, typography, or layout. Inspect and iterate with one targeted correction when necessary.
- Do not set `input_fidelity` for `gpt-image-2`; its image inputs are always processed at high fidelity.

### Artifact delivery

- Read `references/artifact-delivery.md` before the first artifact command in a session.
- Let `/v1/artifact-capabilities` discover the input target and `bot`/`user` identity. Use `artifact-configure` only as an explicit local override; never hardcode target tokens in project files.
- `artifact-edit` validates and snapshots ordered images and the mask before uploading them. The job manifest uses `role=image` in order and one final `role=mask` when present.
- Use `artifact-handoff --file ... --instruction ...` for non-image attachments. `ready_for_processing` means the files were downloaded and verified by the server; it does not mean later processing is complete.
- Inputs and results are retained in Lark until the user explicitly deletes them. Never add automatic expiry or cleanup.

## Output and failure handling

- Decode only `data[].b64_json`; GPT Image does not use `response_format`.
- In artifact mode, do not expect Base64. Poll by the same 8-128 character `request_id`, then download each `file_token` through the locally authenticated `lark-cli` identity.
- Save final images atomically and refuse to overwrite existing files unless the user explicitly requests replacement.
- Artifact downloads use a `.part` path, retry safe reads, verify `size_bytes` and SHA-256, and only then atomically commit the local file. A failed download can be resumed by rerunning `artifact-download` with the same request ID without regenerating the image.
- Preflight every possible PNG/JPEG/WebP target before a quota-consuming call. Use `--strict-output` when a format or exact size mismatch must make the command fail after preserving the returned file.
- Keep partial streamed images separate from final outputs and label them by index.
- Do not retry a generation or edit after an ambiguous timeout or after any stream event without warning: the first request may already have consumed quota.
- Treat `401/403` as an authentication or account-eligibility issue, `404/405` as a route mismatch, `413` as a body-size failure, `429` as rate limiting, and `5xx` or disconnects as an unknown-completion state.
- The relay's Request Log can retain prompts, multipart source images, masks, generated Base64, and other response data. Do not claim that local key handling makes image content private from the relay host.

## References

- Relay endpoints, authentication, transport, logging, streaming, and errors: `references/relay-contract.md`
- Lark upload, job manifests, polling, downloads, retries, and retention: `references/artifact-delivery.md`
- Models, quality, size, format, compression, multi-image, mask, and streaming options: `references/image-options.md`
- Prompt construction for generation and editing: `references/prompting.md`
