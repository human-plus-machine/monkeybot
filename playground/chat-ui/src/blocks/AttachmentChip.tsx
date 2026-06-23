/** Attachment pill for composer pending list and frozen user-turn descriptors. */

export type AttachmentChipProps = {
  filename: string
  mimeType: string
  attachmentId?: string
  description?: string
  frozen?: boolean
  onRemove?: () => void
}

function mimeLabel(mime: string): string {
  if (mime.startsWith('image/')) return 'image'
  if (mime === 'application/pdf') return 'pdf'
  return mime.split('/').pop() ?? 'file'
}

function truncateTooltip(text: string, max = 200): string {
  const trimmed = text.trim()
  if (trimmed.length <= max) return trimmed
  return `${trimmed.slice(0, max - 1).trimEnd()}…`
}

export default function AttachmentChip({
  filename,
  mimeType,
  attachmentId,
  description,
  frozen = false,
  onRemove,
}: AttachmentChipProps) {
  const tooltip = description?.trim()
    ? truncateTooltip(description)
    : filename
  return (
    <span
      className={`attachment-chip${frozen ? ' attachment-chip--frozen' : ''}`}
      data-testid="attachment-chip"
      data-frozen={frozen ? 'true' : undefined}
      title={tooltip}
      aria-label={
        frozen
          ? `${filename}, attachment saved for this turn`
          : `${filename}, ${mimeLabel(mimeType)} attachment`
      }
    >
      <span className="attachment-chip-kind" aria-hidden>
        {mimeLabel(mimeType)}
      </span>
      <span className="attachment-chip-name">{filename}</span>
      {attachmentId ? (
        <span className="attachment-chip-id" title={attachmentId}>
          {attachmentId.slice(0, 12)}
          {attachmentId.length > 12 ? '…' : ''}
        </span>
      ) : null}
      {onRemove ? (
        <button
          type="button"
          className="attachment-chip-remove"
          aria-label={`Remove ${filename}`}
          onClick={onRemove}
        >
          ×
        </button>
      ) : null}
    </span>
  )
}
