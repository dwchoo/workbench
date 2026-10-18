# Security review

Read only when the candidate diff hits a contract section 7 security trigger. This is a
separate step from the general review, uses `reviewer` (a leaf, read-only) and adds no new
persona. A pass is hypothesis-level evidence, never a proof of safety.

## Threat packet (Root writes it)

Assets and trust boundaries touched; input sources and who controls them; attacker
capabilities assumed; the frozen diff including new files and the candidate ID; existing
mitigations and known unknowns; any new dependency with name, version and source.

## Reviewer invocation

Dispatch a fresh `reviewer` by name with `review_kind: security` and the packet, recorded as its
own call in the ledger (it consumes budget). Ask for status `no_material_findings`, `findings`,
`needs_oracle` or `blocked`; each finding needs severity, confidence, file/symbol, attack
scenario, reproduction when safe, and a fix direction. Root adjudicates, routes accepted fixes
to a writer, and reviews only the delta afterwards. Optional hardening never blocks unless tied
to an approved criterion.

## Deterministic checks (recorded with `scripts/verify.py check`)

- Secret scan of the diff and new files: use the project's tool when it has one (for example
  gitleaks); otherwise report the missing tool. No tool means `not_run`, not a pass.
- Dependency audit when a lockfile or manifest changed: run the project's audit command as
  argv (for example `npm audit`, `pip-audit`, `cargo audit`). A missing tool, blocked network
  or timeout is `not_run` with an unknown; never assume it passed.
- Never install tools automatically. Record command, cwd, candidate, exit code and result.

## Effect on completion

An unresolved High-or-above finding, or a required security check left `not_run`, prevents
`done_verified` (contract section 9). Report the gap and the unverified scope instead.
