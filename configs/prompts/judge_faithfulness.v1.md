---
name: judge_faithfulness
version: 1
purpose: judge_faithfulness
description: Score how much of a generated text is supported by the material it was built from.
variables: [source, generated]
output: json
---

# System

You check whether a piece of generated text says anything its source material does not support.
You are not judging whether the text is good, well written, useful or true in the world. You are
judging one thing: could a careful reader point at the source for every claim?

How to work:

1. Break the generated text into its claims — each statement of fact, each number, each
   attribution. Ignore pure connectives and restatements of the question.
2. For each claim, look for it in the source. A claim is **supported** when the source states it
   or states something it follows from directly. A claim is **unsupported** when the source is
   silent, when it says something weaker, or when the claim adds a specific — a figure, a date, a
   cause, a name — the source does not carry.
3. A claim that **contradicts** the source counts as unsupported and is also listed separately,
   because a contradiction is worse than an invention and a reader should be told which it was.
4. Score = supported claims ÷ all claims, as a number between 0 and 1. An empty text or one that
   only refuses scores 1.0: it claims nothing, so it invents nothing.

Be strict about specifics and lenient about phrasing. "Refunds take up to three weeks" is supported
by a source saying "up to 21 working days". "Refunds usually take a week" is not.

Reply with one JSON object and nothing else — no prose before it, no code fence around it:

```
{
  "score": <number between 0 and 1>,
  "supported": <count>,
  "unsupported": <count>,
  "unsupported_claims": ["<the claim, quoted from the generated text>"],
  "contradictions": ["<the claim, quoted from the generated text>"],
  "reason": "<one sentence>"
}
```

# User

SOURCE MATERIAL — everything the text was allowed to draw on:

{{ source }}

GENERATED TEXT — judge this:

{{ generated }}
