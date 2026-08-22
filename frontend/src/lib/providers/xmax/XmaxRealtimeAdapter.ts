import { createXmaxClient, models, type RealtimeSession, type XmaxClient } from '@xmaxai/sdk'
import { requireXmaxTemporaryApiKey } from './xmaxBrowserCredential'

export type XmaxRealtimeStatus = 'preparing' | 'connecting' | 'generating' | 'recording' | 'completed' | 'stopping' | 'stopped' | 'failed'
export type XmaxStopReason = 'source-ended' | 'user-stop' | 'hard-timeout' | 'output-stalled' | 'recorder-error' | 'sdk-error' | 'network-error' | 'session-active-timeout' | 'output-ready-timeout' | 'page-hidden' | 'component-unmount' | 'batch-cancelled'
type XmaxStartupState = 'idle' | 'preparing' | 'connecting' | 'session-ready' | 'stopping'

export type XmaxSessionDiagnostics = {
  sessionObjectCreated: boolean
  getSessionUidCalled: boolean
  getSessionUidResolved: boolean
  rawUidType: string
  extractedSessionId: string
  outputStreamReceived: boolean
  outputVideoTrackCount: number
  activeConfirmationSource: string
  activeWaitMs: number
  stopAttempted: boolean
  stopCompleted: boolean
  stopMethod: string
  stopReason: string
  inputVideoPlayCalled: boolean
  inputVideoPlayResolved: boolean
  inputVideoPaused: boolean
  inputVideoCurrentTimeStart: number
  inputVideoCurrentTimeEnd: number
  videoFrameCallbackCount: number
  presentedFrames: number
  captureStreamCreated: boolean
  captureVideoTrackCount: number
  captureTrackReadyState: string
  captureTrackMuted: string
  sdkCreateCalled: boolean
  sdkCreateStartedAt: string
  sdkCreateResolved: boolean
  sdkCreateRejected: boolean
  createSessionWaitMs: number
  resultType: string
  resultConstructorName: string
  resultFieldNames: string
  resultMethodNames: string
  resultHasGetSessionUid: boolean
  resultHasStop: boolean
  resultHasClose: boolean
  resultHasDisconnect: boolean
  errorName: string
  errorCode: string
  errorMessage: string
  errorStackHead: string
  providerRequestMethod: string
  providerRequestUrl: string
  providerHttpStatus: number | null
  providerResponseBody: string
  providerErrorCode: string
  providerErrorMessage: string
  cleanupCompleted: boolean
  prewarmStreamCreated: boolean
  prewarmTrackCount: number
  prewarmStreamStopped: boolean
  finalStreamCreated: boolean
  finalStreamActive: boolean
  finalVideoTrackCount: number
  finalTrackId: string
  finalTrackReadyState: string
  finalTrackEnabled: string
  finalTrackMuted: string
  sdkReceivedFinalStream: boolean
  videoDecoderReady: boolean
  videoPlayResolved: boolean
  canvasCreated: boolean
  canvasContextCreated: boolean
  canvasWidth: number
  canvasHeight: number
  canvasCaptureFps: number
  canvasDrawCount: number
  firstCanvasFrameTime: string
  lastCanvasFrameTime: string
  canvasStreamCreated: boolean
  canvasStreamActive: boolean
  canvasVideoTrackCount: number
  canvasTrackId: string
  canvasTrackKind: string
  canvasTrackReadyState: string
  canvasTrackEnabled: string
  canvasTrackMuted: string
  sdkReceivedCanvasStream: boolean
  finalInputStreamActive: boolean
  finalInputVideoTrackCount: number
  finalInputTrackId: string
  finalInputTrackReadyState: string
  finalInputTrackEnabled: string
  finalInputTrackMuted: string
  finalInputTrackSettings: string
  localElementTagName: string
  remoteElementTagName: string
  canvasDrawCountBeforeConnect: number
  videoCurrentTimeBeforeConnect: number
  startupState: XmaxStartupState
  inputLoopDuringConnect: boolean
  endedIgnoredDuringStartupCount: number
  sessionReady: boolean
  loopDisabledAfterSession: boolean
  sourceCurrentTimeWhenSessionReady: number
}

export type XmaxRealtimeResult = {
  objectUrl: string
  blob: Blob
  mimeType: string
  durationSeconds: number
  processedSeconds: number
  estimatedCredits: number
  style: string
  targetLanguage: string
  characterName: string
  createdAt: string
}

export type XmaxRealtimeStartOptions = {
  inputVideo: HTMLVideoElement
  outputVideo: HTMLVideoElement
  localElement: HTMLDivElement
  remoteElement: HTMLDivElement
  referenceImage: string | File | Blob
  prompt: string
  sourceDuration: number
  style: string
  targetLanguage: string
  characterName: string
  onStatus?: (status: XmaxRealtimeStatus) => void
  onSessionCreated?: (sessionId: string) => void | Promise<void>
  onSessionActive?: (sessionId: string, source: string) => void | Promise<void>
  onSessionDiagnostics?: (diagnostics: XmaxSessionDiagnostics) => void
  onLifecycleMarker?: (marker: XmaxLifecycleMarker, value?: string) => void
  onProgress?: (processedSeconds: number) => void
  onRemoteStream?: (stream: MediaStream) => void
  onError?: (message: string, audit?: XmaxProviderAudit) => void
  onStopReason?: (reason: XmaxStopReason) => void
  onResult?: (result: XmaxRealtimeResult) => void
}

export type XmaxLifecycleMarker =
  | 'adapter_called'
  | 'adapter_method_name'
  | 'sdk_diagnostics_wrapper_entered'
  | 'sdk_create_called'

export type XmaxProviderAudit = {
  requestMethod?: string
  requestUrl?: string
  httpStatus?: number | null
  responseBody?: string
  errorName?: string
  errorCode?: string
  errorMessage?: string
  errorStackHead?: string
}

export const MAX_SESSION_MS = 18000
export const UNKNOWN_DURATION_SESSION_MS = 15000

export async function extractXmaxSessionUid(session: RealtimeSession | null | undefined): Promise<string> {
  const fieldNames = ['getSessionUid']
  if (!session || typeof session.getSessionUid !== 'function') {
    const failure = new Error('Xmax Session object does not expose getSessionUid.') as Error & { code?: string; stage?: string; availableFields?: string[]; rawUidType?: string }
    failure.code = 'XMAX_SESSION_UID_METHOD_MISSING'
    failure.stage = 'get_xmax_session_uid'
    failure.availableFields = fieldNames
    failure.rawUidType = 'undefined'
    throw failure
  }
  let timeout: number | null = null
  try {
    const rawUid = await Promise.race<unknown>([
      Promise.resolve().then(() => session.getSessionUid.call(session)),
      new Promise<never>((_, reject) => {
        timeout = window.setTimeout(() => {
          const failure = new Error('Timed out while obtaining the Xmax session UID.') as Error & { code?: string; stage?: string; availableFields?: string[]; rawUidType?: string }
          failure.code = 'XMAX_SESSION_UID_TIMEOUT'
          failure.stage = 'get_xmax_session_uid'
          failure.availableFields = fieldNames
          failure.rawUidType = 'timeout'
          reject(failure)
        }, 5000)
      }),
    ])
    const rawUidType = typeof rawUid
    if (typeof rawUid === 'string' && rawUid.trim()) return rawUid.trim()
    const failure = new Error('Xmax session UID is missing.') as Error & { code?: string; stage?: string; availableFields?: string[]; rawUidType?: string }
    failure.code = 'XMAX_SESSION_UID_MISSING'
    failure.stage = 'get_xmax_session_uid'
    failure.availableFields = fieldNames
    failure.rawUidType = rawUidType
    throw failure
  } finally {
    if (timeout !== null) window.clearTimeout(timeout)
  }
}

export function maxSessionMsForDuration(durationSeconds: number) {
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) return UNKNOWN_DURATION_SESSION_MS
  return Math.min(MAX_SESSION_MS, Math.max(0, durationSeconds * 1000 + 3000))
}

function safeErrorMessage(error: unknown, fallback: string) {
  const text = typeof error === 'string' ? error : error instanceof Error ? error.message : ''
  const cleaned = text.replace(/Bearer\s+[^\s]+/gi, 'Bearer [REDACTED]').trim()
  return cleaned.slice(0, 500) || fallback
}

function safeAuditText(value: unknown, limit = 8000) {
  if (value === null || value === undefined) return ''
  let text = typeof value === 'string' ? value : (() => {
    try { return JSON.stringify(value) } catch { return String(value) }
  })()
  text = text.replace(/Bearer\s+[^\s,;}]+/gi, 'Bearer [REDACTED]')
  text = text.replace(/((?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|secret|password)\s*[=:]\s*)([^\s,;}]+)/gi, '$1[REDACTED]')
  return text.slice(0, limit)
}

function providerAuditFromError(error: unknown, fallbackMessage = ''): XmaxProviderAudit {
  const raw = (error && typeof error === 'object' ? error : {}) as Record<string, unknown>
  const response = raw.response && typeof raw.response === 'object' ? raw.response as Record<string, unknown> : {}
  const request = raw.request && typeof raw.request === 'object' ? raw.request as Record<string, unknown> : {}
  const statusValue = raw.status ?? raw.statusCode ?? response.status
  const status = Number.isFinite(Number(statusValue)) ? Number(statusValue) : null
  const body = raw.body ?? raw.responseBody ?? response.body ?? response.data ?? raw.data
  return {
    requestMethod: String(raw.method ?? request.method ?? '').slice(0, 20),
    requestUrl: safeAuditText(raw.url ?? request.url ?? response.url ?? ''),
    httpStatus: status,
    responseBody: safeAuditText(body),
    errorName: String(raw.name || 'Error').slice(0, 120),
    errorCode: String(raw.code || raw.errorCode || '').slice(0, 160),
    errorMessage: safeAuditText(raw.message || fallbackMessage, 1000),
    errorStackHead: safeAuditText(String(raw.stack || '').split('\n').slice(0, 2).join(' | '), 1000),
  }
}

