"""Local operator controls. Run beside server.py using the same PURRQUEST_DB."""
import argparse,json
import server
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('action',choices=['list','hide','remove','clear-avatar'])
parser.add_argument('id',nargs='?')
a=parser.parse_args()
server.init()
with server.connect() as db:
    if a.action=='list':
        for r in db.execute('SELECT s.id,s.account,s.place,s.hidden,count(f.account) flags FROM showcases s LEFT JOIN showcase_flags f ON f.showcase=s.id GROUP BY s.id ORDER BY s.created DESC LIMIT 200'):
            print(json.dumps(dict(r)))
    else:
        if not a.id:parser.error('An ID is required.')
        if a.action=='hide':db.execute('UPDATE showcases SET hidden=1 WHERE id=?',(a.id,))
        elif a.action=='remove':db.execute('DELETE FROM showcases WHERE id=?',(a.id,))
        else:db.execute("UPDATE accounts SET avatar='{}' WHERE id=?",(a.id,))
        print('Operator action committed.')
