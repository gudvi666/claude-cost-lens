const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { startMenuBar } = require('../bin/menubar.cjs');

async function native({ built = false, tools = true, answers = [], buildStatus = 0 } = {}) {
  const calls = [];
  let installed = tools;
  const result = await startMenuBar({
    root: '/fixture', exists: () => built,
    findPython: () => ({ command: 'fixture-python', args: [] }),
    prompt: async () => answers.length ? answers.shift() : null,
    run: (command, args) => {
      calls.push([command, args]);
      if (command === 'xcode-select' && args[0] === '--install') { installed = true; return { status: 0 }; }
      if (command === 'xcode-select' || command === 'xcrun') return { status: installed ? 0 : 1 };
      return { status: command === 'fixture-python' ? buildStatus : 0 };
    }
  });
  return { result, calls };
}
test('existing app opens without probing or installing development tools', async () => {
  const {result,calls}=await native({built:true,tools:false});
  assert.equal(result.code,0); assert.deepEqual(calls.map(c=>c[0]),['open']);
});
test('available toolchain builds before opening; does not open browser',async()=>{
  const {result,calls}=await native();assert.equal(result.code,0);
  assert.deepEqual(calls.map(c=>c[0]),['xcode-select','xcrun','xcrun','fixture-python','open']);
});
test('missing tools Web selection neither installs nor builds',async()=>{
  const {result,calls}=await native({tools:false,answers:['2']});
  assert.equal(result.web,true);assert.equal(calls.length,1);
});
test('install requires selection; successful recheck builds and opens',async()=>{
  const {result,calls}=await native({tools:false,answers:['1','']});
  assert.equal(result.code,0);assert(calls.some(c=>c[0]==='xcode-select'&&c[1][0]==='--install'));
  assert.equal(calls.at(-1)[0],'open');
});
test('cancel and noninteractive modes do not trigger installation',async()=>{
  for(const answers of [['0'],[]]){
    const expected=answers.length===0?1:0;
    const {result,calls}=await native({tools:false,answers});
    assert.equal(result.code,expected);assert.equal(calls.length,1);
  }
});
test('build failure offers explicit Web fallback',async()=>{
  const {result,calls}=await native({buildStatus:1,answers:['w']});
  assert.equal(result.web,true);assert(!calls.some(c=>c[0]==='open'));
});

async function cli(args, platform='darwin', nativeResult={code:0}, probe=()=>({status:0})) {
  const source=fs.readFileSync(path.join(__dirname,'../bin/claude-cost-lens.cjs'),'utf8');
  const calls=[];
  const probes=[];
  const stop={};
  const proc={argv:['node','cli',...args],platform,env:{},once(){},removeAllListeners(){}};
  proc.exit=code=>{proc.exitCode=code;throw stop;};
  const child={on(){return this;},kill(){}};
  try { vm.runInNewContext(source,{
    __filename:'/fixture/bin/claude-cost-lens.cjs',process:proc,console:{log(){},error(){}},
    require(name){
      if(name==='node:fs')return {existsSync:()=>true};
      if(name==='node:path')return path;
      if(name==='node:child_process')return {spawnSync:(command,argv)=>{probes.push({command,argv});return probe(command,argv);},spawn:(command,argv)=>{calls.push({command,argv});return child;}};
      if(name==='./windows-tray.cjs')return {startWindowsTray:async options=>{const python=options.findPython();if(!python)return {code:1};calls.push({tray:true,python});return {code:0};}};
      if(name==='./menubar.cjs')return {startMenuBar:async()=>{calls.push({native:true});return nativeResult;}};
      throw Error(name);
    }
  }); } catch(error) { if(error!==stop) throw error; }
  await new Promise(resolve=>setImmediate(resolve));
  return {calls,proc,probes};
}
test('macOS no-argument CLI routes only to native launcher',async()=>{
  assert.deepEqual((await cli([])).calls,[{native:true}]);
});
test('native Web selection runs browser backend',async()=>{
  const {calls}=await cli([],'darwin',{web:true});
  assert.equal(calls.length,2);assert(calls[1].argv.includes('--open'));
});
test('explicit Web bypasses native and removes launcher-only flag',async()=>{
  const {calls}=await cli(['--web','--port','8787']);
  assert.equal(calls.length,1);assert(!calls[0].argv.includes('--web'));
  assert(calls[0].argv.includes('--open'));assert(calls[0].argv.includes('8787'));
});
test('once stays noninteractive; Linux is rejected before Python lookup',async()=>{
  assert(!(await cli(['--once'])).calls[0].argv.includes('--open'));
  const {calls,probes,proc}=await cli([],'linux');
  assert.equal(proc.exitCode,1);assert.equal(calls.length,0);assert.equal(probes.length,0);
});

test('Windows default uses tray with py -3, without browser startup',async()=>{
  const {calls}=await cli([],'win32');
  assert.equal(calls.length,1);assert(calls[0].tray);assert.equal(calls[0].python.command,'py');
  assert.deepEqual(Array.from(calls[0].python.args),['-3']);
});
test('Windows Python detection falls back from py to python',async()=>{
  const {calls,probes}=await cli([],'win32',{code:0},command=>({status:command==='python'?0:1}));
  assert.deepEqual(probes.map(p=>p.command),['py','python']);
  assert.equal(calls[0].python.command,'python');
});
test('Windows menu option fails with Web guidance without probing Python',async()=>{
  const {calls,probes,proc}=await cli(['--menubar'],'win32');
  assert.equal(proc.exitCode,1);assert.equal(calls.length,0);assert.equal(probes.length,0);
});
test('Windows once remains noninteractive without a browser',async()=>{
  const {calls}=await cli(['--once'],'win32');
  assert.equal(calls.length,1);assert(!calls[0].argv.includes('--open'));
});
test('Windows without a compatible Python exits before spawning backend',async()=>{
  const {calls,proc}=await cli([],'win32',{code:0},()=>({status:1}));
  assert.equal(calls.length,0);assert.equal(proc.exitCode,1);
});

test('Windows explicit Web bypasses tray and opens browser',async()=>{
  const {calls}=await cli(['--web'],'win32');
  assert.equal(calls.length,1);assert.equal(calls[0].command,'py');
  assert(calls[0].argv.includes('--open'));assert(!calls[0].argv.includes('--web'));
});
test('Windows explicit tray works; macOS tray and mixed modes reject',async()=>{
  assert((await cli(['--tray'],'win32')).calls[0].tray);
  assert.equal((await cli(['--tray'],'darwin')).proc.exitCode,1);
  assert.equal((await cli(['--tray','--web'],'win32')).proc.exitCode,1);
});
