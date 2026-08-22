import { useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react'
import { Download, ImagePlus, LoaderCircle, Play, Square, X } from 'lucide-react'
import PreviewVideo from '../../../PreviewVideo'
import {
  assetUrl,
  cancelXmaxSingleShotTask,
  createXmaxSingleShotTask,
  getLatestXmaxSingleShotTask,
  normalizeBackendAssetUrl,
  type Episode,
  type Project,
  type XmaxSingleShotTask,
  uploadXmaxSingleShotOutput,
} from '../../../api'
import XmaxSingleShotRunner, { type SingleShotRunnerStatus, type XmaxSingleShotRunnerDiagnostics } from './XmaxSingleShotRunner'

const DEFAULT_PROMPT = '视频中角色替换成参考图中角色，保持原始动作、镜头和背景。'

function sourceUrl(episode: Episode) {
  if (!episode.video_filename) return ''
  const suffix = episode.video_filename.toLowerCase().endsWith('.mov') ? '.mov' : '.mp4'
  return normalizeBackendAssetUrl(assetUrl(`/uploads/${episode.id}${suffix}`))
}

function versionedUrl(value?: string | null, version?: number) {
  const normalized = normalizeBackendAssetUrl(value)
  if (!normalized || !version) return normalized
  return `${normalized}${normalized.includes('?') ? '&' : '?'}v=${version}`
}

function readableStatus(status: SingleShotRunnerStatus | XmaxSingleShotTask['status'] | 'Ready' | 'Saving output' | 'Loading') {
  const labels: Record<string, string> = {
    Ready: 'Ready',
    Loading: 'Loading previous result',
    Preparing: 'Preparing',
    Connecting: 'Connecting',
    'Uploading reference': 'Uploading reference',
    'Starting transformation': 'Starting transformation',
    Running: 'Running',
    Recording: 'Recording',
    'Saving output': 'Saving output',
    Completed: 'Completed',
    Failed: 'Failed',
    'Persistence failed': 'Persistence failed',
    Cancelled: 'Cancelled',
    Stopping: 'Stopping',
    pending: 'Preparing',
    preparing: 'Preparing',
    running: 'Running',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
  }
  return labels[status] || status
}

export default function XmaxSingleShotPanel({ project, episode }: { project: Project; episode: Episode }) {
  const localContainerRef = useRef<HTMLDivElement>(null)
  const remoteContainerRef = useRef<HTMLDivElement>(null)
  const runnerRef = useRef<XmaxSingleShotRunner | null>(null)
  const taskRef = useRef<XmaxSingleShotTask | null>(null)
  const cancelSentRef = useRef(false)
  const cancelledByUserRef = useRef(false)
  const [referenceFile, setReferenceFile] = useState<File | null>(null)
  const [referencePreviewUrl, setReferencePreviewUrl] = useState('')
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT)
  const [status, setStatus] = useState<SingleShotRunnerStatus | 'Ready' | 'Saving output' | 'Loading'>('Loading')
  const [error, setError] = useState('')
  const [latestTask, setLatestTask] = useState<XmaxSingleShotTask | null>(null)
  const [diagnostics, setDiagnostics] = useState<XmaxSingleShotRunnerDiagnostics | null>(null)
  const [previewVersion, setPreviewVersion] = useState(0)
  const [busy, setBusy] = useState(false)
  const [localOutputUrl, setLocalOutputUrl] = useState('')
  const [localOutputSize, setLocalOutputSize] = useState(0)
  const [persistenceNotice, setPersistenceNotice] = useState('')
  const [savingOutput, setSavingOutput] = useState(false)
  const localOutputBlobRef = useRef<Blob | null>(null)
  const localOutputUrlRef = useRef('')
  const saveInFlightRef = useRef(false)
  const persistenceAttemptCountRef = useRef(0)

  const episodeSource = useMemo(() => sourceUrl(episode), [episode.id, episode.video_filename])
  const previewSource = versionedUrl(latestTask?.output_url, previewVersion)
  const hasValidDuration = typeof episode.duration === 'number' && episode.duration >= 5 && episode.duration <= 15
  const canStart = Boolean(episode.video_filename && episodeSource && hasValidDuration && referenceFile && prompt.trim() && !busy)

  const clearLocalOutput = (notice = '') => {
    if (localOutputUrlRef.current) URL.revokeObjectURL(localOutputUrlRef.current)
    localOutputUrlRef.current = ''
    localOutputBlobRef.current = null
    setLocalOutputUrl('')
    setLocalOutputSize(0)
    setPersistenceNotice(notice)
  }

  const retainLocalOutput = (blob: Blob) => {
    clearLocalOutput()
    const url = URL.createObjectURL(blob)
    localOutputBlobRef.current = blob
    localOutputUrlRef.current = url
    setLocalOutputUrl(url)
    setLocalOutputSize(blob.size)
  }

  useEffect(() => {
    let active = true
    setStatus('Loading')
    void getLatestXmaxSingleShotTask(episode.id).then((task) => {
      if (!active) return
      setLatestTask(task)
      taskRef.current = task
      setPrompt(task?.prompt || DEFAULT_PROMPT)
      if (task?.status === 'completed' && task.output_url) setPreviewVersion(Date.now())
      setStatus(task ? readableStatus(task.status) as SingleShotRunnerStatus : 'Ready')
    }).catch((reason) => {
      if (!active) return
      setStatus('Ready')
      setError(reason instanceof Error ? reason.message : String(reason))
    })
    return () => { active = false }
  }, [episode.id])

  useEffect(() => () => {
    if (localOutputUrlRef.current) URL.revokeObjectURL(localOutputUrlRef.current)
    localOutputUrlRef.current = ''
    localOutputBlobRef.current = null
  }, [])

  useEffect(() => {
    if (!referenceFile) {
      setReferencePreviewUrl('')
      return
    }
    const url = URL.createObjectURL(referenceFile)
    setReferencePreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [referenceFile])

  useEffect(() => () => {
    const runner = runnerRef.current
    if (!runner) return
    const task = taskRef.current
    void (async () => {
      await runner.stop('component_unmount')
      if (task && !cancelSentRef.current && task.status !== 'completed' && task.status !== 'cancelled') {
        cancelSentRef.current = true
        await cancelXmaxSingleShotTask(task.task_id).catch(() => undefined)
      }
    })()
  }, [])

  const handleReferenceChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] || null
    event.target.value = ''
    if (!file) return
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
      setError('请选择 PNG、JPEG 或 WebP 参考图。')
      return
    }
    setError('')
    setReferenceFile(file)
  }

  const persistOutput = async (task: XmaxSingleShotTask, blob: Blob, baseDiagnostics: XmaxSingleShotRunnerDiagnostics) => {
    if (saveInFlightRef.current) return false
    saveInFlightRef.current = true
    setSavingOutput(true)
    setStatus('Saving output')
    setError('')
    setPersistenceNotice('')
    const attempt = persistenceAttemptCountRef.current + 1
    persistenceAttemptCountRef.current = attempt
    const uploadDiagnostics = {
      ...baseDiagnostics,
      localBlobRetained: true,
      persistenceAttemptCount: attempt,
      remuxAttempted: false,
      remuxSucceeded: false,
      originalProbeError: null,
      remuxProbeError: null,
      events: [...baseDiagnostics.events, 'outputUploadStarted'],
    }
    setDiagnostics(uploadDiagnostics)
    try {
      const completed = await uploadXmaxSingleShotOutput(task.task_id, blob, uploadDiagnostics)
      taskRef.current = completed
      setLatestTask(completed)
      const persistenceDiagnostics = completed.persistence_diagnostics
      setDiagnostics({
        ...uploadDiagnostics,
        finalBlobDuration: completed.output_duration ?? null,
        remuxAttempted: persistenceDiagnostics?.remux_attempted ?? false,
        remuxSucceeded: persistenceDiagnostics?.remux_succeeded ?? false,
        originalProbeError: persistenceDiagnostics?.original_probe_error ?? null,
        remuxProbeError: persistenceDiagnostics?.remux_probe_error ?? null,
        events: [...uploadDiagnostics.events, 'outputUploadCompleted'],
      })
      setPreviewVersion(Date.now())
      setStatus('Completed')
      return true
    } catch (reason) {
      setStatus('Persistence failed')
      setError(reason instanceof Error ? reason.message : String(reason))
      setPersistenceNotice('转绘已经完成，结果仍可本地下载。保存到项目失败，可重新保存。')
      return false
    } finally {
      saveInFlightRef.current = false
      setSavingOutput(false)
    }
  }

  const handleStart = async () => {
    if (!canStart || !localContainerRef.current || !remoteContainerRef.current) return
    setError('')
    setBusy(true)
    cancelledByUserRef.current = false
    cancelSentRef.current = false
    if (localOutputUrlRef.current) clearLocalOutput('已清理上一条未保存的本地结果。')
    else setPersistenceNotice('')
    setLatestTask(null)
    setDiagnostics(null)
    setPreviewVersion(0)
    persistenceAttemptCountRef.current = 0
    setStatus('Preparing')
    try {
      const task = await createXmaxSingleShotTask(episode.id, prompt.trim(), referenceFile as File)
      taskRef.current = task
      setLatestTask(task)
      const runner = new XmaxSingleShotRunner({
        sourceUrl: task.source_url || episodeSource,
        referenceUrl: task.reference_url || '',
        sourceFilename: episode.video_filename || 'episode-source.mp4',
        sourceDuration: task.source_duration,
        prompt: task.prompt,
        localElement: localContainerRef.current,
        remoteElement: remoteContainerRef.current,
        onStatus: setStatus,
        onDiagnostics: setDiagnostics,
        onError: (reason) => setError(reason.message),
      })
      runnerRef.current = runner
      const result = await runner.run()
      setDiagnostics(result.diagnostics)
      if (cancelledByUserRef.current) {
        setStatus('Cancelled')
        return
      }
      if (!result.outputBlob || result.outputBlob.size <= 0) throw new Error('没有录制结果，无法保存 WebM。')
      retainLocalOutput(result.outputBlob)
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0))
      await persistOutput(task, result.outputBlob, result.diagnostics)
    } catch (reason) {
      if (cancelledByUserRef.current) {
        setStatus('Cancelled')
      } else {
        setStatus('Failed')
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      runnerRef.current = null
      setBusy(false)
    }
  }

  const handleRetrySave = async () => {
    const task = taskRef.current
    const blob = localOutputBlobRef.current
    if (!task || !blob || !diagnostics || saveInFlightRef.current) return
    await persistOutput(task, blob, diagnostics)
  }

  const handleCancel = async () => {
    const runner = runnerRef.current
    if (!runner || cancelSentRef.current) return
    cancelledByUserRef.current = true
    setStatus('Stopping')
    await runner.stop('cancelled')
    const task = taskRef.current
    if (task && !cancelSentRef.current) {
      cancelSentRef.current = true
      try {
        const cancelled = await cancelXmaxSingleShotTask(task.task_id)
        setLatestTask(cancelled)
        taskRef.current = cancelled
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    }
    setStatus('Cancelled')
    setBusy(false)
  }

  return <div className="xmax-single-shot-panel">
    <div className="xmax-single-shot-notice">当前为单人物、单镜头 5–15 秒 MVP。Xmax 按 Session 运行时间计费，预计 credits 仅供参考；运行中请勿刷新或重复点击。</div>
    <div className="xmax-single-shot-layout">
      <section className="xmax-single-shot-controls">
        <div className="panel-topline"><div><span className="section-label">SINGLE-SHOT XMAX</span><h2>Episode transformation</h2></div><span className={`xmax-single-shot-status status-${status.toLowerCase().replace(/\s+/g, '-')}`}>{readableStatus(status)}</span></div>
        <div className="xmax-single-shot-source">
          {episodeSource ? <video controls playsInline preload="metadata" src={episodeSource} /> : <div className="empty-video">未找到 Episode 源视频。</div>}
          <div className="xmax-single-shot-source-meta"><strong>{episode.title}</strong><span>{episode.duration?.toFixed(2) || '—'}s · {episode.video_filename || 'No source video'}</span></div>
        </div>
        <label className="xmax-single-shot-field"><span>Reference image</span><input type="file" accept="image/png,image/jpeg,image/webp" onChange={handleReferenceChange} disabled={busy} /><div className="xmax-single-shot-reference-upload"><ImagePlus size={16} /><span>{referenceFile?.name || 'Upload fictional adult reference'}</span></div></label>
        {referencePreviewUrl && <div className="xmax-single-shot-reference-preview"><img src={referencePreviewUrl} alt="Reference preview" /><button type="button" className="icon-button" title="Remove reference image" onClick={() => setReferenceFile(null)} disabled={busy}><X size={15} /></button></div>}
        <label className="xmax-single-shot-field"><span>Prompt</span><textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={3} disabled={busy} /></label>
        <div className="xmax-single-shot-actions"><button type="button" className="primary-button" onClick={() => void handleStart()} disabled={!canStart}><Play size={15} /> 开始转绘</button>{busy && runnerRef.current && <button type="button" className="secondary-button" onClick={() => void handleCancel()}><Square size={14} /> 取消</button>}</div>
        {!hasValidDuration && <p className="xmax-single-shot-hint">当前 MVP 仅支持 5–15 秒 Episode。</p>}
        {error && <div className="xmax-warning">{error}</div>}
      </section>
      <section className="xmax-single-shot-output">
        <div className="panel-topline"><div><span className="section-label">OUTPUT</span><h2>Realtime result</h2></div><span className="xmax-single-shot-status">{readableStatus(status)}</span></div>
        <div className="xmax-single-shot-sdk-stage"><div><span>Input</span><div ref={localContainerRef} className="xmax-single-shot-sdk-container" /></div><div><span>Output</span><div ref={remoteContainerRef} className="xmax-single-shot-sdk-container" /></div></div>
        {previewSource ? <><PreviewVideo src={previewSource} title="Xmax single-shot output" status="completed" versionLabel="Latest WebM" /><a className="primary-button xmax-single-shot-download" href={previewSource} download={latestTask?.output_url?.split('/').pop() || 'xmax-single-shot.webm'}><Download size={15} /> 下载 WebM</a></> : <div className="xmax-single-shot-empty-output"><LoaderCircle size={18} /> 完成后显示正式 WebM 结果</div>}
        <div className="xmax-single-shot-summary"><span>Estimated credits<strong>{latestTask?.estimated_credits?.toFixed(2) || (episode.duration?.toFixed(2) || '—')}</strong></span><span>Processed seconds<strong>{diagnostics?.processedSeconds?.toFixed(2) || latestTask?.processed_seconds?.toFixed(2) || '0.00'}</strong></span><span>Task<strong>{latestTask?.task_id || '—'}</strong></span></div>
        {latestTask?.status === 'failed' && <div className="xmax-warning">上次任务失败：{latestTask.error_message || latestTask.error_code || '未知错误'}</div>}
        {latestTask?.status === 'cancelled' && <div className="xmax-single-shot-hint">上次任务已取消，可以重新开始。</div>}
        {localOutputUrl && status === 'Persistence failed' && <div className="xmax-single-shot-local-output"><strong>本地原始 WebM 已保留</strong><span>{localOutputSize} bytes</span><video controls playsInline src={localOutputUrl} /><div className="xmax-single-shot-actions"><a className="secondary-button" href={localOutputUrl} download="xmax-single-shot-local.webm"><Download size={15} /> 下载本地原始 WebM</a><button type="button" className="primary-button" onClick={() => void handleRetrySave()} disabled={savingOutput}><Download size={15} /> {savingOutput ? '正在重新保存' : '重新保存结果'}</button></div></div>}
        {persistenceNotice && <div className="xmax-warning">{persistenceNotice}</div>}
        {diagnostics && <details className="xmax-single-shot-diagnostics"><summary>Frame and recording diagnostics</summary><div className="xmax-single-shot-diagnostics-grid"><span>Remote frames<strong>{diagnostics.remoteFrameCount}</strong></span><span>Max frame gap<strong>{diagnostics.remoteMaxFrameGapMs} ms</strong></span><span>Gaps &gt;100/250/500/1000ms<strong>{diagnostics.remoteGapsOver100Ms}/{diagnostics.remoteGapsOver250Ms}/{diagnostics.remoteGapsOver500Ms}/{diagnostics.remoteGapsOver1000Ms}</strong></span><span>Track<strong>{diagnostics.trackSettingsWidth || 'n/a'}x{diagnostics.trackSettingsHeight || 'n/a'} @ {diagnostics.trackSettingsFrameRate || 'n/a'} fps</strong></span><span>Track mute/unmute<strong>{diagnostics.trackMutedEvents}/{diagnostics.trackUnmutedEvents}</strong></span><span>Recorder<strong>{diagnostics.recorderChunkCount} chunks / {diagnostics.recorderTotalBytes} bytes</strong></span><span>MIME<strong>{diagnostics.recorderMimeType || 'n/a'}</strong></span><span>Blob<strong>{diagnostics.finalBlobSize} bytes / {diagnostics.finalBlobDuration ?? 'n/a'} s</strong></span></div></details>}
      </section>
    </div>
  </div>
}
