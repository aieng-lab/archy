#!/usr/bin/env python3
"""
GitHub File Miner v4
====================
Reads repos from a CSV, checks quality filters, traverses up to MAX_DEPTH
directory levels (non-recursive, GitHub Trees API) and downloads files whose
CONTENT contains at least one architecture/design keyword.

Keyword matching
────────────────
• Filenames are irrelevant – any filename is accepted.
• For text/markup formats (.md .html .xml .adoc .rst .svg .uml .puml
  .xmi .drawio): keyword must appear inside the file content.
• For binary formats (.pdf .docx .pptx .vsd .png .jpg …): content
  cannot be decoded as text, so the parent directory path is checked
  as a fallback heuristic.

Path strategy
─────────────
• Files WITHOUT image references are saved with the GitHub path mirrored 1-to-1:
    {output}/{owner}/{repo}/docs/overview.md

• Files WITH image references get a sibling folder named
  ``{file_stem}_images``. BOTH the document AND all its images land there.
  Image references inside the document are rewritten to plain basenames
  so every file is directly openable with a single click:
    {output}/{owner}/{repo}/docs/system-design_images/
        system-design.md        ← refs rewritten to plain names
        component.png
        logo.png

Token:  export GITHUB_TOKEN=ghp_...   OR   create file  .github_token
Usage:  python filter21_improved.py repos.csv [--output-dir DIR] [--debug]
Needs:  pip install requests
"""

import os, re, csv, sys, json, time, base64, logging, argparse, textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("pip install requests")

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DOWNLOAD_DIR   = "github_download3_1"     # PB 3
MAX_FILES_REPO = 550          # max files *confirmed* (keyword found) per repo     # 20
MAX_CANDIDATES = 1520         # max candidate files fetched per repo for content-check   #120
                             # (should be >> MAX_FILES_REPO; many candidates may not
                             #  contain keywords and are discarded after fetching)
MAX_DEPTH      = 5           # directory depth to traverse (root = 0)                   #4
MAX_REPOS      = 50          # max qualifying repos to process                    # 20
MAX_TREE_CALLS = 650         # BFS hard-stop: max tree-API calls per repo            #150
PAUSE          = 0.2         # seconds between API calls
API            = "https://api.github.com"
RAW            = "https://raw.githubusercontent.com"

# Keywords searched inside file CONTENT (case-insensitive).
# Filenames are irrelevant – any filename is accepted as long as the
# content contains at least one of these keywords.
KEYWORDS = [
    "architecture", "design", "decision", 
    "software engineering", "specification", "construction",
]

# All accepted file extensions.
# No keyword check on the filename/path – only on file content.
EXTENSIONS = frozenset({
    ".md", ".html", ".doc", ".docx", ".adoc",        # kein ".xml",
    ".pdf", ".svg", ".rst", ".vsd", ".vsdx",
    ".ppt", ".pptx", ".png", ".jpg", ".jpeg",
    ".uml", ".puml", ".xmi", ".drawio",
})

# Text-based extensions whose byte content can be decoded and searched
# for keywords.  All formats listed here are plain-text or XML/markup.
CONTENT_SEARCHABLE_EXT = frozenset({
    ".md", ".html", ".adoc", ".rst",                 # kein ".xml",           
    ".svg",                   # SVG is XML
    ".uml", ".puml",          # PlantUML text
    ".xmi",                   # XMI is XML
    ".drawio",                # draw.io is XML
})

# Binary extensions that cannot be grepped for text keywords without
# additional libraries.  For these, keyword presence in the parent
# *directory path* is used as a fallback heuristic.
BINARY_EXT = frozenset({
    ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".vsd", ".vsdx", ".png", ".jpg", ".jpeg",
})

# Extensions whose content is parsed for embedded image links
TEXT_EXT = frozenset({".md", ".html", ".adoc", ".rst"})   # kein ".xml"

# Extensions rendered as <img> previews in the HTML index
IMG_EXT = frozenset({".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"})

