# API types and schema

The concrete form of Stage 0's third checkbox in `13-mvp.md`: Pydantic v2 request and response
schemas for the P0 endpoints. `04-api-design.md` says which endpoints exist and why, `13-mvp.md`
freezes the surface; this doc is the types themselves.

Three questions drive it:

1. What may a client send, exactly.
2. What does a finished job hand back: one video, a list, or something else.
3. What does that answer become when the output is not a video.

Short version of 2 and 3: a job produces **one Deliverable**, which is a **role-keyed set of
Artifacts with one designated primary**. A `profile` decides what the primary is. Video is the
first profile, not the only possible one, and the response shape does not change when a second
profile appears.

`13-mvp.md` is frozen (D071). This doc proposes four amendments to it, all additive except one
rename, all made **before Stage 0 starts**, which is the cheapest moment they will ever cost.
They are listed together at the end rather than scattered, and each carries a decision line.

## Naming

| Term | Meaning |
|------|---------|
| Artifact | One stored, verified file. The existing domain type. |
| Deliverable | The complete set of artifacts one job produced, with one primary. |
| Profile | The server-owned id a client asks for: `video.short.v1`. Resolves to an Output Contract. |
| Output Contract | What must exist when generation finishes. Sent to the worker, then re-used as the acceptance test. |

A Deliverable is a projection, not an aggregate. Invariants 3 and 4 of `VideoJob` say a new brief
is a new job and terminal is terminal, so a job holds at most one. It is addressed by `job_id`
and needs no id, no table, and no lifecycle of its own.

## Ids

Every id is `uuid5` off the idempotency spine, rendered as Crockford base32 of the 16 bytes
behind a type prefix. Fixed length, no ambiguous characters, opaque to a client (D072).

```python
type JobId      = Annotated[str, StringConstraints(pattern=r"^job_[0-9A-HJKMNP-TV-Z]{26}$")]
type ArtifactId = Annotated[str, StringConstraints(pattern=r"^art_[0-9A-HJKMNP-TV-Z]{26}$")]
type TraceId    = Annotated[str, StringConstraints(pattern=r"^tr_[0-9A-HJKMNP-TV-Z]{26}$")]

# Foreign. We do not mint it, so it is validated as an opaque token and nothing more.
type ChatContextId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
```

`@audit` `ChatContextId` is the one id accepted without owning it. The pattern stops path and
header injection; it does not make the id ours, and claim-on-first-use (D068) is only safe while
the minting side keeps them unguessable (Q-W).

---

## What the client sends

```python
class ContextKind(StrEnum):
    MEMORY = "MEMORY"
    LEVEL = "LEVEL"
    PRIOR_TOPIC = "PRIOR_TOPIC"
    MISCONCEPTION = "MISCONCEPTION"
    LANGUAGE = "LANGUAGE"
    NOTE = "NOTE"


class ReadingLevel(StrEnum):
    PRIMARY = "PRIMARY"
    LOWER_SECONDARY = "LOWER_SECONDARY"
    UPPER_SECONDARY = "UPPER_SECONDARY"


class ProfileId(StrEnum):
    VIDEO_SHORT_V1 = "video.short.v1"     # built
    HTML_LESSON_V1 = "html.lesson.v1"     # designed, not built


class ContextItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: ContextKind
    text: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class JobOptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: ProfileId = ProfileId.VIDEO_SHORT_V1
    max_duration_s: Annotated[int, Field(ge=15, le=180)] = 90
    language: Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")] = "en"
    reading_level: ReadingLevel | None = None


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    chat_context_id: ChatContextId
    instruction: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    context: Annotated[list[ContextItemIn], Field(max_length=8)] = Field(default_factory=list)
    options: JobOptionsIn = Field(default_factory=JobOptionsIn)
```

`extra="forbid"` on every inbound model. An unknown field is a `400`, not a shrug. A client that
sends `principal_id`, `output_paths`, or `system_prompt` finds out immediately that it does not
get to.

What is deliberately absent from the request:

- **No principal.** It comes from the auth stub. A client cannot name whose job this is.
- **No paths, mime types, or check names.** The client picks a `profile`; the server resolves it.
  A client-authored output contract lets the caller tell the validator what to accept, which is
  the same as having no validator.
- **No prompt, system message, or template override.** User text is data, never instruction
  (D029).
- **No `parent_job_id`.** A revision is a new brief and therefore a new job. See Q-AF.

`options.profile` replaces `options.output_contract` from `04-api-design.md`. The old field was a
bare version string, which said which rules applied but not what was being asked for. Two fields
now: `profile` is *what to make*, `contract_version` (server-side, echoed in responses) is *under
which rules*.