type InputStreamFailure = Error & { code?: string; stage?: string; diagnostics?: XmaxSessionDiagnostics }

function captureOutputStream(video: HTMLVideoElement) {
  const candidate = video as HTMLVideoElement & { captureStream?: () => MediaStream; mozCaptureStream?: () => MediaStream }
  if (typeof candidate.captureStream === 'function') return candidate.captureStream()
  if (typeof candidate.mozCaptureStream === 'function') return candidate.mozCaptureStream()
  throw new Error('This browser cannot capture the realtime output stream.')
}

function inputStreamFailure(code: string, message: string, diagnostics: XmaxSessionDiagnostics) {
  const failure = new Error(message) as InputStreamFailure
  failure.code = code
  failure.stage = 'input_video_stream'
  failure.diagnostics = { ...diagnostics }
  return failure
}

type XmaxCreateFailure = InputStreamFailure & { originalErrorName?: string; originalErrorCode?: string; originalErrorMessage?: string; originalErrorStackHead?: string }

function sessionObjectSummary(result: unknown) {
  const objectLike = result !== null && (typeof result === 'object' || typeof result === 'function')
  if (!objectLike) return { resultType: typeof result, resultConstructorName: '', fieldNames: '', methodNames: '', hasGetSessionUid: false, hasStop: false, hasClose: false, hasDisconnect: false }
  const names = new Set<string>()
  let current: object | null = result as object
  for (let depth = 0; current && depth < 3; depth += 1) {
    Object.getOwnPropertyNames(current).forEach((name) => names.add(name))
    current = Object.getPrototypeOf(current) as object | null
  }
  const descriptors = (name: string) => {
    let owner: object | null = result as object
    for (let depth = 0; owner && depth < 3; depth += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(owner, name)
      if (descriptor) return descriptor
      owner = Object.getPrototypeOf(owner) as object | null
    }
    return undefined
  }
  const fieldNames = [...names].filter((name) => name !== 'constructor').sort()
  const methodNames = fieldNames.filter((name) => typeof descriptors(name)?.value === 'function')
  const constructorName = (result as { constructor?: { name?: string } }).constructor?.name || ''
  return {
    resultType: typeof result,
    resultConstructorName: constructorName,
    fieldNames: fieldNames.join(', '),
    methodNames: methodNames.join(', '),
    hasGetSessionUid: methodNames.includes('getSessionUid'),
    hasStop: methodNames.includes('stop'),
    hasClose: methodNames.includes('close'),
    hasDisconnect: methodNames.includes('disconnect'),
  }
}

function classifyXmaxCreateError(error: unknown) {
  const candidate = error as { name?: string; code?: string; message?: string; status?: number; response?: { status?: number } }
  const name = String(candidate?.name || 'Error')
  const originalCode = String(candidate?.code || '')
  const message = safeErrorMessage(candidate?.message || error, 'Xmax Session creation failed.')
  const status = Number(candidate?.status || candidate?.response?.status || 0)
  const text = `${name} ${originalCode} ${message}`.toLowerCase()
  if (status === 429 || /overload|rate limit|too many requests|capacity|busy/.test(text)) return { code: 'XMAX_PROVIDER_OVERLOADED', message: `Xmax is currently overloaded: ${message}` }
  if (status === 401 || status === 403 || /api key|apikey|unauthorized|forbidden|credential|authentication|configuration/.test(text)) return { code: 'XMAX_CONFIGURATION_ERROR', message: `Xmax configuration or authentication failed: ${message}` }
  if (/media|stream|track|video|input|invalid parameter|invalid argument|bad request/.test(text)) return { code: 'XMAX_INVALID_INPUT', message: `Xmax rejected the input media: ${message}` }
  if (name === 'TypeError' || /network|fetch|timeout|socket|connection|cors/.test(text)) return { code: 'XMAX_NETWORK_ERROR', message: `Unable to connect to Xmax: ${message}` }
  return { code: 'XMAX_CREATE_SESSION_FAILED', message }
}

function pickRecorderMimeType() {
  if (typeof MediaRecorder === 'undefined') return ''
  return ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm'].find((type) => MediaRecorder.isTypeSupported(type)) || ''
}

export class XmaxRealtimeAdapter {
  private client: XmaxClient | null = null
  private session: RealtimeSession | null = null
  private prewarmStream: MediaStream | null = null
  private finalInputStream: MediaStream | null = null
  private inputCanvas: HTMLCanvasElement | null = null
  private canvasContext: CanvasRenderingContext2D | null = null
  private canvasInputStream: MediaStream | null = null
  private canvasTrack: MediaStreamTrack | null = null
  private canvasFrameCallbackId: number | null = null
  private canvasAnimationFrameId: number | null = null
  private canvasDrawLoopVideo: HTMLVideoElement | null = null
  private canvasPreviousSample: number[] | null = null
  private canvasFailure: InputStreamFailure | null = null
  private canvasSampleChanged = false
  private canvasDrawCount = 0
  private outputStream: MediaStream | null = null
  private recorder: MediaRecorder | null = null
  private recorderChunks: Blob[] = []
  private recorderPromise: Promise<Blob | null> | null = null
  private resolveRecorder: ((blob: Blob | null) => void) | null = null
  private captureTimer: number | null = null
  private hardTimeoutTimer: number | null = null
  private outputWatchdogTimer: number | null = null
  private progressTimer: number | null = null
  private outputFrameCallbackId: number | null = null
  private outputFrameVideo: HTMLVideoElement | null = null
  private outputTimeUpdateHandler: (() => void) | null = null
  private inputFrameCallbackId: number | null = null
  private inputFramePollTimer: number | null = null
  private inputFrameTimeout: number | null = null
  private inputFrameVideo: HTMLVideoElement | null = null
  private inputVideoAppendedByAdapter = false
  private sourcePlaybackStartedAt = 0
  private lastOutputFrameAt = 0
  private currentOptions: XmaxRealtimeStartOptions | null = null
  private startupState: XmaxStartupState = 'idle'
  private startedAt = 0
  private stopping = false
  private stopPromise: Promise<void> | null = null
  private stopReason: XmaxStopReason | null = null
  private sessionId: string | null = null
  private activeConfirmationSource: string | null = null
  private sessionDiagnostics: XmaxSessionDiagnostics = {
    sessionObjectCreated: false,
    getSessionUidCalled: false,
    getSessionUidResolved: false,
    rawUidType: '',
    extractedSessionId: '',
    outputStreamReceived: false,
    outputVideoTrackCount: 0,
    activeConfirmationSource: '',
    activeWaitMs: 0,
    stopAttempted: false,
    stopCompleted: false,
    stopMethod: '',
    stopReason: '',
    inputVideoPlayCalled: false,
    inputVideoPlayResolved: false,
    inputVideoPaused: true,
    inputVideoCurrentTimeStart: 0,
    inputVideoCurrentTimeEnd: 0,
    videoFrameCallbackCount: 0,
    presentedFrames: 0,
    captureStreamCreated: false,
    captureVideoTrackCount: 0,
    captureTrackReadyState: '',
    captureTrackMuted: '',
    sdkCreateCalled: false,
    sdkCreateStartedAt: '',
    sdkCreateResolved: false,
    sdkCreateRejected: false,
    createSessionWaitMs: 0,
    resultType: '',
    resultConstructorName: '',
    resultFieldNames: '',
    resultMethodNames: '',
    resultHasGetSessionUid: false,
    resultHasStop: false,
    resultHasClose: false,
    resultHasDisconnect: false,
    errorName: '',
    errorCode: '',
    errorMessage: '',
    errorStackHead: '',
    providerRequestMethod: '',
    providerRequestUrl: '',
    providerHttpStatus: null,
    providerResponseBody: '',
    providerErrorCode: '',
    providerErrorMessage: '',
    cleanupCompleted: false,
    prewarmStreamCreated: false,
    prewarmTrackCount: 0,
    prewarmStreamStopped: false,
    finalStreamCreated: false,
    finalStreamActive: false,
    finalVideoTrackCount: 0,
    finalTrackId: '',
    finalTrackReadyState: '',
    finalTrackEnabled: '',
    finalTrackMuted: '',
    sdkReceivedFinalStream: false,
    videoDecoderReady: false,
    videoPlayResolved: false,
    canvasCreated: false,
    canvasContextCreated: false,
    canvasWidth: 0,
    canvasHeight: 0,
    canvasCaptureFps: 30,
    canvasDrawCount: 0,
    firstCanvasFrameTime: '',
    lastCanvasFrameTime: '',
    canvasStreamCreated: false,
    canvasStreamActive: false,
    canvasVideoTrackCount: 0,
    canvasTrackId: '',
    canvasTrackKind: '',
    canvasTrackReadyState: '',
    canvasTrackEnabled: '',
    canvasTrackMuted: '',
    sdkReceivedCanvasStream: false,
    finalInputStreamActive: false,
    finalInputVideoTrackCount: 0,
    finalInputTrackId: '',
    finalInputTrackReadyState: '',
    finalInputTrackEnabled: '',
    finalInputTrackMuted: '',
    finalInputTrackSettings: '',
    localElementTagName: '',
    remoteElementTagName: '',
    canvasDrawCountBeforeConnect: 0,
    videoCurrentTimeBeforeConnect: 0,
    startupState: 'idle',
    inputLoopDuringConnect: false,
    endedIgnoredDuringStartupCount: 0,
    sessionReady: false,
    loopDisabledAfterSession: false,
    sourceCurrentTimeWhenSessionReady: 0,
  }

  private emitDiagnostics() {
    this.currentOptions?.onSessionDiagnostics?.({ ...this.sessionDiagnostics })
  }

