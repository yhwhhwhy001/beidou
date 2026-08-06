#!/usr/bin/env python3
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]

def fail(msg:str)->None: print('ERROR:',msg); raise SystemExit(1)
def main()->int:
    delivery=ROOT/'delivery.yaml'
    if not delivery.exists(): fail('delivery.yaml missing')
    data=yaml.safe_load(delivery.read_text(encoding='utf-8'))
    ids=[]
    for t in data.get('tasks',[]):
        tid=t['id']; ids.append(tid); d=ROOT/t['path']
        for name in ('TASK.md','AGENT_PROMPT.md','ACCEPTANCE.yaml','ROLLBACK.md'):
            if not (d/name).exists(): fail(f'{tid}/{name} missing')
        ac=yaml.safe_load((d/'ACCEPTANCE.yaml').read_text(encoding='utf-8'))
        if ac.get('task_id')!=tid: fail(f'{tid} acceptance id mismatch')
        if not ac.get('criteria'): fail(f'{tid} has no acceptance criteria')
    if len(ids)!=20 or len(set(ids))!=20: fail(f'expected 20 unique tasks, got {len(ids)}')
    known=set(ids)
    for t in data['tasks']:
        unknown=set(t.get('dependencies',[]))-known
        if unknown: fail(f"{t['id']} unknown deps {unknown}")
    # cycle detection
    graph={t['id']:t.get('dependencies',[]) for t in data['tasks']}; visiting=set(); visited=set()
    def dfs(n):
        if n in visiting: fail(f'dependency cycle at {n}')
        if n in visited:return
        visiting.add(n)
        for x in graph[n]: dfs(x)
        visiting.remove(n); visited.add(n)
    for n in graph: dfs(n)
    json.loads((ROOT/'delivery/schemas/evidence-manifest.schema.json').read_text(encoding='utf-8'))
    print('PASS: package structure, task uniqueness, dependencies, YAML/JSON schemas')
    return 0
if __name__=='__main__': raise SystemExit(main())