### Out-of-range is rejected, not clamped

`04-api-design.md` clamped `max_duration_s` into range. Withdrawn. Silent clamping means the
request and the result disagree with no way for the client to tell, which is the same failure
shape as a silent partial success (N5). Out of range is `400 INVALID_REQUEST` naming the bound.

The *effective* constraints are echoed on the job document regardless, because a profile may cap
tighter than the field allows: `video.short.v1` caps at 120s while the field permits 180.

### Headers

| Header | On | Rule |
|--------|-----|------|
| `Idempotency-Key` | `POST /v1/jobs` | Required. UUID, or 16–128 chars of `[A-Za-z0-9_-]`. Missing is `400` (D055). |
| `If-None-Match` | `GET /v1/jobs/{id}` | Optional. `304` when nothing a client can see has changed. |
| `Content-Type` | writes | `application/json` only. |

`If-None-Match` is an addition. This is a polling API by choice (D015), so making an unchanged
poll cost one index read and no body is worth two lines. `ETag` is `W/"{version}.{last_seq}"`,
which moves exactly when the job document does.

---

## What comes back

One model for the job, always fully populated. Nulls are serialised rather than omitted, so a
client never branches on key presence.

```python
class JobStatus(StrEnum):
    QUEUED = "QUEUED"; RUNNING = "RUNNING"; SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"; CANCELLED = "CANCELLED"


class StageName(StrEnum):
    INTAKE = "INTAKE"; ADMISSION = "ADMISSION"; PLACEMENT = "PLACEMENT"
    PREPARING = "PREPARING"; GENERATING = "GENERATING"; COLLECTING = "COLLECTING"
    VERIFYING = "VERIFYING"; PUBLISHING = "PUBLISHING"; DONE = "DONE"; FAILED = "FAILED"


class ProgressView(BaseModel):
    percent: Annotated[int, Field(ge=0, le=100)]   # monotonic within a job (D042)
    step: str
    message: str


class ConstraintsView(BaseModel):
    """Effective values after profile caps. Not an echo of the request."""
    max_duration_s: int
    language: str
    reading_level: ReadingLevel | None


class CostView(BaseModel):
    ceiling_micros: int
    spent_micros: int
    measured_micros: int      # metered by us
    claimed_micros: int       # asserted by a worker, never added to measured (D058)


class FailureView(BaseModel):
    code: ErrorCode
    stage: StageName
    message: str              # from ERROR_CATALOG, never written at a raise site (D062)
    retryable: bool
    occurred_at: datetime
    trace_id: TraceId


class JobLinks(BaseModel):
    self: str
    events: str
    deliverable: str | None
    content: str | None       # 302 to the primary artifact's bytes


class JobView(BaseModel):
    job_id: JobId
    chat_context_id: ChatContextId
    status: JobStatus
    stage: StageName
    attempt: int
    progress: ProgressView
    profile: ProfileId
    contract_version: str
    constraints: ConstraintsView
    cost: CostView
    failure: FailureView | None
    artifact: ArtifactSummaryView | None      # the PRIMARY only; the set is behind links.deliverable
    created_at: datetime
    updated_at: datetime
    links: JobLinks
```

`POST /v1/jobs` and `GET /v1/jobs/{id}` both return exactly this document. One shape, one place
to change. `Location` is set on the `202` and on the idempotent-replay `200` alike.

`artifact` stays singular on the job document, holding the primary, because that is the field
`13-mvp.md` froze and because the overwhelmingly common client question is "what do I play". The
full set is one link away and costs nobody a request who does not need it.

### Paging

Two idioms on purpose, and they are not interchangeable.

```python
class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None
```

Collections use an opaque keyset cursor (D072). The event feed uses a dense integer `seq`,
because its whole point is that a client can **detect a gap**, which an opaque cursor hides.

### Events

```python
class JobEventView(BaseModel):
    seq: int
    at: datetime
    type: EventType
    stage: StageName
    severity: Literal["INFO", "WARN", "ERROR"]
    message: str


class EventPageView(BaseModel):
    job_id: JobId
    events: list[JobEventView]
    next_since: int
    complete: bool
```

`detail` and `visibility` are not fields on `JobEventView`. The operator payload cannot leak
because there is nowhere for it to land. That is stronger than a filter at the read site, and it
makes the test in `08-observability.md` a regression guard rather than the only defence.

---

## The deliverable

### "Is it a list?"

