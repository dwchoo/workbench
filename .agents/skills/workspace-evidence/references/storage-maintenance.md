# Local workflow storage maintenance

Generated runtime homes and graph outputs are not checkpoint inputs. Pass
`exclude: [".workflow", "graphify-out"]` for source recovery snapshots. If specific
operational evidence must be observed, select its individual files separately.
The shared helper rejects checkpoints with more than 32 MiB of unique recovery
bytes. It never silently omits a file to meet this limit.

Check `du -sh .workflow graphify-out` before a fresh isolated run. Repository
guidance sets a 10 GiB operational threshold for `.workflow`: clean stopped
regenerable caches before starting another run, and report remaining oversized
items if cleanup is insufficient. This is owner guidance, not an automatic runner
limit or a product budget change. Check size again after success, failure, or
interruption. Keep compact manifests for checks; reuse the same unchanged
candidate's recovery checkpoint instead of creating another byte copy each step.

Run from the repository root after an owned fixture has stopped:

```bash
python3 .agents/skills/workspace-evidence/scripts/storage_maintenance.py
python3 .agents/skills/workspace-evidence/scripts/storage_maintenance.py --apply
```

The first command reports candidates. The second removes only regenerable native
libraries below fixture `private/**/.omp/natives` and model cache databases below
fixture `private/**/agent` or `private/**/.omp`. It preserves configuration, auth,
reports, traces, journal databases, and result databases. It checks same-UID
process command lines, open descriptors, mapped files, and working directories,
and skips resources that are in use. Inaccessible workload processes block the
operation; protected login/session daemons are excluded from this owner check.
Run cleanup serially, with no new fixture starting concurrently.

Old, unneeded large recovery copies may be archived on an explicit cleanup:

```bash
python3 .agents/skills/workspace-evidence/scripts/storage_maintenance.py --apply --archive-checkpoints
```

Only JSON files of at least 10 MiB starting with the helper's `blobs` object are
selected. A `.json.gz` replaces each selected original only after decompression
verifies its original SHA-256. Original/archive hashes and paths are recorded in
`.workflow/storage-maintenance/<timestamp>/actions.jsonl`. Existing reports and
references are not rewritten: an archived path is historical evidence, not a
missing or failed result. Restore a referenced checkpoint before a consumer that
requires its original path reads it:

```bash
python3 .agents/skills/workspace-evidence/scripts/storage_maintenance.py --apply --restore .workflow/path/to/checkpoint.json
```

Restore verifies both archive and original hashes and refuses to overwrite an
existing path. Avoid restoring old full-workspace snapshots merely for inspection;
prefer their small result records. Archives can retain old generated blobs, but
new checkpoints must exclude generated outputs.
