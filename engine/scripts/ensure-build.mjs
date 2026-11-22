import { access } from 'node:fs/promises';
import { spawn } from 'node:child_process';

const requiredBuildFiles = [
  'dist/src/account-transfer.js',
  'dist/vendor/lib/storage/load-save.js',
];

try {
  await Promise.all(requiredBuildFiles.map(file => access(file)));
} catch {
  const isWindows = process.platform === 'win32';
  const npm = isWindows ? 'npm.cmd' : 'npm';
  const command = isWindows ? (process.env.ComSpec ?? 'cmd.exe') : npm;
  const args = isWindows
    ? ['/d', '/s', '/c', `${npm} run build`]
    : ['run', 'build'];
  await new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: 'inherit',
    });
    child.once('error', reject);
    child.once('exit', code => {
      if (code === 0) resolve();
      else reject(new Error(`Build failed with exit code ${code ?? 'unknown'}`));
    });
  });
}
