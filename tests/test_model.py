import json
import os
from pathlib import Path
import tempfile
import unittest
from model import Tail, State, Session, Watcher, discover, clean
from ui import render, Canvas, picker


class ObserverTests(unittest.TestCase):
    def test_partial_line_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'session.jsonl';p.write_bytes(b'{"type":')
            t=Tail(p);self.assertEqual(t.read(),[])
            with p.open('ab') as f:f.write(b'"user"}\ninvalid\n{"n":2}\n')
            self.assertEqual(t.read(),[{'type':'user'},{'n':2}]);self.assertEqual(t.read(),[])
            p.write_text('{"n":3}\n');self.assertEqual(t.read(),[{'n':3}]);self.assertTrue(t.reset)

    def test_replacement_inode(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'s';p.write_text('{"a":1}\n');t=Tail(p);t.read()
            q=Path(d)/'other';q.write_text('{"a":2}\n');q.replace(p)
            self.assertEqual(t.read(),[{'a':2}])

    def test_discovery_excludes_subagents_and_extracts_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'projects'/'-tmp-project';p.mkdir(parents=True)
            (p/'abc.jsonl').write_text(json.dumps({'type':'user','cwd':'/tmp/project','message':{'content':'Fix login'}})+'\n')
            (p/'agent-old.jsonl').write_text('{}\n')
            (p/'abc'/'subagents').mkdir(parents=True)
            (p/'abc'/'subagents'/'agent-x.jsonl').write_text('{}\n')
            s=discover(d);self.assertEqual(len(s),1);self.assertEqual(s[0].title,'Fix login');self.assertEqual(s[0].project,'/tmp/project')

    def test_hook_transcript_dedup_and_parallel_failures(self):
        s=State()
        s.hook({'hook_event_name':'PreToolUse','agent_id':'worker','tool_use_id':'t1','tool_name':'Read'})
        s.transcript({'uuid':'u1','message':{'content':[{'type':'tool_use','id':'t1','name':'Read','input':{}}]}},'worker')
        s.start_tool('worker','t2','Bash',{},False)
        s.finish_tool('t1',False,False)
        self.assertEqual(s.agents['worker'].status,'working')
        s.finish_tool('t2',True,False);s.finish_tool('t2',True,False)
        self.assertEqual(s.agents['worker'].failures,1)
        self.assertEqual(s.agents['worker'].calls,2)
        self.assertEqual(s.agents['worker'].status,'failed')

    def test_usage_deduplicated_by_message_id(self):
        s=State()
        for i in range(3):
            s.transcript({'uuid':str(i),'message':{'id':'m1','usage':{'input_tokens':100,'output_tokens':i},'content':[]}})
        self.assertEqual(s.tokens(),102)

    def test_open_agent_stays_visible_between_tools_until_stopped(self):
        s=State()
        s.hook({'hook_event_name':'SubagentStart','agent_id':'writer'})
        s.start_tool('writer','read','Read',{},True)
        s.finish_tool('read',False,True)
        self.assertEqual(s.agents['writer'].status,'observed')
        self.assertIn('writer',[a.id for a in s.visible_agents()])
        s.start_tool('writer','test','Bash',{},True)
        s.finish_tool('test',True,True)
        self.assertEqual(s.agents['writer'].status,'failed')
        self.assertIn('writer',[a.id for a in s.visible_agents()])
        s.hook({'hook_event_name':'SubagentStop','agent_id':'writer'})
        self.assertNotIn('writer',[a.id for a in s.visible_agents()])
        self.assertIn('writer',[a.id for a in s.visible_agents(show_inactive=True)])
        s.hook({'hook_event_name':'SessionStart','agent_id':'writer'})
        self.assertIn('writer',[a.id for a in s.visible_agents()])

    def test_live_result_opens_agent_after_historical_tool_start(self):
        s=State()
        s.start_tool('reader','read','Read',{},False)
        self.assertNotIn('reader',[a.id for a in s.visible_agents()])
        s.hook({'hook_event_name':'PostToolUse','tool_use_id':'read'})
        self.assertIn('reader',[a.id for a in s.visible_agents()])

    def test_live_transcript_opens_agent_but_history_does_not(self):
        s=State()
        s.transcript({'uuid':'history','message':{'id':'response','usage':{'input_tokens':100},'content':[]}},'reader',live=False)
        self.assertNotIn('reader',[a.id for a in s.visible_agents()])
        s.transcript({'uuid':'live','message':{'content':[{'type':'text','text':'Checking the code'}]}},'reader')
        self.assertIn('reader',[a.id for a in s.visible_agents()])
        s.transcript({'uuid':'done','message':{'stop_reason':'end_turn','content':[]}},'reader')
        self.assertNotIn('reader',[a.id for a in s.visible_agents()])
        s.transcript({'uuid':'unchanged','message':{'id':'response','usage':{'input_tokens':100},'content':[]}},'reader')
        self.assertNotIn('reader',[a.id for a in s.visible_agents()])

    def test_live_subagent_discovery_and_stale_history(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'abc.jsonl';p.write_text(json.dumps({'message':{'content':[{'type':'tool_use','name':'Read','id':'t1'}]}})+'\n')
            w=Watcher(Session('abc',p,'project','title',0),Path(d)/'events');w.poll()
            self.assertEqual(w.state.agents['main'].status,'last seen')
            sub=p.with_suffix('')/'subagents';sub.mkdir(parents=True)
            (sub/'agent-child.jsonl').write_text(json.dumps({'agentId':'child','message':{'model':'claude-haiku','content':[{'type':'tool_use','id':'t2','name':'Grep'}]}})+'\n')
            w.next_scan=0;w.poll()
            self.assertEqual(w.state.agents['child'].calls,1)
            self.assertEqual(w.state.agents['child'].parent,None)

    def test_escape_injection_is_sanitized(self):
        self.assertNotIn('\x1b',clean('\x1b[2Jattack\n'))
        c=Canvas(30,5);c.text(0,0,'\x1b]52;clipboard\x07')
        self.assertNotIn('\x07',c.ansi());self.assertNotIn('\x1b]52',c.ansi())

    def test_renderer_handles_sizes_and_many_agents(self):
        s=State()
        for i in range(15):s.agent(str(i))
        session=Session('id',Path('/tmp/id.jsonl'),'project','title',0)
        for w,h in [(60,20),(84,40),(100,42),(124,48),(180,60)]:
            c=render(w,h,state=s,session=session,selected=15,show_inactive=True)
            self.assertEqual(len(c.cells),h);self.assertTrue(all(len(r)==w for r in c.cells))


if __name__=='__main__':unittest.main()
