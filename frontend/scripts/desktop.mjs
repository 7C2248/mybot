import { existsSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { delimiter, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const localEnvironment = join(frontend, '.env.desktop.local');
if (existsSync(localEnvironment)) process.loadEnvFile(localEnvironment);

// Custom Rust installations can be configured per checkout, without changing
// the user's global PATH or embedding machine-specific paths in tracked code.
const environment = { ...process.env };
if (environment.CARGO_HOME) {
  const pathKey = Object.keys(environment).find((key) => key.toLowerCase() === 'path') ?? 'PATH';
  environment[pathKey] = `${join(environment.CARGO_HOME, 'bin')}${delimiter}${environment[pathKey] ?? ''}`;
}
const require = createRequire(import.meta.url);
const cli = require.resolve('@tauri-apps/cli/tauri.js');
const child = spawn(process.execPath, [cli, ...process.argv.slice(2)], {
  cwd: frontend, env: environment, stdio: 'inherit', windowsHide: true,
});
child.on('error', (error) => { console.error(error.message); process.exitCode = 1; });
child.on('exit', (code) => { process.exitCode = code ?? 1; });
