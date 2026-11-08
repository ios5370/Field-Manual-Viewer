import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from field_manual_viewer import core


class PrototypeUpgradeTests(unittest.TestCase):
    def setUp(self):
        (core.ROOT/'tmp').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=core.ROOT/'tmp');self.root=Path(self.temp.name)
        self.patcher=patch.object(core,'ROOT',self.root);self.patcher.start()
        with closing(core.database()) as db,db:
            db.execute('DROP TABLE file_evidence')
            db.execute('INSERT INTO publications VALUES (?,?)',('saved',json.dumps({'id':'saved','title':'Existing publication'})))
            db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)',('saved','pdf','https://source.invalid','archive/saved/saved.pdf','complete',123,'original-sha',None,'original-date'))
            db.execute('INSERT INTO index_state VALUES (?,?,?,?,?,?,?)',('saved','pdf','original-sha','existing-engine',1,0,None))
            db.execute('INSERT INTO passages VALUES (?,?,?,?,?,?)',('saved',1,'Existing publication','FM 1','','Saved searchable text'))
        self.current=self.root/'data/field-manual-viewer.sqlite'
        self.legacy=self.root/'data/nomad.sqlite'
        self.current.rename(self.legacy)
        core._INITIALIZED_DATABASES.discard(self.current.resolve())
        self.before=hashlib.sha256(self.legacy.read_bytes()).hexdigest()

    def tearDown(self):self.patcher.stop();self.temp.cleanup()

    def test_upgrade_reuses_paths_checksums_and_index_without_network(self):
        with patch('socket.socket.connect',side_effect=AssertionError('Unexpected network')):
            with closing(core.database()) as db:
                self.assertEqual(tuple(db.execute('SELECT path,sha256,updated FROM files').fetchone()),('archive/saved/saved.pdf','original-sha','original-date'))
                self.assertEqual(db.execute('SELECT body FROM passages').fetchone()[0],'Saved searchable text')
                self.assertEqual(db.execute('SELECT sha256 FROM index_state').fetchone()[0],'original-sha')
                self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='file_evidence'").fetchone())
        self.assertEqual(hashlib.sha256(self.legacy.read_bytes()).hexdigest(),self.before)
        self.assertTrue(json.loads((self.root/'data/prototype-upgrade.json').read_text())['original_database_preserved'])

    def test_active_acquisition_blocks_upgrade_without_changing_legacy(self):
        with core.FileLock('acquisition'):
            with self.assertRaisesRegex(RuntimeError,'Pause any existing'):
                core.database()
        self.assertFalse(self.current.exists())
        self.assertEqual(hashlib.sha256(self.legacy.read_bytes()).hexdigest(),self.before)

    def test_existing_new_database_is_never_overwritten_by_legacy(self):
        with closing(core.database()) as db,db:
            db.execute('UPDATE publications SET metadata=?',(json.dumps({'id':'saved','title':'Updated local title'}),))
        with closing(core.database()) as db:
            self.assertEqual(json.loads(db.execute('SELECT metadata FROM publications').fetchone()[0])['title'],'Updated local title')


if __name__=='__main__':unittest.main()
