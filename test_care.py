import datetime, json, os, tempfile, time, unittest, uuid
import server

class CareSharingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); server.DB=os.path.join(self.tmp.name,'care.sqlite')
        server.init()
        self.tokens=[server.dispatch('POST','/session',{})['token'] for _ in range(5)]
        self.ids=[str(uuid.uuid4()) for _ in range(5)]
    def tearDown(self): self.tmp.cleanup()
    def payload(self, **extra):
        result=dict(breed='Siamese',age='1–6 years',sex='Female',periodEnd=datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
            foodTotal='300',foodDays='3',waterTotal='0',waterDays='0',poopTotal='6',poopDays='3')
        result.update(extra);return result
    def post(self,i,**extra): return server.dispatch('POST','/care/'+self.ids[i],self.payload(**extra),self.tokens[i])
    def peers(self,i=0): return server.dispatch('GET','/care/'+self.ids[i]+'/cohort',{},self.tokens[i])
    def test_no_upload_until_explicit_post_and_auth_required(self):
        with server.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM care_shares').fetchone()[0],0)
        with self.assertRaises(server.APIError): server.dispatch('POST','/care/'+self.ids[0],self.payload(),'bad')
    def test_threshold_matching_and_missing_days(self):
        for i in range(3): self.post(i)
        self.assertIsNone(self.peers()['food']['mean'])
        self.post(3,sex='Male');self.assertIsNone(self.peers()['food']['mean'])
        self.post(3,age='7–10 years');self.assertIsNone(self.peers()['food']['mean'])
        self.post(3,breed='Bengal');self.assertIsNone(self.peers()['food']['mean'])
        self.post(3)
        result=self.peers()
        self.assertEqual(result['food'],dict(owners=3,mean=100))
        self.assertEqual(result['poop'],dict(owners=3,mean=2))
        self.assertEqual(result['water'],dict(owners=0,mean=None))
        self.assertNotIn('Siamese',json.dumps(result));self.assertNotIn('player',json.dumps(result))
    def test_only_owner_can_remove_and_update_does_not_duplicate(self):
        self.post(0);self.post(0,foodTotal='600')
        server.dispatch('DELETE','/care/'+self.ids[0],{},self.tokens[1])
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM care_shares').fetchone()[0],1)
            self.assertEqual(json.loads(db.execute('SELECT payload FROM care_shares').fetchone()[0])['foodTotal'],'600')
        server.dispatch('DELETE','/care/'+self.ids[0],{},self.tokens[0])
        server.dispatch('DELETE','/care/'+self.ids[0],{},self.tokens[0])
        with server.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM care_shares').fetchone()[0],0)
    def test_payload_minimization_and_invalid_numbers(self):
        for extra in [dict(note='private'),dict(location='secret'),dict(foodTotal='nan'),dict(foodTotal='-1'),
            dict(waterTotal='100',waterDays='0'),dict(foodDays='8'),dict(poopTotal='1.5'),dict(sex='Unknown'),dict(breed='Unknown')]:
            with self.subTest(extra=extra),self.assertRaises(server.APIError): self.post(0,**extra)
    def test_expiry_and_identity_deletion(self):
        for i in range(4):self.post(i)
        with server.connect() as db: db.execute('UPDATE care_shares SET updated=? WHERE profile=?',(time.time()-31*86400,self.ids[3]))
        self.assertIsNone(self.peers()['food']['mean'])
        self.post(3)
        server.dispatch('DELETE','/session',{},self.tokens[3])
        self.assertIsNone(self.peers()['food']['mean'])
    def test_multiple_cats_from_one_owner_cannot_satisfy_threshold(self):
        self.post(0);self.post(1)
        for _ in range(4): server.dispatch('POST','/care/'+str(uuid.uuid4()),self.payload(),self.tokens[1])
        self.assertIsNone(self.peers()['food']['mean'])
    def test_one_owner_has_equal_weight_with_multiple_cats(self):
        for i in range(4):self.post(i)
        server.dispatch('POST','/care/'+str(uuid.uuid4()),self.payload(foodTotal='900'),self.tokens[1])
        self.assertAlmostEqual(self.peers()['food']['mean'], (200+100+100)/3)
        self.assertEqual(self.peers()['food']['owners'],3)
    def test_background_cleanup_without_requests(self):
        self.post(0)
        with server.connect() as db: db.execute('UPDATE care_shares SET updated=?',(time.time()-31*86400,))
        http=server.CareHTTPServer(('127.0.0.1',0),server.Handler)
        try: http.service_actions()
        finally: http.server_close()
        with server.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM care_shares').fetchone()[0],0)
    def test_zero_is_observed_not_missing(self):
        self.post(0)
        for i in range(1,4):self.post(i,foodTotal='0')
        self.assertEqual(self.peers()['food'],dict(owners=3,mean=0))

if __name__=='__main__': unittest.main()
