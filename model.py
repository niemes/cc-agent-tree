"""Read-only adapters for Claude JSONL transcripts and optional hook events."""
from __future__ import annotations
import json
import time
from datetime import datetime
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path


def clean(value, limit=160):
    # Never let transcript text inject terminal control sequences.
    return ''.join(c if c.isprintable() and ord(c) != 127 else ' ' for c in str(value))[:limit]


class Tail:
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.identity = None
        self.reset = False

    def read(self):
        self.reset = False
        try:
            with self.path.open('rb') as f:
                stat = self.path.stat()
                identity = (stat.st_dev, stat.st_ino)
                if self.identity is not None and (identity != self.identity or stat.st_size < self.offset):
                    self.offset = 0
                    self.reset = True
                self.identity = identity
                f.seek(self.offset)
                # Bound each tick so a huge transcript never freezes the UI.
                chunk = f.read(2 * 1024 * 1024)
                end = chunk.rfind(b'\n')
                if end < 0:
                    # Skip a pathological >2MiB line rather than wedging the watcher.
                    if len(chunk) == 2 * 1024 * 1024:
                        f.readline()
                        self.offset = f.tell()
                    return []
                self.offset += end + 1
                result = []
                for line in chunk[:end].splitlines():
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict):
                            result.append(item)
                    except (ValueError, UnicodeError):
                        continue
                return result
        except OSError:
            return []


@dataclass
class Session:
    id: str
    path: Path
    project: str
    title: str
    modified: float


def metadata(path):
    title, project = '', path.parent.name
    try:
        with path.open('rb') as f:
            head = f.read(32768)
            f.seek(max(0, path.stat().st_size - 32768))
            tail = f.read()
        for raw in (head + b'\n' + tail).splitlines():
            try:
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    continue
                project = obj.get('cwd') or project
                if obj.get('type') == 'custom-title':
                    title = clean(obj.get('customTitle', ''), 90) or title
                elif not title and obj.get('type') == 'user':
                    content = obj.get('message', {}).get('content', '')
                    if isinstance(content, str) and not content.startswith('<'):
                        title = clean(content, 90)
            except (ValueError, UnicodeError, AttributeError):
                continue
    except OSError:
        pass
    return str(project), title or 'Untitled session'


def discover(config_dir):
    root = Path(config_dir) / 'projects'
    found = []
    for path in root.glob('*/*.jsonl'):
        if path.name.startswith('agent-'):
            continue
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            pass
    sessions = []
    for modified, path in sorted(found, reverse=True)[:300]:
        project, title = metadata(path)
        sessions.append(Session(path.stem, path, project, title, modified))
    return sessions


@dataclass
class Agent:
    id: str
    name: str = 'agent'
    model: str = 'model unknown'
    status: str = 'observed'
    action: str = 'Waiting for events'
    parent: str | None = None
    updated: float = field(default_factory=time.time)
    calls: int = 0
    failures: int = 0
    inflight: set = field(default_factory=set)
    stopped: bool = False
    session_open: bool = False


