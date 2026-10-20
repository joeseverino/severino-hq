#!/usr/bin/env node
// Configure the generated CLI without duplicating its commands or request shapes.
import { run, wiring } from './generated/hq.cli.ts';

const args = process.argv.slice(2);
const flag = args.indexOf('--idempotency-key');
if (flag !== -1) {
  const value = args[flag + 1];
  if (!value || value.startsWith('--')) {
    console.error('--idempotency-key requires a value');
    process.exit(4);
  }
  wiring.configure({ headers: { 'Idempotency-Key': value } });
  args.splice(flag, 2);
}
// The generated CLI resolves credentials here, including dry-run redaction.
wiring.env = { ...process.env, HQ_TOKEN: process.env.HQ_API_TOKEN ?? process.env.HQ_TOKEN };
if (process.env.HQ_API_URL) wiring.configure({ serverUrl: process.env.HQ_API_URL });
process.exit(await run(args));
