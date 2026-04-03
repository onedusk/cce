"""Emit MDX from all completed pipeline runs.

Reads output/run_*/result.json, deserializes packages,
and writes MDX to output/mdx/.
"""

import json
import sys
from pathlib import Path

from cce.models.package import PublishPackage
from cce.output.mdx import emit_mdx


def main() -> None:
    output_dir = Path("output")
    target_dir = output_dir / "mdx"
    target_dir.mkdir(exist_ok=True)

    runs = sorted(output_dir.glob("run_*/result.json"))
    if not runs:
        print("No completed runs found in output/")
        sys.exit(1)

    emitted = 0
    for result_path in runs:
        data = json.loads(result_path.read_text())
        if data.get("status") != "completed" or not data.get("package"):
            print(f"  SKIP {result_path.parent.name} ({data.get('status', '?')})")
            continue

        topic = data["job"]["request"]["topic"]
        package = PublishPackage.model_validate(data["package"])

        result = emit_mdx(package, target_dir, topic_name=topic)
        emitted += 1

        print(f"  {result.topic_slug}/")
        for p in result.paths_written:
            print(f"    {p}/page.mdx")
        print(f"    _evidence.json  meta.json")
        print(f"    ({result.files_written} files, {len(package.evidence)} evidence)")
        print()

    print(f"Done — {emitted} topics → {target_dir}/")


if __name__ == "__main__":
    main()
