<!-- Sampo's own playbook (T-4914), from the user's 2026-11-17 'no walls of text'. -->
# Write plainly

Use when writing anything a person reads: a README, a report, a research note, a PR body, a CHANGELOG entry, a
task's close.

## The rules

1. **Lead with the answer.** The first line says what happened or what to do. The reasons come after.
2. **A list or a table before a paragraph.** A set of things (options, findings, steps, numbers) is a list or a
   table, never a chain of sentences.
3. **Short paragraphs.** Three sentences, 80 words at most. After three prose paragraphs in a row, break with a
   list, a table, a figure or a heading.
4. **Short sentences, plain words.** One idea a sentence. The reader's words, not the code's. A term is defined at
   its first use.
5. **Depth goes elsewhere.** Detail lives in the docs, the task or a linked page; the summary links to it.

## Limits by kind

Counted, not judged: length is structure.

| What you wrote | Limit | Check |
|---|---|---|
| README | the rules above; an opening of 120 words before the first section | `sampo readme check` |
| Report, research note, PR body | a paragraph 80 words; 3 prose paragraphs in a row | `sampo readme check --prose PATH` |
| CHANGELOG entry | 80 words: what changed for the user | `sampo readme check --prose CHANGELOG.md` |
| Close fields | TITLE 15 words, LESSON 50, any other 80 | `sampo readme check --prose FILE` |

`sampo check` also names each wall in a research note written in the last day, as a warning.

## Before you hand it over

Run the check for what you wrote and fix each line it names.
