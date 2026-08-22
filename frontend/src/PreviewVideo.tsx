// Adapted preview interactions are documented in C:/CodexProjects/drama_ai_tool/THIRD_PARTY_NOTICES.md.
import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, Copy, Play, RefreshCw } from 'lucide-react'
import { assetUrl, normalizeBackendAssetUrl } from './api'
import { useI18n } from './i18n'

export type PreviewVideoProps = {
  src?: string | null
  poster?: string | null
  title?: string
  status?: string
  errorMessage?: string | null
  className?: string
  hoverPlay?: boolean
  showControls?: boolean
  muted?: boolean
  versionLabel?: string
}

function resolveAsset(value?: string | null) {
  const normalized = normalizeBackendAssetUrl(value)
  if (!normalized) return ''
  if (/^(data|blob|https?):/i.test(normalized) || normalized.startsWith('/')) return normalized
  return assetUrl(`/${normalized}`)
}

export default function PreviewVideo({
  src,
  poster,
  title,
  status,
  errorMessage,
  className,
  hoverPlay = false,
  showControls = true,
  muted = false,
  versionLabel,
}: PreviewVideoProps) {
  const { t } = useI18n()
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const copyTimerRef = useRef<number | undefined>(undefined)
  const resolvedSrc = resolveAsset(src)
  const resolvedPoster = resolveAsset(poster)
  const [mediaError, setMediaError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(Boolean(resolvedSrc))
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const normalizedStatus = status?.toLowerCase() || ''
  const isPending = ['loading', 'pending', 'queued', 'processing', 'rendering', 'generating'].includes(normalizedStatus)
  const statusError = ['failed', 'error'].includes(normalizedStatus) ? t('previewVideo.mediaError') : null
  const visibleError = errorMessage || mediaError || statusError
  const shellClassName = ['preview-video', className].filter(Boolean).join(' ')

  useEffect(() => {
    setMediaError(null)
    setIsLoading(Boolean(resolvedSrc))
    setCopyState('idle')
  }, [resolvedSrc])

  useEffect(() => () => {
    if (copyTimerRef.current !== undefined) window.clearTimeout(copyTimerRef.current)
  }, [])

  const handleVideoError = () => {
    setIsLoading(false)
    setMediaError(t('previewVideo.mediaError'))
  }

  const handleReload = () => {
    setMediaError(null)
    setIsLoading(Boolean(resolvedSrc))
    videoRef.current?.load()
  }

  const handleCopy = async () => {
    if (!resolvedSrc) return
    try {
      if (!navigator.clipboard) throw new Error('Clipboard API unavailable')
      await navigator.clipboard.writeText(resolvedSrc)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
    if (copyTimerRef.current !== undefined) window.clearTimeout(copyTimerRef.current)
    copyTimerRef.current = window.setTimeout(() => setCopyState('idle'), 2200)
  }

  const handleMouseEnter = () => {
    if (!hoverPlay || visibleError) return
    const video = videoRef.current
    if (!video) return
    if (!muted) video.muted = true
    void video.play().catch(() => {
      // Browser autoplay policy may block hover playback; native controls remain available.
    })
  }

  const handleMouseLeave = () => {
    if (hoverPlay) videoRef.current?.pause()
  }

  return <div className={shellClassName} title={title}>
    <div className="preview-video-stage" onMouseEnter={handleMouseEnter} onMouseLeave={handleMouseLeave}>
      {resolvedSrc ? <video
        ref={videoRef}
        className="preview-video-media"
        controls={showControls}
        muted={muted}
        playsInline
        preload="metadata"
        poster={resolvedPoster || undefined}
        src={resolvedSrc}
        aria-label={title}
        onLoadedData={() => setIsLoading(false)}
        onCanPlay={() => setMediaError(null)}
        onError={handleVideoError}
      /> : <div className="preview-video-empty" role="status">
        <Play size={20} aria-hidden="true" />
        <span>{t('previewVideo.empty')}</span>
      </div>}
      {versionLabel && <span className="preview-video-version">{versionLabel}</span>}
      {resolvedSrc && !visibleError && (isLoading || isPending) && <div className="preview-video-loading" role="status">
        <span className="preview-video-spinner" aria-hidden="true" />
        <span>{t('previewVideo.loading')}</span>
      </div>}
      {resolvedSrc && visibleError && <div className="preview-video-error" role="alert">
        <AlertTriangle size={20} aria-hidden="true" />
        <strong>{t('previewVideo.mediaError')}</strong>
        <span>{visibleError}</span>
        <div className="preview-video-error-actions">
          <button type="button" className="preview-video-action" onClick={handleReload}>
            <RefreshCw size={14} aria-hidden="true" /> {t('previewVideo.reload')}
          </button>
          <button type="button" className="preview-video-action" onClick={() => void handleCopy()}>
            <Copy size={14} aria-hidden="true" /> {copyState === 'copied' ? t('previewVideo.copied') : t('previewVideo.copy')}
          </button>
        </div>
      </div>}
    </div>
    {resolvedSrc && !visibleError && <div className="preview-video-actions">
      <button type="button" className="preview-video-action" onClick={() => void handleCopy()}>
        {copyState === 'copied' ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
        {copyState === 'copied' ? t('previewVideo.copied') : copyState === 'failed' ? t('previewVideo.copyFailed') : t('previewVideo.copy')}
      </button>
    </div>}
  </div>
}
