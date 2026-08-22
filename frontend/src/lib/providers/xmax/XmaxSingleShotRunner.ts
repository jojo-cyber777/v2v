import {
  createXmaxClient,
  models,
  type RealtimeSession,
  type XmaxClient,
} from '@xmaxai/sdk'
import { normalizeBackendAssetUrl } from '../../../api'
import {
  buildXmaxFailureDiagnostics,
  type XmaxFailureDiagnostics,
  type XmaxFailurePhase,
} from './XmaxFailureDiagnostics'
import { runXmaxStartupOperation, XmaxStartupTimeoutError } from './XmaxStartupLifecycle'

export type SingleShotRunnerStatus = 'Preparing' | 'Connecting' | 'Uploading reference' | 'Starting transformation' | 'Running' | 'Recording' | 'Stopping' | 'Completed' | 'Failed' | 'Persistence failed' | 'Cancelled'
export type SingleShotStopReason = 'manual' | 'hard_cap' | 'video_ended' | 'sdk_disconnect' | 'client_error' | 'component_unmount' | 'cancelled' | 'startup_timeout' | 'error'
export type XmaxSingleShotRunnerDiagnostics = {
  connectMs: number | null
  referenceUploadMs: number | null
  restartMs: number | null
  firstOutputFrameMs: number | null
  processedSeconds: number
  stopReason: string
  sessionId?: string
  events: string[]
  remoteFrameCount: number
  firstRemoteFrameAt: number | null
  lastRemoteFrameAt: number | null
  remoteFrameIntervals: number[]
  remoteMaxFrameGapMs: number
  remoteGapsOver100Ms: number
  remoteGapsOver250Ms: number
  remoteGapsOver500Ms: number
  remoteGapsOver1000Ms: number
  presentedFrames: number | null
  mediaTime: number | null
  remoteFrameFallback: boolean
  trackSettingsFrameRate: number | null
  trackSettingsWidth: number | null
  trackSettingsHeight: number | null
  trackMutedEvents: number
  trackUnmutedEvents: number
  trackEndedAt: number | null
  recorderStartedAt: number | null
  recorderStoppedAt: number | null
  recorderCreated: boolean
  recorderStarted: boolean
  recorderState: string
  recorderStopObserved: boolean
  recorderCompletionTimedOut: boolean
  dataavailableCount: number
  recorderChunkCount: number
  recorderChunkSizes: Array<{ relativeMs: number; size: number }>
  recorderTotalBytes: number
  recorderMimeType: string
  recorderStartMode: 'single-final-chunk'
  recorderError: string | null
  finalBlobSize: number
  finalBlobDuration: number | null
  localBlobRetained: boolean
  persistenceAttemptCount: number
  remuxAttempted: boolean
  remuxSucceeded: boolean
  originalProbeError: string | null
  remuxProbeError: string | null
  activeSessionCount: number
  activeRecorderCount: number
  activeRemoteTrackCount: number
  mountedInputVideoCount: number
  mountedRemoteVideoCount: number
  retainedBlobBytes: number
  activeObjectUrlCount: number
  outputStreamReceived: boolean
  outputFileReceived: boolean
  outputUploadAttempted: boolean
  disconnectReason: string
  restartCount: number
  cleanupStartedAt: number | null
  cleanupFinishedAt: number | null
  cleanupErrors: string[]
  inputLoop: boolean
  inputPlaybackCount: number
  inputPlaybackStartedAt: number | null
  inputEndedAt: number | null
  inputPausedBeforeConnect: boolean
  inputCurrentTimeBeforeConnect: number | null
  connectResolvedAt: number | null
  referenceUploadResolvedAt: number | null
  restartResolvedAt: number | null
  inputPlaybackAllowedAt: number | null
  startupStage: string
  startupFailureStage: string | null
  startupFrameRequestCount: number
  proxyPreRoll: number
  proxyContentDuration: number
  proxyPostRoll: number
  proxyTotalDuration: number
  remoteFirstMediaTime: number | null
  remoteLastMediaTime: number | null
  timelineStatus: string
  referenceFaceCount: number | null
  failure: XmaxFailureDiagnostics | null
}
export type XmaxSingleShotRunnerResult = {
  outputBlob: Blob | null
  mimeType: string
  fileName: string
  diagnostics: XmaxSingleShotRunnerDiagnostics
}

type RunnerOptions = {
  temporaryApiKey?: string
  sourceUrl: string
  referenceUrl: string
  sourceFilename: string
  sourceDuration: number
  contentDuration?: number
  preRollDuration?: number
  postRollDuration?: number
  prompt: string
  localElement: HTMLDivElement
  remoteElement: HTMLDivElement
  onStatus?: (status: SingleShotRunnerStatus) => void
  onDiagnostics?: (diagnostics: XmaxSingleShotRunnerDiagnostics) => void
  onRemoteStream?: (stream: MediaStream) => void
  onSessionCreated?: (sessionUid: string) => void | Promise<void>
  onError?: (error: Error) => void
  startupTimeouts?: {
    referenceUploadMs?: number
    connectMs?: number
  }
}

const MIME_TYPES = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm']
const RECORDER_FINALIZATION_TIMEOUT_MS = 3000
export const DEFAULT_REFERENCE_UPLOAD_TIMEOUT_MS = 30_000
export const DEFAULT_CONNECT_TIMEOUT_MS = 60_000

