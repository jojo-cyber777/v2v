import { useEffect, useMemo, useRef, useState } from 'react'
import { Download, Film, LoaderCircle, Sparkles, X } from 'lucide-react'
import { api, assetUrl, normalizeBackendAssetUrl, type Character, type Episode, type Project, type XmaxAttempt, type XmaxBatch, type XmaxShot } from '../../../api'
import { XmaxRealtimeAdapter, releaseXmaxResult, type XmaxLifecycleMarker, type XmaxProviderAudit, type XmaxRealtimeResult, type XmaxRealtimeStatus, type XmaxSessionDiagnostics, type XmaxStopReason } from './XmaxRealtimeAdapter'
import { browserXmaxCredentialConfigured } from './xmaxBrowserCredential'

const FIXED_PROMPT = '视频中角色替换成参考图中角色'
const API_BASE = (import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_URL || 'http://127.0.0.1:8001').replace(/\/$/, '')
type PipelineStatus = XmaxBatch['status'] | 'idle' | 'preparing' | 'starting' | 'running' | 'failed' | 'cancelled' | 'completed' | 'stopping'

type StartupError = {
  generateButtonClicked: string
  batchPrepared: string
  shotPipelineEntered: string
  shotPipelineShotId: string
  adapterCalled: string
  adapterMethodName: string
  sdkDiagnosticsWrapperEntered: string
  stage: string
  apiUrl: string
  method: string
  httpStatus: string
  errorCode: string
  errorMessage: string
  batchId: string
  shotId: string
  sessionId: string
  browserError: string
  originalReferenceUrl: string
  normalizedReferenceUrl: string
  responseContentType: string
  blobSize: string
  viaViteProxy: string
  decodeError: string
  originalShotUrl: string
  normalizedShotUrl: string
  fetchUrl: string
  fetchStatus: string
  videoContentType: string
  videoBlobSize: string
  readyState: string
  networkState: string
  videoDuration: string
  videoWidth: string
  videoHeight: string
  mediaErrorCode: string
  mediaErrorMessage: string
  currentSrcType: string
  objectUrlRevoked: string
  sdkResultFieldNames: string
  sessionObjectCreated: string
  getSessionUidCalled: string
  getSessionUidResolved: string
  rawUidType: string
  extractedSessionId: string
  sessionSavedToBackend: string
  outputStreamReceived: string
  outputVideoTrackCount: string
  activeConfirmationSource: string
  activeWaitMs: string
  stopAttempted: string
  stopCompleted: string
  stopMethod: string
  stopReason: string
  inputVideoPlayCalled: string
  inputVideoPlayResolved: string
  inputVideoPaused: string
  inputVideoCurrentTimeStart: string
  inputVideoCurrentTimeEnd: string
  videoFrameCallbackCount: string
  presentedFrames: string
  captureStreamCreated: string
  captureVideoTrackCount: string
  captureTrackReadyState: string
  captureTrackMuted: string
  sdkCreateCalled: string
  sdkCreateStartedAt: string
  sdkCreateResolved: string
  sdkCreateRejected: string
  createSessionWaitMs: string
  resultType: string
  resultConstructorName: string
  resultFieldNames: string
  resultMethodNames: string
  resultHasGetSessionUid: string
  resultHasStop: string
  resultHasClose: string
  resultHasDisconnect: string
  errorName: string
  rawErrorCode: string
  rawErrorMessage: string
  errorStackHead: string
  providerRequestMethod: string
  providerRequestUrl: string
  providerHttpStatus: string
  providerResponseBody: string
  providerErrorName: string
  providerErrorCode: string
  providerErrorMessage: string
  providerErrorStackHead: string
  cleanupCompleted: string
  prewarmStreamCreated: string
  prewarmTrackCount: string
  prewarmStreamStopped: string
  finalStreamCreated: string
  finalStreamActive: string
  finalVideoTrackCount: string
  finalTrackId: string
  finalTrackReadyState: string
  finalTrackEnabled: string
  finalTrackMuted: string
  sdkReceivedFinalStream: string
  videoDecoderReady: string
  videoPlayResolved: string
  canvasCreated: string
  canvasContextCreated: string
  canvasWidth: string
  canvasHeight: string
  canvasCaptureFps: string
  canvasDrawCount: string
  firstCanvasFrameTime: string
  lastCanvasFrameTime: string
  canvasStreamCreated: string
  canvasStreamActive: string
  canvasVideoTrackCount: string
  canvasTrackId: string
  canvasTrackKind: string
  canvasTrackReadyState: string
  canvasTrackEnabled: string
  canvasTrackMuted: string
  sdkReceivedCanvasStream: string
  finalInputStreamActive: string
  finalInputVideoTrackCount: string
  finalInputTrackId: string
  finalInputTrackReadyState: string
  finalInputTrackEnabled: string
  finalInputTrackMuted: string
  finalInputTrackSettings: string
  localElementTagName: string
  remoteElementTagName: string
  canvasDrawCountBeforeConnect: string
  videoCurrentTimeBeforeConnect: string
}

type ReferenceImageLoadResult = {
  blob: Blob
  originalUrl: string
  normalizedUrl: string
  contentType: string
  status: string
  viaViteProxy: boolean
  decoded: boolean
}

type ReferenceImageFailure = Error & Partial<StartupError> & { availableFields?: string[]; code?: string; diagnostics?: XmaxSessionDiagnostics }
type ShotVideoFailure = Error & Partial<StartupError>

type PipelineMarkers = {
  generateButtonClicked: boolean
  batchPrepared: boolean
  shotPipelineEntered: boolean
  shotPipelineShotId: string
  adapterCalled: boolean
  adapterMethodName: string
  sdkDiagnosticsWrapperEntered: boolean
  sdkCreateCalled: boolean
}

function emptyPipelineMarkers(): PipelineMarkers {
  return {
    generateButtonClicked: false,
    batchPrepared: false,
    shotPipelineEntered: false,
    shotPipelineShotId: '',
    adapterCalled: false,
    adapterMethodName: '',
    sdkDiagnosticsWrapperEntered: false,
    sdkCreateCalled: false,
  }
}

function episodeSource(episode?: Episode) {
  if (!episode?.video_filename) return ''
  const suffix = episode.video_filename.toLowerCase().endsWith('.mov') ? '.mov' : '.mp4'
  return assetUrl(`/uploads/${episode.id}${suffix}`)
}

function referenceUrl(character?: Character) {
  const value = character?.target_reference_path || character?.main_reference_image || character?.source_reference_image || character?.original_reference_image || character?.reference_assets?.find((asset) => asset.is_primary)?.image
  if (!value) return ''
  return normalizeBackendAssetUrl(value)
}

