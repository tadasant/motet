// Submit the waitlist form without leaving the page.
//
// An enhancement, not a requirement: every form here is a real <form> posting to the API,
// and without this script the browser posts it natively and lands on the small page the API
// answers with. With it, the same form body is sent with `fetch` and the answer is shown in
// place.
//
// The request stays a CORS "simple request" — a URL-encoded body and an `Accept` header,
// nothing else — so the browser sends it to the API's origin with no preflight, and the
// API's `Access-Control-Allow-Origin: *` on that one route lets this script read the reply.
// Adding a custom header or a JSON body would turn it into a preflighted request the API
// refuses. See api/src/motet_api/waitlist.py.

const JOINED = "You’re on the list. We’ll write when there’s a place for you."
const UNREACHABLE = 'We couldn’t reach Motet just now. Try again in a moment.'

for (const form of document.querySelectorAll('form[data-waitlist]')) {
  const status = form.querySelector('[data-waitlist-status]')
  const button = form.querySelector('button[type="submit"]')

  form.addEventListener('submit', async (event) => {
    event.preventDefault()
    if (form.dataset.state === 'sending') return

    form.dataset.state = 'sending'
    button.disabled = true
    status.textContent = 'Joining…'

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: new URLSearchParams(new FormData(form)),
        headers: { Accept: 'application/json' },
      })
      if (response.ok) {
        form.dataset.state = 'joined'
        status.textContent = JOINED
        form.reset()
        return
      }
      const body = await response.json().catch(() => ({}))
      form.dataset.state = 'error'
      status.textContent = typeof body.detail === 'string' ? body.detail : UNREACHABLE
    } catch {
      form.dataset.state = 'error'
      status.textContent = UNREACHABLE
    } finally {
      if (form.dataset.state !== 'joined') button.disabled = false
    }
  })
}
