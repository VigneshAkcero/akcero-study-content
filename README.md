# AlphaBuddy — Study Hub Content

Source content for the **Study Hub** feature of the AlphaBuddy app (NCERT-aligned
notes, summaries, practice questions and flashcards for classes 6–10).

This repository is the **editable master copy** of the study content. It is kept
deliberately **outside the app codebase** — none of this content ships inside the
app binary. Instead it is published to Firebase, and the app downloads each
student's class content at runtime.

```
edit a JSON file here  →  run the publisher  →  Firebase (Firestore + Storage)  →  app downloads on next launch
```

No app rebuild or store release is needed to update content.

## Structure

```
class<6-10>/
  <subject>/                 # math | science | social
    <Chapter Name>/
      summary.json           # chapter summary (category + points)
      notes.json             # sectioned notes
      practice-questions.json# practice questions (+ answers / solutions)
      flashcards.json        # front/back flashcards
```

Each feature JSON carries a `chapter_info` block:

```json
{
  "chapter_info": { "class": 8, "subject": "Mathematics", "chapter_name": "A Square and A Cube", "chapter_number": 1 },
  "summary": [ { "category": "...", "points": ["...", "..."] } ]
}
```

## How it reaches the app

Publishing is done by `tooling/publish_study_content.py` in the AlphaBuddy app
repo. For each chapter-feature file it:

1. computes a **SHA-256 content hash**,
2. uploads the file to Firebase Storage at
   `study-content-v1/<grade>/<subject>/<chapter-slug>/<feature>.json`,
3. writes/updates the per-class manifest document `study_content_v1/<grade>` in
   Firestore, recording each feature's `{ path, version, hash }` — bumping the
   integer `version` **only when the hash changes**.

On launch (and whenever the student's class changes) the app reads the manifest
for the student's class and downloads **only the chapter-features whose hash
changed** — silently, in the background. Unchanged files are skipped.

> The app's update check compares **content hashes**, not version numbers. The
> `version` field is a human-readable label; the publisher keeps it in sync with
> the hash automatically. If you ever edit Firebase by hand, you must update the
> `hash`, not just the version, or the app won't pick up the change.

## Editing content

1. Add or edit a JSON file under the appropriate `class<N>/<subject>/<Chapter>/`.
2. From the app repo, run:
   ```bash
   python3 tooling/publish_study_content.py            # all classes
   python3 tooling/publish_study_content.py --grade 08 # one class
   python3 tooling/publish_study_content.py --dry-run   # preview, no writes
   ```
   (The publisher defaults its content source to `~/Desktop/akcero-study-content`;
   use `--source <path>` to point elsewhere.)
3. Students receive the change on their next app launch.

## Notes

- Supported classes: **6, 7, 8, 9, 10**. Subjects: **math, science, social**.
- This is educational study material only — no app code, secrets, or student data.
