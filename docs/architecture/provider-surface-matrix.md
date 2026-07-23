# Provider surface matrix

Date verified: 2026-07-23

The machine-readable source of truth is
`src/torq_cli/data/provider_surfaces.v1.yaml`. Its schema is closed: every
provider must declare every required surface, and every surface must carry a
status, evidence, and verification date. Unknown providers or fields fail
validation rather than silently gaining capabilities.

## Current provider decisions

| Provider | Preferred integration | Current evidence boundary |
|---|---|---|
| `claude` | Agent SDK / first-party CLI | Authentication, structured output, resume, and usage were verified. Cancellation, tool-event fidelity, working-directory control, and rate-limit behavior remain unavailable; resolved model identity is unattestable. |
| `codex` | SDK / first-party CLI | Authentication, structured event output, and tool events were verified. Usage was not reported; resume, cancellation, working-directory control, and rate-limit behavior remain unavailable; resolved model identity is unattestable. |
| `grok` | ACP when explicitly authorized | The installed CLI was unauthenticated. The current xAI acceptable-use boundary excludes an automated consumer fallback, so the provider remains unavailable until an approved integration is demonstrated. |
| `kimi` | Direct API after credential remediation | A legacy wrapper exposed a credential during inspection. All legacy credential-bearing wrappers are prohibited until credential rotation is completed and the replacement is stored in an approved vault. |
| `zai` | Direct API after approved credential setup | No approved vault-backed credential is available. Legacy wrappers are prohibited. |
| `deepseek` | MMH/direct adapter after approved credential setup | No approved vault-backed credential is available. Legacy wrappers are prohibited. |

## Security and release boundary

The matrix records demonstrated behavior, not intended behavior. A provider
with an unavailable or blocked required surface must fail closed and cannot be
selected as a silent fallback. In particular, credential rotation is a release
gate for `kimi`; no secret value is retained in this document or in the matrix.
Model aliases are not treated as proof of resolved model identity when that
identity is unattestable from provider output.

## Primary references

- Anthropic Claude Code setup: <https://docs.anthropic.com/en/docs/claude-code/getting-started>
- OpenAI terms of use: <https://openai.com/policies/terms-of-use/>
- xAI acceptable use policy: <https://x.ai/legal/acceptable-use-policy>
- Moonshot API documentation: <https://platform.moonshot.ai/docs/guide/prompt-best-practice>
- Z.ai terms of use: <https://docs.z.ai/legal-agreement/terms-of-use>
- DeepSeek terms of use: <https://cdn.deepseek.com/policies/en-US/deepseek-terms-of-use.html>