  private recordProviderError(error: unknown, fallbackMessage: string) {
    const audit = providerAuditFromError(error, fallbackMessage)
    this.sessionDiagnostics.providerRequestMethod = audit.requestMethod || this.sessionDiagnostics.providerRequestMethod
    this.sessionDiagnostics.providerRequestUrl = audit.requestUrl || this.sessionDiagnostics.providerRequestUrl
    this.sessionDiagnostics.providerHttpStatus = audit.httpStatus ?? this.sessionDiagnostics.providerHttpStatus
    this.sessionDiagnostics.providerResponseBody = audit.responseBody || this.sessionDiagnostics.providerResponseBody
    this.sessionDiagnostics.providerErrorCode = audit.errorCode || this.sessionDiagnostics.providerErrorCode
    this.sessionDiagnostics.providerErrorMessage = audit.errorMessage || this.sessionDiagnostics.providerErrorMessage
    this.emitDiagnostics()
    return audit
  }

  private resetDiagnostics() {
    this.sessionDiagnostics = {
      sessionObjectCreated: false,
      getSessionUidCalled: false,
      getSessionUidResolved: false,
      rawUidType: '',
      extractedSessionId: '',
      outputStreamReceived: false,
      outputVideoTrackCount: 0,
      activeConfirmationSource: '',
      activeWaitMs: 0,
      stopAttempted: false,
      stopCompleted: false,
      stopMethod: '',
      stopReason: '',
      inputVideoPlayCalled: false,
      inputVideoPlayResolved: false,
      inputVideoPaused: true,
      inputVideoCurrentTimeStart: 0,
      inputVideoCurrentTimeEnd: 0,
      videoFrameCallbackCount: 0,
      presentedFrames: 0,
      captureStreamCreated: false,
      captureVideoTrackCount: 0,
      captureTrackReadyState: '',
      captureTrackMuted: '',
      sdkCreateCalled: false,
      sdkCreateStartedAt: '',
      sdkCreateResolved: false,
      sdkCreateRejected: false,
      createSessionWaitMs: 0,
      resultType: '',
      resultConstructorName: '',
      resultFieldNames: '',
      resultMethodNames: '',
      resultHasGetSessionUid: false,
      resultHasStop: false,
      resultHasClose: false,
      resultHasDisconnect: false,
      errorName: '',
      errorCode: '',
      errorMessage: '',
      errorStackHead: '',
      providerRequestMethod: '',
      providerRequestUrl: '',
      providerHttpStatus: null,
      providerResponseBody: '',
      providerErrorCode: '',
      providerErrorMessage: '',
      cleanupCompleted: false,
      prewarmStreamCreated: false,
      prewarmTrackCount: 0,
      prewarmStreamStopped: false,
      finalStreamCreated: false,
      finalStreamActive: false,
      finalVideoTrackCount: 0,
      finalTrackId: '',
      finalTrackReadyState: '',
      finalTrackEnabled: '',
      finalTrackMuted: '',
      sdkReceivedFinalStream: false,
      videoDecoderReady: false,
      videoPlayResolved: false,
      canvasCreated: false,
      canvasContextCreated: false,
      canvasWidth: 0,
      canvasHeight: 0,
      canvasCaptureFps: 30,
      canvasDrawCount: 0,
      firstCanvasFrameTime: '',
      lastCanvasFrameTime: '',
      canvasStreamCreated: false,
      canvasStreamActive: false,
      canvasVideoTrackCount: 0,
      canvasTrackId: '',
      canvasTrackKind: '',
      canvasTrackReadyState: '',
      canvasTrackEnabled: '',
      canvasTrackMuted: '',
      sdkReceivedCanvasStream: false,
      finalInputStreamActive: false,
      finalInputVideoTrackCount: 0,
      finalInputTrackId: '',
      finalInputTrackReadyState: '',
      finalInputTrackEnabled: '',
      finalInputTrackMuted: '',
      finalInputTrackSettings: '',
      localElementTagName: '',
      remoteElementTagName: '',
      canvasDrawCountBeforeConnect: 0,
      videoCurrentTimeBeforeConnect: 0,
      startupState: 'idle',
      inputLoopDuringConnect: false,
      endedIgnoredDuringStartupCount: 0,
      sessionReady: false,
      loopDisabledAfterSession: false,
      sourceCurrentTimeWhenSessionReady: 0,
    }
  }

  private setStartupState(state: XmaxStartupState) {
    this.startupState = state
    this.sessionDiagnostics.startupState = state
    this.emitDiagnostics()
    console.debug('[Xmax startup lifecycle]', {
      startupState: state,
      inputLoopDuringConnect: this.sessionDiagnostics.inputLoopDuringConnect,
      endedIgnoredDuringStartupCount: this.sessionDiagnostics.endedIgnoredDuringStartupCount,
      sessionReady: this.sessionDiagnostics.sessionReady,
      loopDisabledAfterSession: this.sessionDiagnostics.loopDisabledAfterSession,
      sourceCurrentTimeWhenSessionReady: this.sessionDiagnostics.sourceCurrentTimeWhenSessionReady,
    })
  }

  handleInputEnded() {
    const video = this.currentOptions?.inputVideo
    if (!video) return
    if (this.startupState === 'preparing' || this.startupState === 'connecting') {
      this.sessionDiagnostics.endedIgnoredDuringStartupCount += 1
      video.loop = true
      this.sessionDiagnostics.inputLoopDuringConnect = true
      if (video.ended) {
        try {
          video.currentTime = 0
          void video.play().then(() => {
            if (!this.stopping && (this.startupState === 'preparing' || this.startupState === 'connecting')) {
              this.stopCanvasDrawing()
              this.startCanvasDrawing(video)
            }
          }).catch(() => undefined)
        } catch {
          // Media errors are reported by the existing video error path.
        }
      }
      this.emitDiagnostics()
      return
    }
    if (this.startupState === 'session-ready') void this.finishStop('source-ended')
  }

  async start(options: XmaxRealtimeStartOptions) {
    if (this.session || this.stopping || this.stopPromise || this.stopReason) throw new Error('The Xmax session start was cancelled or is already running.')
    const apiKey = requireXmaxTemporaryApiKey()
    if (!options.inputVideo.src) throw new Error('The source video is not ready.')

    this.currentOptions = options
    options.onLifecycleMarker?.('adapter_called')
    options.onLifecycleMarker?.('adapter_method_name', 'XmaxRealtimeAdapter.start')
    this.startedAt = performance.now()
    this.stopping = false
    this.stopReason = null
    this.sessionId = null
    this.activeConfirmationSource = null
    this.resetDiagnostics()
    this.setStartupState('preparing')
    options.inputVideo.loop = true
    this.sessionDiagnostics.inputLoopDuringConnect = true
    this.emitDiagnostics()
    options.onStatus?.('preparing')
    try {
      await this.prepareInputStream(options.inputVideo)
      await this.startCanvasSessionInput(options.inputVideo)
    } catch (error) {
      const typed = error as InputStreamFailure
      typed.diagnostics = { ...this.sessionDiagnostics }
      await this.finishStop('sdk-error')
      typed.diagnostics = { ...this.sessionDiagnostics }
      throw typed
    }
    const model = models.realtime('x2.0')
    const client = createXmaxClient({
      apiKey,
      onError: (message, error) => {
        const fallback = safeErrorMessage(error || message, 'Realtime connection failed.')
        const audit = this.recordProviderError(error || message, fallback)
        options.onError?.(fallback, audit)
        void this.finishStop('sdk-error')
      },
    })
    this.client = client
    options.inputVideo.loop = true
    this.sessionDiagnostics.inputLoopDuringConnect = true
    this.setStartupState('connecting')
    options.onStatus?.('connecting')
    const canvasStream = this.canvasInputStream
    if (!canvasStream) {
      const failure = inputStreamFailure('XMAX_CANVAS_VIDEO_TRACK_MISSING', 'Canvas captureStream did not produce a live video track.', this.sessionDiagnostics)
      await this.finishStop('sdk-error')
      throw failure
    }
    this.assertCanvasInputStream(options.inputVideo, canvasStream)
    if (options.localElement.tagName !== 'DIV' || options.remoteElement.tagName !== 'DIV') {
      const failure = inputStreamFailure('XMAX_SDK_ELEMENT_CONTAINER_INVALID', 'Xmax realtime localElement and remoteElement must be DIV containers.', this.sessionDiagnostics)
      await this.finishStop('sdk-error')
      throw failure
    }
    const canvasTrack = canvasStream.getVideoTracks().find((track) => track.readyState === 'live' && track.enabled)
    if (!canvasTrack) {
      const failure = inputStreamFailure('XMAX_CANVAS_VIDEO_TRACK_MISSING', 'Canvas captureStream did not produce a live video track.', this.sessionDiagnostics)
      await this.finishStop('sdk-error')
      throw failure
    }
    const inputStream = new MediaStream([canvasTrack])
    this.finalInputStream = inputStream
    this.updateFinalInputStreamDiagnostics(inputStream, options)
    this.sessionDiagnostics.sdkReceivedCanvasStream = true
    this.emitDiagnostics()
    const referenceImageUrl = typeof options.referenceImage === 'string'
      ? options.referenceImage
      : await client.files.uploadAndCheckImage(
          options.referenceImage instanceof File
            ? options.referenceImage
            : new File([options.referenceImage], 'xmax-reference-image', { type: options.referenceImage.type || 'image/png' }),
        )
    try {
      this.session = await this.createXmaxSessionWithDiagnostics(() => client.realtime.connect(inputStream, {
        model,
        stream: {
          width: this.sessionDiagnostics.canvasWidth,
          height: this.sessionDiagnostics.canvasHeight,
          fps: this.sessionDiagnostics.canvasCaptureFps,
        },
        render: {
          localContainer: options.localElement,
          remoteContainer: options.remoteElement,
          drag: { enabled: false },
          mirror: false,
        },
        context: { prompt: options.prompt, refImageUrl: referenceImageUrl },
        autoStart: true,
        onRemoteStream: (stream) => {
          this.attachOutputStream(stream)
          options.onRemoteStream?.(stream)
        },
        onError: (message, error) => {
          const fallback = safeErrorMessage(error || message, 'Realtime generation failed.')
          const audit = this.recordProviderError(error || message, fallback)
          options.onError?.(fallback, audit)
          void this.finishStop('sdk-error')
        },
        onDisconnect: (reason) => {
          if (!this.stopping && reason !== 'client') {
            options.onError?.(`Realtime connection closed: ${reason}`)
            void this.finishStop('network-error')
          }
        },
      }))
    } catch (error) {
      const typed = error as InputStreamFailure
      typed.diagnostics = { ...this.sessionDiagnostics }
      await this.finishStop('sdk-error')
      typed.diagnostics = { ...this.sessionDiagnostics }
      throw typed
    }
    if (this.stopReason) {
      await this.closeLateCreatedSession()
      return
    }
    options.inputVideo.loop = false
    this.sessionDiagnostics.sessionReady = true
    this.sessionDiagnostics.loopDisabledAfterSession = true
    this.sessionDiagnostics.sourceCurrentTimeWhenSessionReady = options.inputVideo.currentTime
    this.setStartupState('session-ready')
    this.sessionDiagnostics.getSessionUidCalled = true
    this.emitDiagnostics()
    let sessionId: string
    try {
      sessionId = await extractXmaxSessionUid(this.session)
      this.sessionDiagnostics.getSessionUidResolved = true
      this.sessionDiagnostics.rawUidType = 'string'
      this.sessionDiagnostics.extractedSessionId = sessionId
      this.emitDiagnostics()
    } catch (error) {
      const typed = error as Error & { rawUidType?: string; diagnostics?: XmaxSessionDiagnostics }
      this.sessionDiagnostics.rawUidType = typed.rawUidType || this.sessionDiagnostics.rawUidType
      typed.diagnostics = { ...this.sessionDiagnostics }
      this.emitDiagnostics()
      await this.finishStop('sdk-error')
      typed.diagnostics = { ...this.sessionDiagnostics }
      throw typed
    }
    this.sessionId = sessionId
    try {
      await options.onSessionCreated?.(sessionId)
    } catch (error) {
      await this.finishStop('sdk-error')
      throw error
    }
    if (this.stopReason) {
      await this.closeLateCreatedSession()
      return
    }
    let activeSource: string
    try {
      activeSource = await this.waitForOutputReady(options, 10000)
    } catch (error) {
      const code = (error as Error & { code?: string }).code
      const waitError = error as Error & { activeWaitMs?: number; activeConfirmationSource?: string }
      this.sessionDiagnostics.activeWaitMs = waitError.activeWaitMs || this.sessionDiagnostics.activeWaitMs
      this.sessionDiagnostics.activeConfirmationSource = waitError.activeConfirmationSource || this.sessionDiagnostics.activeConfirmationSource
      this.emitDiagnostics()
      await this.finishStop(code === 'XMAX_OUTPUT_READY_TIMEOUT' ? 'output-ready-timeout' : 'sdk-error')
      const typed = error as Error & { diagnostics?: XmaxSessionDiagnostics }
      typed.diagnostics = { ...this.sessionDiagnostics }
      throw error
    }
    if (this.stopReason) {
      await this.closeLateCreatedSession()
      return
    }
    try {
      await options.onSessionActive?.(sessionId, activeSource)
    } catch (error) {
      await this.finishStop('sdk-error')
      throw error
    }
    if (this.stopReason) {
      await this.closeLateCreatedSession()
      return
    }
    options.onStatus?.('generating')
    this.captureOutputWhenReady(options.outputVideo)
    this.startSessionWatchdogs(options)
  }

