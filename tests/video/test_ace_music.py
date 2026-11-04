import copy
import json
import unittest
from pathlib import Path

from ai_service.services.video.ace_music import (
    ACE_MODEL_REVISION, JOYFUL_TRAVEL_CAPTION, ace_music_asset_from_history,
    ace_music_downloads, build_ace_music_workflow, validate_ace_music_workflow,
)
from ai_service.services.video.errors import VideoError


class AceMusicTests(unittest.TestCase):
    def test_one_instrumental_composition_has_matching_lm_and_latent_duration(self):
        graph=build_ace_music_workflow(duration=30,bpm=120,seed=23)
        self.assertEqual(graph['4']['inputs']['duration'],30)
        self.assertEqual(graph['7']['inputs']['seconds'],30)
        self.assertEqual(graph['4']['inputs']['lyrics'],'[Instrumental]')
        self.assertEqual(graph['4']['inputs']['language'],'unknown')
        self.assertTrue(graph['4']['inputs']['generate_audio_codes'])
        self.assertEqual(graph['4']['inputs']['seed'],graph['8']['inputs']['seed'])
        self.assertEqual(sum(n['class_type']=='KSampler' for n in graph.values()),1)
        self.assertEqual(graph['6']['inputs']['shift'],3.0)
        self.assertEqual((graph['8']['inputs']['steps'],graph['8']['inputs']['cfg']),(8,1.0))
        self.assertEqual(graph['10']['class_type'],'SaveAudio')
        self.assertLessEqual(len(JOYFUL_TRAVEL_CAPTION),512)

    def test_downloads_are_pinned_and_profiles_do_not_mix_encoder_sizes(self):
        normal=ace_music_downloads();xl=ace_music_downloads('xl_turbo')
        self.assertEqual(len(normal),4)
        for entry in normal+xl:
            self.assertIn('/'+ACE_MODEL_REVISION+'/',entry['url'])
            self.assertNotIn('/main/',entry['url'])
            self.assertEqual(len(entry['sha256']),64)
            self.assertGreater(entry['size_bytes'],300_000_000)
        self.assertIn('1.7b',normal[2]['filename'])
        self.assertIn('4b',xl[2]['filename'])
        self.assertGreater(xl[0]['size_bytes'],normal[0]['size_bytes'])

    def test_history_waits_for_completion_and_accepts_only_safe_lossless_audio(self):
        self.assertIsNone(ace_music_asset_from_history({},'p'))
        self.assertIsNone(ace_music_asset_from_history({'p':{'status':{'completed':False}}},'p'))
        good={'p':{'status':{'completed':True},'outputs':{'10':{'audio':[
            {'filename':'score.flac','subfolder':'audigo/music','type':'output'}]}}}}
        self.assertEqual(ace_music_asset_from_history(good,'p')['filename'],'score.flac')
        for filename in ('../private.flac','score.mp3','score.mp4'):
            changed=copy.deepcopy(good);changed['p']['outputs']['10']['audio'][0]['filename']=filename
            with self.assertRaises(VideoError):ace_music_asset_from_history(changed,'p')
        with self.assertRaises(VideoError):ace_music_asset_from_history({'p':{'status':{'status_str':'error'}}},'p')

    def test_invalid_duration_seed_and_prefix_fail_without_submission(self):
        for fields in ({'duration':0},{'duration':601},{'seed':True},{'prefix':'../escape'},{'caption':''}):
            with self.assertRaises(VideoError):build_ace_music_workflow(**fields)

    @unittest.skipUnless(Path('artifacts/video/revisit30-live/object-info.json').is_file(),'Live schema snapshot not included in source tree')
    def test_graph_contract_against_actual_comfy_schema_and_missing_model_preflight(self):
        info=json.loads(Path('artifacts/video/revisit30-live/object-info.json').read_text())
        graph=build_ace_music_workflow()
        with self.assertRaises(VideoError):validate_ace_music_workflow(graph,info)
        files=ace_music_downloads()
        info['UNETLoader']['input']['required']['unet_name'][0].append(files[0]['filename'])
        for field,entry in [('clip_name1',files[1]),('clip_name2',files[2])]:
            info['DualCLIPLoader']['input']['required'][field][0].append(entry['filename'])
        info['VAELoader']['input']['required']['vae_name'][0].append(files[3]['filename'])
        validate_ace_music_workflow(graph,info)
