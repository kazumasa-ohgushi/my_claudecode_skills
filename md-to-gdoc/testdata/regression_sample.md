# Regression Sample — md-to-gdoc

Cases that previously broke. Keep each one; add new cases here when a bug is fixed.

## Inline formatting inside table cells

| Column | Content |
|---|---|
| **Revenue** *(USD)* | spaces between bold and italic must survive |
| Links | [first](https://example.com/a) [second](https://example.com/b) |
| Code | `a` and `b` |

## Blockquote shapes

Plain single line:

> Plain single-line quote.

Multi-paragraph:

> First paragraph of the quote.
>
> Second paragraph of the quote.

List only:

> - quoted bullet one
> - quoted bullet two

Intro paragraph followed by a list:

> Intro paragraph before the list.
>
> - item one
> - item two

## Nested sentinels

Inline code inside a blockquote (the inner inline-code deletes used to shift
the outer blockquote's close sentinel, leaving the close sentinel behind):

> A single-line blockquote with `inline code` inside.

> Multi-paragraph quote with `code_a` in the first paragraph.
>
> And `code_b` plus `code_c` in the last one.

> - quoted bullet with `inline code`
