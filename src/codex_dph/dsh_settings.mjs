// Read model settings afresh for each task; let Harness resolve its own credentials.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';

const plugins = new Map([
  ['agent-default-model', '@deepseek-ai/dsh-agent-default-model'],
  ['llm-pi-ai', '@deepseek-ai/dsh-llm-pi-ai'],
  ['llm-deepseek', '@deepseek-ai/dsh-llm-deepseek-api-key'],
]);
function rejectSecrets(value) {
  if (!value || typeof value !== 'object') return;
  for (const [key, item] of Object.entries(value)) {
    if (/^(api[-_]?key|token|access[-_]?token|secret|password|authorization|headers|httpHeaders)$/i.test(key)) {
      throw new Error('literal credential settings are unsupported; use the Harness credential store');
    }
    rejectSecrets(item);
  }
}
let temporary;
try {
  const [binary, ...args] = process.argv.slice(2);
  const sourceHome = path.resolve(process.env.CODEX_DPH_DSH_CONFIG_HOME || path.join(os.homedir(), '.dsh'));
  const profile = process.env.CODEX_DPH_DSH_SETTINGS_PROFILE;
  if (!profile || !/^[a-zA-Z0-9_-]+$/.test(profile)) throw new Error('Invalid settings profile');
  const runtimeHome = process.env.DSH_HOME;
  if (!runtimeHome || path.resolve(runtimeHome) === sourceHome) throw new Error('A separate DSH_HOME is required for task state');
  fs.mkdirSync(runtimeHome, {recursive:true, mode:0o700});
  if (fs.realpathSync(runtimeHome) === fs.realpathSync(sourceHome)) throw new Error('A separate DSH_HOME is required for task state');
  let yaml;
  for (const anchor of [fileURLToPath(new URL('../../.tools/package.json', import.meta.url)), fs.realpathSync(binary)]) {
    try { yaml = createRequire(anchor)('yaml'); break; } catch {}
  }
  if (!yaml) throw new Error('DSH YAML runtime is unavailable');
  const entries = new Map();
  for (const file of [path.join(sourceHome, 'profiles', profile, 'cordis.patch.yml'), path.join(sourceHome, 'cordis.patch.yml')]) {
    if (!fs.existsSync(file)) continue;
    let rows;
    try {
      const doc = yaml.parseDocument(fs.readFileSync(file, 'utf8'));
      if (doc.errors.length || doc.warnings.length) throw new Error('Unsupported YAML');
      rows = doc.toJS();
    }
    catch { throw new Error('Cannot parse Harness settings; no credential details displayed'); }
    if (!Array.isArray(rows)) throw new Error('Unsupported Harness settings format');
    for (const row of rows) {
      if (!row || typeof row !== 'object' || !row.id) throw new Error('Unsupported Harness patch operation');
      if (!plugins.has(row.id)) continue;
      if (row.name && row.name !== plugins.get(row.id)) throw new Error('Unsupported model plugin');
      if (!row.config || typeof row.config !== 'object' || row.disabled) throw new Error('Unsupported model settings entry');
      rejectSecrets(row.config);
      // Harness patches replace config wholesale; do not retain stale endpoints.
      entries.set(row.id, {id: row.id, config: row.config});
    }
  }
  const selection = entries.get('agent-default-model')?.config;
  if (!selection?.provider || !selection?.model) throw new Error('Harness settings must specify a default model; refusing fallback');
  // Shipped headless disables the file store; explicitly enable it for reuse.
  entries.set('credentials', {id:'credentials', disabled:false, config:{path:path.join(sourceHome, '.credentials.yaml')}});
  fs.mkdirSync(runtimeHome, {recursive:true, mode:0o700});
  temporary = fs.mkdtempSync(path.join(runtimeHome, 'model-settings-'));
  const overlay = path.join(temporary, 'overlay.json');
  fs.writeFileSync(overlay, JSON.stringify([...entries.values()]), {mode:0o600});
  const env = {...process.env};
  // Do not use a stale environment key in place of the selected provider's store.
  delete env.DEEPSEEK_API_KEY;
  const child = spawn(binary, ['--patch', overlay, ...args], {env, stdio:'inherit'});
  const handlers = new Map();
  for (const signal of ['SIGTERM', 'SIGINT']) {
    const handler = () => child.kill(signal);
    handlers.set(signal, handler); process.on(signal, handler);
  }
  const code = await new Promise((resolve, reject) => {
    child.once('error', () => reject(new Error('Unable to start Harness')));
    child.once('exit', (status) => resolve(status ?? 1));
  });
  for (const [signal, handler] of handlers) process.off(signal, handler);
  process.exitCode = code;
} catch (error) {
  console.error('codex-dph: ' + error.message);
  process.exitCode = 1;
} finally {
  if (temporary) fs.rmSync(temporary, {recursive:true, force:true});
}
