import os, tempfile, unittest
import server
class MultiplayerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        server.DB=os.path.join(self.tmp.name,'test.sqlite')
        server.init()
        self.a=server.dispatch('POST','/session',{})['token']
        self.b=server.dispatch('POST','/session',{})['token']
        self.room=server.dispatch('POST','/rooms',{},self.a)['code']
    def tearDown(self): self.tmp.cleanup()
    def test_two_players_complete_challenge(self):
        server.dispatch('POST',f'/rooms/{self.room}/join',{},self.b)
        for token in [self.a,self.b]:
            for cat in ['miso','mochi','luna','fern','cloud','nova']:
                result=server.dispatch('POST',f'/rooms/{self.room}/catches',{'cat':cat},token)
        self.assertEqual((result['count'],result['members']),(12,2))
        server.init() # persisted after initialization
        self.assertEqual(server.dispatch('GET',f'/rooms/{self.room}',{},self.a)['count'],12)
    def test_retries_are_idempotent(self):
        for _ in range(3): result=server.dispatch('POST',f'/rooms/{self.room}/catches',{'cat':'miso'},self.a)
        self.assertEqual(result['count'],1)
    def test_private_room(self):
        with self.assertRaises(server.APIError) as e: server.dispatch('GET',f'/rooms/{self.room}',{},self.b)
        self.assertEqual(e.exception.status,403)
    def test_invalid_cat_and_token(self):
        for token, cat in [(self.a,'fake'),('invalid','miso')]:
            with self.assertRaises(server.APIError): server.dispatch('POST',f'/rooms/{self.room}/catches',{'cat':cat},token)
    def test_delete_removes_player_contributions(self):
        server.dispatch('POST',f'/rooms/{self.room}/join',{},self.b)
        server.dispatch('POST',f'/rooms/{self.room}/catches',{'cat':'miso'},self.a)
        server.dispatch('DELETE','/session',{},self.a)
        result=server.dispatch('GET',f'/rooms/{self.room}',{},self.b)
        self.assertEqual((result['count'],result['members']),(0,1))
        with self.assertRaises(server.APIError): server.dispatch('GET',f'/rooms/{self.room}',{},self.a)
    def test_catalog_and_new_breeds(self):
        catalog=server.dispatch('GET','/catalog',{},self.a)
        self.assertEqual(set(catalog['cats']),server.CATS)
        self.assertEqual(len(catalog['cats']),89)
        for cat in catalog['cats']:
            result=server.dispatch('POST',f'/rooms/{self.room}/catches',{'cat':cat},self.a)
        self.assertEqual(result['count'],89)
        self.assertEqual(set(result['mine']),server.CATS)
    def test_catalog_requires_authentication(self):
        with self.assertRaises(server.APIError) as e:
            server.dispatch('GET','/catalog',{},'wrong')
        self.assertEqual(e.exception.status,401)
if __name__=='__main__': unittest.main()
