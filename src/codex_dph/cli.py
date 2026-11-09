import argparse
import json
import os
import sys
from pathlib import Path

from .adapter import capabilities
from .service import Service, Invalid
from .worker import Worker


def main():
    parser = argparse.ArgumentParser(description='Codex / DeepSeek local task handoff')
    parser.add_argument('--home',default=os.environ.get('CODEX_DPH_HOME',str(Path.home()/'Library/Application Support/codex-dph')))
    subs = parser.add_subparsers(dest='action',required=True)
    subs.add_parser('doctor')
    register = subs.add_parser('register')
    register.add_argument('root')
    register.add_argument('--checks',help='JSON file mapping check IDs to argv arrays')
    configure = subs.add_parser('configure-checks')
    configure.add_argument('project_id')
    configure.add_argument('file')
    subs.add_parser('projects')
    submit = subs.add_parser('submit')
    submit.add_argument('file')
    submit.add_argument('--key',required=True)
    for name in ('status','cancel','artifacts','reconcile','context'):
        p = subs.add_parser(name)
        p.add_argument('run_id')
    subs.add_parser('runs')
    worker = subs.add_parser('worker')
    worker.add_argument('--once',action='store_true')
    subs.add_parser('serve')
    args = parser.parse_args()
    try:
        if args.action == 'doctor':
            result = capabilities()
        else:
            s = Service(args.home)
            if args.action == 'register':
                result = s.register(args.root,json.loads(Path(args.checks).read_text()) if args.checks else {})
            elif args.action == 'configure-checks':
                s.configure_checks(args.project_id,json.loads(Path(args.file).read_text()))
                result = s.project(args.project_id)
            elif args.action == 'projects':
                result = s.projects()
            elif args.action == 'submit':
                result = s.status(s.submit(json.loads(Path(args.file).read_text()),args.key)['run_id'])
            elif args.action == 'status':
                result = s.status(args.run_id)
            elif args.action == 'cancel':
                result = s.cancel(args.run_id)
            elif args.action == 'reconcile':
                result = s.reconcile(args.run_id)
            elif args.action == 'context':
                result = s.artifacts(args.run_id).get('context_bundle')
                if result is None:
                    raise Invalid('This older task has no frozen context bundle')
            elif args.action == 'artifacts':
                result = s.artifacts(args.run_id)
            elif args.action == 'runs':
                result = s.list_runs()
            elif args.action == 'worker':
                Worker(s).run(once=args.once)
                return
            elif args.action == 'serve':
                from .server import build_server
                build_server(s).run(transport='stdio')
                return
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (Invalid,ValueError,OSError) as e:
        print(json.dumps({'error':str(e)},ensure_ascii=False),file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
