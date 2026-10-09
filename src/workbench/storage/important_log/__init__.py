"""Private, bounded SQLite storage for important log entries."""

from .store import ImportantLogStore, ImportantLogStoreError, decode_body, encode_body

__all__ = ["ImportantLogStore", "ImportantLogStoreError", "decode_body", "encode_body"]
