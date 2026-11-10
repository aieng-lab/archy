# ARCHY: Dataset for bimodal informal software architecture descriptions

ARCHY is a dataset of textual and visual software architecture descriptions. It combines files from the code repositories of large open-source projects with architecture descriptions from a Web search and from the [PURE dataset](https://zenodo.org/records/1414117) of public requirements documents. Each record comes with metadata that traces it to its source and links documents to the files they embed, e.g., a Markdown file and the diagrams it shows.

## Contents

| | Records |
|---|---:|
| Code repositories of 62 open-source projects | 1,447 |
| Web search | 196 |
| PURE dataset | 68 |
| **Total** | **1,711** |

The records split into 1,079 documents and 632 chunks (files embedded in a document). By file type:

| Modality | File type | Records |
|---|---|---:|
| Text | md | 752 |
| | rst | 46 |
| | pdf | 160 |
| | doc | 14 |
| Image | png | 658 |
| | jpg | 57 |
| | svg | 24 |

158 records are README files of projects, which we include as context. Five images are logos that are embedded in documents. All other records are architectural.

## Repository layout

```
data/
  md/<owner>/<repo>/...        files from the code repositories, mirroring the repository paths
  pdf/<name>/                  PDF documents and Web pages (converted to PDF)
  doc/<name>/                  Word documents
  image/<name>/                images from the Web search
scripts/
  md/filter21_3.py             miner for the code repositories
  md/process_md_folder.py      creates the metadata for data/md
  pdf/process_pdf_folder.py    creates the metadata for data/pdf
  doc/process_doc_folder.py    creates the metadata for data/doc
  image/process_image_folder.py  creates the metadata for data/image
  JSON-Schema/entity-schema.json  JSON Schema of the metadata
```

Each data file has a metadata record in the same folder, named `doc-<number>-<name>.json` for documents and `chunk-<number>-<name>.json` for chunks. If a document embeds images, the document and its images are stored together in a folder named `<name>_images`, and image links within the document point to these local files. All folder names are lowercase.

## Metadata

Each record is a JSON object that conforms to [`scripts/JSON-Schema/entity-schema.json`](scripts/JSON-Schema/entity-schema.json). Example:

```json
{
  "json_schema": "2020-12",
  "schema_version": "1.0",
  "entity_id": "doc-7920",
  "entity_source_path": "data/md/apple/swift/docs/arrays_images/Arrays.md",
  "entity_uri": null,
  "entity_role": "document",
  "parent_doc_id": null,
  "parent_doc_path": null,
  "children_chunk_ids": ["chunk-7921", "chunk-7922", "chunk-7923", "chunk-7924"],
  "entity_title": "The Swift Array Design",
  "entity_is_readme": false,
  "entity_is_architectural": true,
  "entity_source": "repository",
  "entity_modality": "bimodal",
  "mime_type": "text/markdown",
  "project_name": "apple/swift",
  "project_uri": "https://github.com/apple/swift.git",
  "issue_tracking_uri": "https://github.com/apple/swift/issues",
  "version_controlled_uri": "https://github.com/apple/swift/commits",
  "imported_at": "2026-06-03",
  "page_range": null,
  "line_range": null,
  "char_range": null,
  "section": null
}
```

| Field | Description |
|---|---|
| `json_schema`, `schema_version` | Version of JSON Schema (`2020-12`) and of the metadata schema (`1.0`). |
| `entity_id` | Unique ID of the record, e.g., `doc-7920` or `chunk-7921`. |
| `entity_source_path` | Path of the data file within this repository. |
| `entity_uri` | URI from which the record was retrieved. Set for records from the Web search and the PURE dataset; `null` for records from the code repositories. |
| `entity_role` | `document` (a self-contained file) or `chunk` (a file embedded in a document). |
| `parent_doc_id`, `parent_doc_path` | For chunks: ID and path of the document that embeds the chunk. |
| `children_chunk_ids` | For documents: IDs of the chunks the document embeds. |
| `entity_title` | Title of the record. For Markdown and reStructuredText documents, the title from the front matter or the first heading. |
| `entity_is_readme` | `true` if the record is a README of a project. |
| `entity_is_architectural` | `true` if the record describes architecture; `false` for READMEs and for logos embedded in documents. |
| `entity_source` | `repository` (code repository of a project), `web-search`, or `pure` (PURE dataset). |
| `entity_modality` | `text`, `image`, or `bimodal` (a document that embeds images). |
| `mime_type` | MIME type of the data file. |
| `project_name` | Project of the record, e.g., `apple/swift`. Optional for records from the Web search and the PURE dataset. |
| `project_uri`, `issue_tracking_uri`, `version_controlled_uri` | Links to the code repository, the issue tracker, and the commit history of the project. |
| `imported_at` | Date on which the record was imported. |
| `page_range`, `line_range`, `char_range`, `section` | Location of a chunk within its parent document. Reserved for chunks that are parts of larger documents; currently `null` for all records. |

For PURE documents, `entity_uri` points to the original source of the document where we could trace it, and to the PURE dataset otherwise.

## Loading the data

```python
import json
from pathlib import Path

records = [json.loads(p.read_text(encoding="utf-8")) for p in Path("data").glob("**/*.json")]
by_id = {r["entity_id"]: r for r in records}

# all architectural documents from the code repositories, with their embedded images
for doc in records:
    if doc["entity_role"] == "document" and doc["entity_source"] == "repository" and doc["entity_is_architectural"]:
        images = [by_id[c]["entity_source_path"] for c in doc["children_chunk_ids"] or []]
        print(doc["entity_title"], doc["entity_source_path"], images)
```

## Scripts

All scripts require Python 3.9 or newer.

**Mining the code repositories.** `scripts/md/filter21_3.py` reads repositories (`owner/repo`, one per line) from a CSV file, checks the inclusion criteria, and downloads all files of the accepted file types that contain one of the keywords, together with the images they embed. It requires `requests` and a GitHub token in the environment variable `GITHUB_TOKEN` or in a file `.github_token`.

```
python3 scripts/md/filter21_3.py repos.csv --output-dir <dir>
```

**Creating the metadata.** The `process_*_folder.py` scripts create the metadata records for new data files and skip files that already have a record. Run them from the repository root.

```
python3 scripts/md/process_md_folder.py --schema scripts/JSON-Schema/entity-schema.json --md-folder data/md [--apply]
python3 scripts/pdf/process_pdf_folder.py --pdf-folder data/pdf [--dry-run]
python3 scripts/doc/process_doc_folder.py --doc-folder data/doc [--dry-run]
python3 scripts/image/process_image_folder.py --image-folder data/image [--apply]
```

`process_md_folder.py` and `process_image_folder.py` only report what they would do unless `--apply` is given; the other two scripts write unless `--dry-run` is given. For `data/pdf`, `data/doc`, and `data/image`, new files are placed directly in the top-level folder; the script copies each file into its own subfolder, where it creates the record, after which the original file can be removed. `process_md_folder.py` treats a folder with exactly one Markdown or reStructuredText file and one or more images as a document with chunks, and sets the README and architecture flags based on the file name. The records for the Web search and the PURE dataset are completed manually afterwards, i.e., the title, the URI, the project, and the source `pure` for PURE documents.

