import os
import subprocess
from pathlib import Path


def git(root, *args, raw=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    p = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                        '-c', 'user.name=Codex DPH', '-c', 'user.email=codex-dph@localhost',
                        *args], cwd=root, env=env, capture_output=True, timeout=60)
    if p.returncode:
        raise ValueError(p.stderr.decode('utf-8',errors='replace').strip())
    if raw:
        return p.stdout
    output=p.stdout.decode('utf-8')
    return output if '-z' in args else output.strip()


def clean(root):
    return not git(root, 'status', '--porcelain', '--untracked-files=all')


def changed_files(root, base):
    raw = git(root, 'diff', '--name-only', '-z', '--no-renames', base)
    return raw.split('\0')[:-1] if raw else []


def in_scope(name, patterns):
    from pathlib import PurePosixPath
    return any(p == '**' or name == p or PurePosixPath(name).match(p) or
               (p.endswith('/**') and name.startswith(p[:-2])) for p in patterns)


def snapshot(root, base, allowed):
    # Stage intent-to-add so newly created files are included in validation/diff.
    git(root, 'add', '-N', '--', '.')
    files = changed_files(root, base)
    for name in files:
        if not in_scope(name, allowed):
            raise ValueError(f'Out-of-scope change: {name}')
        if any(part in {'.git', '.env', '.ssh'} for part in Path(name).parts):
            raise ValueError(f'Sensitive path: {name}')
        path = Path(root) / name
        if path.is_symlink():
            raise ValueError(f'Symlink delivery is unsupported: {name}')
        if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError(f'File exceeds 5 MiB: {name}')
    git(root, 'add', '--all', '--', '.')
    git(root, 'commit', '--allow-empty', '-qm', 'Codex DPH delivery')
    return git(root, 'rev-parse', 'HEAD'), files
