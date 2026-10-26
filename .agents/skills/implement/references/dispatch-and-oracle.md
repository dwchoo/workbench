# Dispatch / Oracle evidence packet

Handoff fields, not Codex tool arguments.
Keep immutable packets with existing attempt evidence; no separate routing ledger.

## Assignment
- feature_id, ticket_id, attempt_id, invocation, approved_bundle_digest
- actual_workspace (absolute worktree path when Root created one), repository_identity, baseline_snapshot, effective_write_capability
- acceptance_ids, relevant_contracts, allowed_write_paths, prohibited_paths
- method_reference: scope/receipt-resolved absolute tdd path and file identity; seam_refs,
  behavior and independent expected-value source, authorized test_paths/production_paths
- exact_checks include RED/GREEN commands; agreed seam or Root-return condition; justified
  TDD exception when needed. Missing reference/harness is a reported gap, not blanket exemption.
- generated_outputs, resource_claims, mutation_owner, checkpoint_ref
- exact_checks, reserved_budget, stop_conditions; review_kind (`general` or `security`) for reviewers
- feasibility assumption/experiment/result reference when applicable
- configured_model/effort; host_observed_model/effort or unverified, with metadata reference
- routing: planned_role (original recommendation, or null), selected_role (current dispatch ID),
  reason_codes, evidence_refs. Preserve legacy aliases in planned_role.
- prior_assignment_ref on reassignment; reference previous result and stop evidence

Attempt and invocation are distinct identities. Existing bundle/run evidence identifies the
instruction bytes used. Root compares actual role, pins, workspace and budget with the packet,
reserves the same invocation/role, and links the assignment in starting transition evidence.
Failed reserve forbids dispatch. Starting is not proof of a successful start; host observations
support subsequent transitions. Unknown starts retain capacity. Reconcile legacy results
lacking invocation against host evidence; never invent connections.

## Result
- invocation, ticket_id, candidate_id, scope inspected and limits
- status by role (task outcome, not journal state); Root's handling in parentheses:
  worker: candidate_ready (verify) / needs_reroute (reassess) / needs_oracle / blocked;
  reviewer: no_material_findings (pass) / findings (adjudicate) / needs_oracle / blocked;
  test_designer: tests_ready (review) / coverage_sufficient (review) / implementation_defect
  (Root adjudication under contract section 7, then accepted fix) / needs_oracle / blocked
- actual_workspace, base_snapshot, candidate_id, attributable_output_bundle
- changed_paths, scope_audit, checks with command/cwd/exit_code/result/evidence
- findings: supported premises, consequence, invariant/evidence; distinguish uncertainty and suggestions
- active_or_stopped_subprocesses, remaining_gaps
- for blockers: cause/evidence, direct ticket and acceptance/gate impact, partial delta,
  needed decision or prerequisite and resume condition; reference an existing issue when known
- tdd_evidence: behavior/seam, RED command/cause/then-candidate, minimal implementation delta,
  GREEN command/candidate; or exception behavior/reason/alternative check/uncertainty.
  Missing chronology is unverified; post-hoc sensitivity is not RED-first.
- gate_result_refs, original_review_ref, delta_review_scope and affected review_axis when applicable
- for needs_reroute: requested_role, reason_code, remaining_problem, evidence_refs

## Accounting and gates

Use journal v1 [formats](../../workflow-ledger/references/formats.md) and contract section 8.
All assignment/result fields above are packet content, not spawn arguments or new journal keys. No routing fields or task statuses enter the journal schema. Transition evidence links packets
and host observations; Root checks packet invocation/selected_role against the journal call.
A result status does not establish process termination or ticket integration.
Required gate results retain PLAN item IDs, candidate identity, evidence level,
command/cwd/environment/exit_code and unknowns. Fixture/schema success is not live evidence.

## Oracle request (Root-routed)
- incident_id, ticket_id, spec_revision, candidate_id
- expected_behavior, observed_behavior
- reproducer_and_logs (or explicit missing-evidence statement)
- attempted_approaches and observable outcomes
- constraints, unknowns, precise_question

## Oracle response
- supported_observations, candidate_hypotheses, uncertainty
- discriminating_experiments, minimal_fix_direction
- regression_test_ideas, integration_risks, plan_invalidated, remaining_unknowns
