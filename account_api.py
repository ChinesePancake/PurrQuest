"""OIDC browser sign-in, opaque app sessions, profiles and shared showcases.
Provider credentials and encryption key belong on the server only.
"""
import landmarks
import friends_api
import base64,hashlib,hmac,io,json,math,os,secrets,time,urllib.parse,urllib.request,urllib.error,uuid


class Error(Exception):
    def __init__(self,status,message):self.status,self.message=status,message

def init_schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,provider TEXT,subject TEXT,grant_token TEXT,avatar TEXT NOT NULL DEFAULT '{}',UNIQUE(provider,subject));
    CREATE TABLE IF NOT EXISTS account_sessions(token TEXT PRIMARY KEY,account TEXT REFERENCES accounts(id) ON DELETE CASCADE,expires REAL);
    CREATE TABLE IF NOT EXISTS oauth_flows(state TEXT PRIMARY KEY,provider TEXT,nonce TEXT,verifier TEXT,challenge TEXT,created REAL);
    CREATE TABLE IF NOT EXISTS oauth_tickets(ticket TEXT PRIMARY KEY,account TEXT REFERENCES accounts(id) ON DELETE CASCADE,challenge TEXT,expires REAL);
    CREATE TABLE IF NOT EXISTS account_players(player TEXT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,account TEXT REFERENCES accounts(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS owned_cats(account TEXT REFERENCES accounts(id) ON DELETE CASCADE,id TEXT,kind TEXT,breed TEXT,virtual_id TEXT,xp INTEGER,colors TEXT,PRIMARY KEY(account,id));
    CREATE TABLE IF NOT EXISTS showcases(id TEXT PRIMARY KEY,account TEXT REFERENCES accounts(id) ON DELETE CASCADE,cat TEXT,place TEXT,created REAL,hidden INTEGER DEFAULT 0,UNIQUE(account,cat,place));
    CREATE INDEX IF NOT EXISTS showcase_place ON showcases(place);
    CREATE TABLE IF NOT EXISTS showcase_flags(showcase TEXT REFERENCES showcases(id) ON DELETE CASCADE,account TEXT REFERENCES accounts(id) ON DELETE CASCADE,PRIMARY KEY(showcase,account));
    ''')

def env(name):return os.environ.get(name,'')
def base_url():
    value=env('ACCOUNT_PUBLIC_URL').rstrip('/');p=urllib.parse.urlparse(value)
    if p.scheme!='https' or not p.netloc or p.query or p.fragment or p.username:raise Error(503,'Account service needs a permanent HTTPS URL.')
    return value

def provider_config(provider):
    if provider not in ['apple','google']:raise Error(400,'Unknown identity provider.')
    if provider=='google':return dict(client=env('GOOGLE_WEB_CLIENT_ID'),secret=env('GOOGLE_WEB_CLIENT_SECRET'),authorize='https://accounts.google.com/o/oauth2/v2/auth',token='https://oauth2.googleapis.com/token',issuer=['https://accounts.google.com','accounts.google.com'],jwks='https://www.googleapis.com/oauth2/v3/certs',revoke='https://oauth2.googleapis.com/revoke')
    return dict(client=env('APPLE_SERVICE_ID'),secret='',authorize='https://appleid.apple.com/auth/authorize',token='https://appleid.apple.com/auth/token',issuer=['https://appleid.apple.com'],jwks='https://appleid.apple.com/auth/keys',revoke='https://appleid.apple.com/auth/revoke')

def cipher():
    from cryptography.fernet import Fernet
    return Fernet(env('ACCOUNT_ENCRYPTION_KEY').encode())

def configured(provider):
    try:
        import jwt
        base_url();cipher()
        c=provider_config(provider)
        return bool(c['client'] and (c['secret'] if provider=='google' else env('APPLE_TEAM_ID') and env('APPLE_KEY_ID') and env('APPLE_PRIVATE_KEY_PATH')))
    except Exception:return False

def secret(provider):
    c=provider_config(provider)
    if provider=='google':return c['secret']
    import jwt
    with open(env('APPLE_PRIVATE_KEY_PATH'),'r') as f:key=f.read()
    now=int(time.time())
    return jwt.encode(dict(iss=env('APPLE_TEAM_ID'),iat=now,exp=now+300,aud='https://appleid.apple.com',sub=c['client']),key,algorithm='ES256',headers={'kid':env('APPLE_KEY_ID')})

def form_post(url,body):
    req=urllib.request.Request(url,data=urllib.parse.urlencode(body).encode(),headers={'Content-Type':'application/x-www-form-urlencoded'},method='POST')
    with urllib.request.urlopen(req,timeout=12) as response:
        raw=response.read(1024*1024)
        return json.loads(raw) if raw else {}

def verify_id_token(token,provider,nonce):
    import jwt
    c=provider_config(provider)
    header=jwt.get_unverified_header(token)
    if header.get('alg')!='RS256':raise Error(401,'Invalid token algorithm.')
    key=jwt.PyJWKClient(c['jwks'],timeout=10).get_signing_key_from_jwt(token).key
    claims=jwt.decode(token,key,algorithms=['RS256'],audience=c['client'],issuer=c['issuer'],leeway=30,
        options={'require':['exp','iat','sub','iss','aud','nonce']})
    if not isinstance(claims['sub'],str) or not claims['sub'] or len(claims['sub'])>256 or not hmac.compare_digest(str(claims['nonce']),nonce):raise Error(401,'Invalid identity token.')
    return claims

def encrypt(value):return cipher().encrypt(value.encode()).decode()
def decrypt(value):return cipher().decrypt(value.encode()).decode()
def token_hash(value):return hashlib.sha256(value.encode()).hexdigest()
def challenge(value):return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).decode().rstrip('=')

def oauth_callback(connect,provider,params):
    state=params.get('state','')
    # Consume state before contacting the provider; callbacks cannot replay.
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM oauth_flows WHERE state=?',(token_hash(state),)).fetchone()
        if not row or row['provider']!=provider or row['created']<time.time()-600:raise Error(400,'Expired sign-in. Start again in the app.')
        db.execute('DELETE FROM oauth_flows WHERE state=?',(token_hash(state),))
    try:
        if params.get('error') or not params.get('code'):raise Error(401,'Sign-in cancelled.')
        c=provider_config(provider)
        body=dict(client_id=c['client'],client_secret=secret(provider),code=params['code'],grant_type='authorization_code',redirect_uri=base_url()+'/oauth/callback/'+provider)
        if provider=='google':body['code_verifier']=row['verifier']
        tokens=form_post(c['token'],body)
        claims=verify_id_token(tokens['id_token'],provider,row['nonce'])
        with connect() as db:
            account=db.execute('SELECT * FROM accounts WHERE provider=? AND subject=?',(provider,claims['sub'])).fetchone()
            aid=account['id'] if account else str(uuid.uuid4())
            grant=encrypt(tokens['refresh_token']) if tokens.get('refresh_token') else account['grant_token'] if account else encrypt(tokens['access_token']) if tokens.get('access_token') else None
            if not grant:raise Error(503,'No revocable provider grant was received.')
            if account:db.execute('UPDATE accounts SET grant_token=? WHERE id=?',(grant,aid))
            else:db.execute('INSERT INTO accounts(id,provider,subject,grant_token) VALUES (?,?,?,?)',(aid,provider,claims['sub'],grant))
            ticket=secrets.token_urlsafe(32)
            db.execute('INSERT INTO oauth_tickets VALUES (?,?,?,?)',(token_hash(ticket),aid,row['challenge'],time.time()+60))
        return 'purrquest://auth?ticket='+urllib.parse.quote(ticket)
    except Exception:
        # Never put provider tokens or detailed exceptions in the callback URL.
        return 'purrquest://auth?error=signin_failed'

def authenticated(db,token):
    row=db.execute('SELECT a.* FROM accounts a JOIN account_sessions s ON a.id=s.account WHERE s.token=? AND s.expires>?',(token_hash(token),time.time())).fetchone()
    if not row:raise Error(401,'Sign in again to manage your account.')
    return row

def owned_public(row):
    return dict(id=row['id'],kind=row['kind'],breed=row['breed'],virtualID=row['virtual_id'],xp=row['xp'],level=min(50,1+row['xp']//100),colors=json.loads(row['colors']))
def account_public(db,a):return dict(friendCode=friends_api.code(db,a['id']),id=a['id'],provider=a['provider'],avatar=json.loads(a['avatar']),cats=[owned_public(c) for c in db.execute('SELECT * FROM owned_cats WHERE account=?',(a['id'],))])

def revoke(a):
    provider=a['provider'];c=provider_config(provider)
    body={'token':decrypt(a['grant_token'])}
    if provider=='apple':body.update(client_id=c['client'],client_secret=secret(provider))
    try:form_post(c['revoke'],body)
    except urllib.error.HTTPError as e:
        # Already-invalid grants need no further revocation. Other errors leave deletion retryable.
        payload=e.read(16384).decode(errors='replace')
        if e.code==400 and 'invalid_token' in payload:return
        raise Error(503,'Provider revocation failed. Account deletion is not complete; retry.')

def place_center(place):
    try:
        row,col=map(int,place.split(':'))
        if not 0<=row<36000 or not 0<=col<72000:raise ValueError()
        return (row*.005-90+.0025,col*.005-180+.0025)
    except (ValueError,TypeError):raise Error(400,'Invalid showcase location.')

def dispatch(connect,method,path,body,token,catalog):
    now=time.time()
    with connect() as db:
        if path=='/account/config' and method=='GET':return dict(apple=configured('apple'),google=configured('google'))
        if path=='/account/begin' and method=='POST':
            provider=body.get('provider');client_challenge=body.get('challenge','')
            if not configured(provider):raise Error(503,'This sign-in provider is not configured yet.')
            if not isinstance(client_challenge,str) or len(client_challenge)!=43 or not all(c.isalnum() or c in '-_' for c in client_challenge):raise Error(400,'Invalid sign-in challenge.')
            state=secrets.token_urlsafe(32);nonce=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48);c=provider_config(provider)
            db.execute('INSERT INTO oauth_flows VALUES (?,?,?,?,?,?)',(token_hash(state),provider,nonce,verifier,client_challenge,now))
            params=dict(client_id=c['client'],redirect_uri=base_url()+'/oauth/callback/'+provider,response_type='code',state=state,nonce=nonce)
            if provider=='google':params.update(scope='openid',code_challenge=challenge(verifier),code_challenge_method='S256',access_type='offline',prompt='consent')
            else:params.update(response_mode='form_post')
            return dict(url=c['authorize']+'?'+urllib.parse.urlencode(params))
        if path=='/account/exchange' and method=='POST':
            verifier=body.get('verifier','')
            if not isinstance(verifier,str) or not 43<=len(verifier)<=128:raise Error(400,'Invalid handoff proof.')
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM oauth_tickets WHERE ticket=? AND expires>?',(token_hash(body.get('ticket','')),now)).fetchone()
            if not row or not hmac.compare_digest(row['challenge'],challenge(verifier)):raise Error(401,'Expired or invalid sign-in handoff.')
            db.execute('DELETE FROM oauth_tickets WHERE ticket=?',(token_hash(body['ticket']),))
            raw=secrets.token_urlsafe(32);db.execute('INSERT INTO account_sessions VALUES (?,?,?)',(token_hash(raw),row['account'],now+30*86400))
            a=db.execute('SELECT * FROM accounts WHERE id=?',(row['account'],)).fetchone()
            return dict(token=raw,account=account_public(db,a))
        if path=='/showcases/search' and method=='POST':
            places=body.get('places',[])
            if not isinstance(places,list) or not 1<=len(places)<=150:raise Error(400,'Choose up to 150 locations.')
            for place in places:
                if not landmarks.allowed_showcase(db,place):
                    if isinstance(place,str) and place.startswith('osm:'):continue
                    place_center(place)
            placeholders=','.join('?' for _ in places)
            rows=db.execute('SELECT s.*,c.kind,c.breed,c.virtual_id,c.xp,c.colors,a.avatar FROM showcases s JOIN owned_cats c ON c.account=s.account AND c.id=s.cat JOIN accounts a ON a.id=s.account WHERE s.place IN ('+placeholders+') AND s.created>? AND s.hidden=0 AND EXISTS (SELECT 1 FROM public_landmarks l WHERE l.id=s.place AND l.active=1) ORDER BY s.created DESC LIMIT 101',(*places,now-7*86400)).fetchall()
            mine=db.execute('SELECT account FROM account_sessions WHERE token=? AND expires>?',(token_hash(token),now)).fetchone()
            return dict(entries=[dict(id=r['id'],place=r['place'],cat=owned_public(dict(r,id=r['cat'])),avatar=json.loads(r['avatar']),mine=bool(mine and mine['account']==r['account']),expires=r['created']+7*86400) for r in rows[:100]],truncated=len(rows)>100)
        a=authenticated(db,token);aid=a['id']
        if path.startswith('/friends/'):
            try:return friends_api.dispatch(db,aid,method,path,body)
            except friends_api.Error as e:raise Error(e.status,e.message)
        if path=='/showcases/mine' and method=='GET':
            rows=db.execute('SELECT s.*,c.kind,c.breed,c.virtual_id,c.xp,c.colors FROM showcases s JOIN owned_cats c ON c.account=s.account AND c.id=s.cat WHERE s.account=? ORDER BY s.created DESC',(aid,)).fetchall()
            return dict(entries=[dict(id=r['id'],place=r['place'],cat=owned_public(dict(r,id=r['cat'])),avatar=json.loads(a['avatar']),mine=True,expires=r['created']+7*86400) for r in rows],truncated=False)
        if path=='/account/me' and method=='GET':return account_public(db,a)
        if path=='/account/logout' and method=='POST':db.execute('DELETE FROM account_sessions WHERE token=?',(token_hash(token),));return {'ok':True}
        if path=='/account/attach' and method=='POST':
            tokens=body.get('tokens',[])
            if not isinstance(tokens,list) or len(tokens)>3:raise Error(400,'Invalid guest identities.')
            for raw in tokens:
                player=db.execute('SELECT id FROM players WHERE token=?',(token_hash(raw),)).fetchone()
                if not player:continue
                link=db.execute('SELECT account FROM account_players WHERE player=?',(player['id'],)).fetchone()
                if link and link['account']!=aid:raise Error(409,'This device identity is already linked to another account.')
                db.execute('INSERT OR IGNORE INTO account_players VALUES (?,?)',(player['id'],aid))
            return {'ok':True}
        if path=='/account/delete' and method=='POST':
            session=db.execute('SELECT expires FROM account_sessions WHERE token=?',(token_hash(token),)).fetchone()
            if session['expires'] < now+30*86400-600:raise Error(401,'Verify sign-in again, then delete within 10 minutes.')
            try:revoke(a)
            except Error:raise
            except Exception:raise Error(503,'Provider revocation unavailable. Deletion is not complete; retry.')
            hashes=[r[0] for r in db.execute('SELECT token FROM players WHERE id IN (SELECT player FROM account_players WHERE account=?)',(aid,))]
            db.execute('DELETE FROM players WHERE id IN (SELECT player FROM account_players WHERE account=?)',(aid,))
            db.execute('DELETE FROM accounts WHERE id=?',(aid,))
            return {'ok':True,'guestTokenHashes':hashes}
        if path=='/account/cats' and method=='POST':
            cats=body.get('cats',[])
            if not isinstance(cats,list) or len(cats)>150:raise Error(400,'Invalid owned-cat list.')
            for cat in cats:
                if set(cat)!={'id','kind','breed','virtualID','xp','colors'}:raise Error(400,'Invalid cat fields.')
                cid=cat['id'];kind=cat['kind'];xp=cat['xp'];colors=cat['colors']
                if not isinstance(cid,str) or not 1<=len(cid)<=80 or kind not in ['real','virtual']:raise Error(400,'Invalid cat.')
                if type(xp)!=int or not 0<=xp<=4900 or (kind=='real' and xp!=0):raise Error(400,'Invalid cat XP.')
                if not isinstance(colors,list) or not 1<=len(colors)<=3 or not set(colors)<=set(['Black','White','Grey / blue','Orange / ginger','Brown','Cream','Other / unsure']):raise Error(400,'Invalid coat colours.')
                if cat['breed'] not in catalog['breeds']+['Unknown','Mixed breed']:raise Error(400,'Invalid breed.')
                if kind=='virtual' and (cat['virtualID'] not in catalog['cats'] or cat['breed']!=catalog['breeds'][catalog['cats'].index(cat['virtualID'])]):raise Error(400,'Invalid virtual cat.')
                if kind=='real' and cat['virtualID']!='':raise Error(400,'Invalid real cat.')
                old=db.execute('SELECT xp FROM owned_cats WHERE account=? AND id=?',(aid,cid)).fetchone()
                value=max(old['xp'] if old else 0,xp)
                db.execute('INSERT INTO owned_cats VALUES (?,?,?,?,?,?,?) ON CONFLICT(account,id) DO UPDATE SET breed=excluded.breed,xp=excluded.xp,colors=excluded.colors',(aid,cid,kind,cat['breed'],cat['virtualID'],value,json.dumps(colors)))
            return account_public(db,db.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone())
        if path=='/account/avatar' and method=='POST':
            cid=body.get('catID','');cat=db.execute('SELECT * FROM owned_cats WHERE account=? AND id=?',(aid,cid)).fetchone()
            if not cat:raise Error(400,'Choose a cat from your owned-cat list.')
            avatar={'catID':cid,'virtualID':cat['virtual_id'],'jpeg':''}
            if cat['kind']=='real':
                from PIL import Image
                try:
                    raw=base64.b64decode(body.get('jpeg',''),validate=True)
                    if len(raw)>150000:raise ValueError()
                    with Image.open(io.BytesIO(raw)) as image:
                        if image.width*image.height>4_000_000:raise ValueError()
                        clean=image.convert('RGB');clean.thumbnail((192,192));out=io.BytesIO();clean.save(out,format='JPEG',quality=75)
                    avatar['jpeg']=base64.b64encode(out.getvalue()).decode()
                except Exception:raise Error(400,'Choose a small valid cat photo.')
            db.execute('UPDATE accounts SET avatar=? WHERE id=?',(json.dumps(avatar),aid));return account_public(db,db.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone())
        if path=='/showcases/post' and method=='POST':
            place=body.get('place','');cid=body.get('catID','')
            if not landmarks.allowed_showcase(db,place):raise Error(400,'Choose an active public landmark on the map.')
            if not db.execute('SELECT 1 FROM owned_cats WHERE account=? AND id=?',(aid,cid)).fetchone():raise Error(400,'Register your cat first.')
            existing=db.execute('SELECT id,hidden FROM showcases WHERE account=? AND cat=? AND place=?',(aid,cid,place)).fetchone()
            if existing and existing['hidden']:raise Error(403,'This showcase is hidden pending review.')
            if not existing and db.execute('SELECT count(*) FROM showcases WHERE account=? AND created>?',(aid,now-7*86400)).fetchone()[0]>=20:raise Error(429,'Remove a showcase before adding another.')
            if existing:db.execute('UPDATE showcases SET created=? WHERE id=?',(now,existing['id']))
            else:db.execute('INSERT INTO showcases VALUES (?,?,?,?,?,0)',(str(uuid.uuid4()),aid,cid,place,now))
            return {'ok':True}
        if path.startswith('/showcases/') and method=='POST':
            parts=path.split('/')
            if len(parts)!=4:raise Error(404,'Unknown route.')
            sid,action=parts[2:];row=db.execute('SELECT * FROM showcases WHERE id=?',(sid,)).fetchone()
            if not row:raise Error(404,'Showcase unavailable.')
            if action=='delete' and row['account']==aid:db.execute('DELETE FROM showcases WHERE id=?',(sid,));return {'ok':True}
            if action=='flag' and row['account']!=aid:
                db.execute('INSERT OR IGNORE INTO showcase_flags VALUES (?,?)',(sid,aid))
                if db.execute('SELECT count(*) FROM showcase_flags WHERE showcase=?',(sid,)).fetchone()[0]>=3:db.execute('UPDATE showcases SET hidden=1 WHERE id=?',(sid,))
                return {'ok':True}
            raise Error(403,'Action not allowed.')
        raise Error(404,'Account route not found.')
