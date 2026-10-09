"""Cell-based true-colour renderer. No external dependencies."""
from collections import Counter
from itertools import islice
import math
import shutil
import time
import unicodedata
from model import clean

PALETTE = dict(text='#D5D9DD', muted='#818894', dim='#424A53',
               purple='#B5A5E3', blue='#94A8C6', green='#78EAA4', orange='#D9936D', red='#D45C74')


def rgb(color):
    s = PALETTE.get(color, color).lstrip('#')
    return tuple(int(s[i:i+2], 16) for i in (0, 2, 4))


def role(agent):
    m = agent.model.lower()
    return 'purple' if 'opus' in m or 'advisor' in agent.name.lower() else ('green' if 'haiku' in m or 'explor' in agent.name.lower() else 'blue')


def status_color(a):
    return 'red' if a.status == 'failed' else ('orange' if a.status in ('approval', 'compacting') else ('green' if a.status in ('working', 'complete') else 'muted'))


def short_model(model):
    return model.replace('claude-', '').replace('-20', ' · 20')


def age(seconds):
    if seconds < 60:
        return f'{int(seconds)}s ago'
    if seconds < 3600:
        return f'{int(seconds // 60)}m ago'
    if seconds < 86400:
        return f'{int(seconds // 3600)}h ago'
    return f'{int(seconds // 86400)}d ago'


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.cells = [[(' ', 'text', None, False) for _ in range(w)] for _ in range(h)]
        self.connections = {}
        self.arrows = {}

    def text(self, x, y, text, color='text', bold=False, bg=None, limit=None):
        if not 0 <= y < self.h:
            return
        for char in clean(text, 10000):
            if unicodedata.combining(char):
                continue
            # Keep every glyph one terminal cell; user text may contain emoji/CJK.
            if unicodedata.east_asian_width(char) in ('W', 'F'):
                char = '?'
            if limit is not None and limit <= 0:
                break
            if 0 <= x < self.w:
                self.cells[y][x] = (char, color, bg, bold)
            x += 1
            if limit is not None:
                limit -= 1

    def center(self, x, y, w, text, color='text', bold=False):
        text = clean(text, max(0, w - 2))
        self.text(x + max(1, (w-len(text))//2), y, text, color, bold)

    def box(self, x, y, w, h, color='dim', title=None):
        if w < 3 or h < 3:
            return
        self.text(x, y, '┌' + '╌'*(w-2) + '┐', color)
        self.text(x, y+h-1, '└' + '╌'*(w-2) + '┘', color)
        for row in range(y+1, y+h-1):
            self.text(x, row, '╎', color)
            self.text(x+w-1, row, '╎', color)
        if title:
            self.text(x+2, y, ' '+title+' ', color)

    def line(self, points, color='dim', pulse=False, clock=0, arrow=False, source_above=False):
        cells = []
        for (x1,y1),(x2,y2) in zip(points, points[1:]):
            length = abs(x2-x1)+abs(y2-y1)
            dx = 0 if x1 == x2 else (1 if x2>x1 else -1)
            dy = 0 if y1 == y2 else (1 if y2>y1 else -1)
            for i in range(length+1):
                p=(x1+i*dx,y1+i*dy)
                if not cells or cells[-1]!=p:
                    cells.append(p)
        glyphs = {0:'┊',1:'┊',2:'┄',3:'└',4:'┊',5:'┊',6:'┌',7:'├',
                  8:'┄',9:'┘',10:'┄',11:'┴',12:'┐',13:'┤',14:'┬',15:'┼'}
        directions = {(0,-1):1,(1,0):2,(0,1):4,(-1,0):8}
        if cells and arrow:
            dx,dy=(0,1) if len(cells)<2 else (cells[-1][0]-cells[-2][0],cells[-1][1]-cells[-2][1])
            self.arrows[cells[-1]] = {(0,-1):'▲',(1,0):'▶',(0,1):'▼',(-1,0):'◀'}[(dx,dy)]
        for i,p in enumerate(cells):
            mask=self.connections.get(p,0)
            if i==0 and source_above:
                mask |= 1
            for neighbor in cells[max(0,i-1):i]+cells[i+1:i+2]:
                mask |= directions[(neighbor[0]-p[0],neighbor[1]-p[1])]
            self.connections[p]=mask
            self.text(*p,self.arrows.get(p,glyphs[mask]),color)
        if cells and pulse:
            head = int(clock*15)%len(cells)
            for lag,ch in ((3,'·'),(1,'•'),(0,'◆')):
                p=cells[(head-lag)%len(cells)]
                if p not in self.arrows and self.connections[p] in (0,1,2,4,5,8,10):
                    self.text(*p,ch,color,True)

    def ansi(self):
        out=['\x1b[H']
        previous=None
        for y,row in enumerate(self.cells):
            if y:
                out.append(f'\x1b[{y+1};1H')
            for char,fg,bg,bold in row:
                style=(fg,bg,bold)
                if style != previous:
                    r,g,b=rgb(fg)
                    out.append(f'\x1b[0;{1 if bold else 22};38;2;{r};{g};{b}m')
                    previous=style
                out.append(char)
        out.append('\x1b[0m')
        return ''.join(out)

    def svg(self):
        import html
        parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w*9}" height="{self.h*18}" viewBox="0 0 {self.w*9} {self.h*18}"><g font-family="DejaVu Sans Mono, monospace" font-size="14">']
        for y,row in enumerate(self.cells):
            for x,(ch,fg,bg,bold) in enumerate(row):
                if ch!=' ':
                    parts.append(f'<text x="{x*9}" y="{y*18+14}" fill="{PALETTE[fg]}" font-weight="{700 if bold else 400}">{html.escape(ch)}</text>')
        return ''.join(parts)+'</g></svg>'


def dither(fill, width):
    """Bar as (lit, track): 2-cell █▊ segments with a small gap, like an LCD bar graph, then a braille checkerboard."""
    return ('█▊'*fill)[:fill], '⢕'*(width-fill)


def gauge(seconds, width=13):
    """Freshness meter: shorter the longer a session has been quiet."""
    return dither(max(1, width - 3*sum(seconds > t for t in (30, 300, 3600, 86400))), width)


SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'


def lapse(s):
    return f'{s:.1f}s' if s < 10 else f'{s:.0f}s' if s < 100 else f'{int(s)//60}m{int(s)%60:02d}s' if s < 3600 else '>1h'


def solo(c, state, x, y, w, h, now, wall, caption, paused=False):
    """Main agent alone: a tape of its real tool calls (spinner, duration bar) and a per-second event heartbeat."""
    main = state.agents['main']
    busy = main.status == 'working' and not paused
    tone = status_color(main)
    ix, iw = x+2, w-4
    bw = 12 if iw >= 70 else 8
    bx = ix+iw-bw-7
    c.box(x, y, w, h, role(main) if main.inflight else 'dim', 'MAIN AGENT · TOOL TAPE')
    right = f'{len(main.inflight)} in flight' if main.inflight else main.status
    c.text(ix+iw-len(right), y+1, right, tone, True)
    c.text(ix, y+1, caption, 'dim', limit=iw-len(right)-2)
    rows = list(islice(((i, t) for i, t in reversed(state.tools.items()) if t['agent'] == 'main'), h-4))[::-1]
    # Only a tool still in flight while main is live has a running clock; Stop/boot leave stale ids behind.
    spans = [max(0, t['end']-t['start']) if t['end'] else max(0, wall-t['start']) if busy and i in main.inflight else None for i, t in rows]
    longest = max([d for d in spans if d is not None] + [2])
    for k, ((tid, t), d) in enumerate(zip(rows, spans)):
        ry = y+2+k; flying = tid in main.inflight
        if flying:
            glyph = SPINNER[int(now*8+k) % 10] if busy else '◆◇'[int(now*2) % 2] if main.status == 'approval' and not paused else '◌'
            color = tone
        else:
            glyph, color = ('×', 'red') if t['failed'] else ('✓', 'green') if t['end'] else ('·', 'dim')
        c.text(ix, ry, glyph, color, flying)
        c.text(ix+2, ry, t['name'], 'text', limit=9)
        c.text(ix+12, ry, t['detail'], 'muted', limit=bx-ix-13)
        if d is not None:
            lit, track = dither(max(1, min(bw, round(d/longest*bw))), bw)
            hue = color if flying or t['failed'] else 'muted'
            c.text(bx, ry, lit, hue); c.text(bx+len(lit), ry, track, 'dim')
            c.text(ix+iw-6, ry, f'{lapse(d):>6}', hue)
    if not rows:
        mid = y+1+(h-2)//2
        c.center(x, mid-1, w, 'Waiting for the first tool call', 'muted')
        lit, track = dither(1+round((math.sin(int(now*4)*.4)+1)/2*23), 24)
        c.text(x+(w-24)//2, mid+1, lit, tone); c.text(x+(w-24)//2+len(lit), mid+1, track, 'dim')
    beats = Counter(int(now-t) for t, aid, _ in state.pulses if aid == 'main')
    failed = {int(now-t) for t, aid, st in state.pulses if aid == 'main' and st == 'failed'}
    c.text(ix, y+h-2, 'pulse', 'dim')
    for i in range(iw-7):
        ago = iw-8-i
        c.text(ix+7+i, y+h-2, '▁▂▃▄▅▆▇█'[min(7, beats[ago])], 'red' if ago in failed else 'green' if beats[ago] else 'dim')


def picker(c, sessions, index, query, config):
    c.text(2,1,'CLAUDE CODE / AGENT TREE','text',True)
    c.text(c.w-39,1,'● LOCAL OBSERVER','green',True)
    c.text(2,2,'choose a Claude Code session','muted')
    c.text(c.w-39,2,f'{len(sessions)} sessions','dim')
    c.text(2,3,'Attach a view. Keep working in your usual Claude terminal.','dim')
    c.box(2,5,c.w-4,3,'purple')
    c.text(4,6,'/ '+query+('▌' if int(time.monotonic()*2)%2 else ' '),'purple')
    gx=c.w-18
    c.text(4,9,'PROJECT / SESSION','muted')
    c.text(gx-9,9,'LAST UPDATED','muted')
    visible=max(1,(c.h-13)//4)
    start=max(0,min(index-visible//2,max(0,len(sessions)-visible)))
    for offset,s in enumerate(sessions[start:start+visible]):
        n=start+offset; y=10+offset*4; selected=n==index
        seconds=max(0,time.time()-s.modified); fresh=seconds<30
        c.box(2,y,c.w-4,4,'purple' if selected else 'dim')
        c.text(4,y+1,'◆' if selected else '·','purple' if selected else 'dim')
        c.text(7,y+1,s.project,'text' if selected else 'muted',selected,limit=gx-18)
        c.text(gx-9,y+1,age(seconds),'green' if fresh else 'muted')
        lit,track=gauge(seconds)
        c.text(gx,y+1,lit,'green' if fresh else 'muted')
        c.text(gx+len(lit),y+1,track,'dim')
        c.text(7,y+2,s.title,'muted',limit=gx-9)
        c.text(gx,y+2,s.id[:12],'dim')
    if not sessions:
        c.text(4,11,'No matching sessions yet.', 'orange')
        c.text(4,13,'Start Claude Code locally, or press d for the animated demo.', 'muted',limit=c.w-8)
        c.text(4,15,str(config / 'projects'),'dim',limit=c.w-8)
    c.text(3,c.h-3,'↑↓ select   enter listen   type to filter   ctrl-u clear   d demo   esc quit','muted',limit=c.w-6)
    c.text(3,c.h-2,'refreshes automatically  ·  timestamps do not prove a session is running','dim',limit=c.w-6)


def dashboard(c,state,session,selected=0,paused=False,demo=False,detail=False,show_inactive=False):
    now=time.monotonic()
    agents=state.visible_agents(show_inactive)
    selected=max(0,min(selected,len(agents)-1))
    focus=agents[selected]
    main=state.agents['main']
    c.text(2,1,'CLAUDE CODE / AGENT TREE','text',True)
    c.text(c.w-39,1,'DEMO · synthetic events' if demo else ('PAUSED' if paused else '● LISTENING · local only'),'orange' if demo or paused else 'green',True)
    c.text(2,2,session.project,'muted',limit=c.w-44)
    c.text(c.w-39,2,session.id[:28],'dim')
    c.text(2,3,'▪ advisor / opus','purple'); c.text(24,3,'▪ main / worker','blue'); c.text(47,3,'▪ haiku / explorer','green')
    if c.w>=100:
        c.text(c.w-27,3,'● activity  × failure','muted')
    # Fixed inspector rail, with the same silhouette as the reference's advisor.
    side=27 if c.w>=110 else 23
    graphx=side+4; graphw=c.w-graphx-2
    top=4; graphbottom=c.h-14
    mainh=5
    show_return=c.h>=46
    gap_count=3 if show_return else 2
    fixed_height=14 if show_return else 11
    minimum_gap=3 if show_return and c.h<48 else 4
    cardh=min(7,graphbottom-top+1-fixed_height-minimum_gap*gap_count)
    row_gap,remainder=divmod(graphbottom-top+1-fixed_height-cardh,gap_count)
    cardh+=remainder
    ty=top+mainh+row_gap
    cardy=ty+6+row_gap
    ry=cardy+cardh+row_gap
    bus_y=cardy-3
    label_y=bus_y-1
    railh=graphbottom-top+1
    c.box(2,top,side,railh,role(focus))
    c.text(4,top+2,focus.name,role(focus),True,limit=side-4)
    c.text(4,top+4,short_model(focus.model),'muted',limit=side-4)
    c.text(4,top+6,'◆ '+focus.status,status_color(focus),True,limit=side-4)
    action=focus.action
    for i in range(min(4,(len(action)+side-5)//(side-4))):
        c.text(4,top+9+i,action[i*(side-4):(i+1)*(side-4)],'muted')
    c.text(4,top+15,'tool calls','muted'); c.text(2+side-7,top+15,str(focus.calls),'text',True)
    c.text(4,top+17,'failures','muted'); c.text(2+side-7,top+17,str(focus.failures),'red' if focus.failures else 'dim',True)
    if railh>25:
        c.text(4,graphbottom-4,'parent','dim')
        c.text(4,graphbottom-3,focus.parent or ('root' if focus.id=='main' else 'not identified'),'muted',limit=side-4)
    # Main session card.
    mw=min(48,graphw-4); mx=graphx+(graphw-mw)//2; center=mx+mw//2
    c.box(mx,top,mw,mainh,'orange')
    c.center(mx,top+1,mw,short_model(main.model),'blue',True)
    c.center(mx,top+2,mw,'main session','text',True)
    c.center(mx,top+3,mw,main.status,status_color(main))
    activity=(time.time()-state.last_event<3 and not paused)
    c.line([(center,top+mainh),(center,ty-1)],'green' if activity else 'dim',activity,now,arrow=True,source_above=True)
    # Real observed tool mix, not invented routing probabilities.
    tw=min(graphw-2,68); tx=graphx+(graphw-tw)//2
    c.box(tx,ty,tw,6,'green')
    c.text(tx+2,ty+1,'OBSERVED TOOLS','green',True)
    c.text(tx+tw-17,ty+1,f'{sum(state.counts.values()):>6} calls','muted')
    counts=state.counts.most_common(3)
    maximum=max([n for _,n in counts],default=1)
    for i,(name,count) in enumerate(counts):
        y=ty+2+i; barw=max(4,tw-32); fill=max(1,round(count/maximum*barw))
        color='orange' if name in ('Edit','Write','Bash') else 'green'
        c.text(tx+2,y,name,'text',limit=15)
        lit,track=dither(fill,barw)
        c.text(tx+19,y,lit,color)
        c.text(tx+19+len(lit),y,track,'dim')
        c.text(tx+tw-8,y,f'{count:>5}','muted')
    if not counts:
        c.text(tx+2,ty+3,'Waiting for the first observed tool call…','muted',limit=tw-4)
    # Session membership bus; not a claimed parent hierarchy.
    children=agents[1:]
    if children and row_gap>=4:
        c.text(graphx+2,label_y,'SESSION AGENTS' if show_inactive else 'OPEN AGENTS','green',True)
    capacity=3 if graphw>=64 else 2
    page=0 if selected==0 else (selected-1)//capacity
    shown=children[page*capacity:(page+1)*capacity]
    gap=2; cardw=(graphw-gap*(capacity-1))//capacity
    if shown:
        midpoints=[graphx+i*(cardw+gap)+cardw//2 for i in range(len(shown))]
        for i,a in enumerate(shown):
            x=graphx+i*(cardw+gap); color=role(a)
            moving=not paused and any(aid==a.id and now-t<2.5 for t,aid,_ in state.pulses)
            c.line([(center,ty+6),(center,bus_y),(midpoints[i],bus_y),(midpoints[i],cardy-1)],color if moving else 'dim',moving,now+i,arrow=True,source_above=True)
            c.box(x,cardy,cardw,cardh,color if a.id==focus.id or moving else 'dim')
            c.center(x,cardy+1,cardw,a.name,'text',True)
            if cardh>=5:
                c.center(x,cardy+2,cardw,short_model(a.model),color)
            if cardh>6:
                c.center(x,cardy+4,cardw,a.action,'muted')
            c.center(x,cardy+cardh-2,cardw,('✓ ' if a.status=='complete' else '× ' if a.status=='failed' else '')+a.status,status_color(a))
    else:
        solo(c,state,graphx,ty+7,graphw,min(18,graphbottom-ty-6),now,time.time(),'No subagents observed in this session' if show_inactive else 'No open subagents observed',paused)
    if shown and show_return:
        for i,a in enumerate(shown):
            x=graphx+i*(cardw+gap)+cardw//2
            returned=not paused and any(aid==a.id and status in ('returned','complete') and now-t<2.5 for t,aid,status in state.pulses)
            c.line([(x,cardy+cardh),(x,ry-3),(center,ry-3),(center,ry-1)],role(a) if returned else 'dim',returned,now+i,arrow=True,source_above=True)
        c.box(mx,ry,mw,3,'orange')
        c.center(mx,ry+1,mw,'back to main  ·  '+main.status,'orange')
    if railh>26:
        counts=[sum(1 for t,_,_ in state.pulses if i<=now-t<i+1) for i in range(15,-1,-1)]
        bars='▁▂▃▄▅▆▇█'
        c.text(4,graphbottom-7,'EVENT ACTIVITY · 16s','dim')
        c.text(4,graphbottom-6,''.join(bars[min(7,n)] for n in counts),'green')
    # Log and footer.
    ly=graphbottom+2; lh=c.h-ly-4
    c.box(2,ly,c.w-4,lh,'dim','session log' if not detail else 'selected agent log')
    logs=list(state.logs)
    if detail:
        logs=[entry for entry in logs if entry[1]==focus.id]
    for i,(stamp,aid,text,status) in enumerate(logs[-max(0,lh-2):]):
        y=ly+1+i; a=state.agents.get(aid,main)
        c.text(4,y,stamp,'dim'); c.text(14,y,a.name,role(a),limit=16)
        c.text(32,y,text,'muted',limit=c.w-57)
        c.text(c.w-22,y,status,'red' if status=='failed' else 'green' if status in ('returned','complete') else 'muted',limit=18)
    c.text(2,c.h-3,'s sessions  tab agent  enter details  i inactive  space pause  q quit','muted')
    tokens=state.tokens()
    c.text(2,c.h-2,f'{len(agents)}/{len(state.agents)} agents  ·  inactive '+('shown' if show_inactive else 'hidden')+f'  ·  {tokens:,} recorded tokens  ·  '+('hooks + transcript' if state.hooks_seen else 'transcript only'),'dim',limit=c.w-3)
    if children and len(children)>capacity:
        c.text(c.w-22,label_y if row_gap>=4 else top+mainh,f'page {page+1}/{math.ceil(len(children)/capacity)}','dim')


def render(w,h,**kwargs):
    c=Canvas(w,h)
    if w<84 or h<40:
        c.text(2,2,'AGENT TREE','green',True)
        c.text(2,4,'Please resize to at least 84 columns × 40 rows.','text',limit=w-4)
        c.text(2,6,f'Current: {w} × {h}   ·   q quits','muted',limit=w-4)
        return c
    dashboard(c,**kwargs)
    return c
