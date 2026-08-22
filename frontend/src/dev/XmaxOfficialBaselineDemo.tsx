import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react'
import {
  createVideoFileStream,
  createXmaxClient,
  models,
  type RealtimeSession,
  type VideoFileStream,
  type XmaxClient,
} from '@xmaxai/sdk'
import { browserXmaxCredentialConfigured, requireXmaxTemporaryApiKey, XMAX_TEMP_KEY_REQUIRED } from '../lib/providers/xmax/xmaxBrowserCredential'

const REALTIME_MODEL = 'x2.0'
type StopReason = 'manual' | 'video_ended' | 'hard_cap' | 'sdk_disconnect' | 'client_error' | 'component_unmount' | 'superseded'
type LifecycleEventName = 'run_created' | 'video_metadata_loaded' | 'connect_started' | 'connect_resolved' | 'connect_failed' | 'connect_resolved_after_stop_requested' | 'input_video_discovered' | 'output_video_discovered' | 'first_input_frame' | 'first_output_frame' | 'input_video_ended' | 'output_video_ended' | 'reference_upload_started' | 'reference_upload_resolved' | 'reference_upload_failed' | 'session_restart_started' | 'reference_bound_restart_started' | 'session_restart_resolved' | 'session_restart_failed' | 'output_failed' | 'remote_stream_received' | 'recorder_created' | 'recorder_started' | 'recorder_dataavailable' | 'recorder_stopped' | 'recorded_blob_created' | 'recorded_download_url_created' | 'recorded_file_name' | 'recording_failed' | 'hard_cap_armed' | 'hard_cap_fired' | 'manual_stop_clicked' | 'sdk_disconnect' | 'client_error' | 'session_disconnect_started' | 'session_disconnect_resolved' | 'disconnect_failed' | 'cleanup_failed' | 'media_stream_destroyed' | 'containers_cleared' | 'cleanup_completed'

type DomSnapshot = {
  localConnected: boolean
  remoteConnected: boolean
  localTagName: string
  remoteTagName: string
  localChildElementCount: number
  remoteChildElementCount: number
  localWidth: number
  remoteWidth: number
  localHeight: number
  remoteHeight: number
  localDisplay: string
  remoteDisplay: string
  localVisibility: string
  remoteVisibility: string
  localOpacity: string
  remoteOpacity: string
}

type LifecycleEvent = {
  timestamp: string
  runId: number
  event: LifecycleEventName
  elapsedFromConnectMs: number | null
  videoDurationSeconds: number | null
  sessionRuntimeLimitMs: number | null
  stopReason: string
  sdkDisconnectReason: string
  recordedChunkCount?: number
  recordedFileName?: string
  recordingMimeType?: string
  outputStreamTrackCount?: number
  restartHasRefImage?: boolean
  restartHasPrompt?: boolean
  referenceValueType?: string
  dom?: DomSnapshot
}

type VideoFrameStats = {
  role: 'Input' | 'Output'
  found: boolean
  currentTime: number | null
  duration: number | null
  paused: boolean | null
  ended: boolean | null
  readyState: number | null
  networkState: number | null
  videoWidth: number | null
  videoHeight: number | null
  totalVideoFrames: number | null
  droppedVideoFrames: number | null
  corruptedVideoFrames: number | null
  estimatedPresentedFps: number | null
  estimatedDroppedFps: number | null
}

type FrameDiagnostics = { input: VideoFrameStats | null; output: VideoFrameStats | null }

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : String(error || fallback)
}

function emptyFrameDiagnostics(): FrameDiagnostics {
  return { input: null, output: null }
}

function formatNumber(value: number | null | undefined, digits = 2) {
  return value == null || !Number.isFinite(value) ? '—' : value.toFixed(digits)
}

