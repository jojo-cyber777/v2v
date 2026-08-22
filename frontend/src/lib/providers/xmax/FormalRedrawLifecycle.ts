export const FORMAL_REDRAW_REQUEST_EVENT = 'formal-xmax-redraw'

export type FormalRedrawPhase =
  | 'redraw_requested'
  | 'redraw_acknowledged'
  | 'redraw_started'
  | 'redraw_finished'
  | 'redraw_failed'

export type FormalRedrawRequest = {
  requestId: string
  taskId: string
  unitId: string
  requestedPointsLimit: number
}

export type FormalRedrawRequestDetail = FormalRedrawRequest & {
  acknowledge: () => void
  started: () => void
  finish: () => void
  fail: (reason: unknown) => void
}

type Level2Identity = {
  crop?: { x: number; y: number; width: number; height: number } | null
  scale?: { width: number; height: number } | null
  preprocessedSourceSha256?: string | null
}

type RequestOptions = FormalRedrawRequest & {
  target: EventTarget
  onPhase?: (phase: FormalRedrawPhase, requestId: string) => void
}

export class FormalRedrawRequestEvent extends Event {
  constructor(public readonly detail: FormalRedrawRequestDetail) {
    super(FORMAL_REDRAW_REQUEST_EVENT)
  }
}

const errorMessage = (reason: unknown) => reason instanceof Error ? reason.message : String(reason)

export function requestFormalRedraw(options: RequestOptions): Promise<void> {
  return new Promise((resolve, reject) => {
    let acknowledged = false
    let terminal = false
    const phase = (value: FormalRedrawPhase) => options.onPhase?.(value, options.requestId)
    const fail = (reason: unknown) => {
      if (terminal) return
      terminal = true
      phase('redraw_failed')
      reject(new Error(errorMessage(reason)))
    }
    const detail: FormalRedrawRequestDetail = {
      requestId: options.requestId,
      taskId: options.taskId,
      unitId: options.unitId,
      requestedPointsLimit: options.requestedPointsLimit,
      acknowledge: () => {
        if (terminal || acknowledged) return
        acknowledged = true
        phase('redraw_acknowledged')
      },
      started: () => {
        if (terminal || !acknowledged) return
        phase('redraw_started')
      },
      finish: () => {
        if (terminal || !acknowledged) return
        terminal = true
        phase('redraw_finished')
        resolve()
      },
      fail,
    }
    phase('redraw_requested')
    try {
      options.target.dispatchEvent(new FormalRedrawRequestEvent(detail))
    } catch (reason) {
      fail(reason)
      return
    }
    queueMicrotask(() => {
      if (!acknowledged) fail(new Error('The Xmax redraw handler is not available on this page.'))
    })
  })
}

export function formalRedrawDetail(event: Event): FormalRedrawRequestDetail | null {
  return event instanceof FormalRedrawRequestEvent ? event.detail : null
}

export function consumeFormalRedrawRequest(
  event: Event,
  handler: (detail: FormalRedrawRequestDetail) => Promise<{ status: 'finished' | 'failed'; error?: string }>,
): boolean {
  const detail = formalRedrawDetail(event)
  if (!detail) return false
  detail.acknowledge()
  detail.started()
  void Promise.resolve()
    .then(() => handler(detail))
    .then((result) => {
      if (result.status === 'failed') detail.fail(new Error(result.error || 'The Xmax redraw failed.'))
      else detail.finish()
    }, detail.fail)
  return true
}

export function level2RouteIdentityMatches(left: Level2Identity, right: Level2Identity): boolean {
  return Boolean(
    left.crop
    && right.crop
    && left.crop.x === right.crop.x
    && left.crop.y === right.crop.y
    && left.crop.width === right.crop.width
    && left.crop.height === right.crop.height
    && left.scale
    && right.scale
    && left.scale.width === right.scale.width
    && left.scale.height === right.scale.height
    && left.preprocessedSourceSha256
    && right.preprocessedSourceSha256
    && left.preprocessedSourceSha256.toLowerCase() === right.preprocessedSourceSha256.toLowerCase(),
  )
}
