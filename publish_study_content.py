#!/usr/bin/env python3
"""Publish Study Hub content to Firebase (Firestore manifest + Storage files).

Mirrors `publish_science_page_bundles.py` (AR packs) but for Study Hub:
walks the content source tree (default: the directory this script lives in),
which holds `class{6..10}/<subject>/<chapter>/<feature>.json`. The content lives
OUTSIDE the app repo on purpose — no study content ships in the app build.
It uploads each chapter-feature JSON to Firebase Storage, and writes a
per-class manifest document to Firestore. Per chapter-feature it records a
content hash and an integer version that is only bumped when the hash changes
(so the mobile client downloads only what actually changed).

Storage layout:  study-content-v1/{grade}/{subject}/{chapterSlug}/{feature}.json
Firestore:       study_content_v1/{grade}

Usage:
    python3 tooling/publish_study_content.py                # all classes 6-10
    python3 tooling/publish_study_content.py --grade 08     # one class
    python3 tooling/publish_study_content.py --grade 06 --grade 07
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


PROJECT_ID = "akceroedu-99491"
BUCKET = "akceroedu-99491.firebasestorage.app"
COLLECTION = "study_content_v1"
STORAGE_ROOT = "study-content-v1"

# Source filename -> canonical feature id understood by the Flutter client
# (see StudyFeatureKindX.fromId in lib/models/study_content_pack.dart).
FEATURE_BY_FILENAME = {
    "summary.json": "summary",
    "summary-family.json": "summary",
    "notes.json": "notes",
    "practice-questions.json": "practice",
    "practice.json": "practice",
    "flashcards.json": "flashcards",
}

SUBJECTS = {"math", "science", "social"}


def run(cmd: list[str], capture: bool = False) -> str:
    proc = subprocess.run(cmd, check=True, text=True, capture_output=capture)
    return proc.stdout if capture else ""


def encode_firestore_value(value):
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [encode_firestore_value(v) for v in value]}}
    if isinstance(value, dict):
        return {
            "mapValue": {
                "fields": {k: encode_firestore_value(v) for k, v in value.items()}
            }
        }
    if value is None:
        return {"nullValue": None}
    raise TypeError(f"Unsupported Firestore type: {type(value)!r}")


def decode_firestore_value(value):
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "booleanValue" in value:
        return value["booleanValue"]
    if "nullValue" in value:
        return None
    if "arrayValue" in value:
        return [decode_firestore_value(v) for v in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {
            k: decode_firestore_value(v)
            for k, v in value["mapValue"].get("fields", {}).items()
        }
    raise ValueError(f"Unsupported Firestore value: {value}")


def get_access_token() -> str:
    return run(["gcloud", "auth", "print-access-token"], capture=True).strip()


def firestore_get_doc(token: str, doc_id: str) -> dict:
    url = (
        f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/"
        f"databases/(default)/documents/{COLLECTION}/{doc_id}"
    )
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise
    return {
        k: decode_firestore_value(v)
        for k, v in payload.get("fields", {}).items()
    }


def firestore_patch_doc(token: str, doc_id: str, payload: dict) -> None:
    params = urllib.parse.urlencode(
        [
            ("updateMask.fieldPaths", field)
            for field in ("version", "grade", "isActive", "subjects")
        ]
    )
    url = (
        f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/"
        f"databases/(default)/documents/{COLLECTION}/{doc_id}?{params}"
    )
    body = json.dumps(
        {
            "fields": {
                "version": encode_firestore_value(payload["version"]),
                "grade": encode_firestore_value(payload["grade"]),
                "isActive": encode_firestore_value(payload["isActive"]),
                "subjects": encode_firestore_value(payload["subjects"]),
            }
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request) as response:
        response.read()


def upload_file(src: Path, dest: str) -> None:
    print(f"UPLOAD {src} -> {dest}")
    run(["gcloud", "storage", "cp", str(src), dest])


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def slugify(value: str) -> str:
    normalized = value.lower().strip()
    normalized = re.sub(r"[^\w\s-]+", "", normalized)
    normalized = re.sub(r"[_\s-]+", "-", normalized)
    return normalized.strip("-")


def read_chapter_info(path: Path) -> tuple[str, int | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "", None
    info = data.get("chapter_info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return "", None
    name = str(info.get("chapter_name") or "").strip()
    number_raw = info.get("chapter_number")
    number = number_raw if isinstance(number_raw, int) else None
    if number is None:
        try:
            number = int(str(number_raw))
        except (TypeError, ValueError):
            number = None
    return name, number


def existing_feature_version(prev_doc: dict, subject: str, slug: str, feature: str):
    """Return (version, hash) for a previously published feature, or (0, '')."""
    try:
        feat = prev_doc["subjects"][subject]["chapters"][slug]["features"][feature]
        return int(feat.get("version") or 0), str(feat.get("hash") or "")
    except (KeyError, TypeError):
        return 0, ""


def build_manifest_for_grade(
    source_root: Path, grade: str, prev_doc: dict
) -> tuple[dict, list[tuple[Path, str]], bool]:
    """Returns (subjects_manifest, uploads, changed)."""
    class_dir = source_root / f"class{int(grade)}"
    if not class_dir.exists():
        raise FileNotFoundError(class_dir)

    subjects: dict[str, dict] = {}
    uploads: list[tuple[Path, str]] = []
    changed = False

    for subject_dir in sorted(p for p in class_dir.iterdir() if p.is_dir()):
        subject = subject_dir.name.strip().lower()
        if subject not in SUBJECTS:
            continue
        chapters: dict[str, dict] = {}

        for chapter_dir in sorted(p for p in subject_dir.iterdir() if p.is_dir()):
            slug = slugify(chapter_dir.name)
            if not slug:
                continue
            features: dict[str, dict] = {}
            chapter_name = ""
            chapter_number = None

            for feature_file in sorted(chapter_dir.glob("*.json")):
                feature = FEATURE_BY_FILENAME.get(feature_file.name.lower())
                if feature is None:
                    continue
                if not chapter_name:
                    chapter_name, chapter_number = read_chapter_info(feature_file)

                file_hash = sha256_of(feature_file)
                prev_version, prev_hash = existing_feature_version(
                    prev_doc, subject, slug, feature
                )
                if prev_hash == file_hash and prev_version > 0:
                    version = prev_version
                else:
                    version = prev_version + 1 if prev_version > 0 else 1
                    changed = True

                dest_path = f"{STORAGE_ROOT}/{grade}/{subject}/{slug}/{feature}.json"
                uploads.append((feature_file, dest_path))
                features[feature] = {
                    "path": dest_path,
                    "version": version,
                    "hash": file_hash,
                }

            if not features:
                continue
            chapters[slug] = {
                "chapterName": chapter_name or chapter_dir.name,
                "chapterNumber": chapter_number if chapter_number is not None else 9999,
                "features": features,
            }

        if chapters:
            subjects[subject] = {"chapters": chapters}

    return subjects, uploads, changed


def publish_grade(token: str, source_root: Path, grade: str, dry_run: bool) -> None:
    doc_id = grade
    prev_doc = firestore_get_doc(token, doc_id)
    subjects, uploads, changed = build_manifest_for_grade(source_root, grade, prev_doc)

    if not subjects:
        print(f"SKIP grade {grade}: no content found")
        return

    prev_version = int(prev_doc.get("version") or 0)
    # Bump the class-level version only when at least one feature changed,
    # or when there was no previous publish.
    version = (prev_version + 1) if (changed or prev_version == 0) else prev_version

    chapter_count = sum(len(s["chapters"]) for s in subjects.values())
    print(
        f"GRADE {grade}: subjects={len(subjects)} chapters={chapter_count} "
        f"files={len(uploads)} version={version} (was {prev_version}) changed={changed}"
    )

    if dry_run:
        print(f"DRY-RUN grade {grade}: skipping uploads + Firestore write")
        return

    for src, dest in uploads:
        upload_file(src, f"gs://{BUCKET}/{dest}")

    firestore_patch_doc(
        token,
        doc_id,
        {
            "version": version,
            "grade": grade,
            "isActive": True,
            "subjects": subjects,
        },
    )
    print(f"DONE grade {grade}: Firestore {COLLECTION}/{doc_id} updated")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parent,
        help=(
            "Path to the study content source tree (the folder containing "
            "class6..class10). Defaults to the directory this script lives in "
            "(the content repo root)."
        ),
    )
    parser.add_argument(
        "--grade",
        action="append",
        help="Grade to publish (e.g. 08). Repeatable. Default: all of 06-10.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source_root: Path = args.source
    if not source_root.exists():
        print(f"Source not found: {source_root}", file=sys.stderr)
        return 1

    if args.grade:
        grades = [g.strip().zfill(2) for g in args.grade]
    else:
        grades = [str(g).zfill(2) for g in range(6, 11)]

    token = get_access_token()
    for grade in grades:
        try:
            publish_grade(token, source_root, grade, args.dry_run)
        except FileNotFoundError as exc:
            print(f"SKIP grade {grade}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
