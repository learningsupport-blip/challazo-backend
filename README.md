# Challazo Backend

FastAPI service for the Challazo hackathon platform. Students enroll in a published round, submit a demo, and wait for review. Admins assign approved evaluators. Evaluators score with a rubric and can run Gemini on the demo video and an external analyser on a GitHub URL. A super admin approves evaluations. Students see scores and the round leaderboard only after those results are published.

Data lives in **Cloud Firestore**. Videos live in **Google Cloud Storage**. Login uses **Firebase Authentication**. There is no Realtime Database and no Postgres.

System design (layers, collections, job flow) is in [docs/architecture.md](docs/architecture.md). Field-level request and response schemas are the OpenAPI document at `/docs` when the API is running in development.

## Stack

| Concern | Choice |
|---|---|
| API | FastAPI, Python ≥ 3.11, Pydantic v2 |
| Auth | Firebase Identity Toolkit login, Admin SDK token verification, Firestore roles |
| Database | Cloud Firestore |
| Files | GCS bucket `EVALUATION_BUCKET_NAME` |
| Video AI | Vertex / Gemini (`GEMINI_MODEL`, default `gemini-3.5-flash`) |
| Repo AI | External analyser at `GITHUB_AI_EVALUATION_URL` |
| Email | Console log in development; Brevo SMTP or Firestore `mail` when configured |
| Long jobs | Cloud Tasks in production; in-process `BackgroundTasks` locally |
| Time | Asia/Kolkata (IST) for windows and timestamps |
| Package | `challazo-backend` (`pyproject.toml`); import path is `app` |

## Repository layout

```text
app/
  main.py              # app, CORS, lifespan, /health, /
  dependencies.py      # process-scoped service container
  exceptions.py        # AppError → HTTP status
  middleware/          # cookie/Bearer auth, CSRF, activity log
  models/              # request and response schemas
  routes/              # HTTP handlers
  services/            # business logic; FirebaseService is the only Admin SDK client
  utils/seeder.py      # startup Super Admin
docs/architecture.md   # system design
tests/                 # pytest
Dockerfile             # Cloud Run, port 8080
cloudbuild.yaml        # Artifact Registry, GCS, Cloud Tasks, Cloud Run
docker-compose.yml     # local container on port 8000
```

Routes call services. Services call `FirebaseService`. Do not import `firebase_admin` from a route.

## Local setup

Requirements: Python 3.11 or newer, a Firebase project with Firestore and Authentication, and a GCS bucket for videos.

```bash
cd challazo-backend
python3 -m venv .venv
source .venv/bin/activate          # Windows Git Bash: source .venv/Scripts/activate
pip install -e ".[dev]"
cp .env.example .env               # then fill the values in Configuration
```

Start the API with reload. This command is the local default and listens on port **8000**:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

`python -m app.main` also binds `0.0.0.0:8000`. To use another port, pass it explicitly:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

The Docker image and Cloud Run listen on **8080** (`PORT` in the `Dockerfile`). `docker-compose` maps host port 8000.

```bash
docker-compose --profile dev up    # hot reload, http://localhost:8000
```

When `ENVIRONMENT` is not `production`, interactive docs are on:

- http://localhost:8000/docs
- http://localhost:8000/health — `{ "status": "healthy", "message": "Challazo Backend is running" }`
- http://localhost:8000/ — welcome payload, plus `/docs` and `/openapi.json` links

`GET /health` and `GET /` do not require a login.

### Startup seed

`SEED_ON_STARTUP=true` (the local default) creates one Super Admin if that account is missing:

| Field | Value |
|---|---|
| Email | `superadmin@challazo.in` |
| Role | `super_admin` |
| Password | `password` on that user in `app/utils/seeder.py` |

The seed does not create sample students, evaluators, or AI prompts. Admins are created later from Application Access. A previous bootstrap login `admin@nxtwave.co.in` is deleted on startup if it still exists. Set `SEED_ON_STARTUP=false` in production after the Super Admin exists. A seed failure is logged and does not stop the process.

This process writes to the Firebase project in `.env`. A local run against a shared project creates that Super Admin there.

## Roles

Roles are stored on the Firestore user document, not on Firebase custom claims.

