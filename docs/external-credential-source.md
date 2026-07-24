# External credential source

TORQ CLI can reuse the provider credentials already maintained for TORQ
Console. The source must be passed explicitly; TORQ performs no home-directory
or repository-wide secret discovery.

```powershell
torq auth status --credential-file E:\TORQ-CONSOLE\.env
torq setup `
  --config .torq\config.yaml `
  --answers examples\torq-v5-6-live.answers.json `
  --credential-file E:\TORQ-CONSOLE\.env
```

The parser is UTF-8, bounded to 64 KiB, rejects duplicate or malformed keys,
and refuses relative paths, symlinks, and non-regular files. Values remain in
memory and are neither printed nor written to generated configuration.

## Provider mapping

| TORQ provider | Source key | Claude-compatible child variables |
| --- | --- | --- |
| DeepSeek Builder | `DEEPSEEK_API_KEY` | token + `https://api.deepseek.com/anthropic` + `deepseek-v4-pro` |
| OpenAI / Codex | `OPENAI_API_KEY` | API credential + `gpt-5.5`; ChatGPT subscription billing is separate |
| Kimi Refine Bug | `KIMI_CODE_API_KEY`, fallback `KIMI_API_KEY` | subscription token + `https://api.kimi.com/coding/` + `k3` |
| Z.ai Refine UI | `GLM_API_KEY`, fallback `ZAI_API_KEY` | token + `https://api.z.ai/api/anthropic`; model flag `glm-5.2` |
| Qwen challenge lane | `QWEN_TOKEN_PLAN_API_KEY` + `QWEN_TOKEN_PLAN_BASE_URL` | Token Plan Anthropic-compatible endpoint + live-attested `qwen3.8-max-preview` |

Claude retains its first-party authenticated session. Codex can use the
explicit `OPENAI_API_KEY`; this is an API credential and is not treated as
proof of ChatGPT subscription entitlement. Qwen replaces the former Grok
challenge lane and receives only its Token Plan key and configured base URL.

Each direct-provider child receives only the selected credential plus the
small safe operating-system environment allowlist. Credentials for the other
providers are excluded.
