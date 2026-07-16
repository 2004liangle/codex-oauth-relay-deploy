# Prompting

Turn the user's request into a compact production specification. Preserve intent and exact text; add detail only when it materially improves the requested result.

## Choose the intent

- Use `generate` when there are no input images.
- Use `edit` when any input image supplies content, style, composition, identity, or a region to modify.
- With multiple inputs, decide which is the primary canvas and keep it first. The bundled client accepts at most 16 inputs within the aggregate request limit. When a mask is present, the first image is always the masked edit target and both it and the mask must be PNG.

## Prompt scaffold

Use only relevant lines:

```text
Primary request: <the requested result>
Asset use: <where the image will be used>
Input images: <Image 1 role; Image 2 role; ...>
Subject: <main subject and required attributes>
Scene: <environment or background>
Style/medium: <photo, illustration, 3D, print, etc.>
Composition: <aspect, framing, camera, placement, negative space>
Lighting/color: <only requested or useful details>
Text (verbatim): "<exact visible text>"
Change: <what must change in an edit>
Preserve: <what must remain invariant>
Avoid: <specific unwanted artifacts or content>
```

Do not add brands, slogans, characters, objects, palettes, or narrative details that the user did not request or imply.

## Text-to-image

- State the primary subject and intended asset use first.
- Match composition to the requested output size. For banners and heroes, specify usable negative space only when the consuming layout needs it.
- Quote all visible text exactly. State the language, line breaks, hierarchy, and placement when they matter.
- Keep negative constraints concrete: `no watermark`, `no extra text`, `no cropped product`, rather than a long generic negative list.

Example:

```text
Primary request: A clean catalog photograph of a matte white ceramic cup.
Asset use: Square product thumbnail.
Scene: Neutral light-gray seamless studio backdrop.
Composition: Centered three-quarter view, entire cup visible, balanced margin.
Lighting/color: Soft daylight from the left, natural shadow, accurate white ceramic.
Preserve: No logo or printed text.
Avoid: Watermark, extra objects, cropped handle, dramatic color cast.
```

## Single-image edit

Name the change once, then lock the invariants:

```text
Primary request: Replace only the background with a bright modern kitchen.
Input images: Image 1 is the primary edit target.
Change: Background only.
Preserve: Product identity, shape, material, camera angle, crop, scale, handle position, and all existing text exactly.
Avoid: New objects overlapping the product, logo changes, reframing, or altered reflections on the product.
```

Use `preserve the rest unchanged` only after listing the important invariants. A vague preservation request is not enough for identity- or layout-sensitive edits.

## Multiple-image edit or composition

- Number every source according to multipart order.
- Assign one role per input: primary canvas, subject donor, style reference, material reference, or compositing element.
- State which properties transfer and which must not transfer.
- Specify scale, placement, perspective, lighting, and occlusion when compositing.

Example:

```text
Primary request: Place the product from Image 2 on the empty counter in Image 1.
Input images: Image 1 is the primary scene and composition. Image 2 supplies only the product and its exact label.
Change: Add the Image 2 product to the center-right counter at realistic countertop scale.
Preserve: Image 1 camera, room geometry, crop, and lighting; Image 2 product shape, colors, and label text.
Lighting/color: Match the product shadow, white balance, reflections, and perspective to Image 1.
Avoid: Importing Image 2's background, changing the room, duplicating the product, or rewriting the label.
```

## Masked edit

- State explicitly that the mask applies to Image 1.
- Treat transparent or low-alpha mask regions as editable and opaque regions as protected. Describe the desired content for the entire resulting image, then identify the intended change inside the editable region.
- Repeat what outside the mask must remain unchanged.
- Do not promise a pixel-exact boundary. GPT Image uses the mask as prompt guidance and can alter nearby pixels.

Example:

```text
Primary request: Add a small flamingo pool float in the masked region.
Input images: Image 1 is the edit target; the mask applies to Image 1.
Change: Modify the masked pool region to add one correctly scaled pink flamingo float.
Preserve: Everything outside the masked region, including architecture, furniture, camera, crop, people, lighting direction, and color grade.
Avoid: Additional floats, changes to the pool outline, or edits outside the masked area.
```

## Iteration

Inspect every result before accepting it. When revising:

- Change one important instruction at a time.
- Restate the primary invariant and exact visible text.
- Do not broaden a local edit into a full regeneration unless the user agrees.
- Keep the same input order across retries.
- If a mask boundary drifts, strengthen the preserve instruction; do not claim the endpoint can enforce exact pixels.
