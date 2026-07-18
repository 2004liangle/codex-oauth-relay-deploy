# Image options

Use only options supported by the selected model and relay. Prefer omitting optional fields over guessing.

## Model

Use `gpt-image-2`. It supports generation, editing, multiple reference images, masks, flexible output sizes, and high-fidelity image inputs. The bundled client deliberately rejects other model names because their parameter contracts differ and are not verified for this relay.

- Model availability depends on the relay's authenticated Codex account. Do not silently switch models after an unsupported-model error.
- Do not set `input_fidelity` for `gpt-image-2`; the API always processes every image input at high fidelity.
- The direct `gpt-image-2` route does not currently provide reliable `background=transparent` output. Transparent output is supported only through `artifact-generate` or `artifact-edit`, where the client first requires advertised Alpha validation and the artifact service applies local background removal when necessary.

Bundled client defaults are `gpt-image-2`, `quality=low`, `size=1024x1024`, `output_format=png`, `background=auto`, `moderation=auto`, `n=1`, non-streaming, and a 300-second timeout. Override them only to serve the request.

## Core parameters

| Parameter | Values | Guidance |
| --- | --- | --- |
| `quality` | `low`, `medium`, `high`, `auto` | Use `low` for quick drafts. Use `medium` or `high` for final, text-heavy, identity-sensitive, or detailed assets. Use `auto` when the user has no firm preference. |
| `size` | `auto` or valid `WIDTHxHEIGHT` | Square is usually fastest. Match the consuming layout; do not generate 4K by default. |
| `output_format` | `png`, `jpeg`, `webp` | CLI: `--format`. PNG for lossless graphics and text; JPEG for photographic compatibility; WebP for compact web delivery. |
| `output_compression` | integer `0`-`100` | CLI: `--compression`. Send only for JPEG or WebP. Higher values preserve more detail and create larger files. |
| `n` | integer `1`-`10` | Variants of one prompt. Use separate jobs and prompts for distinct assets. |
| `moderation` | `auto`, `low` | Keep `auto` unless the user has a valid reason for the supported lower setting. |
| `background` | `auto`, `opaque`; `transparent` in artifact mode | Transparent output requires PNG and server-side artifact delivery. The default general model includes fine-hair Alpha refinement and should be tried first; use `--cutout-model isnet-anime` when a flat-color illustration loses its outer contour. |

## `gpt-image-2` size validation

`auto` is valid. A custom resolution must satisfy all of these rules:

- Each edge is a multiple of 16 pixels.
- The longest edge is no more than 3840 pixels.
- Long-edge to short-edge ratio is no more than 3:1.
- Total pixels are between 655,360 and 8,294,400 inclusive.

Common valid sizes:

| Use | Size |
| --- | --- |
| Fast square | `1024x1024` |
| Landscape | `1536x1024` |
| Portrait | `1024x1536` |
| 2K square | `2048x2048` |
| Widescreen | `2048x1152` |
| 4K landscape | `3840x2160` |
| 4K portrait | `2160x3840` |

Outputs above 3,686,400 total pixels are experimental and can be slower. Confirm that the larger output is useful before requesting it.

Treat `size` as the requested size, then inspect the decoded file. The current Codex OAuth image backend can normalize dimensions instead of returning the exact requested values. The bundled client reports actual width and height from the decoded image and warns on a mismatch. Do not claim exact pixel dimensions until that check passes; resize locally as a separate step when a consuming system requires an exact canvas.

## Editing, multiple images, and masks

- The bundled client accepts up to 16 ordered `--image` / multipart `image[]` inputs, subject to the relay's aggregate 64 MiB safety limit. The 50 MB per-file ceiling does not mean two near-limit files fit in one request.
- Image order is semantic. Describe each input by its one-based index and role in the prompt.
- A single mask may be supplied. It applies to the first input image only.
- As conservative client-side rules, unmasked inputs must be PNG, JPEG, or WebP and each file must be smaller than 50 MB.
- When using a mask, the first image and mask must both be valid PNG, have identical dimensions, each be smaller than 50 MB, and the mask must be a fully decodable non-interlaced 8-bit or 16-bit alpha PNG with both editable and protected regions.
- Masking is prompt-guided. It may not follow the alpha shape pixel-for-pixel, so repeat what may and may not change and inspect the result.
- Multiple high-fidelity image inputs increase image-token use. Include only references that materially affect the result.

## Format and compression choices

- Prefer PNG for UI assets, diagrams, screenshots, small text, hard edges, or further editing.
- Prefer JPEG for opaque photographs when broad compatibility matters.
- Prefer WebP for web delivery when the consuming environment supports it.
- Do not send `output_compression` with PNG.
- Compare `output_format` with the decoded magic bytes. The current relay can normalize the output; on a mismatch, warn and save with the actual format's extension. Use `--strict-output` when a mismatch must return a nonzero status. Treat the decoded signature and dimensions, not the requested values, as authoritative.

## Streaming

Streaming and partial previews are available only with the direct `generate`/`edit` commands. `artifact-generate` and `artifact-edit` deliver completed files through Lark and reject these options.

| Parameter | Values | Guidance |
| --- | --- | --- |
| `stream` | `true`, `false` | Default to non-streaming. Enable only for requested or useful progress previews. |
| `partial_images` | integer `0`-`3` | CLI: `--partial-images`. A positive value automatically enables streaming; `0` with `--stream` requests only the final streamed image. |

The bundled client currently requires `--n 1` for streaming. The service may return fewer partial images than requested if the final image completes first. Each partial image incurs additional image output tokens, so do not enable partials for unattended work. Partial and completed events carry Base64 and require the same strict decoding and redaction as non-streaming output.

## Output contract

- Non-streaming images: `data[].b64_json`.
- Streaming previews and final images: event-specific Base64 fields documented in `relay-contract.md`.
- Do not expose `response_format`; it does not apply to GPT Image output.
- Do not treat a partial image as a successful final result.

## Sources

- [OpenAI image generation guide](https://developers.openai.com/api/docs/guides/image-generation)
- [GPT Image 2 model](https://developers.openai.com/api/docs/models/gpt-image-2)
- [Generate image API reference](https://developers.openai.com/api/reference/resources/images/methods/generate)
- [Edit image API reference](https://developers.openai.com/api/reference/resources/images/methods/edit)
