# Attribution

The contents of this `playbook/` directory (`skills/`, `plays/`, `templates/finding.md`) are
sourced from the **OWASP Secure Agent Playbook**:

- Project: https://github.com/OWASP/secure-agent-playbook
- License: Creative Commons Attribution 4.0 International (CC-BY-4.0) — see `LICENSE.md` in this folder.
- Only a subset of the original project was copied here: the `agent-security-audit`,
  `prompt-injection-test`, and `mcp-server-review` skills and their corresponding plays,
  plus the shared `finding.md` output template.

These files are used, unmodified, as the security-review procedure fed to Claude by
`auditoria.py` in the parent directory. No code was imported from the original project —
it ships markdown procedures only, not a library.
