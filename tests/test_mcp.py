import os
import json
import sys
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_real_stdio_discovery_and_query(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=['-m','codex_dph','--home',str(tmp_path),'serve'], env=dict(os.environ))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            result = await session.list_tools()
            names = {t.name for t in result.tools}
            assert {'submit_task','get_run','read_events','cancel_run','submit_review','revise_task','get_context'} <= names
            result = await session.call_tool('list_projects',{})
            assert not result.isError
            result = await session.call_tool('get_run',{'run_id':'unknown'})
            assert result.isError


@pytest.mark.asyncio
async def test_submit_disconnect_worker_and_reconnect(tmp_path):
    import asyncio
    from pathlib import Path
    from codex_dph.service import Service
    from test_workflow import git

    repo = tmp_path/'repo'
    repo.mkdir()
    git(repo,'init','-q')
    (repo/'hello.txt').write_text('base\n')
    git(repo,'add','.')
    git(repo,'commit','-qm','base')
    home = tmp_path/'state'
    service = Service(home)
    project = service.register(repo)
    params = StdioServerParameters(command=sys.executable,args=['-m','codex_dph','--home',str(home),'serve'],env=dict(os.environ))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            result = await session.call_tool('submit_task',{'spec':dict(project_id=project['project_id'],title='small change',objective='update hello',allowed_paths=['hello.txt','new.txt'],acceptance_criteria=['hello updated']),'idempotency_key':'mcp-one'})
            assert not result.isError
            run_id = json.loads(result.content[0].text)['run_id']
            context = await session.call_tool('get_context',{'run_id':run_id,'limit':100})
            assert not context.isError
            page=json.loads(context.content[0].text)
            assert len(page['text'])==100 and page['context_hash']
    # MCP process has disconnected; independently launched worker consumes persistent queue.
    fixture = str(Path(__file__).with_name('fake_dsh.py'))
    script = 'from codex_dph.service import Service; from codex_dph.worker import Worker; import sys; Worker(Service(sys.argv[1]),command=[sys.executable,sys.argv[2]]).run(once=True)'
    proc = await asyncio.create_subprocess_exec(sys.executable,'-c',script,str(home),fixture,
                                               stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(),15)
        assert proc.returncode == 0, err.decode()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            result = await session.call_tool('get_run',{'run_id':run_id})
            assert json.loads(result.content[0].text)['state'] == 'awaiting_review'
            diff = await session.call_tool('get_diff',{'run_id':run_id})
            assert 'new.txt' in json.loads(diff.content[0].text)['text']
    assert (repo/'hello.txt').read_text() == 'base\n'
