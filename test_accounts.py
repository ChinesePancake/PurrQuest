import base64,io,json,os,tempfile,time,unittest,uuid
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
from cryptography.fernet import Fernet
from PIL import Image
import server,account_api as api

class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();server.DB=os.path.join(self.tmp.name,'test.sqlite');server.init()
        self.env=patch.dict(os.environ,{'ACCOUNT_PUBLIC_URL':'https://example.test','ACCOUNT_ENCRYPTION_KEY':Fernet.generate_key().decode(),'GOOGLE_WEB_CLIENT_ID':'client','GOOGLE_WEB_CLIENT_SECRET':'secret'});self.env.start()
        self.tokens=[];self.ids=[]
        with server.connect() as db:
            for i in range(4):
                aid=str(uuid.uuid4());token='account-token-'+str(i);self.tokens.append(token);self.ids.append(aid)
                db.execute('INSERT INTO accounts(id,provider,subject,grant_token) VALUES (?,?,?,?)',(aid,'google','subject'+str(i),api.encrypt('provider-grant')))
                db.execute('INSERT INTO account_sessions VALUES (?,?,?)',(api.token_hash(token),aid,time.time()+30*86400))
        with server.connect() as db:
            db.execute("INSERT INTO public_landmarks VALUES ('osm:node:100','Public library',0,0,'showcase',1)")
        self.cat={'id':'v:miso','kind':'virtual','breed':'Domestic Shorthair','virtualID':'miso','xp':240,'colors':['Orange / ginger','White']}
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def call(self,path,body=None,owner=0,method='POST'):
        return server.dispatch(method,path,body or {},self.tokens[owner])
    def register(self,owner=0,cat=None):return self.call('/account/cats',{'cats':[cat or self.cat]},owner)
    def search(self):return server.dispatch('POST','/showcases/search',{'places':['osm:node:100']})['entries']
    def test_handoff_challenge_ticket_expiry_replay_and_state(self):
        verifier='v'*43
        with patch.object(api,'configured',return_value=True):
            begin=server.dispatch('POST','/account/begin',{'provider':'google','challenge':api.challenge(verifier)})
        query=parse_qs(urlparse(begin['url']).query);state=query['state'][0]
        with patch.object(api,'form_post',return_value={'id_token':'signed-provider-token','refresh_token':'grant'}),patch.object(api,'verify_id_token',return_value={'sub':'new-subject'}) as verify:
            callback=api.oauth_callback(server.connect,'google',{'state':state,'code':'provider-code'})
            verify.assert_called_once_with('signed-provider-token','google',query['nonce'][0])
        ticket=parse_qs(urlparse(callback).query)['ticket'][0]
        with self.assertRaises(server.APIError):server.dispatch('POST','/account/exchange',{'ticket':ticket,'verifier':'x'*43})
        result=server.dispatch('POST','/account/exchange',{'ticket':ticket,'verifier':verifier})
        self.assertEqual(result['account']['provider'],'google')
        with self.assertRaises(server.APIError):server.dispatch('POST','/account/exchange',{'ticket':ticket,'verifier':verifier})
        with self.assertRaises(api.Error):api.oauth_callback(server.connect,'google',{'state':state,'code':'again'})
    def test_write_auth_owned_cats_levels_and_isolation(self):
        with self.assertRaises(server.APIError):server.dispatch('POST','/account/cats',{'cats':[self.cat]},'guest')
        result=self.register();self.assertEqual(result['cats'][0]['level'],3)
        downgraded=dict(self.cat,xp=10);self.register(cat=downgraded)
        self.assertEqual(self.call('/account/me',method='GET')['cats'][0]['xp'],240)
        with self.assertRaises(server.APIError):self.call('/showcases/post',{'catID':'v:miso','place':'osm:node:100'},owner=1)
        with self.assertRaises(server.APIError):self.register(cat=dict(self.cat,xp=5000))
        with self.assertRaises(server.APIError):self.register(cat=dict(self.cat,breed='Sphynx'))
    def test_virtual_avatar_must_be_owned_and_real_photo_is_reencoded(self):
        with self.assertRaises(server.APIError):self.call('/account/avatar',{'catID':'v:miso'})
        self.register();result=self.call('/account/avatar',{'catID':'v:miso'});self.assertEqual(result['avatar']['virtualID'],'miso')
        real=dict(self.cat,id='r:1',kind='real',virtualID='',xp=0)
        self.register(cat=real)
        with self.assertRaises(server.APIError):self.call('/account/avatar',{'catID':'r:1','jpeg':'invalid'})
        out=io.BytesIO();Image.new('RGB',(300,300),'orange').save(out,format='PNG')
        result=self.call('/account/avatar',{'catID':'r:1','jpeg':base64.b64encode(out.getvalue()).decode()})
        image=Image.open(io.BytesIO(base64.b64decode(result['avatar']['jpeg'])))
        self.assertEqual(image.format,'JPEG');self.assertLessEqual(image.width,192)
    def test_shared_places_visibility_deletion_flags_and_expiry(self):
        self.register();self.call('/showcases/post',{'catID':'v:miso','place':'osm:node:100'})
        entries=self.search();self.assertEqual(len(entries),1);self.assertEqual(entries[0]['cat']['level'],3)
        self.assertFalse(entries[0]['mine']);self.assertNotIn('account',entries[0])
        sid=entries[0]['id']
        with self.assertRaises(server.APIError):self.call('/showcases/'+sid+'/delete',owner=1)
        for _ in range(3):self.call('/showcases/'+sid+'/flag',owner=1)
        self.assertEqual(len(self.search()),1)
        self.call('/showcases/'+sid+'/flag',owner=2);self.call('/showcases/'+sid+'/flag',owner=3)
        self.assertEqual(self.search(),[])
        self.assertEqual(len(self.call('/showcases/mine',method='GET')['entries']),1)
        self.assertEqual(self.call('/showcases/mine',owner=1,method='GET')['entries'],[])
        self.call('/showcases/'+sid+'/delete')
        self.call('/showcases/post',{'catID':'v:miso','place':'osm:node:100'})
        with server.connect() as db:db.execute('UPDATE showcases SET created=?',(time.time()-8*86400,))
        self.assertEqual(self.search(),[])
    def test_delete_waits_for_revocation_and_cascades_linked_data(self):
        guest=server.dispatch('POST','/session',{})['token']
        self.call('/account/attach',{'tokens':[guest]})
        with self.assertRaises(server.APIError):self.call('/account/attach',{'tokens':[guest]},owner=1)
        self.register();self.call('/showcases/post',{'catID':'v:miso','place':'osm:node:100'})
        with patch.object(api,'revoke',side_effect=Exception('network')):
            with self.assertRaises(server.APIError):self.call('/account/delete')
        self.assertEqual(self.call('/account/me',method='GET')['id'],self.ids[0])
        with patch.object(api,'revoke') as revoke:
            result=self.call('/account/delete');revoke.assert_called_once()
        self.assertTrue(result['ok']);self.assertIn(api.token_hash(guest),result['guestTokenHashes'])
        self.assertEqual(self.search(),[])
        with self.assertRaises(server.APIError):self.call('/account/me',method='GET')
        with self.assertRaises(server.APIError):server.dispatch('GET','/catalog',{},guest)
    def test_old_session_requires_recent_signin_for_delete(self):
        with server.connect() as db:db.execute('UPDATE account_sessions SET expires=? WHERE token=?',(time.time()+86400,api.token_hash(self.tokens[0])))
        with patch.object(api,'revoke') as revoke:
            with self.assertRaises(server.APIError) as error:self.call('/account/delete')
            self.assertEqual(error.exception.status,401);revoke.assert_not_called()
    def test_place_bounds(self):
        self.assertAlmostEqual(api.place_center('18000:36000')[0],.0025)
        for value in ['-1:0','36000:0','0:72000','fake']:
            with self.assertRaises(api.Error):api.place_center(value)

if __name__=='__main__':unittest.main()
