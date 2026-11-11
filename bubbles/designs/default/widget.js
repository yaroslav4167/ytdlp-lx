var I=__I18N__,W=window.TelegramWidget,S={},C={},selected=0,busy=false,phase='';
function el(id){return document.getElementById(id);}
function text(id,value){el(id).textContent=value||'';}
function ask(data){
  if(!W||busy)return;
  busy=true;text('feedback',I.requesting);
  W.request(data).then(function(a){busy=false;text('feedback',S.view==='progress'&&!S.error?'':a.result||'');},function(e){
    busy=false;
    if(S.view==='progress'&&!S.error){text('feedback','');return;}
    var errors={TIMEOUT:I.timeout,RATE_LIMITED:I.rate_limit,BUSY:I.busy,FAILED:I.timeout};
    text('feedback',errors[e.code]||I.unavailable);
  });
}
function download(index,f){ask('dl|'+index+'|'+f);}
function recover(){if(S.problem)ask(S.problem[5]);}
function images(){
  try{C=JSON.parse(W.publicData)||{};}catch(e){C={};}
  document.querySelectorAll('[data-cover]').forEach(function(box){
    box.textContent='♫';var src=C[box.getAttribute('data-cover')];
    if(!src&&C.atlas){var i=Number(box.getAttribute('data-cover'));box.textContent='';box.style.backgroundImage='url('+C.atlas+')';box.style.backgroundSize='400% 200%';box.style.backgroundPosition=(i%4*100/3)+'% '+(Math.floor(i/4)*100)+'%';return;}
    box.style.backgroundImage='none';
    if(src){var img=document.createElement('img');img.src=src;box.textContent='';box.appendChild(img);}
  });
}
function pick(index){
  if(busy)return;
  selected=index;
  document.querySelectorAll('.result').forEach(function(button,i){
    var active=S.rows[i][0]===selected;
    button.className='result'+(active?' selected':'');button.setAttribute('aria-selected',String(active));
  });
  var row=(S.rows||[]).find(function(r){return r[0]===selected;});
  if(row){el('search-audio').style.display=(row[4]&1)?'block':'none';el('search-video').style.display=(row[4]&2)?'block':'none';}
}
function search(){
  var rows=S.rows||[];
  if(!rows.some(function(r){return r[0]===selected;}))selected=rows.length?rows[0][0]:0;
  text('count',S.range||S.total+' '+I.found);el('results').textContent='';
  rows.forEach(function(row){
    var button=document.createElement('div');button.setAttribute('role','option');button.setAttribute('aria-selected',String(selected===row[0]));button.className='result'+(selected===row[0]?' selected':'');button.onclick=function(){pick(row[0]);};
    var cover=document.createElement('span');cover.className='mini';cover.setAttribute('data-cover',String(row[0]));button.appendChild(cover);
    var content=document.createElement('span');content.className='result-text';
    var title=document.createElement('strong');title.textContent=row[1];content.appendChild(title);
    var meta=document.createElement('span');meta.className='muted';meta.textContent=[row[2],row[3]].filter(Boolean).join(' · ');content.appendChild(meta);
    button.appendChild(content);el('results').appendChild(button);

  });
  pick(selected);images();
}
function animate(value){
  var e=el('percent');
  if(e.textContent===value)return;
  e.textContent=value;
  if(e.animate&&!matchMedia('(prefers-reduced-motion:reduce)').matches)e.animate([{opacity:.25,transform:'translateY(4px)'},{}],220);
}
function render(){
  try{S=JSON.parse(W.chatData)||{};}catch(e){S={};}
  var view=S.error?'error':S.view||'format';
  ['format','search','progress','error','processing'].forEach(function(v){el(v+'-view').style.display=v===view?'block':'none';});
  document.querySelectorAll('.media-title').forEach(function(e){e.textContent=S.title||I.recording;});
  document.querySelectorAll('.media-meta').forEach(function(e){e.textContent=[S.artist,S.duration].filter(Boolean).join(' · ');});
  text('feedback','');
  if(view==='processing'){text('processing-title',S.stage_title);return;}
  if(view==='error'){
    var p=S.problem||['','!',I.unavailable,'',I.other_link,'input'];
    ['label','icon','title','copy','primary'].forEach(function(id,i){text('error-'+id,p[i]);});
    el('error-secondary').style.display=S.error==='send'||S.error==='source'||S.error==='uncertain'?'block':'none';
  }else if(view==='format'){
    el('mp3').style.display=S.audio===false?'none':'block';el('mp4').style.display=S.video===false?'none':'block';
    images();
  }else if(view==='search'){search();}else{
    var stage=S.stage||'downloading',active={downloading:0,converting:1,sending:2}[stage];
    text('stage',S.stage_title||el('step'+active).textContent+(S.part?' · '+S.part:''));text('filetype',(S.format||'').toUpperCase());
    var percent=Math.max(0,Math.min(100,Number(S.percent)||0));animate(stage==='sending'?I.step_send+'…':S.percent==null?'…':percent+'%');
    var key=stage+'|'+(S.part||'');el('fill').style.transition=phase!==key||S.percent==null?'none':'';phase=key;el('fill').style.width=percent+'%';
    el('fill').className=S.percent==null?'waiting':'';text('eta',S.eta);text('size',S.size);text('speed',S.speed);
    for(var i=0;i<3;i++)el('step'+i).className=i<=active?'active-step':'';
    text('progress-hint',S.detail);el('loading-cover').setAttribute('data-cover',String(S.cover_index||0));images();
  }
}
document.querySelectorAll('[data-i18n]').forEach(function(e){e.textContent=I[e.getAttribute('data-i18n')]||'';});
if(W){W.onEvent('chatDataChanged',render);W.onEvent('publicDataChanged',images);render();W.ready();}
