from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import threading
import zipfile

import pytest

from waraq import cliproxy
from waraq.config import Config, defaults
from waraq.scheduler import Scheduler
from waraq.services import OpenCode, ServiceError
from waraq.bridge import Bridge
from waraq.store import Store


class Response:
    def __init__(self, payload=None, status=200):
        self.payload, self.status_code, self.ok = payload, status, 200 <= status < 400
    def json(self):
        return self.payload


def test_url_normalization_and_v1_url():
    assert cliproxy.normalize_cliproxy_url(' HTTPS://proxy.example:8317/v1/ ') == 'https://proxy.example:8317'
    assert cliproxy.normalize_cliproxy_url('http://localhost:8317/api/v1/v1') == 'http://localhost:8317/api'
    assert cliproxy.cliproxy_v1_url('http://localhost:8317/v1') == 'http://localhost:8317/v1'


@pytest.mark.parametrize('url', ['', 'ftp://proxy', 'https:///no-host', 'http://user:pass@proxy', 'http://proxy:99999', 'http://proxy?q=x', 'http://proxy/#f'])
def test_url_rejects_invalid_or_credentialed_urls(url):
    with pytest.raises(ValueError):
        cliproxy.normalize_cliproxy_url(url)


def test_fetch_models_v1_and_normalizes_metadata(monkeypatch):
    calls=[]
    def get(url, **kwargs):
        calls.append((url,kwargs))
        return Response({'data':[{'id':'cliproxy/acme/model','display_name':'Model','owned_by':'acme','thinking':{'levels':['low','high']}}, 'other']})
    monkeypatch.setattr(cliproxy.requests, 'get', get)
    models=cliproxy.fetch_cliproxy_models('http://proxy/v1', api_key='secret')
    assert calls[0][0]=='http://proxy/v1/models'
    assert calls[0][1]['headers']['Authorization']=='Bearer secret'
    assert [m['id'] for m in models]==['acme/model','other']
    assert models[0]['thinkingLevels']==['low','high']


def test_models_management_fallback(monkeypatch):
    calls=[]
    def get(url, **kwargs):
        calls.append((url,kwargs))
        if url.endswith('/v1/models'): return Response({},401)
        if url.endswith('/v8/management/credentials'): return Response({'files':[{'name':'auth.json'}]})
        return Response({'models':[{'id':'fallback'}]})
    monkeypatch.setattr(cliproxy.requests,'get',get)
    assert cliproxy.fetch_cliproxy_models('http://proxy',management_key='manage')[0]['id']=='fallback'
    assert calls[-1][0].endswith('/v8/management/credentials/models')
    assert calls[-1][1]['params']=={'name':'auth.json'}


def test_model_errors_do_not_leak_secret(monkeypatch):
    monkeypatch.setattr(cliproxy.requests,'get',lambda *a,**k: Response({},401))
    with pytest.raises(ValueError) as error:
        cliproxy.fetch_cliproxy_models('http://proxy',api_key='TOP_SECRET')
    assert 'TOP_SECRET' not in str(error.value)


def test_accounts_codex_layer1_quota_and_schema(monkeypatch):
    def get(url, **kwargs): return Response({'files':[{'name':'x','auth_index':'1','type':'codex','supports_quota':True}]})
    def post(url, **kwargs):
        assert url.endswith('/v0/management/quota/fetch')
        return Response({'plan':'Plus','buckets':[{'name':'primary','remaining_fraction':.72}]})
    monkeypatch.setattr(cliproxy.requests,'get',get); monkeypatch.setattr(cliproxy.requests,'post',post)
    rows=cliproxy.fetch_cliproxy_accounts('http://proxy')
    assert rows[0]['remainingPercent']==72
    assert rows[0]['hasLimits']
    assert set(rows[0]) == {
        'id','authIndex','name','email','provider','providerLabel','plan','status','statusLabel','available','disabled',
        'remainingPercent','hasLimits','fiveHourRemaining','fiveHourResetText','weeklyRemaining','weeklyResetText',
        'limitsSummary','windows','error'}


def test_accounts_codex_layer2_wham_usage(monkeypatch):
    def get(url, **kwargs):
        if url.endswith('/v8/management/credentials'): return Response({'files':[{'name':'c','auth_index':7,'type':'codex','chatgpt_account_id':'acct'}]})
        return Response({'files':[]})
    def post(url, **kwargs):
        if url.endswith('/requests/api-call'):
            body=kwargs['json']; assert body['url'].endswith('/wham/usage')
            return Response({'body':json.dumps({'plan_type':'Plus','rate_limit':{'primary_window':{'used_percent':25},'secondary_window':{'used_percent':50}}})})
        return Response({})
    monkeypatch.setattr(cliproxy.requests,'get',get); monkeypatch.setattr(cliproxy.requests,'post',post)
    row=cliproxy.fetch_cliproxy_accounts('http://proxy')[0]
    assert (row['fiveHourRemaining'],row['weeklyRemaining'],row['plan'])==(75,50,'Plus')