function statusLabel(status: PipelineStatus, zh: boolean) {
  const labels: Partial<Record<PipelineStatus, [string, string]>> = {
    idle: ['待开始', 'Ready'], prepared: ['已准备', 'Prepared'], starting: ['正在启动 Shot 001', 'Starting Shot 001'], pending: ['待处理', 'Pending'], running: ['处理中', 'Running'], preparing: ['正在准备 Shot 批次', 'Preparing shot batch'], splitting: ['镜头分段中', 'Splitting'], processing: ['处理中', 'Processing'], recording: ['录制中', 'Recording'], merging: ['合并并恢复原声', 'Merging'], completed: ['已完成', 'Completed'], stopped: ['已停止', 'Stopped'], failed: ['失败', 'Failed'], interrupted: ['任务已中断', 'Task interrupted'], cancelled: ['已取消', 'Cancelled'],
  }
  if (status === 'stopping') return zh ? '正在停止...' : 'Stopping...'
  return labels[status]?.[zh ? 0 : 1] || status
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function apiRequestUrl(path: string) {
  return `${API_BASE}/api${path}`
}

function startupError(stage: string, error: unknown, context: Partial<StartupError> = {}): StartupError {
  const text = errorText(error)
  const line = (label: string) => text.match(new RegExp(`${label}: ([^\\n]*)`))?.[1]?.trim() || ''
  let errorCode = ''
  let errorMessage = text.split('\\n')[0] || 'Xmax startup failed.'
  const body = line('response body')
  if (body) {
    try {
      const payload = JSON.parse(body) as { detail?: string | { code?: string; message?: string } }
      const detail = payload.detail
      if (typeof detail === 'object' && detail) {
        errorCode = detail.code || ''
        errorMessage = detail.message || errorMessage
      } else if (typeof detail === 'string') errorMessage = detail
    } catch {
      // Keep a plain-text response readable.
    }
  }
  return {
    generateButtonClicked: context.generateButtonClicked || '',
    batchPrepared: context.batchPrepared || '',
    shotPipelineEntered: context.shotPipelineEntered || '',
    shotPipelineShotId: context.shotPipelineShotId || '',
    adapterCalled: context.adapterCalled || '',
    adapterMethodName: context.adapterMethodName || '',
    sdkDiagnosticsWrapperEntered: context.sdkDiagnosticsWrapperEntered || '',
    stage,
    apiUrl: context.apiUrl || line('API URL'),
    method: context.method || line('HTTP method'),
    httpStatus: context.httpStatus || line('status code'),
    errorCode: context.errorCode || errorCode,
    errorMessage: context.errorMessage || errorMessage,
    batchId: context.batchId || '',
    shotId: context.shotId || '',
    sessionId: context.sessionId || '',
    browserError: context.browserError || (text.startsWith('Network request failed') ? text.split('response body: ')[1]?.split('\\n')[0] || text : ''),
    originalReferenceUrl: context.originalReferenceUrl || '',
    normalizedReferenceUrl: context.normalizedReferenceUrl || '',
    responseContentType: context.responseContentType || '',
    blobSize: context.blobSize || '',
    viaViteProxy: context.viaViteProxy || '',
    decodeError: context.decodeError || '',
    originalShotUrl: context.originalShotUrl || '',
    normalizedShotUrl: context.normalizedShotUrl || '',
    fetchUrl: context.fetchUrl || '',
    fetchStatus: context.fetchStatus || '',
    videoContentType: context.videoContentType || '',
    videoBlobSize: context.videoBlobSize || '',
    readyState: context.readyState || '',
    networkState: context.networkState || '',
    videoDuration: context.videoDuration || '',
    videoWidth: context.videoWidth || '',
    videoHeight: context.videoHeight || '',
    mediaErrorCode: context.mediaErrorCode || '',
    mediaErrorMessage: context.mediaErrorMessage || '',
    currentSrcType: context.currentSrcType || '',
    objectUrlRevoked: context.objectUrlRevoked || '',
    sdkResultFieldNames: context.sdkResultFieldNames || '',
    sessionObjectCreated: context.sessionObjectCreated || '',
    getSessionUidCalled: context.getSessionUidCalled || '',
    getSessionUidResolved: context.getSessionUidResolved || '',
    rawUidType: context.rawUidType || '',
    extractedSessionId: context.extractedSessionId || '',
    sessionSavedToBackend: context.sessionSavedToBackend || '',
    outputStreamReceived: context.outputStreamReceived || '',
    outputVideoTrackCount: context.outputVideoTrackCount || '',
    activeConfirmationSource: context.activeConfirmationSource || '',
    activeWaitMs: context.activeWaitMs || '',
    stopAttempted: context.stopAttempted || '',
    stopCompleted: context.stopCompleted || '',
    stopMethod: context.stopMethod || '',
    stopReason: context.stopReason || '',
    inputVideoPlayCalled: context.inputVideoPlayCalled || '',
    inputVideoPlayResolved: context.inputVideoPlayResolved || '',
    inputVideoPaused: context.inputVideoPaused || '',
    inputVideoCurrentTimeStart: context.inputVideoCurrentTimeStart || '',
    inputVideoCurrentTimeEnd: context.inputVideoCurrentTimeEnd || '',
    videoFrameCallbackCount: context.videoFrameCallbackCount || '',
    presentedFrames: context.presentedFrames || '',
    captureStreamCreated: context.captureStreamCreated || '',
    captureVideoTrackCount: context.captureVideoTrackCount || '',
    captureTrackReadyState: context.captureTrackReadyState || '',
    captureTrackMuted: context.captureTrackMuted || '',
    sdkCreateCalled: context.sdkCreateCalled || '',
    sdkCreateStartedAt: context.sdkCreateStartedAt || '',
    sdkCreateResolved: context.sdkCreateResolved || '',
    sdkCreateRejected: context.sdkCreateRejected || '',
    createSessionWaitMs: context.createSessionWaitMs || '',
    resultType: context.resultType || '',
    resultConstructorName: context.resultConstructorName || '',
    resultFieldNames: context.resultFieldNames || '',
    resultMethodNames: context.resultMethodNames || '',
    resultHasGetSessionUid: context.resultHasGetSessionUid || '',
    resultHasStop: context.resultHasStop || '',
    resultHasClose: context.resultHasClose || '',
    resultHasDisconnect: context.resultHasDisconnect || '',
    errorName: context.errorName || '',
    rawErrorCode: context.rawErrorCode || '',
    rawErrorMessage: context.rawErrorMessage || '',
    errorStackHead: context.errorStackHead || '',
    providerRequestMethod: context.providerRequestMethod || '',
    providerRequestUrl: context.providerRequestUrl || '',
    providerHttpStatus: context.providerHttpStatus || '',
    providerResponseBody: context.providerResponseBody || '',
    providerErrorName: context.providerErrorName || '',
    providerErrorCode: context.providerErrorCode || '',
    providerErrorMessage: context.providerErrorMessage || '',
    providerErrorStackHead: context.providerErrorStackHead || '',
    cleanupCompleted: context.cleanupCompleted || '',
    prewarmStreamCreated: context.prewarmStreamCreated || '',
    prewarmTrackCount: context.prewarmTrackCount || '',
    prewarmStreamStopped: context.prewarmStreamStopped || '',
    finalStreamCreated: context.finalStreamCreated || '',
    finalStreamActive: context.finalStreamActive || '',
    finalVideoTrackCount: context.finalVideoTrackCount || '',
    finalTrackId: context.finalTrackId || '',
    finalTrackReadyState: context.finalTrackReadyState || '',
    finalTrackEnabled: context.finalTrackEnabled || '',
    finalTrackMuted: context.finalTrackMuted || '',
    sdkReceivedFinalStream: context.sdkReceivedFinalStream || '',
    videoDecoderReady: context.videoDecoderReady || '',
    videoPlayResolved: context.videoPlayResolved || '',
    canvasCreated: context.canvasCreated || '',
    canvasContextCreated: context.canvasContextCreated || '',
    canvasWidth: context.canvasWidth || '',
    canvasHeight: context.canvasHeight || '',
    canvasCaptureFps: context.canvasCaptureFps || '',
    canvasDrawCount: context.canvasDrawCount || '',
    firstCanvasFrameTime: context.firstCanvasFrameTime || '',
    lastCanvasFrameTime: context.lastCanvasFrameTime || '',
    canvasStreamCreated: context.canvasStreamCreated || '',
    canvasStreamActive: context.canvasStreamActive || '',
    canvasVideoTrackCount: context.canvasVideoTrackCount || '',
    canvasTrackId: context.canvasTrackId || '',
    canvasTrackKind: context.canvasTrackKind || '',
    canvasTrackReadyState: context.canvasTrackReadyState || '',
    canvasTrackEnabled: context.canvasTrackEnabled || '',
    canvasTrackMuted: context.canvasTrackMuted || '',
    sdkReceivedCanvasStream: context.sdkReceivedCanvasStream || '',
    finalInputStreamActive: context.finalInputStreamActive || '',
    finalInputVideoTrackCount: context.finalInputVideoTrackCount || '',
    finalInputTrackId: context.finalInputTrackId || '',
    finalInputTrackReadyState: context.finalInputTrackReadyState || '',
    finalInputTrackEnabled: context.finalInputTrackEnabled || '',
    finalInputTrackMuted: context.finalInputTrackMuted || '',
    finalInputTrackSettings: context.finalInputTrackSettings || '',
    localElementTagName: context.localElementTagName || '',
    remoteElementTagName: context.remoteElementTagName || '',
    canvasDrawCountBeforeConnect: context.canvasDrawCountBeforeConnect || '',
    videoCurrentTimeBeforeConnect: context.videoCurrentTimeBeforeConnect || '',
  }
}

function PipelineNotice({ children }: { children: React.ReactNode }) {
  return <div className="demo-notice"><Sparkles size={15} /><span>{children}</span></div>
}

export default function XmaxShotPipelinePanel({ project, onChange }: { project: Project; onChange: (project: Project) => void }) {
  const episodeId = new URLSearchParams(window.location.search).get('episode_id') || project.active_episode_id
  const episode = project.episodes?.find((item) => item.id === episodeId)
  const source = episodeSource(episode) || (project.video_filename ? assetUrl(`/uploads/${project.id}${project.video_filename.toLowerCase().endsWith('.mov') ? '.mov' : '.mp4'}`) : '')
  const referenceCharacter = project.characters.find((item) => item.confirmed && item.priority_level === 'Locked' && referenceUrl(item))
  const referenceImage = referenceUrl(referenceCharacter)
  const configured = browserXmaxCredentialConfigured()
  const { locale } = { locale: localStorage.getItem('locale') === 'zh-CN' ? 'zh-CN' : 'en' } as { locale: 'zh-CN' | 'en' }
  const zh = locale === 'zh-CN'
  const inputRef = useRef<HTMLVideoElement>(null)
  const inputObjectUrlRef = useRef<string | null>(null)
  const outputRef = useRef<HTMLVideoElement>(null)
  const localContainerRef = useRef<HTMLDivElement>(null)
  const remoteContainerRef = useRef<HTMLDivElement>(null)
  const previewRef = useRef<HTMLVideoElement>(null)
  const adapterRef = useRef<XmaxRealtimeAdapter | null>(null)
  const stopRequestedRef = useRef(false)
  const stopReasonRef = useRef<XmaxStopReason | null>(null)
  const sessionActiveRef = useRef(false)
  const sessionIdRef = useRef<string | null>(null)
  const sessionDiagnosticsRef = useRef<XmaxSessionDiagnostics | null>(null)
  const attemptRef = useRef<XmaxAttempt | null>(null)
  const pipelineMarkersRef = useRef<PipelineMarkers>(emptyPipelineMarkers())
  const backendStopSentRef = useRef(false)
  const mountedRef = useRef(true)
  const batchRef = useRef<XmaxBatch | null>(null)
  const [batch, setBatch] = useState<XmaxBatch | null>(null)
  const [status, setStatus] = useState<PipelineStatus>('idle')
  const [currentShot, setCurrentShot] = useState(0)
  const [processedSeconds, setProcessedSeconds] = useState(0)
  const [sessionSeconds, setSessionSeconds] = useState(0)
  const [sourceDuration, setSourceDuration] = useState(episode?.duration || project.video_duration || 0)
  const [error, setError] = useState('')
  const [prepareNotice, setPrepareNotice] = useState('')
  const [currentRunError, setCurrentRunError] = useState<StartupError | null>(null)
  const [historicalRun, setHistoricalRun] = useState<XmaxBatch | null>(null)
  const [latestAttempt, setLatestAttempt] = useState<XmaxAttempt | null>(null)
  const startupFailure = currentRunError
  const [, setPipelineMarkers] = useState<PipelineMarkers>(emptyPipelineMarkers)
  const [style, setStyle] = useState(project.settings.style_id || 'western-live-action')
  const [targetLanguage, setTargetLanguage] = useState(project.settings.target_language || 'english')
  const [shotCharacterIds, setShotCharacterIds] = useState<Record<string, string[]>>({})

  const styleLabel = useMemo(() => style, [style])
  const languageLabel = useMemo(() => targetLanguage, [targetLanguage])

  function markPipeline(marker: keyof PipelineMarkers, value?: string) {
    const next = { ...pipelineMarkersRef.current }
    if (marker === 'shotPipelineShotId' || marker === 'adapterMethodName') next[marker] = value || '' as never
    else next[marker] = true as never
    pipelineMarkersRef.current = next
    if (mountedRef.current) setPipelineMarkers(next)
  }

  function markAdapterLifecycle(marker: XmaxLifecycleMarker, value?: string) {
    if (marker === 'adapter_called') markPipeline('adapterCalled')
    else if (marker === 'adapter_method_name') markPipeline('adapterMethodName', value)
    else if (marker === 'sdk_diagnostics_wrapper_entered') markPipeline('sdkDiagnosticsWrapperEntered')
    else if (marker === 'sdk_create_called') markPipeline('sdkCreateCalled')
  }

  function updateAttempt(patch: Record<string, unknown>) {
    const attempt = attemptRef.current
    const attemptId = attempt?.id
    if (!attemptId) return
    if (attempt && ['completed', 'failed', 'cancelled'].includes(attempt.status)) {
      if (patch.status && patch.status !== attempt.status) return
      if (patch.stop_reason === 'page-hidden') return
    }
    void api<XmaxAttempt>(`/xmax-attempts/${attemptId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) })
      .then((next) => { attemptRef.current = next; if (mountedRef.current) setLatestAttempt(next) })
      .catch(() => undefined)
  }

  function diagnosticsAttemptPatch(diagnostics: XmaxSessionDiagnostics | null, providerAudit?: XmaxProviderAudit) {
    if (!diagnostics && !providerAudit) return {}
    return {
      session_id: diagnostics?.extractedSessionId || sessionIdRef.current || undefined,
      provider_request_method: diagnostics?.providerRequestMethod || providerAudit?.requestMethod || undefined,
      provider_request_url: diagnostics?.providerRequestUrl || providerAudit?.requestUrl || undefined,
      provider_http_status: diagnostics?.providerHttpStatus ?? providerAudit?.httpStatus ?? undefined,
      provider_response_body: diagnostics?.providerResponseBody || providerAudit?.responseBody || undefined,
      provider_error_name: diagnostics?.errorName || providerAudit?.errorName || undefined,
      provider_error_code: diagnostics?.providerErrorCode || providerAudit?.errorCode || diagnostics?.errorCode || undefined,
      provider_error_message: diagnostics?.providerErrorMessage || providerAudit?.errorMessage || diagnostics?.errorMessage || undefined,
      provider_error_stack_head: diagnostics?.errorStackHead || providerAudit?.errorStackHead || undefined,
      output_stream_received: diagnostics?.outputStreamReceived || false,
      processed_seconds: Number.isFinite(diagnostics?.inputVideoCurrentTimeEnd) ? diagnostics?.inputVideoCurrentTimeEnd : undefined,
      final_input_stream_active: diagnostics?.finalInputStreamActive,
      final_input_video_track_count: diagnostics?.finalInputVideoTrackCount,
      final_input_track_id: diagnostics?.finalInputTrackId || undefined,
      final_input_track_ready_state: diagnostics?.finalInputTrackReadyState || undefined,
      final_input_track_enabled: diagnostics?.finalInputTrackEnabled === '' ? undefined : diagnostics?.finalInputTrackEnabled === 'true',
      final_input_track_muted: diagnostics?.finalInputTrackMuted === '' ? undefined : diagnostics?.finalInputTrackMuted === 'true',
      final_input_track_settings: diagnostics?.finalInputTrackSettings || undefined,
      local_element_tag_name: diagnostics?.localElementTagName || undefined,
      remote_element_tag_name: diagnostics?.remoteElementTagName || undefined,
      canvas_draw_count_before_connect: diagnostics?.canvasDrawCountBeforeConnect,
      video_current_time_before_connect: diagnostics?.videoCurrentTimeBeforeConnect,
    }
  }

  useEffect(() => {
    mountedRef.current = true
    if (!episodeId) return () => { mountedRef.current = false }
    void Promise.all([
      api<XmaxBatch | null>(`/episodes/${episodeId}/xmax-batches/latest`),
      api<XmaxAttempt | null>(`/episodes/${episodeId}/xmax-attempts/latest`),
    ]).then(([latest, attempt]) => {
      if (!mountedRef.current) return
      if (attempt) { attemptRef.current = attempt; setLatestAttempt(attempt) }
      if (!latest) return
      batchRef.current = latest
      setBatch(latest)
      const isHistoricalFailure = latest.status === 'failed' || latest.status === 'interrupted'
      setStatus(isHistoricalFailure ? 'idle' : latest.status)
      setProcessedSeconds(latest.processed_seconds || 0)
      if (isHistoricalFailure) setHistoricalRun(latest)
      setShotCharacterIds(Object.fromEntries(latest.shots.map((shot) => [shot.id, shot.character_ids?.length ? shot.character_ids : (referenceCharacter ? [referenceCharacter.id] : [])])))
    }).catch(() => undefined)
    return () => {
      mountedRef.current = false
      stopRequestedRef.current = true
      void adapterRef.current?.dispose('component-unmount')
      releaseInputObjectUrl()
    }
  }, [episodeId])

  useEffect(() => {
    const stopForPageLifecycle = (reason: XmaxStopReason, flags: Record<string, boolean> = {}) => {
      const attempt = attemptRef.current
      if (!attempt || !['prepared', 'starting', 'running'].includes(attempt.status)) return
      stopRequestedRef.current = true
      stopReasonRef.current = reason
      updateAttempt({ status: 'cancelled', stop_reason: reason, ...flags, request_end_at: new Date().toISOString() })
      void adapterRef.current?.finishStop(reason)
      void requestBackendStop(reason)
    }
    const onVisibilityChange = () => { if (document.visibilityState === 'hidden') stopForPageLifecycle('page-hidden', { page_hidden: true, tab_hidden: true }) }
    const onBeforeUnload = () => stopForPageLifecycle('page-hidden', { page_hidden: true, unload: true })
    document.addEventListener('visibilitychange', onVisibilityChange)
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('beforeunload', onBeforeUnload)
    }
  }, [])

  function saveSelection() {
    const settings = { ...project.settings, style_id: style, target_language: targetLanguage }
    onChange({ ...project, settings, selected_style: style, target_language: targetLanguage })
    return api(`/projects/${project.id}/settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings) })
  }

  async function requestBackendStop(reason: XmaxStopReason | string) {
    const batchId = batchRef.current?.id
    if (!batchId || backendStopSentRef.current) return null
    backendStopSentRef.current = true
    return apiWithTimeout<XmaxBatch>(`/xmax-batches/${batchId}/stop`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason }) }, 3000).catch(() => null)
  }

  async function waitForLocalCleanup(cleanup: Promise<unknown>, timeoutMs = 3000) {
    let timer: number | undefined
    try {
      return await Promise.race([
        cleanup.then(() => false, () => false),
        new Promise<boolean>((resolve) => { timer = window.setTimeout(() => resolve(true), timeoutMs) }),
      ])
    } finally {
      if (timer) window.clearTimeout(timer)
    }
  }

  function releaseInputObjectUrl() {
    if (inputObjectUrlRef.current) URL.revokeObjectURL(inputObjectUrlRef.current)
    inputObjectUrlRef.current = null
  }

  async function fetchWithTimeout(url: string, init?: RequestInit, timeoutMs = 10000) {
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), timeoutMs)
    try {
      return await fetch(url, { ...init, signal: controller.signal })
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') throw new Error(`Request timed out after ${timeoutMs / 1000} seconds: ${url}`)
      throw reason
    } finally {
      window.clearTimeout(timer)
    }
  }

  async function apiWithTimeout<T>(path: string, init: RequestInit, timeoutMs = 10000) {
    let timer: number | undefined
    try {
      return await Promise.race([
        api<T>(path, init),
        new Promise<T>((_, reject) => { timer = window.setTimeout(() => { const failure = new Error(`Request timed out after ${timeoutMs / 1000} seconds: ${path}`) as Error & { apiUrl?: string; method?: string; httpStatus?: string }; failure.apiUrl = apiRequestUrl(path); failure.method = init.method || 'GET'; failure.httpStatus = '0'; reject(failure) }, timeoutMs) }),
      ])
    } finally {
      if (timer) window.clearTimeout(timer)
    }
  }

  function videoDiagnostics(video: HTMLVideoElement, extra: Record<string, string> = {}) {
    const mediaError = video.error
    return {
      readyState: String(video.readyState),
      networkState: String(video.networkState),
      videoDuration: Number.isFinite(video.duration) ? String(video.duration) : 'unknown',
      videoWidth: String(video.videoWidth || 0),
      videoHeight: String(video.videoHeight || 0),
      mediaErrorCode: mediaError ? String(mediaError.code) : '',
      mediaErrorMessage: mediaError?.message || '',
      currentSrcType: video.currentSrc.startsWith('blob:') ? 'blob' : video.currentSrc ? (video.currentSrc.startsWith('/') ? 'same-origin' : 'external') : '',
      ...extra,
    }
  }

  function videoReady(video: HTMLVideoElement, objectUrl: string) {
    return video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
      && video.videoWidth > 0
      && video.videoHeight > 0
      && Number.isFinite(video.duration)
      && video.duration > 0
      && !video.error
      && Boolean(video.currentSrc)
      && video.currentSrc === objectUrl
      && inputObjectUrlRef.current === objectUrl
  }

  function waitForVideo(video: HTMLVideoElement, objectUrl: string) {
    if (videoReady(video, objectUrl)) return Promise.resolve()
    return new Promise<void>((resolve, reject) => {
      let settled = false
      const timer = window.setTimeout(() => {
        const invalidMetadata = video.videoWidth <= 0 || video.videoHeight <= 0 || !Number.isFinite(video.duration) || video.duration <= 0
        finish(new Error(invalidMetadata ? 'Shot video metadata did not become valid within 20 seconds.' : 'Shot video decode timed out after 20 seconds.'), invalidMetadata ? 'shot_video_invalid_metadata' : 'decode_shot_video_timeout')
      }, 20000)
      const cleanup = () => {
        window.clearTimeout(timer)
        video.removeEventListener('loadedmetadata', check)
        video.removeEventListener('loadeddata', check)
        video.removeEventListener('canplay', check)
        video.removeEventListener('error', onError)
        video.removeEventListener('abort', onAbort)
      }
      const finish = (reason?: Error, code = '') => {
        if (settled) return
        settled = true
        cleanup()
        if (reason) {
          const failure = Object.assign(reason, videoDiagnostics(video), { errorCode: code }) as ShotVideoFailure
          reject(failure)
        } else resolve()
      }
      const check = () => {
        if (videoReady(video, objectUrl)) finish()
        else if (video.error) finish(new Error('The shot video reported a media decoding error.'), 'shot_video_media_error')
      }
      const onError = () => finish(new Error('The shot video reported a media error.'), 'shot_video_media_error')
      const onAbort = () => finish(new Error('The shot video load was aborted.'), 'shot_video_media_error')
      video.addEventListener('loadedmetadata', check)
      video.addEventListener('loadeddata', check)
      video.addEventListener('canplay', check)
      video.addEventListener('error', onError)
      video.addEventListener('abort', onAbort)
      check()
    })
  }

  async function loadInputVideoFromBlob(video: HTMLVideoElement, originalMediaUrl: string) {
    releaseInputObjectUrl()
    const normalizedMediaUrl = normalizeBackendAssetUrl(originalMediaUrl)
    const metadata = { originalShotUrl: originalMediaUrl, normalizedShotUrl: normalizedMediaUrl, fetchUrl: normalizedMediaUrl, fetchStatus: '', videoContentType: '', videoBlobSize: '0', objectUrlRevoked: 'false' }
    const fail = (code: string, message: string, extra: Record<string, string> = {}): never => {
      const failure = Object.assign(new Error(message), metadata, videoDiagnostics(video, extra), { errorCode: code }) as ShotVideoFailure
      throw failure
    }
    if (!normalizedMediaUrl) fail('SHOT_VIDEO_URL_EMPTY', 'The Shot 001 video URL is empty or is a Windows local path.')
    let response: Response | undefined
    try {
      response = await fetchWithTimeout(normalizedMediaUrl, undefined, 20000)
    } catch (reason) {
      const timedOut = errorText(reason).toLowerCase().includes('timed out')
      fail(timedOut ? 'fetch_shot_video_timeout' : 'SHOT_VIDEO_NETWORK_ERROR', `Shot video fetch failed: ${errorText(reason)}`, { browserError: errorText(reason) })
    }
    if (!response) fail('SHOT_VIDEO_NETWORK_ERROR', 'Shot 001 video request returned no response.')
    const checkedResponse = response as Response
    metadata.fetchStatus = String(checkedResponse.status)
    metadata.videoContentType = checkedResponse.headers.get('content-type') || ''
    if (checkedResponse.status === 404) fail('SHOT_VIDEO_NOT_FOUND', 'Shot 001 video was not found.')
    if (!checkedResponse.ok) fail('SHOT_VIDEO_HTTP_ERROR', `Shot 001 video request failed (${checkedResponse.status}).`)
    if (metadata.videoContentType.toLowerCase().includes('text/html')) fail('SHOT_VIDEO_RETURNED_HTML', 'Shot 001 video endpoint returned HTML instead of video.')
    if (!metadata.videoContentType.toLowerCase().startsWith('video/')) fail('SHOT_VIDEO_INVALID_CONTENT_TYPE', `Shot 001 response is not video (${metadata.videoContentType || 'missing content-type'}).`)
    const videoBlob = await checkedResponse.blob()
    metadata.videoBlobSize = String(videoBlob.size)
    if (!videoBlob.size) fail('SHOT_VIDEO_EMPTY_BLOB', 'Shot 001 video Blob is empty.')
    const objectUrl = URL.createObjectURL(videoBlob)
    inputObjectUrlRef.current = objectUrl
    video.preload = 'auto'
    video.muted = true
    video.playsInline = true
    video.loop = true
    video.playbackRate = 1
    video.style.position = 'fixed'
    video.style.left = '-10000px'
    video.style.top = '-10000px'
    video.style.width = '2px'
    video.style.height = '2px'
    video.style.opacity = '0.001'
    video.style.visibility = 'visible'
    video.style.display = 'block'
    video.style.pointerEvents = 'none'
    if (!document.body.contains(video)) document.body.appendChild(video)
    video.src = objectUrl
    video.load()
    try {
      await waitForVideo(video, objectUrl)
    } catch (reason) {
      const typed = reason as ShotVideoFailure
      const failure = Object.assign(new Error(errorText(reason)), metadata, videoDiagnostics(video), { errorCode: typed.errorCode || 'decode_shot_video_timeout' }) as ShotVideoFailure
      releaseInputObjectUrl()
      video.pause()
      video.removeAttribute('src')
      video.load()
      failure.objectUrlRevoked = 'true'
      throw failure
    }
    console.debug('[Xmax Shot 001 video ready]', { original_shot_url: originalMediaUrl, normalized_shot_url: normalizedMediaUrl, fetch_status: metadata.fetchStatus, content_type: metadata.videoContentType, blob_size: videoBlob.size, ...videoDiagnostics(video), object_url_revoked: false })
  }

  async function loadReferenceImage(mediaUrl: string) {
    const originalUrl = mediaUrl || ''
    const normalizedUrl = normalizeBackendAssetUrl(originalUrl)
    const viaViteProxy = normalizedUrl.startsWith('/uploads/')
    const meta = { originalReferenceUrl: originalUrl, normalizedReferenceUrl: normalizedUrl, viaViteProxy: String(viaViteProxy), blobSize: '0', responseContentType: '', httpStatus: '' }
    const fail = (code: string, message: string, extra: Partial<ReferenceImageFailure> = {}): never => {
      const failure = Object.assign(new Error(message), meta, { errorCode: code }, extra) as ReferenceImageFailure
      throw failure
    }
    if (!normalizedUrl) fail('REFERENCE_IMAGE_URL_EMPTY', 'The character reference image URL is empty or is a Windows local path.')
    let response: Response | undefined
    try {
      response = await fetchWithTimeout(normalizedUrl)
    } catch (reason) {
      fail('REFERENCE_IMAGE_NETWORK_ERROR', `Failed to fetch the reference image: ${errorText(reason)}`, { browserError: errorText(reason) })
    }
    if (!response) fail('REFERENCE_IMAGE_NETWORK_ERROR', 'The reference image request returned no response.')
    const checkedResponse = response as Response
    const contentType = checkedResponse.headers.get('content-type') || ''
    meta.responseContentType = contentType
    meta.httpStatus = String(checkedResponse.status)
    if (checkedResponse.status === 404) fail('REFERENCE_IMAGE_NOT_FOUND', 'The character reference image was not found.')
    if (!checkedResponse.ok) fail('REFERENCE_IMAGE_HTTP_ERROR', `The reference image request failed (${checkedResponse.status}).`)
    if (contentType.toLowerCase().includes('text/html')) fail('REFERENCE_IMAGE_RETURNED_HTML', 'The reference image endpoint returned HTML instead of an image.')
    if (!contentType.toLowerCase().startsWith('image/')) fail('REFERENCE_IMAGE_INVALID_CONTENT_TYPE', `The reference image response is not an image (${contentType || 'missing content-type'}).`)
    const blob = await checkedResponse.blob()
    meta.blobSize = String(blob.size)
    if (!blob.size) fail('REFERENCE_IMAGE_EMPTY', 'The reference image response is empty.')
    let objectUrl = ''
    try {
      if (typeof createImageBitmap === 'function') {
        const bitmap = await createImageBitmap(blob)
        bitmap.close()
      } else {
        objectUrl = URL.createObjectURL(blob)
        await new Promise<void>((resolve, reject) => {
          const image = new Image()
          image.onload = () => resolve()
          image.onerror = () => reject(new Error('The reference image could not be decoded by the browser.'))
          image.src = objectUrl
        })
      }
    } catch (reason) {
      fail('REFERENCE_IMAGE_DECODE_ERROR', `The reference image could not be decoded: ${errorText(reason)}`, { decodeError: errorText(reason) })
    } finally {
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
    return { blob, originalUrl, normalizedUrl, contentType, status: String(checkedResponse.status), viaViteProxy, decoded: true } satisfies ReferenceImageLoadResult
  }

  async function processShot(shot: XmaxShot, batchId: string): Promise<XmaxRealtimeResult> {
    markPipeline('shotPipelineEntered')
    markPipeline('shotPipelineShotId', shot.id)
    const input = inputRef.current
    const output = outputRef.current
    const localContainer = localContainerRef.current
    const remoteContainer = remoteContainerRef.current
    if (!input || !output || !localContainer || !remoteContainer) throw new Error('Realtime video elements or SDK containers are unavailable.')
    const sourceUrl = normalizeBackendAssetUrl(shot.source_url)
    try {
      await loadInputVideoFromBlob(input, sourceUrl)
    } catch (reason) {
      const typedReason = reason as ShotVideoFailure
      const failure = new Error(errorText(reason)) as ShotVideoFailure
      failure.stage = 'load_shot_video'
      failure.apiUrl = typedReason.normalizedShotUrl || sourceUrl
      failure.method = 'GET'
      Object.assign(failure, typedReason)
      throw failure
    }
    const referenceMediaUrl = shot.reference_url ? normalizeBackendAssetUrl(shot.reference_url) : referenceImage
    let referenceBlob: Blob
    try {
      referenceBlob = (await loadReferenceImage(referenceMediaUrl)).blob
    } catch (reason) {
      const typedReason = reason as ReferenceImageFailure
      const failure = new Error(errorText(reason)) as ReferenceImageFailure
      failure.stage = 'load_reference_image'
      failure.apiUrl = typedReason.normalizedReferenceUrl || referenceMediaUrl
      failure.method = 'GET'
      failure.errorCode = typedReason.errorCode
      failure.originalReferenceUrl = typedReason.originalReferenceUrl || referenceMediaUrl
      failure.normalizedReferenceUrl = typedReason.normalizedReferenceUrl || normalizeBackendAssetUrl(referenceMediaUrl)
      failure.responseContentType = typedReason.responseContentType || ''
      failure.httpStatus = typedReason.httpStatus || ''
      failure.blobSize = typedReason.blobSize || '0'
      failure.viaViteProxy = typedReason.viaViteProxy || String(failure.normalizedReferenceUrl.startsWith('/uploads/'))
      failure.decodeError = typedReason.decodeError || ''
      throw failure
    }
    input.currentTime = 0
    output.srcObject = null
    const adapter = new XmaxRealtimeAdapter()
    adapterRef.current = adapter
    sessionActiveRef.current = false
    setSessionSeconds(0)
    setStatus('starting')
    let result: XmaxRealtimeResult | null = null
    let completedNormally = false
    let rejectRun: ((reason?: unknown) => void) | null = null
    let resolveRun: ((value: XmaxRealtimeResult) => void) | null = null
    const run = new Promise<XmaxRealtimeResult>((resolve, reject) => { resolveRun = resolve; rejectRun = reject })
    const timeUpdate = () => setProcessedSeconds((batchRef.current?.processed_seconds || 0) + Math.min(shot.duration, input.currentTime))
    const stopAtEnd = () => { adapter.handleInputEnded() }
    const stopAtError = () => { stopRequestedRef.current = true; void adapter.finishStop('network-error') }
    input.addEventListener('timeupdate', timeUpdate)
    input.addEventListener('ended', stopAtEnd)
    input.addEventListener('error', stopAtError)
    input.addEventListener('abort', stopAtError)
    try {
      await adapter.start({
        inputVideo: input,
        outputVideo: output,
        localElement: localContainer,
        remoteElement: remoteContainer,
        referenceImage: referenceBlob,
        prompt: FIXED_PROMPT,
        sourceDuration: shot.duration,
        style: styleLabel,
        targetLanguage: languageLabel,
        characterName: referenceCharacter?.name || '',
        onSessionCreated: async (sessionId) => {
          sessionIdRef.current = sessionId
          const startPath = `/xmax-batches/${batchId}/start`
          let started: XmaxBatch
          try {
            started = await apiWithTimeout<XmaxBatch>(startPath, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ shot_id: shot.id, session_id: sessionId, status: 'starting' }) })
          } catch (reason) {
            const failure = new Error(errorText(reason)) as Error & { stage?: string; apiUrl?: string; method?: string }
            failure.stage = 'create_xmax_session'
            failure.apiUrl = (reason as Error & { apiUrl?: string }).apiUrl || apiRequestUrl(startPath)
            failure.method = 'POST'
            throw failure
          }
          batchRef.current = started
          sessionActiveRef.current = false
          updateAttempt({ status: 'starting', session_id: sessionId, ...diagnosticsAttemptPatch(sessionDiagnosticsRef.current) })
          if (mountedRef.current) {
            setBatch(started)
            setStatus('starting')
          }
          if (stopRequestedRef.current) await requestBackendStop('user-stop')
        },
        onSessionDiagnostics: (diagnostics) => {
          sessionDiagnosticsRef.current = diagnostics
        },
        onLifecycleMarker: markAdapterLifecycle,
        onSessionActive: async (sessionId, source) => {
          if (stopRequestedRef.current) return
          const runningPath = `/xmax-batches/${batchId}/running`
          let runningBatch: XmaxBatch
          try {
            runningBatch = await apiWithTimeout<XmaxBatch>(runningPath, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ shot_id: shot.id, session_id: sessionId, active_confirmation_source: source }) })
          } catch (reason) {
            const failure = new Error(errorText(reason)) as Error & { stage?: string; apiUrl?: string; method?: string }
            failure.stage = 'wait_session_active'
            failure.apiUrl = (reason as Error & { apiUrl?: string }).apiUrl || apiRequestUrl(runningPath)
            failure.method = 'POST'
            throw failure
          }
          sessionActiveRef.current = true
          batchRef.current = runningBatch
          updateAttempt({ status: 'running', session_id: sessionId, output_stream_received: sessionDiagnosticsRef.current?.outputStreamReceived || false, ...diagnosticsAttemptPatch(sessionDiagnosticsRef.current) })
          if (mountedRef.current) {
            setBatch(runningBatch)
            setCurrentShot(1)
            setStatus('running')
          }
        },
        onStatus: (next: XmaxRealtimeStatus) => {
          if (next === 'recording') setStatus(sessionActiveRef.current ? 'running' : 'starting')
          else if (next === 'connecting' || next === 'generating' || next === 'preparing') setStatus(sessionActiveRef.current ? 'running' : 'starting')
          else if (next === 'stopping') setStatus('stopping')
          else if (next === 'failed') rejectRun?.(new Error('Xmax realtime generation failed.'))
          else if (next === 'stopped' && !stopRequestedRef.current) rejectRun?.(new Error(`Shot ${shot.sequence} stopped before a capturable result was produced.`))
        },
        onProgress: (seconds) => setSessionSeconds(seconds),
        onRemoteStream: (stream) => {
          updateAttempt({ output_stream_received: true })
          if (previewRef.current) { previewRef.current.srcObject = stream; void previewRef.current.play().catch(() => undefined) }
        },
        onError: (message, providerAudit) => {
          updateAttempt({ status: 'failed', request_end_at: new Date().toISOString(), ...diagnosticsAttemptPatch(sessionDiagnosticsRef.current, providerAudit) })
          if (mountedRef.current) setError(message)
          rejectRun?.(new Error(message))
        },
        onStopReason: (reason) => {
          stopReasonRef.current = reason
          if (reason === 'hard-timeout' || reason === 'output-stalled' || reason === 'recorder-error' || reason === 'sdk-error' || reason === 'network-error' || reason === 'session-active-timeout' || reason === 'output-ready-timeout') {
            stopRequestedRef.current = true
            const message = reason === 'output-stalled' ? (zh ? '输出卡住，任务已停止' : 'Output stalled; the task was stopped.') : reason === 'hard-timeout' ? (zh ? '超时，任务已停止' : 'Timed out; the task was stopped.') : (zh ? '实时 Session 异常，任务已停止' : 'The realtime session failed and the task was stopped.')
            if (mountedRef.current) setError(message)
            rejectRun?.(new Error(message))
          } else if (reason !== 'source-ended') rejectRun?.(new Error(`Xmax session stopped: ${reason}`))
        },
        onResult: (next) => { result = next; if (next.processedSeconds > 0) setProcessedSeconds((batchRef.current?.processed_seconds || 0) + next.processedSeconds); resolveRun?.(next) },
      })
      const captured = await run
      completedNormally = true
      return captured
    } finally {
      input.removeEventListener('timeupdate', timeUpdate)
      input.removeEventListener('ended', stopAtEnd)
      input.removeEventListener('error', stopAtError)
      input.removeEventListener('abort', stopAtError)
      const cleanupTimedOut = await waitForLocalCleanup(adapter.dispose(), 3000)
      if (!completedNormally && result) releaseXmaxResult(result)
      adapterRef.current = null
      sessionActiveRef.current = false
      input.pause()
      releaseInputObjectUrl()
      input.removeAttribute('src')
      input.load()
      if (!result) output.srcObject = null
      if (mountedRef.current) {
        if (cleanupTimedOut) setError((current) => current || (zh ? '本地清理超时，已解除停止状态。' : 'Local cleanup timed out; the stop state was released.'))
        setStatus(completedNormally ? 'completed' : stopRequestedRef.current ? 'cancelled' : 'failed')
        setSessionSeconds(0)
      }
    }
  }

  async function saveShotReferences(currentBatch: XmaxBatch) {
    let next = currentBatch
    for (const shot of currentBatch.shots) {
      const ids = [shotCharacterIds[shot.id]?.find(Boolean) || referenceCharacter?.id].filter(Boolean) as string[]
      if (!ids.length) throw new Error(zh ? '每个镜头请选择一个已确认角色。' : 'Choose one confirmed character for each shot.')
      const result = await api<XmaxBatch>(`/xmax-batches/${currentBatch.id}/shots/${shot.id}/reference`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ character_ids: ids }) })
      next = result
    }
    return next
  }

  async function start() {
    markPipeline('generateButtonClicked')
    if (!configured) { setError(zh ? 'Xmax 尚未配置' : 'Xmax is not configured'); return }
    if (!episodeId || !source) { setError(zh ? '请先上传当前剧集视频' : 'Upload the current episode video first.'); return }
    if (!referenceCharacter || !referenceImage) { setError(zh ? '请先确认角色卡。' : 'Please confirm a character card first.'); return }
    if (['preparing', 'starting', 'running', 'stopping', 'processing', 'recording', 'splitting', 'merging'].includes(status)) return
    stopRequestedRef.current = false
    stopReasonRef.current = null
    sessionIdRef.current = null
    sessionDiagnosticsRef.current = null
    const freshMarkers = emptyPipelineMarkers()
    pipelineMarkersRef.current = freshMarkers
    setPipelineMarkers(freshMarkers)
    markPipeline('generateButtonClicked')
    backendStopSentRef.current = false
    setError('')
    setPrepareNotice('')
    setCurrentRunError(null)
    setHistoricalRun(null)
    batchRef.current = null
    setBatch(null)
    setCurrentShot(0)
    setProcessedSeconds(0)
    let activeBatchId: string | undefined
    try {
      const createdAttempt = await api<XmaxAttempt>(`/episodes/${episodeId}/xmax-attempts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ estimated_credits: sourceDuration || episode?.duration || 0 }),
      })
      attemptRef.current = createdAttempt
      setLatestAttempt(createdAttempt)
      await saveSelection()
      setStatus('preparing')
      const prepared = await apiWithTimeout<XmaxBatch>(`/episodes/${episodeId}/xmax-batches/prepare`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reference_character_id: referenceCharacter.id }) })
      if (!prepared.id) throw new Error('The prepare response did not include batch_id.')
      if (prepared.episode_id !== episodeId) throw new Error(`Prepare returned the wrong episode_id: ${prepared.episode_id || 'missing'}.`)
      if (!prepared.total_shots || prepared.total_shots <= 0 || !prepared.shots?.length) throw new Error('Prepare returned no valid shots.')
      const firstPreparedShot = prepared.shots.find((shot) => shot.sequence === 1) || prepared.shots[0]
      if (!firstPreparedShot?.id) throw new Error('Prepare did not return a valid Shot 001 id.')
      markPipeline('batchPrepared')
      activeBatchId = prepared.id
      updateAttempt({ batch_id: prepared.id, shot_id: firstPreparedShot.id, status: 'prepared', estimated_credits: prepared.estimated_credits })
      if (prepared.reused) setPrepareNotice(zh ? '已恢复现有批次。' : 'Existing prepared batch restored.')
      else if (prepared.replaced_stale_batch_id) setPrepareNotice(zh ? '旧批次已失效，已重新准备。' : 'The stale batch was replaced and a new batch was prepared.')
      setShotCharacterIds(Object.fromEntries(prepared.shots.map((shot) => [shot.id, shot.character_ids?.length ? shot.character_ids : [referenceCharacter.id]])))
      setBatch(prepared)
      batchRef.current = prepared
      setSourceDuration(prepared.source_duration || sourceDuration)
      setStatus('starting')
      const referenced = await saveShotReferences({ ...prepared, shots: [firstPreparedShot] })
      prepared.shots = referenced.shots
      batchRef.current = prepared
      setBatch(prepared)
      const firstShot = prepared.shots.find((shot) => shot.sequence === 1) || prepared.shots[0]
      if (!firstShot?.id || !firstShot.source_url) throw new Error('Shot 001 has no accessible source video URL.')
      if (!firstShot.reference_url) throw new Error('Shot 001 has no accessible character reference image URL.')
      if (stopRequestedRef.current) {
        const stopped = await api<XmaxBatch>(`/xmax-batches/${prepared.id}/stop`, { method: 'POST' }).catch(() => null)
        if (stopped) { batchRef.current = stopped; setBatch(stopped) }
        setStatus('cancelled')
        return
      }
      await apiWithTimeout<XmaxBatch>(`/xmax-batches/${prepared.id}/starting`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ shot_id: firstShot.id }) })
      updateAttempt({ batch_id: prepared.id, shot_id: firstShot.id, status: 'starting' })
      const captured = await processShot(firstShot, prepared.id)
      if (!stopRequestedRef.current) {
        const form = new FormData()
        form.append('file', captured.blob, 'shot-001-xmax.webm')
        form.append('processed_seconds', String(captured.processedSeconds))
        const current = await api<XmaxBatch>(`/xmax-batches/${prepared.id}/shots/${firstShot.id}/result`, { method: 'POST', body: form })
        batchRef.current = current
        if (mountedRef.current) { setBatch(current); setProcessedSeconds(current.processed_seconds) }
        releaseXmaxResult(captured)
        const stopped = await requestBackendStop('source-ended')
        batchRef.current = stopped
        if (mountedRef.current && stopped) setBatch(stopped)
        setStatus('cancelled')
        setError(zh ? 'Shot 001 已完成本轮启动验证，Shot 002 未处理。' : 'Shot 001 startup validation completed; Shot 002 was not processed.')
      } else {
        releaseXmaxResult(captured)
      }
    } catch (reason) {
      const typed = reason as ReferenceImageFailure & { stage?: string; apiUrl?: string; method?: string; httpStatus?: string; rawUidType?: string }
      const diagnostics = typed.diagnostics || sessionDiagnosticsRef.current
      const stage = typed.stage || (activeBatchId ? 'create_xmax_session' : 'prepare_batch')
      const stopReason = stopReasonRef.current
      const details = startupError(stage, reason, {
        generateButtonClicked: String(pipelineMarkersRef.current.generateButtonClicked),
        batchPrepared: String(pipelineMarkersRef.current.batchPrepared),
        shotPipelineEntered: String(pipelineMarkersRef.current.shotPipelineEntered),
        shotPipelineShotId: pipelineMarkersRef.current.shotPipelineShotId,
        adapterCalled: String(pipelineMarkersRef.current.adapterCalled),
        adapterMethodName: pipelineMarkersRef.current.adapterMethodName,
        sdkDiagnosticsWrapperEntered: String(pipelineMarkersRef.current.sdkDiagnosticsWrapperEntered),
        apiUrl: typed.apiUrl,
        method: typed.method,
        httpStatus: typed.httpStatus,
        errorCode: typed.errorCode || typed.code,
        batchId: activeBatchId || '',
        shotId: batchRef.current?.shots?.[0]?.id || '',
        originalReferenceUrl: typed.originalReferenceUrl,
        normalizedReferenceUrl: typed.normalizedReferenceUrl,
        responseContentType: typed.responseContentType,
        blobSize: typed.blobSize,
        viaViteProxy: typed.viaViteProxy,
        decodeError: typed.decodeError,
        originalShotUrl: typed.originalShotUrl,
        normalizedShotUrl: typed.normalizedShotUrl,
        fetchUrl: typed.fetchUrl,
        fetchStatus: typed.fetchStatus,
        videoContentType: typed.videoContentType,
        videoBlobSize: typed.videoBlobSize,
        readyState: typed.readyState,
        networkState: typed.networkState,
        videoDuration: typed.videoDuration,
        videoWidth: typed.videoWidth,
        videoHeight: typed.videoHeight,
        mediaErrorCode: typed.mediaErrorCode,
        mediaErrorMessage: typed.mediaErrorMessage,
        currentSrcType: typed.currentSrcType,
        objectUrlRevoked: typed.objectUrlRevoked,
        sessionId: sessionIdRef.current || '',
        sdkResultFieldNames: typed.availableFields?.join(', ') || 'getSessionUid',
        sessionObjectCreated: diagnostics ? String(diagnostics.sessionObjectCreated) : '',
        getSessionUidCalled: diagnostics ? String(diagnostics.getSessionUidCalled) : '',
        getSessionUidResolved: diagnostics ? String(diagnostics.getSessionUidResolved) : '',
        rawUidType: diagnostics?.rawUidType || typed.rawUidType || '',
        extractedSessionId: diagnostics?.extractedSessionId || sessionIdRef.current || '',
        sessionSavedToBackend: batchRef.current?.session_id ? 'true' : 'false',
        outputStreamReceived: diagnostics ? String(diagnostics.outputStreamReceived) : '',
        outputVideoTrackCount: diagnostics ? String(diagnostics.outputVideoTrackCount) : '',
        activeConfirmationSource: diagnostics?.activeConfirmationSource || typed.activeConfirmationSource,
        activeWaitMs: diagnostics?.activeWaitMs ? String(diagnostics.activeWaitMs) : typed.activeWaitMs ? String(typed.activeWaitMs) : '',
        stopAttempted: diagnostics ? String(diagnostics.stopAttempted) : sessionIdRef.current ? 'true' : 'false',
        stopCompleted: diagnostics ? String(diagnostics.stopCompleted) : stopReason ? 'true' : 'false',
        stopMethod: diagnostics?.stopMethod || '',
        stopReason: stopReason || '',
        inputVideoPlayCalled: diagnostics ? String(diagnostics.inputVideoPlayCalled) : '',
        inputVideoPlayResolved: diagnostics ? String(diagnostics.inputVideoPlayResolved) : '',
        inputVideoPaused: diagnostics ? String(diagnostics.inputVideoPaused) : '',
        inputVideoCurrentTimeStart: diagnostics ? String(diagnostics.inputVideoCurrentTimeStart) : '',
        inputVideoCurrentTimeEnd: diagnostics ? String(diagnostics.inputVideoCurrentTimeEnd) : '',
        videoFrameCallbackCount: diagnostics ? String(diagnostics.videoFrameCallbackCount) : '',
        presentedFrames: diagnostics ? String(diagnostics.presentedFrames) : '',
        captureStreamCreated: diagnostics ? String(diagnostics.captureStreamCreated) : '',
        captureVideoTrackCount: diagnostics ? String(diagnostics.captureVideoTrackCount) : '',
        captureTrackReadyState: diagnostics?.captureTrackReadyState || '',
        captureTrackMuted: diagnostics?.captureTrackMuted || '',
        sdkCreateCalled: diagnostics ? String(diagnostics.sdkCreateCalled) : '',
        sdkCreateStartedAt: diagnostics?.sdkCreateStartedAt || '',
        sdkCreateResolved: diagnostics ? String(diagnostics.sdkCreateResolved) : '',
        sdkCreateRejected: diagnostics ? String(diagnostics.sdkCreateRejected) : '',
        createSessionWaitMs: diagnostics ? String(diagnostics.createSessionWaitMs) : '',
        resultType: diagnostics?.resultType || '',
        resultConstructorName: diagnostics?.resultConstructorName || '',
        resultFieldNames: diagnostics?.resultFieldNames || '',
        resultMethodNames: diagnostics?.resultMethodNames || '',
        resultHasGetSessionUid: diagnostics ? String(diagnostics.resultHasGetSessionUid) : '',
        resultHasStop: diagnostics ? String(diagnostics.resultHasStop) : '',
        resultHasClose: diagnostics ? String(diagnostics.resultHasClose) : '',
        resultHasDisconnect: diagnostics ? String(diagnostics.resultHasDisconnect) : '',
        errorName: diagnostics?.errorName || '',
        rawErrorCode: diagnostics?.errorCode || '',
        rawErrorMessage: diagnostics?.errorMessage || '',
        errorStackHead: diagnostics?.errorStackHead || '',
        providerRequestMethod: diagnostics?.providerRequestMethod || '',
        providerRequestUrl: diagnostics?.providerRequestUrl || '',
        providerHttpStatus: diagnostics?.providerHttpStatus == null ? '' : String(diagnostics.providerHttpStatus),
        providerResponseBody: diagnostics?.providerResponseBody || '',
        providerErrorName: diagnostics?.errorName || '',
        providerErrorCode: diagnostics?.providerErrorCode || diagnostics?.errorCode || '',
        providerErrorMessage: diagnostics?.providerErrorMessage || diagnostics?.errorMessage || '',
        providerErrorStackHead: diagnostics?.errorStackHead || '',
        cleanupCompleted: diagnostics ? String(diagnostics.cleanupCompleted) : '',
        prewarmStreamCreated: diagnostics ? String(diagnostics.prewarmStreamCreated) : '',
        prewarmTrackCount: diagnostics ? String(diagnostics.prewarmTrackCount) : '',
        prewarmStreamStopped: diagnostics ? String(diagnostics.prewarmStreamStopped) : '',
        finalStreamCreated: diagnostics ? String(diagnostics.finalStreamCreated) : '',
        finalStreamActive: diagnostics ? String(diagnostics.finalStreamActive) : '',
        finalVideoTrackCount: diagnostics ? String(diagnostics.finalVideoTrackCount) : '',
        finalTrackId: diagnostics?.finalTrackId || '',
        finalTrackReadyState: diagnostics?.finalTrackReadyState || '',
        finalTrackEnabled: diagnostics?.finalTrackEnabled || '',
        finalTrackMuted: diagnostics?.finalTrackMuted || '',
        sdkReceivedFinalStream: diagnostics ? String(diagnostics.sdkReceivedFinalStream) : '',
        videoDecoderReady: diagnostics ? String(diagnostics.videoDecoderReady) : '',
        videoPlayResolved: diagnostics ? String(diagnostics.videoPlayResolved) : '',
        canvasCreated: diagnostics ? String(diagnostics.canvasCreated) : '',
        canvasContextCreated: diagnostics ? String(diagnostics.canvasContextCreated) : '',
        canvasWidth: diagnostics ? String(diagnostics.canvasWidth) : '',
        canvasHeight: diagnostics ? String(diagnostics.canvasHeight) : '',
        canvasCaptureFps: diagnostics ? String(diagnostics.canvasCaptureFps) : '',
        canvasDrawCount: diagnostics ? String(diagnostics.canvasDrawCount) : '',
        firstCanvasFrameTime: diagnostics?.firstCanvasFrameTime || '',
        lastCanvasFrameTime: diagnostics?.lastCanvasFrameTime || '',
        canvasStreamCreated: diagnostics ? String(diagnostics.canvasStreamCreated) : '',
        canvasStreamActive: diagnostics ? String(diagnostics.canvasStreamActive) : '',
        canvasVideoTrackCount: diagnostics ? String(diagnostics.canvasVideoTrackCount) : '',
        canvasTrackId: diagnostics?.canvasTrackId || '',
        canvasTrackKind: diagnostics?.canvasTrackKind || '',
        canvasTrackReadyState: diagnostics?.canvasTrackReadyState || '',
        canvasTrackEnabled: diagnostics?.canvasTrackEnabled || '',
        canvasTrackMuted: diagnostics?.canvasTrackMuted || '',
        sdkReceivedCanvasStream: diagnostics ? String(diagnostics.sdkReceivedCanvasStream) : '',
      })
      const message = details.errorMessage
      const failedStop = stopReason === 'hard-timeout' || stopReason === 'output-stalled' || stopReason === 'recorder-error' || stopReason === 'sdk-error' || stopReason === 'network-error' || stopReason === 'session-active-timeout' || stopReason === 'output-ready-timeout'
      const terminalStatus = stopRequestedRef.current && !failedStop ? 'cancelled' : 'failed'
      updateAttempt({
        status: terminalStatus,
        request_end_at: new Date().toISOString(),
        session_id: sessionIdRef.current || undefined,
        stop_reason: stopReason || undefined,
        ...diagnosticsAttemptPatch(diagnostics, undefined),
        provider_error_code: details.providerErrorCode || undefined,
        provider_error_message: details.providerErrorMessage || details.errorMessage || undefined,
        provider_error_name: details.providerErrorName || details.errorName || undefined,
        provider_error_stack_head: details.providerErrorStackHead || details.errorStackHead || undefined,
        provider_request_method: details.providerRequestMethod || undefined,
        provider_request_url: details.providerRequestUrl || undefined,
        provider_http_status: details.providerHttpStatus ? Number(details.providerHttpStatus) : undefined,
        provider_response_body: details.providerResponseBody || undefined,
        classified_error_code: details.errorCode || undefined,
        classified_error_message: details.errorMessage || undefined,
      })
      if (stopRequestedRef.current && !failedStop) return
      setCurrentRunError(details)
      setError(message)
      setStatus('failed')
      if (sessionIdRef.current) {
        void requestBackendStop(details.errorCode || stage)
      } else if (activeBatchId) {
        void api(`/xmax-batches/${activeBatchId}/fail`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: details.errorCode || 'XMAX_SHOT_FAILED', message, stage }) }).catch(() => undefined)
      }
    }
  }

  async function stop() {
    stopRequestedRef.current = true
    stopReasonRef.current = 'user-stop'
    updateAttempt({ status: 'cancelled', stop_reason: 'user-stop', request_end_at: new Date().toISOString(), session_id: sessionIdRef.current || undefined, ...diagnosticsAttemptPatch(sessionDiagnosticsRef.current) })
    if (adapterRef.current) await waitForLocalCleanup(adapterRef.current.finishStop('user-stop'), 3000)
    const stopped = await requestBackendStop('user-stop')
    if (stopped) { batchRef.current = stopped; setBatch(stopped) }
    setStatus('cancelled')
  }

  async function resetBatch() {
    if (!batch?.id || running) return
    const next = await api<XmaxBatch>(`/xmax-batches/${batch.id}/reset`, { method: 'POST' })
    batchRef.current = next
    setBatch(next)
    setStatus(next.status)
    setCurrentRunError(null)
    setHistoricalRun(null)
    setCurrentShot(0)
    setProcessedSeconds(next.processed_seconds || 0)
    setSessionSeconds(0)
    setError('')
  }

  const totalShots = batch?.total_shots || 0
  const displaySeconds = processedSeconds || batch?.processed_seconds || 0
  const outputUrl = batch?.output_url ? assetUrl(batch.output_url) : ''
  const running = status === 'running'
  const canCancel = status === 'starting' || status === 'running'
  const preparing = status === 'preparing'
  const originalDuration = episode?.duration || project.video_duration || sourceDuration
  const attemptAuditSummary = latestAttempt && <div className="xmax-warning"><strong>{zh ? '最新 Xmax 运行审计' : 'Latest Xmax attempt audit'}</strong><div>attempt_id: {latestAttempt.id}</div><div>status: {latestAttempt.status}</div><div>session_id: {latestAttempt.session_id || '-'}</div><div>provider_http_status: {latestAttempt.provider_http_status ?? '-'}</div><div>provider_error_code: {latestAttempt.provider_error_code || '-'}</div><div>classified_error_code: {latestAttempt.classified_error_code || '-'}</div><div>provider_error_message: {latestAttempt.provider_error_message || '-'}</div><div>processed_seconds: {Number(latestAttempt.processed_seconds || 0).toFixed(2)}</div><div>billed_credits: {latestAttempt.billed_credits ?? '-'}</div><div>estimated_credits: {Number(latestAttempt.estimated_credits || 0).toFixed(2)}</div><div>output_stream_received: {String(Boolean(latestAttempt.output_stream_received))}</div><div>output_file_received: {String(Boolean(latestAttempt.output_file_received))}</div><div>stop_reason: {latestAttempt.stop_reason || '-'}</div><details><summary>{zh ? 'Provider 原始响应 / 技术详情' : 'Provider raw response / technical details'}</summary><div>request: {latestAttempt.provider_request_method || '-'} {latestAttempt.provider_request_url || '-'}</div><pre>{latestAttempt.provider_response_body || '-'}</pre><div>error_name: {latestAttempt.provider_error_name || '-'}</div><div>error_stack_head: {latestAttempt.provider_error_stack_head || '-'}</div></details></div>
  return <div className="step-content"><PipelineNotice>{zh ? '镜头级 Xmax 转绘：每个片段独立处理，片段结束后立即关闭 Session。' : 'Shot-level Xmax transformation: each shot uses an independent session that closes at shot end.'}</PipelineNotice>
    <section className="xmax-panel"><div className="xmax-heading"><div><span className="section-label">XMAX / SHOT PIPELINE</span><h2>{zh ? '生成海外版' : 'Generate overseas version'}</h2><p>{zh ? '当前流程按镜头串行处理并在后端恢复原视频音频。' : 'Shots are processed serially and the original audio is restored by the backend.'}</p></div><div className="xmax-heading-status"><span className={`xmax-status xmax-status-${status}`}>{statusLabel(status, zh)}</span><span className="xmax-configured">Xmax configured: {configured ? 'true' : 'false'}</span></div></div>
      {!configured && <div className="xmax-warning">{zh ? 'Xmax 尚未配置，当前不会发起请求。' : 'Xmax is not configured. No realtime request will be started.'}</div>}
      {error && <div className="xmax-warning">{error}{startupFailure && <details><summary>SDK create diagnostics</summary><div>sdk_create_started_at: {startupFailure.sdkCreateStartedAt || '-'}</div><div>sdk_create_rejected: {startupFailure.sdkCreateRejected || '-'}</div><div>sdk_create_wait_ms: {startupFailure.createSessionWaitMs || '-'}</div><div>result_type: {startupFailure.resultType || '-'}</div><div>result_constructor_name: {startupFailure.resultConstructorName || '-'}</div><div>result_field_names: {startupFailure.resultFieldNames || '-'}</div><div>result_method_names: {startupFailure.resultMethodNames || '-'}</div><div>get_session_uid_available: {startupFailure.resultHasGetSessionUid || '-'}</div><div>result_has_stop: {startupFailure.resultHasStop || '-'}</div><div>result_has_close: {startupFailure.resultHasClose || '-'}</div><div>result_has_disconnect: {startupFailure.resultHasDisconnect || '-'}</div><div>error_name: {startupFailure.errorName || '-'}</div><div>raw_error_code: {startupFailure.rawErrorCode || '-'}</div><div>raw_error_message: {startupFailure.rawErrorMessage || '-'}</div><div>error_stack_head: {startupFailure.errorStackHead || '-'}</div></details>}</div>}
      {attemptAuditSummary}
      {historicalRun && !currentRunError && <div className="xmax-warning"><details><summary>{zh ? '上次运行失败' : 'Previous run failed'}</summary><div>{zh ? '失败时间' : 'Failed at'}: {new Date(historicalRun.finished_at || historicalRun.updated_at || historicalRun.created_at).toLocaleString()}</div><div>batch_id: {historicalRun.id}</div><div>error_code: {historicalRun.error_code || '-'}</div><div>failed_stage: {historicalRun.failed_stage || '-'}</div><div>{historicalRun.error_message || (zh ? '上次运行未完成。' : 'The previous run did not complete.')}</div><details><summary>{zh ? '技术详情' : 'Technical details'}</summary><div>Pipeline diagnostics are from the previous run and are intentionally collapsed.</div></details></details></div>}
      {prepareNotice && <PipelineNotice>{prepareNotice}</PipelineNotice>}
      {startupFailure && <details className="xmax-warning"><summary>Pipeline diagnostics</summary><div>generate_button_clicked: {startupFailure.generateButtonClicked || '-'}</div><div>batch_prepared: {startupFailure.batchPrepared || '-'}</div><div>shot_pipeline_entered: {startupFailure.shotPipelineEntered || '-'}</div><div>shot_pipeline_shot_id: {startupFailure.shotPipelineShotId || '-'}</div><div>adapter_called: {startupFailure.adapterCalled || '-'}</div><div>adapter_method_name: {startupFailure.adapterMethodName || '-'}</div><div>sdk_diagnostics_wrapper_entered: {startupFailure.sdkDiagnosticsWrapperEntered || '-'}</div><div>sdk_create_called: {startupFailure.sdkCreateCalled || '-'}</div><div>video_decoder_ready: {startupFailure.videoDecoderReady || '-'}</div><div>video_play_resolved: {startupFailure.videoPlayResolved || '-'}</div><div>canvas_created: {startupFailure.canvasCreated || '-'}</div><div>canvas_context_created: {startupFailure.canvasContextCreated || '-'}</div><div>canvas_width: {startupFailure.canvasWidth || '-'}</div><div>canvas_height: {startupFailure.canvasHeight || '-'}</div><div>canvas_capture_fps: {startupFailure.canvasCaptureFps || '-'}</div><div>canvas_draw_count: {startupFailure.canvasDrawCount || '-'}</div><div>first_canvas_frame_time: {startupFailure.firstCanvasFrameTime || '-'}</div><div>last_canvas_frame_time: {startupFailure.lastCanvasFrameTime || '-'}</div><div>canvas_stream_created: {startupFailure.canvasStreamCreated || '-'}</div><div>canvas_stream_active: {startupFailure.canvasStreamActive || '-'}</div><div>canvas_video_track_count: {startupFailure.canvasVideoTrackCount || '-'}</div><div>canvas_track_id: {startupFailure.canvasTrackId || '-'}</div><div>canvas_track_kind: {startupFailure.canvasTrackKind || '-'}</div><div>canvas_track_ready_state: {startupFailure.canvasTrackReadyState || '-'}</div><div>canvas_track_enabled: {startupFailure.canvasTrackEnabled || '-'}</div><div>canvas_track_muted: {startupFailure.canvasTrackMuted || '-'}</div><div>sdk_received_canvas_stream: {startupFailure.sdkReceivedCanvasStream || '-'}</div></details>}
      {startupFailure && <div className="xmax-warning"><div>session_object_created: {startupFailure.sessionObjectCreated || '-'}</div><div>get_session_uid_called: {startupFailure.getSessionUidCalled || '-'}</div><div>get_session_uid_resolved: {startupFailure.getSessionUidResolved || '-'}</div><div>raw_uid_type: {startupFailure.rawUidType || '-'}</div><div>extracted_session_id: {startupFailure.extractedSessionId || startupFailure.sessionId || '-'}</div><div>session_saved_to_backend: {startupFailure.sessionSavedToBackend || '-'}</div><div>input_video_play_called: {startupFailure.inputVideoPlayCalled || '-'}</div><div>input_video_play_resolved: {startupFailure.inputVideoPlayResolved || '-'}</div><div>input_video_paused: {startupFailure.inputVideoPaused || '-'}</div><div>input_video_current_time_start: {startupFailure.inputVideoCurrentTimeStart || '-'}</div><div>input_video_current_time_end: {startupFailure.inputVideoCurrentTimeEnd || '-'}</div><div>video_frame_callback_count: {startupFailure.videoFrameCallbackCount || '-'}</div><div>presented_frames: {startupFailure.presentedFrames || '-'}</div><div>prewarm_stream_created: {startupFailure.prewarmStreamCreated || '-'}</div><div>prewarm_track_count: {startupFailure.prewarmTrackCount || '-'}</div><div>prewarm_stream_stopped: {startupFailure.prewarmStreamStopped || '-'}</div><div>final_stream_created: {startupFailure.finalStreamCreated || '-'}</div><div>final_stream_active: {startupFailure.finalStreamActive || '-'}</div><div>final_video_track_count: {startupFailure.finalVideoTrackCount || '-'}</div><div>final_track_id: {startupFailure.finalTrackId || '-'}</div><div>final_track_ready_state: {startupFailure.finalTrackReadyState || '-'}</div><div>final_track_enabled: {startupFailure.finalTrackEnabled || '-'}</div><div>final_track_muted: {startupFailure.finalTrackMuted || '-'}</div><div>capture_stream_created: {startupFailure.captureStreamCreated || '-'}</div><div>capture_video_track_count: {startupFailure.captureVideoTrackCount || '-'}</div><div>capture_track_ready_state: {startupFailure.captureTrackReadyState || '-'}</div><div>capture_track_muted: {startupFailure.captureTrackMuted || '-'}</div><div>sdk_received_final_stream: {startupFailure.sdkReceivedFinalStream || '-'}</div><div>sdk_create_called: {startupFailure.sdkCreateCalled || '-'}</div><div>sdk_create_started_at: {startupFailure.sdkCreateStartedAt || '-'}</div><div>sdk_create_resolved: {startupFailure.sdkCreateResolved || '-'}</div><div>sdk_create_rejected: {startupFailure.sdkCreateRejected || '-'}</div><div>sdk_create_wait_ms: {startupFailure.createSessionWaitMs || '-'}</div><div>result_type: {startupFailure.resultType || '-'}</div><div>result_constructor_name: {startupFailure.resultConstructorName || '-'}</div><div>result_field_names: {startupFailure.resultFieldNames || '-'}</div><div>result_method_names: {startupFailure.resultMethodNames || '-'}</div><div>get_session_uid_available: {startupFailure.resultHasGetSessionUid || '-'}</div><div>result_has_stop: {startupFailure.resultHasStop || '-'}</div><div>result_has_close: {startupFailure.resultHasClose || '-'}</div><div>result_has_disconnect: {startupFailure.resultHasDisconnect || '-'}</div><div>error_name: {startupFailure.errorName || '-'}</div><div>raw_error_code: {startupFailure.errorCode || '-'}</div><div>raw_error_message: {startupFailure.errorMessage || '-'}</div><div>error_stack_head: {startupFailure.errorStackHead || '-'}</div><div>output_stream_received: {startupFailure.outputStreamReceived || '-'}</div><div>output_video_track_count: {startupFailure.outputVideoTrackCount || '-'}</div><div>active_confirmation_source: {startupFailure.activeConfirmationSource || '-'}</div><div>active_wait_ms: {startupFailure.activeWaitMs || '-'}</div><div>cleanup_completed: {startupFailure.cleanupCompleted || '-'}</div><div>stop_attempted: {startupFailure.stopAttempted || '-'}</div><div>stop_completed: {startupFailure.stopCompleted || '-'}</div><div>stop_method: {startupFailure.stopMethod || '-'}</div><div>stop_reason: {startupFailure.stopReason || '-'}</div><div>sdk_result_field_names: {startupFailure.sdkResultFieldNames || '-'}</div></div>}
      {startupFailure?.originalShotUrl && <div className="xmax-warning"><div>original_shot_url: {startupFailure.originalShotUrl}</div><div>normalized_shot_url: {startupFailure.normalizedShotUrl || '-'}</div><div>fetch_url: {startupFailure.fetchUrl || '-'}</div><div>fetch_status: {startupFailure.fetchStatus || startupFailure.httpStatus || '-'}</div><div>content_type: {startupFailure.videoContentType || '-'}</div><div>blob_size: {startupFailure.videoBlobSize || '0'}</div><div>ready_state: {startupFailure.readyState || '-'}</div><div>network_state: {startupFailure.networkState || '-'}</div><div>duration: {startupFailure.videoDuration || '-'}</div><div>video_width: {startupFailure.videoWidth || '-'}</div><div>video_height: {startupFailure.videoHeight || '-'}</div><div>media_error_code: {startupFailure.mediaErrorCode || '-'}</div><div>media_error_message: {startupFailure.mediaErrorMessage || '-'}</div><div>current_src_type: {startupFailure.currentSrcType || '-'}</div><div>object_url_revoked: {startupFailure.objectUrlRevoked || 'false'}</div></div>}
      {startupFailure?.originalReferenceUrl && <div className="xmax-warning"><div>original_reference_url: {startupFailure.originalReferenceUrl}</div><div>normalized_reference_url: {startupFailure.normalizedReferenceUrl || '-'}</div><div>response_content_type: {startupFailure.responseContentType || '-'}</div><div>response_status: {startupFailure.httpStatus || '-'}</div><div>blob_size: {startupFailure.blobSize || '0'}</div><div>via_vite_proxy: {startupFailure.viaViteProxy || 'false'}</div>{startupFailure.decodeError && <div>decode_error: {startupFailure.decodeError}</div>}</div>}
      {startupFailure && <div className="xmax-warning"><strong>{zh ? '启动失败技术详情' : 'Startup failure details'}</strong><div>{zh ? '失败阶段' : 'Stage'}: {startupFailure.stage}</div><div>{zh ? 'API URL' : 'API URL'}: {startupFailure.apiUrl || '-'}</div><div>{zh ? 'HTTP 方法' : 'HTTP method'}: {startupFailure.method || '-'}</div><div>{zh ? 'HTTP 状态' : 'HTTP status'}: {startupFailure.httpStatus || '-'}</div><div>{zh ? '错误代码' : 'Error code'}: {startupFailure.errorCode || '-'}</div><div>{zh ? '错误信息' : 'Error message'}: {startupFailure.errorMessage || '-'}</div><div>batch_id: {startupFailure.batchId || batch?.id || '-'}</div><div>shot_id: {startupFailure.shotId || batch?.shots?.[0]?.id || '-'}</div><div>session_id: {startupFailure.sessionId || batch?.session_id || '-'}</div>{startupFailure.browserError && <div>{zh ? '浏览器原始错误' : 'Browser error'}: {startupFailure.browserError}</div>}</div>}
      <div className="xmax-media-grid"><div className="xmax-media-card"><span className="media-label">{zh ? '原始视频' : 'Original video'}</span>{source ? <video controls playsInline preload="metadata" src={source} onLoadedMetadata={(event) => { if (Number.isFinite(event.currentTarget.duration)) setSourceDuration(event.currentTarget.duration) }} /> : <div className="empty-video"><Film size={20} /> {zh ? '未上传视频' : 'No source video'}</div>}</div><div className="xmax-media-card"><span className="media-label">{zh ? '转绘结果' : 'Transformation result'}</span>{outputUrl ? <video ref={previewRef} controls playsInline src={outputUrl} /> : <div className="empty-video"><Film size={20} /> {zh ? '完成后显示完整结果' : 'The complete result appears after merge'}</div>}</div></div>
      <div className="xmax-reference"><div className="xmax-reference-empty">{referenceImage ? <img src={referenceImage} alt="" /> : zh ? '未上传' : 'No image'}</div><div><strong>{referenceCharacter?.name || (zh ? '未确认角色卡' : 'No confirmed character card')}</strong><p>{zh ? '本轮使用一张已确认主参考图。' : 'One confirmed primary reference image is used for this MVP.'}</p></div></div>
      <div className="xmax-control-grid"><label className="field-label">{zh ? '目标画风' : 'Target style'}<select value={style} onChange={(event) => setStyle(event.target.value)} disabled={running}><option value="western-live-action">{zh ? '欧美仿真人' : 'Western Live Action'}</option><option value="cinematic-realism">{zh ? '电影写实' : 'Cinematic Realism'}</option><option value="western-fantasy">{zh ? '西幻真人' : 'Western Fantasy'}</option></select></label><label className="field-label">{zh ? '目标语言' : 'Target language'}<select value={targetLanguage} onChange={(event) => setTargetLanguage(event.target.value)} disabled={running}><option value="english">{zh ? '英语' : 'English'}</option><option value="japanese">{zh ? '日语' : 'Japanese'}</option><option value="korean">{zh ? '韩语' : 'Korean'}</option></select></label></div>
      <div className="xmax-summary"><div><span>{zh ? '原视频时长' : 'Source duration'}</span><strong>{originalDuration.toFixed(1)}s</strong></div><div><span>{zh ? '当前片段' : 'Current shot'}</span><strong>{currentShot && totalShots ? `${currentShot} / ${totalShots}` : `0 / ${totalShots || '—'}`}</strong></div><div><span>{zh ? '已处理秒数' : 'Processed seconds'}</span><strong>{displaySeconds.toFixed(1)}s</strong></div><div><span>{zh ? '预计 credits' : 'Estimated credits'}</span><strong>{(batch?.estimated_credits || 0).toFixed(1)}</strong><small>{zh ? '实际创建 Session 的持续秒数 × 1' : 'Actual session seconds × 1'}</small></div></div>
      {batch && <div className="xmax-shot-reference-list"><div className="section-label">{zh ? '当前角色' : 'Current character'}</div>{batch.shots.map((shot) => <div className="xmax-shot-reference-row" key={shot.id}><strong>Shot {String(shot.sequence).padStart(3, '0')}</strong><div className="xmax-shot-character-selects"><label className="field-label">{zh ? '当前角色' : 'Current character'}<select value={shotCharacterIds[shot.id]?.[0] || referenceCharacter?.id || ''} disabled={running} onChange={(event) => setShotCharacterIds((old) => ({ ...old, [shot.id]: event.target.value ? [event.target.value] : [] }))}><option value="">{zh ? '选择角色' : 'Select character'}</option>{project.characters.filter((character) => character.confirmed && character.priority_level === 'Locked' && referenceUrl(character)).map((character) => <option key={character.id} value={character.id}>{character.character_code || character.id} · {character.name}</option>)}</select></label></div>{shot.reference_url && <img src={assetUrl(shot.reference_url)} alt="" />}</div>)}</div>}
      {batch && <div className="xmax-shot-progress"><div><span>{zh ? '批次进度' : 'Batch progress'}</span><strong>{batch.completed_shots} / {batch.total_shots}</strong></div><div className="progress-track"><span style={{ width: `${batch.total_shots ? (batch.completed_shots / batch.total_shots) * 100 : 0}%` }} /></div></div>}
      <div className="xmax-actions">{running ? <button className="danger-button" onClick={() => void stop()}><X size={16} /> {zh ? '停止' : 'Stop'}</button> : canCancel ? <button className="secondary-button" onClick={() => void stop()}><X size={16} /> {zh ? '取消启动' : 'Cancel startup'}</button> : <button className="primary-button" onClick={() => void start()} disabled={!configured || !source || !referenceImage || status === 'completed' || preparing}><Sparkles size={17} /> {preparing ? (zh ? '正在准备...' : 'Preparing...') : zh ? '生成海外版' : 'Generate overseas version'}</button>}{outputUrl && <a className="secondary-button" href={outputUrl} download><Download size={16} /> {zh ? '下载完整视频' : 'Download complete video'}</a>}<span className="xmax-disclaimer">{zh ? '本轮只使用固定角色替换 Prompt；不执行翻译、配音或字幕。' : 'This MVP uses the fixed character replacement prompt only; no translation, dubbing, or subtitles are run.'}</span></div>
      {batch && ['failed', 'interrupted', 'cancelled', 'stopped'].includes(status) && <button className="secondary-button compact" onClick={() => void resetBatch()}>{zh ? '重新开始当前批次' : 'Restart current batch'}</button>}
      <div className="xmax-shot-session"><span>{zh ? '\u5f53\u524d Session' : 'Current Session'}</span><strong>{sessionSeconds.toFixed(1)}s</strong></div>
      <video ref={inputRef} className="xmax-hidden-video" muted playsInline preload="auto" /><video ref={outputRef} className="xmax-hidden-video" muted playsInline autoPlay /><div ref={localContainerRef} className="xmax-sdk-container"><span className="xmax-sdk-container-label">Xmax Local Container</span></div><div ref={remoteContainerRef} className="xmax-sdk-container"><span className="xmax-sdk-container-label">Xmax Remote Container</span></div>
    </section></div>
}
