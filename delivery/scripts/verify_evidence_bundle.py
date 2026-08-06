#!/usr/bin/env python3
from __future__ import annotations
import json,sys,hashlib
from pathlib import Path

def sha(p):
 h=hashlib.sha256();h.update(Path(p).read_bytes());return h.hexdigest()
def main():
 root=Path(sys.argv[1] if len(sys.argv)>1 else 'artifacts/evidence'); bad=0; count=0
 for f in root.rglob('*.manifest.json'):
  count+=1; d=json.loads(f.read_text(encoding='utf-8'))
  for key in ('stdout_path','stderr_path'):
   p=Path(d[key]); expected=d[key.replace('_path','_sha256')]
   if not p.exists() or sha(p)!=expected: print('FAIL',f,key);bad+=1
 print(f'manifests={count} bad={bad}');return 1 if bad or count==0 else 0
if __name__=='__main__':raise SystemExit(main())
