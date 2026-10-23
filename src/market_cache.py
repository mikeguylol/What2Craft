"""Durable item/DC observations, independent of report publication."""
import json
import sqlite3
import sys
import time
from market_validation import valid_sample
from contextlib import closing


class CacheReadError(RuntimeError):
    pass


class CacheWriteError(RuntimeError):
    pass


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute('''CREATE TABLE IF NOT EXISTS samples (
        dc TEXT NOT NULL, item_id TEXT NOT NULL, fetched_at REAL NOT NULL,
        listing_entries INTEGER NOT NULL, history_entries INTEGER NOT NULL,
        sample TEXT NOT NULL, PRIMARY KEY (dc, item_id))''')
    return connection


def save_samples(path, dc, requested_ids, samples, fetched_at, listings, history):
    try:
        with closing(connect(path)) as connection, connection:
            for item in requested_ids:
                key = str(item)
                sample = dict(samples.get(key, {'listings': [], 'history': [], 'source_updates': {}, 'unresolved': True}),
                              fetched_at=fetched_at)
                connection.execute('''INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dc, item_id) DO UPDATE SET fetched_at=excluded.fetched_at,
                    listing_entries=excluded.listing_entries, history_entries=excluded.history_entries,
                    sample=excluded.sample WHERE excluded.fetched_at >= samples.fetched_at
                    OR samples.fetched_at > ? OR samples.fetched_at <= 0''',
                                   (dc, key, fetched_at, listings, history, json.dumps(sample), time.time() + 300))
    except (sqlite3.Error, OSError) as exc:
        raise CacheWriteError(f'Cannot persist fetched market data to {path}: {exc}') from exc


def load_samples(path, item_ids, data_centers, listings, history):
    if not path.exists():
        return {}
    wanted, dcs = set(map(str, item_ids)), set(data_centers)
    result = {}
    invalid = 0
    try:
        # A read must not create tables or modify a damaged database.
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as connection:
            for dc, item, timestamp, sample in connection.execute(
                    'SELECT dc, item_id, fetched_at, sample FROM samples WHERE listing_entries=? AND history_entries=?',
                    (listings, history)):
                if item not in wanted or dc not in dcs:
                    continue
                try:
                    value = json.loads(sample)
                    if not valid_sample(value, timestamp) or timestamp > time.time() + 300:
                        raise ValueError('Invalid cached observation')
                except (ValueError, TypeError, UnicodeError):
                    invalid += 1
                    continue
                value['fetched_at'] = timestamp
                result.setdefault(item, {})[dc] = value
    except (sqlite3.Error, OSError) as exc:
        raise CacheReadError(f'Cannot read market cache {path}; existing reports were preserved. '
                             f'The cache was not deleted or replaced: {exc}') from exc
    if invalid:
        print(f'Ignoring {invalid} invalid cached item/DC observations; they will be requested again.', file=sys.stderr)
    return result