| Role | What they can do |
|---|---|
| `super_admin` | Every admin route, plus staff roles, registration settings, database reset, bulk evaluation approval, and winner email |
| `admin` | Hackathons, themes, rubrics, assignment, round publish, leaderboard publish |
| `custom` | Admin routes whose area is listed on that role's permissions (`/admin/access`) |
| `evaluator` | Assigned submissions only, after `approval_status` is `approved`. Email must end with `@nxtwave.co.in` |
| `student` | Catalog, enrollment, own submissions. Scores and leaderboard after publish |

A pending evaluator can call `GET /auth/me`. Other app routes return `403` until an admin approves them.

## Authentication

`POST /auth/login` calls Firebase Identity Toolkit with `FIREBASE_WEB_API_KEY`. It does not mint tokens with the Admin SDK. A successful login sets an HttpOnly `access_token` cookie. The JSON body includes `csrf_token` for the SPA. Swagger and scripts can send `Authorization: Bearer <id_token>` instead of the cookie.

A token that Firebase accepts but that has no Firestore user document returns `404`.

Mutating requests must send header `X-CSRF-Token` equal to the `csrf_token` cookie when `CSRF_PROTECTION=true`. On a cross-origin frontend, read the token from the login JSON or from `GET /auth/csrf`. The API cookie is not visible to `document.cookie` on the frontend origin. `POST /auth/change-password` also requires the current password when `REQUIRE_CURRENT_PASSWORD_ON_CHANGE=true`.

Local cookie default is `COOKIE_SAMESITE=lax`. A cross-origin production frontend uses `none` (Secure, HTTPS).

```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -c cookies.txt \
  -d '{"email":"superadmin@challazo.in","password":"<password-from-seeder>"}'
```

### Registration

Students and evaluators share one verification sequence. Team formation happens later, per hackathon round.

1. `POST /auth/register/start` — `{ "email", "mobile_number" }`, or `{ "role": "evaluator", "email" }`. Evaluator email must be `@nxtwave.co.in`. Returns `session_id`.
2. `POST /auth/email/send-otp` — `{ "session_id", "email" }`.
3. `POST /auth/email/verify-otp` — `{ "session_id", "code" }`.
4. Firebase Phone Auth in the browser, then `POST /auth/verify-phone-token` — `{ "session_id", "firebase_id_token", "mobile_number" }`.
5. `POST /auth/register/complete` for a student, or `POST /auth/register/evaluator/complete` for an evaluator.

Evaluator accounts start as `pending`. `POST /auth/register/evaluator` returns `410`; use the sequence above.

In development, email OTP is printed in the server log unless `EMAIL_PROVIDER=smtp`. Phone Auth must be enabled in the Firebase console. Use `http://127.0.0.1:5173` for local phone testing; Firebase rejects `localhost` as an authorized domain.

Staff invited by a super admin finish their own profile with `POST /auth/profile/start` and `POST /auth/profile/complete`.

### Password reset

1. `POST /auth/forgot-password/start` — `{ "email" }`.
2. Email OTP, then Firebase Phone Auth on the registered mobile.
3. `POST /auth/forgot-password/reset` — sets the new password and revokes refresh tokens. It does not log the user in.

Errors from verification use `{ "detail": { "code", "message" } }` (`EMAIL_TAKEN`, `INVALID_CODE`, `NOT_VERIFIED`, `RATE_LIMITED`, and others).

## How a submission is evaluated

1. Student `POST /submissions/upload-url`, uploads the file with HTTP `PUT` to the signed GCS URL, then `POST /submissions/from-upload`. A legacy multipart `POST /submissions` still exists. One student or team is limited per round (`submission-limit`, default 1, maximum 3).
2. Admin assigns an evaluator with `POST /submissions/{id}/assign`, or splits a hackathon with `POST /submissions/admin/hackathons/{id}/assign-equally`. The dropdown is the hackathon roster from `PUT /hackathons/{id}/evaluators`.
3. `POST /submissions/{id}/evaluate` returns **202**. Gemini reads the `gs://` URI. Locally the work runs in the API process. In production Cloud Tasks calls `POST /internal/jobs/evaluate-submission` with header `X-Internal-Job-Secret`. Optional GitHub analysis is `POST /submissions/{id}/evaluate-github-ai` (also 202).
4. The client polls `GET /submissions/{id}`. `status` moves `uploaded` → `processing` → `completed` or `failed`.
5. Evaluator `POST /submissions/{id}/submit-for-review`. Super admin `POST /submissions/{id}/approve-evaluation` or `POST /submissions/{id}/request-changes`. Bulk approval is `POST /hackathons/{id}/evaluation-approvals/approve-all`.
6. Students see the report after publish (`POST /submissions/{id}/publish`, or hackathon auto-publish). They see the round ranking after `POST /hackathons/{id}/rounds/{round_index}/leaderboard/publish`.

