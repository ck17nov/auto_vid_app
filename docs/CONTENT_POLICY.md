# CONTENT_POLICY.md

The rules this system enforces on itself, and where in the code each one lives.

The goal is a smaller number of genuinely good videos, not the maximum number of
automatically generated ones.

---

## 1. Originality

**What is never done:**

- copy or paraphrase another creator's script
- reproduce another creator's narration
- download or reuse copyrighted footage
- recreate a video shot-for-shot
- clone a real person's voice
- publish near-duplicate videos at scale

**How research is actually used.** The engine reads only what the official
YouTube API returns — titles, descriptions, public statistics. It never
downloads a video, an audio track or a transcript. Those titles are treated as
*evidence of audience demand*, and the prompt says so explicitly:

> the titles above show what the audience wants; they are NOT material to copy.
> Do not rewrite, translate, reorder or paraphrase any of them.

**How it is enforced, not just requested:**

| Check | Where | Effect |
|---|---|---|
| Concept vs researched titles | `content/ideas.py` | idea rejected above 0.62 angle overlap |
| Concept vs our own past topics | `content/ideas.py` | rejected above the duplicate threshold |
| Script vs researched text | `content/originality.py` | **blocks upload** above 0.30 4-gram Jaccard |
| Script vs our own past scripts | `content/originality.py` | **blocks upload** above 0.80 |
| Intra-batch duplicates | `content/ideas.py` | dropped |

Similarity compares the **angle**, not the subject: every video about black holes
shares the words "black holes", which is a topic, not plagiarism. Topic tokens
are stripped before measuring, so "What Most People Get Wrong About Black Holes"
does not collide with "10 Facts About Black Holes" — while a true copy still
scores 1.0 and is rejected.

**Every video ships an `originality_report.json`** containing the research
sources with how each was used, the inspiration video IDs, the concept and its
new angle, the measured similarity plus the script hash and method, every visual
asset with source and licence, and the audio provenance.

---

## 2. Assets and licensing

Every asset in `asset_manifest.json` carries a source and a licence. There are
exactly four permitted origins:

| Source | Licence | Commercial YouTube use |
|---|---|---|
| Procedural generator (local) | generated locally, no third-party rights | yes |
| AI generation (Pollinations) | generated, no third-party rights asserted | yes — verify current provider terms |
| Pixabay | Pixabay Content License | yes, no attribution required |
| Pexels | Pexels License | yes, no attribution required |

Attribution is recorded even when not required, so the report is complete.

**Audio:** narration is synthetic TTS using the provider's own voices. Music is
either procedurally synthesised from oscillators and filtered noise, or a file
*you* placed in `assets/music/`. Sound effects are synthesised from noise
envelopes. Nothing is ever auto-downloaded from a music site, and no third-party
samples are used.

**Fonts:** Anton, under the SIL Open Font License 1.1 — redistributable,
commercial use permitted.

---

## 3. Children's content

Selecting a child-directed niche, or enabling the flag, activates a distinct
safety profile — it is not normal content with a checkbox.

**Enforced content restrictions** (`core/niche.py`): no violence, injury, blood
or weapons; no scary imagery or horror atmosphere; no dangerous challenges or
imitable risky behaviour; no profanity; no romance or sexual references; no
alcohol, tobacco, drugs or gambling; no manipulative urgency or fake giveaways;
no misleading titles or thumbnails; no requests for personal information; no
external links or purchase prompts aimed at children.

**Also changed:** simpler vocabulary, slower pacing (2.0 words/second), longer
scenes, calm block captions instead of aggressive karaoke, brighter and softer
palettes, gentler contrast.

**Enforced at publish time:**

- The app **asks explicitly** and blocks the automation until answered — the
  backend returns 409 `kids_confirmation_required` if a child-directed niche is
  submitted without the flag.
- `selfDeclaredMadeForKids` is set on the upload itself, at insert time.
- Kids content **always** requires human approval, even in AUTO mode.
- The quality gate scans for violence, frightening language, inappropriate
  language, romance, regulated goods and commercial pressure, and **blocks** on
  any hit.
- A mismatch between the niche profile and the flag is itself a blocker.

No attempt is made to work around YouTube's requirements. Misclassifying
child-directed content has legal consequences.

---

## 4. Factual accuracy

Research and script generation are separate stages. The generator receives
research as context and is told never to invent statistics, studies, dates or
quotes.

For factual niches (science, technology, history, finance, health, space, news)
`FactChecker` grades every claim:

- **High risk, requires approval:** medical cure claims, vaccine
  misinformation patterns, financial guarantees, personalised buy/sell advice,
  overstated scientific certainty ("scientists have proved"), conspiracy framing.
- **Medium risk:** a number or date that appears in the narration but not in the
  script's own declared claims array — the most common hallucination surface;
  references to "studies" or "researchers" with no sources listed; a script
  produced by the template builder rather than an LLM.

The independent scan matters: a model that hallucinates a statistic will also
omit it from its own claims list, so the narration is re-scanned separately.

**Finance:** never phrased as advice, no guaranteed-return language, no price
predictions. "Educational content only. Not financial advice." is appended
automatically.

**Health:** no diagnosis, dosage or treatment instructions, no cure claims.
"General information only. Not medical advice." is appended automatically.

---

## 5. Titles and thumbnails

Deceptive clickbait is scored **down**, not up. `misleading_risk` is subtractive
and covers unverifiable claims ("aliens"), overstated certainty ("proof"), health
and financial overpromises, guarantee language, empty clickbait ("you won't
believe"), punctuation shouting and ALL CAPS.

A title whose vocabulary barely appears in the actual script is penalised — that
is the mechanical definition of a promise the video does not keep.

```
BAD :  Scientists FOUND ALIENS!!!
GOOD:  Scientists Found a Strange Signal From Deep Space
```

Thumbnails: one clear subject, high contrast, very little text (1-4 words), no
fake claims. Three variants are generated and scored on contrast, exposure, text
economy, subject focus and honesty; overpromising words cost points even when
they came from the title.

---

## 6. Anti-spam

Automated publishing is exactly how a channel gets flagged, so:

| Guard | Default |
|---|---|
| Daily video limit | 3 |
| Minimum quality score | 80/100 |
| Duplicate topic detection | 0.80 similarity |
| Duplicate script detection | blocks upload at 0.80 |
| Repetitive title detection | angle overlap rejection |
| Consecutive similar videos before halting | 3 |

The system will **stop and require approval** rather than continue producing
near-identical videos.

---

## 7. Platform rules

Never: mass reupload, spam uploads, copying creators, misleading viewers,
automating fake engagement, buying views or subscribers, generating fake
comments, manipulating metrics, or evading platform restrictions. None of these
capabilities exist in the code, and a policy-risk flag is evaluated before every
publish.

Hard blocks in the quality gate: dangerous instructions, hate speech, content
sexualising minors, platform-manipulation offers, dangerous challenges.

---

## 8. AI disclosure

`youtube.synthetic_disclosure` defaults to **true**. Every description states
that the video was produced with AI assistance (script, synthetic narration,
generated visuals), and the preview screen shows the disclosure status before you
approve.

No attempt is made to hide AI-generated content. Where YouTube requires an
altered-or-synthetic-content declaration, declare it. The system is designed to
support that workflow, not evade it.

---

## 9. Where the human stays in the loop

APPROVAL mode is the default. You see the video, its title, description,
thumbnail, quality score, retention notes and blockers before anything is
published. AUTO mode is opt-in — and even then, blocking quality checks, kids
confirmation and high factual risk all still stop the upload and ask.
