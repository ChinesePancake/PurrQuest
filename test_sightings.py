import json,os,tempfile,time,unittest,uuid
import server

class SightingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();server.DB=os.path.join(self.tmp.name,'test.sqlite');server.init()
        self.tokens=[server.dispatch('POST','/session',{})['token'] for _ in range(5)]
        self.now=time.time();self.sid=str(uuid.uuid4())
    def tearDown(self):self.tmp.cleanup()
    def data(self,**kwargs):
        d=dict(latitude='37.770123',longitude='-122.480321',observed=str(self.now-3600),colors=json.dumps(['Black','White']),pattern='Bicolour / tuxedo',category='Possibly lost',collar='Seen',earTip='Unsure');d.update(kwargs);return d
    def post(self,owner=0,sid=None,**kwargs):return server.dispatch('POST','/sightings/'+(sid or self.sid),self.data(**kwargs),self.tokens[owner])
    def search(self,owner=1,**kwargs):
        q=dict(latitude='37.77',longitude='-122.48',radius='5',days='7',colors='[]',pattern='',category='');q.update(kwargs)
        return server.dispatch('POST','/sightings/search',q,self.tokens[owner])['reports']
    def change(self,action,owner=0,**body):
        return server.dispatch('DELETE' if action=='delete' else 'POST','/sightings/'+self.sid+('' if action=='delete' else '/'+action),body,self.tokens[owner])
    def test_publish_rounding_minimal_payload_and_retry(self):
        self.post();self.post()
        reports=self.search();self.assertEqual(len(reports),1)
        self.assertEqual(reports[0]['latitude'],37.77);self.assertEqual(reports[0]['longitude'],-122.48)
        self.assertFalse(reports[0]['mine']);self.assertNotIn('player',reports[0]);self.assertNotIn('token',reports[0])
        with server.connect() as db:
            row=db.execute('SELECT * FROM sightings').fetchone();self.assertEqual(row['lat'],37.77);self.assertEqual(row['lon'],-122.48)
        self.assertTrue(self.search(owner=0)[0]['mine'])
    def test_color_pattern_status_distance_time_filters(self):
        self.post();self.assertEqual(len(self.search(colors='["Black","White"]')),1)
        self.assertEqual(self.search(colors='["Orange / ginger"]'),[])
        self.assertEqual(self.search(pattern='Tabby'),[]);self.assertEqual(self.search(category='Community cat — unverified'),[])
        self.assertEqual(self.search(latitude='0'),[])
        self.post(sid=str(uuid.uuid4()),observed=str(self.now-2*86400))
        self.assertEqual(len(self.search(days='1')),1);self.assertEqual(len(self.search(days='7')),2)
    def test_dateline_and_polar_distance(self):
        self.post(latitude='0',longitude='179.999')
        self.assertEqual(len(self.search(latitude='0',longitude='-179.999',radius='1')),1)
    def test_owner_only_resolve_delete_and_idempotent_deletion(self):
        self.post()
        for action in ['resolve','delete']:
            with self.assertRaises(server.APIError) as e:self.change(action,owner=1)
            self.assertEqual(e.exception.status,403)
        self.change('resolve');self.assertEqual(self.search(),[])
        mine=server.dispatch('GET','/sightings/mine',{},self.tokens[0])['reports'];self.assertTrue(mine[0]['resolved'])
        self.change('delete');self.change('delete')
        self.assertEqual(server.dispatch('GET','/sightings/mine',{},self.tokens[0])['reports'],[])
    def test_blocking_is_viewer_specific_and_unblock(self):
        self.post();self.change('block',owner=1)
        self.assertEqual(self.search(owner=1),[]);self.assertEqual(len(self.search(owner=2)),1)
        server.dispatch('POST','/sightings/unblock',{},self.tokens[1]);self.assertEqual(len(self.search()),1)
    def test_unique_flags_hide_at_three_and_owner_can_still_manage(self):
        self.post()
        for _ in range(3):self.change('flag',owner=1,reason='Incorrect sighting')
        self.assertEqual(len(self.search()),1)
        for i in [2,3]:self.change('flag',owner=i,reason='Unsafe or private location')
        self.assertEqual(self.search(),[])
        self.assertTrue(server.dispatch('GET','/sightings/mine',{},self.tokens[0])['reports'][0]['hidden'])
        self.change('delete')
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM sighting_flags').fetchone()[0],0)
    def test_expiry_and_cascade_identity_deletion(self):
        self.post()
        with server.connect() as db:db.execute('UPDATE sightings SET observed=?',(self.now-31*86400,))
        self.assertEqual(self.search(days='30'),[])
        http=server.CareHTTPServer(('127.0.0.1',0),server.Handler)
        try:http.service_actions()
        finally:http.server_close()
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM sightings').fetchone()[0],0)
        self.post();server.dispatch('DELETE','/session',{},self.tokens[0]);self.assertEqual(self.search(),[])
    def test_validation_auth_and_report_limit(self):
        for bad in [dict(latitude='nan'),dict(longitude='181'),dict(observed=str(self.now+500)),dict(observed=str(self.now-31*86400)),dict(colors='[]'),dict(colors='["fake"]'),dict(notes='private text'),dict(category='Confirmed feral')]:
            with self.subTest(bad=bad),self.assertRaises(server.APIError):self.post(**bad)
        with self.assertRaises(server.APIError):server.dispatch('POST','/sightings/'+self.sid,self.data(),'bad')
        for _ in range(20):self.post(sid=str(uuid.uuid4()))
        with self.assertRaises(server.APIError) as e:self.post()
        self.assertEqual(e.exception.status,429)

if __name__=='__main__':unittest.main()
