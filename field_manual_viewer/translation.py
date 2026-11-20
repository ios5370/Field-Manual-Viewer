"""Optional local, offline Spanish-to-English translation.

The model is a CTranslate2 OPUS-MT package.  It runs on CPU with float32
weights; int8 was not reliable for this model.  Nothing here uses the network.
"""
import hashlib
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .core import ROOT, database
from .apply_bpe import BPE

MODEL_DIR = ROOT / 'translation/es_en'
MODEL_FILE = MODEL_DIR / 'model/model.bin'
BPE_FILE = MODEL_DIR / 'bpe.model'
ENGINE = 'opus-mt-es-en-ct2-float32-v1'
TARGET = 'en'

_LOCK = threading.RLock()
_TRANSLATOR = None
_BPE = None
_NORMALIZER = None
_TOKENIZER = None
_DETOKENIZER = None


class TranslationUnavailable(RuntimeError):
    pass


def available():
    return all(path.is_file() for path in (MODEL_FILE, BPE_FILE,
                                           MODEL_DIR / 'model/config.json',
                                           MODEL_DIR / 'model/shared_vocabulary.json'))


def _load():
    global _TRANSLATOR, _BPE, _NORMALIZER, _TOKENIZER, _DETOKENIZER
    with _LOCK:
        if _TRANSLATOR is not None:
            return
        if not available():
            raise TranslationUnavailable('Offline Spanish translation pack is not installed.')
        try:
            import ctranslate2
            from sacremoses.normalize import MosesPunctNormalizer
            from sacremoses.tokenize import MosesDetokenizer, MosesTokenizer
        except ImportError as error:
            raise TranslationUnavailable('Offline translation runtime is not installed.') from error
        _TRANSLATOR = ctranslate2.Translator(str(MODEL_DIR / 'model'), device='cpu',
                                             compute_type='float32',
                                             inter_threads=1, intra_threads=4)
        with BPE_FILE.open(encoding='utf8') as stream:
            _BPE = BPE(stream)
        _NORMALIZER = MosesPunctNormalizer('es')
        _TOKENIZER = MosesTokenizer('es')
        _DETOKENIZER = MosesDetokenizer('en')


def _case_normalize(text):
    letters = [ch for ch in text if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.72:
        text = text.lower()
        text = re.sub(r'(^|[.!?]\s+)([a-z])',
                      lambda m: m.group(1) + m.group(2).upper(), text)
    return text


def _segments(text):
    text = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    text = _case_normalize(text)
    for paragraph in re.split(r'\n\s*\n|\n', text):
        paragraph = re.sub(r'\s+', ' ', paragraph).strip()
        if not paragraph:
            continue
        for value in re.split(r'(?<=[.!?])\s+', paragraph):
            value = value.strip()
            while len(value) > 420:
                cut = value.rfind(' ', 0, 420)
                cut = 420 if cut < 200 else cut
                yield value[:cut].strip()
                value = value[cut:].strip()
            if value:
                yield value


def _translate_text(text):
    _load()
    paragraphs = []
    for paragraph in re.split(r'\n\s*\n', text.replace('\r\n', '\n')):
        if not paragraph.strip():
            continue
        pieces = []
        for sentence in _segments(paragraph):
            normalized = _NORMALIZER.normalize(sentence)
            tokenized = ' '.join(_TOKENIZER.tokenize(normalized))
            tokens = _BPE.segment_tokens(tokenized.strip('\r\n ').split(' '))
            result = _TRANSLATOR.translate_batch(
                [tokens], beam_size=2, max_decoding_length=512)[0]
            translated = _DETOKENIZER.detokenize(
                ' '.join(result.hypotheses[0]).replace('@@ ', '').split(' '))
            pieces.append(translated)
        if pieces:
            paragraphs.append(' '.join(pieces))
    return '\n\n'.join(paragraphs)


def translate(text, target=TARGET):
    source = str(text or '').strip()
    if not source:
        raise TranslationUnavailable('There is no text on this page/section to translate.')
    if target != 'en':
        raise TranslationUnavailable('Only the English target pack is installed.')
    source_sha = hashlib.sha256(source.encode('utf8')).hexdigest()
    with database() as db:
        row = db.execute(
            'SELECT translation FROM translations WHERE source_sha=? AND target=? AND engine=?',
            (source_sha, target, ENGINE)).fetchone()
        if row:
            return {'translation': row[0], 'cached': True, 'target': target, 'engine': ENGINE}
    value = _translate_text(source)
    with database() as db:
        db.execute('INSERT OR REPLACE INTO translations VALUES (?,?,?,?,?)',
                   (source_sha, target, ENGINE, value,
                    datetime.now(timezone.utc).isoformat()))
    return {'translation': value, 'cached': False, 'target': target, 'engine': ENGINE}
