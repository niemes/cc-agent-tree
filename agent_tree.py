#!/usr/bin/env python3
"""Agent Tree: a read-only live Claude Code terminal observer."""
import argparse
import json
import os
from pathlib import Path
import select
import shutil
import sys
import time
from model import Session, State, Watcher, discover
from ui import Canvas, picker, render

DEFAULT_CONFIG = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home()/'.claude')).expanduser()
DEFAULT_EVENTS = Path(os.environ.get('AGENT_TREE_EVENTS_DIR', Path.home()/'.local/state/agent-tree/events')).expanduser()


class Demo:
    def __init__(self):
        self.session = Session('demo-session', Path('/demo/session.jsonl'), '~/work/agent-tree · DEMO', 'Synthetic demo', time.time())
        self.start = time.monotonic()
        self.step = -1
        self.state = State()
        self.events = self.build()

    @staticmethod
    def build():
        events=[]
        def add(event, aid='main', **kw):
            events.append(dict(hook_event_name=event,agent_id=aid,**kw))
        add('SessionStart',model='claude-sonnet')
        add('UserPromptSubmit',prompt='Review the auth flow and fix the failing test')
        for aid,name,model in [('worker','worker','claude-sonnet'),('explorer','explorer','claude-haiku'),('advisor','advisor','claude-opus')]:
            add('SubagentStart',aid,agent_type=name,parent_agent_id='main')
            add('SessionStart',aid,model=model)
        add('PreToolUse','explorer',tool_use_id='r1',tool_name='Read',tool_input={'file_path':'src/auth.ts'})
        add('PreToolUse','worker',tool_use_id='b1',tool_name='Bash',tool_input={'command':'npm test -- auth'})
        add('PostToolUse','explorer',tool_use_id='r1')
        add('PreToolUse','explorer',tool_use_id='g1',tool_name='Grep',tool_input={'pattern':'verifyToken'})
        add('PostToolUse','explorer',tool_use_id='g1')
        add('PostToolUseFailure','worker',tool_use_id='b1')
        add('PreToolUse','advisor',tool_use_id='r2',tool_name='Read',tool_input={'file_path':'tests/auth.test.ts'})
        add('PostToolUse','advisor',tool_use_id='r2')
        add('SubagentStop','advisor')
        add('PreToolUse','worker',tool_use_id='e1',tool_name='Edit',tool_input={'file_path':'tests/auth.test.ts'})
        add('PostToolUse','worker',tool_use_id='e1')
        add('SubagentStop','explorer')
        add('PreToolUse','worker',tool_use_id='b2',tool_name='Bash',tool_input={'command':'npm test -- auth'})
        add('PostToolUse','worker',tool_use_id='b2')
        add('SubagentStop','worker')
        add('Stop')
        return events

    def poll(self):
        step=int((time.monotonic()-self.start)/0.9)
        if step >= len(self.events)+4:
            self.start=time.monotonic(); self.step=-1; self.state=State(); step=0
        while self.step < min(step,len(self.events)-1):
            self.step+=1
            self.state.hook(self.events[self.step])


class Terminal:
    def __enter__(self):
        import termios, tty
        self.buffer=''
        self.saved=termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write('\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J'); sys.stdout.flush()
        return self

    def __exit__(self,*args):
        import termios
        termios.tcsetattr(sys.stdin.fileno(),termios.TCSADRAIN,self.saved)
        sys.stdout.write('\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l'); sys.stdout.flush()

    def key(self):
        if select.select([sys.stdin],[],[],0)[0]:
            self.buffer+=os.read(sys.stdin.fileno(),64).decode('utf-8',errors='ignore')
        if not self.buffer:
            return ''
        if self.buffer=='\x1b' and select.select([sys.stdin],[],[],0.03)[0]:
            self.buffer+=os.read(sys.stdin.fileno(),32).decode(errors='ignore')
        for sequence in ('\x1b[A','\x1b[B','\x1b[C','\x1b[D','\x1b[Z'):
            if self.buffer.startswith(sequence):
                self.buffer=self.buffer[len(sequence):]
                return sequence
        key,self.buffer=self.buffer[0],self.buffer[1:]
        return key


