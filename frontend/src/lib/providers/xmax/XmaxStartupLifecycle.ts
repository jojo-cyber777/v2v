export type XmaxStartupStage = 'reference_upload' | 'connect'

export class XmaxStartupTimeoutError extends Error {
  readonly code = 'XMAX_STARTUP_TIMEOUT'
  readonly stage: XmaxStartupStage
  readonly timeoutMs: number

  constructor(stage: XmaxStartupStage, timeoutMs: number) {
    super(`Xmax ${stage} timed out after ${timeoutMs}ms.`)
    this.name = 'XmaxStartupTimeoutError'
    this.stage = stage
    this.timeoutMs = timeoutMs
  }
}

type StartupOperationOptions<T> = {
  stage: XmaxStartupStage
  timeoutMs: number
  operation: () => Promise<T>
  onEvent: (event: string) => void
  onTimeout?: () => void
  onLateResolve?: (value: T) => void | Promise<void>
}

export function runXmaxStartupOperation<T>({
  stage,
  timeoutMs,
  operation,
  onEvent,
  onTimeout,
  onLateResolve,
}: StartupOperationOptions<T>): Promise<T> {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new Error(`Invalid ${stage} timeout.`)
  }

  let terminal = false
  let timedOut = false
  const operationPromise = Promise.resolve().then(operation)

  void operationPromise.then(async (value) => {
    if (!timedOut) return
    onEvent(`${stage}_late_resolved`)
    try {
      await onLateResolve?.(value)
      onEvent(`${stage}_late_cleanup_completed`)
    } catch {
      onEvent(`${stage}_late_cleanup_failed`)
    }
  }, () => {
    if (timedOut) onEvent(`${stage}_late_failed`)
  })

  onEvent(`${stage}_started`)
  return new Promise<T>((resolve, reject) => {
    const timer = globalThis.setTimeout(() => {
      if (terminal) return
      terminal = true
      timedOut = true
      onTimeout?.()
      onEvent(`${stage}_timeout`)
      reject(new XmaxStartupTimeoutError(stage, timeoutMs))
    }, timeoutMs)

    void operationPromise.then((value) => {
      if (terminal) return
      terminal = true
      globalThis.clearTimeout(timer)
      onEvent(`${stage}_completed`)
      resolve(value)
    }, (error) => {
      if (terminal) return
      terminal = true
      globalThis.clearTimeout(timer)
      onEvent(`${stage}_failed`)
      reject(error)
    })
  })
}
