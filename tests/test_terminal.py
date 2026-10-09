"""Exercise the actual interactive entry point through a pseudo-terminal."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]


class TerminalTests(unittest.TestCase):
    def launch(self,*args):
        master,slave=pty.openpty()
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',48,124,0,0))
        process=subprocess.Popen([sys.executable,str(ROOT/'agent_tree.py'),*args],stdin=slave,stdout=slave,stderr=slave)
        os.close(slave)
        self.addCleanup(os.close,master)
        self.addCleanup(lambda:(process.kill(),process.wait()) if process.poll() is None else None)
        return master,process

    def read_until(self,fd,text,timeout=4):
        data=b'';deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if select.select([fd],[],[],0.05)[0]:
                try:data+=os.read(fd,262144)
                except OSError:break
                if text.encode() in data:return data
        self.fail(f'Missing terminal output: {text}')

    def wait_exit(self,fd,p,timeout=4):
        # Drain like a real terminal: an unread frame can block the app's write before it sees the key.
        deadline=time.monotonic()+timeout
        while p.poll() is None and time.monotonic()<deadline:
            if select.select([fd],[],[],0.05)[0]:
                try:os.read(fd,262144)
                except OSError:break
        p.wait(timeout=1)

    def test_demo_switch_back_and_clean_exit(self):
        fd,p=self.launch('--demo')
        self.read_until(fd,'AGENT TREE')
        os.write(fd,b'\t\r ')
        os.write(fd,b's')
        self.read_until(fd,'choose a Claude Code session')
        os.write(fd,b'\x1b')
        self.wait_exit(fd,p);self.assertEqual(p.returncode,0)

    def test_select_session_then_observe_live_append(self):
        with tempfile.TemporaryDirectory() as d:
            project=Path(d)/'projects'/'fixture';project.mkdir(parents=True)
            transcript=project/'session.jsonl'
            transcript.write_text(json.dumps({'type':'user','cwd':'/fixture/project','message':{'content':'Fix the login'}})+'\n')
            fd,p=self.launch('--claude-dir',d)
            self.read_until(fd,'Fix the login')
            os.write(fd,b'\r');self.read_until(fd,'LISTENING')
            with transcript.open('a') as f:
                f.write(json.dumps({'uuid':'tool-row','message':{'model':'claude-sonnet','content':[{'type':'tool_use','id':'live1','name':'Bash','input':{'command':'unique-live-command'}}]}})+'\n')
            self.read_until(fd,'unique-live-command')
            os.write(fd,b'q');self.wait_exit(fd,p);self.assertEqual(p.returncode,0)

    def test_hook_bridge_is_silent_and_filters_payload(self):
        with tempfile.TemporaryDirectory() as d:
            env=dict(os.environ,AGENT_TREE_EVENTS_DIR=d)
            result=subprocess.run([sys.executable,str(ROOT/'plugin/scripts/emit.py')],input=json.dumps({'session_id':'abc','hook_event_name':'PreToolUse','tool_use_id':'t','tool_input':{'file_path':'auth.py','new_string':'SECRET'},'prompt':'SECRET'}),text=True,capture_output=True,env=env)
            self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,'');self.assertEqual(result.stderr,'')
            output=(Path(d)/'abc.jsonl').read_text();self.assertNotIn('SECRET',output)
            self.assertEqual(os.stat(Path(d)/'abc.jsonl').st_mode & 0o777,0o600)

    def test_inactive_toggle_and_live_agent_navigation(self):
        with tempfile.TemporaryDirectory() as d:
            transcript=Path(d)/'session.jsonl'
            transcript.write_text('{}\n')
            sub=transcript.with_suffix('')/'subagents';sub.mkdir(parents=True)
            child=sub/'agent-old.jsonl'
            child.write_text(json.dumps({'message':{'model':'claude-opus','stop_reason':'end_turn','content':[]}})+'\n')
            fd,p=self.launch('--session',str(transcript),'--events-dir',str(Path(d)/'events'))
            data=self.read_until(fd,'transcript only')
            self.assertNotIn(b'agent old',data)
            self.assertNotIn(b'48;2;',data)
            os.write(fd,b'i')
            self.read_until(fd,'agent old')
            os.write(fd,b'\t\r')
            self.read_until(fd,'selected agent log')
            os.write(fd,b'i')
            self.read_until(fd,'No open subagents observed')
            with child.open('a') as f:
                f.write(json.dumps({'uuid':'live','message':{'content':[{'type':'tool_use','id':'live-tool','name':'Read','input':{'file_path':'live-agent-action'}}]}})+'\n')
            self.read_until(fd,'agent old')
            os.write(fd,b'\t')
            self.read_until(fd,'live-agent-action')
            os.write(fd,b'q')
            self.read_until(fd,'\x1b[?1049l')
            p.wait(timeout=4);self.assertEqual(p.returncode,0)


if __name__=='__main__':unittest.main()
