# 中转文件接口说明

在不使用随附脚本直接调用协议、排查故障或选择图片参数时，请阅读本参考说明。

## 配置

脚本可通过 `--config`、`RELAY_ARTIFACTS_CONFIG` 或 `assets/config.json` 读取私有 JSON 配置。客户端工具模式只需要中转服务配置；脚本直连 Lark/飞书传输时还需要 Lark/飞书配置。以下环境变量会覆盖 JSON 中的值：

| 环境变量 | 含义 |
| --- | --- |
| `RELAY_ARTIFACTS_BASE_URL` | 中转服务源地址，或以 `/v1` 结尾的 OpenAI 风格地址 |
| `RELAY_ARTIFACTS_API_KEY` | 中转服务采用 `Bearer` 认证时使用的密钥 |
| `LARK_APP_ID` | 备用的 Lark/飞书自建应用 ID，可不填 |
| `LARK_APP_SECRET` | 备用的 Lark/飞书自建应用密钥，可不填 |
| `LARK_API_BASE_URL` | 通常为 `https://open.feishu.cn`；使用 Lark 时填写 `https://open.larksuite.com` |
| `LARK_INPUT_FOLDER_TOKEN` | 可选，用于覆盖能力发现返回的目标文件夹 |

不要公开 `assets/config.json`。发布包中只包含带占位值的 `config.example.json`。

中转服务地址可以是 `https://relay.example.com`，也可以是 `https://relay.example.com/v1`。客户端会将两者转换为相同的文件接口地址，并拒绝无关的路径前缀。

首选客户端工具模式：使用客户端已经认证的云盘连接器传输文件内容，脚本只负责查询中转能力、计算本地哈希、提交任务、轮询状态和获取输出清单。客户端工具上传完成后，使用 `manifest --file LOCAL --file-token TOKEN --role ROLE` 生成经过校验的输入清单。`submit-edit` 和 `submit-handoff` 可通过重复传入 `--input-manifest` 接收内联 JSON 或 `@path`。`download REQUEST_ID` 返回输出清单；传入 `--output-dir` 才会启用脚本直连 Lark/飞书下载。

## 中转服务端点

所有请求都使用 `Authorization: Bearer <relay-key>`。

| 方法和路径 | 用途 |
| --- | --- |
| `GET /v1/artifact-capabilities` | 查询协议、操作、限制、保留策略和输入文件夹 |
| `POST /v1/artifact-jobs` | 以幂等方式提交任务清单 |
| `GET /v1/artifact-jobs/{request_id}` | 轮询已有任务 |

请求 ID 长度为 8-128 个 ASCII 字符，只能包含字母、数字、`.`、`_` 或 `-`，且必须以字母或数字开头。使用相同 ID 重复提交完全相同的载荷是幂等操作；使用同一 ID 提交不同载荷会返回 `409`。

提交以下 JSON：

```json
{
  "request_id": "art-20260717T120000Z-0123456789abcdef",
  "operation": "image.edit",
  "parameters": {
    "model": "gpt-image-2",
    "prompt": "将图片 2 放入图片 1",
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

支持以下操作：

- `image.generate`：不接收输入文件；必须提供 `parameters.prompt`。
- `image.edit`：按顺序接收 1-16 个 `role=image` 输入，最多再接收一个 `role=mask` 输入；必须提供 `parameters.prompt`。
- `artifact.handoff`：接收 1-32 个 `role=attachment` 输入；只接受可选的 `parameters.instruction` 参数。

中转服务支持的图片参数包括 `model`、`prompt`、`quality`、`size`、`n`、`output_format`、`output_compression`、`background`、`moderation`、`user`，以及本地使用的 `output_name`。随附客户端开放了其中常用的参数。

状态值：

- `queued`、`downloading`、`processing`、`uploading`：任务仍在运行。
- `ready_for_processing`：中转附件已到达并通过完整性校验，可以开始处理。
- `completed`：输出清单已可获取。
- `failed`：任务已终止；检查结构化字段 `error.code`、`error.message` 和 `error.retryable`。

每个输入和输出清单都包含 `file_token`、安全的基础文件名 `name`、`mime_type`、正数 `size_bytes` 和小写 SHA-256。图片编辑清单还包含 `role`。

## Lark/飞书文件传输

调用 `POST /open-apis/auth/v3/tenant_access_token/internal`，并在 JSON 中提供 `app_id` 和 `app_secret`，以获取应用访问令牌。

对于不超过 20 MiB 的非空文件，使用 multipart 请求调用 `POST /open-apis/drive/v1/files/upload_all`。字段包括 `file_name`、`parent_type=explorer`、`parent_node`、`size`、可选的 Adler-32 `checksum`，以及 `file`。

对于更大的文件：

1. 使用 JSON 字段 `file_name`、`parent_type=explorer`、`parent_node` 和 `size` 调用 `POST /open-apis/drive/v1/files/upload_prepare`。
2. 读取 `upload_id`、`block_size` 和 `block_num`。
3. 按顺序对每个分片调用 multipart 接口 `POST /open-apis/drive/v1/files/upload_part`。传入 `upload_id`、从零开始的 `seq`、精确的 `size`、Adler-32 `checksum` 和 `file`。不要并发上传分片。
4. 使用 JSON 字段 `upload_id` 和 `block_num` 调用 `POST /open-apis/drive/v1/files/upload_finish`，然后读取 `file_token`。

使用 `GET /open-apis/drive/v1/files/{file_token}/download` 下载文件。通过 `Range: bytes=<current-size>-<expected-size-minus-one>` 续传部分文件。续传响应必须为 `206`；如果服务端返回 `200`，应从头写入本地临时文件。只有在核对精确字节数和 SHA-256 后，才能以原子方式替换目标文件。

## 可靠性与保留策略

- 重试中转服务的 POST 请求时，必须复用同一个请求 ID。
- 临时下载失败后保留 `.part` 文件，以便后续命令断点续传。
- Lark/飞书单次上传的成功响应如果丢失，可能已经产生重复文件；脚本不会盲目重试该调用。
- Lark/飞书文件采用手动保留策略。不得增加定时删除，也不得在用户没有明确要求时删除附件。
