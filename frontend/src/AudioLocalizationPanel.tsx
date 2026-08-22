import { useEffect, useMemo, useState } from 'react'
import { Check, Download, Languages, LoaderCircle } from 'lucide-react'

import { api, assetUrl, type AudioLocalizationState, type AudioLocalizationTask } from './api'


type Props = {
  episodeId?: string
  sourceTaskId?: string
}

const activeStatuses = new Set([
  'pending',
  'preparing_assets',
  'rendering_subtitles',
  'separating_audio',
  'building_tts_timeline',
  'mixing_audio',
  'muxing_final',
])

function taskLabel(task?: AudioLocalizationTask | null) {
  if (!task) return 'Ready'
  if (task.status === 'completed') return 'Completed'
  if (task.status === 'failed') return 'Failed'
  if (task.status === 'interrupted') return 'Interrupted'
  return task.progress_message || 'Processing'
}

export function AudioLocalizationPanel({ episodeId, sourceTaskId }: Props) {
  const [state, setState] = useState<AudioLocalizationState | null>(null)
  const [task, setTask] = useState<AudioLocalizationTask | null>(null)
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [confirmRegenerate, setConfirmRegenerate] = useState(false)
  const [error, setError] = useState('')

  const query = sourceTaskId ? `?source_task_id=${encodeURIComponent(sourceTaskId)}` : ''
  useEffect(() => {
    if (!episodeId) {
      setLoading(false)
      return
    }
    let active = true
    setLoading(true)
    api<AudioLocalizationState>(`/episodes/${episodeId}/audio-localization${query}`)
      .then((next) => {
        if (!active) return
        setState(next)
        setTask(next.latest_task || null)
        setError('')
      })
      .catch((reason) => { if (active) setError((reason as Error).message) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [episodeId, query])

  const running = Boolean(task && activeStatuses.has(task.status))
  useEffect(() => {
    if (!task || !activeStatuses.has(task.status)) return
    let active = true
    const poll = async () => {
      try {
        const next = await api<AudioLocalizationTask>(`/audio-localization/${task.id}`)
        if (!active) return
        setTask(next)
        if (!activeStatuses.has(next.status) && episodeId) {
          const refreshed = await api<AudioLocalizationState>(`/episodes/${episodeId}/audio-localization${query}`)
          if (active) setState(refreshed)
        }
      } catch (reason) {
        if (active) setError((reason as Error).message)
      }
    }
    const timer = window.setInterval(() => { void poll() }, 1500)
    void poll()
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [episodeId, query, task?.id, task?.status])

  const output = task?.status === 'completed' && task.output_url
    ? {
        output_url: task.output_url,
        created_at: task.completed_at,
        version: task.output_version || task.output_path,
        file_name: task.output_version,
      }
    : state?.latest_output
  const outputUrl = useMemo(() => {
    if (!output?.output_url) return ''
    const version = output.version || output.created_at || ''
    const value = output.output_url
    return `${assetUrl(value)}${value.includes('?') ? '&' : '?'}v=${encodeURIComponent(version)}`
  }, [output])
  const taskError = task && ['failed', 'interrupted'].includes(task.status)
    ? `${task.error_code || task.status.toUpperCase()}: ${task.error_message || task.progress_message}`
    : ''
  const durationFit = task?.duration_fit || state?.assets.duration_fit

  if (!episodeId) return null

  async function generate(confirmed = false) {
    if ((task?.status === 'completed' || state?.latest_output) && !confirmed) {
      setConfirmRegenerate(true)
      return
    }
    setConfirmRegenerate(false)
    setCreating(true)
    setError('')
    try {
      const next = await api<AudioLocalizationTask>(`/episodes/${episodeId}/audio-localization`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_task_id: sourceTaskId || null,
          target_language: 'english',
          voice: 'default',
          subtitles_enabled: true,
          hide_original_subtitles: true,
          original_subtitle_region_start_percent: state?.settings.original_subtitle_region_start_percent ?? 85.5,
          original_subtitle_region_height_percent: state?.settings.original_subtitle_region_height_percent ?? 14.5,
          subtitle_position: 'low',
        }),
      })
      setTask(next)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setCreating(false)
    }
  }

  return <section className="audio-localization-panel">
    <div className="audio-localization-heading">
      <div>
        <span className="section-label">AUDIO LOCALIZATION</span>
        <h2>Overseas audio version</h2>
        <p>Automatically preserves background music and ambient audio while replacing the original-language dialogue.</p>
      </div>
      <span className={`audio-localization-state ${state?.ready && !taskError ? 'ready' : ''}`}>
        {loading ? 'Checking assets' : taskLabel(task)}
      </span>
    </div>

    {loading ? <div className="audio-localization-loading"><LoaderCircle className="spin" size={16} /> Loading completed assets...</div> : <>
      <div className="audio-localization-controls audio-localization-controls-minimal">
        <label>
          <span>Target language</span>
          <select value="english" disabled onChange={() => undefined}>
            <option value="english">English</option>
          </select>
        </label>
      </div>

      {(running || task) && <div className="audio-localization-progress">
        <div>
          <strong>{taskLabel(task)}</strong>
          <span>{Math.max(0, Math.min(100, task?.progress || 0))}%</span>
        </div>
        <progress max="100" value={Math.max(0, Math.min(100, task?.progress || 0))} />
        <small>{task?.progress_message}</small>
      </div>}

      {(error || taskError) && <div className="audio-localization-error">{error || taskError}</div>}
      {confirmRegenerate && <div className="audio-localization-confirm">
        <span>A completed localized version already exists. A new run creates another file and keeps the current result.</span>
        <button type="button" onClick={() => setConfirmRegenerate(false)}>Keep current</button>
        <button type="button" className="primary-button" onClick={() => void generate(true)}>Generate new version</button>
      </div>}

      <div className="audio-localization-actions">
        <button className="primary-button" disabled={!state?.ready || running || creating} onClick={() => void generate(false)}>
          {running || creating ? <LoaderCircle className="spin" size={16} /> : <Languages size={16} />}
          {running || creating ? 'Generating localized version...' : 'Generate localized version'}
        </button>
        <span>Existing fitted voice assets are reused. Only lines that exceed their original slots are automatically shortened and regenerated; Xmax is never called.</span>
      </div>
      {durationFit && <div className={`audio-localization-fit ${durationFit.all_fit ? 'ready' : ''}`}>
        <strong>{durationFit.all_fit ? 'Voice timing fitted' : 'Voice timing check'}</strong>
        <span>{durationFit.fit_count ?? 0}/{durationFit.segment_count ?? state?.assets.translated_count ?? 0} lines fit their original slots</span>
      </div>}

      {task && <details className="audio-localization-advanced">
        <summary>Technical details</summary>
        <div className="audio-localization-technical">
          <span>Task</span><code>{task.id}</code>
          <span>Mode</span><code>{task.mode}</code>
          <span>Stage</span><code>{task.status}</code>
          <span>Translation calls</span><code>{task.translation_provider_calls}</code>
          <span>TTS calls</span><code>{task.tts_provider_calls}</code>
          <span>Blocked lines</span><code>{task.duration_fit?.blocked_segment_ids?.join(', ') || 'none'}</code>
          {task.error_code && <><span>Error</span><code>{task.error_code}</code></>}
        </div>
      </details>}
    </>}

    {outputUrl && <div className="audio-localization-output">
      <div className="audio-localization-output-head">
        <span><Check size={15} /> Localized version ready</span>
        <small>{output?.created_at ? `Completed ${output.created_at}` : 'Latest completed export'}</small>
      </div>
      <video key={outputUrl} controls playsInline preload="metadata" src={outputUrl} />
      <a className="primary-button" href={outputUrl} download={`localized-${episodeId}-en.mp4`}><Download size={16} /> Download localized MP4</a>
      {output?.file_name && <small className="audio-localization-output-file">{output.file_name}</small>}
    </div>}
  </section>
}

export default AudioLocalizationPanel