A set with roles and one designated primary. `primary_artifact_id` says what to open, `role` says
what everything else is for. A player reads one field; a lesson panel reads the list.

The two shapes it is not, and why:

- A single file does not survive contact with the video profile, which already ships a poster, a
  transcript, and captions. `04-api-design.md` called those `sidecars`, which was a list wearing
  another name.
- A bare array makes the client guess which file to open, usually by sniffing mime. That guess
  breaks the first time a profile has two files of one type.

### "Videos only?"

No. The primary's media type is a property of the **profile**, not of the API. An HTML lesson
returns the same document shape as a video: `profile` differs, `media_type` differs, and the
checks that ran differ. Adding a deliverable type is a registry entry and a validator chain, not
an endpoint and not a response model.

```python
class ArtifactRole(StrEnum):
    PRIMARY    = "PRIMARY"      # the thing the learner opens
    POSTER     = "POSTER"       # still preview
    TRANSCRIPT = "TRANSCRIPT"   # plain text
    CAPTIONS   = "CAPTIONS"     # timed text
    ASSET      = "ASSET"        # referenced by the primary; bundle profiles only
    SOURCE     = "SOURCE"       # scene plan or render input
    LOG        = "LOG"          # harvested agent log


class Audience(StrEnum):
    LEARNER  = "LEARNER"
    OPERATOR = "OPERATOR"
```

`role` replaces `Artifact.kind`, which folded three axes into one enum: what the file is for
(`VIDEO`, `POSTER`), what format it is (`mime` already said), and who may see it (`SOURCE`,
`LOG`). They are three fields now, and the third had nowhere to live before: a harvested
`agent.jsonl` can pass every safety check, be `CLEAN`, and still be something no learner may
fetch. `scan_verdict` answers *is this file dangerous*. `audience` answers *is this file theirs*.
One enum cannot hold both, and conflating them means the only thing standing between an agent's
log and a learner is that nobody wrote the query.

```python
class MediaView(BaseModel):
    """Present only for time-based media. Absent, not null-filled, otherwise."""
    duration_s: float
    width: int | None
    height: int | None
    has_audio: bool


class ArtifactSummaryView(BaseModel):
    """Embedded in JobView and in list responses. Enough to render a card."""
    artifact_id: ArtifactId
    role: ArtifactRole
    media_type: str
    size_bytes: int
    duration_s: float | None
    poster_url: str | None
    content_url: str
    created_at: datetime


class ArtifactView(BaseModel):
    artifact_id: ArtifactId
    job_id: JobId
    role: ArtifactRole
    media_type: str                 # from our probe, never from the worker's manifest
    size_bytes: int
    content_hash: str               # "sha256:..."
    rel_path: str | None            # position in the bundle tree; None when standalone
    media: MediaView | None
    content_url: str
    created_at: datetime


class CheckResultView(BaseModel):
    name: str
    passed: bool


class VerificationView(BaseModel):
    validator_version: str          # recorded per artifact (D064)
    verdict: Literal["CLEAN", "QUARANTINED"]
    checks: list[CheckResultView]
    worker_claim_agreed: bool       # our verdict against the manifest's; feeds worker_claim_mismatch_total


class DeliverableView(BaseModel):
    job_id: JobId
    profile: ProfileId
    contract_version: str
    status: Literal["READY", "QUARANTINED", "UNAVAILABLE"]
    primary_artifact_id: ArtifactId | None
    artifacts: list[ArtifactView]   # LEARNER audience only, primary included
    total_size_bytes: int
    verification: VerificationView
    completed_at: datetime | None
```

`artifacts` includes the primary rather than excluding it. One list, one lookup by
`primary_artifact_id`, and no "is it in both places" question at serialisation time.

### Endpoints

Additions and clarifications against the frozen table in `13-mvp.md`. No frozen path changes
meaning, no frozen path is removed.

| Method | Path | Returns | Status |
|--------|------|---------|--------|
| GET | `/v1/contexts/{ctx}/artifacts` | `Page[ArtifactSummaryView]`, `role=PRIMARY` by default | frozen, P0, filter is new |
| GET | `/v1/artifacts/{artifact_id}/content` | bytes | frozen, P0 |
| GET | `/v1/jobs/{job_id}/deliverable` | `DeliverableView` | new, P1 |
| GET | `/v1/jobs/{job_id}/content` | `302` to the primary's bytes | new, P1 |
| GET | `/v1/bundles/{primary_id}/{rel_path}` | bytes, path-addressed | reserved, `@TODO` |