  private async createXmaxSessionWithDiagnostics(createSession: () => Promise<RealtimeSession>) {
    this.currentOptions?.onLifecycleMarker?.('sdk_diagnostics_wrapper_entered')
    const startedAt = performance.now()
    const startedAtIso = new Date().toISOString()
    this.sessionDiagnostics.sdkCreateCalled = true
    this.currentOptions?.onLifecycleMarker?.('sdk_create_called')
    this.sessionDiagnostics.sdkCreateStartedAt = startedAtIso
    this.emitDiagnostics()
    let timeout: number | null = null
    let resolved = false
    try {
      const result = await Promise.race<unknown>([
        Promise.resolve().then(createSession),
        new Promise<never>((_, reject) => {
          timeout = window.setTimeout(() => {
            const failure = new Error('The input video stream was flowing, but Xmax did not return a Session object in time.') as XmaxCreateFailure
            failure.code = 'XMAX_CREATE_SESSION_TIMEOUT'
            failure.stage = 'create_xmax_session'
            reject(failure)
          }, 10000)
        }),
      ])
      resolved = true
      const summary = sessionObjectSummary(result)
      this.sessionDiagnostics.sdkCreateResolved = true
      this.sessionDiagnostics.resultType = summary.resultType
      this.sessionDiagnostics.resultConstructorName = summary.resultConstructorName
      this.sessionDiagnostics.resultFieldNames = summary.fieldNames
      this.sessionDiagnostics.resultMethodNames = summary.methodNames
      this.sessionDiagnostics.resultHasGetSessionUid = summary.hasGetSessionUid
      this.sessionDiagnostics.resultHasStop = summary.hasStop
      this.sessionDiagnostics.resultHasClose = summary.hasClose
      this.sessionDiagnostics.resultHasDisconnect = summary.hasDisconnect
      this.emitDiagnostics()
      if (!result || !['object', 'function'].includes(typeof result)) {
        const failure = new Error('Xmax createSession resolved without returning a Session object.') as XmaxCreateFailure
        failure.code = 'XMAX_CREATE_SESSION_EMPTY_RESULT'
        failure.stage = 'create_xmax_session'
        throw failure
      }
      this.session = result as RealtimeSession
      this.sessionDiagnostics.sessionObjectCreated = true
      this.emitDiagnostics()
      if (!summary.hasGetSessionUid) {
        const businessError = /(^|, )(error|message|code|status)(,|$)/i.test(summary.fieldNames)
          ? classifyXmaxCreateError(result)
          : { code: 'XMAX_SESSION_UID_METHOD_MISSING', message: 'Xmax Session object does not expose getSessionUid.' }
        const failure = new Error(businessError.message) as XmaxCreateFailure
        failure.code = businessError.code
        failure.stage = 'create_xmax_session'
        throw failure
      }
      return this.session
    } catch (error) {
      const typed = error as XmaxCreateFailure
      const raw = error as { name?: string; code?: string; message?: string; stack?: string }
      const originalMessage = safeErrorMessage(raw?.message || error, 'Xmax Session creation failed.')
      this.sessionDiagnostics.createSessionWaitMs = Math.round(performance.now() - startedAt)
      this.sessionDiagnostics.sdkCreateRejected = !resolved
      this.sessionDiagnostics.errorName = raw?.name || 'Error'
      this.sessionDiagnostics.errorCode = raw?.code || ''
      this.sessionDiagnostics.errorMessage = originalMessage
      this.sessionDiagnostics.errorStackHead = raw?.stack?.split('\n').slice(0, 2).join(' | ') || ''
      const providerAudit = this.recordProviderError(error, originalMessage)
      this.sessionDiagnostics.providerErrorCode = providerAudit.errorCode || this.sessionDiagnostics.providerErrorCode
      this.sessionDiagnostics.providerErrorMessage = providerAudit.errorMessage || this.sessionDiagnostics.providerErrorMessage
      if (!typed.code?.startsWith('XMAX_')) {
        const classified = classifyXmaxCreateError(error)
        typed.code = classified.code
        typed.message = classified.message
      }
      typed.stage = typed.stage || 'create_xmax_session'
      typed.originalErrorName = this.sessionDiagnostics.errorName
      typed.originalErrorCode = this.sessionDiagnostics.errorCode
      typed.originalErrorMessage = this.sessionDiagnostics.errorMessage
      typed.originalErrorStackHead = this.sessionDiagnostics.errorStackHead
      typed.diagnostics = { ...this.sessionDiagnostics }
      this.emitDiagnostics()
      throw typed
    } finally {
      if (timeout !== null) window.clearTimeout(timeout)
      this.sessionDiagnostics.createSessionWaitMs = Math.round(performance.now() - startedAt)
      this.emitDiagnostics()
    }
  }

  private async waitForOutputReady(options: XmaxRealtimeStartOptions, timeoutMs: number) {
    if (this.activeConfirmationSource) return this.activeConfirmationSource
    const output = options.outputVideo
    const hasOutput = () => {
      if (this.activeConfirmationSource) return this.activeConfirmationSource
      if (output.srcObject instanceof MediaStream && output.srcObject.getVideoTracks().length > 0) return 'output_media_stream'
      if (output.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && output.videoWidth > 0 && output.videoHeight > 0) return 'output_video_ready'
      return ''
    }
    const immediate = hasOutput()
    if (immediate) {
      this.sessionDiagnostics.activeConfirmationSource = immediate
      this.sessionDiagnostics.activeWaitMs = 0
      this.emitDiagnostics()
      return immediate
    }
    return await new Promise<string>((resolve, reject) => {
      let settled = false
      const startedAt = performance.now()
      const cleanup = () => {
        window.clearInterval(poll)
        window.clearTimeout(timeout)
        output.removeEventListener('loadedmetadata', check)
        output.removeEventListener('loadeddata', check)
        output.removeEventListener('canplay', check)
        output.removeEventListener('playing', check)
        output.removeEventListener('error', onError)
      }
      const finish = (value?: string, reason?: Error) => {
        if (settled) return
        settled = true
        cleanup()
        if (reason) reject(reason)
        else {
          this.sessionDiagnostics.activeConfirmationSource = value || 'output_media_stream'
          this.sessionDiagnostics.activeWaitMs = Math.round(performance.now() - startedAt)
          this.emitDiagnostics()
          resolve(value || 'output_media_stream')
        }
      }
      const check = () => {
        const source = hasOutput()
        if (source) finish(source)
      }
      const onError = () => {
        const failure = new Error('Xmax output video reported a media error.') as Error & { code?: string; stage?: string; activeWaitMs?: number; activeConfirmationSource?: string }
        failure.code = 'XMAX_OUTPUT_MEDIA_ERROR'
        failure.stage = 'wait_xmax_output_ready'
        failure.activeWaitMs = Math.round(performance.now() - startedAt)
        failure.activeConfirmationSource = this.activeConfirmationSource || ''
        finish(undefined, failure)
      }
      const poll = window.setInterval(check, 100)
      const timeout = window.setTimeout(() => {
        const failure = new Error('Xmax session was created, but no usable output stream arrived within 10 seconds.') as Error & { code?: string; stage?: string; activeWaitMs?: number; activeConfirmationSource?: string }
        failure.code = 'XMAX_OUTPUT_READY_TIMEOUT'
        failure.stage = 'wait_xmax_output_ready'
        failure.activeWaitMs = Math.round(performance.now() - startedAt)
        failure.activeConfirmationSource = this.activeConfirmationSource || ''
        finish(undefined, failure)
      }, timeoutMs)
      output.addEventListener('loadedmetadata', check)
      output.addEventListener('loadeddata', check)
      output.addEventListener('canplay', check)
      output.addEventListener('playing', check)
      output.addEventListener('error', onError)
      check()
    })
  }

