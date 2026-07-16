# Plain-Chinese Usage Dashboard

This directory contains the reproducible source patch and build script for the
plain Simplified Chinese frontend served at `/usage/`.

- Upstream: `Willxup/cpa-usage-keeper` `v1.13.2`
- Pinned commit: `05573ca5aa701786b9ecf1b5af56e3cc31547ca8`
- License: MIT; the binary package retains the upstream `LICENSE`
- Backend: the unmodified, pinned CPA Usage Keeper `v1.13.2`

The customization changes only the browser frontend. It defaults new browser
profiles to Simplified Chinese and replaces dashboard jargon with short labels
and explanations intended for non-technical users. Model names, HTTP status
codes, API paths, and API key values remain unchanged for troubleshooting.

Build with Node.js 24 and GNU tar/gzip:

```bash
bash usage-ui/build-plain-zh.sh /tmp/usage-ui-dist
```

The script fetches the exact upstream commit, applies `plain-zh.patch`, runs the
upstream frontend tests, lint, type check, and build, fixes the public base path
to `/usage`, includes the upstream license, and creates a deterministic archive.