def test_accounts_gemini_layer2_retrieve_quota(monkeypatch):
    monkeypatch.setattr(cliproxy.requests,'get',lambda url,**k: Response({'files':[{'name':'g','auth_index':2,'type':'gemini-cli','project_id':'p'}]}) if url.endswith('/v8/management/credentials') else Response({'files':[]}))
    def post(url, **kwargs):
        if url.endswith('/requests/api-call'): return Response({'body':{'buckets':[{'modelId':'gemini-pro','remainingFraction':.4}]}})
        return Response({})
    monkeypatch.setattr(cliproxy.requests,'post',post)
    row=cliproxy.fetch_cliproxy_accounts('http://proxy')[0]
    assert row['fiveHourRemaining']==40 and row['windows'][0]['id']=='gemini-pro'


@pytest.mark.parametrize(('provider','signals','five','weekly'),[
    ('codex',{'x-codex-primary-used-percent':'30','x-codex-secondary-used-percent':'80'},70,20),
    ('claude',{'anthropic-ratelimit-unified-5h-utilization':.2,'anthropic-ratelimit-unified-7d-utilization':.6},80,40),
])
def test_accounts_layer3_quota_signals(monkeypatch, provider, signals, five, weekly):
    monkeypatch.setattr(cliproxy.requests,'get',lambda url,**k: Response({'files':[{'name':'a','type':provider,'quota':{'signals':signals}}]}) if url.endswith('/v8/management/credentials') else Response({'files':[]}))
    monkeypatch.setattr(cliproxy.requests,'post',lambda *a,**k: Response({}))
    row=cliproxy.fetch_cliproxy_accounts('http://proxy')[0]
    assert row['fiveHourRemaining']==five and row['weeklyRemaining']==weekly


def test_write_proxy_config_isolated(tmp_path):
    path=cliproxy.write_opencode_cliproxy_config(tmp_path,'https://proxy/v1','cliproxy/org/model#old','xhigh')
    data=json.loads(path.read_text())
    assert data['providers']['cliproxy']['env']==['CLIPROXY_API_KEY']
    assert data['providers']['cliproxy']['settings']['baseURL']=='https://proxy/v1'
    assert data['model']=='cliproxy/org/model'
    assert 'xhigh' in data['providers']['cliproxy']['models']['org/model']['variants']


def test_config_allows_empty_proxy_model_and_scheduler_stops_clearly(tmp_path):
    config=Config(tmp_path)
    current={'id':'oc1','provider':'openai','model':'openai/existing','variant':'low'}
    current_scheduler=Scheduler(None,config,None,youtube=object(),deepgram=object(),opencode=object(),library=object(),accounts=object())
    assert current_scheduler.effective_worker(current)==current
    config.save({'opencode_source_mode':'cliproxy','cliproxy_model':''})
    assert config.snapshot()['cliproxy_model']==''
    scheduler=Scheduler(None, config, None, youtube=object(), deepgram=object(), opencode=object(), library=object())
    worker=scheduler.effective_worker({'id':'oc1','provider':'openai','model':'openai/old'})
    assert worker['provider']=='cliproxy' and worker['model']==''
    source=tmp_path/'source.txt'; source.write_text('transcript',encoding='utf-8')
    job={'transcript':str(source),'folder':str(tmp_path/'job'),'position':1,'title':'test'}
    with pytest.raises(ServiceError,match='حدد نموذج') as error:
        OpenCode().process(job, worker, {**defaults(tmp_path),'opencode_source_mode':'cliproxy'}, '', None, lambda _:None)
    assert error.value.kind=='blocked'


