"""PurrQuest development API. Python 3.11+, no third-party dependencies."""
import hashlib, json, os, re, secrets, sqlite3, time, math, datetime, uuid
from pathlib import Path
import friends_api
import account_api
import landmarks
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
DB = os.environ.get('PURRQUEST_DB', 'purrquest.sqlite3')
CATALOG = json.loads(Path(__file__).with_name('catalog.json').read_text())
CATS = set(CATALOG['cats'])

def connect():
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db

def init():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS players (id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS rooms (code TEXT PRIMARY KEY, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS members (room TEXT REFERENCES rooms(code), player TEXT REFERENCES players(id) ON DELETE CASCADE, PRIMARY KEY(room,player));
        CREATE TABLE IF NOT EXISTS catches (room TEXT, player TEXT, cat TEXT, PRIMARY KEY(room,player,cat), FOREIGN KEY(room,player) REFERENCES members(room,player) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS care_shares (
            player TEXT REFERENCES players(id) ON DELETE CASCADE,
            profile TEXT, breed TEXT NOT NULL, age TEXT NOT NULL, sex TEXT NOT NULL,
            payload TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(player,profile));
        CREATE TABLE IF NOT EXISTS sightings (
            id TEXT PRIMARY KEY, player TEXT REFERENCES players(id) ON DELETE CASCADE,
            lat REAL NOT NULL, lon REAL NOT NULL, observed REAL NOT NULL, created REAL NOT NULL,
            colors TEXT NOT NULL, pattern TEXT NOT NULL, category TEXT NOT NULL,
            collar TEXT NOT NULL, ear_tip TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS sightings_area ON sightings(lat,lon,observed);
        CREATE TABLE IF NOT EXISTS sighting_flags (
            sighting TEXT REFERENCES sightings(id) ON DELETE CASCADE,
            player TEXT REFERENCES players(id) ON DELETE CASCADE, reason TEXT NOT NULL,
            PRIMARY KEY(sighting,player));
        CREATE TABLE IF NOT EXISTS sighting_blocks (
            player TEXT REFERENCES players(id) ON DELETE CASCADE,
            blocked TEXT REFERENCES players(id) ON DELETE CASCADE, PRIMARY KEY(player,blocked));
        CREATE INDEX IF NOT EXISTS care_cohort ON care_shares(breed,age,sex);
        ''')
        account_api.init_schema(db)
        friends_api.init_schema(db)
        landmarks.init_schema(db)

class APIError(Exception):
    def __init__(self, status, message): self.status, self.message = status, message

def dispatch(method, path, body, token=''):
    if path=='/landmarks/search' and method=='POST':
        try: return landmarks.search(connect,body)
        except landmarks.Error as e: raise APIError(400,str(e))
    if path.startswith('/account/') or path.startswith('/showcases/') or path.startswith('/friends/'):
        try: return account_api.dispatch(connect,method,path,body,token,CATALOG)
        except account_api.Error as e: raise APIError(e.status,e.message)
    with connect() as db:
        if method == 'POST' and path == '/session':
            raw = secrets.token_urlsafe(32)
            pid = secrets.token_hex(16)
            db.execute('INSERT INTO players VALUES (?,?,?)', (pid,hashlib.sha256(raw.encode()).hexdigest(),time.time()))
            return {'token':raw}
        player = db.execute('SELECT id FROM players WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not player: raise APIError(401,'Session expired. Reconnect to the server.')
        pid = player['id']
        if path.startswith('/sightings'):
            return sightings_dispatch(db, pid, method, path, body)
        if path.startswith('/care/'):
            return care_dispatch(db, pid, method, path, body)
        if method == 'GET' and path == '/catalog':
            return CATALOG
        if method == 'DELETE' and path == '/session':
            db.execute('DELETE FROM players WHERE id=?',(pid,))
            return {'deleted':True}
        if method == 'POST' and path == '/rooms':
            while True:
                code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
                if not db.execute('SELECT 1 FROM rooms WHERE code=?',(code,)).fetchone(): break
            db.execute('INSERT INTO rooms VALUES (?,?)',(code,time.time()))
            db.execute('INSERT INTO members VALUES (?,?)',(code,pid))
        else:
            match = re.fullmatch(r'/rooms/([A-Z2-9]{8})(/join|/catches)?',path)
            if not match: raise APIError(404,'Route not found.')
            code, action = match.groups()
            if not db.execute('SELECT 1 FROM rooms WHERE code=?',(code,)).fetchone(): raise APIError(404,'Room not found. Check the invitation code.')
            if method == 'POST' and action == '/join':
                db.execute('INSERT OR IGNORE INTO members VALUES (?,?)',(code,pid))
            if not db.execute('SELECT 1 FROM members WHERE room=? AND player=?',(code,pid)).fetchone(): raise APIError(403,'Join this room first.')
            if method == 'POST' and action == '/catches':
                cat = body.get('cat')
                if not isinstance(cat,str) or cat not in CATS: raise APIError(400,'Unknown cat.')
                db.execute('INSERT OR IGNORE INTO catches VALUES (?,?,?)',(code,pid,cat))
            elif not ((method == 'GET' and not action) or (method == 'POST' and action == '/join')):
                raise APIError(405,'Method not allowed.')
        count = db.execute('SELECT count(*) FROM catches WHERE room=?',(code,)).fetchone()[0]
        members = db.execute('SELECT count(*) FROM members WHERE room=?',(code,)).fetchone()[0]
        mine = [r[0] for r in db.execute('SELECT cat FROM catches WHERE room=? AND player=?',(code,pid))]
        return {'code':code,'count':count,'goal':12,'members':members,'mine':mine}

def care_dispatch(db, pid, method, path, body):
    match = re.fullmatch(r'/care/([a-fA-F0-9-]{36})(/cohort)?', path)
    if not match: raise APIError(404, 'Route not found.')
    profile, cohort = match.groups()
    try: profile = str(uuid.UUID(profile))
    except ValueError: raise APIError(400, 'Invalid profile.')
    db.execute('DELETE FROM care_shares WHERE updated < ?', (time.time()-30*86400,))
    if method == 'DELETE' and not cohort:
        db.execute('DELETE FROM care_shares WHERE player=? AND profile=?', (pid,profile))
        return {'deleted':True}
    if method == 'POST' and not cohort:
        allowed = {'breed','age','sex','periodEnd','foodTotal','foodDays','waterTotal','waterDays','poopTotal','poopDays'}
        if set(body) != allowed or not all(isinstance(v,str) for v in body.values()):
            raise APIError(400,'Unexpected summary fields.')
        if body['breed'] not in CATALOG['breeds'] or body['age'] not in ['Under 1 year','1–6 years','7–10 years','11+ years'] or body['sex'] not in ['Female','Male']:
            raise APIError(400,'Choose a known breed, age group and sex.')
        try:
            end = datetime.date.fromisoformat(body['periodEnd'])
            if abs((end-datetime.datetime.now(datetime.timezone.utc).date()).days)>1: raise ValueError()
            for metric in ['food','water','poop']:
                total=float(body[metric+'Total']); days=int(body[metric+'Days'])
                if not math.isfinite(total) or not 0<=total<=10_000_000 or not 0<=days<=7 or (days==0 and total!=0): raise ValueError()
                if metric=='poop' and not total.is_integer(): raise ValueError()
            if not any(int(body[m+'Days'])>0 for m in ['food','water','poop']): raise ValueError()
        except (ValueError,OverflowError): raise APIError(400,'Invalid seven-day summary.')
        existing=db.execute('SELECT 1 FROM care_shares WHERE player=? AND profile=?',(pid,profile)).fetchone()
        if not existing and db.execute('SELECT count(*) FROM care_shares WHERE player=?',(pid,)).fetchone()[0]>=20:
            raise APIError(400,'Maximum shared profiles reached.')
        db.execute('INSERT OR REPLACE INTO care_shares VALUES (?,?,?,?,?,?,?)',
            (pid,profile,body['breed'],body['age'],body['sex'],json.dumps(body),time.time()))
        return {'shared':True}
    if method == 'GET' and cohort:
        own=db.execute('SELECT * FROM care_shares WHERE player=? AND profile=?',(pid,profile)).fetchone()
        if not own: raise APIError(403,'Share a current summary to see matching community stats.')
        peers=db.execute('SELECT player,payload FROM care_shares WHERE player!=? AND breed=? AND age=? AND sex=?',
            (pid,own['breed'],own['age'],own['sex'])).fetchall()
        result={}
        for metric in ['food','water','poop']:
            owners={}
            for peer in peers:
                data=json.loads(peer['payload']); days=int(data[metric+'Days'])
                if days: owners.setdefault(peer['player'],[]).append(float(data[metric+'Total'])/days)
            # Equal weight per owner, even when an owner shares multiple matching cats.
            values=[sum(cats)/len(cats) for cats in owners.values()]
            result[metric]={'owners':len(values) if len(values)>=3 else 0,
                'mean':sum(values)/len(values) if len(values)>=3 else None}
        return result
    raise APIError(405,'Method not allowed.')

SIGHTING_COLORS = {'Black','White','Grey / blue','Orange / ginger','Brown','Cream','Other / unsure'}
SIGHTING_PATTERNS = {'Solid','Tabby','Bicolour / tuxedo','Calico','Tortoiseshell','Colourpoint','Spotted','Unsure'}
SIGHTING_CATEGORIES = {'Outdoor cat — status unknown','Possibly lost','Community cat — unverified','Possibly unsocialized'}
OBSERVATIONS = {'Unsure','Seen','Not seen'}

def sighting_distance(lat,lon,a,b):
    r=math.pi/180
    x=math.sin((a-lat)*r/2)**2+math.cos(lat*r)*math.cos(a*r)*math.sin((b-lon)*r/2)**2
    return 6371*2*math.asin(math.sqrt(min(1,max(0,x))))

def sighting_public(row,pid):
    return {'id':row['id'],'latitude':row['lat'],'longitude':row['lon'],'observed':row['observed'],
        'colors':json.loads(row['colors']),'pattern':row['pattern'],'category':row['category'],
        'collar':row['collar'],'earTip':row['ear_tip'],'mine':row['player']==pid,
        'resolved':bool(row['resolved']),'hidden':bool(row['hidden'])}

def sightings_dispatch(db,pid,method,path,body):
    now=time.time();cutoff=now-30*86400
    if method=='GET' and path=='/sightings/mine':
        rows=db.execute('SELECT * FROM sightings WHERE player=? AND observed>=? ORDER BY observed DESC',(pid,cutoff)).fetchall()
        return {'reports':[sighting_public(row,pid) for row in rows],'truncated':False}
    if method=='POST' and path=='/sightings/unblock':
        db.execute('DELETE FROM sighting_blocks WHERE player=?',(pid,));return {'ok':True}
    if method=='POST' and path=='/sightings/search':
        try:
            if set(body)!={'latitude','longitude','radius','days','colors','pattern','category'}: raise ValueError()
            lat=float(body['latitude']);lon=float(body['longitude']);radius=int(body['radius']);days=int(body['days'])
            colors=set(json.loads(body['colors']))
            if not math.isfinite(lat) or not -90<=lat<=90 or not math.isfinite(lon) or not -180<=lon<=180: raise ValueError()
            if radius not in [1,5,10,25] or days not in [1,7,30] or not colors<=SIGHTING_COLORS: raise ValueError()
            if body['pattern'] not in SIGHTING_PATTERNS|{''} or body['category'] not in SIGHTING_CATEGORIES|{''}: raise ValueError()
        except (ValueError,TypeError): raise APIError(400,'Invalid search filters.')
        # Latitude prefilter also works at the poles/dateline; final radius uses great-circle distance.
        rows=db.execute('''SELECT * FROM sightings WHERE observed>=? AND observed<=? AND lat BETWEEN ? AND ?
            AND resolved=0 AND hidden=0 AND player NOT IN (SELECT blocked FROM sighting_blocks WHERE player=?)
            ORDER BY observed DESC''',(now-days*86400,now+60,lat-radius/110,lat+radius/110,pid))
        results=[]
        for row in rows:
            if not colors<=set(json.loads(row['colors'])):continue
            if body['pattern'] and row['pattern']!=body['pattern']:continue
            if body['category'] and row['category']!=body['category']:continue
            if sighting_distance(lat,lon,row['lat'],row['lon'])>radius:continue
            results.append(sighting_public(row,pid))
            if len(results)>200:break
        return {'reports':results[:200],'truncated':len(results)>200}
    match=re.fullmatch(r'/sightings/([a-fA-F0-9-]{36})(/resolve|/flag|/block)?',path)
    if not match: raise APIError(404,'Route not found.')
    sid,action=match.groups()
    try:sid=str(uuid.UUID(sid))
    except ValueError:raise APIError(400,'Invalid sighting ID.')
    existing=db.execute('SELECT * FROM sightings WHERE id=?',(sid,)).fetchone()
    if method=='POST' and not action:
        if existing:
            if existing['player']!=pid:raise APIError(403,'This sighting belongs to another reporter.')
            return {'ok':True} # Immutable report; safely retry a lost creation response.
        try:
            if set(body)!={'latitude','longitude','observed','colors','pattern','category','collar','earTip'}:raise ValueError()
            lat=float(body['latitude']);lon=float(body['longitude']);observed=float(body['observed'])
            colors=json.loads(body['colors'])
            if not isinstance(colors,list) or not 1<=len(colors)<=3 or len(set(colors))!=len(colors) or not set(colors)<=SIGHTING_COLORS:raise ValueError()
            if not math.isfinite(lat) or not -90<=lat<=90 or not math.isfinite(lon) or not -180<=lon<=180:raise ValueError()
            if not math.isfinite(observed) or not cutoff<=observed<=now+60:raise ValueError()
            if body['pattern'] not in SIGHTING_PATTERNS or body['category'] not in SIGHTING_CATEGORIES:raise ValueError()
            if body['collar'] not in OBSERVATIONS or body['earTip'] not in OBSERVATIONS:raise ValueError()
        except (ValueError,TypeError):raise APIError(400,'Invalid sighting details.')
        if db.execute('SELECT count(*) FROM sightings WHERE player=? AND created>?',(pid,now-86400)).fetchone()[0]>=20:
            raise APIError(429,'Daily sighting limit reached.')
        # Both client and server round to a grid. Never persist the submitted precise coordinates.
        db.execute('INSERT INTO sightings(id,player,lat,lon,observed,created,colors,pattern,category,collar,ear_tip) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (sid,pid,round(lat,3),round(lon,3),observed,now,json.dumps(sorted(colors)),body['pattern'],body['category'],body['collar'],body['earTip']))
        return {'ok':True}
    if method=='DELETE' and not action:
        if existing and existing['player']!=pid:raise APIError(403,'Only the reporter can delete this sighting.')
        db.execute('DELETE FROM sightings WHERE id=? AND player=?',(sid,pid));return {'ok':True}
    if not existing or existing['observed']<cutoff:raise APIError(404,'Sighting expired or unavailable.')
    if method=='POST' and action=='/resolve':
        if existing['player']!=pid:raise APIError(403,'Only the reporter can resolve this sighting.')
        db.execute('UPDATE sightings SET resolved=1 WHERE id=?',(sid,));return {'ok':True}
    if method=='POST' and action=='/flag':
        if body.get('reason') not in ['Incorrect sighting','Unsafe or private location','Spam or misuse']:raise APIError(400,'Choose a reason.')
        if existing['player']==pid:raise APIError(400,'Delete your own sighting instead.')
        db.execute('INSERT OR IGNORE INTO sighting_flags VALUES (?,?,?)',(sid,pid,body['reason']))
        if db.execute('SELECT count(*) FROM sighting_flags WHERE sighting=?',(sid,)).fetchone()[0]>=3:
            db.execute('UPDATE sightings SET hidden=1 WHERE id=?',(sid,))
        return {'ok':True}
    if method=='POST' and action=='/block':
        if existing['player']==pid:raise APIError(400,'This is your own sighting.')
        db.execute('INSERT OR IGNORE INTO sighting_blocks VALUES (?,?)',(pid,existing['player']));return {'ok':True}
    raise APIError(405,'Method not allowed.')

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass # Never log bearer tokens or invite codes.
    def do_GET(self): self.handle_api()
    def do_POST(self): self.handle_api()
    def do_DELETE(self): self.handle_api()
    def handle_api(self):
        if self.path.startswith('/oauth/callback/'):
            try:
                parsed=urlparse(self.path); provider=parsed.path.rsplit('/',1)[-1]
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=16384:raise ValueError()
                params=parse_qs(self.rfile.read(length).decode() if self.command=='POST' else parsed.query)
                location=account_api.oauth_callback(connect,provider,{k:v[0] for k,v in params.items()})
                self.send_response(303);self.send_header('Location',location);self.send_header('Cache-Control','no-store');self.send_header('Content-Length','0');self.end_headers()
            except Exception:
                self.send_response(400);self.send_header('Content-Length','0');self.end_headers()
            return
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 <= length <= (220000 if self.path.startswith('/account/') else 4096): raise APIError(413,'Request too large.')
            body = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body,dict): raise APIError(400,'Expected an object.')
            result = dispatch(self.command,self.path,body,self.headers.get('Authorization','').removeprefix('Bearer '))
            status = 200
        except APIError as e: status, result = e.status, {'error':e.message}
        except (ValueError,TypeError): status, result = 400, {'error':'Invalid request.'}
        except Exception: status, result = 500, {'error':'Server error.'}
        data = json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)

class CareHTTPServer(ThreadingHTTPServer):
    last_care_cleanup = 0
    def service_actions(self):
        # Expire shared snapshots even when nobody makes a request.
        now = time.time()
        if now - self.last_care_cleanup >= 60:
            with connect() as db:
                db.execute('DELETE FROM care_shares WHERE updated < ?', (now-30*86400,))
                db.execute('DELETE FROM sightings WHERE observed < ?', (now-30*86400,))
                db.execute('DELETE FROM oauth_flows WHERE created < ?', (now-600,))
                db.execute('DELETE FROM oauth_tickets WHERE expires < ?', (now,))
                db.execute('DELETE FROM account_sessions WHERE expires < ?', (now,))
                db.execute('DELETE FROM showcases WHERE created < ?', (now-7*86400,))
            self.last_care_cleanup = now

if __name__ == '__main__':
    init()
    host, port = os.environ.get('HOST','127.0.0.1'), int(os.environ.get('PORT','8787'))
    print(f'PurrQuest development API: {host}:{port}',flush=True)
    CareHTTPServer((host,port),Handler).serve_forever()
