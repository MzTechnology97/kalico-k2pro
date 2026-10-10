#!/usr/bin/env python3
"""Host-only mock tests. No real printer/serial connection."""
from pathlib import Path
import importlib.util,types,sys
p=Path(__file__).resolve().parent
extras=types.ModuleType('extras');extras.__path__=[]
proto=types.ModuleType('extras.box_protocol');proto.ProtocolError=type('ProtocolError',(Exception,),{})
serial=types.ModuleType('extras.serial_485');serial.build_485_body=lambda *a,**k:b''
extras.box_protocol=proto;sys.modules.update({'extras':extras,'extras.box_protocol':proto,'extras.serial_485':serial})
spec=importlib.util.spec_from_file_location('runtime_v319',p.parents[0]/'klippy/extras/box_cfs_runtime.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Driver:
 def __init__(self):self.calls=[];self.values={i:s.default for i,s in enumerate(m.PARAMETERS)};self.values[6]=0
 def probe(self):self.calls.append(('probe',));return {'version':2,'count':28,'features':0xF7,'override_mask':0}
 def info_v2(self):return self.probe()
 def get_v2(self,i):self.calls.append(('get',i));return self.values[i]
 def set_v2(self,i,v):self.calls.append(('set',i,v));assert i>=7;self.values[i]=v
 def reset_v2(self,i):self.calls.append(('reset',i));self.values[i]=m.PARAMETERS[i].default
 def describe_v2(self,i):raise AssertionError('v3.19 has no DESCRIBE')
class G:
 def __init__(self,params=None):self.params=params or {};self.lines=[]
 def get(self,name,default=None):return self.params.get(name,default)
 def get_int(self,name,default=None,minval=None,maxval=None):return int(self.params.get(name,default))
 def respond_info(self,line):self.lines.append(line)
 def error(self,msg):return ValueError(msg)
d=Driver();x=m.BoxCfsRuntime.__new__(m.BoxCfsRuntime);x._driver=lambda:d;x._probe=lambda dr:dr.probe();x._record_meta=lambda meta:None;x.overrides={2:220,7:3300};x.last_values={};x.last_error=None;x.override_mask=0
try:x._apply_configured();raise AssertionError('v3.19 auto apply must fail closed')
except m.CfsRuntimeUnsupported:pass
assert not any(c[0]=='set' for c in d.calls)
try:x.cmd_apply(G());raise AssertionError('bulk apply should refuse v3.19')
except ValueError:pass
assert not any(c[0]=='set' for c in d.calls)
try:x.cmd_set(G({'PARAM':'feeder_forward_speed','VALUE':'200'}));raise AssertionError('stock speed SET must fail')
except ValueError:pass
x.cmd_set(G({'PARAM':'hub_transition_wait_ms','VALUE':'3300'}))
assert d.values[7]==3300
assert ('set',7,3300) in d.calls
x.cmd_reset(G({'PARAM':'hub_transition_wait_ms'}))
assert d.values[7]==3200
try:x.cmd_reset(G({'PARAM':'feeder_forward_speed'}));raise AssertionError('stock reset must fail')
except ValueError:pass
x.cmd_diag(G({'ALL':1}))
assert not any(c[0]=='set' and c[1] in range(7) for c in d.calls)
print('HOST V319 PASS: auto/bulk blocked; advanced manual SET/RESET allowed; stock writes rejected; GET-only supported')
