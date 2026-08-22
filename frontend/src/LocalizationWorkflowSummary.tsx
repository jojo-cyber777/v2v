import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Check, FileText, Mic2, RefreshCw, SlidersHorizontal } from 'lucide-react'
import {
  api,
  type DubbingState,
  type TranscriptionSubtitleState,
} from './api'
import { revealAndScrollToModule, WORKFLOW_MODULE_IDS } from './workflowModuleNavigation'

type Props = {
  episodeId?: string
  sourceLanguage: string
  targetLanguage: string
}

function translationException(segment: TranscriptionSubtitleState['segments'][number]) {
  return (
    ['failed', 'stale'].includes(segment.translation_status)
    || (Boolean(segment.final_translation.trim()) && segment.translation_status !== 'completed')
  )
}

function voiceException(segment: DubbingState['segments'][number]) {
  const status = segment.output?.duration_fit_status
  return Boolean(
    ['failed', 'text_changed', 'timing_exception'].includes(segment.voice_status)
    ||
    segment.output?.error_code
    || status === 'manual_review_required'
    || status === 'failed',
  )
}

export default function LocalizationWorkflowSummary({ episodeId }: Props) {
  const [transcription, setTranscription] = useState<TranscriptionSubtitleState | null>(null)
  const [dubbing, setDubbing] = useState<DubbingState | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [availableTargets, setAvailableTargets] = useState<Record<string, boolean>>({})

  const load = useCallback(async () => {
    if (!episodeId) return
    setLoading(true)
    try {
      const [nextTranscription, nextDubbing] = await Promise.all([
        api<TranscriptionSubtitleState>(`/episodes/${episodeId}/transcription-subtitles`),
        api<DubbingState>(`/episodes/${episodeId}/dubbing`),
      ])
      setTranscription(nextTranscription)
      setDubbing(nextDubbing)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }, [episodeId])

  useEffect(() => {
    setTranscription(null)
    setDubbing(null)
    setError('')
    void load()
  }, [episodeId, load])

  useEffect(() => {
    const refresh = (event: Event) => {
      const detail = (event as CustomEvent<{ episodeId?: string }>).detail
      if (detail?.episodeId === episodeId) void load()
    }
    window.addEventListener('localization-assets-updated', refresh)
    return () => window.removeEventListener('localization-assets-updated', refresh)
  }, [episodeId, load])

  useEffect(() => {
    const update = () => setAvailableTargets(
      Object.fromEntries(Object.values(WORKFLOW_MODULE_IDS).map((id) => [id, Boolean(document.getElementById(id))])),
    )
    update()
    const observer = new MutationObserver(update)
    observer.observe(document.body, { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [episodeId])

  const translatedCount = transcription?.segments.filter(
    (segment) => segment.translation_status === 'completed' && Boolean(segment.final_translation.trim()),
  ).length || 0
  const untranslatedCount = transcription?.segments.filter((segment) => !segment.final_translation.trim()).length || 0
  const translationExceptions = useMemo(
    () => transcription?.segments.filter(translationException) || [],
    [transcription?.segments],
  )
  const voiceExceptions = useMemo(
    () => dubbing?.segments.filter(voiceException) || [],
    [dubbing?.segments],
  )
  const missingVoiceBindingCount = dubbing?.summary.missing_voice_speakers.length || 0
  const allMixIds = dubbing?.mix_eligible_segment_ids || []
  const subtitleReady = Boolean(
    transcription
    && !transcription.style.preview_stale
    && transcription.outputs.some((output) => output.output_type === 'subtitle_preview' && output.status === 'completed'),
  )
  const subtitleStatusText = subtitleReady ? '\u5b57\u5e55\u5df2\u5c31\u7eea' : '\u5b57\u5e55\u672a\u5c31\u7eea'
  const mixStatusText = dubbing?.audio_assets.formal_mix_ready
    ? '正式混音已就绪'
    : dubbing?.audio_assets.quick_preview_ready
      ? '快速预览已生成，正式混音未就绪'
      : dubbing?.audio_assets.stems_ready
        ? '背景音已就绪，正式混音未生成'
        : '正式混音未生成'

  if (!episodeId) return null

  return <section className="localization-workflow-summary">
    <div className="localization-workflow-heading">
      <div>
        <span className="section-label">AUDIO LOCALIZATION</span>
        <h2>翻译与配音</h2>
        <p>默认只显示完成度和异常；逐句文本、混音参数与时间轴位于高级调整。</p>
      </div>
      <button className="icon-button" title="刷新" disabled={loading} onClick={() => void load()}>
        <RefreshCw className={loading ? 'spin' : ''} size={15} />
      </button>
    </div>

    <div className="localization-workflow-steps">
      <details>
        <summary>
          <span className="localization-step-number">01</span>
          <FileText size={18} />
          <span><strong>真实转录</strong><small>{transcription?.segments.length || 0} 句 · {transcription?.transcription_summary.status || '未开始'}</small></span>
        </summary>
        <p>转录完成后，使用其全部 Speaker 构建声音映射。</p>
        <button
          className="secondary-button compact"
          disabled={!availableTargets[WORKFLOW_MODULE_IDS.transcription]}
          onClick={() => void revealAndScrollToModule(WORKFLOW_MODULE_IDS.transcription)}
        >前往真实转录</button>
      </details>

      <details open={translationExceptions.length > 0}>
        <summary>
          <span className="localization-step-number">02</span>
          <Check size={18} />
          <span><strong>一键翻译</strong><small>已完成 {translatedCount} · 未完成 {untranslatedCount} · 翻译异常 {translationExceptions.length}</small></span>
        </summary>
        <button
          className="secondary-button compact"
          disabled={!availableTargets[WORKFLOW_MODULE_IDS.translation]}
          onClick={() => void revealAndScrollToModule(WORKFLOW_MODULE_IDS.translation)}
        >前往一键翻译</button>
      </details>

      <details>
        <summary>
          <span className="localization-step-number">03</span>
          <AlertTriangle size={18} />
          <span><strong>时间轴高级调整</strong><small>翻译异常 {translationExceptions.length} · TTS异常 {voiceExceptions.length}</small></span>
        </summary>
        <p>在生成配音前保存最终译文、Speaker 和每句起止时间。</p>
        <button
          className="quiet-button compact"
          disabled={!availableTargets[WORKFLOW_MODULE_IDS.timeline]}
          onClick={() => void revealAndScrollToModule(WORKFLOW_MODULE_IDS.timeline)}
        >高级调整</button>
      </details>

      <details open={voiceExceptions.length > 0 || missingVoiceBindingCount > 0}>
        <summary>
          <span className="localization-step-number">04</span>
          <Mic2 size={18} />
          <span><strong>声音角色与批量 TTS</strong><small>Speaker {dubbing?.summary.speaker_count || 0} · 对白片段 {dubbing?.summary.dialogue_segment_count || 0} · 待绑定 {missingVoiceBindingCount} · TTS 异常 {voiceExceptions.length}</small></span>
        </summary>
        <button
          className="secondary-button compact"
          disabled={!availableTargets[WORKFLOW_MODULE_IDS.voice]}
          onClick={() => void revealAndScrollToModule(WORKFLOW_MODULE_IDS.voice)}
        >前往声音映射</button>
      </details>

      <details>
        <summary>
          <span className="localization-step-number">05</span>
          <SlidersHorizontal size={18} />
          <span><strong>混音预览</strong><small>可混音 {allMixIds.length} · {mixStatusText} · {subtitleStatusText}</small></span>
        </summary>
        <button
          className="secondary-button compact"
          disabled={!availableTargets[WORKFLOW_MODULE_IDS.mix]}
          onClick={() => void revealAndScrollToModule(WORKFLOW_MODULE_IDS.mix)}
        >前往混音</button>
      </details>

    </div>

    {(translationExceptions.length > 0 || voiceExceptions.length > 0) && <details className="localization-exceptions">
      <summary>异常详情 · 翻译 {translationExceptions.length} / TTS {voiceExceptions.length}</summary>
      {translationExceptions.map((segment) => <p key={`translation-${segment.id}`}>
        <strong>字幕 {segment.seq}</strong> · {segment.final_translation || segment.source_text} · {segment.translation_status}
      </p>)}
      {voiceExceptions.map((segment) => <p key={`voice-${segment.id}`}>
        <strong>配音 {segment.seq}</strong> · {segment.final_translation} · {segment.output?.error_code || segment.output?.duration_fit_status}
      </p>)}
    </details>}
    {error && <div className="inline-error"><AlertTriangle size={16} /><span>{error}</span></div>}
  </section>
}
