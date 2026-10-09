---
name: init
description: Use when starting a piece of work from a source, whether a tracker item, a spec file or document, or the user's own description, to turn it into a tm spec ready for design. Not for preparing a project for tm; that is `tm init`.
---

# Starting a piece of work

This turns a source into a starting position: a spec that holds the source and what it leaves open, the repos involved, the branch the work lands on, and the next step. `tm init` prepares a project for tm; this starts one piece of work in a project that is ready.

In tm the spec is the work's home. Its `source` and `context` sections hold what was asked, its decisions hold what only a person can answer, its `land_order` names the repos and its `land_on` the branch the work lands on. tm cuts its own worktree per claim, so nothing here checks a branch out.

Record, do not design. Nothing in this skill picks an approach, a shape or a file to change; that is the next stage's.

## 1. Ready check

Run `tm root` and `tm doctor`. When `tm root` finds no estate, or `tm doctor` exits non-zero, run `tm init` with the user and fix each missing piece it names before going on. `tm root` prints the project root every repo below is relative to.

## 2. Source

Read `tm guide intake` first: it says how to read a source and how to split its open questions. Then take the source in the form the user gave it.

- **A reference to an item in a tracker.** Read it through a connector the session has for that tracker: the body, every comment, every resolved thread, and every item and page it links, followed. On a defect, the rule being broken usually lives in a linked page. Count what you read against what the item says it holds. With no connector, stop and ask the user to paste the item and what it links. Never scrape a page behind a login, and never guess at what you could not read.
- **A file or document.** Read the whole of it, and every document it links that governs the work.
- **The user's own description, given with the call.** Take it as written. Ask only what `tm guide intake` says must be known before design, all in one message, and record the answers as part of the source.

## 3. Record

Create the spec, then write its sections from files:

```
tm spec add "<title>" --slug <SPEC-ID>
tm section set <spec-id>:source --file <source-file> --header "## Source"
tm section set <spec-id>:context --file <context-file> --header "## Context"
```

- `source` holds the source verbatim: every word as read, never translated, tidied or summarised, with where it came from on the first line. When the source is a file, also keep the file itself with `tm attach <spec-id> <file> --source <uri> --caption "<what it is>"`.
- `context` follows `references/spec-template.md`: what, why, scope in and out with the source line each comes from, contracts and constraints only as the source states them, and the repos. Where the source is silent, write "not stated in the source". Never fill a gap with a guess.

Then split the open questions:

- **The code answers it.** Read the code, answer it in `context` under its question, and cite `file:line`.
- **Only a person can answer it.** Raise each one, all at once, one question per decision, holding the spec:
  `tm decision add "<question>" --context-file <path> --option "a|Do X|why" --option "b|Do Y|why" --recommend a --blocks <spec-id>`

  Then write each question and the decision id tm printed into `context` under Open questions, and set the section again.

## 4. Repos

Propose the repos the work touches, from what the source names and from a search of the repositories under `tm root`. When the `codegraph` CLI is on PATH and a repo has a `.codegraph/` index, ask the index (`codegraph explore -p <repo> --max-files 5 "<what the source asks for>"`) before reading files. Show the list with the reason for each, and take the user's list over yours.

Validate every repo before any node names it. A repo is a directory under the root that is its own git working tree with an `origin` remote:

```
git -C <root>/<repo> rev-parse --show-toplevel   # prints <root>/<repo>
git -C <root>/<repo> remote get-url origin
```

`.` names the root itself when it is the repository. A repo that fails either check is reported to the user and left out: a repo missing from the list is never reviewed or landed. A repo with no `repos.<repo>.gates.main` (`tm config get repos.<repo>.gates.main`) stops every landing: set it with the user as `tm guide overview` describes. Record the validated list as the spec's landing order and in `context`:

```
tm task update <spec-id> --land-order <repo>,<repo>
```

## 5. Landing branch

Propose the spec's `land_on`: a work branch named from the source's id or title, such as `feature/<id>-<short-title>` or `fix/<id>-<short-title>`. Its base is each repo's default branch, freshly fetched:

```
tm config get repos.<repo>.default_branch
git -C <root>/<repo> fetch origin <default-branch>
git -C <root>/<repo> ls-remote --heads origin <branch>
git -C <root>/<repo> branch --list <branch>
```

When the branch exists already, locally or on `origin`, in any repo, it holds someone's work: show the user, and either land on it as it is or pick another name. Never move or delete an existing branch, and never check anything out over a dirty tree. Set it on the spec once the user agrees:

```
tm task update <spec-id> --set land_on=<branch>
```

tm cuts that branch from the default branch on the first landing; nothing here creates it.

## 6. Hand off

Report, in this order:

- the spec id;
- the validated repos;
- the landing branch, and whether it exists already;
- every open question verbatim, with its decision id, and the ones the code answered with their `file:line`;
- the next stage: `/taskmanager:design <spec-id>`, which settles the open questions and writes the design into the spec.

## Re-running

Re-running on an existing spec adds and never clobbers. Read what is there first with `tm section get <spec-id>`.

- **A missed repo.** Validate it as in step 4, then write the whole list back with `tm task update <spec-id> --land-order <old>,<new>` and add it to `context`.
- **A changed source.** Re-read it as in step 2. Keep the earlier `source` and add the new reading below it, dated, saying which one governs; re-capture an attached file with `tm attach <spec-id> <file> --source <uri> --replace <asset>`. Update only the `context` lines the change touches, and raise a decision for each new open question.
- **The landing branch.** Change `land_on` only when the user asks. Never move or delete an existing branch.
