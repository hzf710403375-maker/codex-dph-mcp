import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT/'src/codex_dph/dsh_settings.mjs'
pytestmark = pytest.mark.skipif(not shutil.which('node') or not (ROOT/'.tools/node_modules/yaml').exists(), reason='Local DSH Node dependencies required')


def invoke(tmp_path, rows):
    source = tmp_path/'source'
    profile = source/'profiles/desktop'
    profile.mkdir(parents=True, exist_ok=True)
    (profile/'cordis.patch.yml').write_text(json.dumps(rows))
    (source/'.credentials.yaml').write_text('do not read or copy this file')
    fake = tmp_path/'dsh'
    fake.write_text('#!/usr/bin/env python3\nimport json,sys,os\na=sys.argv\np=a[a.index("--patch")+1]\nprint(json.dumps({"overlay":json.load(open(p)),"home":os.environ["DSH_HOME"],"args":a[1:]}))\n')
    fake.chmod(0o700)
    env=dict(os.environ, CODEX_DPH_DSH_CONFIG_HOME=str(source), CODEX_DPH_DSH_SETTINGS_PROFILE='desktop', DSH_HOME=str(tmp_path/'runtime'))
    return subprocess.run(['node',str(BRIDGE),str(fake),'--profile','headless','--json','-'],env=env,text=True,capture_output=True)


def test_reuses_model_and_original_credential_path_without_ui_or_secret_copy(tmp_path):
    rows=[{'id':'agent-default-model','config':{'provider':'custom','model':'model-one','reasoningEffort':'high'}},
          {'id':'llm-pi-ai','config':{'providers':{'custom':{'baseURL':'https://example.invalid/v1','apiKeyEnv':'CUSTOM_KEY','models':[{'id':'model-one'}]}}}},
          {'id':'ui-chat','config':{'foo':'bar'}}]
    result=invoke(tmp_path,rows)
    assert result.returncode==0,result.stderr
    out=json.loads(result.stdout)
    entries={x['id']:x for x in out['overlay']}
    assert entries['agent-default-model']['config']==rows[0]['config']
    assert entries['llm-pi-ai']['config']==rows[1]['config']
    assert entries['credentials']['config']['path']==str(tmp_path/'source/.credentials.yaml')
    assert entries['credentials']['disabled'] is False
    assert 'ui-chat' not in entries
    assert 'do not read or copy' not in result.stdout
    assert not list((tmp_path/'runtime').glob('*.json'))
    rows[0]['config']['model']='model-two'
    assert 'model-two' in invoke(tmp_path,rows).stdout


def test_missing_model_refuses_silent_fallback(tmp_path):
    result=invoke(tmp_path,[])
    assert result.returncode!=0
    assert 'default model' in result.stderr


def test_literal_secrets_refused_without_disclosure(tmp_path):
    result=invoke(tmp_path,[{'id':'agent-default-model','config':{'provider':'custom','model':'m'}},
         {'id':'llm-pi-ai','config':{'apiKey':'never-echo-this'}}])
    assert result.returncode!=0
    assert 'literal credential' in result.stderr
    assert 'never-echo-this' not in result.stdout+result.stderr


def test_home_override_replaces_config_like_native_harness(tmp_path):
    source=tmp_path/'source';source.mkdir()
    (source/'cordis.patch.yml').write_text(json.dumps([
        {'id':'agent-default-model','config':{'provider':'custom','model':'new'}},
        {'id':'llm-deepseek','config':{'apiKeyEnv':'NEW_KEY'}}]))
    result=invoke(tmp_path,[
        {'id':'agent-default-model','config':{'provider':'custom','model':'old','reasoningEffort':'high'}},
        {'id':'llm-deepseek','config':{'baseURL':'https://old.invalid'}}])
    assert result.returncode==0,result.stderr
    entries={x['id']:x for x in json.loads(result.stdout)['overlay']}
    assert entries['agent-default-model']['config']=={'provider':'custom','model':'new'}
    assert entries['llm-deepseek']['config']=={'apiKeyEnv':'NEW_KEY'}


def test_runtime_symlink_to_source_is_rejected(tmp_path):
    source=tmp_path/'source';source.mkdir()
    (tmp_path/'runtime').symlink_to(source, target_is_directory=True)
    result=invoke(tmp_path,[{'id':'agent-default-model','config':{'provider':'custom','model':'m'}}])
    assert result.returncode!=0
    assert 'separate DSH_HOME' in result.stderr
