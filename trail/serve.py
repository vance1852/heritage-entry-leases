"""额度服务进程入口：python -m trail.serve [--port 8091]。"""

import argparse
import json
from pathlib import Path

from .httpapi import run
from .service import QuotaService


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=Path,
                        help="合作方资料 JSON（取其中 facts 初始化窗口与闸机）")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    if args.facts:
        data = json.loads(args.facts.read_text(encoding="utf-8"))
        facts = data["facts"] if "facts" in data else data
        service = QuotaService(facts["windows"], facts["gates"])
    else:
        service = QuotaService([], [])
    run(service, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