export default function XmaxOfficialBaselineDemo() {
  const localRef = useRef<HTMLDivElement>(null)
  const remoteRef = useRef<HTMLDivElement>(null)
  const sessionRef = useRef<RealtimeSession | null>(null)
  const mediaStreamRef = useRef<VideoFileStream | null>(null)
  const clientRef = useRef<XmaxClient | null>(null)
  const runIdRef = useRef(0)
  const connectInFlightRef = useRef(false)
  const stopRequestedRef = useRef(false)
  const stopReasonRef = useRef('')
  const sdkDisconnectReasonRef = useRef('')
  const sdkAlreadyDisconnectedRef = useRef(false)
  const stopPromiseRef = useRef<Promise<void> | null>(null)
  const resolveStopRef = useRef<(() => void) | null>(null)
  const stopActiveRunRef = useRef<((reason: StopReason, sdkReason?: string) => Promise<void>) | null>(null)
  const hardCapTimerRef = useRef<number | null>(null)
  const endedTimerRef = useRef<number | null>(null)
  const statsTimerRef = useRef<number | null>(null)
  const mutationObserverRef = useRef<MutationObserver | null>(null)
  const inputVideoElementRef = useRef<HTMLVideoElement | null>(null)
  const outputVideoElementRef = useRef<HTMLVideoElement | null>(null)
  const inputEndedHandlerRef = useRef<(() => void) | null>(null)
  const outputEndedHandlerRef = useRef<(() => void) | null>(null)
  const connectStartedAtRef = useRef<number | null>(null)
  const connectResolvedAtRef = useRef<number | null>(null)
  const stopStartedAtRef = useRef<number | null>(null)
  const sessionRuntimeLimitMsRef = useRef<number | null>(null)
  const videoMetadataRequestRef = useRef(0)
  const restartResolvedAtRef = useRef<number | null>(null)
  const referenceUploadStartedAtRef = useRef<number | null>(null)
  const restartStartedAtRef = useRef<number | null>(null)
  const firstInputFrameRecordedRef = useRef(false)
  const firstOutputFrameRecordedRef = useRef(false)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const recordingStreamRef = useRef<MediaStream | null>(null)
  const recordingChunksRef = useRef<Blob[]>([])
  const recordingStopPromiseRef = useRef<Promise<void> | null>(null)
  const resolveRecordingStopRef = useRef<(() => void) | null>(null)
  const pendingOutputStreamRef = useRef<MediaStream | null>(null)
  const recordingStartedRef = useRef(false)
  const recordingAttemptedRef = useRef(false)
  const recordedDownloadUrlRef = useRef<string | null>(null)
  const recordedFileNameRef = useRef('')
  const previousFrameSampleRef = useRef<{ input: { timestamp: number; total: number; dropped: number } | null; output: { timestamp: number; total: number; dropped: number } | null }>({ input: null, output: null })

  const [videoFile, setVideoFile] = useState<File | null>(null)
  const [referenceFile, setReferenceFile] = useState<File | null>(null)
  const [prompt, setPrompt] = useState('')
  const [status, setStatus] = useState('Ready')
  const [error, setError] = useState('')
  const [isConnecting, setIsConnecting] = useState(false)
  const [isConnected, setIsConnected] = useState(false)
  const [videoDurationSeconds, setVideoDurationSeconds] = useState<number | null>(null)
  const [videoMetadataStatus, setVideoMetadataStatus] = useState('尚未读取')
  const [sessionElapsed, setSessionElapsed] = useState(0)
  const [frameDiagnostics, setFrameDiagnostics] = useState<FrameDiagnostics>(emptyFrameDiagnostics)
  const [events, setEvents] = useState<LifecycleEvent[]>([])
  const [lastStopReason, setLastStopReason] = useState('')
  const [sdkDisconnectReason, setSdkDisconnectReason] = useState('')
  const [waitingForSdkVideo, setWaitingForSdkVideo] = useState(false)
  const [timings, setTimings] = useState({ connectMs: null as number | null, referenceUploadMs: null as number | null, restartMs: null as number | null, firstOutputFromRestartMs: null as number | null })
  const [recordingStatus, setRecordingStatus] = useState<'idle' | 'recording' | 'stopped' | 'failed'>('idle')
  const [recordedChunkCount, setRecordedChunkCount] = useState(0)
  const [recordedBlobSize, setRecordedBlobSize] = useState(0)
  const [recordedFileName, setRecordedFileName] = useState('')
  const [recordedDownloadUrl, setRecordedDownloadUrl] = useState<string | null>(null)
  const [recordingMimeType, setRecordingMimeType] = useState('')
  const [outputStreamTrackCount, setOutputStreamTrackCount] = useState(0)

  const configured = browserXmaxCredentialConfigured()

  const clearTimersAndObservers = useCallback(() => {
    if (hardCapTimerRef.current !== null) window.clearTimeout(hardCapTimerRef.current)
    if (endedTimerRef.current !== null) window.clearTimeout(endedTimerRef.current)
    if (statsTimerRef.current !== null) window.clearInterval(statsTimerRef.current)
    hardCapTimerRef.current = null
    endedTimerRef.current = null
    statsTimerRef.current = null
    mutationObserverRef.current?.disconnect()
    mutationObserverRef.current = null
    const inputVideo = inputVideoElementRef.current
    const endedHandler = inputEndedHandlerRef.current
    if (inputVideo && endedHandler) inputVideo.removeEventListener('ended', endedHandler)
    const outputVideo = outputVideoElementRef.current
    const outputEndedHandler = outputEndedHandlerRef.current
    if (outputVideo && outputEndedHandler) outputVideo.removeEventListener('ended', outputEndedHandler)
    inputVideoElementRef.current = null
    outputVideoElementRef.current = null
    inputEndedHandlerRef.current = null
    outputEndedHandlerRef.current = null
  }, [])

  const clearRenderContainers = useCallback(() => {
    localRef.current?.replaceChildren()
    remoteRef.current?.replaceChildren()
  }, [])

  const readDomSnapshot = useCallback((): DomSnapshot => {
    const local = localRef.current
    const remote = remoteRef.current
    const read = (element: HTMLDivElement | null) => {
      const style = element ? getComputedStyle(element) : null
      return {
        connected: Boolean(element?.isConnected),
        tagName: element?.tagName || '—',
        childElementCount: element?.childElementCount || 0,
        width: element?.clientWidth || 0,
        height: element?.clientHeight || 0,
        display: style?.display || '—',
        visibility: style?.visibility || '—',
        opacity: style?.opacity || '—',
      }
    }
    const localState = read(local)
    const remoteState = read(remote)
    return {
      localConnected: localState.connected,
      remoteConnected: remoteState.connected,
      localTagName: localState.tagName,
      remoteTagName: remoteState.tagName,
      localChildElementCount: localState.childElementCount,
      remoteChildElementCount: remoteState.childElementCount,
      localWidth: localState.width,
      remoteWidth: remoteState.width,
      localHeight: localState.height,
      remoteHeight: remoteState.height,
      localDisplay: localState.display,
      remoteDisplay: remoteState.display,
      localVisibility: localState.visibility,
      remoteVisibility: remoteState.visibility,
      localOpacity: localState.opacity,
      remoteOpacity: remoteState.opacity,
    }
  }, [])

  const recordEvent = useCallback((event: LifecycleEventName, includeDom = false, details?: Pick<LifecycleEvent, 'recordedChunkCount' | 'recordedFileName' | 'recordingMimeType' | 'outputStreamTrackCount' | 'restartHasRefImage' | 'restartHasPrompt' | 'referenceValueType'>) => {
    const now = performance.now()
    const connectStartedAt = connectStartedAtRef.current
    setEvents((current) => [...current, {
      timestamp: new Date().toISOString(),
      runId: runIdRef.current,
      event,
      elapsedFromConnectMs: connectStartedAt == null ? null : Math.round(now - connectStartedAt),
      videoDurationSeconds,
      sessionRuntimeLimitMs: sessionRuntimeLimitMsRef.current,
      stopReason: stopReasonRef.current,
      sdkDisconnectReason: sdkDisconnectReasonRef.current,
      ...(details || {}),
      ...(includeDom ? { dom: readDomSnapshot() } : {}),
    }])
  }, [readDomSnapshot, videoDurationSeconds])

  const clearRecordedDownload = useCallback(() => {
    if (recordedDownloadUrlRef.current) URL.revokeObjectURL(recordedDownloadUrlRef.current)
    recordedDownloadUrlRef.current = null
    recordedFileNameRef.current = ''
    setRecordedDownloadUrl(null)
    setRecordedFileName('')
    setRecordedBlobSize(0)
    setRecordedChunkCount(0)
  }, [])

  const startOutputRecording = useCallback((stream: MediaStream) => {
    if (recordingStartedRef.current || recordingAttemptedRef.current) return
    if (restartResolvedAtRef.current == null) {
      pendingOutputStreamRef.current = stream
      return
    }
    recordingAttemptedRef.current = true
    const videoTracks = stream.getVideoTracks()
    setOutputStreamTrackCount(videoTracks.length)
    if (videoTracks.length === 0) {
      setRecordingStatus('failed')
      setError('无可录制的 Output 视频轨道。')
      recordEvent('recording_failed')
      return
    }
    if (typeof MediaRecorder === 'undefined') {
      setRecordingStatus('failed')
      setError('当前浏览器不支持录制。')
      recordEvent('recording_failed')
      return
    }
    const mimeTypes = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm']
    const mimeType = mimeTypes.find((candidate) => MediaRecorder.isTypeSupported(candidate)) || ''
    try {
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
      recorderRef.current = recorder
      recordingStreamRef.current = stream
      recordingChunksRef.current = []
      recordingStartedRef.current = true
      setRecordedChunkCount(0)
      setRecordedBlobSize(0)
      setRecordingMimeType(recorder.mimeType || mimeType || 'video/webm')
      setRecordingStatus('recording')
      recordEvent('recorder_created', false, { recordingMimeType: recorder.mimeType || mimeType || 'video/webm' })
      recorder.ondataavailable = (event) => {
        if (event.data.size <= 0) return
        recordingChunksRef.current.push(event.data)
        const count = recordingChunksRef.current.length
        setRecordedChunkCount(count)
        if (count === 1) recordEvent('recorder_dataavailable', false, { recordedChunkCount: count })
      }
      recorder.onerror = () => {
        setRecordingStatus('failed')
        setError('Output 录制失败。')
        recordEvent('recording_failed')
        recorderRef.current = null
        recordingStartedRef.current = false
        resolveRecordingStopRef.current?.()
        recordingStopPromiseRef.current = null
        resolveRecordingStopRef.current = null
      }
      recorder.onstop = () => {
        const chunks = recordingChunksRef.current
        const blob = new Blob(chunks, { type: recorder.mimeType || mimeType || 'video/webm' })
        recordEvent('recorder_stopped', false, { recordedChunkCount: chunks.length })
        recorderRef.current = null
        recordingStartedRef.current = false
        if (chunks.length === 0 || blob.size === 0) {
          setRecordingStatus('failed')
          setError('无录制结果：Output 没有产生可用数据。')
        } else {
          const uid = sessionRef.current?.getSessionUid() || 'session'
          const safeUid = uid.replace(/[^a-zA-Z0-9_-]/g, '').slice(-24) || 'session'
          const timestamp = new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 17)
          const fileName = `xmax-official-demo-${timestamp}-${safeUid}.webm`
          const url = URL.createObjectURL(blob)
          clearRecordedDownload()
          recordedDownloadUrlRef.current = url
          recordedFileNameRef.current = fileName
          setRecordedDownloadUrl(url)
          setRecordedFileName(fileName)
          setRecordedBlobSize(blob.size)
          setRecordingStatus('stopped')
          recordEvent('recorded_blob_created')
          recordEvent('recorded_download_url_created')
          recordEvent('recorded_file_name', false, { recordedFileName: fileName })
        }
        resolveRecordingStopRef.current?.()
        recordingStopPromiseRef.current = null
        resolveRecordingStopRef.current = null
      }
      recorder.start()
      recordEvent('recorder_started')
    } catch (reason) {
      recordingStartedRef.current = false
      setRecordingStatus('failed')
      setError(errorMessage(reason, '无法创建 Output 录制器'))
      recordEvent('recording_failed')
    }
  }, [clearRecordedDownload, recordEvent])

  const stopOutputRecording = useCallback(async () => {
    const recorder = recorderRef.current
    if (!recorder || recorder.state === 'inactive') return
    if (recordingStopPromiseRef.current) return recordingStopPromiseRef.current
    const promise = new Promise<void>((resolve) => { resolveRecordingStopRef.current = resolve })
    recordingStopPromiseRef.current = promise
    try {
      recorder.stop()
    } catch (reason) {
      setRecordingStatus('failed')
      setError(errorMessage(reason, '无法停止 Output 录制'))
      recordEvent('recording_failed')
      resolveRecordingStopRef.current?.()
      resolveRecordingStopRef.current = null
      recordingStopPromiseRef.current = null
    }
    return promise
  }, [recordEvent])

  const finalizeStop = useCallback(async (lateSession: RealtimeSession | null = null) => {
    const stopPromise = stopPromiseRef.current
    if (!stopPromise) return
    if (connectInFlightRef.current && !lateSession) return

    await stopOutputRecording()
    if (recordingAttemptedRef.current && !recordedDownloadUrlRef.current) {
      setRecordingStatus('failed')
      setError((current) => current || '无录制结果：Output 没有产生可下载数据。')
    }
    const session = lateSession || sessionRef.current
    sessionRef.current = null
    if (session && !sdkAlreadyDisconnectedRef.current) {
      recordEvent('session_disconnect_started', true)
      try {
        await session.disconnect()
        recordEvent('session_disconnect_resolved', true)
      } catch (reason) {
        setError(errorMessage(reason, 'Xmax Session disconnect 失败'))
        recordEvent('disconnect_failed', true)
      }
    }

    clearTimersAndObservers()
    const mediaStream = mediaStreamRef.current
    mediaStreamRef.current = null
    if (mediaStream) {
      try {
        mediaStream.destroy()
        recordEvent('media_stream_destroyed')
      } catch (reason) {
        setError(errorMessage(reason, '视频流清理失败'))
        recordEvent('cleanup_failed')
      }
    }
    try {
      clearRenderContainers()
    } catch (reason) {
      setError(errorMessage(reason, '容器清理失败'))
      recordEvent('cleanup_failed')
    }
    recordEvent('containers_cleared')
    clientRef.current = null
    recordingStreamRef.current = null
    pendingOutputStreamRef.current = null
    recordingAttemptedRef.current = false
    setIsConnected(false)
    setIsConnecting(false)
    setSessionElapsed(0)
    setWaitingForSdkVideo(false)
    setLastStopReason(stopReasonRef.current)
    setSdkDisconnectReason(sdkDisconnectReasonRef.current)
    setStatus(stopReasonRef.current === 'video_ended' || stopReasonRef.current === 'hard_cap' ? 'Completed' : stopReasonRef.current === 'manual' || stopReasonRef.current === 'superseded' ? 'Ready' : 'Failed')
    recordEvent('cleanup_completed')
    const resolve = resolveStopRef.current
    stopPromiseRef.current = null
    resolveStopRef.current = null
    stopRequestedRef.current = false
    sdkAlreadyDisconnectedRef.current = false
    resolve?.()
  }, [clearRenderContainers, clearTimersAndObservers, recordEvent, stopOutputRecording])

  const stopActiveRun = useCallback((reason: StopReason, sdkReason = '') => {
    if (stopPromiseRef.current) return stopPromiseRef.current
    if (!connectInFlightRef.current && !sessionRef.current && !mediaStreamRef.current && !clientRef.current) return Promise.resolve()
    stopRequestedRef.current = true
    stopReasonRef.current = reason
    if (sdkReason) sdkDisconnectReasonRef.current = sdkReason
    stopStartedAtRef.current = performance.now()
    setStatus('Stopping')
    const promise = new Promise<void>((resolve) => { resolveStopRef.current = resolve })
    stopPromiseRef.current = promise
    void finalizeStop()
    return promise
  }, [finalizeStop])

  stopActiveRunRef.current = stopActiveRun

  useEffect(() => () => {
    ++runIdRef.current
    void stopActiveRunRef.current?.('component_unmount')
    clearRecordedDownload()
  }, [clearRecordedDownload])

  const readVideoStats = useCallback((role: 'Input' | 'Output', video: HTMLVideoElement | null): VideoFrameStats => {
    const base: VideoFrameStats = {
      role,
      found: Boolean(video),
      currentTime: video ? video.currentTime : null,
      duration: video && Number.isFinite(video.duration) ? video.duration : null,
      paused: video ? video.paused : null,
      ended: video ? video.ended : null,
      readyState: video ? video.readyState : null,
      networkState: video ? video.networkState : null,
      videoWidth: video?.videoWidth || null,
      videoHeight: video?.videoHeight || null,
      totalVideoFrames: null,
      droppedVideoFrames: null,
      corruptedVideoFrames: null,
      estimatedPresentedFps: null,
      estimatedDroppedFps: null,
    }
    if (!video || typeof video.getVideoPlaybackQuality !== 'function') return base
    const quality = video.getVideoPlaybackQuality()
    const sampleKey = role === 'Input' ? 'input' : 'output'
    const previous = previousFrameSampleRef.current[sampleKey]
    const timestamp = performance.now()
    const elapsedMs = previous == null ? 0 : timestamp - previous.timestamp
    const estimatedPresentedFps = previous == null || elapsedMs <= 0 ? null : ((quality.totalVideoFrames - previous.total) * 1000) / elapsedMs
    const estimatedDroppedFps = previous == null || elapsedMs <= 0 ? null : ((quality.droppedVideoFrames - previous.dropped) * 1000) / elapsedMs
    previousFrameSampleRef.current[sampleKey] = { timestamp, total: quality.totalVideoFrames, dropped: quality.droppedVideoFrames }
    return {
      ...base,
      totalVideoFrames: quality.totalVideoFrames,
      droppedVideoFrames: quality.droppedVideoFrames,
      corruptedVideoFrames: quality.corruptedVideoFrames,
      estimatedPresentedFps,
      estimatedDroppedFps,
    }
  }, [])

  const updateVideoDiagnostics = useCallback(() => {
    const inputVideo = localRef.current?.querySelector('video') || null
    const outputVideo = remoteRef.current?.querySelector('video') || null
    const previousOutputVideo = outputVideoElementRef.current
    const previousOutputEndedHandler = outputEndedHandlerRef.current
    if (previousOutputVideo && previousOutputEndedHandler && previousOutputVideo !== outputVideo) {
      previousOutputVideo.removeEventListener('ended', previousOutputEndedHandler)
      outputEndedHandlerRef.current = null
    }
    if (inputVideo && inputVideoElementRef.current !== inputVideo) recordEvent('input_video_discovered')
    if (outputVideo && outputVideoElementRef.current !== outputVideo) {
      recordEvent('output_video_discovered')
      const outputEndedHandler = () => {
        recordEvent('output_video_ended')
        void stopActiveRun('video_ended')
      }
      outputVideo.addEventListener('ended', outputEndedHandler)
      outputEndedHandlerRef.current = outputEndedHandler
    }
    inputVideoElementRef.current = inputVideo
    outputVideoElementRef.current = outputVideo
    setWaitingForSdkVideo(!inputVideo || !outputVideo)
    setFrameDiagnostics({ input: readVideoStats('Input', inputVideo), output: readVideoStats('Output', outputVideo) })
    if (inputVideo && !firstInputFrameRecordedRef.current && inputVideo.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && inputVideo.currentTime > 0) {
      firstInputFrameRecordedRef.current = true
      recordEvent('first_input_frame')
    }
    if (outputVideo && !firstOutputFrameRecordedRef.current && outputVideo.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && outputVideo.currentTime > 0) {
      firstOutputFrameRecordedRef.current = true
      recordEvent('first_output_frame')
      const restartResolvedAt = restartResolvedAtRef.current
      if (restartResolvedAt != null) setTimings((current) => ({ ...current, firstOutputFromRestartMs: Math.round(performance.now() - restartResolvedAt) }))
    }
    const capturableOutputVideo = outputVideo as (HTMLVideoElement & { captureStream?: () => MediaStream }) | null
    if (capturableOutputVideo && restartResolvedAtRef.current != null && !recordingAttemptedRef.current && typeof capturableOutputVideo.captureStream === 'function') {
      try {
        startOutputRecording(capturableOutputVideo.captureStream())
      } catch (reason) {
        recordingAttemptedRef.current = true
        setRecordingStatus('failed')
        setError(errorMessage(reason, '无法获取 Output 录制流'))
        recordEvent('recording_failed')
      }
    }
    const startedAt = connectResolvedAtRef.current
    if (startedAt != null) setSessionElapsed(Math.max(0, (performance.now() - startedAt) / 1000))
  }, [readVideoStats, recordEvent, startOutputRecording])

  const scheduleEndedStop = useCallback(() => {
    if (endedTimerRef.current !== null || stopRequestedRef.current) return
    endedTimerRef.current = window.setTimeout(() => {
      endedTimerRef.current = null
      if (!stopRequestedRef.current) void stopActiveRun('video_ended')
    }, 1000)
  }, [stopActiveRun])

  const bindInputVideo = useCallback(() => {
    const inputVideo = localRef.current?.querySelector('video') || null
    const previousVideo = inputVideoElementRef.current
    const previousHandler = inputEndedHandlerRef.current
    if (previousVideo && previousHandler && previousVideo !== inputVideo) previousVideo.removeEventListener('ended', previousHandler)
    if (!inputVideo) {
      inputVideoElementRef.current = null
      setWaitingForSdkVideo(true)
      return
    }
    if (inputVideoElementRef.current !== inputVideo) {
      const endedHandler = () => {
        recordEvent('input_video_ended')
        setStatus('视频播放结束，1 秒后自动停止')
        scheduleEndedStop()
      }
      inputVideo.addEventListener('ended', endedHandler)
      inputEndedHandlerRef.current = endedHandler
      inputVideoElementRef.current = inputVideo
      recordEvent('input_video_discovered')
    }
    if (inputVideo.ended) scheduleEndedStop()
    setWaitingForSdkVideo(!inputVideo || !remoteRef.current?.querySelector('video'))
  }, [recordEvent, scheduleEndedStop])

  const observeSdkVideos = useCallback(() => {
    const localContainer = localRef.current
    const remoteContainer = remoteRef.current
    if (!localContainer || !remoteContainer) return
    const observer = new MutationObserver(() => {
      bindInputVideo()
      updateVideoDiagnostics()
    })
    observer.observe(localContainer, { childList: true, subtree: true })
    observer.observe(remoteContainer, { childList: true, subtree: true })
    mutationObserverRef.current = observer
    bindInputVideo()
    updateVideoDiagnostics()
  }, [bindInputVideo, updateVideoDiagnostics])

  const armRuntimeGuards = useCallback(() => {
    if (videoDurationSeconds == null) return
    const runtimeLimitMs = Math.ceil(videoDurationSeconds * 1000) + 3000
    sessionRuntimeLimitMsRef.current = runtimeLimitMs
    hardCapTimerRef.current = window.setTimeout(() => {
      recordEvent('hard_cap_fired')
      void stopActiveRun('hard_cap')
    }, runtimeLimitMs)
    recordEvent('hard_cap_armed')
    statsTimerRef.current = window.setInterval(updateVideoDiagnostics, 1000)
  }, [recordEvent, stopActiveRun, updateVideoDiagnostics, videoDurationSeconds])

  const destroyMetadataReader = useCallback((reader: HTMLVideoElement, url: string) => {
    reader.removeAttribute('src')
    reader.load()
    URL.revokeObjectURL(url)
  }, [])

  const handleVideoChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] || null
    event.target.value = ''
    if (!file) return
    if (isConnecting || isConnected) {
      setError('请先完成当前运行的清理，再选择新视频。')
      return
    }
    const requestId = ++videoMetadataRequestRef.current
    setVideoFile(file)
    setVideoDurationSeconds(null)
    setVideoMetadataStatus('视频元数据读取中')
    setStatus('视频元数据读取中')
    setError('')
    const url = URL.createObjectURL(file)
    const reader = document.createElement('video')
    reader.preload = 'metadata'
    reader.muted = true
    reader.playsInline = true
    const finish = (duration: number | null) => {
      reader.removeEventListener('loadedmetadata', onLoadedMetadata)
      reader.removeEventListener('error', onError)
      destroyMetadataReader(reader, url)
      if (requestId !== videoMetadataRequestRef.current) return
      if (duration == null || !Number.isFinite(duration) || duration <= 0) {
        setVideoMetadataStatus('无法读取视频时长')
        setStatus('无法读取视频时长')
        setVideoDurationSeconds(null)
        return
      }
      setVideoDurationSeconds(duration)
      setVideoMetadataStatus('已读取')
      setStatus('Ready')
      recordEvent('video_metadata_loaded')
    }
    const onLoadedMetadata = () => finish(Number.isFinite(reader.duration) && reader.duration > 0 ? reader.duration : null)
    const onError = () => finish(null)
    reader.addEventListener('loadedmetadata', onLoadedMetadata)
    reader.addEventListener('error', onError)
    reader.src = url
    reader.load()
  }

  const handleReferenceChange = (event: ChangeEvent<HTMLInputElement>) => {
    setReferenceFile(event.target.files?.[0] || null)
    event.target.value = ''
    setError('')
  }

  const handleConnect = async () => {
    if (!configured) {
      setError(XMAX_TEMP_KEY_REQUIRED)
      return
    }
    if (!videoFile || videoDurationSeconds == null || videoMetadataStatus !== '已读取' || !Number.isFinite(videoDurationSeconds) || videoDurationSeconds <= 0) {
      setError('请先选择能够读取到有效时长的视频。')
      return
    }
    if (!referenceFile) {
      setError('请先选择人物参考图。')
      return
    }
    if (!prompt.trim()) {
      setError('请输入 Prompt。')
      return
    }
    const localElement = localRef.current
    const remoteElement = remoteRef.current
    if (!localElement || !remoteElement || !localElement.isConnected || !remoteElement.isConnected) {
      setError('Input / Output 容器尚未挂载。')
      return
    }

    const runId = ++runIdRef.current
    await stopActiveRun('superseded')
    if (runId !== runIdRef.current) return
    clearRecordedDownload()
    recordingChunksRef.current = []
    pendingOutputStreamRef.current = null
    recordingAttemptedRef.current = false
    recordingStartedRef.current = false
    setRecordingStatus('idle')
    setRecordingMimeType('')
    setOutputStreamTrackCount(0)
    setEvents([])
    setError('')
    setStatus('Connecting')
    setIsConnecting(true)
    setLastStopReason('')
    setSdkDisconnectReason('')
    setFrameDiagnostics(emptyFrameDiagnostics())
    previousFrameSampleRef.current = { input: null, output: null }
    stopReasonRef.current = ''
    sdkDisconnectReasonRef.current = ''
    sdkAlreadyDisconnectedRef.current = false
    connectStartedAtRef.current = performance.now()
    connectResolvedAtRef.current = null
    restartResolvedAtRef.current = null
    referenceUploadStartedAtRef.current = null
    restartStartedAtRef.current = null
    firstInputFrameRecordedRef.current = false
    firstOutputFrameRecordedRef.current = false
    sessionRuntimeLimitMsRef.current = null
    setTimings({ connectMs: null, referenceUploadMs: null, restartMs: null, firstOutputFromRestartMs: null })
    recordEvent('run_created')
    recordEvent('connect_started', true)

    try {
      const apiKey = requireXmaxTemporaryApiKey()
      const client = createXmaxClient({
        apiKey,
        onError: (message) => {
          recordEvent('client_error')
          setError(message)
          void stopActiveRun('client_error')
        },
      })
      clientRef.current = client

      connectInFlightRef.current = true
      const mediaFileStream = await createVideoFileStream(videoFile)
      mediaStreamRef.current = mediaFileStream
      const targetSize: [number, number] = [mediaFileStream.width, mediaFileStream.height]
      if (stopRequestedRef.current || runId !== runIdRef.current) return
      const session = await client.realtime.connect(mediaFileStream.previewStream, {
        model: models.realtime(REALTIME_MODEL),
        stream: {
          width: mediaFileStream.width,
          height: mediaFileStream.height,
          fps: mediaFileStream.fps,
        },
        render: {
          localContainer: localElement,
          remoteContainer: remoteElement,
        },
        onRemoteStream: (stream) => {
          const trackCount = stream.getVideoTracks().length
          setOutputStreamTrackCount(trackCount)
          recordEvent('remote_stream_received', false, { outputStreamTrackCount: trackCount })
          startOutputRecording(stream)
        },
        context: { prompt: prompt.trim() },
        autoStart: false,
        onDisconnect: (reason) => {
          sdkDisconnectReasonRef.current = reason
          sdkAlreadyDisconnectedRef.current = true
          setSdkDisconnectReason(reason)
          recordEvent('sdk_disconnect')
          if (!remoteRef.current?.querySelector('video')) recordEvent('output_failed')
          void stopActiveRun('sdk_disconnect', reason)
        },
      })
      connectResolvedAtRef.current = performance.now()
      recordEvent('connect_resolved', true)
      setTimings((current) => ({ ...current, connectMs: connectStartedAtRef.current == null ? null : Math.round(performance.now() - connectStartedAtRef.current) }))
      sessionRef.current = session
      if (stopRequestedRef.current || runId !== runIdRef.current) {
        recordEvent('connect_resolved_after_stop_requested')
        await finalizeStop(session)
        return
      }

      observeSdkVideos()
      setStatus('Uploading reference')
      const referenceUploadStartedAt = performance.now()
      referenceUploadStartedAtRef.current = referenceUploadStartedAt
      recordEvent('reference_upload_started', true)
      let uploadedReference: Awaited<ReturnType<XmaxClient['files']['uploadAndCheckImage']>>
      try {
        uploadedReference = await client.files.uploadAndCheckImage(referenceFile)
        setTimings((current) => ({ ...current, referenceUploadMs: Math.round(performance.now() - referenceUploadStartedAt) }))
        recordEvent('reference_upload_resolved')
      } catch (reason) {
        recordEvent('reference_upload_failed')
        setError(errorMessage(reason, '参考图上传失败'))
        connectInFlightRef.current = false
        await stopActiveRun('client_error')
        return
      }

      if (stopRequestedRef.current || runId !== runIdRef.current) return
      setStatus('Starting transformation')
      const restartStartedAt = performance.now()
      restartStartedAtRef.current = restartStartedAt
      recordEvent('session_restart_started', true)
      recordEvent('reference_bound_restart_started', false, {
        restartHasRefImage: Boolean(uploadedReference.trim()),
        restartHasPrompt: Boolean(prompt.trim()),
        referenceValueType: typeof uploadedReference,
      })
      try {
        await session.start({
          prompt: prompt.trim(),
          refImageUrl: uploadedReference,
        })
        restartResolvedAtRef.current = performance.now()
        setTimings((current) => ({ ...current, restartMs: Math.round(performance.now() - restartStartedAt) }))
        recordEvent('session_restart_resolved', true)
        const pendingOutputStream = pendingOutputStreamRef.current
        pendingOutputStreamRef.current = null
        if (pendingOutputStream) startOutputRecording(pendingOutputStream)
      } catch (reason) {
        recordEvent('session_restart_failed')
        setError(errorMessage(reason, 'Xmax transformation 启动失败'))
        connectInFlightRef.current = false
        await stopActiveRun('client_error')
        return
      }

      if (stopRequestedRef.current || runId !== runIdRef.current) return
      connectInFlightRef.current = false
      setIsConnected(true)
      setIsConnecting(false)
      setStatus('Running')
      updateVideoDiagnostics()
      armRuntimeGuards()
    } catch (reason) {
      connectInFlightRef.current = false
      recordEvent('connect_failed')
      if (stopRequestedRef.current || runId !== runIdRef.current) {
        await finalizeStop()
        return
      }
      setError(errorMessage(reason, '视频连接失败'))
      await stopActiveRun('client_error')
      setStatus('Failed')
    } finally {
      connectInFlightRef.current = false
      if (runId === runIdRef.current) setIsConnecting(false)
      if (stopRequestedRef.current) await finalizeStop()
    }
  }

  const handleStop = async () => {
    ++runIdRef.current
    recordEvent('manual_stop_clicked')
    await stopActiveRun('manual')
    if (!stopPromiseRef.current) setStatus('Ready')
  }

  const currentRuntimeLimit = sessionRuntimeLimitMsRef.current == null ? null : sessionRuntimeLimitMsRef.current / 1000
  const input = frameDiagnostics.input
  const output = frameDiagnostics.output

  return (
    <main className="xmax-baseline-page">
      <section className="xmax-baseline-panel">
        <div className="xmax-baseline-heading">
          <div>
            <span className="section-label">DEVELOPMENT ONLY</span>
            <h1>Xmax Official 1.0.6 Baseline</h1>
            <p>隔离页面，仅验证官方视频输入与人物参考图接口。页面加载不会创建 Client、Session 或网络请求。</p>
          </div>
          <span className="xmax-configured">Xmax configured: {String(configured)}</span>
        </div>

        <div className="xmax-baseline-controls">
          <label className="xmax-baseline-field">
            <span>视频文件</span>
            <input type="file" accept="video/mp4,video/quicktime,video/*" onChange={handleVideoChange} disabled={isConnecting || isConnected} />
            <small>{videoFile?.name || '未选择'}</small>
          </label>
          <label className="xmax-baseline-field">
            <span>人物参考图</span>
            <input type="file" accept="image/png,image/jpeg,image/webp" onChange={handleReferenceChange} disabled={isConnecting} />
            <small>{referenceFile?.name || '未选择'}</small>
          </label>
          <label className="xmax-baseline-field xmax-baseline-prompt">
            <span>Prompt</span>
            <input value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="请输入人物替换 Prompt" disabled={isConnecting || isConnected} />
          </label>
        </div>

        <div className="xmax-baseline-actions">
          <button type="button" className="primary-button" onClick={() => void handleConnect()} disabled={isConnecting || isConnected || !videoFile || videoDurationSeconds == null || videoMetadataStatus !== '已读取' || !referenceFile || !prompt.trim()}>Start transformation</button>
          <button type="button" className="secondary-button" onClick={() => void handleStop()} disabled={!isConnecting && !isConnected}>Stop</button>
          <span className="xmax-baseline-status">状态：{status}</span>
        </div>

        <div className="xmax-baseline-meta">
          <span>视频时长：{videoDurationSeconds == null ? (videoMetadataStatus === '无法读取视频时长' ? '无法读取视频时长' : '—') : `${videoDurationSeconds.toFixed(2)} 秒`}</span>
          <span>Session elapsed：{sessionElapsed.toFixed(2)} 秒</span>
          <span>运行上限：{currentRuntimeLimit == null ? '—' : `${currentRuntimeLimit.toFixed(2)} 秒`}</span>
          <span>Connect：{timings.connectMs == null ? '—' : `${timings.connectMs}ms`}</span>
          <span>Reference upload：{timings.referenceUploadMs == null ? '—' : `${timings.referenceUploadMs}ms`}</span>
          <span>Restart：{timings.restartMs == null ? '—' : `${timings.restartMs}ms`}</span>
          <span>Restart → first output：{timings.firstOutputFromRestartMs == null ? '—' : `${timings.firstOutputFromRestartMs}ms`}</span>
          {waitingForSdkVideo && isConnected && <span>等待 SDK 视频节点</span>}
        </div>
        {error && <div className="xmax-warning">{error}</div>}

        <div className="xmax-baseline-stage">
          <div className="xmax-baseline-surface">
            <span>Input</span>
            <div ref={localRef} className="xmax-baseline-container" />
          </div>
          <div className="xmax-baseline-surface">
            <span>Output</span>
            <div ref={remoteRef} className="xmax-baseline-container" />
          </div>
        </div>

        <div className="xmax-baseline-diagnostics">
          <div className="xmax-baseline-diagnostic-card">
            <strong>Input frame diagnostics</strong>
            <span>currentTime: {formatNumber(input?.currentTime)}</span>
            <span>total frames: {formatNumber(input?.totalVideoFrames, 0)}</span>
            <span>dropped frames: {formatNumber(input?.droppedVideoFrames, 0)}</span>
            <span>estimated FPS: {formatNumber(input?.estimatedPresentedFps)}</span>
          </div>
          <div className="xmax-baseline-diagnostic-card">
            <strong>Output frame diagnostics</strong>
            <span>currentTime: {formatNumber(output?.currentTime)}</span>
            <span>total frames: {formatNumber(output?.totalVideoFrames, 0)}</span>
            <span>dropped frames: {formatNumber(output?.droppedVideoFrames, 0)}</span>
            <span>estimated FPS: {formatNumber(output?.estimatedPresentedFps)}</span>
          </div>
          <div className="xmax-baseline-diagnostic-card">
            <strong>Stop diagnostics</strong>
            <span>Last stop reason: {lastStopReason || '—'}</span>
            <span>SDK disconnect reason: {sdkDisconnectReason || '—'}</span>
            <span>Events: {events.length}</span>
          </div>
          <div className="xmax-baseline-diagnostic-card">
            <strong>Output recording</strong>
            <span>Recording: {recordingStatus}</span>
            <span>Output stream video tracks: {outputStreamTrackCount}</span>
            <span>Recorded chunks: {recordedChunkCount}</span>
            <span>Recorded blob size: {recordedBlobSize > 0 ? `${recordedBlobSize} bytes` : '—'}</span>
            <span>MediaRecorder MIME: {recordingMimeType || '—'}</span>
            <span>File: {recordedFileName || '—'}</span>
            {recordedDownloadUrl ? <a className="secondary-button" href={recordedDownloadUrl} download={recordedFileName}>Download output</a> : <button type="button" className="secondary-button" disabled>Download output</button>}
            {recordedDownloadUrl && <video className="xmax-baseline-recorded-preview" controls src={recordedDownloadUrl} />}
          </div>
        </div>

        <details className="xmax-baseline-events">
          <summary>Lifecycle events</summary>
          <div>{events.length ? events.map((item, index) => <div key={`${item.timestamp}-${index}`}><span>{item.timestamp}</span><strong>{item.event}</strong><small>run {item.runId} · +{item.elapsedFromConnectMs == null ? '—' : item.elapsedFromConnectMs}ms</small></div>) : <span>暂无记录</span>}</div>
        </details>
      </section>
    </main>
  )
}
