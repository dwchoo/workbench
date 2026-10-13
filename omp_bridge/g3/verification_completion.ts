/** Publish only complete JSON inside the already verified private root. */
import { renameSync, writeFileSync } from "node:fs";

export function publishVerificationCompletion(root: string, value: unknown, kind: "ok" | "err"): void {
  const json = JSON.stringify(value);
  if (typeof json !== "string") throw Error("completion_serialization_unavailable");
  // Separate names let an error be published even after a failed success write.
  const temporary = root + "/.completed." + kind + ".tmp";
  writeFileSync(temporary, json, { flag: "wx", mode: 0o600 });
  renameSync(temporary, root + "/completed.json");
}