  private async closeLateCreatedSession() {
    if (this.stopPromise) await this.stopPromise
    await this.closeSession()
    this.session = null
    this.stopPrewarmStream()
    this.stopFinalInputStream()
    this.outputStream?.getTracks().forEach((track) => track.stop())
    this.clearInputFrameWatch()
    this.cleanupInputVideo()
    this.outputStream = null
    if (this.currentOptions?.outputVideo) this.currentOptions.outputVideo.srcObject = null
    this.sessionId = null
    this.client = null
    this.currentOptions = null
  }

  async finishStop(reason: XmaxStopReason) {
    if (this.stopPromise) {
      await this.stopPromise
      return
    }
    if (this.stopReason) return
    this.stopReason = reason
    this.stopping = true
    this.setStartupState('stopping')
    this.stopPromise = this.performFinishStop(reason)
    await this.stopPromise
    this.stopPromise = null
  }

  async stop(reason: 'completed' | 'stopped' = 'stopped') {
    if (reason === 'completed') {
      this.handleInputEnded()
      return
    }
    await this.finishStop('user-stop')
  }

  private async performFinishStop(reason: XmaxStopReason) {
    this.clearCaptureTimer()
    this.clearSessionWatchdogs()
    const options = this.currentOptions
    options?.onStatus?.('stopping')
    options?.onStopReason?.(reason)
    let result: XmaxRealtimeResult | null = null
    try {
      await this.closeSession()
      const blob = await this.stopRecorder()
      if (blob && options) {
        const objectUrl = URL.createObjectURL(blob)
        const elapsedSeconds = Math.max(0, (performance.now() - this.startedAt) / 1000)
        const processedSeconds = Math.min(options.sourceDuration || elapsedSeconds, elapsedSeconds)
        result = {
          objectUrl,
          blob,
          mimeType: blob.type || 'video/webm',
          durationSeconds: Number(elapsedSeconds.toFixed(2)),
          processedSeconds: Number(processedSeconds.toFixed(2)),
          estimatedCredits: Number(processedSeconds.toFixed(2)),
          style: options.style,
          targetLanguage: options.targetLanguage,
          characterName: options.characterName,
          createdAt: new Date().toISOString(),
        }
        options.onResult?.(result)
      }
      const failed = reason === 'hard-timeout' || reason === 'output-stalled' || reason === 'recorder-error' || reason === 'sdk-error' || reason === 'network-error'
      options?.onStatus?.(failed ? 'failed' : result && reason === 'source-ended' ? 'completed' : 'stopped')
    } catch (error) {
      options?.onError?.(safeErrorMessage(error, 'Stopping realtime generation failed.'))
      options?.onStatus?.('failed')
    } finally {
      if (options?.outputVideo) options.outputVideo.srcObject = null
      this.clearInputFrameWatch()
      this.stopPrewarmStream()
      this.stopFinalInputStream()
      this.outputStream?.getTracks().forEach((track) => track.stop())
      this.cleanupInputVideo()
      this.sessionDiagnostics.cleanupCompleted = true
      this.emitDiagnostics()
      this.session = null
      this.client = null
      this.outputStream = null
      this.currentOptions = null
      this.sessionId = null
      this.activeConfirmationSource = null
      this.stopping = false
    }
  }

  private cleanupInputVideo() {
    const video = this.inputFrameVideo || this.currentOptions?.inputVideo
    this.stopCanvasDrawing()
    if (video) {
      try {
        video.pause()
        video.removeAttribute('src')
        video.load()
      } catch {
        // Media cleanup is best effort and must not hide the original failure.
      }
      if (this.inputVideoAppendedByAdapter && video.parentElement) video.parentElement.removeChild(video)
    }
    if (this.inputCanvas?.parentElement) this.inputCanvas.parentElement.removeChild(this.inputCanvas)
    this.inputCanvas = null
    this.canvasContext = null
    this.canvasDrawLoopVideo = null
    this.canvasFailure = null
    this.inputFrameVideo = null
    this.inputVideoAppendedByAdapter = false
  }

  private stopPrewarmStream() {
    const stream = this.prewarmStream
    if (!stream) return
    stream.getTracks().forEach((track) => track.stop())
    this.prewarmStream = null
    this.sessionDiagnostics.prewarmStreamStopped = true
    this.emitDiagnostics()
  }

  private stopFinalInputStream() {
    const canvasStream = this.canvasInputStream
    const finalStream = this.finalInputStream
    this.stopCanvasInputStream()
    if (finalStream && finalStream !== canvasStream) finalStream.getTracks().forEach((track) => {
      if (track.readyState !== 'ended') track.stop()
    })
    this.finalInputStream = null
  }

  private stopCanvasInputStream() {
    const stream = this.canvasInputStream
    if (!stream) return
    stream.getTracks().forEach((track) => {
      if (track.readyState !== 'ended') track.stop()
    })
    this.canvasTrack = null
    this.canvasInputStream = null
    if (this.finalInputStream === stream) this.finalInputStream = null
  }

  async dispose(reason: XmaxStopReason = 'component-unmount') {
    await this.finishStop(reason)
  }

  getStopReason() {
    return this.stopReason
  }

  private async closeSession() {
    const session = this.session
    if (!session) return
    this.sessionDiagnostics.stopAttempted = true
    this.sessionDiagnostics.stopReason = this.stopReason || this.sessionDiagnostics.stopReason
    this.sessionDiagnostics.stopMethod = 'stopGeneration,disconnect'
    this.emitDiagnostics()
    const close = (async () => {
      try {
        await session.stopGeneration()
      } finally {
        await session.disconnect()
      }
    })()
    const completed = await Promise.race([close.then(() => true), new Promise<boolean>((resolve) => window.setTimeout(() => resolve(false), 2000))])
    this.sessionDiagnostics.stopCompleted = completed
    this.emitDiagnostics()
  }

  private startSessionWatchdogs(options: XmaxRealtimeStartOptions) {
    this.hardTimeoutTimer = window.setTimeout(() => { void this.finishStop('hard-timeout') }, maxSessionMsForDuration(options.sourceDuration))
    this.progressTimer = window.setInterval(() => {
      const elapsed = Math.max(0, (performance.now() - this.startedAt) / 1000)
      options.onProgress?.(Number(elapsed.toFixed(2)))
    }, 250)
    this.outputFrameVideo = options.outputVideo
    this.sourcePlaybackStartedAt = 0
    this.lastOutputFrameAt = 0
    this.outputTimeUpdateHandler = () => { this.lastOutputFrameAt = performance.now() }
    options.outputVideo.addEventListener('timeupdate', this.outputTimeUpdateHandler)
    this.scheduleOutputFrameCallback()
    this.outputWatchdogTimer = window.setInterval(() => this.checkOutputWatchdog(options), 250)
  }

  private scheduleOutputFrameCallback() {
    const video = this.outputFrameVideo as (HTMLVideoElement & { requestVideoFrameCallback?: (callback: (now: number, metadata: unknown) => void) => number }) | null
    if (!video || typeof video.requestVideoFrameCallback !== 'function' || this.stopping) return
    this.outputFrameCallbackId = video.requestVideoFrameCallback(() => {
      if (this.stopping) return
      this.lastOutputFrameAt = performance.now()
      this.scheduleOutputFrameCallback()
    })
  }

  private checkOutputWatchdog(options: XmaxRealtimeStartOptions) {
    if (this.stopping) return
    const input = options.inputVideo
    const playing = !input.paused && !input.ended && input.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
    if (!playing) {
      this.sourcePlaybackStartedAt = 0
      return
    }
    const now = performance.now()
    if (!this.sourcePlaybackStartedAt) {
      this.sourcePlaybackStartedAt = now
      this.lastOutputFrameAt = now
      return
    }
    if (now - this.sourcePlaybackStartedAt >= 3000 && now - this.lastOutputFrameAt >= 3000) void this.finishStop('output-stalled')
  }

  private clearSessionWatchdogs() {
    if (this.hardTimeoutTimer !== null) window.clearTimeout(this.hardTimeoutTimer)
    if (this.outputWatchdogTimer !== null) window.clearInterval(this.outputWatchdogTimer)
    if (this.progressTimer !== null) window.clearInterval(this.progressTimer)
    this.hardTimeoutTimer = null
    this.outputWatchdogTimer = null
    this.progressTimer = null
    const video = this.outputFrameVideo as (HTMLVideoElement & { cancelVideoFrameCallback?: (id: number) => void }) | null
    if (video && this.outputFrameCallbackId !== null && typeof video.cancelVideoFrameCallback === 'function') video.cancelVideoFrameCallback(this.outputFrameCallbackId)
    if (video && this.outputTimeUpdateHandler) video.removeEventListener('timeupdate', this.outputTimeUpdateHandler)
    this.outputFrameCallbackId = null
    this.outputFrameVideo = null
    this.outputTimeUpdateHandler = null
    this.sourcePlaybackStartedAt = 0
    this.lastOutputFrameAt = 0
  }

