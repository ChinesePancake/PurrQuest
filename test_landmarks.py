import os,tempfile,unittest
from unittest.mock import patch
import server,landmarks

def element(i,lat,lon,access='yes'):
    return dict(type='node',id=i,lat=lat,lon=lon,tags={'name':f'Park {i}','leisure':'park','access':access})

class LandmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();server.DB=os.path.join(self.tmp.name,'test.sqlite');server.init();landmarks.ATTEMPTS.clear()
    def tearDown(self):self.tmp.cleanup()
    def search(self):return server.dispatch('POST','/landmarks/search',{'latitude':.025,'longitude':.025})
    def test_spacing_stable_ids_cached_shared_results(self):
        nodes=[element(1,.01,.01),element(2,.0101,.01),element(3,.02,.02),element(4,.04,.04)]
        with patch.object(landmarks,'fetch',return_value=nodes) as fetch:
            first=self.search()['landmarks'];second=self.search()['landmarks']
            self.assertEqual(first,second);fetch.assert_called_once();self.assertEqual(len(first),3)
            for i,a in enumerate(first):
                self.assertTrue(a['id'].startswith('osm:node:'))
                for b in first[i+1:]:self.assertGreaterEqual(landmarks.distance((a['latitude'],a['longitude']),(b['latitude'],b['longitude'])),600)
    def test_access_and_unsupported_locations_excluded(self):
        nodes=[element(1,.01,.01,'private'),element(2,.02,.02,'customers'),element(3,.03,.03,'')]
        home=element(4,.04,.04);home['tags']={'name':'Home','access':'yes','building':'house'};nodes.append(home)
        self.assertEqual(landmarks.candidates(nodes),[])
    def test_failure_has_no_synthetic_fallback_and_retry_is_throttled(self):
        with patch.object(landmarks,'fetch',side_effect=OSError()) as fetch:
            result=self.search();self.assertEqual(result['landmarks'],[]);self.assertIn('unavailable',result['message'])
            self.search();fetch.assert_called_once()
    def test_removed_site_retires_after_refresh(self):
        with patch.object(landmarks,'fetch',return_value=[element(1,.01,.01)]):self.assertEqual(len(self.search()['landmarks']),1)
        with server.connect() as db:db.execute('DELETE FROM landmark_regions')
        landmarks.ATTEMPTS.clear()
        with patch.object(landmarks,'fetch',return_value=[]):self.assertEqual(self.search()['landmarks'],[])
    def test_invalid_areas_rejected_without_provider_call(self):
        with patch.object(landmarks,'fetch') as fetch:
            for value in [float('nan'),float('inf'),91]:
                with self.assertRaises(server.APIError):server.dispatch('POST','/landmarks/search',{'latitude':value,'longitude':0})
            fetch.assert_not_called()
    def test_showcase_requires_active_registry_entry(self):
        with server.connect() as db:
            db.execute("INSERT INTO public_landmarks VALUES ('osm:node:1','Library',1,1,'showcase',1)")
            db.execute("INSERT INTO public_landmarks VALUES ('osm:node:2','Park',2,2,'supply',1)")
            self.assertTrue(landmarks.allowed_showcase(db,'osm:node:1'))
            self.assertFalse(landmarks.allowed_showcase(db,'osm:node:2'))
            self.assertFalse(landmarks.allowed_showcase(db,'100:100'))
            db.execute('UPDATE public_landmarks SET active=0')
            self.assertFalse(landmarks.allowed_showcase(db,'osm:node:1'))

if __name__=='__main__':unittest.main()
