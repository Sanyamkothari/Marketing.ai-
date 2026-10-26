"""Write the fingerprint of the scale scenario to a JSON file, to compare two versions of the code.

    python -m tests.v3.scale.dump_fingerprint OUT.json [--customers 300] [--batch N|none] [--mandate none|paced|violation]

Without `--batch` the config is built without `sweep_batch`, so this also runs on a checkout from
before the setting existed (commit dd878462): copy `tests/v3/scale/` into that checkout, run this
there and here, and `cmp` the two files. `docs/v3/SCALE.md` says what was compared.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.v3.scale import scenario as sc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--customers", type=int, default=300)
    parser.add_argument("--batch", default=None)
    parser.add_argument("--mandate", default="none")
    parser.add_argument("--no-one-contact", action="store_true")
    parser.add_argument("--groups", action="store_true")
    args = parser.parse_args()
    batch = sc.UNSET if args.batch is None else (None if args.batch == "none" else int(args.batch))
    s = sc.build(
        args.customers,
        sweep_batch=batch,
        one_contact=not args.no_one_contact,
        mandate_kind=args.mandate,
        groups=args.groups,
    )
    Path(args.out).write_text(json.dumps(sc.fingerprint(s, sc.play(s)), sort_keys=True))


if __name__ == "__main__":
    main()
