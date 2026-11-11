// Replays the wire-parity calls through the generated TypeScript client:
// node wire_parity.ts <index.ts> <baseUrl> <calls.json>
import { readFileSync } from "node:fs";

const { Client, ValidationError } = await import(process.argv[2]);
const client = new Client({ baseUrl: process.argv[3], maxRetries: 0 });
const calls: { op: string; args: Record<string, unknown> }[] = JSON.parse(readFileSync(process.argv[4], "utf-8"));
const results: unknown[] = [];
for (const call of calls) {
  try {
    results.push({ ok: await client[call.op](call.args) });
  } catch (error) {
    if (!(error instanceof ValidationError)) throw error;
    results.push({ invalid: error.path });
  }
}
console.log(JSON.stringify(results));
