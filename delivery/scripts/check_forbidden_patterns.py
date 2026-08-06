#!/usr/bin/env python3
from __future__ import annotations
import argparse,re,sys
from pathlib import Path
PATTERNS=[
 ('default_secret',re.compile(r'beidou-(?:testnet-)?default-key|default[_-]?secret',re.I)),
 ('mainnet_url',re.compile(r'https?://(?:fapi|api)\.binance\.com',re.I)),
 ('swallowed_exception',re.compile(r'except\s+Exception(?:\s+as\s+\w+)?\s*:\s*(?:pass|continue)')),
]
NETWORK=re.compile(r'\b(?:urllib|requests|httpx|aiohttp)\b|/fapi/')
ALLOWED_NETWORK=('beidou_exchange/', 'tests/', 'tools/', 'scripts/')
def main()->int:
 p=argparse.ArgumentParser();p.add_argument('--repo',default='.');p.add_argument('--strategy',action='store_true');a=p.parse_args();root=Path(a.repo); findings=[]
 for f in root.rglob('*.py'):
  rel=f.as_posix()
  if any(x in rel for x in ('/.git/','/.venv/','/__pycache__/')):continue
  try:s=f.read_text(encoding='utf-8')
  except Exception as e: findings.append((rel,0,'read_error',str(e)));continue
  try:compile(s,rel,'exec')
  except SyntaxError as e: findings.append((rel,e.lineno or 0,'syntax_error',e.msg));continue
  for name,pat in PATTERNS:
   for m in pat.finditer(s): findings.append((rel,s[:m.start()].count('\n')+1,name,m.group(0)[:80]))
  if NETWORK.search(s) and not any(rel.startswith(x) or '/'+x in rel for x in ALLOWED_NETWORK): findings.append((rel,1,'network_bypass','network/endpoint outside exchange adapter'))
 if findings:
  for x in findings: print('%s:%s [%s] %s'%x)
  return 1
 print('PASS: no forbidden patterns');return 0
if __name__=='__main__':raise SystemExit(main())
