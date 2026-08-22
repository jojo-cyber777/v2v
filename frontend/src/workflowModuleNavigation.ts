export const WORKFLOW_MODULE_IDS = {
  transcription: 'a1-transcription-section',
  translation: 'a1-translation-primary',
  voice: 'a2-voice-section',
  mix: 'a2-mix-section',
  timeline: 'a1-timeline-section',
} as const

function nextFrame() {
  return new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()))
}

function expandDetails(element: HTMLElement) {
  if (element instanceof HTMLDetailsElement) element.open = true
  let parent = element.parentElement
  while (parent) {
    if (parent instanceof HTMLDetailsElement) parent.open = true
    parent = parent.parentElement
  }
}

export async function revealAndScrollToModule(targetId: string) {
  const advanced = document.querySelector<HTMLDetailsElement>('.generation-advanced')
  if (advanced) advanced.open = true
  await nextFrame()

  let target = document.getElementById(targetId)
  if (!target) return false
  expandDetails(target)
  await nextFrame()
  await nextFrame()

  target = document.getElementById(targetId)
  if (!target?.isConnected) return false
  target.scrollIntoView({ behavior: 'smooth', block: 'start' })
  return true
}
