"""Offline Workbench UI smoke: fake APIs, no model network, no GPU or checkpoint.

Run against a built frontend. Optional --start owns and cleans up its Next process.
"""
import argparse
import copy
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from levi.paths import PROJECT, ROOT, configure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:17860')
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--browser', type=Path)
    args = parser.parse_args()
    configure()
    output = ROOT/'outputs/LEVI/validation/agent'
    output.mkdir(parents=True, exist_ok=True)
    process = None
    errors = []
    try:
        if args.start:
            address = urlsplit(args.base_url)
            with (output/'browser-server.log').open('wb') as log:
                process = subprocess.Popen([str(PROJECT/'.runtime/bun-linux-x64/bun'),'run','start','--hostname','127.0.0.1','--port',str(address.port)],
                    cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                    env={**os.environ,'LEVI_SAM3_ENABLED':'0','LEVI_SYNC_DISCOVER':'off','NEXT_TELEMETRY_DISABLED':'1'})
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError('Frontend exited before browser check')
                try:
                    with urllib.request.urlopen(args.base_url,timeout=1):
                        break
                except OSError:
                    time.sleep(.5)
            else:
                raise RuntimeError('Frontend not ready')
        provider={'name':'fixture','base_url':'https://example.invalid/v1','model':'fixture','key_env':'LEVI_MODEL_API_KEY','vision':False,'enabled':True,'credential_ready':False,'credential_source':'missing','allow_localhost':False}
        run={'id':'20260920T120000000000','status':'waiting_for_review','context':{'repo_id':'local/browser_fixture','episodes':[0],'cameras':[],'provider':'fixture','instruction':'Fixture review'},'completed':[0],'snapshot_bytes':1000,'requests':1,'tokens':17,'reserved_tokens':0,'changes':'draft'}
        change={'id':'draft','revision':0,'status':'draft','base_revision':'legacy','provenance':{'coverage':'sampled'},'decisions':{},'object_jobs':[], 'proposals':[
            {'episode_index':0,'kind':'segment','content':'Move fixture gripper','start':0,'end':1,'evidence_ids':['frame0']},
            {'episode_index':0,'kind':'issue','content':'Check fixture end state','start':1,'end':None,'evidence_ids':['frame0']}]}
        run['plan']={'revision':1,'digest':'fixture-plan','approval':{'actor':'human'},'pilot_episode':0,'pilot_review':None,'questions':[],'estimate':{'minimum_requests':1,'tokens':'unknown'},'excluded':['source writes']}
        second={**copy.deepcopy(run),'id':'second','changes':None,'status':'planned','completed':[]}
        second['plan']['approval']=None
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True,executable_path=str(args.browser) if args.browser else None,args=['--disable-gpu'])
            context=browser.new_context(viewport={'width':1440,'height':1000})
            page=context.new_page()
            page.on('pageerror',lambda e:errors.append(str(e)))
            def route(request):
                path=urlsplit(request.request.url).path
                if not request.request.url.startswith(args.base_url):
                    request.abort();return
                if path.startswith('/api/levi/agent/v1/'):
                    if path.endswith('/stream'):
                        request.fulfill(content_type='text/event-stream',body='id: 1\ndata: {"seq":1,"type":"action.completed","tool":"runs.prepare","elapsed_seconds":0.1}\n\n');return
                    if path.endswith(('/pilot/sessions', '/pilot/permissions', '/grants')):
                        request.fulfill(json=[]);return
                    if path.endswith('/runtimes'):
                        request.fulfill(json=[{'id':'codex','installed':False,'version':'1.12.0','authentication':'unknown','login_command':'codex login','logout_command':'codex logout'}]);return
                    if path.endswith('/manifest'):
                        request.fulfill(json={'status':'succeeded','artifacts':[{'name':'result.json','path':'agent/datasets/browser_fixture/runs/fixture/result.json','bytes':64}]});return
                    if path.endswith('/providers'):
                        request.fulfill(json=[provider]);return
                    if path.endswith('/runs'):
                        request.fulfill(json=[run,second]);return
                    if '/providers/' in path:
                        if path.endswith('/session'):provider.update(credential_ready=True,credential_source='session')
                        if path.endswith('/disconnect'):provider.update(enabled=False,credential_ready=False,credential_source='missing')
                        if path.endswith('/activate'):provider.update(enabled=True)
                        request.fulfill(json={'ok':True});return
                    payload=request.request.post_data_json or {};name=payload.get('name');a=payload.get('arguments',{})
                    result={}
                    if name=='runs.events':result={'events':[]}
                    elif name=='plans.approve':
                        target=second if a['run_id']=='second' else run
                        target['plan']['approval']={'actor':'human'};result=target
                    elif name=='plans.review_pilot':run['plan']['pilot_review']={'accepted':a['accepted'],'note':a['note']};result=run
                    elif name=='changes.diff':result=change
                    elif name=='media.sample':result={'items':[]}
                    elif name=='objects.status':result={'available':False}
                    elif name=='changes.edit':
                        change.update(proposals=a['proposals'],revision=change['revision']+1,status='draft',decisions={});result=change
                    elif name=='changes.review':
                        change['decisions'].update({str(i):a['decision'] for i in a['indices']});change['revision']+=1;result=change
                    elif name=='changes.validate':result={'ok':True}
                    elif name=='changes.approve':change['status']='approved';result=change
                    elif name=='changes.commit':change['status']='committed';run['status']='succeeded';result={'ok':True}
                    request.fulfill(json=result);return
                if path.startswith('/api/'):
                    request.fulfill(json={});return
                request.continue_()
            page.route('**/*',route)
            page.goto(args.base_url)
            page.get_by_role('button',name='Agent Workbench',exact=True).click()
            dock=page.locator('.levi-agent-dock')
            dock.get_by_label('Task center').select_option('second')
            expect(dock.get_by_role('button',name='Run pilot',exact=True)).to_be_disabled()
            dock.get_by_role('button',name='Approve execution plan',exact=True).click()
            expect(dock.get_by_role('button',name='Run pilot',exact=True)).to_be_enabled()
            expect(dock.get_by_role('button',name='Execute remaining',exact=True)).to_be_disabled()
            dock.get_by_label('Task center').select_option(run['id'])
            expect(dock.get_by_label('Proposal text')).to_have_value('Move fixture gripper')
            dock.get_by_label('Proposal text').fill('Human fixture edit')
            page.wait_for_timeout(3000)
            expect(dock.get_by_label('Proposal text')).to_have_value('Human fixture edit')
            dock.get_by_role('button',name='Save draft edits',exact=True).click()
            expect(dock.get_by_role('button',name='Save draft edits',exact=True)).to_have_count(0)
            color=dock.locator('select option').first.evaluate('(e)=>getComputedStyle(e).color')
            assert color not in {'rgb(255, 255, 255)','rgba(0, 0, 0, 0)'}
            dock.get_by_role('button',name='Accept visible',exact=True).click()
            expect(dock.get_by_text('No suggestions in this filter. Review accepted items or continue to validation.')).to_be_visible()
            dock.get_by_label('Pilot review notes').fill('Fixture boundaries and observed cost reviewed')
            dock.get_by_role('button',name='Accept pilot quality',exact=True).click()
            expect(dock.get_by_role('button',name='Execute remaining',exact=True)).to_be_enabled()
            dock.get_by_role('button',name='Validate & approve',exact=True).click()
            dock.get_by_role('button',name='Commit approved changes',exact=True).click()
            expect(dock.get_by_role('button',name='Create undo draft',exact=True)).to_be_visible()
            expect(dock.get_by_role('heading',name='Pilot activity',exact=True)).to_be_visible()
            dock.get_by_role('button',name='Refresh artifact manifest',exact=True).click()
            expect(dock.get_by_text('agent/datasets/browser_fixture/runs/fixture/result.json',exact=True)).to_be_visible()
            page.screenshot(path=str(output/'workbench-en.png'))
            page.get_by_role('button',name='Accounts & connections',exact=True).click()
            dock.get_by_text('Codex / Claude connections',exact=True).click()
            expect(dock.get_by_text('codex login',exact=True)).to_be_visible()
            expect(dock.get_by_text('fixture',exact=True).first).to_be_visible()
            dock.get_by_role('button',name='Set session credential',exact=True).click()
            dock.get_by_label('Session API key').fill('fixture-secret-not-real')
            dock.get_by_role('button',name='Save session credential',exact=True).click()
            expect(dock.get_by_text('Text only · Session credential', exact=True)).to_be_visible()
            assert page.evaluate('!JSON.stringify(localStorage).includes("fixture-secret")')
            dock.get_by_role('button',name='Disconnect',exact=True).click()
            expect(dock.get_by_role('button',name='Reconnect',exact=True)).to_be_visible()
            page.get_by_role('button',name='Switch to Chinese',exact=True).click()
            expect(page.locator('html')).to_have_attribute('lang','zh-CN')
            page.screenshot(path=str(output/'accounts-zh.png'))
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/'accounts-mobile.png'))
            assert not errors, errors
            assert page.evaluate('!JSON.stringify(localStorage).includes("fixture-secret")')
            browser.close()
        (output/'browser-results.json').write_text(json.dumps({'status':'passed','model':'mock only','gpu':False,'checks':['Pilot event stream','artifact manifest','runtime account guidance','execution approval','pilot gate','draft preservation','batch review','commit','accounts','bilingual','dropdown color','mobile layout'],'errors':errors},indent=2))
        print('Agent browser smoke passed; artifacts: outputs/LEVI/validation/agent')
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait()


if __name__=='__main__':
    main()
