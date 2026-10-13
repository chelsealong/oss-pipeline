"""Numeric runner diagnostics, safe to retain without agent logs or credentials."""
import argparse
import json
from pathlib import Path
import shutil
import time


def sample(meminfo=Path('/proc/meminfo'), events=Path('/sys/fs/cgroup/memory.events'), disk=Path('.')):
    result={'at':time.time(),'disk_free_bytes':shutil.disk_usage(disk).free}
    try:
        memory={line.split(':')[0]:int(line.split()[1])*1024 for line in meminfo.read_text().splitlines()}
        result['memory_bytes']={key:memory[key] for key in ('MemTotal','MemAvailable','SwapTotal','SwapFree') if key in memory}
    except (OSError,ValueError,IndexError):result['memory_bytes']=None
    try:result['memory_events']={line.split()[0]:int(line.split()[1]) for line in events.read_text().splitlines()}
    except (OSError,ValueError,IndexError):result['memory_events']=None
    return result


def process_memory(proc=Path('/proc')):
    """Only PID, executable name and RSS; never argv, environments or contents."""
    rows=[]
    for path in proc.glob('[0-9]*/status'):
        try:
            data=dict(line.split(':',1) for line in path.read_text().splitlines() if ':' in line)
            rss=int(data.get('VmRSS','0 kB').split()[0])*1024
            if rss:rows.append({'pid':int(path.parent.name),'name':data['Name'].strip(),'rss_bytes':rss})
        except (OSError,ValueError,KeyError):continue
    return sorted(rows,key=lambda row:row['rss_bytes'],reverse=True)[:10]


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    while True:
        line=json.dumps({'runner_resources':sample(),'process_memory':process_memory()})
        with args.output.open('a') as out:out.write(line+'\n')
        print(line,flush=True)
        time.sleep(15)
