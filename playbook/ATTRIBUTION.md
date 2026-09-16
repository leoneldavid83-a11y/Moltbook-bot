# Attribution

The contents of this `playbook/` directory (`skills/`, `plays/`, `templates/finding.md`) are
sourced from the **OWASP Secure Agent Playbook**:

- Project: https://github.com/OWASP/secure-agent-playbook
- License: Creative Commons Attribution 4.0 International (CC-BY-4.0) — see `LICENSE.md` in this folder.
- Only a subset of the original project was copied here — the full `ai-security-skills`
  set (`agent-security-audit`, `prompt-injection-test`, `mcp-server-review`,
  `llm-risk-assess`, `agentic-ai-risk-assess`, `multi-agentic-threat-model`) and their
  corresponding plays, plus the shared `finding.md` output template. None of the
  `code-security-skills` plugin (mobile/IaC/web/API review, etc.) is included.

These files are used, unmodified, as the security-review procedure fed to Claude by
`auditoria.py` in the parent directory. No code was imported from the original project —
it ships markdown procedures only, not a library.
