import json
import os
import shutil
import subprocess
from pathlib import Path

from .service import Invalid


def capabilities(executable='dsh'):
    local = Path(__file__).resolve().parents[2]/'.tools/node_modules/.bin/dsh'
    binary = str(local) if executable == 'dsh' and local.exists() else shutil.which(executable)
    if not binary:
        return dict(available=False, supported=False, reason='dsh not found')
    # Inspect installed startup grammar without initializing a profile or reading credentials.
    package = Path(binary).resolve().parent.parent
    candidates = [package/'node_modules/@deepseek-ai/dsh-headless/lib/startup.js',
                  package.parent.parent/'@deepseek-ai/dsh-headless/lib/startup.js',
                  package/'node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-headless/lib/startup.js']
    grammar = next((p.read_text() for p in candidates if p.exists()), '')
    version = subprocess.run([binary,'--version'], capture_output=True,text=True,timeout=10).stdout.strip()
    supported = all(flag in grammar for flag in ('--json','--permission-mode','--approval-policy'))
    return dict(available=True, supported=supported, version=version, executable=binary,
                reason=None if supported else 'Installed headless grammar lacks verified JSON/permission/approval options; update or qualify an adapter')


def command():
    status = capabilities()
    if not status['supported']:
        raise Invalid(status['reason'])
    prefix = [status['executable']]
    if os.environ.get('CODEX_DPH_DSH_SETTINGS_PROFILE'):
        node = shutil.which('node')
        if not node:
            raise Invalid('Node.js is required to reuse Harness model settings')
        prefix = [node, str(Path(__file__).with_name('dsh_settings.mjs')), status['executable']]
    return prefix + ['--profile', 'headless', '--json',
            '--permission-mode', 'workspace-write', '--approval-policy', 'never', '-']


def parse_events(path):
    final = None
    session_id = None
    events = []
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get('type'),str):
                raise Invalid('Invalid JSON event')
            events.append(event)
            if event['type'] == 'session':
                session_id = event.get('sessionId')
            if event['type'] == 'final':
                final = event
    if not final or final.get('reason',{}).get('kind') != 'completed':
        raise Invalid('Missing successful final event')
    return events, session_id
