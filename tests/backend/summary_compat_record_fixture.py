"""Synthetic validator evidence only; never an installed-runtime pass claim.

Raw controls are explicit fixtures. Product projection supplies the serialized
row shape; independent tests check that stored claims remain bound to these raw
observations and private evidence, rather than accepting the projector as oracle.
"""
from copy import deepcopy

from workbench.backend.summary_compat_provider import item_rows


def synthetic_asset_binding(binary_path):
    """Complete validator-only binding, not an observed installed identity."""
    return {"binary_path": str(binary_path),
            "asset_selection": {"PI_PACKAGE_DIR": None, "OMP_NATIVE_LIBRARY_PATH": None}}


def complete_record(record):
    preflight = [{"event": "dispatch_preflight", "outputCap": 4096, "fittedCap": 4096,
                  "conservativeWireBytes": 12000, "mandatoryWireBytes": 8000}]
    normal = {"outcome": {"status": "summary_result"}, "events": preflight,
              "childDisposed": True, "parentUnchanged": True, "receiptBytes": 200}
    result_checks = dict.fromkeys((
        "wrong_id_rejected", "wrong_revision_rejected", "wrong_range_rejected",
        "wrong_id_receipt_absent", "wrong_revision_receipt_absent", "wrong_range_receipt_absent",
        "duplicate_receipt_rejected", "duplicate_receipt_unchanged", "task_spec_unchanged"), True)
    completed = {
        "parentUnchanged": True, "registryUnchanged": True, "settingsUnchanged": True,
        "childDisposed": True, "toolCalls": 1, "events": preflight,
        "exercised": ["Q01-route", "Q01-tools", "Q01-skill", "Q01-result", "Q01-context", "Q01-origin"],
        "outcome": {"status": "summary_result", "receipt": {"fixture": "private-receipt"},
                    "result": {"sources": [{"fixture": "bound-source"}]}},
        "writerObserved": {"request_binding_matches": True, "receipt_matches": True,
            "catalog_entries": 1, "actual_body": {"schemaVersion": 1,
                "facts": ["소유 private scripted fixture의 연결을 확인했다."],
                "interpretations": [], "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]}},
        "caseHost": {"result": {"checks": result_checks},
                     "origin": {"checks": {"task_command_source_authorized": True}}},
        "caseControls": [
            {"name": "worker", "outcome": {"sent": False}, "childCreates": 0},
            *[{"name": name, "outcome": {"sent": False}, "childCreates": 2,
               "childDisposed": True, "disposalBeforeRebuild": True}
              for name in ("api_invalid", "stale_invalid")],
            {"name": "roster_rebuild", "outcome": {"status": "summary_result"},
             "childCreates": 2, "childDisposed": True, "disposalBeforeRebuild": True},
            {"name": "saved_then_bash", "outcome": {"protected": True, "status": "rejected"},
             "receiptAbsent": True, "writerCalls": 0, "writerEntries": 0},
            {"name": "normal2", **deepcopy(normal)}, {"name": "normal3", **deepcopy(normal)}],
    }
    c1 = {"kind": "owned_C1_partial", "cleanup_complete": True, "completed": completed,
          "localhost_model_posts": [
              {"case": case, "tools": ["db_summary_save"], "system_exact": True}
              for case in ("normal", "roster_rebuild", "roster_rebuild", "saved_then_bash", "saved_then_bash")],
          "recovery_control": {"unknown_before_response": True, "replay_reserve_refused": True,
              "replay_row_present": False, "duplicate_receipt_accepted": False,
              "receipt_unchanged": True, "owner_predicate_false_control": {"noSendConfirmed": True}},
          "restart_control": {"completed_artifact_absent": True, "replay_reserve_refused": True,
              "replay_row_present": False, "final_replay_row_absent": True, "fresh_owned_distinct_input": True}}
    priority = {"kind": "owned_product_priority_partial", "native_launches": 2,
        "coverage_complete": True, "cleanup_complete": True, "errors": [],
        "parents": {role: {"strict_parent_unchanged": True, "raw_parent_contamination": False,
                           "prior_entry_views_unchanged": True} for role in ("manager", "worker")},
        "measurement": {"session_ids": {"manager": "m", "worker": "w", "child": "c"},
                        "notice_windows": [{"turn_hold_overlap_ms": 10}, {"turn_hold_overlap_ms": 20}],
                        "due_scheduler_status": "dispatched"},
        "Core_periodic_question": {"returned_before_child_release": True},
        "busy_parent_during_summary": {"state": {"idle": False}, "child_still_held": True},
        "draft_ack": {"status": "deferred"}, "draft": {"Enter_sent": False},
        "scheduler": {"tick60": "dispatched"}, "end_ack": {"status": "api_accepted"},
        "prepared": {"whole_range_identity_preserved": True},
        "producer": {"authority": {"allowed": True}}, "writer": {"sources_matches": True}}
    configured = {"kind": "owned_configured_metadata", "result": "pass", "cleanup_complete": True,
                  "checks": {"same_peer_epoch_readbacks": True},
                  "metadata": deepcopy(record["model_metadata"]), "omp": deepcopy(record["omp"])}
    record["observations"] = [c1, priority, configured]
    record["item_rows"] = item_rows(record["observations"])
    record["caveats"] = ["observer_child_io_unattributed", "priority_operating_io_unattributed"]
    return record


def bind_metadata(record):
    """Keep deliberately varied metadata fixtures paired with their readback.

    Never repair rows, refs, unknowns or safety observations during persistence.
    """
    for row in record.get("observations", []):
        if row.get("kind") == "owned_configured_metadata":
            # Pair before serialization without recursively copying: the
            # existing bounded deep-metadata negative must reach the consumer.
            row["metadata"] = record.get("model_metadata")
