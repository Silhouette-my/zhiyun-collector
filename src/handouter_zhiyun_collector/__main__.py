"""Explicit acquisition-side conversion of an authorized browser export."""

import argparse
import json
import sys

from .collector import CollectorError, convert


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["protocol"]:
        from .protocol import run
        return run()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-export", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--allowed-media-host", required=True,
                        help="Host shown in the browser export's authorized video link")
    args = parser.parse_args(argv)
    try:
        result = convert(args.browser_export, args.output, allowed_media_host=args.allowed_media_host)
    except (CollectorError, OSError, ValueError):
        parser.exit(1, "Collection failed; input unchanged. Check export, access and media host.\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
