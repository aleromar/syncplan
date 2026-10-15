"""Compare a source with its copy on the backup (plan-copies R2, R3, R6).

The two folders are walked together, folder by folder, and every entry is sorted into an
action, a decision, a skip or "unchanged". Same-size pairs are then read and compared.
"""


def key(path: str) -> str:
    """An entry's key: its path inside the source, lower-cased as Windows compares (NFR1)."""
    return path.lower()
