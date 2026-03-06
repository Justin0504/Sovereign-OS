# Security

## Reporting a vulnerability

If you discover a security issue, please report it responsibly:

- **Do not** open a public GitHub issue for security vulnerabilities.
- Email the maintainers (or open a private security advisory on GitHub if the repo supports it) with a description of the issue and steps to reproduce.
- We will acknowledge receipt and work on a fix; we may ask for more detail.

## Security-related design

- **Secrets:** API keys (OpenAI, Anthropic, Stripe, etc.) are read from environment variables only. Do not commit `.env` or credentials to the repository.
- **Audit trail:** Audit reports use a canonical JSON hash (`proof_hash`) so that tampering can be detected. See [AUDIT_PROOF.md](docs/AUDIT_PROOF.md).
- **Human-in-the-loop:** High-value or high-risk jobs can be kept in `pending` until a human approves them in the Web Dashboard. An optional compliance hook can require approval when estimated spend exceeds a threshold. See [PHASE6.md](docs/PHASE6.md).
- **Permissions:** Agents are gated by `SovereignAuth` and `TrustScore`; capabilities (e.g. `SPEND_USD`) are granted based on audit history.
