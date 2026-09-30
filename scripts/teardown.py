"""Delete this lab's cloud resources, and say what went.

    make teardown            # lab 3 — the endpoint and its replicas
    make teardown LAB=2

Separate from the adapter because deleting things deserves a driver that prints what it
found before it removes it, and exits non-zero if the provider reports nothing deleted
while resources are still listed. Teardown scripts fail silently more often than anyone
would like, and the bill is what eventually tells you.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter  # noqa: E402
from src import config  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--lab", type=int, default=3)
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = p.parse_args()

    cfg = config.load()
    tags = cfg.tags(args.lab)
    print(f"tags     {tags}")
    if not args.yes and sys.stdin.isatty():
        if input("delete every resource carrying these tags? [y/N] ").strip().lower() != "y":
            print("nothing deleted")
            return 1

    deleted = get_adapter(cfg).teardown(tags)
    if not deleted:
        print("nothing matched — either it is already gone, or the tags do not match. "
              "Check the console before believing this.")
        return 0
    for item in deleted:
        print(f"deleted  {item}")
    print(f"\n{len(deleted)} resources removed. Verify in the console: deletion is "
          "asynchronous, and a successful call is not the same as a deleted resource.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