The context listing defaults to `role=PRIMARY, audience=LEARNER`. Without that default the chat
panel renders a transcript and a poster as if they were two more lessons. `?role=` widens it for
a client that wants everything; `audience=OPERATOR` rows are not reachable through any value of
that parameter.

`GET /v1/jobs/{id}/content` is a two-line handler and it is the difference between
`curl -L .../jobs/$ID/content -o lesson.mp4` and two requests plus a `jq`. It is the shape the
walkthrough deliverable (D4) actually wants.

`409 ARTIFACT_NOT_READY` keeps its name and now covers the deliverable endpoint too. Never `404`,
which would claim the job does not exist.

---

## Profiles and output contracts

Server-owned. This is the object a client selects indirectly and never authors.

```python
class ServingPolicy(BaseModel):
    origin: Literal["API", "ISOLATED"]   # ISOLATED = separate host, no cookies, no shared storage
    disposition: Literal["inline", "attachment"]
    csp: str | None
    sandbox: bool
    nosniff: bool = True


class PartSpec(BaseModel):
    role: ArtifactRole
    audience: Audience
    required: bool
    media_types: frozenset[str]
    max_bytes: int
    max_count: int = 1
    checks: tuple[str, ...]


class OutputContract(BaseModel):
    profile_id: ProfileId
    contract_version: str
    parts: tuple[PartSpec, ...]
    total_max_bytes: int
    max_file_count: int
    serving: ServingPolicy
```

One object, two uses:

- Serialised into `OUTPUT_CONTRACT.json` in the worker workspace, as the statement of what to make.
- Read by `ResultValidator` on the way back, as the acceptance test for what arrived.

The request and the test are the same document, so they cannot drift. A worker that satisfies the
file it was handed passes verification by construction; a worker that does anything else fails
against the exact bytes it was given. It also means "what counts as done" is a reviewable diff in
one registry rather than a condition spread through custody.

### `video.short.v1`

| Role | Required | Media types | Cap | Checks |
|------|----------|-------------|-----|--------|
| PRIMARY | yes | `video/mp4` | 64 MiB | `container_is_mp4`, `video_stream_present`, `audio_stream_present`, `audio_not_silent`, `duration_within_bounds`, `resolution_at_least_720p`, `no_blank_frames` |
| POSTER | no | `image/png`, `image/jpeg` | 2 MiB | `image_decodes`, `aspect_matches_video` |
| TRANSCRIPT | no | `text/plain` | 256 KiB | `utf8_decodes`, `length_plausible_for_duration` |
| CAPTIONS | no | `text/vtt` | 256 KiB | `vtt_parses`, `cue_times_within_duration` |
| LOG | no, OPERATOR | `application/x-ndjson` | 8 MiB | `utf8_decodes`, `line_count_cap`, `trace_id_present` |

Totals 80 MiB, 24 files, duration bound `max_duration_s` ±10%, cap 120s.
Serving: `origin=API, disposition=inline, csp=None`.

`audio_stream_present` and `audio_not_silent` are two checks because R7 asks for something that
feels like a normal educational video, and a silent AAC track satisfies the first while failing
the requirement. This is exactly the class of defect a worker's own manifest reports as
`QUALIFIED`.

### `html.lesson.v1`, designed and not built

| Role | Required | Media types | Cap | Checks |
|------|----------|-------------|-----|--------|
| PRIMARY | yes | `text/html` | 2 MiB | `html5_parses`, `no_script_elements`, `no_event_handler_attributes`, `no_external_references`, `no_javascript_uris`, `images_are_data_uris_only`, `no_iframes_or_objects`, `stylesheet_inline_only` |
| TRANSCRIPT | no | `text/plain` | 256 KiB | as above |
| LOG | no, OPERATOR | `application/x-ndjson` | 8 MiB | as above |

One self-contained document. No `ASSET` parts, no bundle tree, no JavaScript.

**Why the checks are that blunt: an HTML deliverable is not the same kind of object as a video.**
A video is inert data: the learner's player decodes it and nothing in the file executes. An HTML
page written by an untrusted agent is *active content*, and serving it from our origin runs
agent-authored code in a learner's browser with our cookies, our storage, and our domain name.
Every control in `06-trust-boundary.md` was designed for inert output, and none of them addresses
this. The safety argument for video (harvest allowlist, structural probe, independent
verification) is about what a file *is*. For HTML the question becomes what a file *does*, and
that is not answerable by probing.

So serving policy is part of the contract rather than an implementation detail:

