#!/usr/bin/env python3
"""
process_pdf_folder.py
Create a JSON-Instance in folder pdf.

For the "pdf" folder only (data/pdf by default):

For every PDF file found directly in that folder:

  1. Create a subfolder named after the file (without extension):
         data/pdf/<filename-without-extension>/
     If data/pdf already exists, it is reused as-is - only
     the per-file subfolders are newly created inside it.

  2. Copy the PDF file into that subfolder.

  3. Determine the next running number:
       - Recursively search all JSON files already present under the
         pdf folder for their "entity_id" (pattern "doc-<number>").
       - Continue with the highest number found + 1.
       - If no such JSON file / number exists yet, start at 6000.

  4. Rename the COPY inside the subfolder to:
         doc-<number>-<filename-without-extension>.<extension>

  5. Create a JSON instance in the same subfolder, named:
         doc-<number>-<filename-without-extension>.json
     based on the fixed template below, with these fields filled in:
       - entity_id            = "doc-<number>"
       - entity_source_path   = "data/pdf/doc-<number>-<name>.<ext>"
       - mime_type             = derived from the file extension
       - imported_at          = "2026-05-07"
       - entity_title         = ""
     All other template fields are kept unchanged.

  6. The running number is incremented for every new file processed.

The script is idempotent: if a subfolder for a given file already
contains a matching "doc-*-<name>.json", that file is considered
already processed and is skipped on subsequent runs (so re-running the
script after adding new PDFs does not create duplicate entries or burn
extra numbers for files that were already handled).

A statistics summary is printed at the end.

Usage:
    python3 process_pdf_folder.py \
        [--pdf-folder data/pdf] \
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

# Fixed instance template (from the "Anhang neu" pattern). Used as-is for
# every new instance unless --template points to an external file with a
# different template.
DEFAULT_TEMPLATE = {
    "json_schema": "2020-12",
    "schema_version": "0.9",
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
    "entity_source": "web-search",  # set to "pure" for documents from the PURE dataset
    "entity_modality": "text",
    "mime_type": "application/pdf",
    "project_name": "",
    "project_uri": "",
    "issue_tracking_uri": None,
    "version_controlled_uri": None,
    "imported_at": "2026-07-25",
    "page_range": None,
    "line_range": None,
    "char_range": None,
    "section": None,
}

# Fixed values requested for every newly created instance.
IMPORTED_AT = "2026-05-07"
STARTING_ID = 6000

# Extension -> mime_type mapping (matches the schema's mime_type enum).
EXTENSION_TO_MIME = {
    ".pdf": "application/pdf",
    ".doc": "application/doc",
    ".docx": "application/doc",
    ".jpg": "image/jpg",
    ".jpeg": "image/jpg",
    ".png": "image/png",
    ".svg": "image/svg",
    ".md": "text/markdown",
    ".txt": "text/plain",
}

ENTITY_ID_PATTERN = re.compile(r'"entity_id"\s*:\s*"doc-(\d+)"')


def load_template(template_path: Path | None) -> dict:
    if template_path is None:
        return dict(DEFAULT_TEMPLATE)
    with template_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_next_id(pdf_folder: Path) -> int:
    """
    Scan all JSON files under pdf_folder for "entity_id": "doc-<number>"
    and return the highest number + 1, or STARTING_ID if none is found.
    Uses a tolerant regex scan (not strict JSON parsing) so a malformed
    JSON file elsewhere does not stop the search.
    """
    highest = None
    for json_path in pdf_folder.rglob("*.json"):
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


def already_processed(subfolder: Path, stem: str) -> Path | None:
    """
    Return the path of an existing "doc-*-<stem>.json" in subfolder, if
    one is already there (meaning this source file was already handled
    in a previous run) - else None.
    """
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


def process_pdf_folder(pdf_folder: Path, template: dict, dry_run: bool) -> dict:
    stats = {
        "pdf_files_found": 0,
        "processed": 0,
        "skipped_already_processed": 0,
        "errors": 0,
        "assigned_ids": [],
        "unknown_extension_files": [],
    }
    error_details = []

    resolved = pdf_folder.resolve()
    existed_before = pdf_folder.is_dir()
    pdf_folder.mkdir(parents=True, exist_ok=True)  # reuse if it already exists

    print(f"Working directory      : {Path.cwd()}", flush=True)
    print(f"PDF folder (as given)  : {pdf_folder}", flush=True)
    print(f"PDF folder (absolute)  : {resolved}", flush=True)
    if not existed_before:
        print(
            "WARNING: this folder did NOT exist before this run - it was just "
            "created empty. This usually means the path is wrong relative to "
            "where you are running the script from. Use --pdf-folder with an "
            "absolute path, or cd into the right directory first.",
            flush=True,
        )

    # Only files directly in pdf_folder - subfolders created by earlier
    # runs are not re-scanned as "source" files.
    all_top_level_files = [p for p in pdf_folder.iterdir() if p.is_file()]
    source_files = sorted(
        p for p in all_top_level_files if p.suffix.lower() == ".pdf"
    )
    stats["pdf_files_found"] = len(source_files)

    if not source_files:
        print(
            f"No .pdf files found directly in {resolved}.",
            flush=True,
        )
        if all_top_level_files:
            print("Files that ARE present in this folder (but not matched as .pdf):", flush=True)
            for p in sorted(all_top_level_files)[:20]:
                print(f"  - {p.name}", flush=True)
        else:
            subfolders = [p for p in pdf_folder.iterdir() if p.is_dir()]
            if subfolders:
                print(
                    "The folder is otherwise empty at the top level but contains "
                    f"{len(subfolders)} subfolder(s) - if your PDFs are already "
                    "inside per-document subfolders, they were likely processed "
                    "in an earlier run (check for existing doc-*.json files there).",
                    flush=True,
                )
            else:
                print("The folder is completely empty.", flush=True)

    next_id = find_next_id(pdf_folder)
    print(f"Next running number to use: {next_id} "
          f"({'no existing doc- IDs found, starting fresh' if next_id == STARTING_ID else 'continuing after highest existing ID'})",
          flush=True)

    for i, source_file in enumerate(source_files, start=1):
        stem = source_file.stem
        ext = source_file.suffix
        subfolder = pdf_folder / stem

        print(f"[{i}/{len(source_files)}] {source_file.name} ...", end=" ", flush=True)

        existing = already_processed(subfolder, stem)
        if existing is not None:
            stats["skipped_already_processed"] += 1
            print(f"already processed -> {existing.name}, skipped", flush=True)
            continue

        mime_type = EXTENSION_TO_MIME.get(ext.lower())
        if mime_type is None:
            stats["unknown_extension_files"].append(source_file.name)
            mime_type = "application/pdf"  # sensible fallback for this pdf-only folder
            print(f"(unknown extension {ext!r}, defaulting mime_type to application/pdf) ", end="", flush=True)

        doc_id_number = next_id
        doc_id = f"doc-{doc_id_number}"
        new_filename = f"{doc_id}-{stem}{ext}"
        dest_path = subfolder / new_filename
        source_path_value = f"{pdf_folder.parent.name}/{pdf_folder.name}/{new_filename}"

        instance = build_instance(template, doc_id, source_path_value, mime_type)
        json_path = subfolder / f"{doc_id}-{stem}.json"

        try:
            if not dry_run:
                subfolder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, dest_path)
                with json_path.open("w", encoding="utf-8") as f:
                    json.dump(instance, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            stats["processed"] += 1
            stats["assigned_ids"].append(doc_id)
            print(f"-> {doc_id}  ({dest_path.name}, {json_path.name})", flush=True)
            next_id += 1
        except OSError as exc:
            stats["errors"] += 1
            error_details.append((source_file.name, str(exc)))
            print(f"ERROR: {exc}", flush=True)

    return stats, error_details


def print_report(pdf_folder: Path, dry_run: bool, stats: dict, error_details: list) -> None:
    mode = "DRY RUN (no files/folders written)" if dry_run else "APPLIED"
    print("=" * 70)
    print("Statistics")
    print("=" * 70)
    print(f"Mode                              : {mode}")
    print(f"PDF folder                        : {pdf_folder}")
    print("-" * 70)
    print(f"PDF files found (top level)       : {stats['pdf_files_found']}")
    print(f"Newly processed                   : {stats['processed']}")
    print(f"Skipped (already processed)       : {stats['skipped_already_processed']}")
    print(f"Errors                            : {stats['errors']}")
    if stats["assigned_ids"]:
        print(f"Assigned IDs                      : {', '.join(stats['assigned_ids'])}")
    if stats["unknown_extension_files"]:
        print("-" * 70)
        print("Files with unknown extension (mime_type defaulted to application/pdf):")
        for name in stats["unknown_extension_files"]:
            print(f"  - {name}")
    if error_details:
        print("-" * 70)
        print("Errors:")
        for name, err in error_details:
            print(f"  - {name}: {err}")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf-folder", type=Path, default=Path("data/pdf"),
                         help="Folder containing the source PDF files (default: data/pdf).")
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

    stats, error_details = process_pdf_folder(args.pdf_folder, template, args.dry_run)
    print_report(args.pdf_folder, args.dry_run, stats, error_details)
    return 0 if stats["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
