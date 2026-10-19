const {test}=require('node:test');
const assert=require('node:assert/strict');
const {startWindowsTray}=require('../bin/windows-tray.cjs');

async function launch({state='ready',psMissing=false,noPython=false,interpreterMissing=false,exits=false}={}) {
  const writes=new Map(), runs=[], launches=[], cleanup=[];
  const events={};let killed=false,unref=false;
  const child={on:(event,fn)=>{events[event]=fn;},unref:()=>{unref=true;},kill:()=>{killed=true;}};
  const result=await startWindowsTray({
    root:'/fixture with spaces & symbols',tempRoot:'/own-temp',powershell:'fixture-powershell',
    findPython:()=>noPython?null:{command:'py',args:['-3']},
    fs:{
      mkdtempSync:prefix=>prefix+'fixture',
      writeFileSync:(path,value)=>writes.set(path,value),
      readFileSync:()=>{if(state===null)throw Error('not ready');return '\uFEFF'+JSON.stringify({state,message:'fixture failure'});},
      rmSync:(path,options)=>cleanup.push({path,options})
    },
    run:(command,args,options)=>{
      runs.push({command,args,options});
      return command==='py'?{status:interpreterMissing?1:0,stdout:'C:\\Python with spaces\\python.exe\r\n'}:{status:psMissing?1:0};
    },
    spawn:(command,args,options)=>{launches.push({command,args,options});return child;},
    wait:async()=>{if(exits)events.exit();}
  });
  return {result,runs,launches,writes,cleanup,killed,unref};
}
test('detached Windows host waits for readiness, passes a config file without shell interpolation',async()=>{
  const info=await launch();assert.equal(info.result.code,0);assert(info.unref);assert(!info.killed);
  const invocation=info.launches[0];assert.equal(invocation.command,'fixture-powershell');
  assert(invocation.options.detached);assert(invocation.options.windowsHide);assert.equal(invocation.options.stdio,'ignore');
  assert.equal(invocation.options.shell,undefined);assert(invocation.args.includes('-STA'));
  const config=JSON.parse([...info.writes.values()][0]);
  assert.equal(config.PythonCommand,'C:\\Python with spaces\\python.exe');assert.deepEqual(config.PythonArgs,[]);
  assert.equal(config.AppPath,'/fixture with spaces & symbols/app.py');
  assert.equal(info.cleanup[0].path,'/own-temp/claude-cost-lens-fixture');
});
test('already-running host succeeds without killing the existing tray',async()=>{
  const info=await launch({state:'already_running'});assert.equal(info.result.code,0);assert(!info.killed);
});
test('PowerShell and Python failures do not launch a host',async()=>{
  for(const options of [{psMissing:true},{noPython:true},{interpreterMissing:true}]) {
    const info=await launch(options);assert.equal(info.result.code,1);assert.equal(info.launches.length,0);
  }
});
test('startup error or timeout stops only the spawned host and clears its own temporary files',async()=>{
  for(const state of ['error',null]){
    const info=await launch({state});assert.equal(info.result.code,1);assert(info.killed);assert.equal(info.cleanup.length,1);
  }
});
test('early host exit reports failure without killing another process',async()=>{
  const info=await launch({state:null,exits:true});assert.equal(info.result.code,1);assert(!info.killed);
});
