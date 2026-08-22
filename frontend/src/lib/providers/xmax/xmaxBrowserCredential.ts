export const XMAX_TEMP_KEY_REQUIRED = 'XMAX_TEMP_KEY_REQUIRED'

export function browserXmaxCredentialConfigured(): boolean {
  return false
}

export function requireXmaxTemporaryApiKey(): string {
  throw new Error(`${XMAX_TEMP_KEY_REQUIRED}: a backend-issued temporary Xmax key is required.`)
}
