#!/usr/bin/env python3
"""Best-effort observational hook: always silent; never returns a decision."""
import json
import os
from pathlib import Path
import re
import sys
import time


def main():
    payload=json.load(sys.stdin)
    sid=str(payload.get('session_id',''))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',sid):
        return
    root=Path(os.environ.get('AGENT_TREE_EVENTS_DIR',Path.home()/'.local/state/agent-tree/events')).expanduser()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    allowed=('hook_event_name','session_id','agent_id','agent_type','parent_agent_id','tool_use_id','tool_name','model','from_model','to_model')
    row={key:payload[key] for key in allowed if key in payload}
    row['observed_at']=time.time()
    # Store only a short action label, never code, tool output or prompts.
    inp=payload.get('tool_input',{})
    if isinstance(inp,dict):
        row['tool_input']={key:str(inp[key])[:240] for key in ('file_path','description','pattern') if key in inp}
    data=(json.dumps(row,ensure_ascii=True)+'\n').encode()
    fd=os.open(root/(sid+'.jsonl'),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    try:
        import fcntl
        fcntl.flock(fd,fcntl.LOCK_EX)
        os.write(fd,data)
    finally:
        os.close(fd)


if __name__=='__main__':
    try:
        main()
    except Exception:
        pass
