"""Tests for the actual installer and deterministic classifier; no robot I/O."""
from contextlib import contextmanager
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from install_g1_sentry import install, new_skill
from sentry_rules import SentryRules

@contextmanager
def db_connect(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'data.db'
        with db_connect(self.db) as conn:
            conn.execute('CREATE TABLE config(key TEXT PRIMARY KEY, value TEXT)')

    def seed(self, value):
        with db_connect(self.db) as conn:
            conn.execute("INSERT INTO config VALUES('skills',?)", (json.dumps(value),))

    def read(self):
        with db_connect(self.db) as conn:
            return json.loads(conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()[0])

    def test_missing_key_is_created_disabled(self):
        result = install(self.db)
        self.assertFalse(result['active'])
        self.assertEqual(self.read()['installed'][0]['version'], '1.1.1')
        self.assertTrue(Path(result['backup']).is_file())

    def test_reinstall_preserves_other_data_and_disabled_state(self):
        other = {'slug': 'other', 'active': True, 'instruction': 'unchanged'}
        self.seed({'installed': [other, {'slug':'g1-sentry', 'active':False, 'installedAt':'old', 'custom':42}], 'extra':1})
        install(self.db, backup=False)
        install(self.db, backup=False)
        cfg = self.read()
        self.assertEqual(cfg['installed'][0], other)
        self.assertEqual(cfg['extra'], 1)
        sentry = cfg['installed'][1]
        self.assertFalse(sentry['active'])
        self.assertEqual(sentry['custom'], 42)
        self.assertEqual(sentry['installedAt'], 'old')
        self.assertEqual(len(cfg['installed']), 2)

    def test_backup_contains_original(self):
        original = {'installed': [{'slug':'g1-sentry','active':True,'instruction':'old'}]}
        self.seed(original)
        result = install(self.db)
        with db_connect(result['backup']) as conn:
            value = json.loads(conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()[0])
        self.assertEqual(value, original)
        self.assertTrue(self.read()['installed'][0]['active'])

    def test_invalid_config_is_not_overwritten(self):
        for cfg in ({'installed':{}}, {'installed':[None]}, [], {'installed':[{}]}):
            with self.subTest(cfg=cfg):
                with db_connect(self.db) as conn:
                    conn.execute("INSERT OR REPLACE INTO config VALUES('skills',?)", (json.dumps(cfg),))
                with self.assertRaises(ValueError):
                    install(self.db, backup=False)
                self.assertEqual(self.read(), cfg)

    def test_missing_database_is_not_created(self):
        missing = self.db.with_name('missing.db')
        with self.assertRaises(FileNotFoundError):
            install(missing)
        self.assertFalse(missing.exists())

    def test_syntax_error_rolls_back(self):
        with db_connect(self.db) as conn:
            conn.execute("INSERT INTO config VALUES('skills','not-json')")
        with self.assertRaises(ValueError):
            install(self.db, backup=False)
        with db_connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()[0], 'not-json')

    def test_required_tools_exclude_motion(self):
        self.assertEqual(set(new_skill['requiredTools']), {'led','tts','camera_distance'})

class RulesTests(unittest.TestCase):
    def setUp(self):
        self.rules = SentryRules()

    def feed(self, d, t):
        return self.rules.feed({'distance_m':d}, received_at=t, now=t)

    def series(self, d, start):
        return [self.feed(d,start),self.feed(d,start+.1),self.feed(d,start+.2)][-1]

    def test_near_requires_three_samples_and_dwell(self):
        self.assertIsNone(self.feed(.7, 0))
        self.assertIsNone(self.feed(.7, .01))
        self.assertIsNone(self.feed(.7, .02))
        self.assertEqual(self.feed(.7,.2).kind, 'near')

    def test_steady_near_warns_once(self):
        self.assertEqual(self.series(.7,0).kind, 'near')
        for i in range(1,40):
            self.assertIsNone(self.feed(.7, i))

    def test_zero_is_not_clear_and_does_not_rearm(self):
        self.series(.7,0)
        self.assertEqual(self.feed(0,1).kind, 'unavailable')
        self.assertIsNone(self.series(.7,6))
        self.assertTrue(self.rules.latched)

    def test_valid_clear_rearms_after_cooldown(self):
        self.series(.7,0)
        self.assertEqual(self.series(1.2,1).kind,'clear')
        self.assertIsNone(self.series(.7,2))
        self.assertEqual(self.series(.7,6).kind,'near')

    def test_suspected_occlusion_priority(self):
        self.assertEqual(self.series(.1,0).kind,'occlusion_suspected')

    def test_invalid_numbers(self):
        for d in [None,0,-1,True,'0.7',float('nan'),float('inf')]:
            with self.subTest(d=d):
                r=SentryRules().feed({'distance_m':d},received_at=0,now=0)
                self.assertEqual(r.kind,'unavailable')

    def test_missing_real_field(self):
        r=self.rules.feed({'distance':.7},received_at=0,now=0)
        self.assertEqual(r.kind,'unavailable')

    def test_cached_sample_never_counts_as_three(self):
        for _ in range(10):
            self.assertIsNone(self.rules.feed({'distance_m':.7},received_at=1,now=1.1))

    def test_stale_future_and_invalid_timestamps(self):
        for received, now in [(0,3),(3,0),(float('nan'),0),(True,1)]:
            with self.subTest(received=received):
                self.assertEqual(self.rules.feed({'distance_m':.7},received_at=received,now=now).kind,'unavailable')

    def test_silence_clears_confirmation_not_latch(self):
        self.series(.7,0)
        self.assertEqual(self.rules.tick(4).kind,'unavailable')
        self.assertIsNone(self.series(.7,5))

    def test_boundary_thresholds(self):
        self.assertEqual(self.series(.15,0).kind,'near')
        self.assertIsNone(self.series(1.0,1))
        self.assertTrue(self.rules.latched)
        self.assertEqual(self.series(1.001,2).kind,'clear')
        self.assertIsNone(self.series(.8,8))

    def test_invalid_interrupts_confirmation(self):
        self.feed(.7,0)
        self.feed(0,.1)
        self.assertIsNone(self.feed(.7,.2))
        self.assertIsNone(self.feed(.7,.3))
        self.assertEqual(self.feed(.7,.4).kind,'near')

    def test_long_gap_interrupts_confirmation(self):
        self.feed(.7,0)
        self.assertIsNone(self.feed(.7,5))
        self.assertIsNone(self.feed(.7,5.1))
        self.assertEqual(self.feed(.7,5.2).kind,'near')

if __name__ == '__main__':
    unittest.main()
