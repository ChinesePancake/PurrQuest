"""Local operator review for structured sightings; uses the server's PURRQUEST_DB.
Run from the original server folder. No public admin route is exposed.
"""
import argparse,json
import server
parser=argparse.ArgumentParser()
parser.add_argument('action',choices=['queue','hide','restore','delete'])
parser.add_argument('--id',help='Report UUID; required for mutations')
args=parser.parse_args()
server.init()
with server.connect() as db:
    if args.action=='queue':
        rows=db.execute('''SELECT s.id,s.lat,s.lon,s.observed,s.colors,s.pattern,s.category,s.hidden,
            count(f.player) AS flags,group_concat(f.reason,'; ') AS reasons
            FROM sightings s JOIN sighting_flags f ON f.sighting=s.id GROUP BY s.id ORDER BY flags DESC''').fetchall()
        print(json.dumps([dict(row) for row in rows],indent=2))
    else:
        if not args.id:parser.error('--id is required')
        if not db.execute('SELECT 1 FROM sightings WHERE id=?',(args.id,)).fetchone():parser.error('Report not found')
        if args.action=='delete':db.execute('DELETE FROM sightings WHERE id=?',(args.id,))
        elif args.action=='hide':db.execute('UPDATE sightings SET hidden=1 WHERE id=?',(args.id,))
        else:
            db.execute('UPDATE sightings SET hidden=0 WHERE id=?',(args.id,))
            db.execute('DELETE FROM sighting_flags WHERE sighting=?',(args.id,))
        print('Review action saved. Report UUID:',args.id)
