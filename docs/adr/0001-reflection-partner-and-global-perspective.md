# ADR 0001: Reflection Partner Positioning and Global Perspective

## Status

Accepted for `v0.2.0`.

## Context

Describing Unflincher primarily as a self-hosted AI journal hides its main value. The product reads
across an existing Journal Archive, finds patterns that are difficult to see entry by entry, points
back to dated entries, and lets the owner challenge the interpretation in Conversation.

The previous product language also assumed one fixed mentor persona. That language does not describe
the range of useful stances the product can support and can imply authority or a substitute
relationship that the product must not claim.

## Decision

Unflincher is positioned as an evidence-grounded AI reflection partner for people with years of
journal entries.

The product offers Companion, Coach, Challenger, and Analyst presets plus Custom instructions. It
uses one globally active Perspective for future Entry Reflections, Life Reports, and Conversations.
New installations start with Analyst. An existing active prompt remains byte-for-byte unchanged and
is classified as Custom during migration.

Entry Reflections preserve the point-in-time view available at the target entry. Their model
context contains the target entry plus entries earlier in canonical chronology, never later writing.
Life Reports and Journal Archive Conversations may use the complete archive.

Prompt Workshop remains the control surface. Selecting or editing a Perspective affects only future
generation. Apply remains save-only. Apply and regenerate all remains explicit and confirmed.

Import remains CLI-only in this release. The application explains the supported Douban Excel import
path and manual writing path, but does not add browser upload.

Unflincher is not therapy, diagnosis, or treatment. It does not present a Perspective as a licensed
professional, medical tool, or substitute relationship.

## Consequences

- User-facing language changes from AI Commentary to Entry Reflection and from Chat to Conversation.
- The assistant speaker label becomes Unflincher rather than AI Mentor.
- Perspective is a global setting, so changing it affects the next response in an existing
  Conversation without relabeling prior messages.
- Entry Reflections and their wellbeing scores cannot use knowledge from entries written after the
  target entry.
- Existing generated content is preserved and is not regenerated automatically.
- Public copy must describe dated entry references accurately without claiming independently
  verified or structured citations.
- Full-archive Life Reports and Journal Archive Conversations are limited by the selected model's
  context window and must fail clearly rather than silently omit entries.

## Rejected alternatives

### Keep one fixed mentor persona

This preserves the current implementation but contradicts the product's broader reflection role and
keeps authority-heavy language.

### Per-conversation Perspective

This adds prompt-version state to every Conversation and creates mixed-role history. It can be
reconsidered after the global model has real usage evidence.

### Rename Prompt Workshop

The existing name already describes a real customization capability and is familiar in the public
demo. The page will explain Perspectives inside the workshop instead.

### In-app Excel upload

This expands the release into file upload, validation, import progress, and production write
coordination. The existing CLI import remains the supported path for this release.
