import { useEffect, useState } from 'react'
import { api, assetUrl, type DubbingState, type TranscriptionSubtitleState } from './api'
import { stage3a1Text, stage3a2Text, useI18n } from './i18n'
import PreviewVideo from './PreviewVideo'

export default function CurrentEffectPreview({ episodeId }: { episodeId?: string }) {
  const { locale } = useI18n()
  const a1 = (key: string) => stage3a1Text(locale, key)
  const a2 = (key: string) => stage3a2Text(locale, key)
  const [tab, setTab] = useState<'subtitle' | 'dubbing'>('subtitle')
  const [subtitle, setSubtitle] = useState<TranscriptionSubtitleState | null>(null)
  const [dubbing, setDubbing] = useState<DubbingState | null>(null)

  useEffect(() => {
    if (!episodeId) return
    let active = true
    const reload = async () => {
      const [nextSubtitle, nextDubbing] = await Promise.all([
        api<TranscriptionSubtitleState>(`/episodes/${episodeId}/transcription-subtitles`).catch(() => null),
        api<DubbingState>(`/episodes/${episodeId}/dubbing`).catch(() => null),
      ])
      if (!active) return
      setSubtitle(nextSubtitle)
      setDubbing(nextDubbing)
    }
    void reload()
    const refresh = (event: Event) => {
      const detail = (event as CustomEvent<{ episodeId?: string }>).detail
      if (detail?.episodeId === episodeId) void reload()
    }
    window.addEventListener('localization-assets-updated', refresh)
    return () => {
      active = false
      window.removeEventListener('localization-assets-updated', refresh)
    }
  }, [episodeId])

  if (!episodeId) return null
  const subtitleCandidates = subtitle?.outputs
    .filter((item) => item.output_type === 'subtitle_preview' && item.status === 'completed')
    .sort((left, right) => String(right.created_at).localeCompare(String(left.created_at))) || []
  const latestSubtitleOutput = subtitleCandidates[0]
  const subtitleOutput = latestSubtitleOutput && subtitle?.style?.render_mode === 'target_only' && latestSubtitleOutput.metadata?.render_mode !== 'target_only'
    ? undefined
    : latestSubtitleOutput
  const dubbingOutput = dubbing?.dubbed_output?.preview_video_url ? assetUrl(dubbing.dubbed_output.preview_video_url) : ''
  const versionedUrl = (url: string, version?: string) => url ? `${url}${url.includes('?') ? '&' : '?'}v=${encodeURIComponent(version || String(Date.now()))}` : ''
  const currentVideo = tab === 'dubbing'
    ? versionedUrl(dubbingOutput, dubbing?.dubbed_output?.created_at || dubbing?.dubbed_output?.id)
    : versionedUrl(subtitleOutput?.file_url ? assetUrl(subtitleOutput.file_url) : '', subtitleOutput?.created_at || subtitleOutput?.id)
  const renderMode = subtitle?.style?.render_mode === 'target_only' ? a1('renderTargetOnly') : subtitle?.style?.render_mode || a1('notSelected')
  return <section className="current-effect-preview settings-section">
    <div className="section-heading-inline"><div><span className="section-label">A1 / A2</span><h2>{locale === 'zh-CN' ? '\u5f53\u524d\u6548\u679c\u9884\u89c8' : 'Current effect preview'}</h2></div><div className="mode-row"><button className={`seg-button ${tab === 'subtitle' ? 'active' : ''}`} onClick={() => setTab('subtitle')}>{a1('subtitlePreviewTab')}</button><button className={`seg-button ${tab === 'dubbing' ? 'active' : ''}`} onClick={() => setTab('dubbing')}>{a2('dubbingPreviewTab')}</button></div></div>
    <div className="current-effect-meta">{tab === 'subtitle' ? <span>{a1('renderMode')}: {renderMode} · {subtitle?.translation?.target_language || a1('notSelected')} {subtitle?.style?.preview_stale ? ` · ${a1('stalePreview')}` : ''}</span> : <span>{a2('previewOutput')}: {dubbingOutput ? a2('completed') : a2('noPreviewYet')}</span>}</div>
    <div className="current-effect-player"><PreviewVideo src={currentVideo || null} title={locale === 'zh-CN' ? '\u5f53\u524d\u6548\u679c\u9884\u89c8' : 'Current effect preview'} status={currentVideo ? 'completed' : 'empty'} hoverPlay muted versionLabel={tab === 'dubbing' ? a2('previewOutput') : a1('subtitlePreviewTab')} /></div>
  </section>
}
