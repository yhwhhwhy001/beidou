#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, os, subprocess, sys
from pathlib import Path

def sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def git(*args: str) -> str:
    try: return subprocess.check_output(['git',*args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception: return 'UNKNOWN'

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument('--task', required=True); p.add_argument('--agent-id', default=os.getenv('BEIDOU_AGENT_ID','unknown-agent')); p.add_argument('--config', default=''); p.add_argument('command', nargs=argparse.REMAINDER)
    a=p.parse_args(); cmd=a.command[1:] if a.command and a.command[0]=='--' else a.command
    if not cmd: p.error('command required after --')
    root=Path('artifacts/evidence')/a.task; root.mkdir(parents=True, exist_ok=True)
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'); out=root/f'{stamp}.stdout.log'; err=root/f'{stamp}.stderr.log'; manifest=root/f'{stamp}.manifest.json'
    started=dt.datetime.now(dt.timezone.utc).isoformat()
    with out.open('wb') as fo, err.open('wb') as fe: rc=subprocess.call(cmd, stdout=fo, stderr=fe)
    finished=dt.datetime.now(dt.timezone.utc).isoformat()
    config_hash='';
    if a.config and Path(a.config).exists(): config_hash=sha(Path(a.config))
    data={'task_id':a.task,'commit':git('rev-parse','HEAD'),'branch':git('branch','--show-current'),'config_hash':config_hash,'policy_versions':{},'command':cmd,'exit_code':rc,'started_at':started,'finished_at':finished,'stdout_path':str(out),'stderr_path':str(err),'stdout_sha256':sha(out),'stderr_sha256':sha(err),'artifact_hashes':{},'agent_id':a.agent_id,'status':'PASS' if rc==0 else 'FAIL'}
    manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); print(manifest); return rc
if __name__=='__main__': raise SystemExit(main())