  private prepareInputVideoElement(video: HTMLVideoElement) {
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
    if (!document.body.contains(video)) {
      document.body.appendChild(video)
      this.inputVideoAppendedByAdapter = true
    }
    this.inputFrameVideo = video
  }

  private async waitForInputSeek(video: HTMLVideoElement) {
    if (video.readyState >= HTMLMediaElement.HAVE_METADATA && Math.abs(video.currentTime) < 0.001) return
    await new Promise<void>((resolve, reject) => {
      let settled = false
      const timeout = window.setTimeout(() => finish(new Error('The Shot video could not seek to its first frame.')), 3000)
      const cleanup = () => {
        window.clearTimeout(timeout)
        video.removeEventListener('seeked', done)
        video.removeEventListener('error', fail)
      }
      const finish = (reason?: Error) => {
        if (settled) return
        settled = true
        cleanup()
        if (reason) reject(reason)
        else resolve()
      }
      const done = () => finish()
      const fail = () => finish(new Error('The Shot video reported a media error while seeking.'))
      video.addEventListener('seeked', done, { once: true })
      video.addEventListener('error', fail, { once: true })
      if (Math.abs(video.currentTime) < 0.001) finish()
    })
  }

  private async waitForInputFrames(video: HTMLVideoElement, timeoutMs = 3000, minimumFrames = 2) {
    this.clearInputFrameWatch()
    this.inputFrameVideo = video
    const frameVideo = video as HTMLVideoElement & {
      requestVideoFrameCallback?: (callback: (now: number, metadata: { mediaTime?: number; presentedFrames?: number }) => void) => number
      cancelVideoFrameCallback?: (id: number) => void
    }
    const startedCurrentTime = video.currentTime
    this.sessionDiagnostics.inputVideoCurrentTimeStart = startedCurrentTime
    return await new Promise<void>((resolve, reject) => {
      let settled = false
      let callbackCount = 0
      let previousTime = startedCurrentTime
      let previousMediaTime = startedCurrentTime
      let previousPresentedFrames = this.sessionDiagnostics.presentedFrames
      const startedAt = performance.now()
      const cleanup = () => {
        if (this.inputFrameTimeout !== null) window.clearTimeout(this.inputFrameTimeout)
        if (this.inputFramePollTimer !== null) window.clearInterval(this.inputFramePollTimer)
        if (this.inputFrameCallbackId !== null && typeof frameVideo.cancelVideoFrameCallback === 'function') frameVideo.cancelVideoFrameCallback(this.inputFrameCallbackId)
        this.inputFrameTimeout = null
        this.inputFramePollTimer = null
        this.inputFrameCallbackId = null
        video.removeEventListener('error', onError)
        this.inputFrameVideo = null
      }
      const fail = (reason: Error) => {
        if (settled) return
        settled = true
        cleanup()
        this.sessionDiagnostics.inputVideoCurrentTimeEnd = video.currentTime
        reject(reason)
      }
      const completeIfMoving = (mediaTime?: number, presentedFrames?: number) => {
        callbackCount += 1
        this.sessionDiagnostics.videoFrameCallbackCount += 1
        if (typeof presentedFrames === 'number' && Number.isFinite(presentedFrames)) this.sessionDiagnostics.presentedFrames = Math.max(this.sessionDiagnostics.presentedFrames, presentedFrames)
        const currentTime = video.currentTime
        const mediaMoved = typeof mediaTime === 'number' && mediaTime > previousMediaTime + 0.0005
        const presentedMoved = typeof presentedFrames === 'number' && presentedFrames > previousPresentedFrames
        const currentTimeMoved = currentTime > previousTime + 0.0005
        previousTime = currentTime
        previousMediaTime = typeof mediaTime === 'number' ? mediaTime : currentTime
        previousPresentedFrames = typeof presentedFrames === 'number' ? presentedFrames : previousPresentedFrames
        this.sessionDiagnostics.inputVideoCurrentTimeEnd = currentTime
        if (video.paused || video.ended || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || video.videoWidth <= 0 || video.videoHeight <= 0) return
        if (callbackCount >= minimumFrames && (mediaMoved || presentedMoved || currentTimeMoved)) {
          settled = true
          cleanup()
          resolve()
        }
      }
      const onError = () => fail(inputStreamFailure('XMAX_INPUT_VIDEO_MEDIA_ERROR', 'The Shot video reported a media error while playing.', this.sessionDiagnostics))
      const onPoll = () => {
        if (this.stopping) {
          fail(inputStreamFailure('XMAX_INPUT_STREAM_CANCELLED', 'The Shot video stream check was cancelled.', this.sessionDiagnostics))
          return
        }
        completeIfMoving(video.currentTime, undefined)
      }
      const onFrame = (_now: number, metadata: { mediaTime?: number; presentedFrames?: number }) => {
        if (this.stopping) {
          fail(inputStreamFailure('XMAX_INPUT_STREAM_CANCELLED', 'The Shot video stream check was cancelled.', this.sessionDiagnostics))
          return
        }
        completeIfMoving(metadata.mediaTime, metadata.presentedFrames)
        if (!settled && typeof frameVideo.requestVideoFrameCallback === 'function') this.inputFrameCallbackId = frameVideo.requestVideoFrameCallback(onFrame)
      }
      video.addEventListener('error', onError)
      if (typeof frameVideo.requestVideoFrameCallback === 'function') this.inputFrameCallbackId = frameVideo.requestVideoFrameCallback(onFrame)
      else this.inputFramePollTimer = window.setInterval(onPoll, 100)
      this.inputFrameTimeout = window.setTimeout(() => {
        const elapsed = Math.round(performance.now() - startedAt)
        const failure = inputStreamFailure('XMAX_INPUT_STREAM_NOT_FLOWING', 'The Shot video loaded, but no playable frames were produced.', this.sessionDiagnostics)
        failure.message = `${failure.message} (${elapsed}ms)`
        fail(failure)
      }, timeoutMs)
    })
  }

  private clearInputFrameWatch() {
    if (this.inputFrameTimeout !== null) window.clearTimeout(this.inputFrameTimeout)
    if (this.inputFramePollTimer !== null) window.clearInterval(this.inputFramePollTimer)
    const video = this.inputFrameVideo as (HTMLVideoElement & { cancelVideoFrameCallback?: (id: number) => void }) | null
    if (video && this.inputFrameCallbackId !== null && typeof video.cancelVideoFrameCallback === 'function') video.cancelVideoFrameCallback(this.inputFrameCallbackId)
    this.inputFrameTimeout = null
    this.inputFramePollTimer = null
    this.inputFrameCallbackId = null
    this.inputFrameVideo = null
  }

  private validateInputCaptureStream(video: HTMLVideoElement, stream: MediaStream, requireUnmuted = false) {
    this.sessionDiagnostics.captureStreamCreated = true
    const tracks = stream.getVideoTracks()
    this.sessionDiagnostics.captureVideoTrackCount = tracks.length
    const track = tracks[0]
    this.sessionDiagnostics.captureTrackReadyState = track?.readyState || ''
    this.sessionDiagnostics.captureTrackMuted = typeof track?.muted === 'boolean' ? String(track.muted) : ''
    this.emitDiagnostics()
    if (!tracks.length) throw inputStreamFailure('XMAX_INPUT_CAPTURE_NO_VIDEO_TRACK', 'The captured Shot video stream has no video track.', this.sessionDiagnostics)
    if (track.readyState !== 'live') throw inputStreamFailure('XMAX_INPUT_CAPTURE_TRACK_NOT_LIVE', 'The captured Shot video track is not live.', this.sessionDiagnostics)
    if (!track.enabled) throw inputStreamFailure('XMAX_INPUT_CAPTURE_TRACK_DISABLED', 'The captured Shot video track is disabled.', this.sessionDiagnostics)
    if (requireUnmuted && track.muted === true) throw inputStreamFailure('XMAX_INPUT_CAPTURE_TRACK_MUTED', 'The captured Shot video track remained muted after playback started.', this.sessionDiagnostics)
    if (requireUnmuted && (!stream.active || video.paused || video.ended || video.currentTime <= this.sessionDiagnostics.inputVideoCurrentTimeStart)) throw inputStreamFailure('XMAX_FINAL_VIDEO_TRACK_MISSING', 'The final MediaStream did not contain a live video track.', this.sessionDiagnostics)
    const settings = typeof track.getSettings === 'function' ? track.getSettings() : {}
    if ((settings.width !== undefined && settings.width <= 0) || (settings.height !== undefined && settings.height <= 0) || video.videoWidth <= 0 || video.videoHeight <= 0) {
      throw inputStreamFailure('XMAX_INPUT_CAPTURE_INVALID_DIMENSIONS', 'The captured Shot video track has invalid dimensions.', this.sessionDiagnostics)
    }
  }

  private updateFinalStreamDiagnostics(video: HTMLVideoElement, stream: MediaStream) {
    const track = stream.getVideoTracks()[0]
    this.sessionDiagnostics.finalStreamActive = stream.active
    this.sessionDiagnostics.finalVideoTrackCount = stream.getVideoTracks().length
    this.sessionDiagnostics.finalTrackId = track?.id || ''
    this.sessionDiagnostics.finalTrackReadyState = track?.readyState || ''
    this.sessionDiagnostics.finalTrackEnabled = typeof track?.enabled === 'boolean' ? String(track.enabled) : ''
    this.sessionDiagnostics.finalTrackMuted = typeof track?.muted === 'boolean' ? String(track.muted) : ''
    this.sessionDiagnostics.inputVideoPaused = video.paused
    this.sessionDiagnostics.inputVideoCurrentTimeEnd = video.currentTime
    this.emitDiagnostics()
  }