Videos are stored at `submissions/{student_id}/{submission_id}/video.<ext>`.

## HTTP API

Paths below are the live routers. Request bodies and response fields are the Pydantic models exposed at `/docs`.

### Public

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Process health and IST server time |
| `GET` | `/` | Welcome payload |
| `GET` | `/auth/registration-config` | Whether self-service registration is open |
| `POST` | `/auth/login` | Session cookie |
| `GET` | `/auth/csrf` | CSRF token for an existing session |
| `POST` | `/auth/logout` | Clear the cookie |
| `POST` | `/auth/register/start` | Begin student or evaluator registration |
| `POST` | `/auth/email/send-otp` | Send email OTP |
| `POST` | `/auth/email/verify-otp` | Check email OTP |
| `POST` | `/auth/verify-phone-token` | Check Firebase phone token |
| `POST` | `/auth/register/complete` | Create the student |
| `POST` | `/auth/register/evaluator/complete` | Create a pending evaluator |
| `POST` | `/auth/forgot-password/start` | Start password reset |
| `POST` | `/auth/forgot-password/reset` | Set a new password |

### Signed-in user — `/auth`

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/auth/me` | Current profile, role, approval |
| `PATCH` | `/auth/me` | Update own profile |
| `POST` | `/auth/change-password` | Change password (current password required) |
| `POST` | `/auth/profile/start` | Start staff email/mobile verification |
| `POST` | `/auth/profile/complete` | Save staff employee id |
| `POST` | `/auth/verification/start` | Account verification session |
| `POST` | `/auth/verification/complete` | Finish account verification |

### Hackathons — `/hackathons`

Admin creates a hackathon directly (`POST /hackathons`, multipart form) or through a draft wizard.

| Method | Path | Who | Purpose |
|---|---|---|---|
| `GET` | `/hackathons` | signed in | Hackathons visible to the caller |
| `GET` | `/hackathons/catalog` | public | Home catalog. Signed-in students only see hackathons open to them |
| `GET` | `/hackathons/{id}` | signed in | One hackathon, including themes |
| `POST` | `/hackathons` | admin | Create |
| `PATCH` | `/hackathons/{id}` | admin | Update |
| `DELETE` | `/hackathons/{id}` | admin | Delete |
| `POST` | `/hackathons/drafts` | admin | Start a draft |
| `GET` | `/hackathons/drafts` | admin | Draft inbox |
| `GET` | `/hackathons/drafts/{draft_id}` | admin | One draft |
| `PATCH` | `/hackathons/drafts/{draft_id}` | admin | Save a wizard section |
| `POST` | `/hackathons/drafts/{draft_id}/banner` | admin | Upload banner |
| `POST` | `/hackathons/drafts/{draft_id}/publish` | admin | Create the hackathon and drop the draft |
| `DELETE` | `/hackathons/drafts/{draft_id}` | admin | Discard a draft |
| `POST` | `/hackathons/{id}/duplicate` | admin | Copy into a new draft (no submissions) |
| `POST` | `/hackathons/{id}/rounds/{round_index}/publish` | admin | Publish a round to students |
| `GET` | `/hackathons/{id}/themes` | signed in | Themes for the submission form |
| `GET`/`PUT` | `/hackathons/{id}/evaluators` | admin | Roster allowed on the assign dropdown |
| `GET`/`PUT` | `/hackathons/{id}/visibility` | admin | Universities and batches that can see it |
| `GET`/`PUT` | `/hackathons/{id}/submission-limit` | admin | 1, 2, or 3 submissions per round |
| `GET`/`PUT` | `/hackathons/{id}/report-publishing` | admin | Auto-publish approved reports |
| `GET`/`PUT`/`DELETE` | `/hackathons/{id}/video-analysis-prompts` | admin | Hackathon prompt overrides |
| `DELETE` | `/hackathons/{id}/video-analysis-prompts/{prompt_key}` | admin | Clear one override (`checklist` or `analyze_video`) |
| `GET` | `/hackathons/{id}/evaluation-approvals` | super admin | Count of reviews waiting |
| `POST` | `/hackathons/{id}/evaluation-approvals/approve-all` | super admin | Approve every submitted review |
| `GET` | `/hackathons/{id}/rounds/{round_index}/leaderboard` | signed in | Ranking; students only after publish |
| `POST` | `/hackathons/{id}/rounds/{round_index}/leaderboard/publish` | admin | Publish or unpublish the board |
| `GET` | `/hackathons/{id}/rounds/{round_index}/winners` | super admin | Preview top-N emails |
| `POST` | `/hackathons/{id}/rounds/{round_index}/winners/announce` | super admin | Email winners |

Round index is 0-based. Publish is rejected after the round's end minute in IST. Students see a round only after it is published. Submissions are open from `start_time` through `end_time` (missing times are 00:00 and 23:59).

Leaderboard rank uses `final_score` on submissions whose `review_status` is `approved`. Students on a hackathon they cannot see receive `404`.

### Teams — same `/hackathons` prefix

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/hackathons/{id}/rounds/{round_index}/participation` | This student's enrollment |
| `POST` | `/hackathons/{id}/rounds/{round_index}/enroll/solo` | Solo round |
| `POST` | `/hackathons/{id}/rounds/{round_index}/teams/create` | Create a team; 6-digit join code, 5 minutes |
| `POST` | `/hackathons/{id}/rounds/{round_index}/teams/join` | Join with that code |
| `POST` | `/hackathons/{id}/rounds/{round_index}/teams/join-code` | Leader refreshes the code |

