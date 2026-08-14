import { cpSync, existsSync, rmSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const wrapperRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const canonicalRoot = resolve(wrapperRoot, '..', 'Portal', 'fusion_portal');
const canonicalDist = resolve(canonicalRoot, 'dist');
const wrapperDist = resolve(wrapperRoot, 'dist');
const npmCommand = process.platform === 'win32' ? 'npm.cmd' : 'npm';

function runNpm(args) {
  const command = process.platform === 'win32' ? (process.env.ComSpec || 'cmd.exe') : npmCommand;
  const commandArgs = process.platform === 'win32' ? ['/d', '/s', '/c', npmCommand, ...args] : args;
  const result = spawnSync(command, commandArgs, { cwd: canonicalRoot, stdio: 'inherit' });
  if (result.error) console.error(result.error);
  if (result.status !== 0) process.exit(result.status || 1);
}

if (!existsSync(resolve(canonicalRoot, 'package.json'))) {
  throw new Error(`Canonical portal not found at ${canonicalRoot}`);
}

runNpm(['ci']);
runNpm(['run', 'build']);
rmSync(wrapperDist, { recursive: true, force: true });
cpSync(canonicalDist, wrapperDist, { recursive: true });
console.log(`Render compatibility build copied ${canonicalDist} to ${wrapperDist}`);