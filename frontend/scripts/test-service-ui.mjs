import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const reservation = createServer();
await new Promise((done) => reservation.listen(0, '127.0.0.1', done));
const port = reservation.address().port;
await new Promise((done) => reservation.close(done));
const base = `http://127.0.0.1:${port}`, owner = randomUUID();
const live = process.argv.includes('--live');
const env = { ...process.env, MYBOT_SERVICE_OWNER_TOKEN: owner, PYTHONIOENCODING: 'utf-8' };
const child = spawn(process.env.MYBOT_PYTHON || 'python', ['-B', 'tests/server/ui_fixture.py', '--port', String(port), ...(live ? ['--live'] : [])], { cwd: resolve(frontend, '..'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
let output = ''; child.stdout.on('data', (chunk) => { output = (output + chunk).slice(-16000); }); child.stderr.on('data', (chunk) => { output = (output + chunk).slice(-16000); });
const wait = (ms) => new Promise((done) => setTimeout(done, ms));
try {
  const deadline = Date.now() + 45_000; let ready = false;
  while (Date.now() < deadline && child.exitCode === null) {
    try { ready = (await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(1000) })).ok; } catch {}
    if (ready) break; await wait(200);
  }
  if (!ready) throw new Error('Isolated API did not start: ' + output);
  const test = spawn(process.execPath, process.argv.includes('--desktop') ? ['scripts/verify-service-conversation.mjs'] : ['node_modules/@playwright/test/cli.js', 'test', 'e2e/service.spec.ts', '--workers=1'], {
    cwd: frontend, env: { ...process.env, MYBOT_E2E_API_URL: base, MYBOT_E2E_LIVE: live ? '1' : '0' }, stdio: 'inherit', windowsHide: true,
  });
  const code = await new Promise((done, fail) => { test.once('exit', done); test.once('error', fail); });
  if (code !== 0) { console.error(output); process.exitCode = code ?? 1; }
} finally {
  try { await fetch(`${base}/api/service/shutdown`, { method: 'POST', headers: { 'X-Mybot-Client': 'mybot-desktop', 'X-Mybot-Owner': owner }, signal: AbortSignal.timeout(5000) }); } catch {}
  const deadline = Date.now() + 15_000;
  while (child.exitCode === null && Date.now() < deadline) await wait(100);
  if (child.exitCode === null) { child.kill(); console.error('Fixture did not stop normally; inspect the temporary test database.'); process.exitCode = 1; }
  else if (child.exitCode !== 0) { console.error(output); process.exitCode = 1; }
}
