"""Shared, cached OSM landmarks. No synthetic fallback coordinates."""
import hashlib,json,math,threading,time,urllib.parse,urllib.request
LOCK=threading.Lock()
MIN_SPACING=600
ATTEMPTS={}
class Error(Exception):pass

def init_schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS public_landmarks(id TEXT PRIMARY KEY,name TEXT,latitude REAL,longitude REAL,kind TEXT,active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS landmark_regions(id TEXT PRIMARY KEY,checked REAL);
    ''')

def distance(a,b):
    lat1,lon1=a;lat2,lon2=b
    x=math.sin(math.radians(lat2-lat1)/2)**2+math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(math.radians(lon2-lon1)/2)**2
    return 6371000*2*math.asin(min(1,math.sqrt(max(0,x))))

def fetch(south,west,north,east):
    bbox=f'{south},{west},{north},{east}'
    # Only explicitly public sites. Missing or permissive/customer access is not accepted.
    query='[out:json][timeout:18];('+''.join(f'nwr["{key}"="{value}"]["name"]["access"~"^(yes|public)$"]({bbox});' for key,value in [('leisure','park'),('leisure','garden'),('amenity','library'),('tourism','museum'),('tourism','artwork'),('historic','memorial')])+');out center tags;'
    request=urllib.request.Request('https://overpass-api.de/api/interpreter',data=urllib.parse.urlencode({'data':query}).encode(),headers={'User-Agent':'PurrQuest-Landmarks/0.10','Content-Type':'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(request,timeout=22) as response:
        raw=response.read(4_000_001)
        if len(raw)>4_000_000:raise Error('Landmark area is too large. Try again later.')
        payload=json.loads(raw)
        if payload.get('remark'):raise Error('Landmark provider returned an incomplete area.')
        return payload.get('elements',[])

def candidates(elements):
    result=[]
    for e in elements:
        tags=e.get('tags',{});name=tags.get('name','').strip()
        if not name or tags.get('access') not in ['yes','public']:continue
        if tags.get('indoor')=='yes' or tags.get('disused')=='yes' or tags.get('construction'):continue
        if not any(tags.get(k)==v for k,v in [('leisure','park'),('leisure','garden'),('amenity','library'),('tourism','museum'),('tourism','artwork'),('historic','memorial')]):continue
        point=e if e.get('type')=='node' else e.get('center',{})
        try:lat=float(point['lat']);lon=float(point['lon'])
        except (KeyError,ValueError,TypeError):continue
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:continue
        if e.get('type') not in ['node','way','relation'] or type(e.get('id'))!=int:continue
        sid='osm:'+e['type']+':'+str(e['id'])
        kind='showcase' if int(hashlib.sha256(sid.encode()).hexdigest()[:8],16)%3==0 else 'supply'
        result.append(dict(id=sid,name=name[:160],latitude=lat,longitude=lon,kind=kind))
    return sorted(result,key=lambda r:hashlib.sha256(r['id'].encode()).hexdigest())

def search(connect,body):
    try:
        lat=float(body['latitude']);lon=float(body['longitude'])
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:raise ValueError()
    except (KeyError,TypeError,ValueError):raise Error('Invalid map area.')
    # Coarse 0.05-degree area, not a retained walking trail.
    row=min(3599,int((lat+90)/.05));col=min(7199,int((lon+180)/.05))
    region=f'{row}:{col}';south=row*.05-90;west=col*.05-180
    north=min(90,south+.05);east=min(180,west+.05)
    with LOCK:
        with connect() as db:
            cached=db.execute('SELECT checked FROM landmark_regions WHERE id=?',(region,)).fetchone()
            fresh=cached and cached[0]>time.time()-7*86400
        message=''
        if not fresh and ATTEMPTS.get(region,0)>time.time()-60:
            message='Landmark lookup is cooling down. Showing saved landmarks; retry in a minute.'
        elif not fresh:
            ATTEMPTS[region]=time.time()
            for key in list(ATTEMPTS):
                if ATTEMPTS[key]<time.time()-60:del ATTEMPTS[key]
            try:
                incoming=candidates(fetch(south,west,north,east))
                with connect() as db:
                    incoming_ids={r['id'] for r in incoming}
                    # Retire sites removed from a complete provider response or moved substantially.
                    for old in db.execute('SELECT * FROM public_landmarks WHERE latitude>=? AND latitude<=? AND longitude>=? AND longitude<=?',(south,north,west,east)).fetchall():
                        new=next((r for r in incoming if r['id']==old['id']),None)
                        if old['id'] not in incoming_ids or (new and distance((old['latitude'],old['longitude']),(new['latitude'],new['longitude']))>30):
                            db.execute('UPDATE public_landmarks SET active=0 WHERE id=?',(old['id'],))
                        elif new:db.execute('UPDATE public_landmarks SET name=? WHERE id=?',(new['name'],old['id']))
                    existing=[dict(r) for r in db.execute('SELECT * FROM public_landmarks WHERE active=1')]
                    known={r['id'] for r in db.execute('SELECT id FROM public_landmarks')}
                    for r in incoming:
                        if r['id'] in known:continue
                        if not south<=r['latitude']<=north or not west<=r['longitude']<=east:continue
                        if any(distance((r['latitude'],r['longitude']),(x['latitude'],x['longitude']))<MIN_SPACING for x in existing):continue
                        db.execute('INSERT INTO public_landmarks VALUES (?,?,?,?,?,1)',(r['id'],r['name'],r['latitude'],r['longitude'],r['kind']))
                        existing.append(r);known.add(r['id'])
                    db.execute('INSERT OR REPLACE INTO landmark_regions VALUES (?,?)',(region,time.time()))
            except Exception:
                message='Landmark provider unavailable. Showing previously saved landmarks only; tap refresh to retry.'
        with connect() as db:
            rows=[dict(r) for r in db.execute('SELECT id,name,latitude,longitude,kind FROM public_landmarks WHERE active=1')]
    # Include saved neighbouring regions, nearest first. No fabricated points on outages.
    rows=sorted((r for r in rows if distance((lat,lon),(r['latitude'],r['longitude']))<=8000),key=lambda r:distance((lat,lon),(r['latitude'],r['longitude'])))[:150]
    return dict(landmarks=rows,message=message or ('Public-access landmarks from OpenStreetMap.' if rows else 'No eligible public-access landmarks found here. Walking-distance packs are still available.'))

def allowed_showcase(db,place):
    return isinstance(place,str) and db.execute("SELECT 1 FROM public_landmarks WHERE id=? AND kind='showcase' AND active=1",(place,)).fetchone() is not None
