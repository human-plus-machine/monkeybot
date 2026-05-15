export type RedactedThinkingPlaceholderProps = {
  data: string
}

export default function RedactedThinkingPlaceholder(props: RedactedThinkingPlaceholderProps) {
  void props
  return (
    <div className="redacted-thinking" data-testid="redacted-thinking-placeholder">
      <p className="redacted-thinking-label">[redacted thinking]</p>
    </div>
  )
}
