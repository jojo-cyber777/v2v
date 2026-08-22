import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, Check, ChevronDown, Download, FileText, LoaderCircle, Merge, Play, Plus, RefreshCw, RotateCcw, Save, Scissors, Trash2, Upload } from 'lucide-react'
import { api, assetUrl, type AITask, type SubtitleStyle, type TranscriptSegment, type TranscriptionProviderOption, type TranscriptionSubtitleState } from './api'
import { stage3a1Text, useI18n } from './i18n'
import { revealAndScrollToModule, WORKFLOW_MODULE_IDS } from './workflowModuleNavigation'

type Props = { episodeId?: string; sourceLanguage: string; targetLanguage: string }
type Filter = 'all' | 'pending' | 'warning' | 'low_confidence' | 'edited' | 'translation_failed' | 'voice_missing'

const terminalTask = (task?: AITask) => !task || ['completed', 'failed', 'disabled', 'cancelled', 'interrupted'].includes(task.status)
const sourceLabel: Record<string, string> = { embedded_soft_subtitle: 'Soft', external_subtitle: 'SRT/VTT', burned_in_subtitle: 'Burned-in', speech_only: 'Speech', unknown: 'Unknown' }
const renderLabelKey: Record<string, string> = {
  replace_soft_subtitle: 'renderReplace',
  mask_burned_subtitle_and_add_target: 'renderMask',
  keep_original_and_add_target: 'renderKeep',
  target_only: 'renderTargetOnly',
  no_target_subtitle: 'renderNone',
}

function taskClass(status?: string) { return status ? `a1-status a1-status-${status}` : 'a1-status' }
function transcriptionProviderLabel(provider?: string | null) {
  if (provider === 'local_whisper_pyannote') return 'Local Whisper + Pyannote（本地免费）'
  if (provider === 'tencent') return 'Tencent ASR'
  if (provider === 'elevenlabs') return 'ElevenLabs'
  return provider || 'STT'
}
function taskStatusLabel(locale: string, status: string) {
  const labels: Record<string, [string, string]> = {
    pending: ['待处理', 'Pending'], running: ['正在运行', 'Running'], completed: ['已完成', 'Completed'],
    failed: ['失败', 'Failed'], cancelled: ['已取消', 'Cancelled'], interrupted: ['任务已中断', 'Task interrupted'],
  }
  return labels[status]?.[locale === 'zh-CN' ? 0 : 1] || status
}
function timeRange(segment: TranscriptSegment) { return `${(segment.start_ms / 1000).toFixed(2)}–${(segment.end_ms / 1000).toFixed(2)}s` }

