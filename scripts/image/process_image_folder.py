#!/usr/bin/env python3
"""
process_image_folder.py
Create a JSON-Instance in folder image.

For the "image" folder only (data/image by default) - creates
"document" instances ONLY, never "chunk" instances.

For every image file found directly in that folder (png, jpg/jpeg,
svg by default - see IMAGE_EXTENSIONS_TO_SCAN below, "weiteres" is
supported by extending that set, but only extensions that map to one
of the schema's mime_type enum values can actually be turned into a
valid instance):

  1. Create a subfolder named after the file (without extension):
         data/image/<filename-without-extension>/
     If data/image already exists, it is reused as-is - only
     the per-file subfolders are newly created inside it.

  2. Copy the image file into that subfolder.

  3. Determine the next running number:
       - Recursively search all JSON files already present under the
         image folder for their "entity_id" (pattern "doc-<number>").
       - Continue with the highest number found + 1.
       - If no such JSON file / number exists yet, start at 2000.

  4. Rename the COPY inside the subfolder to:
         doc-<number>-<filename-without-extension>.<extension>

  5. Create a JSON instance (role "document") in the same subfolder,
     named:
         doc-<number>-<filename-without-extension>.json
     based on the template below (derived from entity-schema.json,
     since no separate instance-pattern attachment was given this
     time), with these fields filled in:
       - entity_id            = "doc-<number>"
       - entity_source_path   = "data/image/doc-<number>-<name>.<ext>"
       - mime_type             = derived from the file extension
       - imported_at          = "2026-05-07"
       - entity_title         = ""
     All other template fields are kept unchanged.

  6. The running number is incremented for every new file processed.

Files whose extension has no corresponding schema mime_type (the
schema only defines image/png, image/jpeg and image/svg for images) are
NOT turned into an instance - they are listed separately in the
statistics instead.

The script is idempotent: if a subfolder for a given file already
contains a matching "doc-*-<name>.json", that file is considered
already processed and is skipped on subsequent runs.

--------------------------------------------------------------------
Dry run by default (as requested)
--------------------------------------------------------------------
Reports what it WOULD create/copy, without writing or copying
anything, unless --apply is passed.

Usage:
    python3 process_image_folder.py \
        [--image-folder data/image] \
        [--template entity-schema-template.json] \
        [--apply]
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
# imported_at. entity_source defaults to "web-search".
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
    "entity_modality": "image",
    "mime_type": "image/png",
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
STARTING_ID = 2000

# Extensions to look for in the top-level image folder ("weiteres" =
# broaden this set if you have other image types on disk).
IMAGE_EXTENSIONS_TO_SCAN = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}

# Extension -> mime_type, restricted to the schema's mime_type enum.
# Only extensions listed here can become a valid instance.
EXTENSION_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg",
}

ENTITY_ID_PATTERN = re.compile(r'"entity_id"\s*:\s*"doc-(\d+)"')


def load_template(template_path: Path | None) -> dict:
    if template_path is None:
        return dict(DEFAULT_TEMPLATE)
    with template_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_next_id(image_folder: Path) -> int:
    highest = None
    for json_path in image_folder.rglob("*.json"):
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
    instance["entity_role"] = "document"  # always a document, never a chunk
    instance["imported_at"] = IMPORTED_AT
    instance["entity_title"] = ""
    return instance


def process_image_folder(image_folder: Path, template: dict, dry_run: bool) -> tuple[dict, list]:
    stats = {
        "image_files_found": 0,
        "processed": 0,
        "skipped_already_processed": 0,
        "skipped_unsupported_extension": 0,
        "errors": 0,
        "assigned_ids": [],
        "unsupported_files": [],
    }
    error_details = []

    resolved = image_folder.resolve()
    existed_before = image_folder.is_dir()
    image_folder.mkdir(parents=True, exist_ok=True)  # reuse if it already exists

    print(f"Working directory       : {Path.cwd()}", flush=True)
    print(f"Image folder (as given) : {image_folder}", flush=True)
    print(f"Image folder (absolute) : {resolved}", flush=True)
    if not existed_before:
        print(
            "WARNING: this folder did NOT exist before this run - it was just "
            "created empty. This usually means the path is wrong relative to "
            "where you are running the script from. Use --image-folder with an "
            "absolute path, or cd into the right directory first.",
            flush=True,
        )

    all_top_level_files = [p for p in image_folder.iterdir() if p.is_file()]
    source_files = sorted(
        p for p in all_top_level_files if p.suffix.lower() in IMAGE_EXTENSIONS_TO_SCAN
    )
    stats["image_files_found"] = len(source_files)

    if not source_files:
        print(f"No matching image files found directly in {resolved}.", flush=True)
        if all_top_level_files:
            print("Files that ARE present in this folder (but not matched):", flush=True)
            for p in sorted(all_top_level_files)[:20]:
                print(f"  - {p.name}", flush=True)

    next_id = find_next_id(image_folder)
    print(f"Next running number to use: {next_id} "
          f"({'no existing doc- IDs found, starting fresh' if next_id == STARTING_ID else 'continuing after highest existing ID'})",
          flush=True)

    for i, source_file in enumerate(source_files, start=1):
        stem = source_file.stem
        ext = source_file.suffix
        subfolder = image_folder / stem

        print(f"[{i}/{len(source_files)}] {source_file.name} ...", end=" ", flush=True)

        existing = already_processed(subfolder, stem)
        if existing is not None:
            stats["skipped_already_processed"] += 1
            print(f"already processed -> {existing.name}, skipped", flush=True)
            continue

        mime_type = EXTENSION_TO_MIME.get(ext.lower())
        if mime_type is None:
            stats["skipped_unsupported_extension"] += 1
            stats["unsupported_files"].append(source_file.name)
            print(
                f"SKIPPED - extension {ext!r} has no matching mime_type in the "
                "schema's enum (only .png/.jpg/.jpeg/.svg are supported)",
                flush=True,
            )
            continue

        doc_id_number = next_id
        doc_id = f"doc-{doc_id_number}"
        new_filename = f"{doc_id}-{stem}{ext}"
        dest_path = subfolder / new_filename
        source_path_value = f"{image_folder.parent.name}/{image_folder.name}/{new_filename}" \
            if image_folder.parent.name else f"{image_folder.name}/{new_filename}"

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


def print_report(image_folder: Path, dry_run: bool, stats: dict, error_details: list) -> None:
    mode = "DRY RUN (no files/folders written)" if dry_run else "APPLIED"
    print("=" * 70)
    print("Statistics")
    print("=" * 70)
    print(f"Mode                              : {mode}")
    print(f"Image folder                      : {image_folder}")
    print("-" * 70)
    print(f"Image files found (top level)     : {stats['image_files_found']}")
    print(f"Newly processed                   : {stats['processed']}")
    print(f"Skipped (already processed)       : {stats['skipped_already_processed']}")
    print(f"Skipped (unsupported extension)   : {stats['skipped_unsupported_extension']}")
    print(f"Errors                            : {stats['errors']}")
    if stats["assigned_ids"]:
        print(f"Assigned IDs                      : {', '.join(stats['assigned_ids'])}")
    if stats["unsupported_files"]:
        print("-" * 70)
        print("Files with an extension not in the schema's mime_type enum (not processed):")
        for name in stats["unsupported_files"]:
            print(f"  - {name}")
    if error_details:
        print("-" * 70)
        print("Errors:")
        for name, err in error_details:
            print(f"  - {name}: {err}")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image-folder", type=Path, default=Path("data/image"),
                         help="Folder containing the source image files (default: data/image).")
    parser.add_argument("--template", type=Path, default=None,
                         help="Optional path to a JSON template file to use instead of the "
                              "built-in default template.")
    parser.add_argument("--apply", action="store_true",
                         help="Write/copy the planned files to disk. Without this flag the "
                              "script always runs as a DRY RUN (default, as requested).")
    args = parser.parse_args()

    if args.template is not None and not args.template.is_file():
        print(f"ERROR: template file not found: {args.template}", file=sys.stderr)
        return 1

    template = load_template(args.template)

    stats, error_details = process_image_folder(args.image_folder, template, dry_run=not args.apply)
    print_report(args.image_folder, not args.apply, stats, error_details)
    return 0 if stats["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
