"""Explicit acquisition-side conversion of an authorized browser export."""

import argparse
import json
from pathlib import Path
import sys

from .collector import CollectorError, convert, inspect_export


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["protocol"]:
        from .protocol import run
        return run()
    if argv and argv[0] in {"inspect", "export-script"}:
        parser = argparse.ArgumentParser(description="Local helpers for CLI and TUI users")
        sub = parser.add_subparsers(dest="command", required=True)
        inspect = sub.add_parser("inspect", help="Show safe metadata and exact media domains; never download")
        inspect.add_argument("--browser-export", required=True)
        script = sub.add_parser("export-script", help="Copy the matching browser script to a new file")
        script.add_argument("--output", required=True)
        args = parser.parse_args(argv)
        try:
            if args.command == "inspect":
                result = inspect_export(args.browser_export, require_https=True)
            else:
                from importlib.resources import files
                output = Path(args.output).expanduser().absolute()
                if not output.parent.is_dir() or output.is_symlink():
                    raise CollectorError("Invalid script destination")
                with output.open("xb") as stream:
                    stream.write(files("handouter_zhiyun_collector").joinpath("zhiyun_exporter.user.js").read_bytes())
                result = {"output_path": str(output)}
        except (CollectorError, OSError, ValueError):
            parser.exit(1, "Operation failed; existing files unchanged. Check input and output paths.\n")
        print(json.dumps(result, ensure_ascii=False))
        return 0
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