function asError(value: unknown, fallback: string) {
  return value instanceof Error ? value : new Error(value ? String(value) : fallback)
}

async function fetchAsFile(url: string, filename: string, fallbackType: string) {
  const normalizedUrl = normalizeBackendAssetUrl(url)
  if (!normalizedUrl) throw new Error(`Asset URL is missing for ${filename}.`)
  const response = await fetch(normalizedUrl)
  if (!response.ok) throw new Error(`Asset fetch failed for ${filename}: HTTP ${response.status}.`)
  const blob = await response.blob()
  if (blob.size <= 0) throw new Error(`Asset fetch returned an empty file for ${filename}.`)
  const contentType = blob.type || fallbackType
  if (fallbackType.startsWith('video/') && !contentType.startsWith('video/')) throw new Error(`Source asset is not a video: ${contentType || 'unknown MIME'}.`)
  return new File([blob], filename, { type: contentType })
}

export default class XmaxSingleShotRunner {
  private readonly options: RunnerOptions
  private session: RealtimeSession | null = null
  private recorder: MediaRecorder | null = null
  private remoteStream: MediaStream | null = null
  private remoteObserver: MutationObserver | null = null
  private outputVideo: HTMLVideoElement | null = null
  private outputEndedHandler: (() => void) | null = null
  private remoteVideoFrameCallbackId: number | null = null
  private remoteFrameFallbackTimer: number | null = null
  private remoteVideoForFrames: HTMLVideoElement | null = null
  private remoteFrameCount = 0
  private firstRemoteFrameAt: number | null = null
  private lastRemoteFrameAt: number | null = null
  private remoteFrameIntervals: number[] = []
  private outputStreamReceived = false
  private outputFileReceived = false
  private remoteMaxFrameGapMs = 0
  private remoteGapsOver100Ms = 0
  private remoteGapsOver250Ms = 0
  private remoteGapsOver500Ms = 0
  private remoteGapsOver1000Ms = 0
  private presentedFrames: number | null = null
  private mediaTime: number | null = null
  private remoteFrameFallback = false
  private remoteTrack: MediaStreamTrack | null = null
  private remoteTrackMuteHandler: (() => void) | null = null
  private remoteTrackUnmuteHandler: (() => void) | null = null
  private remoteTrackEndedHandler: (() => void) | null = null
  private trackSettingsFrameRate: number | null = null
  private trackSettingsWidth: number | null = null
  private trackSettingsHeight: number | null = null
  private trackMutedEvents = 0
  private trackUnmutedEvents = 0
  private trackEndedAt: number | null = null
  private hardCapTimer: number | null = null
  private recorderStopPromise: Promise<void> | null = null
  private resolveRecorderStop: (() => void) | null = null
  private completionPromise: Promise<XmaxSingleShotRunnerResult> | null = null
  private resolveCompletion: ((result: XmaxSingleShotRunnerResult) => void) | null = null
  private stopPromise: Promise<XmaxSingleShotRunnerResult> | null = null
  private restartResolved = false
  private stopRequested = false
  private stopReason = ''
  private startedAt = 0
  private restartStartedAt = 0
  private restartResolvedAt = 0
  private firstOutputFrameAt: number | null = null
  private connectResolvedAt: number | null = null
  private connectStartedAt: number | null = null
  private referenceUploadStartedAt: number | null = null
  private referenceUploadResolvedAt: number | null = null
  private restartStartedAtValue: number | null = null
  private sessionId = ''
  private chunks: Blob[] = []
  private recorderStartedAt: number | null = null
  private recorderStoppedAt: number | null = null
  private recorderCreated = false
  private recorderStarted = false
  private recorderState = 'not_created'
  private recorderStopObserved = false
  private recorderCompletionTimedOut = false
  private dataavailableCount = 0
  private recorderChunkCount = 0
  private recorderChunkSizes: Array<{ relativeMs: number; size: number }> = []
  private recorderTotalBytes = 0
  private recorderMimeType = ''
  private recorderError: string | null = null
  private finalBlobSize = 0
  private finalBlobDuration: number | null = null
  private disconnectReason = ''
  private restartCount = 0
  private events: string[] = []
  private finishedResult: XmaxSingleShotRunnerResult | null = null
  private connectInFlight = false
  private connectFinishedPromise: Promise<void> | null = null
  private resolveConnectFinished: (() => void) | null = null
  private cleanupStartedAt: number | null = null
  private cleanupFinishedAt: number | null = null
  private cleanupErrors: string[] = []
  private inputVideo: HTMLVideoElement | null = null
  private inputEndedHandler: (() => void) | null = null
  private inputPlayHandler: ((event: Event) => void) | null = null
  private inputPlaybackCount = 0
  private inputPlaybackStartedAt: number | null = null
  private inputEndedAt: number | null = null
  private inputPlaybackAllowed = false
  private inputPausedBeforeConnect = false
  private inputCurrentTimeBeforeConnect: number | null = null
  private inputPlaybackAllowedAt: number | null = null
  private startupStage = 'preparing'
  private startupFailureStage: string | null = null
  private failure: XmaxFailureDiagnostics | null = null
  private startupFrameRequestCount = 0
  private startupTimedOut = false
  private remoteTailTimer: number | null = null
  private remoteTailDeadline = 0
  private remoteFirstMediaTime: number | null = null
  private remoteLastMediaTime: number | null = null
  private cleanedUp = false

