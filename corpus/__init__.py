"""Ground-truth corpus: content-addressed originals + extracted text in R2.

See docs/CORPUS_ARCHITECTURE.md. Extraction writes once; everything
downstream reads.
"""

from corpus.store import (
    EXTRACT_VERSION,
    CorpusStore,
    close_corpus,
    get_corpus,
    init_corpus,
    sha256_hex,
)
from corpus.r2 import R2Client, R2Error

__all__ = [
    "EXTRACT_VERSION",
    "CorpusStore",
    "R2Client",
    "R2Error",
    "close_corpus",
    "get_corpus",
    "init_corpus",
    "sha256_hex",
]
