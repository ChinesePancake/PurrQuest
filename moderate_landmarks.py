"""Manage shared landmarks on the original server/database."""
import argparse,json
import server
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('action',choices=['list','disable']);p.add_argument('id',nargs='?');a=p.parse_args()
server.init()
with server.connect() as db:
    if a.action=='list':
        for row in db.execute('SELECT * FROM public_landmarks ORDER BY name'):print(json.dumps(dict(row)))
    else:
        if not a.id:p.error('Supply an exact osm:type:id from list.')
        db.execute('UPDATE public_landmarks SET active=0 WHERE id=?',(a.id,));print('Landmark disabled. Devices must refresh their maps.')
