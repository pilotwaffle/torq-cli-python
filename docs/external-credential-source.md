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
| Kimi Refine Bug | `KIMI_CODE_API_KEY`, fallback `KIMI_API_KEY` | token + `https://api.moonshot.ai/anthropic/` + `kimi-k3` |
| Z.ai Refine UI | `GLM_API_KEY`, fallback `ZAI_API_KEY` | token + `https://api.z.ai/api/anthropic`; model flag `glm-5.2` |

Claude and Codex retain their first-party authenticated sessions. The source
also recognizes `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` for status reporting,
without injecting them into the three direct-provider lanes. Grok remains an
optional external lane and recognizes `XAI_API_KEY` or `GROK_API_KEY` when one
is present.

Each direct-provider child receives only the selected credential plus the
small safe operating-system environment allowlist. Credentials for the other
providers are excluded.