# Repository quality filter thresholds
FILTER = dict(
    open_issues_min=5,   commits_min=10,  contributors_min=100,   # contr =10
    size_min_kb=10,      forks_min=0,    stars_min=100,
    fork=False,          archived=False,  private=False,
    created_before=datetime(2020, 1, 1, tzinfo=timezone.utc),   # 2021,1,1
    pushed_after =datetime(2024, 1, 1, tzinfo=timezone.utc),    # 2022,1,1
)

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  GITHUB CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class GitHubClient:
    """Auth + throttle + retry on rate-limit and transport errors."""

    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-file-miner/2.0",
        })
        self._last = 0.0
        # urllib3 retry for 5xx / connection resets
        adapter = HTTPAdapter(max_retries=Retry(
            total=4, backoff_factor=1.5, status_forcelist={500, 502, 503, 504},
            allowed_methods={"GET"}, raise_on_status=False,
        ))
        self.session.mount("https://", adapter)

    def _get(self, url: str, params: Optional[Dict] = None) -> requests.Response:
        """GET with throttle and retry on rate-limit / transport errors."""
        for attempt in range(1, 6):
            elapsed = time.monotonic() - self._last
            if elapsed < PAUSE:
                time.sleep(PAUSE - elapsed)
            self._last = time.monotonic()
            try:
                r = self.session.get(url, params=params, stream=False, timeout=(10, 60))
            except (requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as exc:
                if attempt == 5: raise
                wait = 2.0 * (2 ** (attempt - 1))
                log.warning("Transport err attempt %d: %s – retry in %.0fs", attempt, exc, wait)
                time.sleep(wait); continue

            if r.status_code in (429, 403) and "rate limit" in r.text.lower():
                wait = int(r.headers.get("Retry-After", 60))
                log.warning("Rate-limited – waiting %ds", wait)
                time.sleep(wait); continue

            r.raise_for_status()
            return r
        raise requests.exceptions.RetryError(f"All retries exhausted: {url}")

    # ── High-level API wrappers ───────────────────────────────────────────────

    def repo(self, o: str, n: str) -> Dict:
        return self._get(f"{API}/repos/{o}/{n}").json()

    def _paged_count(self, url: str, params: Dict) -> int:
        """Count items via last-page number from GitHub's Link header."""
        try:
            r    = self._get(url, params)
            link = r.headers.get("Link", "")
            m    = re.search(r'page=(\d+)>; rel="last"', link)
            return int(m.group(1)) if m else len(r.json())
        except requests.HTTPError:
            return 0

    def commits_count(self, o: str, n: str) -> int:
        return self._paged_count(f"{API}/repos/{o}/{n}/commits", {"per_page": 1})

    def contributors_count(self, o: str, n: str) -> int:
        return self._paged_count(f"{API}/repos/{o}/{n}/contributors",
                                 {"per_page": 1, "anon": "true"})

    def tree(self, o: str, n: str, sha: str) -> List[Dict]:
        """Single-level (non-recursive) tree listing."""
        return self._get(f"{API}/repos/{o}/{n}/git/trees/{sha}",
                         {"recursive": "0"}).json().get("tree", [])

    def blob(self, o: str, n: str, sha: str) -> bytes:
        return base64.b64decode(
            self._get(f"{API}/repos/{o}/{n}/git/blobs/{sha}").json()["content"]
        )

    def raw_url(self, url: str) -> bytes:
        return self._get(url).content

    def rate_limit(self) -> Dict:
        return self._get(f"{API}/rate_limit").json()

# ══════════════════════════════════════════════════════════════════════════════
#  FILTER VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def passes_filters(meta: Dict, commits: int, contribs: int) -> Tuple[bool, str]:
    """Return (True, '') or (False, reason) for the first failed filter."""
    def dt(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    checks = [
        (meta.get("fork",     True) != FILTER["fork"],     "is a fork"),
        (meta.get("archived", True) != FILTER["archived"], "is archived"),
        (meta.get("private",  True) != FILTER["private"],  "is private"),
        (meta.get("stargazers_count", 0) < FILTER["stars_min"],
            f"stars {meta.get('stargazers_count',0)} < {FILTER['stars_min']}"),
        (meta.get("forks_count", 0)      < FILTER["forks_min"],
            f"forks {meta.get('forks_count',0)} < {FILTER['forks_min']}"),
        (meta.get("open_issues_count", 0) <= FILTER["open_issues_min"],
            f"issues {meta.get('open_issues_count',0)} <= {FILTER['open_issues_min']}"),
        (meta.get("size", 0)             < FILTER["size_min_kb"],
            f"size {meta.get('size',0)}kB < {FILTER['size_min_kb']}kB"),
        (commits  < FILTER["commits_min"],
            f"commits {commits} < {FILTER['commits_min']}"),
        (contribs < FILTER["contributors_min"],
            f"contributors {contribs} < {FILTER['contributors_min']}"),
    ]
    for cond, reason in checks:
        if cond: return False, reason

    if (c := meta.get("created_at")) and dt(c) >= FILTER["created_before"]:
        return False, f"created {dt(c).date()} >= {FILTER['created_before'].date()}"
    if (p := meta.get("pushed_at"))  and dt(p) <= FILTER["pushed_after"]:
        return False, f"pushed {dt(p).date()} <= {FILTER['pushed_after'].date()}"

    return True, ""

# ══════════════════════════════════════════════════════════════════════════════
#  BFS TREE TRAVERSAL  (non-recursive, capped)
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  KEYWORD MATCHING
# ══════════════════════════════════════════════════════════════════════════════

# Pre-compiled keyword pattern for fast content search.
# Matches any keyword as a case-insensitive substring.
_KW_PATTERN = re.compile(
    "|".join(re.escape(kw) for kw in KEYWORDS),
    re.IGNORECASE,
)

def content_has_keyword(data: bytes) -> bool:
    """
    Return True if the file bytes contain at least one keyword.

    Decodes as UTF-8 (invalid bytes replaced) so any encoding quirk
    never raises – it may miss a keyword at a broken codepoint boundary,
    but that is an acceptable trade-off for robustness.
    """
    text = data.decode("utf-8", errors="replace")
    return bool(_KW_PATTERN.search(text))


def path_has_keyword(path: str) -> bool:
    """
    Fallback for binary files: check whether the file's *directory path*
    (not the filename itself) contains a keyword.

    Example:  "docs/architecture/diagram.pdf"  →  True  ("architecture" in dir)
              "assets/logo.png"                →  False
    """
    dir_part = "/".join(path.lower().split("/")[:-1])  # parent dirs, not filename
    return bool(_KW_PATTERN.search(dir_part))


# ══════════════════════════════════════════════════════════════════════════════
#  BFS TREE TRAVERSAL  (non-recursive, capped)
# ══════════════════════════════════════════════════════════════════════════════

def collect_candidates(client: GitHubClient, owner: str, repo: str,
                       branch: str) -> List[Dict]:
    """
    Iterative BFS up to MAX_DEPTH levels and MAX_TREE_CALLS API calls.

    Collects every file whose extension is in EXTENSIONS – WITHOUT any
    keyword check on the path or filename.  Keyword matching happens
    later, against the actual file content once the blob is downloaded.

    Stops early once MAX_CANDIDATES files are collected so we do not
    fetch thousands of blobs for a single large repo.
    """
    candidates: List[Dict] = []
    queue: List[Tuple[str, int, str]] = [(branch, 0, "")]
    seen:  set = set()
    calls: int = 0

    while queue:
        if calls >= MAX_TREE_CALLS:
            log.info("  [BFS] tree-call cap %d reached, stopping.", calls); break
        if len(candidates) >= MAX_CANDIDATES:
            log.info("  [BFS] candidate cap %d reached, stopping.", MAX_CANDIDATES); break

        sha, depth, prefix = queue.pop(0)
        if sha in seen: continue
        seen.add(sha)

        log.info("  [BFS] d=%d q=%d calls=%d/%d candidates=%d  '%s'",
                 depth, len(queue), calls, MAX_TREE_CALLS,
                 len(candidates), prefix or "<root>")

        try:
            items = client.tree(owner, repo, sha); calls += 1
        except Exception as exc:
            log.warning("  tree '%s' failed: %s", prefix or "<root>", exc)
            calls += 1; continue

        for item in items:
            full = f"{prefix}/{item['path']}".lstrip("/") if prefix else item["path"]
            if item["type"] == "tree":
                if depth + 1 < MAX_DEPTH:
                    queue.append((item["sha"], depth + 1, full))
            elif item["type"] == "blob":
                if Path(item["path"]).suffix.lower() in EXTENSIONS:
                    item["_path"]  = full
                    item["_depth"] = depth
                    candidates.append(item)
                    if len(candidates) >= MAX_CANDIDATES: break

    log.info("  [BFS] done – %d tree calls, %d candidate file(s) collected.",
             calls, len(candidates))
    return candidates

# ══════════════════════════════════════════════════════════════════════════════
#  IMAGE EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

# ---------------------------------------------------------------------------
# Regex patterns for image references in text-based formats.
# Every pattern uses exactly TWO capture groups:
#   group 1 = surrounding context / prefix  (kept unchanged when rewriting)
#   group 2 = the path/URL  (the part we compare and replace)
# ---------------------------------------------------------------------------
_IMG_PATTERNS: List[Tuple[re.Pattern, int]] = [
    # Markdown inline image:  ![alt](path  or  ![alt](path "title")
    (re.compile(r'(!\[.*?\]\()([^)#\s"\']+)', re.S), 2),
    # Markdown reference definition:  [label]: path
    (re.compile(r'(^\[.*?\]:\s*)(\S+)', re.M), 2),
    # HTML/SVG <img src="path">
    (re.compile(r'(<img\b[^>]*?\bsrc=["\'])([^"\'#]+)', re.I), 2),
    # SVG/XML <image href="path">  or  xlink:href="path"
    (re.compile(r'(<image\b[^>]*?(?:xlink:)?href=["\'])([^"\'#]+)', re.I), 2),
    # AsciiDoc  image::path[]  or  image:path[]
    (re.compile(r'(image:{1,2})([^\[\s#]+)(\[)', re.I), 2),
    # RST  .. image:: path  /  .. figure:: path
    (re.compile(r'(\.\.\s+(?:image|figure)::\s+)(\S+)', re.I), 2),
    # DrawIO / XML style:  image;image=path;
    (re.compile(r'(image;image=)([^;"\'\s>#]+)', re.I), 2),
]

# Image file extensions considered downloadable (defined in CONFIG above)


def _resolve_raw_url(ref: str, doc_dir: str, owner: str, repo: str, branch: str
                     ) -> Optional[str]:
    """
    Convert an image reference found in a document to a raw.githubusercontent.com
    download URL.  Returns None if the reference should be skipped.

    Handles:
    • Relative paths:           images/comp.png  →  raw.../branch/dir/images/comp.png
    • Root-absolute paths:      /assets/logo.svg →  raw.../branch/assets/logo.svg
    • GitHub blob URLs:         github.com/.../blob/main/img.png  →  raw URL
    • Already-raw URLs:         raw.githubusercontent.com/...      →  kept as-is
    • Query strings / fragments: img.png?raw=true  →  stripped before ext check
    • External non-GitHub URLs: rejected (None)
    """
    # ── strip query-string and fragment for extension detection ────
    ref_for_ext = ref.split("?")[0].split("#")[0]
    ext = Path(ref_for_ext).suffix.lower()

    # ── external / absolute HTTP(S) references ─────────────────────
    if re.match(r'^https?://', ref):
        if ext not in IMG_EXT:
            log.debug("      skip non-image ext external ref: %r", ref)
            return None

        # GitHub blob URL → must be converted to raw URL
        if "github.com" in ref and "/blob/" in ref:
            raw = (ref.replace("github.com/", "raw.githubusercontent.com/")
                      .replace("/blob/", "/")
                      .split("?")[0].split("#")[0])
            log.debug("      blob→raw: %s", raw)
            return raw

        # Already a raw.githubusercontent.com URL
        if "raw.githubusercontent.com" in ref:
            return ref.split("?")[0].split("#")[0]

        # Any other external host: skip (we can't follow arbitrary CDNs)
        log.debug("      skip external non-GitHub ref: %r", ref)
        return None

    # ── local reference (relative or absolute-from-repo-root) ──────
    if ext not in IMG_EXT:
        log.debug("      skip non-image ext local ref: %r  (ext=%r)", ref, ext)
        return None

    # Normalize: strip fragment + query string, URL-decode percent-encoding
    clean = unquote(ref.split("?")[0].split("#")[0].strip())
    if not clean:
        return None

    # Resolve path segments relative to the document's directory in the repo
    base = doc_dir if not clean.startswith("/") else ""
    parts: List[str] = []
    for seg in f"{base}/{clean}".split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg and seg != ".":
            parts.append(seg)

    if not parts:
        log.debug("      could not resolve path for ref: %r", ref)
        return None

    return f"{RAW}/{owner}/{repo}/{branch}/{'/'.join(parts)}"


def extract_image_refs(content: bytes, doc_path: str,
                       owner: str, repo: str, branch: str
                       ) -> List[Tuple[str, str, str]]:
    """
    Scan a document for embedded local image references.

    Returns a deduplicated list of 3-tuples::

        (original_ref, safe_local_filename, raw_download_url)

    ``original_ref``       – the exact string as it appears in the document
                             (needed for precise search-and-replace later).
    ``safe_local_filename``– collision-safe basename for the local copy
                             (e.g.  "logo.png",  "logo_2.png").
    ``raw_download_url``   – URL to fetch the image bytes from GitHub.

    Only local relative / root-absolute references to known image extensions
    are returned; external images and non-image extensions are skipped.
    """
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return []

    doc_dir = "/".join(doc_path.split("/")[:-1])  # parent dir inside repo

    # ── pass 1: collect unique refs and resolve to raw URLs ─────────
    seen_refs: Dict[str, str] = {}    # original_ref → raw_url (dedup by ref)
    rejected  = 0

    for pattern, path_grp in _IMG_PATTERNS:          # use path_grp, not hardcoded 2
        for m in pattern.finditer(text):
            ref = m.group(path_grp).strip()
            if not ref or ref in seen_refs:
                continue
            raw_url = _resolve_raw_url(ref, doc_dir, owner, repo, branch)
            if raw_url:
                seen_refs[ref] = raw_url
            else:
                rejected += 1

    log.info("    img-scan: %d ref(s) resolved, %d rejected  [%s]",
             len(seen_refs), rejected, doc_path)

    for ref, url in seen_refs.items():
        log.debug("      ✓ %r  →  %s", ref, url)

    if not seen_refs:
        return []

    # ── pass 2: assign collision-safe local filenames ───────────────
    used_names: Dict[str, int] = {}
    result: List[Tuple[str, str, str]] = []

    for ref, raw_url in seen_refs.items():
        basename = Path(unquote(urlparse(raw_url).path)).name or "image"
        stem, ext = Path(basename).stem, Path(basename).suffix.lower()
        if ext not in IMG_EXT:
            ext = ".bin"

        if basename not in used_names:
            used_names[basename] = 1
            local_name = basename
        else:
            used_names[basename] += 1
            local_name = f"{stem}_{used_names[basename]}{ext}"

        result.append((ref, local_name, raw_url))

    return result

# ══════════════════════════════════════════════════════════════════════════════
#  FILE DOWNLOAD HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def safe_path(path: str) -> str:
    """Strip characters that are illegal in Windows / macOS filenames."""
    return re.sub(r'[<>:"|?*]', "_", path)


def _rewrite_image_refs(content: bytes, ref_map: Dict[str, str]) -> bytes:
    """
    Rewrite image references inside a text document in-place.

    Uses the same regex patterns as ``extract_image_refs`` so that only
    real image-path occurrences are replaced – never arbitrary strings that
    happen to appear elsewhere in the file.

    :param content: Original file bytes (UTF-8 text).
    :param ref_map: {original_ref_string → new_relative_filename}
    :return:        Modified bytes, re-encoded as UTF-8.
    """
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return content

    for pattern, path_grp in _IMG_PATTERNS:
        def _sub(m: re.Match, _map: Dict = ref_map, _g: int = path_grp) -> str:
            old_ref = m.group(_g)
            if old_ref not in _map:
                return m.group(0)          # no change
            # Reconstruct the full match with only the path group replaced
            full    = m.group(0)
            g_start = m.start(_g) - m.start()
            g_end   = m.end(_g)   - m.start()
            return full[:g_start] + _map[old_ref] + full[g_end:]

        text = pattern.sub(_sub, text)

    return text.encode("utf-8")


def download_document(client: GitHubClient, owner: str, repo: str,
                      item: Dict, repo_dir: Path, branch: str
                      ) -> Tuple[Optional[Path], List[Path]]:
    """
    Download one matched document and all images it references.

    ═══════════════════════════════════════════════════════════════════
    PATH STRATEGY
    ═══════════════════════════════════════════════════════════════════

    CASE A – file contains NO image references
    ──────────────────────────────────────────
    The GitHub path is mirrored 1-to-1 under *repo_dir*:

        GitHub : docs/architecture/overview.md
        Local  : {repo_dir}/docs/architecture/overview.md

    CASE B – file contains image references
    ────────────────────────────────────────
    A sibling folder named  ``{file_stem}_images``  is created at the
    same level as the file's mirrored location. BOTH the document AND
    all its images are saved inside that folder (flat – no sub-dirs).
    Image references inside the document are rewritten to plain
    basenames so every file is directly openable with one click:

        GitHub doc  : docs/architecture/system-design.md
        GitHub imgs : docs/architecture/img/component.png   (ref: img/component.png)
                      docs/shared/logo.png                   (ref: ../shared/logo.png)

        Local folder: {repo_dir}/docs/architecture/system-design_images/
                        system-design.md    ← image refs rewritten to plain names
                        component.png       ← ref in doc: component.png  ✓
                        logo.png            ← ref in doc: logo.png       ✓

    Collisions (two different images with the same basename) are resolved
    automatically:  ``logo.png`` → ``logo_2.png``, ``logo_3.png``, …

    :return: (local_doc_path, [local_image_paths])  –  doc path is None on failure.
    """
    gh_path   = item["_path"]            # e.g. "docs/arc/overview.md"
    file_name = Path(gh_path).name
    file_ext  = Path(gh_path).suffix.lower()

    # ── 1. Download document bytes via git blob API ────────────────
    try:
        data = client.blob(owner, repo, item["sha"])
    except Exception as exc:
        log.warning("  ✘ blob %s: %s", gh_path, exc)
        return None, []

    # ── 2. Keyword check ───────────────────────────────────────────
    # For text-based formats: search the decoded content for keywords.
    # For binary formats (PDF, DOCX, images …): content cannot be decoded
    # as text, so fall back to checking the parent directory path.
    if file_ext in CONTENT_SEARCHABLE_EXT:
        if not content_has_keyword(data):
            log.debug("  ✗ no keyword in content – skipped: %s", gh_path)
            return None, []
        log.debug("  keyword found in content: %s", gh_path)
    elif file_ext in BINARY_EXT:
        # Content is binary; use parent directory path as fallback heuristic.
        if not path_has_keyword(gh_path):
            log.debug("  ✗ binary, no keyword in dir path – skipped: %s", gh_path)
            return None, []
        log.debug("  keyword found in dir path (binary file): %s", gh_path)
    # Any remaining extension not in either set: no check, always include.

    # ── 3. Extract image references (text formats only) ────────────
    image_triples: List[Tuple[str, str, str]] = []
    if file_ext in TEXT_EXT:
        try:
            image_triples = extract_image_refs(data, gh_path, owner, repo, branch)
        except Exception as exc:
            log.warning("  img-extract failed for %s: %s", gh_path, exc)

    # ══ CASE A: no images → mirror GitHub path 1-to-1 ═════════════
    if not image_triples:
        local = repo_dir / safe_path(gh_path)
        if not local.exists():
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            log.info("  ✔ %s  (%d B)  [mirrored]", gh_path, len(data))
        return local, []

    # ══ CASE B: images present → dedicated {stem}_images/ folder ══

    # Folder sits at the same level the file would occupy when mirrored,
    # named  {file_stem}_images
    #
    #   GitHub: docs/architecture/system-design.md
    #   Folder: {repo_dir}/docs/architecture/system-design_images/
    #
    file_stem     = Path(gh_path).stem
    github_parent = "/".join(gh_path.split("/")[:-1])   # "" for root files

    if github_parent:
        img_folder = repo_dir / safe_path(github_parent) / f"{safe_path(file_stem)}_images"
    else:
        img_folder = repo_dir / f"{safe_path(file_stem)}_images"

    img_folder.mkdir(parents=True, exist_ok=True)
    log.info("  ✔ %s  (%d B)  → folder %s/",
             gh_path, len(data), img_folder.relative_to(repo_dir))

    # ── 4. Download each referenced image ─────────────────────────
    # ref_map: original ref string → new bare filename (for rewriting)
    ref_map:    Dict[str, str]  = {}
    img_locals: List[Path]      = []

    for orig_ref, local_name, raw_url in image_triples:
        dest = img_folder / local_name
        if dest.exists():
            # Already downloaded (another doc in same repo referenced same image)
            ref_map[orig_ref] = local_name
            img_locals.append(dest)
            continue
        try:
            img_bytes = client.raw_url(raw_url)
            dest.write_bytes(img_bytes)
            ref_map[orig_ref] = local_name
            img_locals.append(dest)
            log.info("    ↳ img  %-45s → %s  (%d B)",
                     orig_ref, local_name, len(img_bytes))
        except Exception as exc:
            log.warning("    ↳ img FAIL  %s  (%s): %s", orig_ref, raw_url, exc)
            # Keep original ref unchanged if download failed

    # ── 5. Rewrite image refs in document content ──────────────────
    if ref_map:
        data = _rewrite_image_refs(data, ref_map)
        log.info("    rewrote %d / %d image ref(s) in %s",
                 len(ref_map), len(image_triples), file_name)
    else:
        log.info("    no images resolved – saved with original refs")

    # ── 5. Save document inside the images folder ──────────────────
    doc_local = img_folder / file_name
    doc_local.write_bytes(data)

    return doc_local, img_locals

# ══════════════════════════════════════════════════════════════════════════════
#  HTML INDEX + JSON MANIFEST
# ══════════════════════════════════════════════════════════════════════════════

def _e(s: str) -> str:
    """HTML-escape a string."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))

def write_index(results: List[Dict], out: Path) -> Path:
    """
    Generate a clickable HTML overview of all downloaded files.

    Paths in href attributes always use forward slashes so the index opens
    correctly on both Windows and POSIX systems.
    """
    def _rel_href(abs_path: str) -> str:
        """Return forward-slash relative path from *out* to *abs_path*."""
        return Path(abs_path).relative_to(out).as_posix()

    rows = ""
    for r in results:
        doc_href = _rel_href(r["local"])

        # Label: show GitHub path; link: local file (in _images/ folder if applicable)
        in_img_folder = "_images" in r["local"]
        folder_badge  = (' <span title="saved in _images folder" '
                         'style="font-size:.7rem;color:#888">[📁_images]</span>'
                         if in_img_folder else "")

        imgs = ""
        for img in r.get("images", []):
            irel = _rel_href(img)
            if Path(img).suffix.lower() in IMG_EXT:
                imgs += (f'<a href="{_e(irel)}" target="_blank">'
                         f'<img src="{_e(irel)}" style="max-height:55px;'
                         f'max-width:90px;margin:2px;border:1px solid #ccc;'
                         f'border-radius:3px" loading="lazy"></a>')
            else:
                imgs += (f'<a href="{_e(irel)}" target="_blank">'
                         f'📎{_e(Path(img).name)}</a> ')

        rows += (
            f'<tr><td><code class="r">{_e(r["repo"])}</code></td>'
            f'<td><a href="{_e(doc_href)}" target="_blank">{_e(r["file"])}</a>'
            f'{folder_badge}</td>'
            f'<td><a href="{_e(r["gh_url"])}" target="_blank">GitHub↗</a></td>'
            f'<td class="n">⭐{r["stars"]}</td><td class="n">🍴{r["forks"]}</td>'
            f'<td class="n">👥{r["contribs"]}</td>'
            f'<td class="imgs">{imgs or "—"}</td></tr>\n'
        )

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>GitHub File Miner v3</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font:13px system-ui,sans-serif;background:#f4f6f9;padding:20px}}
h1{{font-size:1.35rem;margin-bottom:4px;color:#1a1a2e}}
.meta{{font-size:.8rem;color:#666;margin-bottom:14px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
       overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.12)}}
th{{background:#1a1a2e;color:#fff;padding:8px 10px;text-align:left;font-size:.8rem}}
td{{padding:7px 10px;border-bottom:1px solid #eee;font-size:.78rem;vertical-align:top}}
tr:last-child td{{border:none}}tr:hover td{{background:#f0f4ff}}
code.r{{background:#e8eaf6;border-radius:3px;padding:1px 5px;font-size:.72rem}}
a{{color:#1565c0;text-decoration:none}}a:hover{{text-decoration:underline}}
.n{{text-align:right;white-space:nowrap}}.imgs{{max-width:250px}}
</style></head><body>
<h1>🔍 GitHub File Miner v3 – Results</h1>
<p class="meta">Generated: {datetime.now():%Y-%m-%d %H:%M:%S} &nbsp;|&nbsp;
Files: {len(results)} &nbsp;|&nbsp; Keywords: {_e(', '.join(KEYWORDS))}</p>
<table><thead><tr><th>Repo</th><th>File</th><th>Source</th>
<th>Stars</th><th>Forks</th><th>Contribs</th><th>Images</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""

    p = out / "index.html"
    p.write_text(html, encoding="utf-8")
    log.info("Index → %s", p)
    return p

def write_manifest(results: List[Dict], out: Path) -> None:
    (out / "manifest.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

# ══════════════════════════════════════════════════════════════════════════════
#  SINGLE-REPO PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def process_repo(client: GitHubClient, owner: str, name: str,
                 out: Path) -> List[Dict]:
    """
    Full pipeline for one repository:
      1. Fetch metadata and apply quality filters.
      2. BFS-traverse to collect all extension-matching candidate files.
      3. For each candidate: download blob, check content for keywords,
         save only when a keyword is found (binary files: check dir path).
      4. Stop once MAX_FILES_REPO confirmed files have been downloaded.
    """
    full = f"{owner}/{name}"
    log.info("━━━ %s ━━━", full)

    try:
        meta = client.repo(owner, name)
    except requests.HTTPError as exc:
        log.warning("Cannot fetch %s: %s", full, exc); return []

    commits  = client.commits_count(owner, name)
    contribs = client.contributors_count(owner, name)
    log.info("  commits=%d contribs=%d stars=%d forks=%d size=%dkB",
             commits, contribs,
             meta.get("stargazers_count", 0), meta.get("forks_count", 0),
             meta.get("size", 0))

    ok, reason = passes_filters(meta, commits, contribs)
    if not ok:
        log.info("  ✗ skipped: %s", reason); return []
    log.info("  ✓ passes filters")

    branch     = meta.get("default_branch", "main")
    candidates = collect_candidates(client, owner, name, branch)
    if not candidates:
        log.info("  no candidates found (no matching extensions in repo)")
        return []

    # Sort shallowest / alphabetically for deterministic output
    candidates.sort(key=lambda f: (f["_depth"], f["_path"]))

    # Mirror owner/repo structure: out/{owner}/{name}/
    repo_dir = out / safe_path(owner) / safe_path(name)
    repo_dir.mkdir(parents=True, exist_ok=True)

    results:   List[Dict] = []
    checked:   int        = 0
    discarded: int        = 0

    for item in candidates:
        if len(results) >= MAX_FILES_REPO:
            log.info("  MAX_FILES_REPO=%d reached – stopping.", MAX_FILES_REPO)
            break

        checked += 1
        local_doc, local_imgs = download_document(
            client, owner, name, item, repo_dir, branch
        )

        if not local_doc:
            # None means keyword not found (or download error) – skip silently
            discarded += 1
            continue

        gh_url = (f"https://github.com/{owner}/{name}"
                  f"/blob/{branch}/{item['_path']}")

        results.append(dict(
            repo     = full,
            file     = item["_path"],
            gh_url   = gh_url,
            local    = str(local_doc),
            stars    = meta.get("stargazers_count", 0),
            forks    = meta.get("forks_count", 0),
            contribs = contribs,
            commits  = commits,
            images   = [str(p) for p in local_imgs],
        ))

    log.info("  checked=%d  confirmed=%d  discarded(no keyword)=%d",
             checked, len(results), discarded)
    return results

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES: TOKEN + CSV
# ══════════════════════════════════════════════════════════════════════════════

def load_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok: return tok
    f = Path(".github_token")
    if f.exists() and (tok := f.read_text(encoding="utf-8").strip()): return tok
    sys.exit("❌  Set GITHUB_TOKEN env var  OR  create file .github_token")

def load_csv(path: Path) -> List[Tuple[str, str]]:
    repos: List[Tuple[str, str]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        sample = fh.read(2048); fh.seek(0)
        try:    dialect = csv.Sniffer().sniff(sample)
        except: dialect = csv.excel
        for row in csv.reader(fh, dialect):
            if not row: continue
            first = row[0].strip()
            if first.startswith("#") or first.lower() in ("repo", "repository", "name"):
                continue
            if "/" in first:
                o, n = first.split("/", 1); repos.append((o.strip(), n.strip()))
            elif len(row) >= 2:
                repos.append((first, row[1].strip()))
            else:
                log.warning("Skipping row: %s", row)
    log.info("Loaded %d repos from %s", len(repos), path)
    return repos

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main(args: argparse.Namespace) -> None:
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(out / "miner.log", encoding="utf-8"),
        ],
    )
    log.info("═" * 65)
    log.info("GitHub File Miner v2  –  %s", datetime.now().isoformat(timespec="seconds"))
    log.info("Output: %s  |  repos=%d  files=%d  depth=%d",
             out.resolve(), MAX_REPOS, MAX_FILES_REPO, MAX_DEPTH)
    log.info("═" * 65)

    token  = load_token()
    client = GitHubClient(token)

    # Show remaining API quota
    try:
        rl    = client.rate_limit().get("resources", {}).get("core", {})
        reset = datetime.fromtimestamp(rl.get("reset", 0)).strftime("%H:%M:%S")
        log.info("Rate limit: %s/%s (resets %s)",
                 rl.get("remaining", "?"), rl.get("limit", "?"), reset)
    except Exception:
        pass

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"❌  CSV not found: {csv_path}")
    repo_list = load_csv(csv_path)
    if not repo_list:
        sys.exit("❌  No repos in CSV")

    all_results: List[Dict] = []
    processed = 0

    try:
        for owner, name in repo_list:
            if processed >= MAX_REPOS:
                log.info("MAX_REPOS=%d reached.", MAX_REPOS); break
            res = process_repo(client, owner, name, out)
            if res:
                all_results.extend(res)
                processed += 1
    except KeyboardInterrupt:
        log.warning("⚠  Interrupted after %d repos / %d files – writing partial results …",
                    processed, len(all_results))

    log.info("─" * 65)
    log.info("Done: %d repos, %d files", processed, len(all_results))

    if all_results:
        idx = write_index(all_results, out)
        write_manifest(all_results, out)
        log.info("Open: %s", idx.resolve())
    else:
        log.warning("No files downloaded. Relax filters or check CSV.")
    log.info("═" * 65)


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            GitHub File Miner v2
            Downloads architecture/design documents from GitHub repos.
            Token: GITHUB_TOKEN env var  OR  file .github_token"""))
    p.add_argument("csv",           metavar="REPOS_CSV",
                   help="CSV with repo slugs (owner/repo, one per line)")
    p.add_argument("--output-dir",  default=DOWNLOAD_DIR, metavar="DIR",
                   help=f"Output directory (default: {DOWNLOAD_DIR})")
    p.add_argument("--debug",       action="store_true", help="Enable DEBUG logging")
    main(p.parse_args())