  constructor(options: RunnerOptions) {
    this.options = options
  }

  private currentFailurePhase(): XmaxFailurePhase {
    if (this.startupStage === 'reference_upload') return 'reference_upload'
    if (this.startupStage === 'connect_media' || this.startupStage === 'session_binding') return 'session_connect'
    if (this.startupStage === 'remote_stream' || this.startupStage === 'running') return 'remote_stream'
    return 'temporary_key'
  }

  private captureFailure(message: string, error: unknown, phase = this.currentFailurePhase()) {
    if (!this.failure) {
      this.failure = buildXmaxFailureDiagnostics(message, error, phase)
      this.event('error_captured')
    }
    return this.failure
  }

  private shouldPersistFinishedBlobAfterRuntimeFailure(result: XmaxSingleShotRunnerResult | null) {
    const runtimeFailure = this.failure?.phase === 'remote_stream'
      && (this.disconnectReason === 'heartbeat_error' || this.stopReason === 'client_error')
    return Boolean(
      runtimeFailure
      && this.recorderStopObserved
      && result?.outputBlob
      && result.outputBlob.size > 0,
    )
  }

  private event(name: string) {
    this.events.push(name)
    const lifecycleAliases: Record<string, string> = {
      connect_started: 'connectStarted',
      connect_resolved: 'connectResolved',
      reference_upload_started: 'referenceUploadStarted',
      reference_upload_resolved: 'referenceUploadResolved',
      session_restart_started: 'restartStarted',
      session_restart_resolved: 'restartResolved',
      remote_stream_received: 'remoteStreamReceived',
      first_output_frame: 'firstRemoteFrame',
      recorder_started: 'recorderStarted',
      recorder_stop_requested: 'recorderStopRequested',
      recorder_stopped: 'recorderStopped',
      hard_cap_armed: 'hardCapArmed',
      hard_cap_fired: 'hardCapFired',
      disconnect_started: 'disconnectStarted',
      disconnect_resolved: 'disconnectResolved',
      cleanup_completed: 'cleanupCompleted',
    }
    const alias = lifecycleAliases[name]
    if (alias) this.events.push(alias)
    this.options.onDiagnostics?.(this.diagnostics())
  }

  private status(status: SingleShotRunnerStatus) {
    this.options.onStatus?.(status)
  }

