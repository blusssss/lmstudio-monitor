"""Optional browser acceptance checks: pip install playwright; playwright install chromium."""
import argparse
import json
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

parser=argparse.ArgumentParser();parser.add_argument('--url',default='http://127.0.0.1:8090');parser.add_argument('--screenshots',default=str(Path(tempfile.gettempdir())/'lmstudio-v2-screenshots'));parser.add_argument('--clear-history',action='store_true');args=parser.parse_args()
out=Path(args.screenshots);out.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':1440,'height':1080},device_scale_factor=1)
    page=context.new_page()
    errors=[]
    page.on('pageerror',lambda err:errors.append(str(err)))
    page.goto(args.url);expect(page.locator('#connection')).not_to_have_text('正在连接')
    page.locator('#range').select_option('300')
    page.screenshot(path=str(out/'desktop.png'),full_page=True)
    assert page.locator('h1').inner_text()=='运行趋势'
    assert page.locator('#ttft').is_visible();assert page.locator('#prefill').is_visible()
    snapshot=page.request.get(args.url+'/api/state').json()
    if not snapshot['lm_connected']:assert page.locator('#notice').is_visible()
    if not snapshot['gpu']:assert page.locator('#gpu-util').inner_text()=='—'
    assert page.locator('body').evaluate('(e)=>e.scrollWidth')<=1440
    page.locator('#pause').click();before=page.locator('#chart-time').inner_text();page.wait_for_timeout(1500)
    assert page.locator('#chart-time').inner_text()==before
    page.locator('#pause').click()
    page.locator('#resource').select_option('power');page.locator('#range').select_option('300')
    page.locator('#model-info').click();assert page.locator('#detail-dialog').is_visible()
    page.keyboard.press('Escape');assert not page.locator('#detail-dialog').is_visible()
    page.locator('#diagnostics').click()
    with page.expect_download() as d:page.get_by_text('下载诊断',exact=True).click()
    diagnostic=json.loads(Path(d.value.path()).read_text());assert diagnostic['version']=='2.0.0';assert 'sources' in diagnostic
    page.locator('#close-detail').click()
    page.locator('#summaries-tab').click();assert '首 Token' in page.locator('#request-head').inner_text()
    page.locator('#tasks-tab').click()
    if page.locator('.row-open').count():
        page.locator('.row-open').first.click();assert page.locator('#detail-dialog').is_visible()
        assert '阶段耗时' in page.locator('#detail-content').inner_text()
        page.screenshot(path=str(out/'task-detail.png'));page.locator('#close-detail').click()
    with page.expect_download() as d:page.locator('#export').click()
    exported=json.loads(Path(d.value.path()).read_text());assert exported['state']['version']=='2.0.0'
    for width in [390,768]:
        page.set_viewport_size({'width':width,'height':844});page.wait_for_timeout(300)
        assert page.locator('body').evaluate('(e)=>e.scrollWidth')<=width,f'page overflow {width}'
        page.locator('#model-info').click();assert page.locator('#close-detail').is_visible();page.keyboard.press('Escape')
        page.screenshot(path=str(out/f'width-{width}.png'),full_page=True)
    if args.clear_history:
        second=page.context.new_page();second.goto(args.url)
        expect(second.locator('#connection')).not_to_have_text('正在连接')
        page.locator('#clear-history').click()
        expect(page.locator('#history-feedback')).to_contain_text('历史已清空')
        state=page.request.get(args.url+'/api/state').json()
        assert not state['summaries']
        assert all(r['status'] not in ('completed','cancelled','interrupted','error') for r in state['requests'])
        assert not any('（历史）' in v for v in page.locator('#model-select option').all_text_contents())
        expect(second.locator('#request-count')).to_have_text('0 条')
        expect(second.locator('#ttft')).to_have_text('—')
        expect(second.locator('#prefill')).to_have_text('—')
        assert not any('（历史）' in v for v in second.locator('#model-select option').all_text_contents())
        second.close()
    assert not errors,errors
    print('PASS desktop/mobile layout, graphs, pause, tabs, drawer, keyboard close, diagnostics and metric downloads; zero page errors')
    browser.close()
