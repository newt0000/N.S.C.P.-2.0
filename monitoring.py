"""Persistent Minecraft-only telemetry for NSCP. No system-wide usage reported."""
import os, time, sqlite3, threading, re
from pathlib import Path
import psutil

DB = Path(__file__).parent / 'data' / 'monitoring.sqlite3'
_lock = threading.Lock()
_join = re.compile(r'\b([A-Za-z0-9_]{3,16}) joined the game\b|\b([A-Za-z0-9_]{3,16})\[/[^]]+\] logged in with entity id', re.I)
_leave = re.compile(r'\b([A-Za-z0-9_]{3,16}) (?:left the game|lost connection:)', re.I)

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB), timeout=10)
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, cpu REAL, rss INTEGER, players INTEGER, online INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, label TEXT)')
    return db

def event(kind, label):
    with _lock:
        with connect() as db:
            db.execute('INSERT INTO events(ts,kind,label) VALUES(?,?,?)',(int(time.time()),str(kind)[:32],str(label)[:160]))

def record_player_log(line):
    m = _join.search(line)
    if m:
        event('join', next(x for x in m.groups() if x)); return
    m = _leave.search(line)
    if m: event('leave', m.group(1))

_cpu_cache = {}
def process_metrics(pid):
    # JVM is a descendant of the shell launched by NSCP. Never include the panel process.
    jvms=[]
    if pid:
        try:
            root=psutil.Process(pid)
            descendants=[root]+root.children(recursive=True)
            for proc in descendants:
                try:
                    name=proc.name().lower()
                    if name in ('java','java.exe') or name.startswith('java'):
                        jvms.append(proc)
                except (psutil.NoSuchProcess,psutil.AccessDenied): pass
        except (psutil.NoSuchProcess,psutil.AccessDenied): pass
    rss=0; cpu=0.0; cores=psutil.cpu_count() or 1
    for proc in jvms:
        try:
            t=proc.cpu_times(); now=time.monotonic(); used=t.user+t.system
            prev=_cpu_cache.get(proc.pid)
            if prev and now>prev[0]: cpu+=max(0,(used-prev[1])/(now-prev[0])*100)
            _cpu_cache[proc.pid]=(now,used)
            rss+=proc.memory_info().rss
        except (psutil.NoSuchProcess,psutil.AccessDenied): pass
    # CPU % is of one logical core; values >100% are expected for multi-threaded Java.
    return {'cpu_percent':round(cpu,1), 'mem_used':rss,'mem_total':0,'mem_percent':0,
            'disk_total':0,'disk_used':0,'disk_percent':0,
            'jvm_found':bool(jvms),'jvm_pids':[p.pid for p in jvms], 'cpu_cores':cores}

def start_sampler(manager):
    def worker():
        last_online=None
        while True:
            try:
                stats=manager.get_stats()
                online=int(bool(stats['server_running']))
                # Query RCON at a low frequency; fallback to log-tracked names.
                count=len(manager.get_online_players()) if online else 0
                now=int(time.time())
                with _lock:
                    with connect() as db:
                        db.execute('INSERT OR REPLACE INTO samples(ts,cpu,rss,players,online) VALUES(?,?,?,?,?)',
                                   (now,stats['cpu_percent'] if stats['jvm_found'] else None,
                                    stats['mem_used'] if stats['jvm_found'] else None,count,online))
                        db.execute('DELETE FROM samples WHERE ts < ?', (now-7*86400,))
                        db.execute('DELETE FROM events WHERE ts < ?', (now-30*86400,))
                if last_online is not None and online!=last_online:
                    event('server', 'Server started' if online else 'Server stopped')
                last_online=online
            except Exception as exc:
                print('[NSCP monitor] sample failed:',exc)
            time.sleep(15)
    threading.Thread(target=worker,daemon=True,name='nscp-monitor').start()

def history(hours=1):
    hours=max(1,min(int(hours),168)); since=int(time.time())-hours*3600
    with connect() as db:
        db.row_factory=sqlite3.Row
        samples=[dict(r) for r in db.execute('SELECT * FROM samples WHERE ts>=? ORDER BY ts',(since,))]
        events=[dict(r) for r in db.execute('SELECT ts,kind,label FROM events WHERE ts>=? ORDER BY ts',(since,))]
    # Limit browser load when displaying 7 days.
    stride=max(1,len(samples)//800)
    return {'samples':samples[::stride], 'events':events,'hours':hours}
