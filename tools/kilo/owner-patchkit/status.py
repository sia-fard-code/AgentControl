"""Report live runtime coverage. Installation alone is never activation."""
import argparse
import json
from pathlib import Path
import runpy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--owner-id")
    parser.add_argument("--directory", type=Path, help="override the metadata journal directory")
    args = parser.parse_args()
    module = runpy.run_path(str(Path(__file__).resolve().parents[3] / "engine/harness_diagnostics.py"))
    result = module["read_coverage"](args.directory or module["diagnostic_directory"](),
                                      owner_pid=args.owner_pid, owner_id=args.owner_id)
    print(json.dumps({"owner_pid": args.owner_pid, **result}, sort_keys=True))
    if not result["pre_kill"]:
        print("Owner pre-kill attribution is not verified active; plugin observations are not a substitute.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
