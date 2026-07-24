# Provider surface matrix

Observation dates: 2026-07-23 through 2026-07-24

The machine-readable decision matrix is
`src/torq_cli/data/provider_surfaces.v1.yaml`. The surface evidence was produced
by out-of-band operator probes and manually transcribed into the matrix. It is
not machine-generated attestation and is not receipt-backed. A `verified`
surface status therefore means "reported as observed by the operator on the
stated date"; it does not mean TORQ generated or independently verified an
attestation artifact.

The schema is closed: every provider must declare every required surface, and
every surface must carry a status, evidence note, and observation date. Unknown
providers or fields fail validation rather than silently gaining capabilities.

## Current provider decisions

| Provider | Preferred integration | Current evidence boundary |
|---|---|---|
| `claude` | Agent SDK / first-party CLI | A bounded live response reported usage and independently included `claude-fable-5` in `modelUsage`. Cancellation, tool-event fidelity, working-directory control, and rate-limit behavior remain unavailable. |
| `codex` | Direct OpenAI API with CLI JSON fallback | `OPENAI_API_KEY` completed a bounded response and independently resolved `gpt-5.5-2026-04-23` for requested `gpt-5.5`; ChatGPT subscription status is not inferred. |
| `qwen` | Anthropic-compatible Token Plan endpoint | The `.env` key and endpoint are wired provider-scoped. A bounded live response reported usage and independently resolved `qwen3.8-max-preview` on 2026-07-24. |
| `kimi` | Kimi Code subscription API via the explicit external credential source | `KIMI_CODE_API_KEY` completed a bounded response against the dedicated coding endpoint and independently reported `k3`. |
| `zai` | Direct API via the explicit external credential source | A tool-disabled live JSON probe succeeded and independently reported `glm-5.2` in `modelUsage`. |
| `deepseek` | MMH/Claude-compatible adapter via the explicit external credential source | A tool-disabled live JSON probe succeeded and independently reported `deepseek-v4-pro` in `modelUsage`. |

## Security and release boundary

The matrix records demonstrated behavior, not intended behavior. A provider
with an unavailable or blocked required surface must fail closed and cannot be
selected as a silent fallback. No secret value is retained in this document or
in the matrix. Model aliases are not treated as proof of resolved model identity
when that identity is unattestable from provider output.

## Primary references

- Anthropic Claude Code setup: <https://docs.anthropic.com/en/docs/claude-code/getting-started>
- OpenAI API/ChatGPT billing separation: <https://help.openai.com/en/articles/8156019-how-can-i-move-my-chatgpt-subscription-to-the-api>
- Qwen Code Token Plan authentication: <https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/>
- Kimi Code subscription API: <https://www.kimi.com/code/docs/en/>
- Z.ai terms of use: <https://docs.z.ai/legal-agreement/terms-of-use>
- DeepSeek terms of use: <https://cdn.deepseek.com/policies/en-US/deepseek-terms-of-use.html>
