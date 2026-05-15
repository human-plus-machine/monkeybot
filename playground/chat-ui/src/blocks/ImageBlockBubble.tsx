export type ImageBlockBubbleProps = {
  mime_type: string
  /** Raw base64 (no `data:` prefix). */
  data: string
  alt?: string
}

export default function ImageBlockBubble({ mime_type, data, alt = 'Assistant image' }: ImageBlockBubbleProps) {
  const src = `data:${mime_type};base64,${data}`
  return (
    <div className="image-block-inline" data-testid="image-block-bubble">
      <img src={src} alt={alt} className="image-block-img" />
    </div>
  )
}
