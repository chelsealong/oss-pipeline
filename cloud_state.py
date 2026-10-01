#!/usr/bin/env python3
"""Portable queue checkpoint. Contains task metadata, never login credentials."""
import argparse
import json
import os
from pathlib import Path
import time
import runtime as rt

TABLES = {
    'tasks': ('id','identity','kind','repo','number','note','status','created','updated','result','attempts'),
    'meta': ('key','value'),
    'calls': ('at','kind'),
}

def export_state(path):
    with rt.db() as db:
        db.execute('BEGIN')
        data = {table: [dict(row) for row in db.execute('SELECT '+','.join(cols)+' FROM '+table)]
                for table, cols in TABLES.items()}
    payload = {'version':1, 'saved_at':time.time(), 'tables':data}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n')
    os.replace(temp, path)
    return payload

def import_state(path):
    payload = json.loads(Path(path).read_text())
    if payload.get('version') != 1 or set(payload.get('tables',{})) != set(TABLES):
        raise ValueError('Unsupported or incomplete cloud checkpoint')
    for table, cols in TABLES.items():
        if not isinstance(payload['tables'][table], list):
            raise ValueError('Invalid checkpoint table')
        for row in payload['tables'][table]:
            if not isinstance(row,dict) or set(row) != set(cols):
                raise ValueError('Invalid checkpoint row')
    with rt.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if any(db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in TABLES):
            raise RuntimeError('Refusing to overwrite an existing runtime database')
        for table, cols in TABLES.items():
            db.executemany('INSERT INTO '+table+' ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',
                           [[row[col] for col in cols] for row in payload['tables'][table]])
        # A lost runner may have published just before it died. Never replay.
        db.execute("UPDATE tasks SET status='interrupted',result='Previous runner ended during task; inspect remote before retry',updated=? WHERE status='running'",(time.time(),))
        # Old heartbeat/auth success is not evidence that this runner works.
        db.execute("DELETE FROM meta WHERE key IN ('health','worker_heartbeat','watch_heartbeat','prwatch_heartbeat','detector_heartbeat')")

if __name__ == '__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('action',choices=['export','import'])
    ap.add_argument('path',type=Path)
    args=ap.parse_args()
    (export_state if args.action=='export' else import_state)(args.path)
