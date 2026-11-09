import json
import sys
from pathlib import Path
sys.stdin.read()
Path('hello.txt').write_text('updated\n')
Path('new.txt').write_text('new\n')
print(json.dumps({'type': 'session', 'sessionId': 'test-session'}))
print(json.dumps({'type': 'final', 'reason': {'kind': 'completed'}}))
