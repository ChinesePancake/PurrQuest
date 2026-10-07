"""Consent-based account friends, private codes, blocking and bounded requests."""
import json,secrets,time
class Error(Exception):
    def __init__(self,status,message):self.status,self.message=status,message

def init_schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS friend_codes(account TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,code TEXT UNIQUE NOT NULL);
    CREATE TABLE IF NOT EXISTS friendships(a TEXT REFERENCES accounts(id) ON DELETE CASCADE,b TEXT REFERENCES accounts(id) ON DELETE CASCADE,requester TEXT REFERENCES accounts(id) ON DELETE CASCADE,status TEXT NOT NULL,created REAL,PRIMARY KEY(a,b));
    CREATE INDEX IF NOT EXISTS friendship_b ON friendships(b);
    CREATE TABLE IF NOT EXISTS friend_blocks(owner TEXT REFERENCES accounts(id) ON DELETE CASCADE,target TEXT REFERENCES accounts(id) ON DELETE CASCADE,PRIMARY KEY(owner,target));
    CREATE TABLE IF NOT EXISTS friend_attempts(account TEXT REFERENCES accounts(id) ON DELETE CASCADE,created REAL);
    CREATE INDEX IF NOT EXISTS friend_attempt_account ON friend_attempts(account,created);
    ''')

def code(db,aid):
    r=db.execute('SELECT code FROM friend_codes WHERE account=?',(aid,)).fetchone()
    if r:return r[0]
    value=secrets.token_hex(6).upper()
    db.execute('INSERT INTO friend_codes VALUES (?,?)',(aid,value));return value

def blocked(db,a,b):return db.execute('SELECT 1 FROM friend_blocks WHERE (owner=? AND target=?) OR (owner=? AND target=?)',(a,b,b,a)).fetchone() is not None

def listing(db,aid):
    groups=dict(friends=[],incoming=[],outgoing=[],blocked=[])
    rows=db.execute('SELECT * FROM friendships WHERE a=? OR b=? ORDER BY created DESC',(aid,aid)).fetchall()
    for r in rows:
        other=r['b'] if r['a']==aid else r['a']
        if blocked(db,aid,other):continue
        account=db.execute('SELECT avatar FROM accounts WHERE id=?',(other,)).fetchone()
        item=dict(id=other,label='Cat lover '+other.replace('-','')[:6].upper(),avatar=json.loads(account['avatar']))
        groups['friends' if r['status']=='accepted' else 'outgoing' if r['requester']==aid else 'incoming'].append(item)
    groups['blocked']=[dict(id=r[0],label='Cat lover '+r[0].replace('-','')[:6].upper(),avatar={}) for r in db.execute('SELECT target FROM friend_blocks WHERE owner=?',(aid,))]
    return dict(code=code(db,aid),**groups)

def dispatch(db,aid,method,path,body):
    if method=='GET' and path=='/friends/list':return listing(db,aid)
    if method!='POST':raise Error(404,'Friend action not found.')
    if path=='/friends/request':
        db.execute('BEGIN IMMEDIATE')
        now=time.time();db.execute('DELETE FROM friend_attempts WHERE created<?',(now-86400,))
        if db.execute('SELECT count(*) FROM friend_attempts WHERE account=?',(aid,)).fetchone()[0]>=30:raise Error(429,'Daily request limit reached. Try tomorrow.')
        # Commit failed lookup attempts too, so guessing cannot bypass the daily limit.
        db.execute('INSERT INTO friend_attempts VALUES (?,?)',(aid,now));db.commit();db.execute('BEGIN IMMEDIATE')
        value=body.get('code','')
        if not isinstance(value,str):raise Error(400,'Enter a friend code.')
        value=value.replace(' ','').replace('-','').strip().upper()
        if len(value)!=12:raise Error(400,'Enter the 12-character friend code.')
        other=db.execute('SELECT account FROM friend_codes WHERE code=?',(value,)).fetchone()
        if not other or other[0]==aid or blocked(db,aid,other[0]):raise Error(400,'This friend request cannot be sent. Check the code.')
        other=other[0];a,b=sorted([aid,other])
        existing=db.execute('SELECT * FROM friendships WHERE a=? AND b=?',(a,b)).fetchone()
        if existing:return listing(db,aid) # Reciprocal requests never auto-accept.
        for who in [aid,other]:
            if db.execute('SELECT count(*) FROM friendships WHERE a=? OR b=?',(who,who)).fetchone()[0]>=200:raise Error(409,'This account cannot accept more friend connections right now.')
            if db.execute("SELECT count(*) FROM friendships WHERE (a=? OR b=?) AND status='pending'",(who,who)).fetchone()[0]>=20:raise Error(409,'Resolve pending requests before adding more.')
        db.execute("INSERT INTO friendships VALUES (?,?,?,'pending',?)",(a,b,aid,now));return listing(db,aid)
    other=body.get('id','')
    if not isinstance(other,str) or other==aid:raise Error(400,'Choose another account.')
    a,b=sorted([aid,other]);row=db.execute('SELECT * FROM friendships WHERE a=? AND b=?',(a,b)).fetchone()
    action=path.removeprefix('/friends/')
    if action=='unblock':db.execute('DELETE FROM friend_blocks WHERE owner=? AND target=?',(aid,other));return listing(db,aid)
    if not row:raise Error(404,'This connection is no longer available.')
    if action=='accept':
        if row['status']!='pending' or row['requester']==aid or blocked(db,aid,other):raise Error(403,'Only the recipient can accept this request.')
        db.execute("UPDATE friendships SET status='accepted' WHERE a=? AND b=?",(a,b))
    elif action in ['remove','decline','cancel']:
        if action=='decline' and (row['status']!='pending' or row['requester']==aid):raise Error(403,'Only the recipient can decline.')
        if action=='cancel' and (row['status']!='pending' or row['requester']!=aid):raise Error(403,'Only the sender can cancel.')
        db.execute('DELETE FROM friendships WHERE a=? AND b=?',(a,b))
    elif action=='block':
        db.execute('INSERT OR IGNORE INTO friend_blocks VALUES (?,?)',(aid,other));db.execute('DELETE FROM friendships WHERE a=? AND b=?',(a,b))
    else:raise Error(404,'Friend action not found.')
    return listing(db,aid)