```
origin=ISOLATED, disposition=inline, sandbox=True

Content-Security-Policy: default-src 'none'; img-src data:; style-src 'unsafe-inline';
                         font-src data:; form-action 'none'; base-uri 'none';
                         frame-ancestors 'self'
X-Content-Type-Options: nosniff
```

`@audit` an isolated origin is a deployment fact, not a code fact. Shipping the HTML profile
without one makes "the backend verified it" a claim about bytes and not about behaviour: the
sanitiser can be perfect and one missed vector still executes on our domain. If HTML ever ships
before an isolated origin exists, `disposition` is `attachment` and nothing renders in place.
That is a downgrade in product value and it is the correct trade.

The multi-file variant (`html.bundle.v1`: a page plus `ASSET` parts served through
`/v1/bundles/...`) is why `rel_path` and the `ASSET` role exist in the types now. Two nullable
fields today; a `/v2` without them.

### Adding a profile

The whole point of the indirection. A new deliverable type is:

1. A `ProfileId` member.
2. An `OutputContract` row in the registry, with its parts, checks, and serving policy.
3. A validator chain entry per new check name.
4. A template repo or a subject pack in the existing one (Q-O).

Not touched: `JobView`, `DeliverableView`, `ArtifactView`, any router, any step, the workflow
engine, the queue, custody's storage path, the event vocabulary, or the SQL schema.

---

## Amendments to the frozen artifacts

`13-mvp.md` is frozen under D071. Four changes, made before Stage 0 writes a line of code, when
the cost is editing a document rather than running a migration and cutting a client release.

**A1. `artifacts.kind` becomes `artifacts.role`, with new members.**
`VIDEO POSTER TRANSCRIPT SOURCE LOG` becomes `PRIMARY POSTER TRANSCRIPT CAPTIONS ASSET SOURCE
LOG`. This is the one non-additive amendment: `VIDEO` is removed rather than deprecated, because
its replacement is not a rename but a different question. `PRIMARY` is a role; `VIDEO` was half a
role and half a format, and format already lives in `mime`.

**A2. `artifacts.audience` added**, `text NOT NULL DEFAULT 'LEARNER'`, written at insert by
`custody` and never derived at read time. Additive.

**A3. `artifacts.rel_path` added**, `text NULL`. Additive, unused until a bundle profile exists.

**A4. `jobs.profile` added**, `text NOT NULL DEFAULT 'video.short.v1'`. `jobs.output_contract`
keeps its name and now holds only the contract version. Additive.

`jobs.artifact_id` keeps its name and its type, and is documented as the **primary** artifact.
Nothing about the frozen listing endpoints changes; `/v1/contexts/{ctx}/artifacts` gains a
default filter and an optional `role` parameter, both additive.

Index consequence, since the chat panel listing is the query that has to stay one index scan:

```sql
CREATE INDEX artifacts_by_context ON artifacts (chat_context_id, created_at DESC)
    WHERE scan_verdict = 'CLEAN' AND audience = 'LEARNER' AND role = 'PRIMARY';
```

That is D073's index with the two new predicates folded in, so the panel query still touches only
rows it is allowed to show.

## Error catalog additions

| Code | HTTP | Meaning |
|------|------|---------|
| `OUTPUT_PROFILE_NOT_SUPPORTED` | 422 | Profile unknown, disabled, or not offered for this concept |
| `DELIVERABLE_INCOMPLETE` | 409 | Required parts missing after verification; the job is FAILED and the evidence is retained |

`ARTIFACT_NOT_READY` and `ARTIFACT_QUARANTINED` are unchanged. Enum members are added, never
removed (D072).

## Contradictions this doc resolves

Three places where the existing docs disagree with each other or with this one. Recorded so the
reasoning is not swallowed by the diff.

1. **`Artifact.kind` splits into `role` + `mime` + `audience`** (`01-domain-model.md`). The old
   enum could not express "clean, but operator-only".

2. **`max_duration_s` rejects instead of clamping** (`04-api-design.md`). Strict schema in,
   honest failure out.

3. **Artifact identity, unresolved.** D010 content-addresses artifacts by
   `hash(query, concept, pipeline_version, ...)` so a repeat query reuses an existing artifact.
   D055 derives `artifact_id = uuid5(NS_ART, job_id | content_hash)`, which includes the job and
   therefore cannot dedupe across jobs. These do not agree, and the disagreement costs money: N1
   and N3 both want the second learner asking Q2 to receive the first learner's video rather than
   pay for a second render. It is also a privacy question, because cross-job reuse means one
   learner's bytes are served to another. Parked as Q-AC rather than decided here, since the
   `artifacts_by_hash` index already exists and nothing in the MVP path depends on the answer.