  private async waitForFinalInputStream(video: HTMLVideoElement, stream: MediaStream, timeoutMs = 3000) {
    const startedCurrentTime = video.currentTime
    await new Promise<void>((resolve, reject) => {
      let settled = false
      const cleanup = () => {
        window.clearInterval(poll)
        window.clearTimeout(timeout)
        stream.removeEventListener('addtrack', check)
      }
      const finish = (reason?: Error) => {
        if (settled) return
        settled = true
        cleanup()
        if (reason) reject(reason)
        else resolve()
      }
      const check = () => {
        this.updateFinalStreamDiagnostics(video, stream)
        const track = stream.getVideoTracks().find((candidate) => candidate.readyState === 'live' && candidate.enabled)
        const currentTimeMoving = video.currentTime > startedCurrentTime + 0.0005
        if (stream.active && track && !video.paused && !video.ended && currentTimeMoving && video.videoWidth > 0 && video.videoHeight > 0) {
          finish()
        }
      }
      const poll = window.setInterval(check, 75)
      const timeout = window.setTimeout(() => {
        this.updateFinalStreamDiagnostics(video, stream)
        finish(inputStreamFailure('XMAX_FINAL_VIDEO_TRACK_MISSING', 'The final MediaStream did not contain a live video track.', this.sessionDiagnostics))
      }, timeoutMs)
      stream.addEventListener('addtrack', check)
      check()
    })
  }

  private async prepareInputStream(video: HTMLVideoElement) {
    this.prepareInputVideoElement(video)
    if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
      await new Promise<void>((resolve, reject) => {
        let settled = false
        const timeout = window.setTimeout(() => finish(inputStreamFailure('XMAX_INPUT_VIDEO_METADATA_TIMEOUT', 'The Shot video metadata did not become ready.', this.sessionDiagnostics)), 3000)
        const cleanup = () => {
          window.clearTimeout(timeout)
          video.removeEventListener('loadeddata', done)
          video.removeEventListener('canplay', done)
          video.removeEventListener('error', onError)
        }
        const finish = (reason?: Error) => { if (settled) return; settled = true; cleanup(); reason ? reject(reason) : resolve() }
        const done = () => finish()
        const onError = () => finish(inputStreamFailure('XMAX_INPUT_VIDEO_MEDIA_ERROR', 'The Shot video reported a media error before playback.', this.sessionDiagnostics))
        video.addEventListener('loadeddata', done, { once: true })
        video.addEventListener('canplay', done, { once: true })
        video.addEventListener('error', onError, { once: true })
      })
    }
    if (video.videoWidth <= 0 || video.videoHeight <= 0 || !Number.isFinite(video.duration) || video.duration <= 0) throw inputStreamFailure('XMAX_INPUT_VIDEO_INVALID_METADATA', 'The Shot video metadata is invalid.', this.sessionDiagnostics)
    this.sessionDiagnostics.videoDecoderReady = true
    this.sessionDiagnostics.inputVideoCurrentTimeStart = video.currentTime
    this.emitDiagnostics()

    const canvas = document.createElement('canvas')
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
    this.inputCanvas = canvas
    this.sessionDiagnostics.canvasCreated = true
    this.sessionDiagnostics.canvasWidth = canvas.width
    this.sessionDiagnostics.canvasHeight = canvas.height
    const context = canvas.getContext('2d', { alpha: false })
    if (!context) throw inputStreamFailure('XMAX_CANVAS_CONTEXT_UNAVAILABLE', 'The browser could not create a 2D canvas context.', this.sessionDiagnostics)
    this.canvasContext = context
    this.sessionDiagnostics.canvasContextCreated = true
    this.emitDiagnostics()
    canvas.style.position = 'fixed'
    canvas.style.left = '-10000px'
    canvas.style.top = '-10000px'
    canvas.style.width = '2px'
    canvas.style.height = '2px'
    canvas.style.opacity = '0.001'
    canvas.style.pointerEvents = 'none'
    document.body.appendChild(canvas)

    video.currentTime = 0
    await this.waitForInputSeek(video)
    this.sessionDiagnostics.inputVideoPlayCalled = true
    this.sessionDiagnostics.inputVideoCurrentTimeStart = video.currentTime
    this.emitDiagnostics()
    try {
      await video.play()
    } catch (reason) {
      throw inputStreamFailure('XMAX_INPUT_VIDEO_PLAY_FAILED', `The Shot video could not start playback: ${safeErrorMessage(reason, 'play() failed')}`, this.sessionDiagnostics)
    }
    this.sessionDiagnostics.inputVideoPlayResolved = true
    this.sessionDiagnostics.videoPlayResolved = true
    this.sessionDiagnostics.inputVideoPaused = video.paused
    this.emitDiagnostics()
    await this.playAndDrawCanvas(video, 2)
    try {
      this.canvasInputStream = canvas.captureStream(30)
    } catch (reason) {
      throw inputStreamFailure('XMAX_CANVAS_VIDEO_TRACK_MISSING', `Canvas captureStream failed: ${safeErrorMessage(reason, 'The browser cannot capture the canvas stream.')}`, this.sessionDiagnostics)
    }
    this.sessionDiagnostics.canvasCaptureFps = 30
    this.sessionDiagnostics.canvasStreamCreated = true
    this.sessionDiagnostics.canvasStreamActive = this.canvasInputStream.active
    this.sessionDiagnostics.canvasVideoTrackCount = this.canvasInputStream.getVideoTracks().length
    this.sessionDiagnostics.finalStreamCreated = true
    this.updateCanvasTrackDiagnostics()
    this.emitDiagnostics()
    await this.waitForCanvasStream(3000)

