# 6.H Attachments and `load_file` (`ATT`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route.

Existing: T1 `test_attachment_*.py`, `test_image_preview.py`, gateway `test_attachments.py`. T2 `tool_load_file.yaml` recording only.

| ID | Case | Asserts |
|---|---|---|
| T2-ATT-01 | `load_image_preview` | PNG via `path`: next request contains an image block of preview size; original bytes stay in the store. |
| T2-ATT-02 | `load_pdf` | PDF via `attachment_id`: file block in request. |
| T2-ATT-03 | `load_text_rejected` | `.py` path: error telling the model to use `read_file`. |
| T2-ATT-04 | `load_both_args` | `path` + `attachment_id`: error. |
| T2-ATT-05 | `load_per_reply_cap` | Six `load_file` calls in one turn all succeed. The five-attachment cap applies to a user's reply, not to the agent. |
| T2-ATT-06 | `freeze_descriptors_in_history` | After the turn, history holds descriptors, not media bytes. |
| T2-ATT-07 | `attachment_symlink_leaf_rejected` | Symlinked store leaf: rejected. |
| T2-ATT-G1 | `upload_then_reply_ref` (gateway) | `POST /attachments` then `/reply` with `attachmentRef`: `AttachmentDescriptor` at turn end. |
| T2-ATT-G2 | `upload_limits` (gateway) | 413 over size; 415 bad type; 404 when `ATTACHMENTS_ENABLED=false`; `INLINE_ATTACHMENT_NOT_ALLOWED`, `AMBIGUOUS_REPLY_BODY`, `ATTACHMENT_NOT_FOUND`. |
| T4-ATT-01 | `describe_image_live` (optional) | Model names the dominant color of a fixture image. |