### Submissions — `/submissions`

| Method | Path | Who | Purpose |
|---|---|---|---|
| `GET` | `/submissions/accepted-video-types` | student | Allowed video MIME types and max size |
| `POST` | `/submissions/upload-url` | student | Signed GCS upload |
| `POST` | `/submissions/from-upload` | student | Finalize after PUT |
| `POST` | `/submissions` | student | Legacy multipart create |
| `GET` | `/submissions` | signed in | Caller's submissions |
| `GET` | `/submissions/{id}` | signed in | One submission; poll this |
| `GET` | `/submissions/{id}/video` | signed in | Video access |
| `GET` | `/submissions/{id}/analysis` | signed in | AI analysis |
| `GET` | `/submissions/{id}/report` | signed in | Report (student after publish) |
| `GET` | `/submissions/{id}/team` | signed in | Team on the submission |
| `POST` | `/submissions/{id}/withdraw` | student | Withdraw |
| `POST` | `/submissions/{id}/evaluate` | evaluator, admin | Queue Gemini; **202** |
| `POST` | `/submissions/{id}/evaluate-github-ai` | evaluator, admin | Queue GitHub analysis; **202** |
| `POST` | `/submissions/{id}/submit-for-review` | evaluator | Hand to super admin |
| `POST` | `/submissions/{id}/approve-evaluation` | admin | Approve score |
| `POST` | `/submissions/{id}/request-changes` | admin | Send back |
| `POST` | `/submissions/{id}/publish` | admin | Show report to the student |
| `POST` | `/submissions/{id}/assign` | admin | Assign or reassign; reassign requires a reason |
| `GET` | `/submissions/assigned-to-me` | evaluator | Flat assigned list |
| `GET` | `/submissions/evaluator/hackathons` | evaluator | Hackathons that have their work |
| `GET` | `/submissions/evaluator/hackathons/{id}` | evaluator | Their rows for one hackathon |
| `GET` | `/submissions/admin/hackathons` | admin | Hackathons with counts |
| `GET` | `/submissions/admin/all` | admin | Every submission |
| `GET` | `/submissions/admin/hackathons/{id}` | admin | Queue. `page` switches the body to `{ items, total, page, page_size, round_summary }` |
| `POST` | `/submissions/admin/hackathons/{id}/assign-equally` | admin | Split unassigned work across evaluators |
| `POST` | `/submissions/admin/hackathons/{id}/export/google-sheet` | admin | Sync a spreadsheet |

