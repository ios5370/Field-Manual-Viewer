import hashlib
import json
import sqlite3
import os
import tempfile
import time
import stat
from functools import lru_cache
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_DATABASES = set()
_SERVER_MUTEXES = set()
_SERVER_MUTEX_GUARD = threading.Lock()


def _import_prototype_database(path):
    """Reuse an in-place prototype library locally; never copy its publications."""
    legacy = ROOT / 'data/nomad.sqlite'
    if path.exists() or not legacy.is_file():
        return
    try:
        with FileLock('database-upgrade'), FileLock('acquisition'), FileLock('index'):
            if path.exists():
                return
            temporary = path.with_suffix('.upgrade.tmp')
            temporary.unlink(missing_ok=True)
            source = sqlite3.connect(legacy.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
            target = None
            try:
                tables = {row[0] for row in source.execute('SELECT name FROM sqlite_master')}
                if not {'publications','files','index_state'}.issubset(tables):
                    raise RuntimeError('The existing prototype catalog is not a supported library database.')
                target = sqlite3.connect(temporary)
                source.backup(target)
                target.close(); target = None
                temporary.replace(path)
            finally:
                source.close()
                if target is not None: target.close()
                temporary.unlink(missing_ok=True)
            save_json(ROOT/'data/prototype-upgrade.json',
                      {'source':'data/nomad.sqlite','destination':'data/field-manual-viewer.sqlite',
                       'original_database_preserved':True,'documents_copied':False,'network_used':False})
    except RuntimeError as error:
        raise RuntimeError('Pause any existing download or indexing job before upgrading this folder. Existing files are safe. '+str(error)) from error


def save_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    tmp=Path(name)
    try:
        with os.fdopen(descriptor,'w',encoding='utf8') as stream:
            stream.write(text)
        # Windows readers/Explorer can briefly deny replacement while reading.
        # Keep the previous complete report until an atomic swap succeeds.
        for attempt in range(100):
            try:tmp.replace(path);break
            except PermissionError:
                if attempt==99:raise
                time.sleep(.02)
    finally:
        try:tmp.unlink(missing_ok=True)
        except OSError:pass


def save_json(path, obj):
    save_text(path,json.dumps(obj,indent=2,ensure_ascii=False))


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def database():
    (ROOT / 'data').mkdir(exist_ok=True)
    path = (ROOT / 'data/field-manual-viewer.sqlite').resolve()
    with _SCHEMA_LOCK:
        _import_prototype_database(path)
    # Some large full-text index transactions legitimately outlast 30 seconds.
    # SQLite's busy handler waits for the writer instead of failing the campaign.
    db = sqlite3.connect(path, timeout=300)
    db.row_factory = sqlite3.Row
    try:
        with _SCHEMA_LOCK:
            initialized = (path in _INITIALIZED_DATABASES
                           and db.execute("SELECT 1 FROM sqlite_master WHERE name='publications'").fetchone())
            if not initialized:
                if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                    db.execute('PRAGMA journal_mode=WAL')
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='file_evidence'").fetchone(): db.executescript('''
    CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS files(pub_id TEXT, format TEXT, url TEXT, path TEXT,
      status TEXT, bytes INTEGER, sha256 TEXT, error TEXT, updated TEXT,
      PRIMARY KEY(pub_id,format));
    CREATE TABLE IF NOT EXISTS extraction(pub_id TEXT PRIMARY KEY, pages INTEGER,
      sparse_pages INTEGER, error TEXT);
    CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
      pub_id UNINDEXED, page UNINDEXED, title, designation, toc, body,
      tokenize='unicode61');
    CREATE TABLE IF NOT EXISTS index_state(pub_id TEXT, format TEXT, sha256 TEXT, engine TEXT,
      segments INTEGER, sparse INTEGER, error TEXT, PRIMARY KEY(pub_id,format));
    CREATE VIRTUAL TABLE IF NOT EXISTS epub_passages USING fts5(
      pub_id UNINDEXED, locator UNINDEXED, title, body, tokenize='unicode61');
    CREATE TABLE IF NOT EXISTS events(time TEXT, kind TEXT, pub_id TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS file_evidence(pub_id TEXT, format TEXT, sha256 TEXT,
      bytes INTEGER, mtime_ns INTEGER, PRIMARY KEY(pub_id,format));
    CREATE TABLE IF NOT EXISTS index_rows(pub_id TEXT, format TEXT, rowid INTEGER,
      PRIMARY KEY(pub_id,format,rowid));
                ''')
                if db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('pdf_pages','reader_lookup')").fetchone()[0]!=2:
                    db.executescript('BEGIN; CREATE TABLE IF NOT EXISTS pdf_pages(pub_id TEXT,page INTEGER,passage_rowid INTEGER,PRIMARY KEY(pub_id,page)) WITHOUT ROWID; CREATE TABLE IF NOT EXISTS reader_lookup(version TEXT PRIMARY KEY); COMMIT;')
                _INITIALIZED_DATABASES.add(path)
            # These tables are also added to libraries created by earlier releases.
            # The sqlite_master checks keep ordinary connections read-only and cheap.
            if (not db.execute("SELECT 1 FROM sqlite_master WHERE name='file_presence'").fetchone()
                    or not db.execute("SELECT 1 FROM sqlite_master WHERE name='reconcile_state'").fetchone()):
                db.executescript('''
    CREATE TABLE IF NOT EXISTS file_presence(pub_id TEXT, format TEXT, present INTEGER NOT NULL DEFAULT 0,
      bytes INTEGER, mtime_ns INTEGER, checked_utc TEXT, error TEXT,
      error_bytes INTEGER, error_mtime_ns INTEGER, PRIMARY KEY(pub_id,format));
    CREATE TABLE IF NOT EXISTS reconcile_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS files_path_index ON files(path);
                ''')
            presence_columns = {row[1] for row in db.execute('PRAGMA table_info(file_presence)')}
            if 'error_bytes' not in presence_columns:
                db.execute('ALTER TABLE file_presence ADD COLUMN error_bytes INTEGER')
            if 'error_mtime_ns' not in presence_columns:
                db.execute('ALTER TABLE file_presence ADD COLUMN error_mtime_ns INTEGER')
            # FTS5 cannot delete by an UNINDEXED column without scanning the whole
            # index.  Track inserted rowids so reindexing stays proportional.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='index_rows'").fetchone():
                db.executescript('''
    CREATE TABLE IF NOT EXISTS index_rows(pub_id TEXT, format TEXT, rowid INTEGER,
      PRIMARY KEY(pub_id,format,rowid));
                ''')
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='pdf_pages'").fetchone():
                    db.execute("INSERT OR IGNORE INTO index_rows SELECT pub_id,'pdf',passage_rowid FROM pdf_pages")
                    db.commit()
            # Seed the cached presence ledger from durable evidence when upgrading an
            # older library.  The next background scan still checks the real files.
            if db.execute('SELECT count(*) FROM file_presence').fetchone()[0] == 0:
                if db.execute('SELECT count(*) FROM files').fetchone()[0]:
                    db.execute('''
    INSERT OR IGNORE INTO file_presence
      (pub_id,format,present,bytes,mtime_ns,checked_utc,error,error_bytes,error_mtime_ns)
    SELECT f.pub_id,f.format,
      CASE WHEN f.status IN ('complete','skipped_verified','recovered')
                AND f.sha256 IS NOT NULL AND e.sha256=f.sha256
                AND e.bytes=f.bytes AND e.mtime_ns IS NOT NULL THEN 1 ELSE 0 END,
      f.bytes,e.mtime_ns,NULL,f.error,NULL,NULL
    FROM files f LEFT JOIN file_evidence e
      ON e.pub_id=f.pub_id AND e.format=f.format
                    ''')
                    db.commit()
    except Exception:
        db.close()
        raise
    return db


def load_json(path, default=None):
    import json
    try:
        return json.loads(Path(path).read_text(encoding='utf8'))
    except (OSError, ValueError):
        # Progress and report files are disposable.  A truncated write, permission
        # problem or hand-edited file must not take down the viewer or Setup.
        return default


class FileLock:
    """OS-held lock; automatically released even if the owning process crashes."""
    def __init__(self, name):
        self.path = ROOT / 'data' / (name + '.lock')

    def __enter__(self):
        self.path.parent.mkdir(exist_ok=True)
        import os
        if os.name=='nt' and self.path.stem=='server':
            return self._enter_server_mutex()
        self.stream = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.stream.write(b'0'); self.stream.flush()
        self.stream.seek(0)
        try:
            import os
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError('Another Field Manual Viewer ' + self.path.stem + ' process is running')
        return self

    def __exit__(self, *args):
        if getattr(self,'mutex',None):
            with _SERVER_MUTEX_GUARD:
                self.kernel.ReleaseMutex(self.mutex)
                self.kernel.CloseHandle(self.mutex)
                _SERVER_MUTEXES.discard(self.mutex_name)
                self.mutex=None
        else:self.stream.close()

    def _enter_server_mutex(self):
        """A kernel mutex keeps server exclusivity without locking a copied file."""
        import ctypes
        import os
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
        kernel.CreateMutexW.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.ReleaseMutex.argtypes=[wintypes.HANDLE]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        name='Local\\FieldManualViewer-'+hashlib.sha256(os.path.normcase(str(self.path.resolve())).encode()).hexdigest()
        with _SERVER_MUTEX_GUARD:
            if name in _SERVER_MUTEXES:
                raise RuntimeError('Another Field Manual Viewer server process is running')
            handle=kernel.CreateMutexW(None,False,name)
            if not handle:raise RuntimeError('Another viewer owns this folder or its server lock is unavailable')
            result=kernel.WaitForSingleObject(handle,0)
            if result not in (0,0x80):
                kernel.CloseHandle(handle)
                raise RuntimeError('Another Field Manual Viewer server process is running')
            try:
                # Recognize older releases' byte lock, but do not hold or create it.
                if self.path.is_file() and self.path.stat().st_size:
                    import msvcrt
                    with self.path.open('r+b') as legacy:
                        msvcrt.locking(legacy.fileno(),msvcrt.LK_NBLCK,1)
                        legacy.seek(0);msvcrt.locking(legacy.fileno(),msvcrt.LK_UNLCK,1)
            except OSError:
                kernel.ReleaseMutex(handle);kernel.CloseHandle(handle)
                raise RuntimeError('An older viewer is still running in this folder')
            _SERVER_MUTEXES.add(name)
            self.kernel=kernel;self.mutex=handle;self.mutex_name=name
        return self


@lru_cache(maxsize=32)
def archive_boundary(root):
    return (root/'archive').resolve()


def original_evidence_current(root, row, stamp):
    """Cheap shared availability check; changed originals require SHA verification."""
    try:
        path = (root / row['path']).resolve()
        if not path.is_relative_to(archive_boundary(root)): return False
        info = path.stat()
        return (stat.S_ISREG(info.st_mode) and bool(row['sha256']) and info.st_size == row['bytes']
                and stamp.get('sha256') == row['sha256'] and stamp.get('bytes') == info.st_size
                and stamp.get('mtime_ns') == info.st_mtime_ns)
    except (OSError, ValueError, TypeError):
        return False