  private diagnostics(): XmaxSingleShotRunnerDiagnostics {
    const processedSeconds = this.restartResolvedAt > 0
      ? Math.min(this.options.sourceDuration, Math.max(0, (performance.now() - this.restartResolvedAt) / 1000))
      : 0
    return {
      connectMs: this.connectStartedAt != null && this.connectResolvedAt != null ? Math.round(this.connectResolvedAt - this.connectStartedAt) : null,
      referenceUploadMs: this.referenceUploadStartedAt != null && this.referenceUploadResolvedAt != null ? Math.round(this.referenceUploadResolvedAt - this.referenceUploadStartedAt) : null,
      restartMs: this.restartStartedAtValue != null && this.restartResolvedAt > 0 ? Math.round(this.restartResolvedAt - this.restartStartedAtValue) : null,
      firstOutputFrameMs: this.firstOutputFrameAt != null && this.restartResolvedAt > 0 ? Math.round(this.firstOutputFrameAt - this.restartResolvedAt) : null,
      processedSeconds,
      stopReason: this.stopReason,
      sessionId: this.sessionId || undefined,
      events: [...this.events],
      remoteFrameCount: this.remoteFrameCount,
      firstRemoteFrameAt: this.firstRemoteFrameAt,
      lastRemoteFrameAt: this.lastRemoteFrameAt,
      remoteFrameIntervals: [...this.remoteFrameIntervals],
      remoteMaxFrameGapMs: this.remoteMaxFrameGapMs,
      remoteGapsOver100Ms: this.remoteGapsOver100Ms,
      remoteGapsOver250Ms: this.remoteGapsOver250Ms,
      remoteGapsOver500Ms: this.remoteGapsOver500Ms,
      remoteGapsOver1000Ms: this.remoteGapsOver1000Ms,
      presentedFrames: this.presentedFrames,
      mediaTime: this.mediaTime,
      remoteFrameFallback: this.remoteFrameFallback,
      trackSettingsFrameRate: this.trackSettingsFrameRate,
      trackSettingsWidth: this.trackSettingsWidth,
      trackSettingsHeight: this.trackSettingsHeight,
      trackMutedEvents: this.trackMutedEvents,
      trackUnmutedEvents: this.trackUnmutedEvents,
      trackEndedAt: this.trackEndedAt,
      recorderStartedAt: this.recorderStartedAt,
      recorderStoppedAt: this.recorderStoppedAt,
      recorderCreated: this.recorderCreated,
      recorderStarted: this.recorderStarted,
      recorderState: this.recorderState,
      recorderStopObserved: this.recorderStopObserved,
      recorderCompletionTimedOut: this.recorderCompletionTimedOut,
      dataavailableCount: this.dataavailableCount,
      recorderChunkCount: this.recorderChunkCount,
      recorderChunkSizes: [...this.recorderChunkSizes],
      recorderTotalBytes: this.recorderTotalBytes,
      recorderMimeType: this.recorderMimeType,
      recorderStartMode: 'single-final-chunk',
      recorderError: this.recorderError,
      finalBlobSize: this.finalBlobSize,
      finalBlobDuration: this.finalBlobDuration,
      localBlobRetained: false,
      persistenceAttemptCount: 0,
      remuxAttempted: false,
      remuxSucceeded: false,
      originalProbeError: null,
      remuxProbeError: null,
      activeSessionCount: this.session ? 1 : 0,
      activeRecorderCount: this.recorder && this.recorder.state !== 'inactive' ? 1 : 0,
      activeRemoteTrackCount: this.remoteStream?.getVideoTracks().filter((track) => track.readyState === 'live').length || 0,
      mountedInputVideoCount: this.options.localElement.querySelectorAll('video').length,
      mountedRemoteVideoCount: this.options.remoteElement.querySelectorAll('video').length,
      retainedBlobBytes: 0,
      activeObjectUrlCount: 0,
      outputStreamReceived: this.outputStreamReceived,
      outputFileReceived: this.outputFileReceived,
      outputUploadAttempted: false,
      disconnectReason: this.disconnectReason,
      restartCount: this.restartCount,
      cleanupStartedAt: this.cleanupStartedAt,
      cleanupFinishedAt: this.cleanupFinishedAt,
      cleanupErrors: [...this.cleanupErrors],
      inputLoop: this.inputVideo?.loop ?? false,
      inputPlaybackCount: this.inputPlaybackCount,
      inputPlaybackStartedAt: this.inputPlaybackStartedAt,
      inputEndedAt: this.inputEndedAt,
      inputPausedBeforeConnect: this.inputPausedBeforeConnect,
      inputCurrentTimeBeforeConnect: this.inputCurrentTimeBeforeConnect,
      connectResolvedAt: this.connectResolvedAt == null ? null : this.connectResolvedAt - this.startedAt,
      referenceUploadResolvedAt: this.referenceUploadResolvedAt == null ? null : this.referenceUploadResolvedAt - this.startedAt,
      restartResolvedAt: this.restartResolvedAt > 0 ? this.restartResolvedAt - this.startedAt : null,
      inputPlaybackAllowedAt: this.inputPlaybackAllowedAt,
      startupStage: this.startupStage,
      startupFailureStage: this.startupFailureStage,
      startupFrameRequestCount: this.startupFrameRequestCount,
      proxyPreRoll: this.options.preRollDuration || 0,
      proxyContentDuration: this.options.contentDuration || this.options.sourceDuration,
      proxyPostRoll: this.options.postRollDuration || 0,
      proxyTotalDuration: this.options.sourceDuration,
      remoteFirstMediaTime: this.remoteFirstMediaTime,
      remoteLastMediaTime: this.remoteLastMediaTime,
      timelineStatus: this.stopReason === 'video_ended' ? 'valid' : 'pending',
      referenceFaceCount: null,
      failure: this.failure ? { ...this.failure } : null,
    }
  }

  private async fetchInputs() {
    this.status('Preparing')
    const sourceFile = await fetchAsFile(this.options.sourceUrl, this.options.sourceFilename || 'episode-source.mp4', 'video/mp4')
    const referenceFile = await fetchAsFile(this.options.referenceUrl, 'reference-image.png', 'image/png')
    return { sourceFile, referenceFile }
  }

  private markFirstOutputFrame() {
    if (this.firstOutputFrameAt != null) return
    this.firstOutputFrameAt = performance.now()
    this.event('first_output_frame')
  }

  private bindOutputVideo() {
    const video = this.options.remoteElement.querySelector('video')
    if (!video || video === this.outputVideo) return
    if (this.outputVideo && this.outputEndedHandler) this.outputVideo.removeEventListener('ended', this.outputEndedHandler)
    this.outputVideo = video
    this.outputEndedHandler = () => { this.event('video_ended'); void this.stop('video_ended') }
    video.addEventListener('ended', this.outputEndedHandler)
    video.addEventListener('loadeddata', () => this.markFirstOutputFrame(), { once: true })
    video.addEventListener('canplay', () => this.markFirstOutputFrame(), { once: true })
    this.startRemoteFrameDiagnostics(video)
  }

  private recordRemoteFrame(presentedFrames: number | null, mediaTime: number | null) {
    const now = performance.now()
    if (this.firstRemoteFrameAt == null) {
      this.firstRemoteFrameAt = now - this.startedAt
      this.markFirstOutputFrame()
      this.armHardCap('first_remote_frame')
    }
    if (this.lastRemoteFrameAt != null) {
      const gap = Math.max(0, now - (this.startedAt + this.lastRemoteFrameAt))
      const roundedGap = Math.round(gap)
      if (this.remoteFrameIntervals.length < 240) this.remoteFrameIntervals.push(roundedGap)
      this.remoteMaxFrameGapMs = Math.max(this.remoteMaxFrameGapMs, roundedGap)
      if (roundedGap > 100) this.remoteGapsOver100Ms += 1
      if (roundedGap > 250) this.remoteGapsOver250Ms += 1
      if (roundedGap > 500) this.remoteGapsOver500Ms += 1
      if (roundedGap > 1000) this.remoteGapsOver1000Ms += 1
    }
    this.lastRemoteFrameAt = now - this.startedAt
    this.remoteFrameCount += 1
    this.presentedFrames = presentedFrames
    this.mediaTime = mediaTime
    if (mediaTime != null) {
      if (this.remoteFirstMediaTime == null) this.remoteFirstMediaTime = mediaTime
      this.remoteLastMediaTime = mediaTime
      if (this.inputEndedAt != null && mediaTime >= this.options.sourceDuration - 0.15) this.waitForRemoteTail()
    }
  }