export default function TranscriptionSubtitlesPanel({ episodeId, sourceLanguage, targetLanguage }: Props) {
  const { locale, formatDuration } = useI18n()
  const tx = (key: string) => stage3a1Text(locale, key)
  const [state, setState] = useState<TranscriptionSubtitleState | null>(null)
  const [segments, setSegments] = useState<TranscriptSegment[]>([])
  const [history, setHistory] = useState<TranscriptSegment[][]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [sourceChoice, setSourceChoice] = useState('')
  const [sourcePriority, setSourcePriority] = useState<'speech_transcription' | 'subtitle_source'>('speech_transcription')
  const [comparisonMode, setComparisonMode] = useState<'source_review' | 'side_by_side_review'>('source_review')
  const [translationStyle, setTranslationStyle] = useState('natural')
  const [style, setStyle] = useState<SubtitleStyle | null>(null)
  const [task, setTask] = useState<AITask | undefined>()
  const [filter, setFilter] = useState<Filter>('all')
  const [page, setPage] = useState(1)
  const [details, setDetails] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [selectedProvider, setSelectedProvider] = useState<TranscriptionProviderOption['id']>('tencent')
  const videoRef = useRef<HTMLVideoElement>(null)
  const stopTimer = useRef<number>()

  async function reload() {
    if (!episodeId) return
    const next = await api<TranscriptionSubtitleState>(`/episodes/${episodeId}/transcription-subtitles`)
    setState(next); setSegments(next.segments); setStyle(next.style); setTask(next.latest_task)
    window.dispatchEvent(new CustomEvent('localization-assets-updated', { detail: { episodeId } }))
    setSelectedProvider(next.selected_transcription_provider)
    setSourcePriority(next.source_priority === 'subtitle_source' ? 'subtitle_source' : 'speech_transcription')
    setComparisonMode(next.comparison_mode === 'side_by_side_review' ? 'side_by_side_review' : 'source_review')
    const activeSubtitle = next.sources.find((item) => item.selected && ['embedded_soft_subtitle', 'external_subtitle'].includes(item.source_type))
    setSourceChoice(activeSubtitle?.id || '')
    setSelected([]); setPage(1); setDirty(false)
  }

  useEffect(() => { setError(''); setState(null); setSegments([]); if (episodeId) reload().catch((reason) => setError((reason as Error).message)) }, [episodeId])
  useEffect(() => {
    if (!task || terminalTask(task)) return
    const timer = window.setInterval(async () => {
      try { const next = await api<AITask>(`/ai-tasks/${task.id}`); setTask(next); if (terminalTask(next)) await reload() } catch (reason) { setError((reason as Error).message) }
    }, 900)
    return () => window.clearInterval(timer)
  }, [task?.id, task?.status, episodeId])
  useEffect(() => () => { if (stopTimer.current) window.clearTimeout(stopTimer.current) }, [])

  async function run<T>(operation: () => Promise<T>, onDone?: (value: T) => void) {
    setLoading(true); setError(''); setMessage('')
    try { const value = await operation(); onDone?.(value) } catch (reason) { setError((reason as Error).message) } finally { setLoading(false) }
  }
  async function detect() { await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/subtitle-detect`, { method: 'POST' }), (next) => { setState(next); setSegments(next.segments); setTask(next.latest_task) }) }
  async function extractAudio() { await run(() => api<AITask>(`/episodes/${episodeId}/extract-audio`, { method: 'POST' }), setTask) }
  async function transcribe() {
    let overwrite = false
    if (state?.document) { overwrite = window.confirm(state.document.user_edited ? tx('overwriteEdited') : tx('overwriteUnedited')); if (!overwrite) return }
    await run(() => api<AITask>(`/episodes/${episodeId}/transcribe`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_language: sourceLanguage, overwrite_user_edits: overwrite, provider: selectedProvider }) }), setTask)
  }
  async function selectProvider(provider: TranscriptionProviderOption['id']) {
    await run(
      () => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/transcription-provider`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider }),
      }),
      (next) => {
        setState(next)
        setSelectedProvider(next.selected_transcription_provider)
        setMessage(locale === 'zh-CN' ? '当前剧集的转录方式已保存。' : 'The transcription provider was saved for this episode.')
      },
    )
  }
  async function uploadExternal(file?: File) {
    if (!file) return
    const form = new FormData(); form.append('file', file); form.append('select_source', 'false')
    await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/external-subtitles`, { method: 'POST', body: form }), (next) => { setState(next); setMessage(tx('uploadSaved')) })
  }
  async function applySource() {
    const overwrite = Boolean(state?.document?.user_edited && window.confirm(tx('overwrite')))
    if (state?.document?.user_edited && !overwrite) return
    const isComparison = comparisonMode === 'side_by_side_review'
    const body = isComparison ? { source_priority: 'side_by_side_review', comparison_mode: comparisonMode } : {
      source_priority: sourcePriority, source_id: sourcePriority === 'subtitle_source' ? sourceChoice : undefined,
      selected_source_mode: sourcePriority === 'subtitle_source' ? 'subtitle_primary' : 'speech_primary', comparison_mode: comparisonMode,
    }
    await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/subtitle-source`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...body, overwrite_user_edits: overwrite }) }), (next) => { setState(next); setSegments(next.segments); setMessage(tx('sourceSaved')) })
  }
  function editSegment(id: string, patch: Partial<TranscriptSegment>) { setHistory((old) => [...old.slice(-19), segments]); setSegments((old) => old.map((item) => item.id === id ? { ...item, ...patch } : item)); setDirty(true) }
  async function saveSegments() {
    const payload = segments.map(({ id, start_ms, end_ms, source_text, source_text_origin, speaker, segment_type, literal_translation, localized_translation, final_translation, translation_status, subtitle_enabled, review_status }) => ({ id, start_ms, end_ms, source_text, source_text_origin, speaker, segment_type, literal_translation, localized_translation, final_translation, translation_status, subtitle_enabled, review_status }))
    await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/transcript-segments`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ segments: payload }) }), (next) => { setState(next); setSegments(next.segments); setDirty(false); setMessage(tx('saved')) })
  }
  async function addSegment() {
    const start = segments.at(-1)?.end_ms || 0; const maxEnd = state?.video_duration ? Math.round(state.video_duration * 1000) : start + 2000
    await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/transcript-segments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ start_ms: start, end_ms: Math.min(start + 2000, maxEnd), source_text: locale === 'zh-CN' ? '新字幕片段' : 'New subtitle segment', source_text_origin: 'manual', speaker: 'Speaker 01', segment_type: 'dialogue', subtitle_enabled: true, review_status: 'pending' }) }), (next) => { setState(next); setSegments(next.segments) })
  }
  async function removeSegment(id: string) { if (!window.confirm(tx('removeConfirm'))) return; await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/transcript-segments/${id}`, { method: 'DELETE' }), (next) => { setState(next); setSegments(next.segments) }) }
  async function splitSegment(id: string) { await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/transcript-segments/${id}/split`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }), (next) => { setState(next); setSegments(next.segments) }) }
  async function mergeSegments() { if (selected.length < 2) return; await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/transcript-segments/merge`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ segment_ids: selected }) }), (next) => { setState(next); setSegments(next.segments); setSelected([]) }) }
  async function batchReview(action: string) { const count = action === 'process_exceptions' ? segments.filter(isException).length : segments.length; if (!window.confirm(`${tx('affected').replace('{{count}}', String(count))}\n${tx(action === 'process_exceptions' ? 'processExceptions' : action)}`)) return; await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/transcript-review/batch`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) }), (next) => { setState(next); setSegments(next.segments); setMessage(tx('saved')) }) }
  function undo() { const previous = history.at(-1); if (!previous) return; setSegments(previous); setHistory((old) => old.slice(0, -1)); setDirty(true) }
  function play(segment: TranscriptSegment) { const video = videoRef.current; if (!video) return; if (stopTimer.current) window.clearTimeout(stopTimer.current); video.currentTime = segment.start_ms / 1000; void video.play(); stopTimer.current = window.setTimeout(() => video.pause(), Math.max(100, segment.end_ms - segment.start_ms)) }
  async function translate() { await run(() => api<AITask>(`/episodes/${episodeId}/translate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_language: sourceLanguage, target_language: targetLanguage, translation_style: translationStyle, segment_ids: [] }) }), setTask) }
  async function exportFiles() { await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/subtitle-export`, { method: 'POST' }), (next) => { setState(next); setMessage(tx('saved')) }) }
  async function saveStyle() { if (!style) return; await run(() => api<TranscriptionSubtitleState>(`/episodes/${episodeId}/subtitle-style`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(style) }), (next) => { setState(next); setStyle(next.style); setMessage(tx('saved')) }) }
  async function preview() {
    if (!style) return
    await saveStyle()
    await run(() => api<AITask>(`/episodes/${episodeId}/subtitle-preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...style,
        render_mode: style.render_mode,
        target_language: targetLanguage,
        mask_enabled: style.render_mode === 'target_only',
        mask_height_ratio: style.mask_height_percent / 100,
      }),
    }), setTask)
  }

  const isException = (segment: TranscriptSegment) => segment.reading_speed_warning || (typeof segment.confidence === 'number' && segment.confidence < 0.75) || ['failed', 'stale'].includes(segment.translation_status) || !segment.final_translation.trim()
  const filteredSegments = useMemo(() => segments.filter((segment) => {
    if (filter === 'pending') return segment.review_status !== 'approved'
    if (filter === 'warning') return segment.reading_speed_warning || isException(segment)
    if (filter === 'low_confidence') return typeof segment.confidence === 'number' && segment.confidence < 0.75
    if (filter === 'edited') return segment.user_edited || segment.translation_edited
    if (filter === 'translation_failed') return segment.translation_status === 'failed'
    if (filter === 'voice_missing') return Boolean(segment.final_translation.trim()) && segment.stale_for_voice
    return true
  }), [segments, filter])
  const pageCount = Math.max(1, Math.ceil(filteredSegments.length / 20))
  const pageSegments = filteredSegments.slice((page - 1) * 20, page * 20)
  const outputs = useMemo(() => state?.outputs.filter((item, index, all) => item.status !== 'stale' && index === all.findIndex((other) => other.output_type === item.output_type && other.file_format === item.file_format && other.status !== 'stale')) || [], [state?.outputs])
  const transcriptionStatus = state?.transcription_summary.status
  const translatedCount = segments.filter((segment) => Boolean(segment.final_translation.trim()) && segment.translation_status === 'completed').length
  const untranslatedCount = segments.filter((segment) => !segment.final_translation.trim()).length
  const translationExceptionCount = segments.filter(
    (segment) => ['failed', 'stale'].includes(segment.translation_status)
      || (Boolean(segment.final_translation.trim()) && segment.translation_status !== 'completed'),
  ).length

  if (!episodeId) return <section className="settings-section a1-shell"><h2>{tx('title')}</h2><p className="muted-copy">{tx('episodeRequired')}</p></section>
  if (!state) return <section className="settings-section a1-shell"><LoaderCircle className="spin" size={18} /> {tx('title')}</section>
  return <section className="settings-section a1-shell">
    <div className="section-heading-inline"><div><span className="section-label">STAGE 3 / A1</span><h2>{tx('title')}</h2></div><span className="selection-count">REAL / NO MOCK FALLBACK</span></div>
    <p className="a1-notice">{tx('notice')}</p>
    {message && <div className="toast-line"><Check size={15} /> {message}</div>}
    {error && <div className="a1-error"><AlertTriangle size={15} /><div>{tx('operationFailed')}<details><summary>{tx('technical')}</summary><code>{error}</code></details></div></div>}
    <div className="a1-status-grid">
      <div><span>{tx('mediaTools')}</span><strong className={state.media_tools.available ? 'ok' : 'bad'}>{state.media_tools.available ? tx('available') : tx('unavailable')}</strong><small>ffmpeg: {state.media_tools.ffmpeg.version || 'not found'}<br />ffprobe: {state.media_tools.ffprobe.version || 'not found'}</small></div>
      <div><span>{tx('transcription')} {tx('provider')}</span><strong className={transcriptionStatus === 'failed' ? 'bad' : transcriptionStatus === 'real' ? 'ok' : 'muted'}>{tx(`status_${transcriptionStatus}`)}</strong><small>{transcriptionProviderLabel(state.transcription_provider.provider)} · {state.transcription_provider.model || '—'}{state.document?.provider ? <><br />Source: {transcriptionProviderLabel(state.document.provider)} · {state.document.model || '—'}</> : null}</small></div>
      <div><span>{tx('transcriptionMetrics')}</span><strong>{formatDuration(state.transcription_summary.audio_duration)}</strong><small>{tx('detectedLanguage')}: {state.transcription_summary.detected_language || '—'}<br />{tx('segments')}: {state.transcription_summary.segment_count} · {tx('speakers')}: {state.transcription_summary.speaker_count}</small></div>
      <div><span>{tx('translation')} {tx('provider')}</span><strong className={state.translation_provider.configured ? 'ok' : 'muted'}>{state.translation_provider.configured ? tx('configured') : tx('disabled')}</strong><small>{state.translation_provider.provider || 'DeepSeek'} · {state.translation_provider.model || '—'}</small></div>
    </div>

    <div className="a1-source-layout">
      <video ref={videoRef} className="a1-hidden-video" preload="metadata" src={assetUrl(state.video_url)} />
      <div className="a1-source-controls"><h3>{tx('sourcePriority')}</h3><div className="mode-row a1-mode-row"><button className={`seg-button ${sourcePriority === 'speech_transcription' && comparisonMode !== 'side_by_side_review' ? 'active' : ''}`} onClick={() => { setSourcePriority('speech_transcription'); setComparisonMode('source_review') }}>{tx('activeSpeech')}</button><button className={`seg-button ${sourcePriority === 'subtitle_source' && comparisonMode !== 'side_by_side_review' ? 'active' : ''}`} disabled={!state.subtitle_source_available} onClick={() => { setSourcePriority('subtitle_source'); setComparisonMode('source_review') }}>{tx('activeSubtitle')}</button><button className={`seg-button ${comparisonMode === 'side_by_side_review' ? 'active' : ''}`} onClick={() => setComparisonMode('side_by_side_review')}>{tx('activeSideBySide')}</button></div>
        {sourcePriority === 'subtitle_source' && <label className="field-label">{tx('source')}<select value={sourceChoice} onChange={(event) => setSourceChoice(event.target.value)}><option value="">{tx('notSelected')}</option>{state.sources.filter((item) => ['embedded_soft_subtitle', 'external_subtitle'].includes(item.source_type) && (item.stream_count > 0 || item.file_path)).map((item) => <option key={item.id} value={item.id}>{sourceLabel[item.source_type]} · {String(item.metadata.filename || item.language || item.stream_count || '')}</option>)}</select></label>}
        <div className="a1-button-row"><button className="secondary-button compact" onClick={detect} disabled={loading}><RefreshCw size={14} /> {tx('detect')}</button><label className="secondary-button compact"><Upload size={14} /> {tx('external')}<input type="file" accept=".srt,.vtt" onChange={(event) => uploadExternal(event.target.files?.[0])} /></label><button className="primary-button compact" onClick={applySource} disabled={loading || (sourcePriority === 'subtitle_source' && !state.subtitle_source_available)}>{tx('applySource')}</button></div>
        {!state.subtitle_source_available && <p className="a1-warning">{tx('subtitleUnavailable')}</p>}
        <p className="active-source-banner">{state.comparison_mode === 'side_by_side_review' ? tx('activeSideBySide') : state.source_priority === 'speech_transcription' ? tx('activeSpeech') : tx('activeSubtitle')}</p>
      </div>
    </div>

    <div id={WORKFLOW_MODULE_IDS.transcription} className="a1-action-band a1-transcription-provider workflow-module-anchor">
      <div>
        <h3>{tx('transcription')}</h3>
        <label className="field-label">STT Provider
          <select value={selectedProvider} disabled={loading} onChange={(event) => void selectProvider(event.target.value as TranscriptionProviderOption['id'])}>
            {state.transcription_providers.map((provider) => <option key={provider.id} value={provider.id} disabled={!provider.configured}>{provider.display_name}{provider.configured ? '' : ' · 未配置'}</option>)}
          </select>
        </label>
        {selectedProvider === 'local_whisper_pyannote' ? <div className="a1-local-provider-note">
          <strong>本地离线转录 · 不消耗云额度</strong>
          <span>支持中文、英文和多人 Speaker</span>
          <span>当前模型：faster-whisper medium</span>
          <span>Speaker 模型：pyannote Community-1</span>
          <span>当前设备：CPU int8</span>
        </div> : <p>{state.transcription_provider.configured ? `${transcriptionProviderLabel(state.transcription_provider.provider)} · ${state.transcription_provider.model}` : tx('providerDisabled')}</p>}
      </div>
      <div className="a1-button-row"><button className="secondary-button compact" onClick={extractAudio} disabled={loading || !state.media_tools.available}>{tx('extractAudio')}</button><button className="primary-button compact" onClick={transcribe} disabled={loading || !state.media_tools.available || !state.transcription_provider.configured}>{state.document ? tx('retranscribe') : tx('startTranscription')}</button></div>
    </div>
    {task && <div className="a1-task"><span>{tx('task')}</span><strong className={taskClass(task.status)}>{taskStatusLabel(locale, task.status)} · {task.stage || task.task_type}{task.status !== 'interrupted' && task.status !== 'failed' ? ` · ${task.progress}%` : ''}</strong>{task.error_message && <div><p>{task.error_code ? tx(`error_${task.error_code}`) : tx('operationFailed')}</p><details open={task.status === 'failed' || task.status === 'disabled' || task.status === 'interrupted'}><summary>{tx('technical')} · {task.error_code || 'UNKNOWN'}</summary><code>{task.error_message}{task.stderr ? `\n${task.stderr}` : ''}</code></details></div>}</div>}

    <div id={WORKFLOW_MODULE_IDS.translation} className="a1-action-band a1-translation-primary workflow-module-anchor">
      <div>
        <span className="section-label">TRANSLATION</span>
        <h3>{tx('translation')}</h3>
        <p>{state.translation_provider.configured ? `DeepSeek · ${state.translation_provider.model}${state.translation?.target_language ? ` · ${state.translation.target_language}` : ''}` : tx('translationProviderDisabled')}</p>
      </div>
      <div className="a1-translation-actions">
        <div className="a1-translation-counts" aria-label={locale === 'zh-CN' ? '翻译状态' : 'Translation status'}>
          <span><strong>{translatedCount}</strong>{locale === 'zh-CN' ? '已完成' : 'Completed'}</span>
          <span><strong>{untranslatedCount}</strong>{locale === 'zh-CN' ? '未完成' : 'Incomplete'}</span>
          <span className={translationExceptionCount ? 'bad' : ''}><strong>{translationExceptionCount}</strong>{locale === 'zh-CN' ? '翻译异常' : 'Exceptions'}</span>
        </div>
        <button className="primary-button compact" disabled={loading || !segments.length || !state.translation_provider.configured} onClick={() => translate()}>{tx('translateAll')}</button>
      </div>
    </div>

    <details id={WORKFLOW_MODULE_IDS.timeline} className="a1-timeline-advanced workflow-module-anchor">
      <summary>{locale === 'zh-CN' ? `高级调整 · 逐句原文、译文与时间轴（${segments.length}）` : `Advanced editing · source, translation and timeline (${segments.length})`}</summary>
      <div className="a1-timeline-advanced-body">
    <div className="a1-editor-head"><div><span className="section-label">{tx('transcriptView')}</span><h3>{tx('transcript')} · {segments.length}</h3></div><div className="a1-button-row"><button className="secondary-button compact" onClick={addSegment}><Plus size={14} /> {tx('addSegment')}</button><button className="secondary-button compact" onClick={undo} disabled={!history.length}><RotateCcw size={14} /> {tx('undo')}</button><button className="secondary-button compact" onClick={mergeSegments} disabled={selected.length < 2}><Merge size={14} /> {tx('merge')}</button><button className="primary-button compact" onClick={saveSegments} disabled={!dirty}><Save size={14} /> {tx('saveSegments')}</button></div></div>
    <div className="a1-batch-toolbar"><span>{tx('batchReview')}</span><button className="secondary-button compact" onClick={() => batchReview('approve_transcription')}>{tx('approveTranscription')}</button><button className="secondary-button compact" onClick={() => batchReview('adopt_recommended_translations')}>{tx('adoptTranslations')}</button><button className="secondary-button compact" onClick={() => batchReview('approve_no_warnings')}>{tx('approveNoWarnings')}</button><button className="secondary-button compact" onClick={() => batchReview('process_exceptions')}>{tx('processExceptions')}</button><button className="secondary-button compact" onClick={() => batchReview('save')}><Save size={14} /> {tx('batchSave')}</button></div>
    <div className="a1-filter-row"><span>{tx('filters')}</span>{([['all', 'all'], ['pending', 'pendingReview'], ['warning', 'warning'], ['low_confidence', 'lowConfidence'], ['edited', 'edited'], ['translation_failed', 'translationFailed'], ['voice_missing', 'voiceMissing']] as [Filter, string][]).map(([value, label]) => <button key={value} className={`seg-button ${filter === value ? 'active' : ''}`} onClick={() => { setFilter(value); setPage(1) }}>{tx(label)}</button>)}</div>
    {!segments.length ? <div className="empty-result">{tx('noSegments')}</div> : <>
      <div className="a1-segment-list">{pageSegments.map((segment) => <SegmentCard key={segment.id} segment={segment} selected={selected.includes(segment.id)} expanded={details.has(segment.id)} onSelect={(checked) => setSelected((old) => checked ? [...old, segment.id] : old.filter((id) => id !== segment.id))} onToggle={() => setDetails((old) => { const next = new Set(old); next.has(segment.id) ? next.delete(segment.id) : next.add(segment.id); return next })} onEdit={editSegment} onPlay={play} onSplit={splitSegment} onDelete={removeSegment} tx={tx} />)}</div>
      <div className="a1-pagination"><button className="secondary-button compact" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>{tx('previous')}</button><span>{tx('page')} {page}/{pageCount} · {filteredSegments.length}</span><button className="secondary-button compact" disabled={page >= pageCount} onClick={() => setPage((value) => value + 1)}>{tx('next')}</button></div>
    </>}
    <p className="a1-footnote">{tx('editStale')}</p>
      </div>
    </details>

    {style && <div className="a1-render"><div className="a1-editor-head"><div><span className="section-label">SUBTITLE RENDER</span><h3>{tx('render')}</h3></div></div><div className="a1-style-grid"><label className="field-label">{tx('renderMode')}<select value={style.render_mode} onChange={(event) => setStyle({ ...style, render_mode: event.target.value })}>{state.render_modes.map((value) => <option key={value} value={value}>{tx(renderLabelKey[value] || value)}</option>)}</select></label><label className="field-label">{tx('font')}<select value={style.font_family} onChange={(event) => setStyle({ ...style, font_family: event.target.value })}>{(state.available_fonts || []).map((font) => <option key={font.id} value={font.id}>{font.label}</option>)}</select></label><label className="field-label">{tx('size')}<input type="number" min="18" max="72" value={style.font_size} onChange={(event) => setStyle({ ...style, font_size: Math.max(18, Math.min(72, Number(event.target.value))) })} /></label><label className="field-label">{tx('position')}<select value={style.position} onChange={(event) => setStyle({ ...style, position: event.target.value })}><option value="bottom">Bottom</option><option value="middle">Middle</option><option value="top">Top</option></select></label><label className="field-label">{tx('safeMargin')}<input type="number" min="0" max="30" value={style.safe_margin_percent} onChange={(event) => setStyle({ ...style, safe_margin_percent: Math.max(0, Math.min(30, Number(event.target.value))) })} /></label><label className="field-label">{tx('maxLines')}<input type="number" min="1" max="4" value={style.max_lines} onChange={(event) => setStyle({ ...style, max_lines: Math.max(1, Math.min(4, Number(event.target.value))) })} /></label><label className="field-label">Text<input type="color" value={style.text_color.slice(0, 7)} onChange={(event) => setStyle({ ...style, text_color: event.target.value })} /></label><label className="field-label">Outline<input type="color" value={style.outline_color.slice(0, 7)} onChange={(event) => setStyle({ ...style, outline_color: event.target.value })} /></label></div>{style.render_mode === 'target_only' && <label className="field-label a1-mask-control">{tx('maskHeight')}<input type="number" min="10" max="35" value={style.mask_height_percent} onChange={(event) => setStyle({ ...style, mask_height_percent: Math.max(10, Math.min(35, Number(event.target.value))) })} /></label>}{style.font_fallback && <p className="a1-warning">{tx('fontFallback')}</p>}{style.render_mode === 'target_only' && <p className="a1-warning">{tx('maskWarning')}</p>}{style.render_mode === 'mask_burned_subtitle_and_add_target' && <p className="a1-warning">{tx('maskWarning')}</p>}<p className="a1-warning">{tx('burnedSubtitleNotice')}</p><div className="a1-button-row"><button className="secondary-button compact" onClick={saveStyle}><Save size={14} /> {tx('saveStyle')}</button><button className="secondary-button compact" onClick={exportFiles} disabled={!segments.length}><Download size={14} /> {tx('export')}</button><button className="primary-button compact" onClick={preview} disabled={!segments.length || !state.media_tools.available}><Play size={14} /> {tx('preview')}</button></div>{style.preview_stale && <p className="a1-warning">{tx('stalePreview')}</p>}</div>}

    <details className="a1-outputs"><summary><FileText size={14} /> {tx('outputs')}</summary>{outputs.length ? outputs.map((output) => <a key={output.id} href={assetUrl(output.file_url)} target="_blank" rel="noreferrer"><FileText size={14} /><span>{output.output_type}</span><strong>{output.file_format.toUpperCase()}</strong><Download size={13} /></a>) : <p>{tx('noOutputs')}</p>}</details>
    <div className="a1-next-stage"><span>{state.tts_provider.configured ? (locale === 'zh-CN' ? 'A2 已配置，可进入真实配音与混音。' : 'A2 is configured and ready for real dubbing & mix.') : (locale === 'zh-CN' ? 'A2 未配置：请检查 TTS Provider 配置。' : 'A2 disabled: check the TTS provider configuration.')}</span><button className="secondary-button compact" disabled={!state.tts_provider.configured} onClick={() => void revealAndScrollToModule(WORKFLOW_MODULE_IDS.voice)}>{locale === 'zh-CN' ? '进入配音与混音' : 'Open dubbing & mix'}</button></div>
  </section>
}

