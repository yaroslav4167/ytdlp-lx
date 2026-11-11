var I=__I18N__,W=window.TelegramWidget,S={},C={},selected=0,busy=false;
function el(id){return document.getElementById(id);}
function text(id,value){el(id).textContent=value||'';}
function ask(data){
  if(!W||busy)return;
  busy=true;text('feedback',I.requesting);
  W.request(data).then(function(a){busy=false;text('feedback',S.view==='progress'&&!S.error?'':a.result||'');},function(e){
    busy=false;
    if(S.view==='progress'&&!S.error){text('feedback','');return;}
    var errors={TIMEOUT:I.timeout,RATE_LIMITED:I.rate_limit,BUSY:I.busy,FAILED:I.failed};
    text('feedback',errors[e.code]||I.unavailable);
  });
}
function download(index,f){ask('dl|'+index+'|'+f);}
function recover(){if(S.problem)ask(S.problem[5]);}
function images(){
  try{C=JSON.parse(W.publicData)||{};}catch(e){C={};}
  document.querySelectorAll('[data-cover]').forEach(function(box){
    box.textContent='♫';var src=C[box.getAttribute('data-cover')];
    if(src){var img=document.createElement('img');img.src=src;box.textContent='';box.appendChild(img);}
  });
}
function pick(index){selected=index;search();}
function search(){
  var rows=S.rows||[];
  if(!rows.some(function(r){return r.i===selected;}))selected=rows.length?rows[0].i:0;
  text('count',S.total+' '+I.found);el('results').textContent='';
  rows.forEach(function(row){
    var button=document.createElement('button');button.className='result'+(selected===row.i?' selected':'');button.onclick=function(){pick(row.i);};
    var cover=document.createElement('span');cover.className='mini';cover.setAttribute('data-cover',String(row.i));button.appendChild(cover);
    var content=document.createElement('span');content.className='result-text';
    var title=document.createElement('strong');title.textContent=row.t;content.appendChild(title);
    var meta=document.createElement('span');meta.className='muted';meta.textContent=[row.a,row.d].filter(Boolean).join(' · ');content.appendChild(meta);
    button.appendChild(content);el('results').appendChild(button);
    if(selected===row.i){el('search-audio').style.display=row.audio?'block':'none';el('search-video').style.display=row.video?'block':'none';}
  });
  text('page',(S.page*3+1)+'–'+Math.min(S.page*3+3,S.total)+' '+I.of+' '+S.total);
  el('prev').disabled=S.page===0;el('next').disabled=(S.page+1)*3>=S.total;images();
}
function render(){
  try{S=JSON.parse(W.chatData)||{};}catch(e){S={};}
  var done=S.stage==='done',view=S.error?'error':done?'format':S.view||'format';
  ['format','search','progress','error'].forEach(function(v){el(v+'-view').style.display=v===view?'block':'none';});
  document.querySelectorAll('.media-title').forEach(function(e){e.textContent=S.title||I.recording;});
  document.querySelectorAll('.media-meta').forEach(function(e){e.textContent=[S.artist,S.duration,done?(S.format||'').toUpperCase():'',done?S.size:''].filter(Boolean).join(' · ');});
  text('feedback','');
  if(view==='error'){
    var p=S.problem||['','!',I.unavailable,'',I.other_link,'input'];
    ['label','icon','title','copy','primary'].forEach(function(id,i){text('error-'+id,p[i]);});
    el('error-secondary').style.display=S.error==='send'||S.error==='source'||S.error==='uncertain'?'block':'none';
  }else if(view==='format'){
    el('formats').style.display=done?'none':'flex';
    text('format-label',done?'✓ '+I.done:I.ready);
    document.querySelector('[data-cover]').setAttribute('data-cover',String(S.cover_index||0));
    el('mp3').style.display=S.audio===false?'none':'block';el('mp4').style.display=S.video===false?'none':'block';
    images();
  }else if(view==='search'){search();}else{
    var stage=S.stage||'downloading',names={downloading:I.download,converting:I.convert,sending:I.send,done:I.done};
    text('stage',names[stage]);text('filetype',(S.format||'').toUpperCase());
    var percent=Math.max(0,Math.min(100,Number(S.percent)||0));text('percent',stage==='done'?'✓':stage==='sending'?'↑':percent+'%');
    el('fill').style.width=percent+'%';text('eta',S.eta);text('size',S.size);text('speed',S.speed);
    var active={downloading:0,converting:1,sending:2,done:3}[stage];
    for(var i=0;i<3;i++)el('step'+i).className=i<=active?'active-step':'';
    text('progress-hint',I.working);
  }
}
document.querySelectorAll('[data-i18n]').forEach(function(e){e.textContent=I[e.getAttribute('data-i18n')]||'';});
if(W){W.onEvent('chatDataChanged',render);W.onEvent('publicDataChanged',images);render();W.ready();}
