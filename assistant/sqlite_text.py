"""Decode bounded SQLite BLOB prefixes without NUL truncation or lossy repair."""
import codecs


def encoding(db):
    name = db.execute('PRAGMA encoding').fetchone()[0]
    try:
        return {'UTF-8':'utf-8','UTF-16le':'utf-16-le','UTF-16be':'utf-16-be'}[name]
    except KeyError:
        raise ValueError('不支援的對話資料庫文字編碼。') from None


def byte_limit(characters):
    # Both UTF-8 and UTF-16 require at most four bytes per Unicode code point.
    # One extra code point lets callers distinguish exact fits from omissions.
    return 4 * (characters + 1)


def prefix(raw, total_bytes, characters, codec):
    try:
        decoder = codecs.getincrementaldecoder(codec)(errors='strict')
        decoded = decoder.decode(raw, final=total_bytes <= len(raw))
    except UnicodeError:
        raise ValueError('對話來源含無效文字編碼，未建立不完整的來源副本。') from None
    return decoded[:characters], len(decoded) > characters or total_bytes > len(raw)


def register_correction(db, predicate):
    codec = encoding(db)
    db.create_function('explicit_correction_bytes', 2,
        lambda raw, total: predicate(prefix(raw, total, 160, codec)[0]), deterministic=True)
    return codec
