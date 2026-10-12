// Conduit push, Desktop half (#87): tells the host which chat this Hermes
// Desktop window shows while the window is in front, so a reply that finishes
// there doesn't also buzz the phone. Other chats, background jobs, approvals,
// questions and calls notify as before.
//
// "In front" is Desktop's own rule for its native notifications: the window
// is visible and has focus. The report is renewed while that holds and
// dropped as soon as it doesn't; a window that goes away without saying so
// stops counting when its hold runs out on the host.

import { host } from '@hermes/plugin-sdk'

const RENEW_MS = 20_000
const HOLD_S = 60

const randomClient = () => {
  const uuid = globalThis.crypto?.randomUUID?.()
  return uuid ? uuid.replace(/-/g, '') : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`
}

export default {
  id: 'conduit_push',
  name: 'Conduit push',
  description: 'Skips the Conduit "reply ready" push for the chat you are reading in this window.',
  defaultEnabled: true,
  register(ctx) {
    const client = randomClient()
    // What the host last took: null until a report lands (or after one failed).
    let lastSent = null

    const inFront = () => !document.hidden && (typeof document.hasFocus !== 'function' || document.hasFocus())

    const shownIds = () =>
      [host.state.focusedSessionId?.get?.(), host.state.focusedStoredSessionId?.get?.()].filter(
        id => typeof id === 'string' && id.trim()
      )

    const send = force => {
      const ids = inFront() ? shownIds() : []
      const key = ids.join('\n')

      // Only a change goes out at once; the renewal timer refreshes the hold.
      if (!force && key === lastSent) {
        return
      }

      lastSent = key
      ctx
        .rest('/desktop/focus', {
          method: 'PUT',
          body: { client, session_ids: ids, hold_s: ids.length ? HOLD_S : 0 }
        })
        .catch(() => {
          // An older plugin on the host, or the host is unreachable: pushes
          // simply go out as before. The next change or renewal tries again.
          lastSent = null
        })
    }

    const changed = () => send(false)

    ctx.addEventListener(window, 'focus', changed)
    ctx.addEventListener(window, 'blur', changed)
    ctx.addEventListener(document, 'visibilitychange', changed)

    for (const atom of [host.state.focusedSessionId, host.state.focusedStoredSessionId]) {
      const unbind = atom?.listen?.(changed)

      if (typeof unbind === 'function') {
        ctx.onDispose(unbind)
      }
    }

    // Renews a chat on screen; retries a report that didn't land.
    ctx.setInterval(() => send(Boolean(lastSent)), RENEW_MS)

    ctx.onDispose(() => {
      if (lastSent) {
        ctx.rest('/desktop/focus', { method: 'PUT', body: { client, session_ids: [], hold_s: 0 } }).catch(() => {})
      }
    })

    send(true)
  }
}