def run(args):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit('Run Agent Tree in an interactive terminal. Try --list or --snapshot for non-interactive use.')
    watch=None
    if args.demo:
        watch=Demo()
    elif args.session:
        path=Path(args.session).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f'Transcript not found: {path}')
        from model import metadata
        project,title=metadata(path)
        watch=Watcher(Session(path.stem,path,project,title,path.stat().st_mtime),args.events_dir)
    sessions=[]; filtered=[]; index=0; query=''; selected=0; paused=False; detail=False; show_inactive=False
    scan=0; previous=''
    with Terminal() as terminal:
        while True:
            tick=time.monotonic()
            key=terminal.key()
            if key=='\x03':
                break
            if watch is None:
                if key=='\x1b':
                    break
                if key in ('\x1b[A','\x1b[B'):
                    index=max(0,min(len(filtered)-1,index+(-1 if key.endswith('A') else 1)))
                elif key in ('\n','\r') and filtered:
                    watch=Watcher(filtered[index],args.events_dir); selected=0; paused=False
                elif key=='d' and not query:
                    watch=Demo(); selected=0; paused=False
                elif key=='\x15':
                    query=''; index=0
                elif key in ('\x7f','\b'):
                    query=query[:-1]; index=0
                elif key and not key.startswith('\x1b') and key.isprintable():
                    query+=key; index=0
                if tick>=scan:
                    current=filtered[index].id if filtered and index<len(filtered) else None
                    sessions=discover(args.claude_dir); scan=tick+3
                    if current:
                        index=next((i for i,s in enumerate(sessions) if s.id==current),index)
                filtered=[s for s in sessions if query.lower() in (s.project+' '+s.title+' '+s.id).lower()]
                index=max(0,min(index,len(filtered)-1))
            else:
                if key in ('q','\x1b'):
                    break
                if key=='s':
                    watch=None; scan=0; previous=''; continue
                if key==' ':
                    paused=not paused
                agents=watch.state.visible_agents(show_inactive)
                selected=min(selected,len(agents)-1)
                focus_id=agents[selected].id
                if key=='i':
                    show_inactive=not show_inactive
                    agents=watch.state.visible_agents(show_inactive)
                    selected=next((i for i,a in enumerate(agents) if a.id==focus_id),0)
                if key in ('\t','\x1b[C','\x1b[B'):
                    selected=(selected+1)%len(agents)
                if key in ('\x1b[Z','\x1b[D','\x1b[A'):
                    selected=(selected-1)%len(agents)
                focus_id=agents[selected].id
                if key in ('\n','\r'):
                    detail=not detail
                if not paused:
                    watch.poll()
                selected=next((i for i,a in enumerate(watch.state.visible_agents(show_inactive)) if a.id==focus_id),0)
            size=shutil.get_terminal_size((120,46))
            if watch is None:
                c=Canvas(size.columns,size.lines)
                picker(c,filtered,index,query,args.claude_dir)
            else:
                c=render(size.columns,size.lines,state=watch.state,session=watch.session,selected=selected,paused=paused,demo=isinstance(watch,Demo),detail=detail,show_inactive=show_inactive)
            frame=c.ansi()
            if frame!=previous:
                sys.stdout.write(frame);sys.stdout.flush();previous=frame
            time.sleep(max(0,1/args.fps-(time.monotonic()-tick)))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--claude-dir',type=Path,default=DEFAULT_CONFIG,help='Claude config directory (default: CLAUDE_CONFIG_DIR or ~/.claude)')
    p.add_argument('--events-dir',type=Path,default=DEFAULT_EVENTS,help='Optional hook event directory')
    p.add_argument('--session',help='Attach directly to a transcript JSONL path')
    p.add_argument('--demo',action='store_true',help='Clearly labelled synthetic animation; no Claude needed')
    p.add_argument('--list',action='store_true',help='List discovered sessions as JSON')
    p.add_argument('--fps',type=int,choices=range(1,31),default=12,metavar='1..30')
    p.add_argument('--snapshot',type=Path,help='Write an SVG of the demo using the same cell renderer')
    args=p.parse_args()
    args.claude_dir=args.claude_dir.expanduser();args.events_dir=args.events_dir.expanduser()
    if args.list:
        print(json.dumps([dict(id=s.id,path=str(s.path),project=s.project,title=s.title,modified=s.modified) for s in discover(args.claude_dir)],indent=2));return
    if args.snapshot:
        demo=Demo()
        for event in demo.events[:10]:
            demo.state.hook(event)
        args.snapshot.write_text(render(124,48,state=demo.state,session=demo.session,selected=1,demo=True).svg())
        return
    try:
        run(args)
    except KeyboardInterrupt:
        pass


if __name__=='__main__':
    main()
