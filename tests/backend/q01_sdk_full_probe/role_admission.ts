/** Probe-local owner admission for the approved existing task@Manager route. */
export function admitSummaryRole(role: unknown, agentName: unknown): { admitted: boolean; reason: string } {
  if (agentName !== "task") return { admitted: false, reason: "agent_not_allowed" };
  if (role !== "Manager") return { admitted: false, reason: "role_not_allowed" };
  return { admitted: true, reason: "approved_task_manager" };
}
