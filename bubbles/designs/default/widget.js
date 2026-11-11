var I=__I18N__;
var W=window.TelegramWidget,S={},format='mp3',busy=false;
function text(id,value){document.getElementById(id).textContent=value||'';}
function ask(data){
  if(!W||busy)return;
  busy=true;text('feedback',I.requesting);
  W.request(data).then(function(a){text('feedback',a.result||'');busy=false;},function(e){
    busy=false;var errors={TIMEOUT:I.timeout,RATE_LIMITED:I.rate_limit,BUSY:I.busy,FAILED:I.failed};
    text('feedback',errors[e.code]||I.unavailable);
  });
}
function selectFormat(value){format=value;document.getElementById('mp3').className=value==='mp3'?'active':'';document.getElementById('mp4').className=value==='mp4'?'active':'';text('download',value==='mp3'?I.download_audio:I.download_video);}
function download(index,f){ask('dl|'+index+'|'+(f||format));}
var problems=I.problems;
function recover(){var p=problems[S.error];if(p)ask(p[5]);}
function render(){
  try{S=JSON.parse(W.chatData)||{};}catch(e){S={};}
  var view=S.error?'error':S.view||'format';
  ['format','search','progress','error'].forEach(function(v){document.getElementById(v+'-view').style.display=v===view?'block':'none';});
  document.querySelectorAll('.media-title').forEach(function(el){el.textContent=S.title||I.recording;});
  document.querySelectorAll('.media-meta').forEach(function(el){el.textContent=[S.artist,S.duration].filter(Boolean).join(' · ');});
  text('feedback','');
  if(view==='error'){
    var p=problems[S.error]||problems.expired;
    ['label','icon','title','copy','primary'].forEach(function(id,i){text('error-'+id,p[i]);});
    document.getElementById('error-secondary').style.display=S.error==='send'||S.error==='source'?'block':'none';
  }else if(view==='format'){
    document.getElementById('mp3').style.display=S.audio===false?'none':'block';
    document.getElementById('mp4').style.display=S.video===false?'none':'block';
    if(S.audio===false)format='mp4';if(S.video===false)format='mp3';selectFormat(format);
  }else if(view==='search'){
    text('count',S.total+' '+I.found);
    var target=document.getElementById('results');target.textContent='';
    (S.rows||[]).forEach(function(row){
      var item=document.createElement('div');item.className='result';
      var title=document.createElement('h3');title.textContent=row.t;item.appendChild(title);
      var artist=document.createElement('p');artist.className='muted';artist.textContent=[row.a,row.d].filter(Boolean).join(' · ');item.appendChild(artist);
      ['mp3','mp4'].forEach(function(f){if(f==='mp3'&&!row.audio||f==='mp4'&&!row.video)return;var button=document.createElement('button');button.textContent=f==='mp3'?'♫ '+I.audio:'▷ '+I.video;button.onclick=function(){download(row.i,f);};item.appendChild(button);});
      target.appendChild(item);
    });
    text('page',(S.page*3+1)+'–'+Math.min(S.page*3+3,S.total)+' '+I.of+' '+S.total);
    document.getElementById('prev').disabled=S.page===0;document.getElementById('next').disabled=(S.page+1)*3>=S.total;
  }else{
    var stage=S.stage||'downloading',names={downloading:I.download,converting:I.convert,sending:I.send,done:I.done};
    text('stage',names[stage]);text('filetype',(S.format||'').toUpperCase());
    var percent=Math.max(0,Math.min(100,Number(S.percent)||0));
    text('percent',stage==='done'?'✓':stage==='sending'?'↑':percent+'%');
    document.getElementById('fill').style.width=percent+'%';text('eta',S.eta);text('size',S.size);text('speed',S.speed);
    var active={downloading:0,converting:1,sending:2,done:3}[stage];for(var i=0;i<3;i++)document.getElementById('step'+i).className=i<=active?'active-step':'';
    text('progress-hint',stage==='done'?I.delivered:I.working);
  }
}
document.querySelectorAll('[data-i18n]').forEach(function(el){el.textContent=I[el.getAttribute('data-i18n')]||'';});
if(W){W.onEvent('chatDataChanged',render);render();W.ready();}
