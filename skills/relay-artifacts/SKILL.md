---
name: relay-artifacts
description: Generate or edit images and hand off or download attachments through a configured asynchronous OpenAI-compatible relay with Lark/Feishu Drive delivery. Use for 文生图, 图生图, 多图合成, 附件中转, and 弱网下载 in Agent Skills-compatible clients when local project files must stay on the client, especially when Base64 image responses or direct large-file uploads fail.
---

# Relay Artifacts

Exchange small job manifests with the relay while Lark/Feishu Drive carries file bytes. Prefer the host client's already-authenticated Drive tools. The bundled standard-library Python client also provides an optional direct-transfer fallback with resumable downloads and SHA-256 verification.

## Requirements

- The host must support standard Agent Skills and either Python 3 script execution or equivalent native HTTP tools.
- The relay URL and relay key must be supplied privately. Never put live values in `SKILL.md`, prompts, logs, or a public ZIP.
- Preferred host-tools mode uses the client's existing authenticated Lark/Feishu Drive connection. It does not require an app ID or app secret in this Skill.
- Script-direct fallback is only for hosts without Drive tools. It additionally reads `LARK_APP_ID` and `LARK_APP_SECRET` from a private runtime configuration or secret environment.
- Whichever Lark identity transfers files must be able to upload to the relay's input folder and download its output files.
- Prefer HTTPS. Permit plain HTTP only when the user explicitly accepts the transport risk.

Provide relay configuration through a client's secret settings or a private `assets/config.json`; never put credentials into the distributed ZIP. The script reads `--config`, then `RELAY_ARTIFACTS_CONFIG`, then `assets/config.json`; environment variables override file values. Add the optional Lark fields only for script-direct fallback.

## Workflow

1. Resolve the local path to `scripts/relay_artifacts.py` from this Skill directory.
2. Run `capabilities` before the first upload. Confirm the requested operation is listed and obtain the input Drive folder token.
3. Select a transfer mode:
   - **Host tools, preferred:** upload local inputs with the client's authenticated Drive tool, then run `manifest` with the local path and returned file token to calculate the name, MIME type, byte size, and SHA-256. Pass that JSON to `submit-edit` or `submit-handoff`. After completion, ask `download` for output manifests and download those tokens with the same host tool.
   - **Script direct, fallback:** use `edit` or `handoff` with local paths. This mode needs private Lark app credentials and handles upload/download itself.
4. Select exactly one operation:
   - No source image: `generate`.
   - One or more source/reference images, or a mask: `edit`.
   - General files for trusted server-side handling: `handoff`.
5. For image work, normally add `--wait`. In host-tools mode, fetch output manifests after completion; in script-direct mode, also add `--download-dir <local-directory>`.
6. Report the request ID, final status, and local result paths. A `ready_for_processing` handoff only confirms verified server receipt; it does not mean later processing is complete.

Read [references/api-contract.md](references/api-contract.md) when implementing the protocol with native client tools, diagnosing a job, or choosing advanced image parameters.

## Commands

Run commands with the Python 3 executable available to the host.

```bash
python3 scripts/relay_artifacts.py capabilities
```

Text to image:

```bash
python3 scripts/relay_artifacts.py submit-generate \
  --prompt "A clean product photograph on a white background" \
  --quality high --size 1024x1024 --format png --wait
```

Host-tools image edit after the client uploads files to Drive:

```bash
python3 scripts/relay_artifacts.py manifest \
  --file scene.png --file-token HOST_RETURNED_TOKEN --role image

python3 scripts/relay_artifacts.py submit-edit \
  --input-manifest @image-1.json --input-manifest @image-2.json \
  --prompt "Place Image 2 naturally into Image 1" \
  --quality high --format webp --wait
```

Each manifest is a JSON object with `file_token`, `name`, `mime_type`, `size_bytes`, `sha256`, and `role` (`image`, `mask`, or `attachment`). A manifest argument may be inline JSON or `@path` to JSON containing one object or an array.

Script-direct image edit or multi-image composition:

```bash
python3 scripts/relay_artifacts.py edit \
  --image scene.png --image product.png --mask mask.png \
  --prompt "Place Image 2 naturally into Image 1" \
  --quality high --format webp \
  --wait --download-dir output
```

Host-tools attachment handoff:

```bash
python3 scripts/relay_artifacts.py submit-handoff \
  --input-manifest @report.json --input-manifest @data.json \
  --instruction "Summarize the report and verify the spreadsheet totals" \
  --wait
```

Script-direct attachment handoff:

```bash
python3 scripts/relay_artifacts.py handoff \
  --file report.pdf --file data.xlsx \
  --instruction "Summarize the report and verify the spreadsheet totals" \
  --wait
```

Continue an existing job or download completed outputs:

```bash
python3 scripts/relay_artifacts.py status REQUEST_ID --wait
python3 scripts/relay_artifacts.py download REQUEST_ID --wait
```

The last command returns output manifests without requiring Lark credentials. Let host Drive tools download each `file_token`. Add `--output-dir output` only for script-direct download.

Use `--prompt-file` or `--instruction-file` when text is already in a local file. Reuse the same request ID after an ambiguous relay response; changing the payload under an existing ID is a conflict.

## File Safety

- Preserve input order. A mask applies to the first image.
- Never substitute Base64 transport for artifact jobs on a weak connection.
- In either transfer mode, do not claim success until the downloaded byte count and SHA-256 match the output manifest.
- Do not delete Lark inputs or outputs automatically. Retention is manual; delete only after an explicit user request.
- If the host cannot execute Python or access local project files, state that limitation. A cloud-only client cannot silently write to a user's local project.
