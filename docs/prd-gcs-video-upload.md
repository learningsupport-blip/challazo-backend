# PRD: Record a demo and store it in the existing GCS bucket

**Status:** describes the live path. No new bucket and no new service account.

**Bucket:** `gs://nxt-acad-hackathon-ls-prod-hackathon-evaluations` (`EVALUATION_BUCKET_NAME`).

**Signer:** the Firebase service account already configured as `FIREBASE_CLIENT_EMAIL`, with its key in `FIREBASE_PRIVATE_KEY`. The API builds the Cloud Storage client from those two values. The browser never receives the key.

## Problem

A student records a demo in the app, or picks a video file, and that file has to land in the evaluation bucket. The API cannot accept the bytes itself: Cloud Run rejects bodies around 32 MiB, and demos are allowed up to 500 MiB. The private key also cannot be shipped to the browser.

## Scope

In scope:

- A logged-in student records a demo or chooses a local file.
- The API plans an object path and returns a short-lived upload URL signed by the existing service account.
- The browser sends the bytes straight to Cloud Storage.
- The API checks that the object is in the right place, then saves the submission.

Out of scope:

- Creating a bucket, a project, or another service account.
- Putting the service-account JSON in the frontend, in Launchpad, or in a download.
- Gemini watching the video. That is a later job. It reads the same `gs://` object. The identity that reads it for Vertex is the Vertex service agent of the project running the model, not this upload signer.
- Admin export, banners, and wiping the bucket.

## Who does what

| Actor | Does |
| --- | --- |
| Student | Records or picks the file. Stays logged in. |
| SPA | Asks for an upload URL, PUTs the bytes, then finalizes the form. |
| API | Checks the student, the type, and the size. Signs the URL. On finalize, checks the object and writes Firestore. |
| Existing service account | Signs the upload URL and later the playback URL. Does not sit in the browser. |
| GCS bucket | Stores the object. CORS must allow the SPA origin. |

## Object path

```
submissions/{student_id}/{submission_id}/video.{ext}
```

`video_source` is `recorded` when the browser recorded it, and `uploaded` when the student picked a file. Both land on this path. The extension comes from the accepted type (`mp4`, `webm`, `mov`, `mkv`, `mpeg`, `ogv`, `avi`). Maximum size is 500 MiB.

## Process

1. The student opens the submit flow and is authenticated (HttpOnly session cookie).
2. They record in the browser, or choose a file. The SPA reads the file name, MIME type, and byte length. It does not upload yet.
3. The SPA calls `POST /submissions/upload-url` with the file name, content type, `video_source` (`recorded` or `uploaded`), and `content_length`.
4. The API rejects a type that is not on the allow-list, and a length over 500 MiB.
5. The API creates a submission id and the object path above. It opens Cloud Storage with the existing service account.
6. The API returns one of three upload plans. The private key is used only on the server to sign or start the session.
   - **Resumable** (default for smaller files): one session URL. The browser can resume if the connection drops.
   - **Signed PUT:** used if the resumable session cannot be started. The browser must send the same `Content-Type` the URL was signed with.
   - **Parallel parts:** when the file is at least 32 MiB. The browser uploads parts, and finalize joins them into the final object.
7. The browser PUTs the bytes to `storage.googleapis.com`. The request `Origin` must be an origin already allowed on the bucket, because the resumable session is created with that origin.
8. The SPA calls `POST /submissions/from-upload` with the `gs://` path, the same content type, the problem, the solution, the hackathon, the round, and the theme.
9. The API refuses the finalize unless all of these are true:
   - The URI is `gs://` in `EVALUATION_BUCKET_NAME`.
   - The object is under `submissions/{this student}/`.
   - The object name ends with `/video.{ext}` for the resolved type.
   - The object exists (or its parts exist and can be composed).
   - The stored size is within 500 MiB.
10. Firestore stores `video_path`, `content_type`, and `video_source`. The file in the bucket is the recording. The API does not keep a second copy.

Playback later is a signed HTTPS GET from the same service account (`VIDEO_SIGNED_URL_EXPIRY_SECONDS`, default 3600). That URL is for viewing. It is not the upload URL.

## Upload URL lifetime

`VIDEO_UPLOAD_URL_EXPIRY_SECONDS` defaults to 3600. After that, the SPA must call `POST /submissions/upload-url` again. A new plan is a new object path. An expired URL must not be reused.

## Security rules

- The service account key stays in Secret Manager (`FIREBASE_PRIVATE_KEY`, `FIREBASE_CLIENT_EMAIL`). It is not a frontend env var.
- A student can only finalize an object under their own prefix. Another student's path is rejected.
- The bucket is not public. Upload and playback both go through URLs the API signs.
- Recording and file pick use the same checks. `video_source` is a label, not a bypass.

## Acceptance

- A recorded webm and a picked mp4 both appear at `submissions/{student_id}/{submission_id}/video.{ext}` in the existing bucket.
- The browser network log shows a PUT to Cloud Storage, not a video body on the API.
- Finalize fails if the object is missing, in another bucket, or under another student.
- A second student cannot finalize the first student's `video_path`.
- No new service account is created for this flow.

## What this document does not change

This is the process the API already implements in `POST /submissions/upload-url` and `POST /submissions/from-upload`.