    this.stopCanvasDrawing()
    video.pause()
    this.sessionDiagnostics.inputVideoPaused = true
    video.currentTime = 0
    await this.waitForInputSeek(video)
    this.resetCanvasFrameDiagnostics()
    this.emitDiagnostics()
  }

  private async startCanvasSessionInput(video: HTMLVideoElement) {
    if (!this.inputCanvas || !this.canvasContext || !this.canvasInputStream) throw inputStreamFailure('XMAX_CANVAS_VIDEO_TRACK_MISSING', 'Canvas input stream is not ready.', this.sessionDiagnostics)
    this.resetCanvasFrameDiagnostics()
    try {
      await video.play()
    } catch (reason) {
      throw inputStreamFailure('XMAX_INPUT_VIDEO_PLAY_FAILED', `The Shot video could not restart playback: ${safeErrorMessage(reason, 'play() failed')}`, this.sessionDiagnostics)
    }
    this.sessionDiagnostics.inputVideoPlayCalled = true
    this.sessionDiagnostics.inputVideoPlayResolved = true
    this.sessionDiagnostics.videoPlayResolved = true
    this.sessionDiagnostics.inputVideoPaused = video.paused
    this.sessionDiagnostics.inputVideoCurrentTimeStart = video.currentTime
    this.emitDiagnostics()
    await this.playAndDrawCanvas(video, 2)
    this.updateCanvasTrackDiagnostics()
    this.assertCanvasInputStream(video, this.canvasInputStream)
    this.emitDiagnostics()
  }

  private async playAndDrawCanvas(video: HTMLVideoElement, minimumFrames: number) {
    this.canvasDrawLoopVideo = video
    this.canvasFailure = null
    this.startCanvasDrawing(video)
    const started = performance.now()
    const initialCount = this.canvasDrawCount
    const initialTime = video.currentTime
    await new Promise<void>((resolve, reject) => {
      let settled = false
      const finish = (reason?: Error) => {
        if (settled) return
        settled = true
        window.clearInterval(poll)
        window.clearTimeout(timeout)
        video.removeEventListener('error', onError)
        if (reason) reject(reason)
        else resolve()
      }
      const onError = () => finish(inputStreamFailure('XMAX_INPUT_STREAM_NOT_FLOWING', 'The Shot video did not produce playable frames.', this.sessionDiagnostics))
      const poll = window.setInterval(() => {
        const frameCount = this.canvasDrawCount - initialCount
        const timeMoved = video.currentTime > initialTime + 0.0005
        if (this.canvasFailure) finish(this.canvasFailure)
        else if (frameCount >= minimumFrames && (timeMoved || this.canvasSampleChanged) && !video.paused && !video.ended) finish()
        else if (this.stopping) finish(inputStreamFailure('XMAX_INPUT_STREAM_NOT_FLOWING', 'Canvas input preparation was cancelled.', this.sessionDiagnostics))
      }, 50)
      const timeout = window.setTimeout(() => finish(inputStreamFailure('XMAX_INPUT_STREAM_NOT_FLOWING', `The Shot video loaded, but no canvas frames were produced within ${Math.round(performance.now() - started)}ms.`, this.sessionDiagnostics)), 3000)
      video.addEventListener('error', onError, { once: true })
    })
  }

  private startCanvasDrawing(video: HTMLVideoElement) {
    const frameVideo = video as HTMLVideoElement & { requestVideoFrameCallback?: (callback: (now: number, metadata: { mediaTime?: number; presentedFrames?: number }) => void) => number }
    this.canvasDrawLoopVideo = video
    const schedule = () => {
      if (this.stopping || video.paused || video.ended) return
      if (typeof frameVideo.requestVideoFrameCallback === 'function') {
        this.canvasFrameCallbackId = frameVideo.requestVideoFrameCallback((_now, metadata) => {
          this.canvasFrameCallbackId = null
          draw(metadata.mediaTime, metadata.presentedFrames)
          schedule()
        })
      } else {
        this.canvasAnimationFrameId = window.requestAnimationFrame(() => {
          this.canvasAnimationFrameId = null
          draw(video.currentTime, undefined)
          schedule()
        })
      }
    }
    const draw = (mediaTime?: number, presentedFrames?: number) => {
      if (this.stopping || !this.canvasContext || !this.inputCanvas || video.paused || video.ended || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || video.videoWidth <= 0 || video.videoHeight <= 0) return
      try {
        this.canvasContext.drawImage(video, 0, 0, this.inputCanvas.width, this.inputCanvas.height)
        if (!this.canvasPreviousSample) {
          const sample = Array.from(this.canvasContext.getImageData(0, 0, 1, 1).data)
          if (sample[3] === 0) throw inputStreamFailure('XMAX_CANVAS_TAINTED', 'Canvas pixel data was transparent or inaccessible.', this.sessionDiagnostics)
          this.canvasPreviousSample = sample
        }
        this.canvasDrawCount += 1
        this.sessionDiagnostics.canvasDrawCount = this.canvasDrawCount
        this.sessionDiagnostics.videoFrameCallbackCount += 1
        if (typeof presentedFrames === 'number' && Number.isFinite(presentedFrames)) this.sessionDiagnostics.presentedFrames = Math.max(this.sessionDiagnostics.presentedFrames, presentedFrames)
        const frameTime = typeof mediaTime === 'number' && Number.isFinite(mediaTime) ? String(mediaTime) : String(video.currentTime)
        if (!this.sessionDiagnostics.firstCanvasFrameTime) this.sessionDiagnostics.firstCanvasFrameTime = frameTime
        this.sessionDiagnostics.lastCanvasFrameTime = frameTime
        this.sessionDiagnostics.inputVideoCurrentTimeEnd = video.currentTime
        this.emitDiagnostics()
      } catch (error) {
        const typed = error as InputStreamFailure
        if (!typed.code) {
          typed.code = error instanceof DOMException && error.name === 'SecurityError' ? 'XMAX_CANVAS_TAINTED' : 'XMAX_CANVAS_DRAW_FAILED'
          typed.stage = 'canvas_input_stream'
        }
        this.sessionDiagnostics.errorCode = typed.code || 'XMAX_CANVAS_DRAW_FAILED'
        this.sessionDiagnostics.errorName = error instanceof Error ? error.name : 'Error'
        this.sessionDiagnostics.errorMessage = safeErrorMessage(error, 'Canvas frame drawing failed.')
        this.emitDiagnostics()
        this.canvasFailure = typed
        void this.finishStop('sdk-error')
        return
      }
    }
    schedule()
  }

  private stopCanvasDrawing() {
    const video = this.canvasDrawLoopVideo as (HTMLVideoElement & { cancelVideoFrameCallback?: (id: number) => void }) | null
    if (video && this.canvasFrameCallbackId !== null && typeof video.cancelVideoFrameCallback === 'function') video.cancelVideoFrameCallback(this.canvasFrameCallbackId)
    if (this.canvasAnimationFrameId !== null) window.cancelAnimationFrame(this.canvasAnimationFrameId)
    this.canvasFrameCallbackId = null
    this.canvasAnimationFrameId = null
    this.canvasDrawLoopVideo = null
  }

  private resetCanvasFrameDiagnostics() {
    this.canvasDrawCount = 0
    this.canvasPreviousSample = null
    this.canvasSampleChanged = false
    this.sessionDiagnostics.canvasDrawCount = 0
    this.sessionDiagnostics.firstCanvasFrameTime = ''
    this.sessionDiagnostics.lastCanvasFrameTime = ''
  }

  private updateCanvasTrackDiagnostics() {
    const stream = this.canvasInputStream
    const track = stream?.getVideoTracks()[0]
    this.canvasTrack = track || null
    this.sessionDiagnostics.canvasStreamActive = Boolean(stream?.active)
    this.sessionDiagnostics.canvasVideoTrackCount = stream?.getVideoTracks().length || 0
    this.sessionDiagnostics.canvasTrackId = track?.id || ''
    this.sessionDiagnostics.canvasTrackKind = track?.kind || ''
    this.sessionDiagnostics.canvasTrackReadyState = track?.readyState || ''
    this.sessionDiagnostics.canvasTrackEnabled = typeof track?.enabled === 'boolean' ? String(track.enabled) : ''
    this.sessionDiagnostics.canvasTrackMuted = typeof track?.muted === 'boolean' ? String(track.muted) : ''
  }

  private updateFinalInputStreamDiagnostics(stream: MediaStream, options: XmaxRealtimeStartOptions) {
    const track = stream.getVideoTracks()[0]
    this.sessionDiagnostics.finalInputStreamActive = stream.active
    this.sessionDiagnostics.finalInputVideoTrackCount = stream.getVideoTracks().length
    this.sessionDiagnostics.finalInputTrackId = track?.id || ''
    this.sessionDiagnostics.finalInputTrackReadyState = track?.readyState || ''
    this.sessionDiagnostics.finalInputTrackEnabled = typeof track?.enabled === 'boolean' ? String(track.enabled) : ''
    this.sessionDiagnostics.finalInputTrackMuted = typeof track?.muted === 'boolean' ? String(track.muted) : ''
    this.sessionDiagnostics.finalInputTrackSettings = safeAuditText(typeof track?.getSettings === 'function' ? track.getSettings() : {})
    this.sessionDiagnostics.localElementTagName = options.localElement.tagName
    this.sessionDiagnostics.remoteElementTagName = options.remoteElement.tagName
    this.sessionDiagnostics.canvasDrawCountBeforeConnect = this.canvasDrawCount
    this.sessionDiagnostics.videoCurrentTimeBeforeConnect = options.inputVideo.currentTime
  }

  private async waitForCanvasStream(timeoutMs: number) {
    await new Promise<void>((resolve, reject) => {
      let settled = false
      const finish = (reason?: Error) => { if (settled) return; settled = true; window.clearInterval(poll); window.clearTimeout(timeout); if (reason) reject(reason); else resolve() }
      const check = () => {
        this.updateCanvasTrackDiagnostics()
        const track = this.canvasInputStream?.getVideoTracks().find((candidate) => candidate.readyState === 'live' && candidate.enabled)
        if (this.canvasInputStream?.active && track) finish()
      }
      const poll = window.setInterval(check, 75)
      const timeout = window.setTimeout(() => finish(inputStreamFailure('XMAX_CANVAS_VIDEO_TRACK_MISSING', 'Canvas captureStream did not produce a live video track.', this.sessionDiagnostics)), timeoutMs)
      check()
    })
  }

  private assertCanvasInputStream(video: HTMLVideoElement, stream: MediaStream | null): asserts stream is MediaStream {
    this.updateCanvasTrackDiagnostics()
    const track = stream?.getVideoTracks().find((candidate) => candidate.readyState === 'live' && candidate.enabled)
    if (!stream || !stream.active || !track || this.canvasDrawCount < 2 || video.paused || video.ended || video.videoWidth <= 0 || video.videoHeight <= 0) {
      throw inputStreamFailure('XMAX_CANVAS_VIDEO_TRACK_MISSING', 'Canvas captureStream did not produce a live video track.', this.sessionDiagnostics)
    }
  }

  private attachOutputStream(stream: MediaStream) {
    if (this.outputStream === stream) return
    this.outputStream = stream
    const videoTrackCount = stream.getVideoTracks().length
    if (videoTrackCount) this.activeConfirmationSource = 'remote_media_stream'
    this.sessionDiagnostics.outputStreamReceived = true
    this.sessionDiagnostics.outputVideoTrackCount = videoTrackCount
    this.sessionDiagnostics.activeConfirmationSource = this.activeConfirmationSource || ''
    this.emitDiagnostics()
    const outputVideo = this.currentOptions?.outputVideo
    if (outputVideo && outputVideo.srcObject !== stream) outputVideo.srcObject = stream
    this.startRecorder(stream)
  }

  private startRecorder(stream: MediaStream) {
    if (this.recorder || !stream.getVideoTracks().length) return
    const options = this.currentOptions
    const mimeType = pickRecorderMimeType()
    if (typeof MediaRecorder === 'undefined' || !mimeType) {
      options?.onError?.('This browser has no supported WebM recorder. The live preview is retained without a download file.')
      options?.onStatus?.('failed')
      return
    }
    this.recorderChunks = []
    this.recorderPromise = new Promise((resolve) => { this.resolveRecorder = resolve })
    this.recorder = new MediaRecorder(stream, { mimeType })
    this.recorder.ondataavailable = (event) => { if (event.data.size) this.recorderChunks.push(event.data) }
    this.recorder.onerror = () => {
      options?.onError?.('Recording the realtime result failed.')
      void this.finishStop('recorder-error')
    }
    this.recorder.onstop = () => {
      const blob = this.recorderChunks.length ? new Blob(this.recorderChunks, { type: mimeType }) : null
      this.resolveRecorder?.(blob)
      this.resolveRecorder = null
      this.recorderPromise = null
    }
    this.recorder.start(1000)
    options?.onStatus?.('recording')
  }

  private async stopRecorder() {
    if (!this.recorder) return null
    const promise = this.recorderPromise
    if (this.recorder.state !== 'inactive') this.recorder.stop()
    this.recorder = null
    return promise ? await promise : null
  }

  private captureOutputWhenReady(video: HTMLVideoElement) {
    const started = performance.now()
    const check = () => {
      if (this.stopping || this.recorder) return
      const stream = video.srcObject instanceof MediaStream ? video.srcObject : null
      if (stream) { this.attachOutputStream(stream); return }
      try {
        const captured = captureOutputStream(video)
        if (captured.getVideoTracks().length) { this.attachOutputStream(captured); return }
        captured.getTracks().forEach((track) => track.stop())
      } catch {
        // The output element may not be ready for capture until the remote stream starts.
      }
      if (performance.now() - started > 15000) {
        this.currentOptions?.onError?.('The live preview connected but exposed no capturable output stream.')
        void this.finishStop('sdk-error')
        return
      }
      this.captureTimer = window.setTimeout(check, 250)
    }
    check()
  }

  private clearCaptureTimer() {
    if (this.captureTimer !== null) window.clearTimeout(this.captureTimer)
    this.captureTimer = null
  }
}

const resultStore = new Map<string, XmaxRealtimeResult>()

export function xmaxResultKey(projectId: string, episodeId?: string | null) {
  return `${projectId}:${episodeId || 'project'}`
}

export function getXmaxResult(key: string) {
  return resultStore.get(key)
}

export function setXmaxResult(key: string, result: XmaxRealtimeResult) {
  const previous = resultStore.get(key)
  if (previous && previous.objectUrl !== result.objectUrl) URL.revokeObjectURL(previous.objectUrl)
  resultStore.set(key, result)
}

export function releaseXmaxResult(result?: XmaxRealtimeResult | null) {
  if (result?.objectUrl) URL.revokeObjectURL(result.objectUrl)
}
