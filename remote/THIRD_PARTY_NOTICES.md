# Runtime dependency notices

The new application code's MIT license does not replace upstream dependency licenses. These components are installed separately inside the owned Colab VM. No third-party source is copied into this directory.

| Component | Pinned version | Declared license | Primary source |
| --- | --- | --- | --- |
| ComfyUI | d91ed5f5b7fa60fa18464c2ad7c80254da2f0f29 | GNU GPL v3 | [Repository license at pinned revision](https://github.com/Comfy-Org/ComfyUI/blob/d91ed5f5b7fa60fa18464c2ad7c80254da2f0f29/LICENSE) |
| ComfyUI frontend | 1.55.14 | GNU GPL v3 | [Source license at pinned tag](https://github.com/Comfy-Org/ComfyUI_frontend/blob/v1.55.14/LICENSE) |
| ComfyUI Manager | 4.2.2 | GPL-3.0-only | [Pinned PyPI metadata](https://pypi.org/pypi/comfyui-manager/4.2.2/json) |
| comfy-cli | 1.22.0 | GPL-3.0-only | [Pinned PyPI metadata](https://pypi.org/pypi/comfy-cli/1.22.0/json) |
| comfy-mcp | 0.10.0 | AGPL-3.0-or-later OR LicenseRef-Comfy-Commercial | [Pinned PyPI metadata](https://pypi.org/pypi/comfy-mcp/0.10.0/json) |
| uv | 0.12.23 | MIT OR Apache-2.0 | [Pinned PyPI metadata](https://pypi.org/pypi/uv/0.12.23/json) |
| ComfyUI-Easy-Install catalog and workflow examples | MAC-Linux fa3882893d083dc116ec0c62b9fdcd4ebc91397e | MIT | [License at pinned revision](https://github.com/Tavris1/ComfyUI-Easy-Install/blob/fa3882893d083dc116ec0c62b9fdcd4ebc91397e/LICENSE) |

The pinned base-node packages retain their individual upstream licenses from their Registry archives or Git checkouts. Their repository/version/hash provenance is recorded in runtime.json and the VM's locks/easy-install-packages.json. The verified Easy Install license is retained under locks/easy-install/. Workflow example import does not grant licenses for model weights referenced by those examples. Pending source-only packages are not installed by this preview.

Metadata and the referenced source licenses were inspected during implementation. The MCP component uses the upstream dual-license declaration; no commercial grant is assumed. An upstream dependency installation retains its packaged notices and source/license references. A distributed runtime image or redistributed dependency bundle must include the applicable upstream notices and satisfy the chosen upstream license terms; it must not present the whole dependency bundle as MIT-only. Transitive wheel notices must be inventoried from the resolved locks before distributing a runtime bundle. The pinned frontend wheel metadata does not declare a license expression and its wheel does not include a `dist-info/licenses` file; the separately checked source license at its pinned tag is linked above. These notices describe dependencies; they do not claim that a release packaging compliance review has been completed.