class State:
    def __init__(self):
        self.agents = {'main': Agent('main', 'main session')}
        self.tools = {}
        self.counts = Counter()
        self.logs = deque(maxlen=300)
        self.seen = set()
        self.usage = {}
        self.pulses = deque(maxlen=100)
        self.hooks_seen = False
        self.last_event = 0.0
        self.prompt = ''
        self.total_events = 0
        self.event_stamp = None

    def agent(self, aid, name=None):
        aid = str(aid or 'main')
        if aid not in self.agents:
            self.agents[aid] = Agent(aid, name or ('agent ' + aid[:7]))
        if name:
            self.agents[aid].name = clean(name, 40)
        return self.agents[aid]

    def visible_agents(self, show_inactive=False):
        return [a for a in self.agents.values()
                if a.id == 'main' or show_inactive or (a.session_open and not a.stopped)]

    def log(self, aid, text, status='', live=True):
        stamp = self.event_stamp or time.time()
        self.logs.append((time.strftime('%H:%M:%S', time.localtime(stamp)), aid, clean(text), clean(status, 30)))
        self.total_events += 1
        if live:
            self.last_event = time.time()
            self.pulses.append((time.monotonic(), aid, status))

    def start_tool(self, aid, tid, name, inp, live):
        if not tid or tid in self.tools:
            return
        a = self.agent(aid)
        inp = inp if isinstance(inp, dict) else {}
        detail = inp.get('file_path') or inp.get('command') or inp.get('pattern') or inp.get('description') or inp.get('query') or ''
        self.tools[tid] = {'agent': aid, 'name': name, 'done': False}
        self.counts[name] += 1
        a.calls += 1
        a.inflight.add(tid)
        a.stopped = False
        a.session_open = a.session_open or live
        a.status, a.action, a.updated = 'working', clean(f'{name}  {detail}'), time.time()
        self.log(aid, a.action, 'started', live)

    def finish_tool(self, tid, failed, live):
        tool = self.tools.get(tid)
        if not tool or tool['done']:
            return
        tool['done'] = True
        a = self.agent(tool['agent'])
        if live and not a.stopped:
            a.session_open = True
        a.inflight.discard(tid)
        a.failures += int(bool(failed))
        a.status = 'working' if a.inflight else ('failed' if failed else 'observed')
        a.updated = time.time()
        self.log(a.id, tool['name'], 'failed' if failed else 'returned', live)

    def transcript(self, obj, aid='main', live=True):
        self.event_stamp = None
        try:
            self.event_stamp = datetime.fromisoformat(str(obj.get('timestamp', '')).replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError):
            pass
        uid = obj.get('uuid')
        key = ('row', aid, uid)
        if uid and key in self.seen:
            return
        if uid:
            self.seen.add(key)
        aid = str(obj.get('agentId') or aid)
        a = self.agent(aid)
        msg = obj.get('message') or {}
        if not isinstance(msg, dict):
            return
        if live and msg and not a.stopped:
            a.session_open = True
        model = msg.get('model')
        if model:
            a.model = clean(model, 40)
        content = msg.get('content', [])
        if isinstance(content, str):
            if obj.get('type') == 'user' and aid == 'main' and not content.startswith('<'):
                self.prompt = clean(content)
                a.status, a.stopped = 'working', False
            content = []
        if not isinstance(content, list):
            return
        usage = msg.get('usage')
        if isinstance(usage, dict) and msg.get('id'):
            self.usage[(aid, msg['id'])] = usage
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_use':
                self.start_tool(aid, block.get('id'), block.get('name', 'tool'), block.get('input', {}), live)
            elif block.get('type') == 'tool_result':
                self.finish_tool(block.get('tool_use_id'), block.get('is_error', False), live)
            elif block.get('type') == 'text' and block.get('text'):
                a.action = clean(block['text'])
        if msg.get('stop_reason') == 'end_turn' and not a.inflight:
            a.status = 'idle' if aid == 'main' else 'complete'
            a.stopped = True
            a.session_open = False
        # agentId can appear in the Agent tool result in current transcript formats.
        result = obj.get('toolUseResult')
        if isinstance(result, dict) and result.get('agentId'):
            child = self.agent(result['agentId'])
            child.parent = aid
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    tool = self.tools.get(block.get('tool_use_id'))
                    if tool and tool['name'] in ('Agent', 'Task'):
                        if result.get('status') == 'completed':
                            child.status, child.stopped, child.session_open = 'complete', True, False

    def hook(self, obj, live=True):
        self.event_stamp = obj.get('observed_at') if isinstance(obj.get('observed_at'), (int, float)) else None
        self.hooks_seen = True
        event = obj.get('hook_event_name', '')
        aid = str(obj.get('agent_id') or 'main')
        a = self.agent(aid, obj.get('agent_type'))
        a.updated = time.time()
        if event == 'PreToolUse':
            self.start_tool(aid, obj.get('tool_use_id'), obj.get('tool_name', 'tool'), obj.get('tool_input', {}), live)
        elif event in ('PostToolUse', 'PostToolUseFailure'):
            self.finish_tool(obj.get('tool_use_id'), event.endswith('Failure'), live)
        elif event == 'SubagentStart':
            a.status, a.stopped = 'working', False
            a.session_open = live
            # Do not invent parent edges: lifecycle payloads need not identify parents.
            a.parent = obj.get('parent_agent_id') or a.parent
            self.log(aid, 'Subagent started', 'spawned', live)
        elif event in ('SubagentStop', 'Stop', 'SessionEnd'):
            a.status = 'complete' if event == 'SubagentStop' else ('ended' if event == 'SessionEnd' else 'idle')
            a.stopped = True
            a.session_open = False
            a.inflight.clear()
            self.log(aid, event, a.status, live)
        elif event == 'PermissionRequest':
            a.status = 'approval'
            a.session_open = a.session_open or live
            self.log(aid, 'Waiting for permission', 'approval', live)
        elif event == 'UserPromptSubmit':
            self.prompt = clean(obj.get('prompt', ''))
            a.status, a.stopped = 'working', False
            self.log(aid, 'Prompt submitted', 'working', live)
        elif event in ('PreCompact', 'PostCompact'):
            a.status = 'compacting' if event == 'PreCompact' else 'observed'
            a.session_open = a.session_open or live
            self.log(aid, event, a.status, live)
        elif event == 'SessionStart':
            a.model = clean(obj.get('model') or a.model)
            a.status, a.stopped = 'observed', False
            a.session_open = a.session_open or live
        elif event == 'PostModelSwitch':
            a.model = clean(obj.get('to_model') or a.model)

    def tokens(self):
        return sum(int(u.get(k, 0) or 0) for u in self.usage.values()
                   for k in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))


class Watcher:
    def __init__(self, session, event_dir):
        self.session = session
        self.event_dir = Path(event_dir)
        self.state = State()
        self.tails = {}
        self.booting = True
        self.next_scan = 0
        self.paths = {}

    def poll(self):
        now = time.monotonic()
        if now >= self.next_scan:
            self.paths = {self.session.path: ('transcript', 'main')}
            folder = self.session.path.with_suffix('') / 'subagents'
            for p in folder.glob('**/agent-*.jsonl'):
                self.paths[p] = ('transcript', p.stem.removeprefix('agent-'))
            hook = self.event_dir / (self.session.id + '.jsonl')
            if hook.exists():
                self.paths[hook] = ('hook', 'main')
            self.next_scan = now + 1
        caught_up = True
        for path, (kind, aid) in self.paths.items():
            tail = self.tails.setdefault(path, Tail(path))
            rows = tail.read()
            for obj in rows:
                if kind == 'hook':
                    self.state.hook(obj, live=not self.booting)
                else:
                    self.state.transcript(obj, aid, live=not self.booting)
            try:
                caught_up = caught_up and path.stat().st_size - tail.offset < 2 * 1024 * 1024
            except OSError:
                pass
        if self.booting and caught_up:
            # Historical unfinished tool calls don't prove that a process is alive.
            for a in self.state.agents.values():
                if a.status in ('working', 'approval', 'compacting'):
                    a.status = 'last seen'
            self.booting = False
