'use strict';
const $=id=>document.getElementById(id);
const fmt=(v,d=1)=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toFixed(d);
const text=(id,v)=>$(id).textContent=v;
const timeLabel=t=>new Date(t*1000).toLocaleTimeString('zh-CN',{hour12:false});
let state=null,history=[],range=1800,paused=false,modelId='',gpuId='',lastHistory=0;
const latest=(items,key)=>items.filter(x=>x[key]!=null).sort((a,b)=>(b.updated_at||0)-(a.updated_at||0))[0]||{};
function options(select,items,value){
 const signature=JSON.stringify(items);
 if(select.dataset.signature!==signature){select.replaceChildren(...items.map(([v,label])=>{const o=document.createElement('option');o.value=v;o.textContent=label;return o;}));select.dataset.signature=signature;}
 select.value=items.some(([v])=>v===value)?value:items[0]?.[0]||'';
 return select.value;
}
function element(tag,content,className){const el=document.createElement(tag);el.textContent=content;if(className)el.className=className;return el;}
function render(data){
 state=data;
 $('connection').className='badge ok';text('connection','采集器在线');text('clock',timeLabel(data.ts));
 $('notice').hidden=data.lm_connected;
 text('notice','LM Studio API 尚未连接。请启动本机服务（127.0.0.1:1234）；GPU 和系统监控仍在采集。连接恢复后会自动接入日志。');
 const instances=data.models.flatMap(m=>(m.loaded_instances||[]).map(i=>({model:m,instance:i})));
 const modelOptions=instances.map(x=>[x.instance.id,x.instance.id]);
 for(const r of data.requests)if(!modelOptions.some(([id])=>id===r.model))modelOptions.push([r.model,`${r.model} · 历史`]);
 modelId=options($('model-select'),modelOptions.length?modelOptions:[['','暂无加载实例']],modelId);
 const selected=instances.find(x=>x.instance.id===modelId),live=data.live[modelId]||{},slots=Object.values(live.slots||{});
 const active=data.lm_connected&&!live.stale?slots.filter(s=>s.active&&data.ts-(s.updated_at||0)<12):[];
 const decode=active.some(s=>s.decode_tps!=null)?active.reduce((n,s)=>n+(s.decode_tps||0),0):null;
 text('decode',fmt(decode));text('decode-note',active.length?`运行中 · ${active.length} 个活跃 slot`:'等待生成 · 约 3 秒更新');
 const pre=latest(slots,'prefill_tps');text('prefill',fmt(pre.prefill_tps));text('prefill-note',pre.prefill_tokens!=null?`最近实际处理 ${pre.prefill_tokens} tokens`:'按实际处理 Token 计时');
 const requests=data.requests.filter(r=>r.model===modelId),last=requests[0]||{};
 text('ttft',fmt(last.timeToFirstTokenSec==null?null:last.timeToFirstTokenSec*1000));
 gpuId=options($('gpu-select'),data.gpu.length?data.gpu.map(g=>[g.uuid,`GPU ${g.index} · ${g.name}`]):[['','GPU 不可用']],gpuId);
 const gpu=data.gpu.find(g=>g.uuid===gpuId)||{};
 text('gpu-util',fmt(gpu.utilization,0));text('gpu-name',gpu.name||'等待硬件采样');text('vram',fmt(gpu.memory_used==null?null:gpu.memory_used/1024,2));text('vram-note',gpu.memory_total?`总计 ${fmt(gpu.memory_total/1024,1)} GiB · 包含其他进程`:'包含其他进程');text('temp',fmt(gpu.temperature,0));text('power',fmt(gpu.power,0));text('clock-note',`GPU 频率 ${fmt(gpu.sm_clock,0)} MHz`);
 text('cpu',`${fmt(data.system.cpu)} %`);text('ram',`${fmt(data.system.memory_used/1024)} / ${fmt(data.system.memory_total/1024)} GiB`);
 const context=latest(slots,'context_tokens'),cap=selected?.instance.config?.context_length;
 text('context-value',`${context.context_tokens??'—'} / ${cap??'—'} tokens`);
 $('context-bar').style.width=`${cap&&context.context_tokens!=null?Math.min(100,context.context_tokens/cap*100):0}%`;
 text('context-note',context.context_tokens!=null?`结束时观测 · ${timeLabel(context.updated_at)}${context.truncated?' · 发生截断':''}；生成中不代表当前精确占用。`:'容量来自加载配置；占用来自 Runtime 结束日志。');
 text('model-subtitle',selected?`${selected.model.display_name} · ${selected.model.quantization?.name||'—'}`:modelId?`${modelId} · 历史实例`:'等待加载模型');
 text('model-status',!data.lm_connected?'未连接':active.length?'生成中':selected?'已加载':modelId?'未加载':'空闲');$('model-status').className='badge'+(active.length?' ok':'');
 const config=selected?.instance.config||{};
 const details=[['模型最大上下文',selected?.model.max_context_length??'—'],['实例上下文',cap??'—'],['并发上限',config.parallel??'—'],['Flash Attention',config.flash_attention==null?'—':config.flash_attention?'开启':'关闭'],['Runtime',live.engine_version?`llama.cpp ${live.engine_version}`:'—'],['最近输入 / 输出',last.promptTokensCount!=null?`${last.promptTokensCount} / ${last.predictedTokensCount}`:'—']];
 $('model-details').replaceChildren(...details.map(([k,v])=>{const d=document.createElement('div');d.append(element('dt',k),element('dd',v));return d;}));
 text('kv-location',config.offload_kv_cache_to_gpu==null?'等待配置':config.offload_kv_cache_to_gpu?'GPU Offload 开启':'GPU Offload 关闭');
 const processes=data.gpu_processes.filter(p=>p.gpu_uuid===gpuId);
 $('processes').replaceChildren(...(processes.length?processes.map(p=>{const d=element('div','','process-row');const isLm=Object.values(data.live).some(l=>l.pid===p.pid)||p.name.includes('llama-server');const name=p.name.split('/').pop();d.append(element('span',`${isLm?'LM Studio':'其他进程'} · ${name} · PID ${p.pid}`),element('b',`${fmt(p.memory,0)} MiB`));return d;}):[element('p','暂无可读取的 GPU 计算进程','muted')]));
 const names={lm_api:'模型 API',runtime:'Runtime 日志',model:'请求统计',gpu:'GPU 采样',system:'系统资源'};
 $('sources').replaceChildren(...Object.entries(names).map(([k,label])=>{const s=data.sources[k]||{};const d=element('div','',`source ${s.ok?'ok':''}`);d.append(element('strong',label),element('p',s.detail||'等待连接'));return d;}));
 text('request-count',`${requests.length} 条`);
 const body=$('request-body');
 if(requests.length){body.replaceChildren(...requests.map(r=>{const tr=document.createElement('tr');const labels=[timeLabel(r.ts),r.model,`${r.promptTokensCount??'—'} / ${r.predictedTokensCount??'—'}`,`${fmt(r.tokensPerSecond)} tok/s`,`${fmt(r.timeToFirstTokenSec==null?null:r.timeToFirstTokenSec*1000)} ms`,`${fmt(r.totalTimeSec,2)} s`,r.stopReason||'—'];labels.forEach((v,i)=>tr.append(element('td',v,i===1?'model':undefined)));return tr;}));}
 else{const tr=document.createElement('tr'),td=element('td','暂无请求。启动 LM Studio 并进行一次对话后，统计会自动出现。','empty');td.colSpan=7;tr.append(td);body.replaceChildren(tr);}
 if(!paused&&data.ts-lastHistory>=1){
  const allSlots=Object.values(data.live).filter(l=>!l.stale&&data.lm_connected).flatMap(l=>Object.values(l.slots||{})).filter(s=>s.active&&data.ts-(s.updated_at||0)<12);
  history.push({ts:data.ts,gpu:data.gpu,decode_tps:allSlots.some(s=>s.decode_tps!=null)?allSlots.reduce((n,s)=>n+(s.decode_tps||0),0):null});lastHistory=data.ts;history=history.filter(h=>h.ts>data.ts-86400);
 }
 if(!paused)draw();
}
function draw(){
 const canvas=$('chart'),box=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
 canvas.width=Math.round(box.width*ratio);canvas.height=Math.round(box.height*ratio);
 const c=canvas.getContext('2d');c.scale(ratio,ratio);
 const w=box.width,h=box.height,left=42,right=85,top=27,bottom=29,pw=w-left-right,ph=h-top-bottom;
 if(pw<1)return;
 const end=paused?(history.at(-1)?.ts||Date.now()/1000):Date.now()/1000,start=end-range;
 const rows=history.filter(x=>x.ts>=start&&x.ts<=end),colors=['#6ce4bf','#6aa8ff','#b595f8','#e9b277'];
 $('chart-empty').style.display=rows.length?'none':'grid';
 const series=rows.map(row=>{const g=row.gpu.find(g=>g.uuid===gpuId)||{};return {ts:row.ts,v:[row.decode_tps,g.utilization,g.memory_used==null?null:g.memory_used/1024,g.power]};});
 const max=(i,floor)=>Math.max(floor,...series.map(r=>r.v[i]||0))*1.15;
 const scales=[max(0,10),100,max(2,1),max(3,20)];
 c.font='10px system-ui';c.lineWidth=1;
 for(let i=0;i<=4;i++){const y=top+ph*i/4;c.strokeStyle='#23303e';c.beginPath();c.moveTo(left,y);c.lineTo(w-right,y);c.stroke();c.fillStyle='#6f8095';c.textAlign='right';c.fillText(fmt(scales[0]*(1-i/4),0),left-8,y+3);}
 c.fillStyle=colors[0];c.textAlign='left';c.fillText('tok/s',left,13);
 c.fillStyle='#8192a8';c.fillText('各曲线独立刻度',Math.max(left+42,w-190),13);
 for(let j=0;j<4;j++){
  c.strokeStyle=colors[j];c.lineWidth=1.7;c.beginPath();let prev=null;
  for(const row of series){const val=row.v[j];if(val==null){prev=null;continue;}const x=left+(row.ts-start)/range*pw,y=top+ph*(1-val/scales[j]);if(!prev||row.ts-prev.ts>15)c.moveTo(x,y);else c.lineTo(x,y);prev=row;}
  c.stroke();c.fillStyle=colors[j];c.textAlign='left';c.fillText(`${fmt(scales[j],j===2?1:0)} ${['tok/s','%','GiB','W'][j]}`,w-right+10,top+12+j*18);
 }
 c.fillStyle='#6f8095';for(let i=0;i<=4;i++){c.textAlign=i===0?'left':i===4?'right':'center';c.fillText(timeLabel(start+range*i/4),left+pw*i/4,h-7);}
 text('chart-time',`${timeLabel(start)} — ${timeLabel(end)}`);
}
$('model-select').addEventListener('change',e=>{modelId=e.target.value;if(state)render(state);});
$('gpu-select').addEventListener('change',e=>{gpuId=e.target.value;if(state)render(state);});
document.querySelectorAll('[data-range]').forEach(b=>b.addEventListener('click',()=>{range=Number(b.dataset.range);document.querySelectorAll('[data-range]').forEach(x=>x.classList.toggle('selected',x===b));draw();}));
$('pause').addEventListener('click',()=>{paused=!paused;$('pause').setAttribute('aria-pressed',String(paused));text('pause',paused?'继续图表':'暂停图表');if(!paused)loadHistory();draw();});
$('export').addEventListener('click',()=>{if(!state)return;const blob=new Blob([JSON.stringify({exported_at:new Date().toISOString(),state,history},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`lmstudio-metrics-${Date.now()}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
async function loadHistory(){try{const r=await fetch('/api/history');if(!r.ok)throw Error();const records=await r.json();const recent=history.filter(x=>x.ts>(records.at(-1)?.ts||0));history=[...records,...recent];draw();}catch{ text('chart-time','历史读取失败；实时采集继续');}}
const stream=new EventSource('/api/events');stream.onmessage=e=>{try{render(JSON.parse(e.data));}catch(err){console.error('无法呈现采集数据',err);}};stream.onerror=()=>{$('connection').className='badge warn';text('connection','采集器断开 · 重连中');text('decode','—');text('decode-note','连接中断，数据暂停更新');};
new ResizeObserver(()=>draw()).observe($('chart').parentElement);loadHistory();
