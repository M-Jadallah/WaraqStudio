"""Real packaged host/plugin acceptance with synthetic credentials and localhost only.

The fixture wraps global fetch inside the plugin host: every external provider
request is diverted to localhost or rejected. No real OAuth or paid model runs.
"""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from waraq.config import Config
from waraq.embedded_accounts import EmbeddedAccountService, AccountUnavailableError
from waraq.embedded_runtime import PackagedPayload, EmbeddedRuntimeSupervisor, private_directory


def main():
    payload = PackagedPayload()
    calls=[]; mode={'quota':None}
    class Model(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            calls.append(('usage',self.headers.get('ChatGPT-Account-Id')))
            data=json.dumps({'plan_type':'plus','rate_limit':{'primary_window':{'used_percent':25,'limit_window_seconds':18000,'reset_at':int(time.time()+1000)},'secondary_window':{'used_percent':60,'limit_window_seconds':604800,'reset_at':int(time.time()+3000)}}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            account=self.headers.get('ChatGPT-Account-Id')
            calls.append(('model',account))
            assert self.headers.get('Authorization','').startswith('Bearer synthetic-access-')
            assert not any(t.get('name','').startswith('codex-') for t in body.get('tools',[]))
            if account==mode['quota']:
                data=json.dumps({'error':{'code':'usage_limit_reached','message':'synthetic quota','retry_after':120}}).encode()
                self.send_response(429);self.send_header('Content-Type','application/json');self.send_header('Retry-After','120');self.end_headers();self.wfile.write(data);return
            text='Local fixture completed.'
            message={'id':'msg_test','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':text,'annotations':[]}]}
            response={'id':'resp_test','object':'response','created_at':int(time.time()),'model':body.get('model'),'status':'completed','output':[message], 'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}}
            events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
                    {'type':'response.output_item.added','output_index':0,'item':{**message,'content':[]}},
                    {'type':'response.content_part.added','item_id':'msg_test','output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}},
                    {'type':'response.output_text.delta','item_id':'msg_test','output_index':0,'content_index':0,'delta':text},
                    {'type':'response.output_item.done','output_index':0,'item':message},
                    {'type':'response.completed','response':response}]
            data=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Model)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='waraq-embedded-') as temp:
            root=Path(temp); cfg=Config(root);cfg.data['accounts_auto_refresh']=False
            profile=private_directory(root/'accounts-v2')
            accounts=[{'accountId':'synthetic-'+str(i),'email':f'{i}@example.test','refreshToken':'synthetic-refresh-'+str(i)+'x'*4000,
                       'accessToken':'synthetic-access-'+str(i),'expiresAt':int((time.time()+86400)*1000),'addedAt':i,'lastUsed':0,'enabled':True} for i in (1,2)]
            seed="import {writeFile} from 'node:fs/promises'; import {seal} from " + json.dumps((payload.root/'plugin/dist/vendor/lib/storage/encrypted-fs.js').as_uri()) + ";let data='';for await(const chunk of process.stdin)data+=chunk; await writeFile(process.argv[1],seal(data));"
            env=dict(os.environ,WARAQ_ACCOUNTS_STORAGE_DIR=str(profile))
            subprocess.run([str(payload.node),'--input-type=module','-e',seed,str(profile/'oc-codex-multi-auth-accounts.json')],input=json.dumps({'version':3,'accounts':accounts,'activeIndex':0}),text=True,env=env,check=True,timeout=20)
            # Test-only wrapper imports the actual packaged entry and blocks all
            # public network destinations. It is never included in the installer.
            fixture=root/'fixture';plugin=fixture/'plugin';plugin.mkdir(parents=True)
            (plugin/'package.json').write_text('{"type":"module"}',encoding='utf-8')
            (plugin/'index.js').write_text("const original=globalThis.fetch; globalThis.fetch=async(input,init)=>{const u=new URL(typeof input==='string'?input:input.url); if(u.hostname==='chatgpt.com')return original('http://127.0.0.1:"+str(server.server_port)+"'+u.pathname,init); if(u.hostname==='127.0.0.1')return original(input,init); return new Response('{}',{status:404,headers:{'content-type':'application/json'}});}; const loaded=await import("+json.dumps((payload.root/'plugin/index.js').as_uri())+"); export default loaded.default;",encoding='utf-8')
            fixture_payload=SimpleNamespace(root=fixture,node=payload.node,opencode=payload.opencode,plugin=payload.plugin)
            with patch('waraq.embedded_runtime.PackagedPayload',return_value=fixture_payload):
                supervisor=EmbeddedRuntimeSupervisor(cfg); service=EmbeddedAccountService(cfg,supervisor)
                try:
                    rows=service.refresh();assert len(rows)==2
                    runtime=supervisor.ensure();assert runtime is supervisor.ensure()
                    try:
                        build_opener(ProxyHandler({})).open(runtime.url+'/api/info',timeout=5)
                    except HTTPError as error: assert error.code==401
                    else: raise AssertionError('Anonymous native request accepted')
                    unauthorized=runtime.request('POST','/api/rpc/waraq-accounts/accounts.list',{'input':{'apiVersion':1,'capability':'wrong'}} ,directory=profile/'home')
                    assert unauthorized['output']['error']['code']=='UNAUTHORIZED'
                    service.refresh_usage(rows[0]['id'],manual=True)
                    usage=service.list()[0]['usage']; assert usage['fiveHour']['remainingPercent']==75 and usage['weekly']['remainingPercent']==40
                    work=root/'attempt';work.mkdir()
                    attempt=service.begin_attempt('job','oc1','openai/gpt-6-astra',work)
                    second_work=root/'attempt2';second_work.mkdir()
                    try: service.begin_attempt('other','oc2','openai/gpt-6-astra',second_work,max_concurrency=1)
                    except AccountUnavailableError as error: assert error.reason=='CONCURRENCY_LIMIT'
                    else: raise AssertionError('Concurrency cap bypassed by another location')
                    session=runtime.create_session(work,{'PWD':str(work)})
                    command=[str(runtime.executable),'run','--server',runtime.url,'--session',session,'--auto','--model','openai/gpt-6-astra','--agent','build','--format','json','--','Return a short local fixture message.']
                    try:
                        result=subprocess.run(command,cwd=work,env=dict(runtime.env,OPENCODE_PASSWORD=runtime.password,PWD=str(work)),stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',timeout=30)
                    except subprocess.TimeoutExpired as error:
                        print('fixture_cli_output',error.stdout,error.stderr,'fixture_calls',calls)
                        for path in ('/api/session/active','/api/form','/api/session/'+session):
                            try: print('fixture_native_state',path,json.dumps(runtime.request('GET',path))[:3000])
                            except Exception: pass
                        raise
                    assert result.returncode==0,result.stdout[-1500:]+result.stderr[-500:]
                    assert runtime.inactive(session)
                    child=runtime.request('POST','/api/session',{'parentID':session})['data']
                    assert child['location']['directory']==str(work)
                    child_command=list(command);child_command[child_command.index('--session')+1]=child['id']
                    child_result=subprocess.run(child_command,cwd=work,env=dict(runtime.env,OPENCODE_PASSWORD=runtime.password,PWD=str(work)),stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',timeout=30)
                    assert child_result.returncode==0,child_result.stdout[-1200:]+child_result.stderr[-500:]
                    assert runtime.inactive(session)
                    resolved=service.resolve_attempt(attempt['attempt_id']);assert not resolved.get('failure'),resolved
                    service.close_attempt(attempt['attempt_id'],outcome='completed')
                    assert any(kind=='model' for kind,_ in calls),calls
                    assert len({account for kind,account in calls if kind=='model'})==1,calls
                    mode['quota']=next(account for kind,account in calls if kind=='model')
                    service.prefer(attempt['account_id'])
                    quota_attempt=service.begin_attempt('quota','oc1','openai/gpt-6-astra',second_work)
                    quota_session=runtime.create_session(second_work,{'PWD':str(second_work)})
                    quota_command=list(command);quota_command[quota_command.index('--session')+1]=quota_session
                    quota_result=subprocess.run(quota_command,cwd=second_work,env=dict(runtime.env,OPENCODE_PASSWORD=runtime.password,PWD=str(second_work)),stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',timeout=30)
                    assert quota_result.returncode!=0
                    assert runtime.inactive(quota_session)
                    failure=service.resolve_attempt(quota_attempt['attempt_id'])['failure']
                    assert failure and failure['kind']=='quota' and failure['confirmed'],failure
                    service.close_attempt(quota_attempt['attempt_id'],outcome='limit')
                    third_work=root/'attempt3';third_work.mkdir()
                    next_attempt=service.begin_attempt('quota','oc1','openai/gpt-6-astra',third_work)
                    assert next_attempt['account_id']!=quota_attempt['account_id']
                    service.close_attempt(next_attempt['attempt_id'],outcome='cancelled')
                    assert b'synthetic-refresh-' not in (profile/'oc-codex-multi-auth-accounts.json').read_bytes()
                    old_ids={row['id'] for row in service.list()}
                    service.close()
                    service=EmbeddedAccountService(cfg,EmbeddedRuntimeSupervisor(cfg))
                    restored=service.refresh()
                    assert {row['id'] for row in restored}==old_ids
                    assert all(row['login_state']=='authenticated' for row in restored)
                    print(json.dumps({'status':'passed','native_version':'2.0.32','accounts':2,'shared_runtime':True,'large_dpapi_pool':True,'private_rpc':True,'global_concurrency':True,'child_session_pin':True,'fresh_attempt_failover':True,'login_survives_restart':True,'model_calls':sum(k=='model' for k,_ in calls),'usage_calls':sum(k=='usage' for k,_ in calls)}))
                except Exception:
                    logfile=profile/'data/opencode/log/opencode.log'
                    if logfile.is_file():
                        for line in logfile.read_text(encoding='utf-8',errors='replace').splitlines():
                            if ('level=WARN' in line or 'level=ERROR' in line) and 'watcher' not in line: print(line[-2000:])
                    raise
                finally: service.close()
    finally: server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':main()
