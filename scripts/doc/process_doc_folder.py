#!/usr/bin/env python3
"""
process_doc_folder.py
Create a JSON-Instance in folder doc.

For the "doc" folder only (data/doc by default):

For every file with extension .doc, .docx, .adoc or .txt found
directly in that folder:

  1. Create a subfolder named after the file (without extension):
         data/doc/<filename-without-extension>/
     If data/doc already exists, it is reused as-is - only the
     per-file subfolders are newly created inside it.

  2. Copy the file into that subfolder.

  3. Determine the next running number:
       - Recursively search all JSON files already present under the
         doc folder for their "entity_id" (pattern "doc-<number>").
       - Continue with the highest number found + 1.
       - If no such JSON file / number exists yet, start at 0001.
       - Numbers are always unique and zero-padded to at least 4
         digits (0001, 0002, ... 0010, ... 10001 if it ever grows
         beyond 4 digits the extra digits are simply kept).

  4. Rename the COPY inside the subfolder to:
         doc-<number>-<filename-without-extension>.<extension>

  5. Create a JSON instance in the same subfolder, named:
         doc-<number>-<filename-without-extension>.json
     based on the template below (derived from entity-schema.json,
     since no separate instance-pattern attachment was given this
     time), with these fields filled in:
       - entity_id            = "doc-<number>"
       - entity_source_path   = "data/doc/doc-<number>-<name>.<ext>"
       - mime_type             = derived from the file extension
       - imported_at          = "2026-05-07"
       - entity_title         = ""
     All other template fields are kept unchanged.

  6. The running number is incremented for every new file processed.

--------------------------------------------------------------------
mime_type note - .adoc has no dedicated schema value
--------------------------------------------------------------------
The schema's mime_type enum defines: application/pdf, application/doc,
image/jpg, image/png, image/svg, text/markdown, text/plain,
text/x-rst - there is still no dedicated AsciiDoc type. This script
maps:
    .doc, .docx -> application/doc
    .txt        -> text/plain
    .adoc       -> text/markdown  (best-effort: AsciiDoc is a
                    lightweight markup language, closest existing
                    enum value - flagged in the console output so you
                    can change EXTENSION_TO_MIME below if you'd rather
                    use a different value, e.g. text/x-rst doesn't fit
                    either since that's specifically reStructuredText)
--------------------------------------------------------------------

The script is idempotent: if a subfolder for a given file already
contains a matching "doc-*-<name>.json", that file is considered
already processed and is skipped on subsequent runs.

A statistics summary is printed at the end.

Usage:
    python3 process_doc_folder.py \
        [--doc-folder data/doc] \
        [--template entity-schema-template.json] \
        [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# Instance template derived from entity-schema.json (no separate
# instance-pattern attachment was provided this time). Uses the
# current field set, including "version_controlled_uri" and a nullable
# imported_at. entity_confidence defaults to the schema's
# "others" enum value so freshly created instances are schema-valid even
# before a later enrichment step fills in the real classification.
DEFAULT_TEMPLATE = {
    "json_schema": "2020-12",
    "schema_version": "1.0",
    "entity_id": "",
    "entity_source_path": "",
    "entity_uri": None,
    "entity_role": "document",
    "parent_doc_id": None,
    "parent_doc_path": None,
    "children_chunk_ids": None,
    "entity_title": "",
    "entity_is_readme": False,
    "entity_is_architectural": True,
    "entity_confidence": "others",
    "entity_modality": "text",
    "mime_type": "application/doc",
    "project_name": "",
    "project_uri": "",
    "issue_tracking_uri": None,
    "version_controlled_uri": None,
    "imported_at": None,
    "page_range": None,
    "line_range": None,
    "char_range": None,
    "section": None,
}

IMPORTED_AT = "2026-05-07"
STARTING_ID = 1
ID_WIDTH = 4  # zero-padded to at least this many digits

# Only these extensions are scanned/processed, as specified.
DOC_EXTENSIONS_TO_SCAN = {".doc", ".docx", ".adoc", ".txt"}

# Extension -> mime_type, restricted to the schema's mime_type enum.
# .adoc has no dedicated value in the schema - text/markdown is used
# as the closest best-effort fit (see module docstring above).
EXTENSION_TO_MIME = {
    ".doc": "application/doc",
    ".docx": "application/doc",
    ".txt": "text/plain",
    ".adoc": "text/markdown",
}
ADOC_MIME_NOTE = "best-effort mapping: schema has no dedicated AsciiDoc mime_type"

ENTITY_ID_PATTERN = re.compile(r'"entity_id"\s*:\s*"doc-(\d+)"')


def load_template(template_path: Path | None) -> dict:
    if template_path is None:
        return dict(DEFAULT_TEMPLATE)
    with template_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_next_id(doc_folder: Path) -> int:
    highest = None
    for json_path in doc_folder.rglob("*.json"):
        try:
            text = json_path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = ENTITY_ID_PATTERN.search(text)
        if match:
            number = int(match.group(1))
            if highest is None or number > highest:
                highest = number
    return (highest + 1) if highest is not None else STARTING_ID


def format_id(number: int) -> str:
    return f"{number:0{ID_WIDTH}d}"


def already_processed(subfolder: Path, stem: str) -> Path | None:
    if not subfolder.is_dir():
        return None
    pattern = re.compile(r"^doc-\d+-" + re.escape(stem) + r"\.json$")
    for candidate in subfolder.iterdir():
        if candidate.is_file() and pattern.match(candidate.name):
            return candidate
    return None


def build_instance(template: dict, doc_id: str, source_path: str, mime_type: str) -> dict:
    instance = dict(template)
    instance["entity_id"] = doc_id
    instance["entity_source_path"] = source_path
    instance["mime_type"] = mime_type
    instance["imported_at"] = IMPORTED_AT
    instance["entity_title"] = ""
    return instance


def process_doc_folder(doc_folder: Path, template: dict, dry_run: bool) -> tuple[dict, list]:
    stats = {
        "doc_files_found": 0,
        "processed": 0,
        "skipped_already_processed": 0,
        "errors": 0,
        "assigned_ids": [],
        "adoc_mime_notes": 0,
    }
    error_details = []

    resolved = doc_folder.resolve()
    existed_before = doc_folder.is_dir()
    doc_folder.mkdir(parents=True, exist_ok=True)  # reuse if it already exists

    print(f"Working directory      : {Path.cwd()}", flush=True)
    print(f"Doc folder (as given)  : {doc_folder}", flush=True)
    print(f"Doc folder (absolute)  : {resolved}", flush=True)
    if not existed_before:
        print(
            "WARNING: this folder did NOT exist before this run - it was just "
            "created empty. This usually means the path is wrong relative to "
            "where you are running the script from. Use --doc-folder with an "
            "absolute path, or cd into the right directory first.",
            flush=True,
        )

    all_top_level_files = [p for p in doc_folder.iterdir() if p.is_file()]
    source_files = sorted(
        p for p in all_top_level_files if p.suffix.lower() in DOC_EXTENSIONS_TO_SCAN
    )
    stats["doc_files_found"] = len(source_files)

    if not source_files:
        print(f"No matching files (.doc/.docx/.adoc/.txt) found directly in {resolved}.", flush=True)
        if all_top_level_files:
            print("Files that ARE present in this folder (but not matched):", flush=True)
            for p in sorted(all_top_level_files)[:20]:
                print(f"  - {p.name}", flush=True)

    next_id_number = find_next_id(doc_folder)
    print(f"Next running number to use: {format_id(next_id_number)} "
          f"({'no existing doc- IDs found, starting fresh' if next_id_number == STARTING_ID else 'continuing after highest existing ID'})",
          flush=True)

    for i, source_file in enumerate(source_files, start=1):
        stem = source_file.stem
        ext = source_file.suffix
        subfolder = doc_folder / stem

        print(f"[{i}/{len(source_files)}] {source_file.name} ...", end=" ", flush=True)

        existing = already_processed(subfolder, stem)
        if existing is not None:
            stats["skipped_already_processed"] += 1
            print(f"already processed -> {existing.name}, skipped", flush=True)
            continue

        mime_type = EXTENSION_TO_MIME[ext.lower()]
        note = ""
        if ext.lower() == ".adoc":
            stats["adoc_mime_notes"] += 1
            note = f" ({ADOC_MIME_NOTE})"

        doc_id = format_id(next_id_number)
        entity_id = f"doc-{doc_id}"
        new_filename = f"{entity_id}-{stem}{ext}"
        dest_path = subfolder / new_filename
        source_path_value = f"{doc_folder.parent.name}/{doc_folder.name}/{new_filename}" \
            if doc_folder.parent.name else f"{doc_folder.name}/{new_filename}"

        instance = build_instance(template, entity_id, source_path_value, mime_type)
        json_path = subfolder / f"{entity_id}-{stem}.json"

        try:
            if not dry_run:
                subfolder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, dest_path)
                with json_path.open("w", encoding="utf-8") as f:
                    json.dump(instance, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            stats["processed"] += 1
            stats["assigned_ids"].append(entity_id)
            print(f"-> {entity_id}  ({dest_path.name}, {json_path.name}, mime_type={mime_type}){note}", flush=True)
            next_id_number += 1
        except OSError as exc:
            stats["errors"] += 1
            error_details.append((source_file.name, str(exc)))
            print(f"ERROR: {exc}", flush=True)

    return stats, error_details


def print_report(doc_folder: Path, dry_run: bool, stats: dict, error_details: list) -> None:
    mode = "DRY RUN (no files/folders written)" if dry_run else "APPLIED"
    print("=" * 70)
    print("Statistics")
    print("=" * 70)
    print(f"Mode                              : {mode}")
    print(f"Doc folder                        : {doc_folder}")
    print("-" * 70)
    print(f"Matching files found (top level)  : {stats['doc_files_found']}")
    print(f"Newly processed                   : {stats['processed']}")
    print(f"Skipped (already processed)       : {stats['skipped_already_processed']}")
    print(f"Errors                            : {stats['errors']}")
    if stats["adoc_mime_notes"]:
        print(f".adoc files (best-effort mime_type): {stats['adoc_mime_notes']} - {ADOC_MIME_NOTE}")
    if stats["assigned_ids"]:
        print(f"Assigned IDs                      : {', '.join(stats['assigned_ids'])}")
    if error_details:
        print("-" * 70)
        print("Errors:")
        for name, err in error_details:
            print(f"  - {name}: {err}")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--doc-folder", type=Path, default=Path("data/doc"),
                         help="Folder containing the source files (default: data/doc).")
    parser.add_argument("--template", type=Path, default=None,
                         help="Optional path to a JSON template file to use instead of the "
                              "built-in default template.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Report what would be done without creating/copying any files.")
    args = parser.parse_args()

    if args.template is not None and not args.template.is_file():
        print(f"ERROR: template file not found: {args.template}", file=sys.stderr)
        return 1

    template = load_template(args.template)

    stats, error_details = process_doc_folder(args.doc_folder, template, args.dry_run)
    print_report(args.doc_folder, args.dry_run, stats, error_details)
    return 0 if stats["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