Admin list filters: `page`, `page_size` (default 10, max 100), `round_index`, `status` (`uploaded`, `processing`, `completed`, `failed`), `review_status` (`none`, `pending_review`, `changes_requested`, `approved`), `q` (team name, theme name, or submission id).

Signed upload:

```bash
# 1. Prepare
curl -X POST http://localhost:8000/submissions/upload-url \
  -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  -H "Content-Type: application/json" \
  -d '{"content_type":"video/mp4","filename":"demo.mp4"}'

# 2. PUT the file to upload_url with the same Content-Type

# 3. Finalize with the returned video_path
curl -X POST http://localhost:8000/submissions/from-upload \
  -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  -H "Content-Type: application/json" \
  -d '{"video_path":"<from step 1>"}'
```

Direct browser upload needs GCS CORS that includes the frontend origin. The bucket must already exist.

### Setup data

| Prefix | Operations |
|---|---|
| `/themes` | Create, list, get, patch, delete |
| `/universities` | `GET /universities` is public (registration dropdown). Create, update, and delete, plus `/{id}/batches`, are admin |
| `/evaluation-requirements` | CRUD, plus `/{id}/scoring-setup`, `/{id}/metric-scoring` get, put, delete |
| `/ai-evaluation-metric-scoring` | Scorecard CRUD by scoring id |
| `/ai-evaluation-prompts` | List, get, and replace a prompt by key |

### People and settings

| Prefix | Who | Operations |
|---|---|---|
| `/admin/overview` | admin | Counts for the dashboard |
| `/admin/users` | admin | Paginated user list |
| `/admin/user/{user_id}` | admin | Get, patch, delete |
| `/admin/evaluators` | admin | Approved and all evaluator lists |
| `/admin/evaluators/pending` | admin | Waiting for approval |
| `/admin/evaluators/{user_id}` | admin | Get, delete |
| `/admin/evaluators/{user_id}/approve` | admin | Set `approval_status` to `approved` |
| `/admin/access/permissions` | super admin | Permission catalog |
| `/admin/access/roles` | super admin | Custom roles: list, create, patch, delete |
| `/admin/access/users` | super admin | Staff logins: list, create, patch, delete |
| `/admin/access/logs` | super admin | Activity log, paginated |
| `/admin/access/observation` | super admin | Observation summary |
| `/super-admin/settings` | super admin | App settings, profile password, registration open/close, hackathon delete, database reset |

`POST /super-admin/settings/reset-database` and hackathon delete remove Firestore data for that action. Treat them as production-destructive.

### Internal jobs

`POST /internal/jobs/evaluate-submission` is for Cloud Tasks, not the SPA. The caller sends `X-Internal-Job-Secret`. Leave `CLOUD_TASKS_*` unset locally; `EVALUATION_JOB_MODE=auto` then uses `BackgroundTasks`.

## Errors

| Source | Client sees |
|---|---|
| `AppError` | Its status and `{ "detail" }`, or `{ "detail": { "code", "message" } }` when a code is set |
| Uncaught `ValueError` | 400, 404, 409, or 413 from the message text |
| `InfrastructureError` | 503 and a generic body |
| Any other exception | 500 `{ "detail": "Internal server error" }` (stack is logged) |

## Configuration

Copy `.env.example` to `.env`. The process reads environment variables. `.env` is gitignored.

Required before startup:

| Variable | Purpose |
|---|---|
| `FIREBASE_PROJECT_ID` | Firebase / GCP project |
| `FIREBASE_PRIVATE_KEY` | Service account private key. Keep `\n` escapes; the app turns them into newlines |
| `FIREBASE_CLIENT_EMAIL` | Service account email |
| `FIREBASE_WEB_API_KEY` | Identity Toolkit key used by `POST /auth/login`. Firebase console → Project settings → General → Web API key |

`FIREBASE_PRIVATE_KEY_ID` and `FIREBASE_CLIENT_ID` are optional and match the service account JSON. Firestore does not use a Realtime Database URL.

