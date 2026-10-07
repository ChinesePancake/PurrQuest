import json,os,tempfile,time,unittest
from unittest.mock import patch
import server,account_api,friends_api

class FriendsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();server.DB=os.path.join(self.tmp.name,'test.sqlite');server.init()
        self.ids=['alice','bob','carol'];self.tokens=['token-'+x for x in self.ids]
        with server.connect() as db:
            for who,token in zip(self.ids,self.tokens):
                db.execute('INSERT INTO accounts(id,provider,subject,grant_token) VALUES (?,\'google\',?,\'test\')',(who,who))
                db.execute('INSERT INTO account_sessions VALUES (?,?,?)',(account_api.token_hash(token),who,time.time()+30*86400))
                friends_api.code(db,who)
        self.codes=[self.call('/friends/list',owner=i,method='GET')['code'] for i in range(3)]
    def tearDown(self):self.tmp.cleanup()
    def call(self,path,body=None,owner=0,method='POST'):return server.dispatch(method,path,body or {},self.tokens[owner])
    def request(self):return self.call('/friends/request',{'code':self.codes[1]})
    def test_auth_and_stable_private_codes(self):
        with self.assertRaises(server.APIError):server.dispatch('GET','/friends/list',{},'bad')
        self.assertEqual(self.codes[0],self.call('/friends/list',method='GET')['code'])
        self.assertEqual(len(set(self.codes)),3);self.assertEqual(len(self.codes[0]),12)
    def test_pending_accept_recipient_only_and_private_fields(self):
        result=self.request();self.assertEqual(len(result['outgoing']),1);self.assertEqual(result['friends'],[])
        for owner in [0,2]:
            with self.assertRaises(server.APIError):self.call('/friends/accept',{'id':'bob' if owner==0 else 'alice'},owner=owner)
        accepted=self.call('/friends/accept',{'id':'alice'},owner=1)
        self.assertEqual(accepted['friends'][0]['id'],'alice')
        self.assertEqual(set(accepted['friends'][0]),{'id','label','avatar'})
        self.assertEqual(len(self.call('/friends/list',method='GET')['friends']),1)
    def test_reciprocal_and_duplicate_requests_do_not_auto_accept(self):
        self.request();self.request();r=self.call('/friends/request',{'code':self.codes[0]},owner=1)
        self.assertEqual(len(r['incoming']),1);self.assertEqual(r['friends'],[])
    def test_cancel_decline_and_remove(self):
        self.request();self.assertEqual(self.call('/friends/cancel',{'id':'bob'})['outgoing'],[])
        self.request();self.assertEqual(self.call('/friends/decline',{'id':'alice'},owner=1)['incoming'],[])
        self.request();self.call('/friends/accept',{'id':'alice'},owner=1)
        self.assertEqual(self.call('/friends/remove',{'id':'bob'})['friends'],[])
        self.assertEqual(self.call('/friends/list',owner=1,method='GET')['friends'],[])
    def test_block_prevents_requests_in_both_directions_and_unblock_does_not_restore(self):
        self.request();result=self.call('/friends/block',{'id':'alice'},owner=1)
        self.assertEqual(len(result['blocked']),1)
        for owner,code in [(0,self.codes[1]),(1,self.codes[0])]:
            with self.assertRaises(server.APIError):self.call('/friends/request',{'code':code},owner=owner)
        result=self.call('/friends/unblock',{'id':'alice'},owner=1)
        self.assertEqual(result['friends'],[]);self.assertEqual(result['incoming'],[])
        self.assertEqual(len(self.request()['outgoing']),1)
    def test_invalid_code_guessing_is_rate_limited(self):
        for _ in range(30):
            with self.assertRaises(server.APIError):self.call('/friends/request',{'code':'000000000000'})
        with self.assertRaises(server.APIError) as e:self.request()
        self.assertEqual(e.exception.status,429)
    def test_delete_cascades_relationships_codes_blocks(self):
        self.request();self.call('/friends/accept',{'id':'alice'},owner=1)
        with patch.object(account_api,'revoke'):self.call('/account/delete')
        self.assertEqual(self.call('/friends/list',owner=1,method='GET')['friends'],[])
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM friend_codes WHERE account='alice'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM friend_attempts WHERE account='alice'").fetchone()[0],0)
    def test_self_requests_are_rejected(self):
        with self.assertRaises(server.APIError):self.call('/friends/request',{'code':self.codes[0]})

if __name__=='__main__':unittest.main()