  private startRemoteFrameDiagnostics(video: HTMLVideoElement) {
    this.stopRemoteFrameDiagnostics()
    this.remoteVideoForFrames = video
    const fallbackVideo: HTMLVideoElement = video
    if ('requestVideoFrameCallback' in video) {
      const onFrame = (_now: number, metadata: VideoFrameCallbackMetadata) => {
        this.recordRemoteFrame(
          typeof metadata.presentedFrames === 'number' ? metadata.presentedFrames : null,
          typeof metadata.mediaTime === 'number' ? metadata.mediaTime : null,
        )
        if (this.remoteVideoForFrames === video && !this.stopRequested) {
          this.remoteVideoFrameCallbackId = video.requestVideoFrameCallback(onFrame)
        }
      }
      this.remoteVideoFrameCallbackId = video.requestVideoFrameCallback(onFrame)
      return
    }
    this.remoteFrameFallback = true
    let lastCurrentTime: number | null = null
    this.remoteFrameFallbackTimer = window.setInterval(() => {
      if (this.stopRequested || fallbackVideo.paused || fallbackVideo.ended || fallbackVideo.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return
      const currentTime = fallbackVideo.currentTime
      if (lastCurrentTime == null || currentTime !== lastCurrentTime) {
        lastCurrentTime = currentTime
        this.recordRemoteFrame(null, currentTime)
      }
    }, 250)
  }

  private waitForRemoteTail() {
    if (this.stopRequested) return
    const target = this.options.sourceDuration
    if (this.remoteLastMediaTime != null && this.remoteLastMediaTime >= target - 0.15) {
      void this.stop('video_ended')
      return
    }
    if (!this.remoteTailDeadline) this.remoteTailDeadline = performance.now() + 3000
    if (performance.now() >= this.remoteTailDeadline) {
      this.event('remote_tail_timeout')
      void this.stop('video_ended')
      return
    }
    if (this.remoteTailTimer === null) {
      this.remoteTailTimer = window.setTimeout(() => {
        this.remoteTailTimer = null
        this.waitForRemoteTail()
      }, 100)
    }
  }

  private stopRemoteFrameDiagnostics() {
    if (this.remoteVideoFrameCallbackId != null && this.remoteVideoForFrames && 'cancelVideoFrameCallback' in this.remoteVideoForFrames) {
      this.remoteVideoForFrames.cancelVideoFrameCallback(this.remoteVideoFrameCallbackId)
    }
    this.remoteVideoFrameCallbackId = null
    if (this.remoteFrameFallbackTimer != null) window.clearInterval(this.remoteFrameFallbackTimer)
    this.remoteFrameFallbackTimer = null
    this.remoteVideoForFrames = null
  }

  private clearMediaElements(container: HTMLDivElement) {
    container.querySelectorAll('video').forEach((video) => {
      try {
        video.pause()
        video.srcObject = null
        video.removeAttribute('src')
        video.load()
      } catch (error) {
        this.cleanupErrors.push(error instanceof Error ? error.message : String(error))
      }
    })
    container.replaceChildren()
  }

  private bindRemoteTrack(stream: MediaStream) {
    const track = stream.getVideoTracks()[0]
    if (!track) return
    this.remoteTrack = track
    const settings = track.getSettings()
    this.trackSettingsFrameRate = typeof settings.frameRate === 'number' ? settings.frameRate : null
    this.trackSettingsWidth = typeof settings.width === 'number' ? settings.width : null
    this.trackSettingsHeight = typeof settings.height === 'number' ? settings.height : null
    this.remoteTrackMuteHandler = () => { this.trackMutedEvents += 1; this.event('remote_track_mute') }
    this.remoteTrackUnmuteHandler = () => { this.trackUnmutedEvents += 1; this.event('remote_track_unmute') }
    this.remoteTrackEndedHandler = () => { this.trackEndedAt = performance.now() - this.startedAt; this.event('remote_track_ended') }
    track.addEventListener('mute', this.remoteTrackMuteHandler)
    track.addEventListener('unmute', this.remoteTrackUnmuteHandler)
    track.addEventListener('ended', this.remoteTrackEndedHandler)
  }

  private observeOutput() {
    this.bindOutputVideo()
    this.remoteObserver = new MutationObserver(() => this.bindOutputVideo())
    this.remoteObserver.observe(this.options.remoteElement, { childList: true, subtree: true })
  }

  private startRecording(stream: MediaStream) {
    if (this.recorder || !this.restartResolved || this.startupTimedOut || this.stopRequested) return
    const trackCount = stream.getVideoTracks().length
    if (!trackCount) throw new Error('Output stream did not contain a video track.')
    if (typeof MediaRecorder === 'undefined') throw new Error('当前浏览器不支持录制。')
    const mimeType = MIME_TYPES.find((candidate) => MediaRecorder.isTypeSupported(candidate)) || ''
    const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
    this.recorder = recorder
    this.recorderCreated = true
    this.recorderState = recorder.state
    this.recorderStopObserved = false
    this.recorderCompletionTimedOut = false
    this.recorderStopPromise = new Promise<void>((resolve) => { this.resolveRecorderStop = resolve })
    this.chunks = []
    this.recorderStartedAt = performance.now() - this.startedAt
    this.recorderStoppedAt = null
    this.recorderStarted = false
    this.dataavailableCount = 0
    this.recorderChunkCount = 0
    this.recorderChunkSizes = []
    this.recorderTotalBytes = 0
    this.recorderMimeType = recorder.mimeType
    this.recorderError = null
    recorder.ondataavailable = (event) => {
      this.dataavailableCount += 1
      this.recorderChunkSizes.push({ relativeMs: Math.round((performance.now() - this.startedAt) - (this.recorderStartedAt || 0)), size: event.data.size })
      if (event.data.size > 0) {
        this.chunks.push(event.data)
        this.recorderChunkCount += 1
        this.recorderTotalBytes += event.data.size
      }
      this.event('recorder_dataavailable')
    }
    recorder.onerror = () => {
      this.recorderError = 'MediaRecorder error.'
      this.options.onError?.(new Error('Output recording failed.'))
      void this.stop('error')
    }
    recorder.onstop = () => {
      this.recorderStoppedAt = performance.now() - this.startedAt
      this.recorderState = recorder.state
      this.recorderStopObserved = true
      this.event('recorder_stopped')
      window.setTimeout(() => {
        this.resolveRecorderStop?.()
        this.resolveRecorderStop = null
      }, 0)
    }
    recorder.start()
    this.recorderStarted = true
    this.recorderState = recorder.state
    this.event('recorder_started')
    this.status('Recording')
  }

  private armHardCap(anchor: 'restart_resolved' | 'first_remote_frame') {
    if (this.hardCapTimer !== null) window.clearTimeout(this.hardCapTimer)
    const durationMs = Math.ceil(this.options.sourceDuration * 1000) + 3000
    this.hardCapTimer = window.setTimeout(() => {
      this.event('hard_cap_fired')
      void this.stop('hard_cap')
    }, durationMs)
    this.event(anchor === 'first_remote_frame' ? 'hard_cap_rearmed_from_first_output_frame' : 'hard_cap_armed_waiting_for_output')
    this.event('hard_cap_armed')
  }

  private async waitForRecorderCompletion(completion: Promise<void>) {
    let timeoutId: number | null = null
    const timeout = new Promise<void>((resolve) => {
      timeoutId = window.setTimeout(() => {
        this.recorderCompletionTimedOut = true
        this.recorderError = this.recorderError || `MediaRecorder finalization timed out after ${RECORDER_FINALIZATION_TIMEOUT_MS}ms.`
        this.event('recorder_finalization_timeout')
        resolve()
      }, RECORDER_FINALIZATION_TIMEOUT_MS)
    })
    await Promise.race([completion, timeout])
    if (timeoutId !== null) window.clearTimeout(timeoutId)
  }

  private async stopRecording() {
    const recorder = this.recorder
    const completion = this.recorderStopPromise
    if (!recorder || !completion) return
    this.event('recorder_stop_requested')
    this.recorderState = recorder.state
    if (recorder.state !== 'inactive') {
      try {
        recorder.requestData()
      } catch (error) {
        this.recorderError = error instanceof Error ? error.message : String(error)
        this.event('recorder_request_data_failed')
      }
      try {
        recorder.stop()
        this.recorderState = recorder.state
      } catch (error) {
        this.recorderError = error instanceof Error ? error.message : String(error)
        this.event('recorder_stop_failed')
      }
    }
    await this.waitForRecorderCompletion(completion)
  }

  private clearLocalResources() {
    if (this.cleanedUp) return
    this.cleanedUp = true
    this.cleanupStartedAt = performance.now() - this.startedAt
    if (this.hardCapTimer !== null) window.clearTimeout(this.hardCapTimer)
    this.hardCapTimer = null
    if (this.remoteTailTimer !== null) window.clearTimeout(this.remoteTailTimer)
    this.remoteTailTimer = null
    this.remoteTailDeadline = 0
    this.remoteObserver?.disconnect()
    this.remoteObserver = null
    this.stopRemoteFrameDiagnostics()
    if (this.remoteTrack && this.remoteTrackMuteHandler) this.remoteTrack.removeEventListener('mute', this.remoteTrackMuteHandler)
    if (this.remoteTrack && this.remoteTrackUnmuteHandler) this.remoteTrack.removeEventListener('unmute', this.remoteTrackUnmuteHandler)
    if (this.remoteTrack && this.remoteTrackEndedHandler) this.remoteTrack.removeEventListener('ended', this.remoteTrackEndedHandler)
    this.remoteTrack = null
    this.remoteTrackMuteHandler = null
    this.remoteTrackUnmuteHandler = null
    this.remoteTrackEndedHandler = null
    if (this.outputVideo && this.outputEndedHandler) this.outputVideo.removeEventListener('ended', this.outputEndedHandler)
    this.outputVideo = null
    this.outputEndedHandler = null
    if (this.inputVideo && this.inputEndedHandler) this.inputVideo.removeEventListener('ended', this.inputEndedHandler)
    if (this.inputVideo && this.inputPlayHandler) this.inputVideo.removeEventListener('play', this.inputPlayHandler, true)
    this.inputVideo = null
    this.inputEndedHandler = null
    this.inputPlayHandler = null
    try {
      this.remoteStream?.getTracks().forEach((track) => track.stop())
    } catch (error) {
      this.cleanupErrors.push(error instanceof Error ? error.message : String(error))
    }
    this.remoteStream = null
    this.clearMediaElements(this.options.localElement)
    this.clearMediaElements(this.options.remoteElement)
    this.recorder = null
    this.cleanupFinishedAt = performance.now() - this.startedAt
    this.event('cleanup_completed')
  }

  async stop(reason: SingleShotStopReason): Promise<XmaxSingleShotRunnerResult | null> {
    if (this.stopPromise) return this.stopPromise
    this.stopRequested = true
    this.stopReason = reason
    this.stopPromise = (async () => {
      this.status('Stopping')
      if (this.connectInFlight && this.connectFinishedPromise) await this.connectFinishedPromise
      await this.stopRecording()
      const blob = this.chunks.length > 0 ? new Blob(this.chunks, { type: this.recorder?.mimeType || 'video/webm' }) : null
      this.finalBlobSize = blob?.size || 0
      this.outputFileReceived = Boolean(blob && blob.size > 0)
      this.chunks = []
      const session = this.session
      this.session = null
      if (session) {
        this.event('disconnect_started')
        try {
          await session.disconnect()
          this.event('disconnect_resolved')
        } catch (error) {
          this.options.onError?.(asError(error, 'Xmax Session disconnect failed.'))
        }
      }
      const result: XmaxSingleShotRunnerResult = {
        outputBlob: blob && blob.size > 0 ? blob : null,
        mimeType: blob?.type || 'video/webm',
        fileName: `xmax-single-shot-${new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 17)}.webm`,
        diagnostics: this.diagnostics(),
      }
      this.finishedResult = result
      this.clearLocalResources()
      result.diagnostics = this.diagnostics()
      if (reason === 'cancelled') this.status('Cancelled')
      else if (result.outputBlob) this.status('Completed')
      else this.status('Failed')
      this.resolveCompletion?.(result)
      return result
    })()
    return this.stopPromise
  }

  async run(): Promise<XmaxSingleShotRunnerResult> {
    if (this.completionPromise) throw new Error('This Xmax single-shot runner can only run once.')
    this.startedAt = performance.now()
    this.completionPromise = new Promise<XmaxSingleShotRunnerResult>((resolve) => { this.resolveCompletion = resolve })
    try {
      const { sourceFile: unitFile, referenceFile } = await this.fetchInputs()
      if (this.stopRequested) return (await this.stop('cancelled')) as XmaxSingleShotRunnerResult
      this.startupStage = 'temporary_key'
      const apiKey = this.options.temporaryApiKey || ''
      if (!apiKey.startsWith('tk-')) throw new Error('XMAX_TEMP_KEY_REQUIRED: a backend-issued temporary Xmax key is required.')
      const client: XmaxClient = createXmaxClient({
        apiKey,
        onError: (message, error) => {
          const captured = this.captureFailure(message, error)
          this.options.onError?.(new Error(captured.message))
          void this.stop('client_error')
        },
      })
      this.startupStage = 'reference_upload'
      this.status('Uploading reference')
      this.referenceUploadStartedAt = performance.now()
      const uploadedReference = await runXmaxStartupOperation({
        stage: 'reference_upload',
        timeoutMs: this.options.startupTimeouts?.referenceUploadMs ?? DEFAULT_REFERENCE_UPLOAD_TIMEOUT_MS,
        operation: () => client.files.uploadAndCheckImage(referenceFile),
        onEvent: (name) => this.event(name),
      })
      this.referenceUploadResolvedAt = performance.now()
      this.event('reference_upload_resolved')
      if (this.stopRequested) return (await this.stop('cancelled')) as XmaxSingleShotRunnerResult

      this.startupStage = 'connect_media'
      this.connectStartedAt = performance.now()
      this.restartStartedAtValue = this.connectStartedAt
      this.inputPlaybackAllowed = true
      this.inputPlaybackAllowedAt = this.connectStartedAt - this.startedAt
      this.inputPausedBeforeConnect = true
      this.inputCurrentTimeBeforeConnect = 0
      this.restartCount += 1
      this.event('official_connect_media_started')
      this.event('session_restart_started')
      this.status('Connecting')
      this.connectInFlight = true
      this.connectFinishedPromise = new Promise<void>((resolve) => { this.resolveConnectFinished = resolve })
      try {
        const session = await runXmaxStartupOperation<RealtimeSession>({
          stage: 'connect',
          timeoutMs: this.options.startupTimeouts?.connectMs ?? DEFAULT_CONNECT_TIMEOUT_MS,
          onEvent: (name) => this.event(name),
          onTimeout: () => { this.startupTimedOut = true },
          operation: () => client.realtime.connectMedia(unitFile, {
          model: models.realtime('x2.0'),
          playback: { playbackRate: 1 },
          context: {
            prompt: this.options.prompt.trim(),
            refImageUrl: uploadedReference,
          },
          render: {
            localContainer: this.options.localElement,
            remoteContainer: this.options.remoteElement,
          },
          onRemoteStream: (stream) => {
            this.startupStage = 'remote_stream'
            if (this.startupTimedOut || this.stopRequested) {
              stream.getTracks().forEach((track) => track.stop())
              this.event('remote_track_ignored_after_timeout')
              return
            }
            this.remoteStream = stream
            this.outputStreamReceived = true
            this.bindRemoteTrack(stream)
            this.event('remote_stream_received')
            this.event('remote_track_received')
            this.options.onRemoteStream?.(stream)
            if (this.restartResolved) {
              try {
                this.startRecording(stream)
              } catch (error) {
                const captured = this.captureFailure(
                  asError(error, 'Unable to record Xmax output.').message,
                  error,
                  'remote_stream',
                )
                this.options.onError?.(new Error(captured.message))
                void this.stop('error')
              }
            }
          },
          onDisconnect: (reason) => {
            this.disconnectReason = reason
            this.event('sdk_disconnect')
            if (!this.startupTimedOut) void this.stop('sdk_disconnect')
          },
          }),
          onLateResolve: async (lateSession) => {
            this.event('late_session_disconnect_started')
            await lateSession.disconnect()
            this.event('late_session_disconnect_completed')
          },
        })
        this.session = session
        this.sessionId = session.getSessionUid() || ''
        if (!this.sessionId) throw new Error('Xmax connect completed without a Session UID.')
        this.startupStage = 'session_binding'
        await this.options.onSessionCreated?.(this.sessionId)
        this.event('session_bound')
        this.connectResolvedAt = performance.now()
        this.restartResolvedAt = this.connectResolvedAt
        this.restartResolved = true
        this.event('connect_resolved')
        this.event('session_restart_resolved')
        this.event('official_connect_media_resolved')
      } finally {
        this.connectInFlight = false
        this.resolveConnectFinished?.()
        this.resolveConnectFinished = null
        this.connectFinishedPromise = null
      }
      const session = this.session
      if (!session) throw new Error('Xmax connect completed without a session.')
      this.observeOutput()
      if (this.stopRequested) return (await this.stop('cancelled')) as XmaxSingleShotRunnerResult
      this.inputVideo = this.options.localElement.querySelector('video')
      if (this.inputVideo) {
        this.inputPlaybackCount = this.inputVideo.paused ? 0 : 1
        this.inputPlaybackStartedAt = this.inputVideo.paused ? null : performance.now() - this.startedAt
        this.inputPlayHandler = () => {
          this.inputPlaybackCount += 1
          if (this.inputPlaybackStartedAt == null) this.inputPlaybackStartedAt = performance.now() - this.startedAt
          this.event('input_video_play')
        }
        this.inputEndedHandler = () => {
          this.inputEndedAt = performance.now() - this.startedAt
          this.event('input_video_ended')
          if (this.inputVideo?.loop) this.event('input_loop_boundary')
          else if (this.restartResolved) this.waitForRemoteTail()
        }
        this.inputVideo.addEventListener('play', this.inputPlayHandler, true)
        this.inputVideo.addEventListener('ended', this.inputEndedHandler)
        if (this.inputVideo.ended) this.inputEndedHandler()
      }
      this.startupStage = 'running'
      if (this.remoteStream) {
        try {
          this.startRecording(this.remoteStream)
        } catch (error) {
          const captured = this.captureFailure(
            asError(error, 'Unable to record Xmax output.').message,
            error,
            'remote_stream',
          )
          this.options.onError?.(new Error(captured.message))
          void this.stop('error')
        }
      }
      if (this.stopRequested) return (await this.stop('cancelled')) as XmaxSingleShotRunnerResult
      this.status('Running')
      if (this.firstRemoteFrameAt == null) this.armHardCap('restart_resolved')
      if (this.finishedResult) return this.finishedResult
      return await this.completionPromise
    } catch (error) {
      const rawFailure = asError(error, 'Xmax single-shot run failed.')
      const captured = this.captureFailure(rawFailure.message, error)
      const failure = new Error(captured.message)
      failure.name = captured.errorName
      if (error instanceof XmaxStartupTimeoutError) this.startupTimedOut = true
      if (!this.restartResolved || !this.inputPlaybackAllowed) {
        this.startupFailureStage = this.startupStage
        if (!(error instanceof XmaxStartupTimeoutError)) this.event(`${this.startupStage}_failed`)
      }
      this.options.onError?.(failure)
      const stoppedResult = await this.stop(error instanceof XmaxStartupTimeoutError ? 'startup_timeout' : 'error')
      if (this.shouldPersistFinishedBlobAfterRuntimeFailure(stoppedResult)) {
        this.event('runtime_failure_blob_retained_for_persistence')
        stoppedResult!.diagnostics = this.diagnostics()
        return stoppedResult as XmaxSingleShotRunnerResult
      }
      throw failure
    }
  }
}