| Variable | Local default | Purpose |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | falls back to `FIREBASE_PROJECT_ID` | GCS and Gemini project |
| `GOOGLE_CLOUD_LOCATION` | `global` | Gemini location |
| `EVALUATION_BUCKET_NAME` | — | Existing GCS bucket for videos and banners |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Video model |
| `GEMINI_ENTERPRISE` | `true` | Enterprise Gemini client |
| `ENVIRONMENT` | `development` | `production` hides `/docs` unless `ENABLE_API_DOCS=true` |
| `ALLOWED_ORIGINS` | localhost and the Vercel app | CORS allow-list, credentials on |
| `COOKIE_SAMESITE` | `lax` | `none` for cross-origin HTTPS |
| `CSRF_PROTECTION` | `true` | Require `X-CSRF-Token` on mutations |
| `SEED_ON_STARTUP` | `true` | Create the Super Admin |
| `EVALUATION_JOB_MODE` | `auto` | Cloud Tasks when queue, target, and secret are set; otherwise in-process |
| `EMAIL_PROVIDER` | console in development | `smtp` (Brevo) or `firestore` |
| `GITHUB_AI_EVALUATION_URL` | analyser Cloud Run URL | GitHub scoring service |
| `OTP_PEPPER` | `FIREBASE_PROJECT_ID` | Hash pepper for email OTPs |
| `OTP_MAX_SENDS_PER_EMAIL_PER_HOUR` | `5` | Per inbox |
| `OTP_MAX_SENDS_PER_IP_PER_HOUR` | `2000` | Shared campus NAT. `0` disables the IP cap |

Google Sheets export needs a Drive folder shared with `FIREBASE_CLIENT_EMAIL` as Editor, then `GOOGLE_SHEETS_EXPORT_FOLDER_ID`.

Brevo (production email): `SMTP_HOST=smtp-relay.brevo.com`, `SMTP_PORT=587`, `SMTP_USERNAME`, `SMTP_PASSWORD` (the SMTP key), and `SMTP_FROM` set to a verified sender. `SMTP_FROM_NAME` defaults to `Challazo`.

## Deployment

`cloudbuild.yaml` builds in **asia-south1** and deploys Cloud Run service **`ai-hackathon-evaluator-backend`** from Artifact Registry repo `ai-hackathon-evaluator-backend`. The container listens on port 8080. The service is `--allow-unauthenticated`; authorization is the application cookie and Bearer check. Memory 1 GiB, 1 CPU, timeout 3600s.

Cloud Build substitutions (override per trigger):

| Substitution | Default |
|---|---|
| `_ALLOWED_ORIGINS` | `https://challazo.nxtlab.tech` |
| `_GCS_CORS_ORIGINS` | that origin, plus localhost 3000 and 5173 |
| `_ENVIRONMENT` | `production` |
| `_SEED_ON_STARTUP` | `false` |
| `_SMTP_HOST` / `_SMTP_PORT` / `_SMTP_FROM` | Brevo relay, 587, `noreply@mail.challazo.nxtlab.tech` |
| `_GITHUB_AI_EVALUATION_URL` | GitHub analyser URL |
| `_OTP_MAX_SENDS_PER_IP_PER_HOUR` | `2000` |
| `_OTP_MAX_SENDS_PER_EMAIL_PER_HOUR` | `5` |

Secret Manager names mounted on the service: `FIREBASE_PROJECT_ID`, `FIREBASE_PRIVATE_KEY`, `FIREBASE_CLIENT_EMAIL`, `FIREBASE_WEB_API_KEY`, `INTERNAL_JOB_SECRET`, `SMTP_USERNAME`, `SMTP_PASSWORD`.

The image does not contain `.env`. Production config is Secret Manager plus `--set-env-vars` in `cloudbuild.yaml`.

```bash
gcloud builds submit --config=cloudbuild.yaml
```

## Development commands

```bash
pytest
pytest tests/test_ids.py::test_new_id_is_version_5_and_unique
black .          # line length 100
flake8 app
mypy app
```

Route handlers are `async`. Firestore, GCS, SMTP, and Identity Toolkit run through `app.utils.async_io.run_sync` so they do not block the event loop.
