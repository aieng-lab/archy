#!/usr/bin/env python3
"""
process_md_folder.py
Create a JSON-Instance in folder md.

For the "md" folder only (data/md by default):

The folder structure under data/md mirrors GitHub:
    data/md/<GitHub user or org>/<Repository>/<...original path...>

Recursively walks this structure. For every file with extension .md,
.rst, .png, .jpg (.jpeg accepted as a variant) or .svg a JSON instance
(role "document") is created NEXT TO the file, in the same folder -
the folder structure and existing files are never changed or renamed.
Files with any other extension (.html etc.) are ignored entirely.

--------------------------------------------------------------------
Exception: document-with-chunks
--------------------------------------------------------------------
If a folder contains EXACTLY ONE .md or .rst file AND one or more
.png/.jpg/.svg files, that is treated specially:
    - the single .md/.rst file becomes a "document" instance with
      entity_modality "bimodal" and children_chunk_ids listing all
      the image files' entity_source_path values.
    - every .png/.jpg/.svg file in that folder becomes a "chunk"
      instance instead of an independent document, with
      parent_doc_id / parent_doc_path pointing back to that document.

In every other case (no images, no md/rst file, or more than one
md/rst file in the folder), every matching file simply gets its own
independent "document" instance (images included).
--------------------------------------------------------------------

Naming: doc-nnnn-<filename-without-extension>.json (document) /
        chunk-nnnn-<filename-without-extension>.json (chunk)
nnnn is also used as the entity_id ("doc-nnnn" / "chunk-nnnn").

nnnn is ONE shared, globally unique running number across BOTH doc-
and chunk- instances (not two separate counters) - the script scans
all existing JSON files under the md folder first, and picks the next
free number starting at 7600, capped at 9999. If a number would ever
exceed 9999, the script stops with an error rather than silently
reusing or overflowing a number.

Already-processed files (a matching doc-*-<name>.json or
chunk-*-<name>.json already exists next to them) are skipped, so
re-running the script is safe.

--------------------------------------------------------------------
Attribute values (from the provided Excel mapping)
--------------------------------------------------------------------
Fixed for every instance, any role:
    entity_origin      = "Open-Source"            (document-without-chunks
                                                     role in the Excel;
                                                     applied to all roles
                                                     here since the schema
                                                     requires a valid,
                                                     non-null value for
                                                     every instance)
    entity_purpose     = "Specification"
    entity_confidence  = "high"
    imported_at        = "2026-06-03"
    version             = "1.0"
    status              = "approved"
    authors             = []
    page_range/line_range/char_range/section = null

Derived:
    entity_title            the file name
    entity_modality          text/image depending on extension, except
                             "bimodal" for a document-with-chunks
    mime_type                 from extension (.md->text/markdown,
                             .rst->text/plain [no dedicated schema
                             value for reStructuredText], .png->
                             image/png, .jpg/.jpeg->image/jpg,
                             .svg->image/svg)
    project_name              "<GitHub user>/<Repository>"
    project_uri               "https://github.com/<user>/<repo>.git"
                             (ends in .git - GitHub's "Code" > "HTTPS" URL)
    issue_tracking_uri         "https://github.com/<user>/<repo>/issues"
    version_controlled_uri     "https://github.com/<user>/<repo>/commits"
    created_at                the GitHub repository's creation date,
                             fetched from the public GitHub API and
                             cached per repository (queried once per
                             repo, no matter how many files it has).
                             Left null if the API call fails.

--------------------------------------------------------------------
Dry run by default (as requested)
--------------------------------------------------------------------
Reports what it WOULD create, without writing any files, unless
--apply is passed.

Requires: requests (only needed for the real, non---mock GitHub
lookup)
    pip install requests

Usage:
    python3 process_md_folder.py \
        --schema entity-schema.json \
        --md-folder data/md \
        [--apply] [--mock]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

MD_RST_EXTENSIONS = {".md", ".rst"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg"}
ALL_EXTENSIONS = MD_RST_EXTENSIONS | IMAGE_EXTENSIONS

ID_MIN = 7600
ID_MAX = 9999

FIXED_VALUES = {
    "entity_origin": "Open-Source",
    "entity_purpose": "Specification",
    "entity_confidence": "high",
    "imported_at": "2026-06-03",
    "version": "1.0",
    "status": "approved",
    "authors": [],
}

EXTENSION_TO_MIME = {
    ".md": "text/markdown",
    ".rst": "text/plain",  # no dedicated reStructuredText value in the schema
    ".png": "image/png",
    ".jpg": "image/jpg",
    ".jpeg": "image/jpg",
    ".svg": "image/svg",
}


ENTITY_ID_PATTERN = re.compile(r'"entity_id"\s*:\s*"(?:doc|chunk)-(\d+)"')


def title_from_filename(stem: str) -> str:
    words = re.split(r"[_\-]+", stem)
    return " ".join(w.capitalize() if w.islower() or w.isupper() else w for w in words if w).strip()


# ---------------------------------------------------------------------------
# Shared, globally-unique running number (doc- and chunk- share one pool)
# ---------------------------------------------------------------------------

class IdPool:
    def __init__(self, used: set[int]):
        self.used = set(used)
        self.next_candidate = ID_MIN

    @classmethod
    def scan(cls, md_folder: Path) -> "IdPool":
        used = set()
        for json_path in md_folder.rglob("*.json"):
            try:
                text = json_path.read_text(encoding="utf-8")
            except OSError:
                continue
            for m in ENTITY_ID_PATTERN.finditer(text):
                used.add(int(m.group(1)))
        return cls(used)

    def take_next(self) -> int:
        while self.next_candidate in self.used:
            self.next_candidate += 1
        if self.next_candidate > ID_MAX:
            raise RuntimeError(f"Ran out of IDs: exceeded maximum {ID_MAX}.")
        number = self.next_candidate
        self.used.add(number)
        self.next_candidate += 1
        return number


# ---------------------------------------------------------------------------
# GitHub lookup (created_at only - everything else is built from the path)
# ---------------------------------------------------------------------------

class GitHubLookupError(Exception):
    pass


def fetch_repo_created_at(owner: str, repo: str, max_retries: int = 2, backoff_seconds: float = 1.5) -> str | None:
    import time
    import requests

    url = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "process_md_folder-script"}

    last_exception: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=(5, 8))
            if resp.status_code == 404:
                raise GitHubLookupError(f"Repository {owner}/{repo} not found (404)")
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                raise requests.exceptions.HTTPError(f"{resp.status_code} from GitHub API", response=resp)
            resp.raise_for_status()
            data = resp.json()
            created_at = data.get("created_at")  # e.g. "2011-01-26T19:01:12Z"
            return created_at.split("T")[0] if created_at else None
        except GitHubLookupError:
            raise
        except requests.exceptions.RequestException as exc:
            last_exception = exc
            if attempt < max_retries:
                time.sleep(backoff_seconds * attempt)
                continue
    raise GitHubLookupError(f"Failed to fetch {owner}/{repo}: {last_exception}")


def mock_fetch_repo_created_at(owner: str, repo: str) -> str | None:
    return "2011-01-26"  # fixed fake value for offline testing


class GitHubInfoCache:
    """Caches created_at per (owner, repo) so each repo is only queried once."""

    def __init__(self, fetch_fn, fail_fast_after: int = 3):
        self.fetch_fn = fetch_fn
        self.fail_fast_after = fail_fast_after
        self._cache: dict[tuple[str, str], str | None] = {}
        self.consecutive_errors = 0
        self.errors: list[tuple[str, str]] = []
        self.circuit_breaker_open = False

    def get_created_at(self, owner: str, repo: str) -> str | None:
        key = (owner, repo)
        if key in self._cache:
            return self._cache[key]

        if self.circuit_breaker_open:
            self._cache[key] = None
            return None

        try:
            created_at = self.fetch_fn(owner, repo)
            self.consecutive_errors = 0
        except GitHubLookupError as exc:
            created_at = None
            self.consecutive_errors += 1
            self.errors.append((f"{owner}/{repo}", str(exc)))
            if self.consecutive_errors >= self.fail_fast_after:
                self.circuit_breaker_open = True

        self._cache[key] = created_at
        return created_at


# ---------------------------------------------------------------------------
# Instance building
# ---------------------------------------------------------------------------

def base_instance(entity_id: str, entity_source_path: str, entity_role: str, entity_title: str,
                   entity_modality: str, mime_type: str, project_name: str, project_uri: str,
                   issue_tracking_uri: str, version_controlled_uri: str, created_at: str | None,
                   parent_doc_id: str | None, parent_doc_path: str | None,
                   children_chunk_ids: list[str] | None) -> dict:
    return OrderedDict([
        ("json_schema", "2020-12"),
        ("schema_version", "0.9"),
        ("entity_id", entity_id),
        ("entity_source_path", entity_source_path),
        ("entity_role", entity_role),
        ("parent_doc_id", parent_doc_id),
        ("parent_doc_path", parent_doc_path),
        ("children_chunk_ids", children_chunk_ids),
        ("entity_title", entity_title),
        ("entity_origin", FIXED_VALUES["entity_origin"]),
        ("entity_purpose", FIXED_VALUES["entity_purpose"]),
        ("entity_confidence", FIXED_VALUES["entity_confidence"]),
        ("entity_modality", entity_modality),
        ("mime_type", mime_type),
        ("project_name", project_name),
        ("project_uri", project_uri),
        ("issue_tracking_uri", issue_tracking_uri),
        ("version_controlled_uri", version_controlled_uri),
        ("created_at", created_at),
        ("imported_at", FIXED_VALUES["imported_at"]),
        ("version", FIXED_VALUES["version"]),
        ("authors", list(FIXED_VALUES["authors"])),
        ("status", FIXED_VALUES["status"]),
        ("page_range", None),
        ("line_range", None),
        ("char_range", None),
        ("section", None),
    ])


def already_processed(folder: Path, stem: str) -> Path | None:
    pattern = re.compile(r"^(?:doc|chunk)-\d+-" + re.escape(stem) + r"\.json$")
    for candidate in folder.iterdir():
        if candidate.is_file() and pattern.match(candidate.name):
            return candidate
    return None


def process_md_folder(md_folder: Path, id_pool: IdPool, gh_cache: GitHubInfoCache, dry_run: bool) -> tuple[dict, list, list]:
    stats = {
        "folders_scanned": 0,
        "files_matched": 0,
        "files_ignored_other_extension": 0,
        "documents_created": 0,
        "documents_with_chunks_created": 0,
        "chunks_created": 0,
        "skipped_already_processed": 0,
        "errors": 0,
        "assigned_ids": [],
        "repos_seen": set(),
    }
    error_details = []
    plan = []

    for folder in sorted([md_folder] + [p for p in md_folder.rglob("*") if p.is_dir()]):
        try:
            rel_parts = folder.relative_to(md_folder).parts
        except ValueError:
            continue
        if len(rel_parts) < 2:
            continue  # need at least <owner>/<repo> before any real content folder
        owner, repo = rel_parts[0], rel_parts[1]
        stats["repos_seen"].add((owner, repo))

        all_files = [p for p in folder.iterdir() if p.is_file()]
        matched = sorted(p for p in all_files if p.suffix.lower() in ALL_EXTENSIONS)
        ignored = [p for p in all_files if p.suffix.lower() not in ALL_EXTENSIONS]
        stats["files_ignored_other_extension"] += len(ignored)
        if not matched:
            continue
        stats["folders_scanned"] += 1
        stats["files_matched"] += len(matched)

        md_rst_files = [p for p in matched if p.suffix.lower() in MD_RST_EXTENSIONS]
        image_files = [p for p in matched if p.suffix.lower() in IMAGE_EXTENSIONS]

        project_name = f"{owner}/{repo}"
        base_repo_url = f"https://github.com/{owner}/{repo}"
        project_uri = f"{base_repo_url}.git"
        issue_tracking_uri = f"{base_repo_url}/issues"
        version_controlled_uri = f"{base_repo_url}/commits"
        created_at = gh_cache.get_created_at(owner, repo)

        def rel_source_path(file_path: Path) -> str:
            rel = file_path.relative_to(md_folder).as_posix()
            return f"{md_folder.parent.name}/{md_folder.name}/{rel}" if md_folder.parent.name else f"{md_folder.name}/{rel}"

        exception_case = len(md_rst_files) == 1 and len(image_files) >= 1

        if exception_case:
            doc_file = md_rst_files[0]
            stem = doc_file.stem
            existing = already_processed(folder, stem)
            if existing is not None:
                # Document and its chunks are always created together in a
                # single run, so if the document exists, so do its chunks.
                stats["skipped_already_processed"] += 1 + len(image_files)
            else:
                try:
                    doc_number = id_pool.take_next()
                except RuntimeError as exc:
                    stats["errors"] += 1
                    error_details.append((str(doc_file), str(exc)))
                    continue
                doc_entity_id = f"doc-{doc_number}"
                doc_source_path = rel_source_path(doc_file)

                chunk_entries = []
                for img_file in image_files:
                    img_stem = img_file.stem
                    img_existing = already_processed(folder, img_stem)
                    if img_existing is not None:
                        stats["skipped_already_processed"] += 1
                        continue
                    try:
                        chunk_number = id_pool.take_next()
                    except RuntimeError as exc:
                        stats["errors"] += 1
                        error_details.append((str(img_file), str(exc)))
                        continue
                    chunk_entity_id = f"chunk-{chunk_number}"
                    chunk_source_path = rel_source_path(img_file)
                    chunk_entries.append((img_file, img_stem, chunk_entity_id, chunk_source_path))

                children_chunk_ids = [c[3] for c in chunk_entries]

                doc_instance = base_instance(
                    entity_id=doc_entity_id, entity_source_path=doc_source_path, entity_role="document",
                    entity_title=title_from_filename(stem), entity_modality="bimodal",
                    mime_type=EXTENSION_TO_MIME[doc_file.suffix.lower()],
                    project_name=project_name, project_uri=project_uri,
                    issue_tracking_uri=issue_tracking_uri, version_controlled_uri=version_controlled_uri,
                    created_at=created_at,
                    parent_doc_id=None, parent_doc_path=None, children_chunk_ids=children_chunk_ids,
                )
                doc_json_path = folder / f"{doc_entity_id}-{stem}.json"
                plan.append((folder, doc_entity_id, doc_json_path, doc_instance, doc_file.name))
                stats["documents_with_chunks_created"] += 1
                stats["assigned_ids"].append(doc_entity_id)

                for img_file, img_stem, chunk_entity_id, chunk_source_path in chunk_entries:
                    chunk_instance = base_instance(
                        entity_id=chunk_entity_id, entity_source_path=chunk_source_path, entity_role="chunk",
                        entity_title=title_from_filename(img_stem),
                        entity_modality="image", mime_type=EXTENSION_TO_MIME[img_file.suffix.lower()],
                        project_name=project_name, project_uri=project_uri,
                        issue_tracking_uri=issue_tracking_uri, version_controlled_uri=version_controlled_uri,
                        created_at=created_at,
                        parent_doc_id=doc_entity_id, parent_doc_path=doc_source_path, children_chunk_ids=None,
                    )
                    chunk_json_path = folder / f"{chunk_entity_id}-{img_stem}.json"
                    plan.append((folder, chunk_entity_id, chunk_json_path, chunk_instance, img_file.name))
                    stats["chunks_created"] += 1
                    stats["assigned_ids"].append(chunk_entity_id)

        else:
            for f in matched:
                stem = f.stem
                existing = already_processed(folder, stem)
                if existing is not None:
                    stats["skipped_already_processed"] += 1
                    continue
                try:
                    number = id_pool.take_next()
                except RuntimeError as exc:
                    stats["errors"] += 1
                    error_details.append((str(f), str(exc)))
                    continue
                entity_id = f"doc-{number}"
                source_path = rel_source_path(f)
                modality = "text" if f.suffix.lower() in MD_RST_EXTENSIONS else "image"
                instance = base_instance(
                    entity_id=entity_id, entity_source_path=source_path, entity_role="document",
                    entity_title=title_from_filename(stem), entity_modality=modality,
                    mime_type=EXTENSION_TO_MIME[f.suffix.lower()],
                    project_name=project_name, project_uri=project_uri,
                    issue_tracking_uri=issue_tracking_uri, version_controlled_uri=version_controlled_uri,
                    created_at=created_at,
                    parent_doc_id=None, parent_doc_path=None, children_chunk_ids=None,
                )
                json_path = folder / f"{entity_id}-{stem}.json"
                plan.append((folder, entity_id, json_path, instance, f.name))
                stats["documents_created"] += 1
                stats["assigned_ids"].append(entity_id)

    if not dry_run:
        for _folder, _entity_id, json_path, instance, _source_name in plan:
            with json_path.open("w", encoding="utf-8") as f:
                json.dump(instance, f, indent=2, ensure_ascii=False)
                f.write("\n")

    return stats, plan, error_details


def print_report(md_folder: Path, dry_run: bool, stats: dict, plan: list, error_details: list, gh_cache: GitHubInfoCache) -> None:
    mode = "DRY RUN (no files written)" if dry_run else "APPLIED"
    print("=" * 70)
    print("Plan (new instances)")
    print("=" * 70)
    for folder, entity_id, json_path, instance, source_name in plan:
        print(f"[{instance['entity_role']:<8}] {entity_id:<12} <- {source_name:<25} -> {json_path}")
        if instance["entity_role"] == "document" and instance["children_chunk_ids"]:
            for c in instance["children_chunk_ids"]:
                print(f"             children_chunk_ids: {c}")
        if instance["entity_role"] == "chunk":
            print(f"             parent_doc_id: {instance['parent_doc_id']}")

    print("=" * 70)
    print("Statistics")
    print("=" * 70)
    print(f"Mode                                 : {mode}")
    print(f"MD folder                            : {md_folder}")
    print(f"Repositories seen                    : {len(stats['repos_seen'])}")
    print("-" * 70)
    print(f"Folders with matching files           : {stats['folders_scanned']}")
    print(f"Matching files (.md/.rst/.png/.jpg/.svg): {stats['files_matched']}")
    print(f"Ignored files (other extension)       : {stats['files_ignored_other_extension']}")
    print(f"Documents created (no chunks)          : {stats['documents_created']}")
    print(f"Documents created (with chunks)         : {stats['documents_with_chunks_created']}")
    print(f"Chunks created                         : {stats['chunks_created']}")
    print(f"Skipped (already processed)            : {stats['skipped_already_processed']}")
    print(f"Errors                                 : {stats['errors']}")
    if stats["assigned_ids"]:
        print(f"Assigned IDs                           : {', '.join(stats['assigned_ids'])}")
    if gh_cache.errors:
        print("-" * 70)
        print("GitHub lookup errors (created_at left null for affected repos):")
        for repo, err in gh_cache.errors:
            print(f"  - {repo}: {err}")
        if gh_cache.circuit_breaker_open:
            print("  (circuit breaker opened - remaining repos were not queried)")
    if error_details:
        print("-" * 70)
        print("Errors:")
        for name, err in error_details:
            print(f"  - {name}: {err}")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema", type=Path, default=Path("entity-schema.json"))
    parser.add_argument("--md-folder", type=Path, default=Path("data/md"))
    parser.add_argument("--apply", action="store_true",
                         help="Write the planned JSON instances to disk. "
                              "Without this flag the script always runs as a DRY RUN.")
    parser.add_argument("--mock", action="store_true",
                         help="Use a fixed fake value instead of a real GitHub API "
                              "call for created_at (offline testing only).")
    args = parser.parse_args()

    if not args.schema.is_file():
        print(f"ERROR: schema file not found: {args.schema}", file=sys.stderr)
        return 1

    resolved = args.md_folder.resolve()
    existed_before = args.md_folder.is_dir()
    print(f"Working directory       : {Path.cwd()}", flush=True)
    print(f"MD folder (as given)    : {args.md_folder}", flush=True)
    print(f"MD folder (absolute)    : {resolved}", flush=True)
    if not existed_before:
        print(f"ERROR: folder not found: {args.md_folder}", file=sys.stderr)
        return 1

    id_pool = IdPool.scan(args.md_folder)
    print(f"Next available number to use: {max(id_pool.used, default=ID_MIN - 1) + 1 if id_pool.used else ID_MIN}", flush=True)

    fetch_fn = mock_fetch_repo_created_at if args.mock else fetch_repo_created_at
    gh_cache = GitHubInfoCache(fetch_fn)

    stats, plan, error_details = process_md_folder(args.md_folder, id_pool, gh_cache, not args.apply)
    print_report(args.md_folder, not args.apply, stats, plan, error_details, gh_cache)
    return 0 if stats["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