function isException(segment: TranscriptSegment) { return segment.reading_speed_warning || (typeof segment.confidence === 'number' && segment.confidence < 0.75) || ['failed', 'stale'].includes(segment.translation_status) || !segment.final_translation.trim() }

function SegmentCard({ segment, selected, expanded, onSelect, onToggle, onEdit, onPlay, onSplit, onDelete, tx }: { segment: TranscriptSegment; selected: boolean; expanded: boolean; onSelect: (checked: boolean) => void; onToggle: () => void; onEdit: (id: string, patch: Partial<TranscriptSegment>) => void; onPlay: (segment: TranscriptSegment) => void; onSplit: (id: string) => void; onDelete: (id: string) => void; tx: (key: string) => string }) {
  const warning = isException(segment)
  return <article className={`a1-segment ${warning ? 'has-warning' : ''}`}>
    <div className="a1-segment-index"><input type="checkbox" checked={selected} onChange={(event) => onSelect(event.target.checked)} /><strong>{String(segment.seq).padStart(3, '0')}</strong><button title={tx('jump')} onClick={() => onPlay(segment)}><Play size={12} /></button></div>
    <div className="a1-segment-main"><div className="a1-compact-row"><span className="a1-time-range">{timeRange(segment)}</span><span className="a1-speaker-chip">{segment.speaker_label || segment.speaker}</span><span className={segment.review_status === 'approved' ? 'ok' : 'warn'}>{segment.review_status === 'approved' ? tx('reviewApproved') : tx('reviewPending')}</span>{warning && <span className="warn"><AlertTriangle size={13} /> {tx('durationWarningShort')}</span>}</div><label className="field-label">{tx('sourceText')}<textarea value={segment.source_text} onChange={(event) => onEdit(segment.id, { source_text: event.target.value })} /></label><label className="field-label">{tx('finalTranslation')}<textarea value={segment.final_translation} onChange={(event) => onEdit(segment.id, { final_translation: event.target.value })} /></label><details open={expanded} onToggle={onToggle}><summary><ChevronDown size={14} /> {tx('details')}</summary><div className="a1-detail-grid"><label className="field-label">IN<input type="number" step="0.01" value={(segment.start_ms / 1000).toFixed(2)} onChange={(event) => onEdit(segment.id, { start_ms: Math.round(Number(event.target.value) * 1000) })} /></label><label className="field-label">OUT<input type="number" step="0.01" value={(segment.end_ms / 1000).toFixed(2)} onChange={(event) => onEdit(segment.id, { end_ms: Math.round(Number(event.target.value) * 1000) })} /></label><label className="field-label">Speaker<input value={segment.speaker} onChange={(event) => onEdit(segment.id, { speaker: event.target.value })} /></label><label className="field-label">Type<select value={segment.segment_type} onChange={(event) => onEdit(segment.id, { segment_type: event.target.value })}><option value="dialogue">Dialogue</option><option value="narration">Narration</option><option value="unknown">Unknown</option></select></label><label className="field-label">Literal<textarea value={segment.literal_translation} onChange={(event) => onEdit(segment.id, { literal_translation: event.target.value })} /></label><label className="field-label">Localized<textarea value={segment.localized_translation} onChange={(event) => onEdit(segment.id, { localized_translation: event.target.value })} /></label></div><div className="a1-segment-meta"><span>{tx('origin')}: {segment.source_text_origin}</span>{segment.timestamp_source && <span>{tx('timestampSource')}: {segment.timestamp_source}</span>}{typeof segment.confidence === 'number' && <span>{tx('confidence')}: {(segment.confidence * 100).toFixed(1)}%</span>}{segment.stale_for_voice && <span className="warn">{tx('stale')}</span>}<label><input type="checkbox" checked={segment.subtitle_enabled} onChange={(event) => onEdit(segment.id, { subtitle_enabled: event.target.checked })} /> {tx('enabled')}</label><select value={segment.review_status} onChange={(event) => onEdit(segment.id, { review_status: event.target.value })}><option value="pending">{tx('pending')}</option><option value="approved">{tx('approved')}</option></select></div></details></div><div className="a1-segment-actions"><button title={tx('playSegment')} onClick={() => onPlay(segment)}><Play size={14} /></button><button title={tx('split')} onClick={() => onSplit(segment.id)}><Scissors size={14} /></button><button title={tx('deleteSegment')} onClick={() => onDelete(segment.id)}><Trash2 size={14} /></button></div>
  </article>
}
