---
name: relay-artifacts
description: 通过已配置的 OpenAI 兼容异步中转服务生成或编辑图片，并借助飞书云盘中转、下载图片和附件。适用于支持 Agent Skills 的客户端中的文生图、图生图、多图合成、附件中转和弱网下载，尤其适合本地项目文件必须留在客户端、Base64 图片回传或大文件直传容易失败的情况。
---

# 飞书中转图片与附件

客户端与中转服务之间只交换体积很小的任务清单，实际文件由飞书云盘传输。优先使用客户端已经登录的飞书云盘工具。包内的纯 Python 客户端也提供备用直传方式，支持断点续传和 SHA-256 完整性校验。

## 使用条件

- 客户端必须支持标准 Agent Skills，并且能够运行 Python 3 脚本或使用等效的原生 HTTP 工具。
- 中转地址和中转密钥必须私下配置。禁止把真实值写入 `SKILL.md`、提示词、日志或公开 ZIP 包。
- **优先方式：客户端工具。** 复用客户端现有的飞书登录状态和云盘工具，不需要在本 Skill 中填写应用 ID 或应用密钥。
- **备用方式：脚本直传。** 只适用于没有云盘工具的客户端，需要从私密运行配置或环境变量读取 `LARK_APP_ID` 和 `LARK_APP_SECRET`。
- 实际传输文件的飞书身份，必须能够向中转服务的输入文件夹上传文件，并能下载输出文件。
- 优先使用 HTTPS。只有用户明确接受明文传输风险时，才允许使用 HTTP。

通过客户端的密钥设置或私有的 `assets/config.json` 提供中转配置，禁止把凭据放进公开分发的 ZIP 包。脚本依次读取 `--config`、`RELAY_ARTIFACTS_CONFIG` 和 `assets/config.json`，环境变量的优先级高于文件配置。只有使用脚本直传时，才需要填写飞书应用相关字段。

## 操作流程

1. 从本 Skill 目录定位 `scripts/relay_artifacts.py` 的本地路径。
2. 第一次上传前运行 `capabilities`，确认服务支持所需操作，并取得飞书输入文件夹的 token。
3. 选择一种传输方式：
   - **客户端工具，推荐：** 用客户端已登录的飞书云盘工具上传本地文件，再把本地路径和上传后返回的文件 token 传给 `manifest`，由它计算文件名、MIME 类型、字节数和 SHA-256。把生成的 JSON 交给 `submit-edit` 或 `submit-handoff`。任务完成后，用 `download` 获取输出清单，再用同一个云盘工具下载清单中的文件 token。
   - **脚本直传，备用：** 直接把本地路径传给 `edit` 或 `handoff`。这种方式需要私密的飞书应用凭据，由脚本自行完成上传和下载。
4. 每次只选择一种操作：
   - 没有原图：使用 `generate` 文生图。
   - 有一张或多张原图、参考图，或者使用蒙版：使用 `edit`。
   - 把普通附件交给可信服务端处理：使用 `handoff`。
   - 需要真正透明的抠图结果：图片任务添加 `--background transparent --format png`。普通照片使用默认模型；动漫人物再添加 `--cutout-model isnet-anime`。服务端会在上传前验证 Alpha 通道，不合格的结果不会标记为完成。
5. 图片任务通常加上 `--wait` 等待完成。使用客户端工具时，完成后获取输出清单；使用脚本直传时，再加 `--download-dir <本地目录>`。
6. 最终说明任务 ID、任务状态和本地结果路径。附件任务出现 `ready_for_processing`，只表示服务端已收到文件并通过完整性检查，不代表后续处理已经完成。

需要直接实现接口、排查任务故障或选择高级图片参数时，读取 [接口说明](references/api-contract.md)。

## 常用命令

使用客户端可用的 Python 3 运行以下命令。

查看中转服务能力：

```bash
python3 scripts/relay_artifacts.py capabilities
```

文生图：

```bash
python3 scripts/relay_artifacts.py submit-generate \
  --prompt "白色背景上的简洁商品摄影图" \
  --quality high --size 1024x1024 --format png --wait
```

客户端先把图片上传到飞书云盘，再提交图生图任务：

```bash
python3 scripts/relay_artifacts.py manifest \
  --file scene.png --file-token HOST_RETURNED_TOKEN --role image

python3 scripts/relay_artifacts.py submit-edit \
  --input-manifest @image-1.json --input-manifest @image-2.json \
  --prompt "把图片 2 中的主体自然地放入图片 1" \
  --quality high --format webp --wait
```

每份清单都是一个 JSON 对象，包含 `file_token`、`name`、`mime_type`、`size_bytes`、`sha256` 和 `role`。`role` 可选值为 `image`（图片）、`mask`（蒙版）或 `attachment`（附件）。清单参数可以直接传入 JSON，也可以使用 `@路径` 读取包含单个对象或对象数组的 JSON 文件。

使用脚本直传完成图生图或多图合成：

```bash
python3 scripts/relay_artifacts.py edit \
  --image scene.png --image product.png --mask mask.png \
  --prompt "把图片 2 中的主体自然地放入图片 1" \
  --quality high --format webp \
  --wait --download-dir output
```

抠出普通图片主体并返回真正透明的 PNG：

```bash
python3 scripts/relay_artifacts.py edit \
  --image product.png \
  --prompt "只保留主体，完整移除背景" \
  --background transparent --format png \
  --wait --download-dir output
```

动漫人物抠图时添加 `--cutout-model isnet-anime`。不要只在提示词中要求透明，也不要把棋盘格当作透明通道。

客户端已上传附件时提交中转任务：

```bash
python3 scripts/relay_artifacts.py submit-handoff \
  --input-manifest @report.json --input-manifest @data.json \
  --instruction "总结报告，并核对表格中的合计数据" \
  --wait
```

使用脚本直传附件：

```bash
python3 scripts/relay_artifacts.py handoff \
  --file report.pdf --file data.xlsx \
  --instruction "总结报告，并核对表格中的合计数据" \
  --wait
```

继续等待已有任务，或获取已完成任务的输出：

```bash
python3 scripts/relay_artifacts.py status REQUEST_ID --wait
python3 scripts/relay_artifacts.py download REQUEST_ID --wait
```

最后一条命令无需飞书应用凭据，只返回输出文件清单。随后让客户端的飞书云盘工具下载每个 `file_token`。只有使用脚本直传下载时，才添加 `--output-dir output`。

提示词或处理说明已经保存在本地文件中时，可以使用 `--prompt-file` 或 `--instruction-file`。如果中转请求的响应不明确，重试时必须复用同一个任务 ID；同一任务 ID 不能改用不同请求内容。

## 文件安全

- 保持输入图片的顺序不变，蒙版只作用于第一张图片。
- 弱网环境下不要把文件改为 Base64 直接传输，应继续使用文件中转任务。
- 无论使用哪种传输方式，只有下载字节数和 SHA-256 都与输出清单一致，才能确认成功。
- 不要自动删除飞书中的输入或输出文件。文件长期保留，只有用户明确要求时才能删除。
- 如果客户端不能运行 Python，或者无法访问本地项目文件，应明确说明限制。纯云端客户端不能直接把文件写入用户的本地项目。
