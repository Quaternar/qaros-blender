#!/usr/bin/env python3
"""Write a QAR-enabled copy of Khronos's CustomAnariRender.py; never overwrite input."""
import argparse
import ast
from pathlib import Path


def prepare(source: str) -> str:
    tree = ast.parse(source)
    candidates = [node for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "librarynames" for t in node.targets)]
    if len(candidates) != 1:
        raise ValueError("Expected exactly one top-level librarynames assignment")
    node = candidates[0]
    names = ast.literal_eval(node.value)
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("librarynames must be a literal list of library names")
    if "qaros" in names:
        return source
    names.append("qaros")
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = [f"librarynames = {names!r}\n"]
    result = "".join(lines)
    ast.parse(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Original CustomAnariRender.py")
    parser.add_argument("output", type=Path, help="New add-on copy; must not exist")
    args = parser.parse_args()
    try:
        text = prepare(args.input.read_text(encoding="utf-8"))
        with args.output.open("x", encoding="utf-8", newline="\n") as out:
            out.write(text)
    except (OSError, SyntaxError, ValueError) as exc:
        parser.exit(1, f"Cannot prepare add-on: {exc}\n")
    print(f"Created {args.output}. Install this instead of enabling two copies of the same add-on.")


if __name__ == "__main__":
    main()