def test_opencode_process_uses_isolated_proxy_env_model_and_thinking(tmp_path, monkeypatch):
    source=tmp_path/'source.txt'; source.write_text('تفريغ عربي',encoding='utf-8')
    job={'transcript':str(source),'folder':str(tmp_path/'job'),'position':1,'title':'درس'}
    cfg=defaults(tmp_path)
    cfg.update(opencode_source_mode='cliproxy',cliproxy_base_url='https://proxy.example/v1',
        cliproxy_model='org/model',cliproxy_thinking_level='xhigh',opencode_executable='opencode')
    monkeypatch.setenv('OPENCODE_CONFIG','inherited-config.json')
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT','inherited secret config')
    monkeypatch.setattr('waraq.services.resolve_opencode',lambda _:Path('opencode.exe'))
    monkeypatch.setattr('waraq.services.opencode_version',lambda _: '2.0.27')
    monkeypatch.setattr(OpenCode,'prepare_context',lambda *args:[])
    observed={}
    def run(command,cwd,cancel,timeout,log,env):
        observed.update(command=command,env=env)
        assert env['CLIPROXY_API_KEY']=='proxy-key'
        assert env['OPENCODE_CONFIG_DIR']==str(Path(cfg['opencode_data_root'])/'oc1'/'config')
        assert 'OPENCODE_CONFIG' not in env and 'OPENCODE_CONFIG_CONTENT' not in env
        with zipfile.ZipFile(cwd/'001-درس.docx','w') as document:
            document.writestr('[Content_Types].xml','<Types/>')
            document.writestr('_rels/.rels','<Relationships/>')
            document.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>مستند</w:t></w:r></w:p></w:body></w:document>')
    monkeypatch.setattr('waraq.services.run_process',run)
    scheduler_worker=Scheduler(None,SimpleNamespace(snapshot=lambda:cfg),None,youtube=object(),deepgram=object(),
        opencode=object(),library=object(),accounts=object()).effective_worker({'id':'oc1','provider':'openai','model':'stale','variant':'low','agent':'build'})
    output=OpenCode().process(job,scheduler_worker,cfg,
        'proxy-key',threading.Event(),lambda _:None)
    assert output.is_file()
    assert observed['command'][observed['command'].index('--model')+1]=='cliproxy/org/model#xhigh'
    generated=json.loads((Path(observed['env']['OPENCODE_CONFIG_DIR'])/'opencode.json').read_text(encoding='utf-8'))
    assert generated['model']=='cliproxy/org/model'


def test_qml_exposes_mode_switch_and_proxy_model_accounts_controls():
    qml=(Path(__file__).parents[1]/'waraq'/'qml'/'Main.qml').read_text(encoding='utf-8')
    assert 'objectName: "aiModeCurrentCard"' in qml
    assert 'objectName: "aiModeCliProxyCard"' in qml
    assert 'objectName: "cliProxyModelCombo"' in qml
    assert 'objectName: "cliProxyThinkingCombo"' in qml
    assert 'objectName: "cliProxyAccountsList"' in qml
    assert 'backend.fetchCliProxyModels' in qml and 'backend.refreshCliProxyAccounts' in qml


def test_bridge_saves_and_clears_secrets_and_publishes_proxy_models_accounts(tmp_path, qt_app, monkeypatch):
    class Secrets:
        def __init__(self): self.data={}
        def set(self,k,v): self.data[k]=v
        def get(self,k): return self.data.get(k,'')
        def delete(self,k): self.data.pop(k,None)
    secrets=Secrets(); config=Config(tmp_path); store=Store(tmp_path/'bridge.db')
    scheduler=SimpleNamespace(accounts=None, running=False, workers=lambda: [], idle=lambda:True,
        disabled_workers={}, forget_keys=lambda:None, shutdown=lambda:None, library=SimpleNamespace())
    bridge=Bridge(store,config,secrets,scheduler); bridge.timer.stop(); bridge._account_usage_timer.stop()
    try:
        assert bridge.setOpenCodeSourceMode('cliproxy')
        assert bridge.saveCliProxyConnection('http://proxy/v1','API_SECRET','MGMT_SECRET')
        assert bridge.saveCliProxyModelSettings('', 'high')
        assert bridge.cliProxyState['hasApiKey'] and bridge.cliProxyState['hasManagementKey']
        bridge.clearCliProxyKey('api_key'); bridge.clearCliProxyKey('management_key')
        assert not bridge.cliProxyState['hasApiKey'] and not bridge.cliProxyState['hasManagementKey']
        bridge._apply_cliproxy_models([{'id':'org/model','name':'Model','provider':'','thinkingLevels':['low']}], '')
        assert bridge.cliProxyModels == ['org/model']
        assert {item['id'] for item in bridge.cliProxyThinkingLevels} >= {'', 'low', 'high'}
        row={'id':'account-1','authIndex':'1','name':'Account','email':'','provider':'codex','providerLabel':'OpenAI Codex',
             'plan':'Plus','status':'active','statusLabel':'متاح','available':True,'disabled':False,'remainingPercent':70.0,
             'hasLimits':True,'fiveHourRemaining':70.0,'fiveHourResetText':'','weeklyRemaining':-1.0,'weeklyResetText':'',
             'limitsSummary':'5 ساعات: 70%','windows':[],'error':''}
        bridge._apply_cliproxy_accounts([row], '')
        assert bridge.cliProxyAccountsModel._items[0]['remainingPercent']==70.0
        assert bridge.cliProxyState['accountsCount']==1
        assert set(bridge.cliProxyAccountsModel._items[0]) == set(row)
    finally:
        bridge.closeAccounts()
        dispose = __import__('qt_test_support').dispose_bridge
        dispose(bridge); store.db.close()
