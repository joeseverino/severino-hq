import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { ApiError, createClient, OPERATIONS } from './generated/hq.ts';

const origin = 'https://example.com';
const reply = (value, status = 200) => Response.json(value, { status });
function mocked(value, status = 200) {
  const calls = [];
  const client = createClient(OPERATIONS, {
    serverUrl: origin, auth: { bearer: 'example-token' },
    fetch: async (url, init) => {
      calls.push({ url: String(url), init });
      return reply(value, status);
    },
  });
  return { client, calls };
}

test('filters are separate URL fields and bearer is sent', async () => {
  const body = { ok: true, data: { items: [], count: 0, filters: { query: 'a & b' } } };
  const { client, calls } = mocked(body);
  assert.deepEqual(await client.listProjectsV2({ query: { limit: 3, status: 'active', query: 'a & b' } }), body);
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, '/api/v2/resources/projects/');
  assert.deepEqual([...url.searchParams], [['limit', '3'], ['status', 'active'], ['query', 'a & b']]);
  assert.equal(new Headers(calls[0].init.headers).get('authorization'), 'Bearer example-token');
});

test('identifiers are escaped as one path segment', async () => {
  const { client, calls } = mocked({ ok: true, data: {} });
  await client.getProjectsV2({ path: { identifier: 'a/b ?#%' } });
  assert.equal(new URL(calls[0].url).pathname, '/api/v2/resources/projects/a%2Fb%20%3F%23%25/');
});

test('writes keep the envelope and explicit idempotency header', async () => {
  const held = { ok: true, data: { status: 'awaiting_approval', approval: { id: 42 } } };
  const { client, calls } = mocked(held);
  const body = { target: 'example', payload: { name: 'Example' }, expected_updated_at: '2026-10-04T00:00:00Z' };
  assert.deepEqual(await client.projectUpdateV2({ body, headers: { 'Idempotency-Key': 'example-retry-1' } }), held);
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(new Headers(calls[0].init.headers).get('idempotency-key'), 'example-retry-1');
  assert.deepEqual(JSON.parse(calls[0].init.body), body);
});

test('one automatic write key survives a retry', async () => {
  const keys = [];
  const client = createClient(OPERATIONS, {
    serverUrl: origin, idempotencyKey: true,
    retry: { retries: 1, retryDelay: 0, jitter: false },
    fetch: async (_url, init) => {
      keys.push(new Headers(init.headers).get('idempotency-key'));
      return keys.length === 1 ? reply({ ok: false }, 503) : reply({ ok: true, data: {} });
    },
  });
  await client.projectUpdateV2({ body: { target: 'example', payload: { name: 'Example' } } });
  assert.equal(keys.length, 2);
  assert.match(keys[0], /^[0-9a-f-]{36}$/);
  assert.equal(keys[0], keys[1]);
});

test('HTTP refusals retain status and structured error', async () => {
  const body = { ok: false, error: { code: 'forbidden', message: 'Denied.' } };
  const { client } = mocked(body, 403);
  await assert.rejects(client.listProjectsV2(), error => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 403);
    assert.deepEqual(error.body, body);
    return true;
  });
});

const launcher = fileURLToPath(new URL('./hq.mjs', import.meta.url));
const noNetwork = fileURLToPath(new URL('./test-no-network.mjs', import.meta.url));
function cli(args) {
  const result = spawnSync(process.execPath, ['--import', noNetwork, launcher, ...args], {
    encoding: 'utf8', env: { ...process.env, HQ_API_URL: origin, HQ_API_TOKEN: 'example-secret-token' },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.ok(!result.stdout.includes('example-secret-token'), 'dry run leaked bearer token');
  assert.ok(!result.stderr.includes('example-secret-token'), 'stderr leaked bearer token');
  return result.stdout;
}

test('CLI help and filtered dry run never use network', () => {
  assert.match(cli(['--help']), /Commands:/);
  const request = JSON.parse(cli(['listProjectsV2', '--limit', '3', '--query', 'a & b', '--dry-run']));
  assert.equal(new URL(request.url).searchParams.get('query'), 'a & b');
  assert.equal(new URL(request.url).searchParams.get('limit'), '3');
  assert.equal(request.headers.Authorization, '***');
});

test('CLI writes support automatic and explicit retry keys without leaking token', () => {
  const body = { target: 'example', payload: { name: 'Example' } };
  const args = ['projectUpdateV2', '--json', JSON.stringify(body), '--dry-run'];
  const automatic = JSON.parse(cli(args));
  assert.match(automatic.headers['Idempotency-Key'], /^[0-9a-f-]{36}$/);
  const explicit = JSON.parse(cli([...args, '--idempotency-key', 'example-retry-2']));
  assert.equal(explicit.headers['Idempotency-Key'], 'example-retry-2');
  assert.equal(explicit.headers.Authorization, '***');
  assert.deepEqual(JSON.parse(explicit.body), body);
});
