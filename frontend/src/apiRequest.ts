export type ApiRequestInit = Omit<RequestInit, 'body'> & {
  body?: BodyInit | object | null
}

export type ApiFailureKind =
  | 'network_unreachable'
  | 'request_cancelled'
  | 'payload_too_large'
  | 'authentication_required'
  | 'csrf_failed'
  | 'forbidden'
  | 'not_found'
  | 'conflict'
  | 'validation_failed'
  | 'origin_busy'
  | 'server_error'
  | 'http_error'
  | 'invalid_json_response'

export class ApiRequestError extends Error {
  readonly kind: ApiFailureKind
  readonly status: number | null
  readonly responseBody: string | null

  constructor(kind: ApiFailureKind, message: string, options: { status?: number | null; responseBody?: string | null; cause?: unknown } = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause })
    this.name = 'ApiRequestError'
    this.kind = kind
    this.status = options.status ?? null
    this.responseBody = options.responseBody ?? null
  }
}

export type EpisodeUploadState = {
  upload_status?: string | null
  video_path?: string | null
  video_filename?: string | null
  file_size?: number | null
}

export function isEpisodeUploadComplete(episode: EpisodeUploadState | null | undefined): boolean {
  const fileSize = Number(episode?.file_size)
  return episode?.upload_status === 'completed'
    && Boolean(episode.video_path?.trim())
    && Boolean(episode.video_filename?.trim())
    && Number.isFinite(fileSize)
    && fileSize > 0
}

function responseErrorCode(error: ApiRequestError): string {
  if (!error.responseBody) return ''
  try {
    const payload = JSON.parse(error.responseBody) as {
      detail?: string | { code?: unknown }
      code?: unknown
    }
    const value = typeof payload.detail === 'object' && payload.detail
      ? payload.detail.code
      : payload.code
    return typeof value === 'string' ? value : ''
  } catch {
    return ''
  }
}

/** Keep provider and credential details out of customer-visible generation errors. */
export function customerGenerationFailureMessage(error: unknown): string {
  if (!(error instanceof ApiRequestError)) return '生成服务暂时不可用，请稍后重试。'
  const code = responseErrorCode(error)
  if (code === 'INSUFFICIENT_AVAILABLE_CREDITS') return '积分不足，请先充值后再试。'
  if (error.kind === 'authentication_required') return '登录状态已失效，请重新登录。'
  if (error.kind === 'request_cancelled') return '生成请求已取消，请确认后重新发起。'
  return '生成服务暂时不可用，请稍后重试。'
}

export function classifyFetchFailure(error: unknown, signal?: AbortSignal | null): ApiRequestError {
  const name = error instanceof Error ? error.name : ''
  const detail = error instanceof Error ? error.message : String(error)
  if (signal?.aborted || name === 'AbortError') {
    return new ApiRequestError('request_cancelled', `Request cancelled before a response was received: ${detail}`, { cause: error })
  }
  return new ApiRequestError('network_unreachable', `Unable to reach the API before an HTTP response was received: ${detail}`, { cause: error })
}

export function classifyHttpFailure(status: number, body: string, contentType = ''): ApiRequestError {
  const normalized = body.toLowerCase()
  const isHtmlResponse = contentType.toLowerCase().includes('text/html')
    || /^\s*<!doctype\b/i.test(body)
    || /^\s*<html\b/i.test(body)
  if (status === 413) {
    const message = '\u6587\u4ef6\u8fc7\u5927\uff0c\u4e0a\u4f20\u672a\u5b8c\u6210\u3002'
    return new ApiRequestError(
      'payload_too_large',
      message,
      {
        status,
        responseBody: JSON.stringify({ detail: { code: 'UPLOAD_TOO_LARGE', message } }),
      },
    )
  }
  if (status === 524 || isHtmlResponse) {
    const message = 'The server is busy processing a heavy task. Please try again shortly.'
    return new ApiRequestError(
      'origin_busy',
      message,
      {
        status,
        responseBody: JSON.stringify({ detail: { code: 'ORIGIN_BUSY', message } }),
      },
    )
  }
  if (status === 401) return new ApiRequestError('authentication_required', 'Authentication is required.', { status, responseBody: body })
  if (status === 403 && normalized.includes('csrf')) return new ApiRequestError('csrf_failed', 'The request was rejected because the CSRF token is missing or invalid.', { status, responseBody: body })
  if (status === 403) return new ApiRequestError('forbidden', 'The current account is not allowed to perform this action.', { status, responseBody: body })
  if (status === 404) return new ApiRequestError('not_found', 'The requested resource was not found or is not owned by this account.', { status, responseBody: body })
  if (status === 409) return new ApiRequestError('conflict', 'The request conflicts with the current resource state.', { status, responseBody: body })
  if (status === 422) return new ApiRequestError('validation_failed', 'The API rejected the request data.', { status, responseBody: body })
  if (status >= 500) return new ApiRequestError('server_error', 'The API failed while processing the request.', { status, responseBody: body })
  return new ApiRequestError('http_error', `The API returned HTTP ${status}.`, { status, responseBody: body })
}

export function invalidJsonResponse(status: number, body: string, cause: unknown): ApiRequestError {
  return new ApiRequestError('invalid_json_response', 'The API returned an invalid JSON response.', { status, responseBody: body, cause })
}

export function prepareApiRequest(init?: ApiRequestInit): RequestInit | undefined {
  if (!init) return { credentials: 'include' }

  const { body, ...request } = init
  const headers = new Headers(init.headers)
  const method = (init.method || 'GET').toUpperCase()
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const cookie = typeof document === 'undefined' ? '' : document.cookie
    const csrfToken = cookie.split('; ').find((item) => item.startsWith('drama_csrf='))?.split('=', 2)[1]
    if (csrfToken && !headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', decodeURIComponent(csrfToken))
  }
  const isPlainObject = body !== null
    && typeof body === 'object'
    && Object.getPrototypeOf(body) === Object.prototype

  if (isPlainObject) {
    if (!headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json')
    }
    return {
      ...request,
      credentials: 'include',
      headers,
      body: JSON.stringify(body),
    }
  }

  return {
    ...request,
    credentials: 'include',
    headers,
    body: body as BodyInit | null | undefined,
  }
}
